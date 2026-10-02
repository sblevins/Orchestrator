"""Project-local foreground authority without inference or shell execution."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.api import request
from orchestrator.bootstrap import ROOT, bootstrap
from orchestrator.hooks import handle_hook
from orchestrator.store import StateError, Store


class TrustedClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        parent = patch("orchestrator.bootstrap.claude_parent", return_value=None)
        parent.start()
        self.addCleanup(parent.stop)
        service = patch("orchestrator.api._start_service", return_value={"started": False})
        service.start()
        self.addCleanup(service.stop)
        bootstrap(self.home, "claude", "writer", reserve=False)
        request(
            self.home,
            "writer",
            "register_project",
            {"project_id": "project", "root": str(self.home)},
        )
        request(self.home, "writer", "bind_project", {"project_id": "project"})
        request(self.home, "writer", "setup_project", {})

    def configure(self, mode):
        return request(
            self.home, "writer", "configure_project", {"settings": {"execution": {"mode": mode}}}
        )

    def hook(self, tool, session="writer", **values):
        return handle_hook(
            self.home,
            "PreToolUse",
            {"session_id": session, "tool_name": tool, "tool_input": {}, **values},
        )

    def assert_denied(self, response):
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_conversational_trust_and_restriction_are_live(self):
        self.assert_denied(self.hook("Bash", tool_input={"command": "git status"}))
        self.assertEqual(self.hook("AskUserQuestion"), {})
        self.assert_denied(self.hook("EnterPlanMode"))
        self.configure("trusted")
        for tool in ("Bash", "Write", "Edit", "Agent", "WebSearch"):
            with self.subTest(tool=tool):
                self.assertEqual(self.hook(tool), {})
        self.configure("restricted")
        self.assert_denied(self.hook("Bash"))
        self.assert_denied(self.hook("Agent"))

    def test_trust_never_relaxes_identity_or_watcher_gates(self):
        self.configure("trusted")
        self.assert_denied(
            self.hook("mcp__orchestrator__status", tool_input={"session_id": "foreign"})
        )
        self.assert_denied(self.hook("mcp__other__configure_project"))
        self.assert_denied(self.hook("Bash", agent_type="orchestrator-watcher"))
        self.assert_denied(self.hook("Agent", tool_input={"subagent_type": "orchestrator-watcher"}))
        self.assertEqual(self.hook("mcp__orchestrator__status"), {})
        store = Store(self.home)
        store.open_session("observer", "claude", "project", observer=True)
        self.assert_denied(self.hook("Bash", session="observer"))
        with self.assertRaises(StateError):
            request(
                self.home,
                "observer",
                "configure_project",
                {"settings": {"execution": {"mode": "trusted"}}},
            )
        store.set_service_value("native-alias:claude:foreign-native", "observer")
        with patch.dict(os.environ, {"ORCHESTRATOR_SESSION_ID": "writer"}):
            self.assert_denied(self.hook("Bash", session="foreign-native"))
        store.open_session("unbound", "claude")
        self.assert_denied(self.hook("Bash", session="unbound"))
        store.open_session("replacement", "claude", "project", takeover=True)
        self.assert_denied(self.hook("Bash"))

    def test_other_projects_remain_restricted(self):
        self.configure("trusted")
        (self.home / "other").mkdir()
        request(
            self.home,
            "writer",
            "register_project",
            {"project_id": "other", "root": str(self.home / "other")},
        )
        Store(self.home).open_session("other-writer", "claude", "other")
        self.assert_denied(self.hook("Bash", session="other-writer"))


PI_TRUST_HARNESS = r"""
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
const handlers = new Map(), tools = new Map(), requests = [], messages = [], timers = [];
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = (callback, milliseconds) => {
  if (milliseconds !== 3000) return originalSetTimeout(callback, milliseconds);
  timers.push(callback); return {poll: true};
};
globalThis.clearTimeout = timer => {if (!timer?.poll) originalClearTimeout(timer);};
let activeTools = [], mode = 'restricted', owner = true, bound = true;
let delivery = {ready: false, interrupting: [], silent: []};
const config = () => ({execution: {mode}, roles: {orchestrator: {
  adapter: 'claude', model: 'configured', effort: 'low', allowed_tools: ['Read', 'Glob', 'Grep']}}});
