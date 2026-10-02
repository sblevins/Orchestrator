"""Owned Pi extension lifecycle and installed-Pi auto-discovery, without model calls."""

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from orchestrator.frontends import ROOT
from orchestrator.store import Store

NATIVE_HARNESS = r"""
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {readFile} from 'node:fs/promises';
const require = createRequire(pathToFileURL(process.argv[3]));
const {createJiti} = require('jiti');
const resolver = createJiti(process.argv[3]);
const jiti = createJiti(import.meta.url, {fsCache: false,
  alias: {'@earendil-works/pi-ai': resolver.esmResolve('@earendil-works/pi-ai')}});
const extension = await jiti.import(process.argv[2], {default: true});
const handlers = new Map();
const entries = [];
const calls = [];
const notices = [];
let tools, tool, selections = 0, effort, modelExists = true, authenticate = true;
let roleAdapter = 'claude', configuredProvider, expectedProvider = 'anthropic';
let configuredModel = 'configured', expectedModel = 'configured';
let catalog = [];
const api = {
  on: (name, callback) => handlers.set(name, callback),
  registerTool: value => {tool = value;},
  registerProvider() {}, registerCommand() {},
  events: {on: () => () => {}, emit() {}},
  setActiveTools: names => {tools = names;},
  setModel: async model => {assert.equal(model.id, expectedModel); selections++; return authenticate;},
  setThinkingLevel: value => {effort = value;},
  appendEntry: (customType, data) => entries.push({type: 'custom', customType, data}),
  sendMessage() {},
  async exec(binary, args) {
    calls.push(args);
    assert.equal(args[1], process.argv[4]);
    if (args.includes('bootstrap')) {
      assert.equal(args[args.indexOf('--session') + 1], 'native-session');
      assert.equal(args[args.indexOf('--pid') + 1], String(process.pid));
      return {code: 0, stdout: JSON.stringify({session: {id: 'native-session'},
        config: {roles: {orchestrator: {adapter: roleAdapter, provider: configuredProvider, model: configuredModel, effort: 'low', allowed_tools: ['Read', 'Glob', 'Grep']}}},
        instructions: 'Role instructions\nPersonalization\nProject context'})};
    }
    if (args.includes('close')) return {code: 0, stdout: '{}'};
    assert(calls[0].includes('bootstrap'));
    const action = args[args.indexOf('--action') + 1];
    const payload = JSON.parse(await readFile(args[args.indexOf('--payload-file') + 1], 'utf8'));
    if (action === 'record_prompt') assert.equal(payload.prompt, '  Exact input\nwith Unicode: café  ');
    return {code: 0, stdout: action === 'updates' ? '[]' : '{}'};
  }
};
const ctx = {hasUI: true, mode: 'rpc', ui: {notify: message => notices.push(message), setStatus() {}},
  sessionManager: {getSessionId: () => 'native-session', getBranch: () => entries},
  modelRegistry: {
    find(provider, id) {assert.equal(provider, expectedProvider); assert.equal(id, configuredModel); return modelExists ? {id, provider} : undefined;},
    getAll: () => catalog,
  },
  abort() {throw Error('Unexpected abort');}
};
extension(api);
await handlers.get('session_start')({}, ctx);
assert.equal(selections, 1);
assert.equal(effort, 'low');
assert.deepEqual(new Set(tools), new Set(['read', 'find', 'ls', 'grep', 'orchestrator']));
const prompt = {systemPromptOptions: {sections: {original: 'keep'}}};
await handlers.get('before_agent_start')(prompt, ctx);
assert.equal(prompt.systemPromptOptions.sections.original, 'keep');
assert.match(prompt.systemPromptOptions.sections.orchestrator, /Project context/);
assert.deepEqual(await handlers.get('input')({source: 'interactive', text: '  Exact input\nwith Unicode: café  '}, ctx), {action: 'continue'});
assert.equal((await handlers.get('tool_call')({toolName: 'bash'}, ctx)).block, true);
assert.equal((await handlers.get('tool_call')({toolName: 'third-party-bridge'}, ctx)).block, true);
assert.equal(await handlers.get('tool_call')({toolName: 'read'}, ctx), undefined);
await handlers.get('before_agent_start')(prompt, ctx);
assert.equal(selections, 1); // A turn must not undo the user's /model choice.
await handlers.get('session_shutdown')({}, ctx);
assert(calls.some(args => args.includes('close')));
extension(api); // /reload creates a fresh extension factory.
await handlers.get('session_start')({}, ctx);
assert.equal(selections, 1); // Nor may reload reset that choice in the same process.
await handlers.get('session_shutdown')({}, ctx);
entries.length = 0;
modelExists = false;
extension(api);
await handlers.get('session_start')({}, ctx);
assert.deepEqual(tools, []);
assert(notices.some(message => /model not found/.test(message)));
assert.deepEqual(await handlers.get('input')({source: 'interactive', text: 'never sent'}, ctx), {action: 'handled'});
assert.equal((await handlers.get('tool_call')({toolName: 'read'}, ctx)).block, true);
await assert.rejects(tool.execute('id', {action: 'projects'}), /model not found/);
await handlers.get('session_shutdown')({}, ctx);
modelExists = true;
authenticate = false;
extension(api);
await handlers.get('session_start')({}, ctx);
assert.deepEqual(tools, []);
assert(notices.some(message => /Authentication unavailable/.test(message)));
await handlers.get('session_shutdown')({}, ctx);
authenticate = true;
roleAdapter = 'pi';
configuredProvider = 'openai-codex';
expectedProvider = configuredProvider;
entries.length = 0;
const previousSelections = selections;
extension(api);
await handlers.get('session_start')({}, ctx);
assert.equal(selections, previousSelections + 1);
assert.deepEqual(new Set(tools), new Set(['read', 'find', 'ls', 'grep', 'orchestrator']));
for (const action of ['routing_policy', 'request_worker', 'worker', 'workers', 'select_worker', 'refresh_worker_policy']) {
  assert(tool.description.includes(action));
}
assert.match(tool.description, /tracked background sub-agents/);
assert.match(tool.description, /never Herder tabs or windows/);
assert.match(tool.description, /never global.*or another project/);
await handlers.get('session_shutdown')({}, ctx);
for (const invalidProvider of [undefined, '', ' ', false]) {
  entries.length = 0;
  configuredProvider = invalidProvider;
  const selectionCount = selections;
  extension(api);
  await handlers.get('session_start')({}, ctx);
  assert.deepEqual(tools, []);
  assert.equal(selections, selectionCount);
  assert.equal((await handlers.get('tool_call')({toolName: 'read'}, ctx)).block, true);
  await handlers.get('session_shutdown')({}, ctx);
}
// Family launches omit the CLI --model, so the extension must select once.
roleAdapter = 'claude';
expectedProvider = 'anthropic';
configuredModel = 'OpUs';
expectedModel = 'claude-opus-4-10';
modelExists = false;
catalog = ['claude-opus-4-9', expectedModel].map(id => ({id, provider: 'anthropic'}));
process.env.ORCHESTRATOR_HOME = process.argv[4];
process.env.ORCHESTRATOR_SESSION_ID = 'native-session';
entries.length = 0;
let selectionCount = selections;
extension(api);
await handlers.get('session_start')({}, ctx);
assert.equal(selections, selectionCount + 1);
assert.equal(entries.at(-1).data.requestedModel, 'OpUs');
assert.equal(entries.at(-1).data.resolvedModel, expectedModel);
assert.equal(entries.at(-1).data.provider, 'anthropic');
assert.equal(entries.at(-1).data.resolution, 'latest-known-stable-catalog');
await handlers.get('session_shutdown')({}, ctx);
extension(api);
await handlers.get('session_start')({}, ctx);
assert.equal(selections, selectionCount + 1); // Reload keeps a manual /model choice.
await handlers.get('session_shutdown')({}, ctx);
// A resumed session in another process must resolve again against its current catalog.
entries.at(-1).data.pid = -1;
extension(api);
await handlers.get('session_start')({}, ctx);
assert.equal(selections, selectionCount + 2);
await handlers.get('session_shutdown')({}, ctx);
// Availability never downgrades to the authenticated older entry.
entries.length = 0;
authenticate = false;
selectionCount = selections;
extension(api);
await handlers.get('session_start')({}, ctx);
assert.equal(selections, selectionCount + 1);
assert.deepEqual(tools, []);
assert(notices.some(message => message.includes('Authentication unavailable') && message.includes(expectedModel)));
assert.equal(entries.length, 0);
await handlers.get('session_shutdown')({}, ctx);
// Exact launcher selections remain the launcher's responsibility.
authenticate = true;
configuredModel = 'claude-opus-4-9';
selectionCount = selections;
extension(api);
await handlers.get('session_start')({}, ctx);
assert.equal(selections, selectionCount);
assert(tools.includes('orchestrator'));
await handlers.get('session_shutdown')({}, ctx);
console.log('native lifecycle passed');
"""


