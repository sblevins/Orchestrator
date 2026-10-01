"""Native launch and bridge contracts without model calls or account access."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import load_config
from orchestrator.frontends import ROOT, build_frontend_command
from orchestrator.store import Store

FAKE_FRONTEND = """#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
arguments = sys.argv[1:]
flag = "--append-system-prompt-file" if "--settings" in arguments else "--append-system-prompt"
prompt = pathlib.Path(arguments[arguments.index(flag) + 1]).read_text()
result = {"prompt": prompt, "cwd": os.getcwd(), "arguments": arguments}
if "--settings" in arguments:
    settings = json.loads(pathlib.Path(arguments[arguments.index("--settings") + 1]).read_text())
    hook = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    completed = subprocess.run(hook, shell=True, input=json.dumps({"session_id": os.environ["ORCHESTRATOR_SESSION_ID"]}), text=True, capture_output=True, timeout=10)
    if completed.returncode:
        raise RuntimeError(completed.stderr + completed.stdout)
    result["hook"] = json.loads(completed.stdout)
print(json.dumps(result))
"""

BRIDGE_HARNESS = """
import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { dirname } from "node:path";
import { execFileSync } from "node:child_process";

const extensionPath = process.argv[2];
const handlers = new Map();
const paths = [];
let tool;
let failure = false;
const require = createRequire(pathToFileURL(process.argv[3]));
const { createJiti } = require("jiti");
const resolver = createJiti(process.argv[3]);
const jiti = createJiti(import.meta.url, {fsCache: false,
  alias: {"@earendil-works/pi-ai": resolver.esmResolve("@earendil-works/pi-ai")}});
const extension = await jiti.import(extensionPath, {default: true});
extension({
  on(name, handler) { handlers.set(name, handler); },
  registerTool(value) { tool = value; },
  sendMessage() {},
  async exec(binary, args, options) {
    assert(options.timeout === 5000);
    const path = args[args.indexOf("--payload-file") + 1];
    assert(path && !args.includes("--payload"));
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.equal((await stat(dirname(path))).mode & 0o777, 0o700);
    paths.push(path);
    // A real exec detects the operating system's single-argument size limit.
    execFileSync(process.execPath, ["-e", "", "--", ...args]);
    const payload = JSON.parse(await readFile(path, "utf8"));
    const action = args[args.indexOf("--action") + 1];
    if (action === "updates") return {code: 0, stdout: "[]", stderr: "", killed: false};
    if (failure) throw new Error("deliberate transport failure");
    assert.equal(payload.prompt, "distinctive long prompt " + "x".repeat(200000));
    return {code: 0, stdout: "{}", stderr: "", killed: false};
  }
});
await handlers.get("session_start")({}, {
  sessionManager: { getBranch: () => [] }, mode: "json", hasUI: false
});
const prompt = "distinctive long prompt " + "x".repeat(200000);
await handlers.get("input")({source: "interactive", text: prompt});
failure = true;
await assert.rejects(tool.execute("id", {action: "record_prompt", payload: {prompt}}), /deliberate transport failure/);
await handlers.get("session_shutdown")();
// Let the initial poll finish its finally block before verifying cleanup.
await new Promise(resolve => setImmediate(resolve));
for (const path of paths) await assert.rejects(stat(path), {code: "ENOENT"});
console.log(JSON.stringify({requests: paths.length}));
"""


class FrontendIntegrationTests(unittest.TestCase):
    def test_native_launch_private_hooks_and_merged_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            home = directory / "private home"
            home.mkdir()
            configuration = home / "config"
            (configuration / "projects").mkdir(parents=True)
            (configuration / "local.toml").write_text('[personalization]\nname = "Local Person"\n')
            (configuration / "projects/demo.toml").write_text(
                '[personalization]\ncommunication_style = "Distinctive project preference"\n'
            )
            config = load_config(home, "demo")
            fake = directory / "fake-frontend"
            fake.write_text(FAKE_FRONTEND)
            fake.chmod(0o700)
            # A distinct installed tree proves paths do not depend on private cwd.
            installed = directory / "installed source's directory"
            (installed / "roles").mkdir(parents=True)
            (installed / "roles/orchestrator.md").write_text(
                "Distinctive coordinator role instruction"
            )
            (installed / ".claude").mkdir()
            shutil.copy(ROOT / ".claude/settings.json", installed / ".claude/settings.json")
            (installed / "bin").mkdir()
            (installed / "bin/orchestrator").symlink_to(ROOT / "bin/orchestrator")
            for frontend in ("claude", "pi"):
                session = str(uuid.uuid4())
                config["frontends"][frontend]["command"] = [str(fake)]
                with patch("orchestrator.frontends.ROOT", installed):
                    command = build_frontend_command(home, config, frontend, session)
                environment = dict(
                    os.environ,
                    ORCHESTRATOR_HOME=str(home),
                    ORCHESTRATOR_SESSION_ID=session,
                    CLAUDE_PROJECT_DIR=str(home),
                )
                environment.pop("ORCHESTRATOR_CHILD", None)
                completed = subprocess.run(
                    command,
                    cwd=home,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                for instruction in (
                    "Distinctive coordinator role instruction",
                    "Local Person",
                    "Distinctive project preference",
                ):
                    self.assertIn(instruction, result["prompt"])
                self.assertEqual(result["cwd"], str(home))
                self.assertEqual(
                    command[command.index("--model") + 1], config["roles"]["orchestrator"]["model"]
                )
                effort_flag = "--effort" if frontend == "claude" else "--thinking"
                self.assertEqual(
                    command[command.index(effort_flag) + 1],
                    config["roles"]["orchestrator"]["effort"],
                )
                if frontend == "claude":
                    self.assertIn(
                        session, result["hook"]["hookSpecificOutput"]["additionalContext"]
                    )
                    self.assertEqual(Store(home).session(session)["frontend"], "claude")

    @unittest.skipUnless(
        shutil.which("node") and shutil.which("pi"),
        "Node and installed Pi are required to execute the Pi bridge",
    )
    def test_pi_large_payload_transport_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "bridge.mjs"
            harness.write_text(BRIDGE_HARNESS)
            environment = dict(
                os.environ, ORCHESTRATOR_HOME=temporary, ORCHESTRATOR_SESSION_ID=str(uuid.uuid4())
            )
            environment.pop("ORCHESTRATOR_CHILD", None)
            completed = subprocess.run(
                [
                    "node",
                    str(harness),
                    str(ROOT / ".pi/extensions/orchestrator.ts"),
                    str(Path(shutil.which("pi")).resolve()),
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertGreaterEqual(json.loads(completed.stdout)["requests"], 3)
