"""Bounded worker commands. Callers must reserve resources for heavy work.

Only host-owned options grant authority; arguments never grant mounts or environment.
Sandboxing defaults on. Explicit sandbox=False runs trusted, unrestricted host commands:
read mode and network restrictions are guidance, not operating-system boundaries.
In plain terms, trusted commands can read and change anything the host user can.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

OUTPUT_LIMIT = 1024 * 1024
BWRAP = "/usr/bin/bwrap"


def _path(value: object, *, directory: bool = True) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError("Expected an absolute path")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Parent path components are forbidden")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Symlink paths are forbidden")
    if directory and not path.is_dir():
        raise ValueError("Directory does not exist")
    if not directory and not path.is_file():
        raise ValueError("Tool must be a regular file")
    return path


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _number(value: object, maximum: int, label: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value


def _environment(cpus: int) -> dict[str, str]:
    return {
        "PATH": "/tools:/usr/bin:/bin",
        "HOME": "/home/worker",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "CARGO_BUILD_JOBS": str(cpus),
        "OMP_NUM_THREADS": str(cpus),
        "MAKEFLAGS": f"-j{cpus}",
    }


def _git_directories(workspace: Path) -> list[Path]:
    if not (workspace / ".git").exists():
        return []
    directories = []
    for argument in ("--absolute-git-dir", "--git-common-dir"):
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(workspace),
                "rev-parse",
                argument,
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            raise ValueError("Cannot resolve Git metadata safely")
        directory = Path(os.fsdecode(result.stdout).strip())
        if not directory.is_absolute():
            directory = workspace / directory
        # Git itself emits paths containing '..' for some worktrees.
        directory = _path(os.path.abspath(directory))
        if directory not in directories:
            directories.append(directory)
    return directories


def _artifact_fd(path: Path) -> int:
    """Open every directory component without following links, creating private dirs."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        information = os.fstat(descriptor)
        if information.st_uid != os.getuid() or stat.S_IMODE(information.st_mode) & 0o077:
            raise ValueError("Artifact directory must be private and owned by this user")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _save(descriptor: int, name: str, data: bytes) -> None:
    temporary = f".{uuid.uuid4().hex}.tmp"
    file_descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor
    )
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False
        )
    finally:
        os.unlink(temporary, dir_fd=descriptor)


def _tools(options: dict) -> dict[str, Path]:
    tools = options.get("tool_paths", {})
    if not isinstance(tools, dict):
        raise ValueError("tool_paths must be a mapping")  # noqa: TRY004 - configuration error contract
    validated = {}
    for name, value in tools.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name):
            raise ValueError("Invalid tool name")
        tool = _path(value, directory=False)
        if not os.access(tool, os.X_OK):
            raise ValueError("Tool is not executable")
        validated[name] = tool
    return validated


