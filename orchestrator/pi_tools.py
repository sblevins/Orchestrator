"""Owned Pi launcher and descriptor-relative file broker with explicit owned tools.

This limits model tools, not other same-user processes or the Pi process itself.
"""

import contextlib
import fnmatch
import hashlib
import importlib.util
import itertools
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PI_VERSION = "0.99.2"
MAX_BYTES = 1024 * 1024
MAX_RESULTS = 1000
MAX_VISITED = 10000
CONTROL_NAMES = {
    "agents",
    "claude",
    "auth.json",
    "credentials.json",
    "credentials",
    "token",
    "tokens",
    "secrets",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "_netrc",
}
READ_TOOLS = {"read", "find", "grep", "ls"}
WRITE_TOOLS = {"edit", "write"}


class PolicyError(ValueError):
    """The request exceeds the broker's authority."""


class AuthorityError(PolicyError):
    """A request explicitly attempts to exceed the configured file roots."""


class UnsupportedTextFile(PolicyError):
    """Binary or oversized data is omitted from recursive text searches."""


def decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise PolicyError("duplicate JSON key")
            result[key] = item
        return result

    def invalid(_value):
        raise PolicyError("nonfinite JSON number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def plain_directory(path):
    """Open every absolute directory component without following any symlink."""
    path = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def forbidden(part, write=False, trusted=False):
    normalized = part.lower()
    if trusted:
        return (
            normalized in (CONTROL_NAMES - {"agents", "claude"})
            or normalized
            in {
                ".git",
                ".orchestrator",
                ".ssh",
                ".aws",
                ".gnupg",
                ".docker",
                ".kube",
                ".npmrc",
                ".pypirc",
                ".netrc",
                ".pgpass",
                ".git-credentials",
            }
            or normalized.startswith((".env", "credentials.", "secrets."))
            or normalized.endswith((".pem", ".key", ".p12", ".pfx"))
        )
    if not write and normalized in {"agents.md", "claude.md"}:
        return False
    return (
        part.startswith(".")
        or normalized in CONTROL_NAMES
        or normalized.startswith(("agents.", "claude.", "credentials.", "secrets."))
        or normalized.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def bounded_integer(value, default, maximum):
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= maximum:
        raise PolicyError("invalid bounded integer")
    return value


class FileBroker:
    """Pin roots and open all tool paths relative to no-follow directory FDs."""

    def __init__(self, options):
        self.cwd = Path(os.path.abspath(options["cwd"]))
        self.project = Path(os.path.abspath(options.get("project_root") or self.cwd))
        self.mode = options["mode"]
        self.trusted = options.get("trusted") is True
        self.commands = options.get("commands")
        self.worker_context = options.get("worker_context")
        self.images = options.get("images", {})
        self.image_state_directory = options.get("image_state_directory")
        if self.mode not in ("read", "write"):
            raise PolicyError("invalid tool mode")
        if self.mode == "write" and (
            self.cwd == self.project
            or self.project in self.cwd.parents
            or self.cwd in self.project.parents
        ):
            raise PolicyError("write worktree must be separate from source checkout")
        context_policy = owned_module("read_context")
        private_paths = [options[key] for key in ("auth_path", "agent_dir") if options.get(key)]
        if self.mode == "read" and self.worker_context is None and self.cwd != self.project:
            private_paths.append(self.cwd)
        try:
            self.read_roots = context_policy.validate_read_roots(
                options.get("read_roots", []),
                require_available=True,
                excluded_paths=private_paths,
            )
        except ValueError as error:
            raise PolicyError(str(error)) from error
        if self.read_roots and (self.mode != "read" or self.worker_context is not None):
            raise PolicyError("Additional read roots are only available to read-only specialists")
        # Deny private subtrees, not ordinary source above them. The supervisor
        # may coordinate its own checkout, with private runs inside that project.
        self.private_paths = {
            candidate
            for path in private_paths
            for candidate in (Path(os.path.abspath(path)), Path(path).resolve())
        }
        # Review tasks run from private supervisor directories. Those directories
        # contain task metadata, not model-readable project material.
        if self.mode == "read" and self.worker_context is None:
            self.cwd = self.project
        self.roots = {}
        try:
            for root in {
                self.cwd,
                self.project,
                *(Path(entry["path"]) for entry in self.read_roots),
            }:
                self.roots[root] = plain_directory(root)
        except BaseException:
            self.close()
            raise

    def close(self):
        for descriptor in self.roots.values():
            os.close(descriptor)
        self.roots.clear()

    def is_private(self, path):
        return any(path == private or private in path.parents for private in self.private_paths)

    def check_private(self, path):
        if self.is_private(path):
            raise AuthorityError("authentication and private runtime paths are forbidden")

    def locate(self, value, write=False):
        if not isinstance(value, str) or not value or "\x00" in value or "~" in value:
            raise PolicyError("invalid tool path")
        supplied = Path(value)
        if ".." in supplied.parts:
            raise AuthorityError("parent traversal is forbidden")
        absolute = supplied if supplied.is_absolute() else self.cwd / supplied
        if self.trusted or not write:
            # Resolve ordinary project aliases, then recheck the resolved root and path policy.
            # Restricted writes retain no-follow traversal throughout.
            try:
                absolute = absolute.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise PolicyError(
                    "Cannot resolve this project path; check for a symlink loop"
                ) from error
        candidates = [self.cwd] if write else sorted(self.roots, key=lambda item: -len(item.parts))
        for root in candidates:
            try:
                relative = absolute.relative_to(root)
            except ValueError:
                continue
            self.check_private(absolute)
            if any(forbidden(part, write=write, trusted=self.trusted) for part in relative.parts):
                raise AuthorityError("control files and hidden paths are forbidden")
            if write and (self.mode != "write" or not relative.parts):
                raise AuthorityError("write permission denied")
            return root, relative.parts
        raise AuthorityError("path is outside allowed roots")

    @contextlib.contextmanager
    def directory(self, root, parts, create=False):
        self.check_private(root.joinpath(*parts))
        descriptor = os.dup(self.roots[root])
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                following = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = following
            yield descriptor
        finally:
            os.close(descriptor)

    def file_text(self, root, parts):
        self.check_private(root.joinpath(*parts))
        if not parts:
            raise PolicyError("expected a regular file")
        with self.directory(root, parts[:-1]) as parent:
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise PolicyError("only singly-linked regular files are allowed")
                if metadata.st_size > MAX_BYTES:
                    raise UnsupportedTextFile("file exceeds tool size limit")
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    content = stream.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise UnsupportedTextFile("file exceeds tool size limit")
                try:
                    decoded = content.decode("utf-8", "strict")
                except UnicodeError as error:
                    raise UnsupportedTextFile("binary files are not supported") from error
                if "\x00" in decoded:
                    raise UnsupportedTextFile("binary files are not supported")
                return decoded
            finally:
                os.close(descriptor)

    def write_text(self, root, parts, content):
        self.check_private(root.joinpath(*parts))
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_BYTES:
            raise PolicyError("invalid or oversized file content")
        with self.directory(root, parts[:-1], create=True) as parent:
            permissions = 0o600
            try:
                current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                    raise PolicyError("write target must be a singly-linked regular file")
                permissions = stat.S_IMODE(current.st_mode) & 0o777
            except FileNotFoundError:
                pass
            temporary = ".orchestrator-write-" + os.urandom(12).hex()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                    stream.flush()
                    os.fchmod(stream.fileno(), permissions)
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)

    def prepare_image(self, value, image_format):
        """Validate the exact destination before making a billable provider request."""
        root, parts = self.locate(value, write=True)
        suffixes = {"png": {".png"}, "jpeg": {".jpg", ".jpeg"}}
        if not parts or Path(parts[-1]).suffix.lower() not in suffixes.get(image_format, set()):
            raise PolicyError("Image destination suffix must match png or jpeg format")
        with self.directory(root, parts[:-1], create=True) as directory:
            try:
                os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return root, parts
            raise PolicyError("Image destination already exists; choose a new output path")

    def save_image(self, target, content):
        """Publish only owned-provider image bytes, never a caller-supplied binary tool."""
        root, parts = target
        # Revalidate authority even if another in-process caller manufactured a target.
        checked_root, checked_parts = self.locate(str(root.joinpath(*parts)), write=True)
        if checked_root != root or checked_parts != parts or not parts:
            raise PolicyError("Image destination changed")
        if not isinstance(content, bytes) or not 1 <= len(content) <= 16 * MAX_BYTES:
            raise PolicyError("Generated image exceeds the 16 MiB artifact limit")
        with self.directory(root, parts[:-1]) as directory:
            temporary = ".orchestrator-image-" + os.urandom(12).hex()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # No-clobber publication also rejects symlinks and targets created in flight.
                os.link(
                    temporary,
                    parts[-1],
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.fsync(directory)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        return {
            "path": "/".join(parts),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def verify_image(self, artifact):
        try:
            root, parts = self.locate(artifact["path"], write=True)
            with self.directory(root, parts[:-1]) as directory:
                descriptor = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or metadata.st_size != artifact["bytes"]
                        or metadata.st_size > 16 * MAX_BYTES
                    ):
                        return False
                    return (
                        hashlib.sha256(stream.read(16 * MAX_BYTES + 1)).hexdigest()
                        == artifact["sha256"]
                    )
        except (ValueError, OSError, KeyError, TypeError):
            return False

    def generate_image(self, arguments):
        if self.mode != "write" or self.images.get("enabled") is not True:
            raise PolicyError("Image generation must be enabled for a write worker")
        state = Path(self.image_state_directory or "")
        if not state.is_absolute() or any(
            state == root or root in state.parents or state in root.parents for root in self.roots
        ):
            raise PolicyError("Image request state must be outside model-accessible roots")
        image_module = owned_module("images")
        if self.worker_context is not None:
            owned_module("worker_mcp").check_worker(self.worker_context)
        # Absolute and relative names for the same output share one billing intent.
        _, image_parts = self.locate(arguments.get("path", ""), True)
        arguments = {**arguments, "path": str(Path(*image_parts))}
        try:
            result = image_module.generate_image(
                self.images, arguments, self, state, os.environ.get("OPENAI_API_KEY")
            )
        except image_module.ImageError as error:
            # Provider/configuration problems are reported to the worker, not disguised
            # as a forbidden tool or a successful generated artifact.
            result = {
                "status": "failed",
                "message": str(error),
                "code": getattr(error, "code", "image_generation_failed"),
                "ambiguous": bool(getattr(error, "ambiguous", False)),
            }
        return json.dumps(result, ensure_ascii=False, allow_nan=False)

    def walk(self, root, parts):
        pending = [parts]
        visited = 0
        while pending:
            current = pending.pop()
            # scandir is incremental: avoid an unbounded directory listing.
            with self.directory(root, current) as descriptor, os.scandir(descriptor) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_VISITED:
                        raise PolicyError("directory scan exceeds limit")
                    child = (*current, entry.name)
                    if forbidden(entry.name, trusted=self.trusted) or self.is_private(
                        root.joinpath(*child)
                    ):
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(child)
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        yield child

    def execute(self, name, arguments):
        if not isinstance(arguments, dict):
            raise PolicyError("tool arguments must be an object")
        if name == "run_command":
            owned_module("worker_mcp").check_worker(self.worker_context or {})
            if not self.commands:
                raise PolicyError("Command configuration is missing")
            result = owned_module("commands").run_command(self.commands, arguments)
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        if name in {"send_team_message", "read_team_messages"}:
            context = self.worker_context or {}
            result = owned_module("team_messages").execute(
                context["home"], context["task_id"], context["token"], name, arguments
            )
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        if name == "generate_image":
            return self.generate_image(arguments)
        allowed = READ_TOOLS | (WRITE_TOOLS if self.mode == "write" else set())
        if name not in allowed or not isinstance(arguments, dict):
            raise PolicyError("forbidden tool")
        root, parts = self.locate(arguments.get("path", "."), name in WRITE_TOOLS)
        if name == "read":
            content = self.file_text(root, parts)
            offset = bounded_integer(arguments.get("offset"), 1, MAX_BYTES)
            limit = bounded_integer(arguments.get("limit"), 2000, 2000)
            return "\n".join(content.split("\n")[offset - 1 : offset - 1 + limit])[:50000]
        if name == "write":
            self.write_text(root, parts, arguments.get("content"))
            return "File written"
        if name == "edit":
            content = self.file_text(root, parts)
            edits = arguments.get("edits")
            if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
                raise PolicyError("edits must be a bounded nonempty list")
            replacements = []
            for edit in edits:
                if not isinstance(edit, dict):
                    raise PolicyError("invalid edit")
                old, new = edit.get("oldText"), edit.get("newText")
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise PolicyError("invalid edit text")
                if content.count(old) != 1:
                    raise PolicyError("edit must uniquely match original text")
                start = content.index(old)
                replacements.append((start, start + len(old), new))
            replacements.sort()
            if any(left[1] > right[0] for left, right in itertools.pairwise(replacements)):
                raise PolicyError("overlapping edits")
            for start, end, new in reversed(replacements):
                content = content[:start] + new + content[end:]
            self.write_text(root, parts, content)
            return "File edited"
        limit = bounded_integer(arguments.get("limit"), 100, MAX_RESULTS)
        if name == "ls":
            results = []
            with self.directory(root, parts) as descriptor, os.scandir(descriptor) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_VISITED:
                        raise PolicyError("directory scan exceeds limit")
                    if forbidden(entry.name, trusted=self.trusted) or self.is_private(
                        root.joinpath(*parts, entry.name)
                    ):
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        results.append(entry.name + "/")
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        results.append(entry.name)
                    if len(results) >= limit:
                        break
            return "\n".join(sorted(results))
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
            raise PolicyError("invalid search pattern")
        results = []
        try:
            with self.directory(root, parts):
                candidates = self.walk(root, parts)
                search_base = parts
        except NotADirectoryError:
            if name != "grep":
                raise
            candidates = [parts]
            search_base = parts[:-1]
        scanned_bytes = 0
        for child in candidates:
            relative = "/".join(child[len(search_base) :])
            if name == "find":
                if fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(
                    relative, pattern.removeprefix("**/")
                ):
                    results.append(relative)
            else:
                try:
                    content = self.file_text(root, child)
                except UnsupportedTextFile:
                    continue
                scanned_bytes += len(content.encode("utf-8"))
                if scanned_bytes > 16 * MAX_BYTES:
                    results.append("Search byte limit reached; narrow the path.")
                    break
                needle = pattern.casefold() if arguments.get("ignoreCase") else pattern
                for number, line in enumerate(content.split("\n"), 1):
                    searchable = line.casefold() if arguments.get("ignoreCase") else line
                    if needle in searchable:
                        results.append(f"{relative}:{number}:{line[:500]}")
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        return "\n".join(results)[:50000]


_OWNED_MODULES = {}


def owned_module(name):
    """Load only a fixed adjacent program module under Python -I, never project imports."""
    if name not in {"images", "commands", "team_messages", "worker_mcp", "read_context"}:
        raise PolicyError("Unknown owned module")
    if name not in _OWNED_MODULES:
        package_name = "orchestrator"
        if package_name not in sys.modules:
            package_specification = importlib.util.spec_from_file_location(
                package_name,
                Path(__file__).with_name("__init__.py"),
                submodule_search_locations=[str(Path(__file__).resolve().parent)],
            )
            package = importlib.util.module_from_spec(package_specification)
            sys.modules[package_name] = package
            package_specification.loader.exec_module(package)
        specification = importlib.util.spec_from_file_location(
            package_name + "." + name, Path(__file__).with_name(name + ".py")
        )
        module = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = module
        specification.loader.exec_module(module)
        _OWNED_MODULES[name] = module
    return _OWNED_MODULES[name]


def serve(options):
    # SystemExit unwinds run_command's finally block, killing its process group.
    # Default SIGTERM would skip that cleanup and orphan a trusted command.
    def stop(_signal, _frame):
        raise SystemExit(128 + _signal)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    broker = FileBroker(options)
    allowed = options.get("tool_names", options.get("tools", []))
    try:
        while True:
            line = sys.stdin.buffer.readline(2 * MAX_BYTES + 1)
            if not line:
                return
            try:
                if len(line) > 2 * MAX_BYTES or not line.endswith(b"\n"):
                    raise PolicyError("oversized or incomplete tool request")
                request = decode(line)
                if (
                    not isinstance(request, dict)
                    or set(request) != {"name", "arguments"}
                    or not isinstance(request["name"], str)
                    or request["name"] not in allowed
                    or not isinstance(request["arguments"], dict)
                ):
                    raise PolicyError("invalid broker protocol or unoffered tool")
            except (ValueError, KeyError, TypeError, RecursionError):
                print(
                    json.dumps({"ok": False, "fatal": True, "error": "Invalid tool protocol"}),
                    flush=True,
                )
                return
            try:
                text = broker.execute(request["name"], request["arguments"])
                response = {"ok": True, "text": text}
            except AuthorityError as error:
                # Deny the individual access, not the entire task; the model can correct its path.
                response = {"ok": False, "fatal": False, "error": str(error)}
            except FileNotFoundError:
                response = {
                    "ok": False,
                    "fatal": False,
                    "error": "File or directory not found; inspect the directory and retry with an existing path",
                }
            except PermissionError:
                response = {
                    "ok": False,
                    "fatal": False,
                    "error": "Permission denied for this path; choose an accessible project path",
                }
            except ValueError as error:
                response = {"ok": False, "fatal": False, "error": str(error)[:1000]}
            except Exception as error:  # noqa: BLE001 - tool protocol must return sanitized failures
                # No provider response, command content, credentials, or token in errors.
                response = {
                    "ok": False,
                    "fatal": False,
                    "error": "Tool failed ("
                    + type(error).__name__
                    + "); check arguments and the current worker state",
                }
            frame = json.dumps(response, ensure_ascii=True)
            if len(frame) + 1 > 2 * MAX_BYTES:
                frame = json.dumps(
                    {
                        "ok": False,
                        "fatal": False,
                        "error": "Tool result exceeds the transport limit; "
                        "narrow the request with a smaller limit, path, or offset",
                    }
                )
            print(frame, flush=True)
    finally:
        broker.close()


def trusted_installation(executable, roots):
    located = shutil.which(executable)
    if not located:
        raise PolicyError("Pi executable not found")
    binary = Path(located).resolve(strict=True)
    if any(binary == root or root in binary.parents for root in roots):
        raise PolicyError("Pi installation must be outside model-accessible roots")
    for directory in binary.parents:
        manifest = directory / "package.json"
        if not manifest.is_file():
            continue
        metadata = decode(manifest.read_bytes())
        if metadata.get("name") != "@earendil-works/pi-coding-agent":
            continue
        if metadata.get("version") != PI_VERSION:
            raise PolicyError(f"Pi SDK version must be {PI_VERSION}")
        if (directory / metadata.get("bin", {}).get("pi", "")).resolve() != binary:
            raise PolicyError("configured Pi executable does not match package entrypoint")
        return directory
    raise PolicyError("configured executable is not an installed supported Pi package")


def launch(options):
    cwd = Path(os.path.abspath(options["cwd"]))
    project = Path(os.path.abspath(options.get("project_root") or cwd))
    auth_directory = os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
    auth_path = (Path(auth_directory).expanduser().resolve() / "auth.json").resolve()
    options = {**options, "auth_path": str(auth_path)}
    # Validate every descriptor root before importing any Pi runtime code.
    broker = FileBroker(options)
    effective_roots = set(broker.roots)
    broker.close()
    package = trusted_installation(options["executable"], effective_roots | {cwd, project})
    if options["mode"] == "write" and cwd in Path(__file__).resolve().parents:
        raise PolicyError("worker cannot overwrite its supervisor installation")
    # All cooperating Pi writers use the canonical auth path, not per-worker aliases.
    node = shutil.which("node", path="/usr/local/bin:/usr/bin:/bin")
    if node is None:
        raise PolicyError("trusted Node.js executable not found")
    with tempfile.TemporaryDirectory(prefix="orchestrator-pi-") as temporary:
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": temporary,
            "LANG": "C.UTF-8",
            "PI_CODING_AGENT_DIR": temporary,
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
        # Stored Pi auth is canonical; only selected provider API variables are inherited.
        provider_variables = {
            "openai": ("OPENAI_API_KEY",),
            "google": ("GEMINI_API_KEY",),
            "deepseek": ("DEEPSEEK_API_KEY",),
            "openrouter": ("OPENROUTER_API_KEY",),
            "xai": ("XAI_API_KEY",),
            "mistral": ("MISTRAL_API_KEY",),
            "groq": ("GROQ_API_KEY",),
            "cerebras": ("CEREBRAS_API_KEY",),
            "github-copilot": ("COPILOT_GITHUB_TOKEN",),
            "nvidia": ("NVIDIA_API_KEY",),
            "ant-ling": ("ANT_LING_API_KEY",),
            "vercel-ai-gateway": ("AI_GATEWAY_API_KEY",),
            "zai": ("ZAI_API_KEY",),
            "zai-coding-cn": ("ZAI_CODING_CN_API_KEY",),
            "opencode": ("OPENCODE_API_KEY",),
            "opencode-go": ("OPENCODE_API_KEY",),
            "radius": ("RADIUS_API_KEY",),
            "typesafe": ("TYPESAFE_API_KEY",),
            "huggingface": ("HF_TOKEN",),
            "fireworks": ("FIREWORKS_API_KEY",),
            "together": ("TOGETHER_API_KEY",),
            "baseten": ("BASETEN_API_KEY",),
            "kimi-coding": ("KIMI_API_KEY",),
            "meta": ("META_API_KEY",),
            "minimax": ("MINIMAX_API_KEY",),
            "minimax-cn": ("MINIMAX_CN_API_KEY",),
            "moonshotai": ("MOONSHOT_API_KEY",),
            "moonshotai-cn": ("MOONSHOT_API_KEY",),
            "qwen-token-plan": ("QWEN_TOKEN_PLAN_API_KEY",),
            "qwen-token-plan-individual": ("QWEN_TOKEN_PLAN_API_KEY",),
            "qwen-token-plan-cn": ("QWEN_TOKEN_PLAN_CN_API_KEY",),
            "xiaomi": ("XIAOMI_API_KEY",),
            "xiaomi-token-plan-cn": ("XIAOMI_TOKEN_PLAN_CN_API_KEY",),
            "xiaomi-token-plan-ams": ("XIAOMI_TOKEN_PLAN_AMS_API_KEY",),
            "xiaomi-token-plan-sgp": ("XIAOMI_TOKEN_PLAN_SGP_API_KEY",),
            "azure-openai-responses": (
                "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_BASE_URL",
                "AZURE_OPENAI_RESOURCE_NAME",
            ),
            "cloudflare-workers-ai": ("CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID"),
            "cloudflare-ai-gateway": (
                "CLOUDFLARE_API_KEY",
                "CLOUDFLARE_ACCOUNT_ID",
                "CLOUDFLARE_GATEWAY_ID",
            ),
            "google-vertex": (
                "GOOGLE_CLOUD_API_KEY",
                "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_CLOUD_LOCATION",
            ),
            "amazon-bedrock": (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_BEARER_TOKEN_BEDROCK",
                "AWS_REGION",
                "AWS_DEFAULT_REGION",
            ),
        }
        for name in provider_variables.get(options["provider"], ()):
            if name in os.environ:
                environment[name] = os.environ[name]
        if (
            options["mode"] == "write"
            and options.get("images", {}).get("enabled") is True
            and "OPENAI_API_KEY" in os.environ
        ):
            environment["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        options = {
            **options,
            "package": str(package),
            "auth_path": str(auth_path),
            "agent_dir": temporary,
            "python": sys.executable,
            "broker": str(Path(__file__).resolve()),
        }
        command = [node, str(Path(__file__).with_name("pi_bridge.mjs")), json.dumps(options)]
        return subprocess.call(command, cwd=cwd, env=environment)


def main():
    try:
        if len(sys.argv) != 3 or sys.argv[1] not in ("launch", "serve"):
            raise PolicyError("invalid owned Pi entrypoint")
        options = decode(sys.argv[2])
        if sys.argv[1] == "serve":
            serve(options)
            return 0
        return launch(options)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Pi adapter: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