const builtins = ['read', 'find', 'ls', 'grep', 'bash', 'edit', 'write', 'agent'];
const inventory = builtins.map(name => ({name, sourceInfo: {path: `builtin:${name}`}, exposure: 'direct'}));
inventory.push({name: 'third_party', sourceInfo: {path: '/extensions/third-party.ts'}, exposure: 'direct'});
const api = {
  on: (name, callback) => handlers.set(name, callback),
  registerTool: tool => tools.set(tool.name, tool), registerProvider() {}, registerCommand() {},
  events: {on: () => () => {}, emit() {}},
  getAllTools: () => inventory,
  setActiveTools: names => {activeTools = names;},
  setModel: async () => true, setThinkingLevel() {}, appendEntry() {},
  sendMessage: (message, options) => messages.push({message, options}),
  async exec(binary, args) {
    if (args.includes('bootstrap')) return {code: 0, stdout: JSON.stringify({
      session: {id: 'session', active: owner ? 1 : 0, observer: 0, project_id: bound ? 'project' : null},
      instructions: 'Quiet coordination', config: config()})};
    if (args.includes('close')) return {code: 0, stdout: '{}'};
    const action = args[args.indexOf('--action') + 1];
    const payload = JSON.parse(await readFile(args[args.indexOf('--payload-file') + 1], 'utf8'));
    requests.push({action, payload});
    if (action === 'configure_project') mode = payload.settings.execution.mode;
    if (action === 'bind_project') bound = true;
    if (action === 'project_settings' && !owner) return {code: 1, stderr: 'Inactive session'};
    const response = action === 'delivery_updates' ? delivery :
      ['configure_project', 'project_settings'].includes(action) ?
        {settings: config(), project_id: 'project', can_configure: owner && bound} : {};
    return {code: 0, stdout: JSON.stringify(response)};
  }
};
const ctx = {mode: 'rpc', hasUI: false, isIdle: () => true, hasPendingMessages: () => false,
  sessionManager: {getSessionId: () => 'session', getBranch: () => []},
  modelRegistry: {find: () => ({id: 'configured', provider: 'anthropic'}), getAll: () => []},
  abort() {throw Error('Unexpected abort');}};
async function waitFor(predicate) {
  for (let attempt = 0; attempt < 1000; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => originalSetTimeout(resolve, 2));
  }
  throw Error('Timed out waiting for local callback');
}
const call = name => handlers.get('tool_call')({toolName: name}, ctx);
const configure = mode => tools.get('orchestrator').execute('tool-id',
  {action: 'configure_project', payload: {settings: {execution: {mode}}}});
const expectedRestricted = ['read', 'find', 'ls', 'grep', 'orchestrator'].sort();
extension(api);
await handlers.get('session_start')({}, ctx);
await waitFor(() => timers.length > 0);
assert.deepEqual([...activeTools].sort(), expectedRestricted);
assert.equal((await call('bash')).block, true);
await configure('trusted');
assert.deepEqual([...activeTools].sort(), [...builtins, 'orchestrator'].sort());
for (const name of builtins) assert.equal(await call(name), undefined);
assert.equal((await call('third_party')).block, true);
inventory.push({name: 'late_tool', sourceInfo: {path: 'builtin:late_tool'}, exposure: 'direct'});
assert.equal((await call('late_tool')).block, true);
// Monitor lifecycle remains live with trusted native tools.
const prompt = {systemPromptOptions: {sections: {}}};
await handlers.get('before_agent_start')(prompt, ctx);
assert.match(prompt.systemPromptOptions.sections.orchestrator, /execution.mode=trusted/);
const turn = requests.find(request => request.action === 'foreground_start').payload.turn_id;
await handlers.get('agent_end')({}, ctx);
assert.equal(requests.find(request => request.action === 'foreground_finished').payload.turn_id, turn);
delivery = {ready: true, interrupting: [], silent: [{id: 7, kind: 'monitor.findings'}]};
timers.shift()();
await waitFor(() => messages.length === 1);
assert.equal(messages[0].message.display, false);
assert.deepEqual(messages[0].options, {deliverAs: 'nextTurn', triggerTurn: false});
await configure('restricted');
assert.deepEqual([...activeTools].sort(), expectedRestricted);
assert.equal((await call('agent')).block, true);
await configure('trusted');
owner = false;
assert.equal((await call('bash')).block, true);
await handlers.get('session_shutdown')({}, ctx);
await waitFor(() => timers.length > 0);
timers.length = 0;
// A trusted project restores tools at startup, but never for unbound sessions.
owner = true;
await handlers.get('session_start')({}, ctx);
assert(activeTools.includes('bash'));
assert(!activeTools.includes('late_tool'));
await waitFor(() => timers.length > 0);
await handlers.get('session_shutdown')({}, ctx);
timers.length = 0;
bound = false;
await handlers.get('session_start')({}, ctx);
assert(!activeTools.includes('bash'));
await tools.get('orchestrator').execute('bind-id', {action: 'bind_project', payload: {project_id: 'project'}});
assert(activeTools.includes('bash'));
await waitFor(() => timers.length > 0);
await handlers.get('session_shutdown')({}, ctx);
console.log('trusted foreground passed');
"""


@unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Installed Pi is required")
class TrustedPiTests(unittest.TestCase):
    def test_installed_sdk_tool_restoration_and_monitor_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "trusted.mjs"
            harness.write_text(PI_TRUST_HARNESS)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("ORCHESTRATOR_") and key != "NO_MISTAKES_GATE"
            }
            result = subprocess.run(
                [
                    "node",
                    str(harness),
                    str(ROOT / ".pi/extensions/orchestrator.ts"),
                    str(Path(shutil.which("pi")).resolve()),
                ],
                env=dict(environment, PI_OFFLINE="1"),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("trusted foreground passed", result.stdout)
