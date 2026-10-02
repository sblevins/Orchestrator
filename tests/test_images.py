"""Offline tests for the owned image backend, never using real credentials."""

import base64
import hashlib
import io
import json
import struct
import tempfile
import unittest
import urllib.error
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import ConfigurationError, load_config, validate_config
from orchestrator.pi_tools import owned_module

images = owned_module("images")


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width=1, height=1):
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


class Broker:
    def __init__(self, root):
        self.root = root
        self.prepare_count = 0
        self.writable = True
        self.fail_publication = False

    def prepare_image(self, path, output_format):
        self.prepare_count += 1
        target = self.root / path
        if not self.writable or target.exists() or target.suffix not in {".png", ".jpeg", ".jpg"}:
            raise ValueError("unsafe target")
        return target

    def save_image(self, target, data):
        if self.fail_publication:
            raise OSError("sensitive publication failure")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as destination:
            destination.write(data)
        return {
            "path": str(target.relative_to(self.root)),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def verify_image(self, result):
        target = self.root / result["path"]
        return (
            target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == result["sha256"]
        )


class ImagesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.broker = Broker(self.root / "artifacts")
        self.state = self.root / "state"
        self.settings = {
            "enabled": True,
            "model": "gpt-image-2",
            "size": "auto",
            "quality": "auto",
            "output_format": "png",
        }
        self.arguments = {"prompt": "private prompt marker", "path": "image.png"}
        self.calls = []

    def transport(self, payload, key):
        self.calls.append((payload, key))
        data = png() if payload["output_format"] == "png" else b"\xff\xd8\xff\xe0\x00\x02\xff\xd9"
        return {"data": [{"b64_json": base64.b64encode(data).decode()}]}, "req_test"

    def generate(self, **kwargs):
        return images.generate_image(
            self.settings,
            self.arguments,
            self.broker,
            self.state,
            kwargs.pop("api_key", "fake-private-key"),
            transport=kwargs.pop("transport", self.transport),
            **kwargs,
        )

    def records(self):
        return [json.loads(path.read_text()) for path in self.state.glob("*.json")]

    def test_success_safe_metadata_and_private_state(self):
        result = self.generate()
        self.assertEqual(
            result,
            {
                "status": "generated",
                "path": "image.png",
                "mime_type": "image/png",
                "bytes": len(png()),
                "sha256": hashlib.sha256(png()).hexdigest(),
                "model": "gpt-image-2",
                "request_id": "req_test",
            },
        )
        for path in self.state.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn("fake-private-key", text)
            self.assertNotIn(self.arguments["prompt"], text)
            self.assertNotIn(base64.b64encode(png()).decode(), text)
        self.assertNotIn("response_format", self.calls[0][0])

    def test_jpeg(self):
        self.arguments.update(path="photo.jpg", format="jpeg")
        self.assertEqual(self.generate()["mime_type"], "image/jpeg")

    def test_cached_normalized_path_does_not_prepare_again(self):
        first = self.generate()
        self.arguments["path"] = "./image.png"
        self.assertEqual(self.generate(), first)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.broker.prepare_count, 1)

    def test_changed_prompt_blocks(self):
        self.generate()
        self.arguments["prompt"] = "different"
        with self.assertRaisesRegex(images.ImageError, "new path"):
            self.generate()
        self.assertEqual(len(self.calls), 1)

    def test_tampered_artifact_not_regenerated(self):
        self.generate()
        (self.broker.root / "image.png").write_bytes(b"changed")
        with self.assertRaisesRegex(images.ImageError, "verified"):
            self.generate()
        self.assertEqual(len(self.calls), 1)

    def test_disabled_readonly_missing_key_invalid_args_before_intent(self):
        for changes in (
            {"prompt": ""},
            {"prompt": "x" * 32001},
            {"quality": []},
            {"size": "1x1"},
            {"format": "gif"},
            {"endpoint": "https://evil.example"},
            {"model": "other"},
            {"token": "other"},
            {"prompt": "\ud800"},
        ):
            original = self.arguments.copy()
            self.arguments.update(changes)
            with self.assertRaises(images.ImageError):
                self.generate()
            self.arguments = original
        with self.assertRaisesRegex(images.ImageError, "OPENAI_API_KEY"):
            self.generate(api_key=None)
        self.settings["enabled"] = False
        with self.assertRaises(images.ImageError):
            self.generate()
        self.settings["enabled"] = True
        self.broker.writable = False
        with self.assertRaises(images.ImageError):
            self.generate()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records(), [])

    def test_bad_responses_unknown_without_retry(self):
        bad_responses = [
            None,
            [],
            {"data": "no"},
            {"data": []},
            {"data": [None]},
            {"data": [{"url": "https://evil.example/a.png"}]},
            {"data": [{"b64_json": 42}]},
            {"data": [{"b64_json": "!"}]},
            {"data": [{"b64_json": base64.b64encode(png(0)).decode()}]},
            {"data": [{"b64_json": base64.b64encode(png()[:-12]).decode()}]},
            {"data": [{"b64_json": base64.b64encode(b"not png").decode()}]},
        ]
        for index, response in enumerate(bad_responses):
            self.arguments["path"] = f"bad{index}.png"
            with self.assertRaises(images.ImageError) as raised:
                self.generate(transport=lambda *_, response=response: (response, None))
            self.assertTrue(raised.exception.ambiguous)
            with self.assertRaises(images.ImageError):
                self.generate()
        self.assertEqual(self.calls, [])

    def test_oversized_image_and_response(self):
        with patch.object(images, "MAX_IMAGE_BYTES", 10), self.assertRaises(images.ImageError):
            self.generate()
        self.arguments["path"] = "second.png"
        with patch.object(images, "MAX_RESPONSE_BYTES", 10), self.assertRaises(images.ImageError):
            self.generate()

    def test_network_error_redacted_and_unknown(self):
        def fail(*_):
            self.calls.append(1)
            raise OSError("fake-private-key private prompt marker base64-marker")

        for _ in range(2):
            with self.assertRaises(images.ImageError) as raised:
                self.generate(transport=fail)
            self.assertNotIn("fake-private-key", str(raised.exception))
            self.assertTrue(raised.exception.ambiguous)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.records()[0]["status"], "unknown")

    def test_publication_failure_records_unknown(self):
        self.broker.fail_publication = True
        with self.assertRaisesRegex(images.ImageError, "may have billed"):
            self.generate()
        record = self.records()[0]
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["response_status"], "publication_failed")
        with self.assertRaises(images.ImageError):
            self.generate()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.broker.root / "image.png").exists())

    def test_interrupted_intent_blocks_submission(self):
        self.generate()
        path = next(self.state.glob("*.json"))
        record = json.loads(path.read_text())
        record.pop("result")
        record["status"] = "intent"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(images.ImageError, "unknown"):
            self.generate()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.records()[0]["status"], "unknown")

    def test_concurrency_submits_once(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.generate(), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.calls), 1)

    def test_nonprivate_state_rejected_before_billing(self):
        self.state.mkdir(mode=0o777)
        self.state.chmod(0o777)
        with self.assertRaises(images.ImageError):
            self.generate()
        self.assertEqual(self.calls, [])

    def test_real_broker_binary_publication_and_no_clobber(self):
        from orchestrator.pi_tools import FileBroker

        workspace, source = self.root / "workspace", self.root / "source"
        workspace.mkdir()
        source.mkdir()
        broker = FileBroker({"cwd": str(workspace), "project_root": str(source), "mode": "write"})
        self.addCleanup(broker.close)
        self.broker = broker
        first = self.generate()
        self.assertEqual((workspace / "image.png").read_bytes(), png())
        self.assertEqual((workspace / "image.png").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.generate(), first)
        self.assertEqual(len(self.calls), 1)
        for path in (str(source / "outside.png"), "../outside.png", "linked/image.png"):
            self.arguments["path"] = path
            if path.startswith("linked"):
                (workspace / "linked").symlink_to(source, target_is_directory=True)
            with self.assertRaises(images.ImageError):
                self.generate()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(source.iterdir()), [])

    def test_state_parent_symlink_rejected(self):
        real = self.root / "real"
        real.mkdir()
        self.state.symlink_to(real, target_is_directory=True)
        with self.assertRaises(images.ImageError):
            self.generate()
        self.assertEqual(self.calls, [])
        self.assertEqual(list(real.iterdir()), [])

    def test_state_symlink_and_oversize_rejected(self):
        self.generate()
        state_path = next(self.state.glob("*.json"))
        state_path.write_bytes(b"x" * (images.MAX_STATE_BYTES + 1))
        with self.assertRaises(images.ImageError):
            self.generate()
        state_path.unlink()
        external = self.root / "external"
        external.write_text("secret")
        state_path.symlink_to(external)
        with self.assertRaises(images.ImageError):
            self.generate()
        self.assertEqual(external.read_text(), "secret")
        self.assertEqual(len(self.calls), 1)

    def test_request_id_wrong_type_omitted(self):
        result = self.generate(transport=lambda *args: (self.transport(*args)[0], {"secret": "x"}))
        self.assertNotIn("request_id", result)

    def test_saved_model_settings_and_image_requests_accept_the_same_identifiers(self):
        config = load_config(Path(self.temporary.name))
        for index, model in enumerate(
            (
                "gpt-image-2",
                "future-image-model-2029",
                "x" * 256,
                "x" * 257,
                "",
                "-flag",
                "gpt image",
                "gpt\x7fimage",
                "gpt\x85image",
                "gpt\u200bimage",
                "gpt\u00a0image",
                "gpt\ud800image",
                None,
            )
        ):
            with self.subTest(model=model):
                config["images"] = {**self.settings, "model": model}
                self.settings["model"] = model
                self.arguments["path"] = f"image-{index}.png"
                try:
                    validate_config(config)
                    saved = True
                except ConfigurationError:
                    saved = False
                try:
                    self.generate()
                    generated = True
                except images.ImageError as error:
                    self.assertEqual(error.code, "invalid_arguments")
                    generated = False
                self.assertEqual(saved, generated)
                self.assertEqual(
                    saved, model in ("gpt-image-2", "future-image-model-2029", "x" * 256)
                )
        self.assertEqual(
            [payload["model"] for payload, _ in self.calls],
            ["gpt-image-2", "future-image-model-2029", "x" * 256],
        )

    def test_pinned_model_and_overrides(self):
        self.settings["model"] = "future-image-model-2029"
        self.arguments.update(size="1536x1024", quality="high")
        self.assertEqual(self.generate()["model"], "future-image-model-2029")
        self.assertEqual(self.calls[0][0]["size"], "1536x1024")

    def test_transport_fixed_endpoint_timeout_bounded_read(self):
        raw = json.dumps({"data": []})
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = raw.encode()
        response.headers = {"x-request-id": "req_http"}
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        with patch.object(images.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(
                images._transport({"prompt": "test"}, "fake-key"), ({"data": []}, "req_http")
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/images/generations")
        self.assertEqual(request.method, "POST")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 180})
        response.read.assert_called_once_with(images.MAX_RESPONSE_BYTES + 1)

    def test_http_errors_sanitized_and_not_retried(self):
        for status in (400, 401, 403, 429, 500):
            self.arguments["path"] = f"http{status}.png"
            opener = unittest.mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                images.ENDPOINT, status, "sensitive provider body", {}, io.BytesIO(b"secret")
            )
            with patch.object(images.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(images.ImageError) as raised:
                    self.generate(transport=images._transport)
                self.assertNotIn("sensitive", str(raised.exception))
                self.assertEqual(raised.exception.ambiguous, status == 500)
                with self.assertRaises(images.ImageError):
                    self.generate(transport=images._transport)
            self.assertEqual(opener.open.call_count, 1)

    def test_transport_invalid_json_wrong_type_and_oversize(self):
        for raw in (b"not json secret", b"[]", b"x" * 65):
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = raw
            opener = unittest.mock.Mock()
            opener.open.return_value = response
            with (
                patch.object(images.urllib.request, "build_opener", return_value=opener),
                patch.object(images, "MAX_RESPONSE_BYTES", 64),
                self.assertRaises(images.ImageError) as raised,
            ):
                images._transport({}, "fake-key")
            self.assertTrue(raised.exception.ambiguous)
            self.assertNotIn("secret", str(raised.exception))
            response.read.assert_called_once_with(65)

    def test_intent_exists_before_network_and_cancellation_blocks_retry(self):
        def cancel(*_):
            self.assertEqual(self.records()[0]["status"], "intent")
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.generate(transport=cancel)
        with self.assertRaises(images.ImageError):
            self.generate()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records()[0]["status"], "unknown")

    def test_lock_symlink_rejected(self):
        self.generate()
        lock = next(self.state.glob("*.lock"))
        lock.unlink()
        external = self.root / "external-lock"
        external.write_text("unchanged")
        lock.symlink_to(external)
        with self.assertRaises(images.ImageError):
            self.generate()
        self.assertEqual(external.read_text(), "unchanged")
        self.assertEqual(len(self.calls), 1)

    def test_redirect_forbidden(self):
        with self.assertRaisesRegex(images.ImageError, "redirects"):
            images._NoRedirect().redirect_request(
                None, None, 302, "secret", {}, "https://evil.example"
            )


if __name__ == "__main__":
    unittest.main()
