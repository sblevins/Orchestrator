"""External validation is not an interactive coordinator and must not start its guards."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ExternalValidationContextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Installed Pi required")
    def test_pi_validation_leaves_native_tools_and_lifecycle_alone(self):
        script = r"""
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require = createRequire(pathToFileURL(process.argv[3]));
const {createJiti} = require('jiti');
const resolver = createJiti(process.argv[3]);
const jiti = createJiti(import.meta.url, {fsCache:false,
  alias:{'@earendil-works/pi-ai': resolver.esmResolve('@earendil-works/pi-ai')}});
const extension = await jiti.import(process.argv[2], {default:true});
const handlers = new Map();
let effects = 0;
extension({on(name, callback) {handlers.set(name, callback);},
  registerTool() {}, registerCommand() {},
  exec() {effects++; throw Error('must not bootstrap');},
  setActiveTools() {effects++;}, setModel() {effects++;},
  events:{on() {return () => {};}, emit() {effects++;}}});
const context = {abort() {effects++;}};
for (const name of ['session_start','before_agent_start','tool_call','agent_end']) {
  const result = await handlers.get(name)({toolName:'bash'}, context);
  assert.notEqual(result?.block, true);
}
assert.equal(effects, 0);
console.log('external validation remains externally managed');
"""
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "external.mjs"
            harness.write_text(script)
            result = subprocess.run(
                [
                    "node",
                    str(harness),
                    str(ROOT / ".pi/extensions/orchestrator.ts"),
                    str(Path(shutil.which("pi")).resolve()),
                ],
                env={**os.environ, "NO_MISTAKES_GATE": "validation-fixture", "PI_OFFLINE": "1"},
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("externally managed", result.stdout)

    def test_hooks_and_long_watch_are_inert_without_state_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "untouched-state"
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": temporary,
                "NO_MISTAKES_GATE": "external-validation-fixture",
            }
            for event, tool in (
                ("SessionStart", None),
                ("UserPromptSubmit", None),
                ("PreToolUse", "Bash"),
                ("PreToolUse", "Agent"),
                ("PreToolUse", "StructuredOutput"),
                ("Stop", None),
                ("SessionEnd", None),
            ):
                with self.subTest(event=event, tool=tool):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "bin/orchestrator"),
                            "--home",
                            str(home),
                            "hooks",
                            event,
                        ],
                        input=json.dumps(
                            {
                                "session_id": "external-review",
                                "tool_name": tool,
                                "tool_input": {},
                                "prompt": "Review the repository",
                            }
                        ),
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {})
                    self.assertFalse(home.exists())
            watched = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bin/orchestrator"),
                    "--home",
                    str(home),
                    "watch",
                    "--seconds",
                    "27000",
                ],
                input="must not read hook input",
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(watched.returncode, 0, watched.stderr)
            self.assertEqual(watched.stdout, "")
            self.assertFalse(home.exists())