def _prepare(options: dict, arguments: dict) -> tuple[list[str], dict[str, str], int, Path]:
    sandbox = options.get("sandbox", True)
    if type(sandbox) is not bool:
        raise ValueError("sandbox must be boolean")
    if set(arguments) - {"command", "cwd", "timeout_seconds"}:
        raise ValueError("Unsupported command arguments")
    command = arguments.get("command")
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > 32000
        or "\0" in command
    ):
        raise ValueError("command must be a nonempty script of at most 32000 characters")
    workspace = _path(options.get("cwd"))
    source = _path(options.get("project_root"))
    mode = options.get("mode")
    if mode not in ("read", "write"):
        raise ValueError("mode must be read or write")
    # A project cannot replace the operating system or the sandbox's private directories.
    for root in (workspace, source):
        if root == Path("/") or any(
            _inside(root, Path(prefix)) or _inside(Path(prefix), root)
            for prefix in (
                "/usr",
                "/bin",
                "/lib",
                "/lib64",
                "/proc",
                "/dev",
                "/tools",
                "/home/worker",
                "/etc",
            )
        ):
            raise ValueError("Project path overlaps sandbox infrastructure")
    if source != workspace and _inside(source, workspace):
        raise ValueError("Source must not be inside writable workspace")
    maximum = _number(options.get("timeout_seconds", 120), 900, "timeout_seconds")
    timeout = _number(arguments.get("timeout_seconds", maximum), maximum, "timeout_seconds")
    cpus = _number(options.get("cpus", 1), 1024, "cpus")
    network = options.get("network", False)
    if type(network) is not bool:
        raise ValueError("network must be boolean")
    relative = arguments.get("cwd", ".")
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValueError("cwd must be a contained relative directory")
    working_directory = _path(str(workspace / relative))
    artifact_value = options.get("artifact_directory")
    if not isinstance(artifact_value, str) or not os.path.isabs(artifact_value):
        raise ValueError("artifact_directory must be absolute")
    artifacts = Path(artifact_value)
    if ".." in artifacts.parts or any(
        _inside(artifacts, root) or _inside(root, artifacts) for root in (workspace, source)
    ):
        raise ValueError("Artifacts must be outside project directories")
    tools = _tools(options)
    if not sandbox:
        environment = _environment(cpus)
        environment.update(PATH="/usr/bin:/bin", HOME="/nonexistent")
        return ["/bin/sh", "-c", command], environment, timeout, artifacts
    argv = [
        BWRAP,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for directory in ("/bin", "/lib", "/lib64", "/sbin"):
        if os.path.islink(directory):
            argv += ["--symlink", os.readlink(directory), directory]
        elif os.path.isdir(directory):
            argv += ["--ro-bind", directory, directory]
    # Debian tool aliases such as /usr/bin/cc resolve through this directory.
    # Mount only the system aliases, never the rest of /etc or host credentials.
    if Path("/etc/alternatives").is_dir():
        argv += ["--ro-bind", "/etc/alternatives", "/etc/alternatives"]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home/worker",
        "--dir",
        "/tools",
    ]
    if network:
        argv += ["--share-net"]
        if Path("/etc/resolv.conf").exists():
            argv += ["--ro-bind", str(Path("/etc/resolv.conf").resolve()), "/etc/resolv.conf"]
    if source != workspace:
        argv += ["--ro-bind", str(source), str(source)]
    argv += ["--bind" if mode == "write" else "--ro-bind", str(workspace), str(workspace)]
    git_entry = workspace / ".git"
    if git_entry.is_symlink():
        raise ValueError("Symlink Git metadata is forbidden")
    for directory in _git_directories(workspace):
        if directory == workspace or _inside(workspace, directory):
            raise ValueError("Git metadata overlaps workspace")
        argv += ["--ro-bind", str(directory), str(directory)]
    if git_entry.exists():
        argv += ["--ro-bind", str(git_entry), str(git_entry)]
    for name, tool in tools.items():
        argv += ["--ro-bind", str(tool), f"/tools/{name}"]
    argv += ["--chdir", str(working_directory), "--", "/bin/sh", "-c", command]
    return argv, _environment(cpus), timeout, artifacts


def _guard_command(lease_descriptor: int, command: list[str]) -> int:
    """Keep ownership of the child PID until its group has been cleaned up.

    EOF on the private lease means the caller exited, including SIGKILL.
    Unlike a parent-death signal on a shell, this also kills its build children.
    Plain version: when the caller disappears, stop its command and children.
    Trusted commands must not deliberately detach into different process groups.
    """
    try:
        child = subprocess.Popen(command, start_new_session=True, close_fds=True)
    except OSError as exception:
        print(f"Trusted command could not start: {exception}", file=sys.stderr)
        return 127
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(lease_descriptor, selectors.EVENT_READ)
            while True:
                # WNOWAIT leaves the leader unreaped, preventing PID/group reuse
                # between observing completion and killing remaining descendants.
                if os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                    break
                if selector.select(0.05):
                    break
    finally:
        os.close(lease_descriptor)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return_code = child.wait()
    return return_code if return_code >= 0 else 128 - return_code


