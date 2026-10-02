"""Owned Images API client with durable, per-output at-most-once submission."""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import urllib.error
import urllib.request
import uuid
from pathlib import Path

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
ENDPOINT = "https://api.openai.com/v1/images/generations"
SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
QUALITIES = {"auto", "low", "medium", "high"}
FORMATS = {"png", "jpeg"}


class ImageError(ValueError):
    def __init__(self, message: str, code: str = "image_error", ambiguous: bool = False):
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ImageError(
            "Images API redirects are not allowed; generation may have billed.", "redirect", True
        )


def _transport(payload: dict, api_key: str) -> tuple[dict, str | None]:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=180) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ImageError(
                    "Images API response is too large; generation may have billed.",
                    "invalid_response",
                    True,
                )
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise TypeError("response type")
            return parsed, response.headers.get("x-request-id")
    except ImageError:
        raise
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in {400, 401, 403, 429}:
            raise ImageError(
                f"Images API rejected the request (HTTP {status}); no automatic retry.",
                f"http_{status}",
            ) from None
        raise ImageError(
            "Images API request outcome is unknown; generation may have billed.", "unknown", True
        ) from None
    except Exception:  # noqa: BLE001 - sanitize provider errors and preserve durable billing intent
        raise ImageError(
            "Images API request outcome is unknown; generation may have billed.", "unknown", True
        ) from None


def _validate(settings: dict, arguments: dict) -> tuple[str, dict]:
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        raise ImageError("Image generation is disabled.", "disabled")
    if not isinstance(arguments, dict) or set(arguments) - {
        "prompt",
        "path",
        "size",
        "quality",
        "format",
    }:
        raise ImageError("Invalid image arguments.", "invalid_arguments")
    prompt, path = arguments.get("prompt"), arguments.get("path")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt) > 32000
        or not isinstance(path, str)
        or not path.strip()
        or len(path) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ImageError(
            "Provide a prompt of 1-32000 characters and a valid output path.", "invalid_arguments"
        )
    model = settings.get("model", "gpt-image-2")
    if (
        not isinstance(model, str)
        or not model.strip()
        or model.startswith("-")
        or len(model) > 256
        or any(ord(character) < 33 or ord(character) == 127 for character in model)
    ):
        raise ImageError("Invalid image model configuration.", "invalid_arguments")
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": arguments.get("size", settings.get("size", "auto")),
        "quality": arguments.get("quality", settings.get("quality", "auto")),
        "output_format": arguments.get("format", settings.get("output_format", "png")),
    }
    for name, choices in (("size", SIZES), ("quality", QUALITIES), ("output_format", FORMATS)):
        if not isinstance(payload[name], str) or payload[name] not in choices:
            raise ImageError("Invalid image size, quality, or format.", "invalid_arguments")
    try:
        prompt.encode("utf-8")
        path.encode("utf-8")
        model.encode("utf-8")
    except UnicodeError:
        raise ImageError("Invalid image argument text.", "invalid_arguments") from None
    return os.path.normpath(path), payload