# Record the boundary, then run the real CLI in an isolated copied installation.
# Only resource accounting is faked; the test runner already reserves these children.
CLI_RECORDER = r"""#!/usr/bin/env python3
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from orchestrator.cli import main
arguments = sys.argv[1:]
home = pathlib.Path(arguments[arguments.index('--home') + 1])
with (home / 'calls.jsonl').open('a') as output:
    output.write(json.dumps(arguments) + '\n')
raise SystemExit(main())
"""


@unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Installed Pi is required")
class NativePiTests(unittest.TestCase):
    def clean_environment(self, directory):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ORCHESTRATOR_") and key != "NO_MISTAKES_GATE"
        }
        # Isolate personal plugins and credentials, not project extension discovery.
        environment["PI_CODING_AGENT_DIR"] = str(directory / "agent")
        environment["PI_OFFLINE"] = "1"
        return environment

    def test_native_lifecycle_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            harness = directory / "native.mjs"
            harness.write_text(NATIVE_HARNESS)
            completed = subprocess.run(
                [
                    "node",
                    str(harness),
                    str(ROOT / ".pi/extensions/orchestrator.ts"),
                    str(Path(shutil.which("pi")).resolve()),
                    str(ROOT),
                ],
                env=self.clean_environment(directory),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("native lifecycle passed", completed.stdout)

    def test_installed_pi_discovers_native_extension_without_launcher(self):
        for configured_model in (
            "claude-native-offline-fixture",
            "claude-nonexistent-orchestrator-model",
            "OpUs",
        ):
            with self.subTest(model=configured_model), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                project = directory / "Orchestrator"
                (project / ".pi/extensions").mkdir(parents=True)
                shutil.copy(
                    ROOT / ".pi/extensions/orchestrator.ts",
                    project / ".pi/extensions/orchestrator.ts",
                )
                shutil.copytree(ROOT / ".pi/lib", project / ".pi/lib")
                shutil.copytree(ROOT / ".pi/agents", project / ".pi/agents")
                (project / "bin").mkdir()
                binary = project / "bin/orchestrator"
                binary.write_text(CLI_RECORDER)
                binary.chmod(0o700)
                shutil.copytree(
                    ROOT / "orchestrator",
                    project / "orchestrator",
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                shutil.copytree(ROOT / "roles", project / "roles")
                (project / "config").mkdir()
                shutil.copy(ROOT / "config/default.toml", project / "config/default.toml")
                (project / "config/local.toml").write_text(
                    "[roles.orchestrator]\nmodel = " + json.dumps(configured_model) + "\n"
                )
                registry = project / "bin/machine-resources"
                registry.write_text(
                    '#!/usr/bin/env python3\nimport sys\nassert sys.argv[1] in ("status", "claim")\n'
                )
                registry.chmod(0o700)
                agent = directory / "agent"
                agent.mkdir()
                (agent / "models.json").write_text(
                    json.dumps(
                        {
                            "providers": {
                                "anthropic": {
                                    "baseUrl": "http://127.0.0.1:1",
                                    "api": "anthropic-messages",
                                    "apiKey": "offline-test-only",
                                    "models": [
                                        {"id": "claude-native-offline-fixture", "reasoning": True},
                                        {"id": "claude-opus-999-9", "reasoning": True},
                                        {"id": "claude-opus-999-10", "reasoning": True},
                                    ],
                                }
                            }
                        }
                    )
                )
                environment = self.clean_environment(directory)
                environment["PATH"] = str(project / "bin") + os.pathsep + environment["PATH"]
                self.assertFalse(any(key.startswith("ORCHESTRATOR_") for key in environment))
                process = subprocess.Popen(
                    ["pi", "--mode", "rpc", "--offline", "--approve", "--no-session"],
                    cwd=project,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                records = queue.Queue()

                def read_records(process=process, records=records):
                    for line in process.stdout:
                        records.put(json.loads(line))

                reader = threading.Thread(target=read_records, daemon=True)
                reader.start()

                def request(command, process=process, records=records):
                    process.stdin.write(json.dumps(command) + "\n")
                    process.stdin.flush()
                    observed = []
                    while True:
                        record = records.get(timeout=30)
                        observed.append(record)
                        if record.get("id") == command["id"] and record.get("type") == "response":
                            return record, observed

                try:
                    response, observed = request({"id": "state", "type": "get_state"})
                    self.assertTrue(response["success"], response)
                    if configured_model in ("claude-native-offline-fixture", "OpUs"):
                        expected_model = (
                            "claude-opus-999-10" if configured_model == "OpUs" else configured_model
                        )
                        self.assertEqual(response["data"]["model"]["id"], expected_model, observed)
                        self.assertEqual(response["data"]["thinkingLevel"], "low")
                    else:
                        self.assertTrue(
                            any(
                                "model not found" in record.get("message", "")
                                for record in observed
                            ),
                            observed,
                        )
                        response, observed = request(
                            {
                                "id": "blocked",
                                "type": "prompt",
                                "message": "Must not reach a provider",
                            }
                        )
                        self.assertEqual(response["data"]["disposition"], "handled", response)
                        self.assertFalse(
                            any(record.get("type") == "agent_start" for record in observed)
                        )
                    process.stdin.close()
                    process.wait(timeout=20)
                    reader.join(timeout=5)
                    self.assertEqual(process.returncode, 0, process.stderr.read())
                    calls = [
                        json.loads(line)
                        for line in (project / "calls.jsonl").read_text().splitlines()
                    ]
                    self.assertIn("bootstrap", calls[0])
                    self.assertEqual(calls[0][1], str(project))
                    self.assertEqual(calls[0][calls[0].index("--pid") + 1], str(process.pid))
                    native_id = calls[0][calls[0].index("--session") + 1]
                    self.assertTrue(native_id)
                    self.assertTrue(any("close" in call and native_id in call for call in calls))
                    store = Store(project)
                    self.assertFalse(store.session(native_id)["active"])
                    owner = json.loads(store.service_value(f"native-owner:{native_id}"))
                    self.assertEqual(owner["pid"], process.pid)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