def run_command(options: dict, arguments: dict) -> dict:
    """Run a bounded script, sandboxed by default or explicitly trusted by the host.

    Configuration errors raise ValueError; launch failures and nonzero exits are
    normal result objects. Sandbox failures never fall back to host execution.
    Trusted cwd validation prevents routing mistakes, not shell access elsewhere.
    In plain terms, the starting folder is checked, but trusted scripts can leave it.
    The caller must reserve CPU and memory beforehand.
    """
    options, arguments = dict(options), dict(arguments)
    argv, environment, timeout, artifacts = _prepare(options, arguments)
    sandboxed = options.get("sandbox", True)
    artifact_descriptor = _artifact_fd(artifacts)
    output = bytearray()
    timed_out = False
    output_limited = False
    exit_code = None
    error = None
    process = None
    trusted_directory = None
    lease_read = lease_write = None
    try:
        working_directory = None
        if not sandboxed:
            working_directory = str(_path(str(Path(options["cwd"]) / arguments.get("cwd", "."))))
            trusted_directory = tempfile.TemporaryDirectory(prefix="orchestrator-command-")
            private_directory = Path(trusted_directory.name)
            for name in ("home", "tmp", "tools"):
                (private_directory / name).mkdir(mode=0o700)
            for name, tool in _tools(options).items():
                (private_directory / "tools" / name).symlink_to(tool)
            environment.update(
                PATH=f"{private_directory / 'tools'}:/usr/bin:/bin",
                HOME=str(private_directory / "home"),
                TMPDIR=str(private_directory / "tmp"),
            )
            # Trusted workers use the host's installed toolchains and Git identity.
            # Only path settings are captured by the host; API tokens are never copied.
            host_environment = options.get("host_environment", {})
            for key in (
                "HOME",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "CARGO_HOME",
                "RUSTUP_HOME",
                "GOPATH",
                "GOMODCACHE",
                "PNPM_HOME",
                "NVM_BIN",
                "VIRTUAL_ENV",
            ):
                if isinstance(host_environment.get(key), str):
                    environment[key] = host_environment[key]
            if isinstance(host_environment.get("PATH"), str):
                environment["PATH"] = (
                    str(private_directory / "tools") + os.pathsep + host_environment["PATH"]
                )
        try:
            launch_options = {}
            if not sandboxed:
                lease_read, lease_write = os.pipe()
                launch_options["pass_fds"] = (lease_read,)
                argv = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--guard-command",
                    str(lease_read),
                    *argv,
                ]
            process = subprocess.Popen(
                argv,
                cwd=working_directory,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                **launch_options,
            )
            if lease_read is not None:
                os.close(lease_read)
                lease_read = None
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = OUTPUT_LIMIT - len(output)
                        output.extend(chunk[:available])
                        if len(chunk) > available:
                            output_limited = True
                            break
                    if output_limited:
                        break
            if not timed_out and not output_limited:
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
        except OSError as exception:
            error = (
                f"Sandbox unavailable; no command ran outside confinement: {exception}"
                if sandboxed
                else f"Trusted command execution failed: {exception}"
            )
        finally:
            if lease_read is not None:
                os.close(lease_read)
                lease_read = None
            if lease_write is not None:
                os.close(lease_write)
                lease_write = None
            if process is not None:
                if sandboxed:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
                process.stdout.close()
                exit_code = process.returncode
        identifier = uuid.uuid4().hex
        log_name = f"{identifier}.log"
        metadata_name = f"{identifier}.json"
        result = {
            "sandboxed": sandboxed,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_limited": output_limited,
            "output": output.decode("utf-8", "replace"),
            "log_path": str(artifacts / log_name),
            "metadata_path": str(artifacts / metadata_name),
            "verification": "program-captured",
        }
        if error:
            result["error"] = error
        _save(artifact_descriptor, log_name, bytes(output))
        _save(
            artifact_descriptor,
            metadata_name,
            json.dumps(
                {key: value for key, value in result.items() if key != "output"}, sort_keys=True
            ).encode(),
        )
        return result
    finally:
        os.close(artifact_descriptor)
        if trusted_directory is not None:
            trusted_directory.cleanup()


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "--guard-command":
        raise SystemExit("This module is an internal command guardian")
    raise SystemExit(_guard_command(int(sys.argv[2]), sys.argv[3:]))