def _image_bytes(response: dict, output_format: str) -> bytes:
    try:
        if not isinstance(response, dict):
            raise TypeError()
        # Bound injected transports too, not just the HTTP reader.
        if len(json.dumps(response).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError()
        records = response.get("data")
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError()
        encoded = records[0].get("b64_json")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError()
        image = base64.b64decode(encoded, validate=True)
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ValueError()
        if output_format == "jpeg":
            if (
                len(image) < 4
                or not image.startswith(b"\xff\xd8")
                or not image.endswith(b"\xff\xd9")
            ):
                raise ValueError()
        else:
            if (
                len(image) < 45
                or image[:8] != b"\x89PNG\r\n\x1a\n"
                or image[8:16] != b"\x00\x00\x00\rIHDR"
            ):
                raise ValueError()
            width, height = struct.unpack(">II", image[16:24])
            if not (
                0 < width <= 16384 and 0 < height <= 16384 and width * height <= 64 * 1024 * 1024
            ):
                raise ValueError()
            position = 8
            while position < len(image):
                if position + 12 > len(image):
                    raise ValueError()
                length = struct.unpack(">I", image[position : position + 4])[0]
                chunk_type = image[position + 4 : position + 8]
                end = position + 12 + length
                if end > len(image):
                    raise ValueError()
                checksum = struct.unpack(">I", image[end - 4 : end])[0]
                if binascii.crc32(image[position + 4 : end - 4]) & 0xFFFFFFFF != checksum:
                    raise ValueError()
                if chunk_type == b"IEND":
                    if length != 0 or end != len(image):
                        raise ValueError()
                    break
                position = end
            else:
                raise ValueError()
        return image
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        raise ImageError(
            "Images API returned invalid image data; generation may have billed.",
            "invalid_response",
            True,
        ) from None


@contextlib.contextmanager
def _state_directory(path: Path):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(os.path.abspath(path)).parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        information = os.fstat(descriptor)
        if information.st_uid != os.getuid() or stat.S_IMODE(information.st_mode) & 0o077:
            raise ImageError(
                "Image state directory must be private and owned by this user.", "state_error"
            )
        yield descriptor
    finally:
        os.close(descriptor)


def _read_state(directory: int, name: str) -> dict | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("invalid state")
        raw = source.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("invalid state")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise TypeError("invalid state")
    return result


def _write_state(directory: int, name: str, record: dict):
    temporary = "." + uuid.uuid4().hex
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(json.dumps(record, sort_keys=True).encode("utf-8"))
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def generate_image(
    settings: dict,
    arguments: dict,
    broker,
    state_directory: Path,
    api_key: str | None,
    *,
    transport=None,
) -> dict:
    """Generate once per normalized path; injected transport takes (payload, api_key)."""
    path, payload = _validate(settings, arguments)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ImageError(
            "Set OPENAI_API_KEY for supervisor; Codex subscription credentials cannot authenticate Images API.",
            "missing_api_key",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in api_key):
        raise ImageError("Invalid Images API credential.", "invalid_api_key")
    request_sha = hashlib.sha256(
        json.dumps({"path": path, "request": payload}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    path_sha = hashlib.sha256(path.encode("utf-8")).hexdigest()
    try:
        with _state_directory(state_directory) as directory:
            lock = os.open(
                path_sha + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                if not stat.S_ISREG(os.fstat(lock).st_mode):
                    raise ValueError("invalid lock")
                fcntl.flock(lock, fcntl.LOCK_EX)
                return _generate_locked(
                    directory,
                    path_sha + ".json",
                    request_sha,
                    path,
                    payload,
                    broker,
                    api_key,
                    transport or _transport,
                )
            finally:
                os.close(lock)
    except ImageError:
        raise
    except Exception:  # noqa: BLE001 - sanitize provider errors and preserve durable billing intent
        raise ImageError(
            "Image state or output could not be accessed safely; no automatic retry.", "state_error"
        ) from None


def _generate_locked(directory, name, request_sha, path, payload, broker, api_key, transport):
    record = _read_state(directory, name)
    if record is not None:
        if record.get("request_sha") != request_sha:
            raise ImageError(
                "This output path already belongs to another request; use a new path.",
                "path_conflict",
            )
        if record.get("status") == "succeeded":
            result = record.get("result")
            if isinstance(result, dict) and broker.verify_image(result) is True:
                return result
            raise ImageError(
                "The saved image could not be verified; use a new path.", "artifact_changed"
            )
        if record.get("status") == "failed":
            raise ImageError(
                "This image request previously failed; use a new path for a deliberate retry.",
                "previous_failure",
            )
        record["status"] = "unknown"
        _write_state(directory, name, record)
        raise ImageError(
            "Image request outcome is unknown; generation may have billed. No automatic retry; use a new path only deliberately.",
            "unknown",
            True,
        )
    # Broker authorization and no-clobber checks must precede submission intent.
    try:
        target = broker.prepare_image(path, payload["output_format"])
    except Exception:  # noqa: BLE001 - sanitize provider errors and preserve durable billing intent
        raise ImageError(
            "Image output is not writable or its path is not allowed.", "invalid_path"
        ) from None
    record = {"request_sha": request_sha, "status": "intent"}
    _write_state(directory, name, record)
    try:
        response, request_id = transport(payload, api_key)
        image = _image_bytes(response, payload["output_format"])
        record["response_status"] = "received"
        saved = broker.save_image(target, image)
        if (
            not isinstance(saved, dict)
            or not isinstance(saved.get("path"), str)
            or saved.get("bytes") != len(image)
            or saved.get("sha256") != hashlib.sha256(image).hexdigest()
        ):
            raise ValueError("invalid publication metadata")
        result = {
            "status": "generated",
            "path": saved["path"],
            "mime_type": "image/" + payload["output_format"],
            "bytes": len(image),
            "sha256": saved["sha256"],
            "model": payload["model"],
        }
        if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", request_id):
            result["request_id"] = request_id
        record.update(status="succeeded", result=result)
        _write_state(directory, name, record)
        return result
    except BaseException as error:
        # Only known HTTP rejections establish that retry is not ambiguous.
        known_failure = (
            record.get("response_status") != "received"
            and isinstance(error, ImageError)
            and error.code in {"http_400", "http_401", "http_403", "http_429"}
        )
        record.pop("result", None)
        record.update(
            status="failed" if known_failure else "unknown",
            error_code=error.code if known_failure else "unknown",
        )
        if record.get("response_status") == "received":
            record["response_status"] = "publication_failed"
        try:
            _write_state(directory, name, record)
        except Exception:  # noqa: BLE001, S110 - durable intent still prevents paid replay
            pass
        if not isinstance(error, Exception):
            raise
        if known_failure:
            raise ImageError(
                "Images API rejected the request; no automatic retry.", error.code
            ) from None
        raise ImageError(
            "Image request or publication outcome is unknown; generation may have billed. No automatic retry.",
            "unknown",
            True,
        ) from None
