"""Editable project guidance and foreground permission boundaries, without model calls."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.bootstrap import ROOT, bootstrap
from orchestrator.hooks import handle_hook


class SetupFrontendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.environment = patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target in ("orchestrator.hooks.claude_parent", "orchestrator.bootstrap.claude_parent"):
            parent = patch(target, return_value=None)
            parent.start()
            self.addCleanup(parent.stop)

    def assert_setup_guidance(self, instructions):
        for text in (
            "project_setup",
            "setup_project",
            "expected_revision",
            "policy_revision",
            "can_configure",
            "project_settings",
            "configure_project",
            "config/projects/<bound-id>.json",
            "planning.templates",
            "no permanent setup seal",
            "easy, hard, or very-hard",
            "planned and on-demand",
            "same frozen Git commit",
            "eight jobs",
            "coordinator_approvals=true",
            "require_write_approval=false",
            "enforce_monitor_holds=true",
            "approve_plan",
            "resolve_hold",
            "approve_worker",
            "accept_worker",
            "approve_node",
            "cancel_worker",
            "standing authorization",
            "/model and /effort",
            "validation.blockers",
            "never invent preferences or defaults",
            "never force an overwrite",
            "including after deletion or an incomplete draft",
            "Workers have run_command",
            "retry_review",
            "execution.base_ref",
            "execution.unattended=true",
        ):
            self.assertIn(text, instructions)
        for obsolete_claim in (
            "permanently closes initial setup",
            "maintenance or repair remains operator work",
            "require operator authorization through the CLI",
        ):
            self.assertNotIn(obsolete_claim, instructions)

    def test_both_frontend_boot_instructions_explain_conversational_setup(self):
        for frontend in ("claude", "pi"):
            with self.subTest(frontend=frontend):
                initialized = bootstrap(self.home, frontend, f"setup-{frontend}", reserve=False)
                self.assert_setup_guidance(initialized["instructions"])

    def test_claude_startup_context_preserves_setup_guidance(self):
        response = handle_hook(self.home, "SessionStart", {"session_id": "setup-claude"})
        self.assert_setup_guidance(response["hookSpecificOutput"]["additionalContext"])

    def test_bash_stays_denied_even_for_policy_initialization(self):
        bootstrap(self.home, "claude", "setup-claude", reserve=False)
        for command in ("pwd", "mkdir -p .orchestrator", "git submodule update --init"):
            with self.subTest(command=command):
                response = handle_hook(
                    self.home,
                    "PreToolUse",
                    {
                        "session_id": "setup-claude",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                )["hookSpecificOutput"]
                self.assertEqual(response["permissionDecision"], "deny")
                self.assertIn("setup_project", response["permissionDecisionReason"])

    def test_owned_setup_tool_allowed_but_not_cross_session_or_foreign_namespace(self):
        bootstrap(self.home, "claude", "setup-claude", reserve=False)
        for action in (
            "project_setup",
            "setup_project",
            "project_settings",
            "configure_project",
            "approve_plan",
            "resolve_hold",
            "approve_worker",
            "accept_worker",
            "approve_node",
            "cancel_worker",
        ):
            for payload in ({}, {"session_id": "setup-claude"}):
                self.assertEqual(
                    handle_hook(
                        self.home,
                        "PreToolUse",
                        {
                            "session_id": "setup-claude",
                            "tool_name": f"mcp__orchestrator__{action}",
                            "tool_input": payload,
                        },
                    ),
                    {},
                )
        for tool_name, payload in (
            ("mcp__orchestrator__setup_project", {"session_id": "another-instance"}),
            ("mcp__other__setup_project", {}),
            ("mcp__orchestrator__configure_project", {"session_id": "another-instance"}),
            ("mcp__other__configure_project", {}),
            ("mcp__orchestrator__accept_worker", {"session_id": "another-instance"}),
            ("mcp__other__approve_plan", {}),
            ("Write", {"file_path": ".orchestrator/crew-dispatch.json", "content": "{}"}),
        ):
            response = handle_hook(
                self.home,
                "PreToolUse",
                {
                    "session_id": "setup-claude",
                    "tool_name": tool_name,
                    "tool_input": payload,
                },
            )
            self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")


PI_SETUP_HARNESS = r"""
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
const configurations = JSON.parse(process.argv[4]);
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = (callback, milliseconds) =>
  milliseconds === 3000 ? {poll: true} : originalSetTimeout(callback, milliseconds);
globalThis.clearTimeout = timer => {if (!timer?.poll) originalClearTimeout(timer);};
const handlers = new Map(), tools = new Map(), requests = [];
const settings = {execution: {mode: 'restricted'}, commands: {enabled: false, sandbox: true},
  roles: {orchestrator: {adapter: 'claude', model: 'configured', effort: 'low',
    allowed_tools: ['Read', 'Glob', 'Grep']}}};
const builtins = ['read', 'find', 'ls', 'grep', 'bash', 'edit', 'write'];
const api = {
  on: (name, callback) => handlers.set(name, callback),
  registerTool: tool => tools.set(tool.name, tool), registerProvider() {}, registerCommand() {},
  events: {on: () => () => {}, emit() {}},
  getAllTools: () => builtins.map(name => ({name, sourceInfo: {path: `builtin:${name}`}})),
  setActiveTools() {}, setModel: async () => true, setThinkingLevel() {}, appendEntry() {},
  sendMessage() {},
  async exec(binary, args) {
    if (args.includes('bootstrap')) return {code: 0, stdout: JSON.stringify({
      session: {id: 'bridge-session', active: 1, observer: 0, project_id: 'project'},
      instructions: 'Coordinate', config: settings})};
    if (args.includes('close')) return {code: 0, stdout: '{}'};
    const action = args[args.indexOf('--action') + 1];
    const payload = JSON.parse(await readFile(args[args.indexOf('--payload-file') + 1], 'utf8'));
    requests.push({action, payload, session: args[args.indexOf('--session') + 1]});
    if (action === 'configure_project') {
      for (const [section, values] of Object.entries(payload.settings ?? {})) {
        Object.assign(settings[section], values);
      }
    }
    const response = action === 'delivery_updates' ? {ready: false, interrupting: [], silent: []} :
      ['configure_project', 'project_settings'].includes(action) ?
        {settings, project_id: 'project', can_configure: true} : {};
    return {code: 0, stdout: JSON.stringify(response)};
  }
};
const ctx = {mode: 'rpc', hasUI: false, isIdle: () => true, hasPendingMessages: () => false,
  sessionManager: {getSessionId: () => 'bridge-session', getBranch: () => []},
  modelRegistry: {find: () => ({id: 'configured', provider: 'anthropic'}), getAll: () => []},
  abort() {throw Error('Unexpected abort');}};
const blocked = async name => (await handlers.get('tool_call')({toolName: name}, ctx))?.block === true;
extension(api);
await handlers.get('session_start')({}, ctx);
const orchestrator = tools.get('orchestrator');
const forwarded = {};
for (const action of process.argv[5].split(',')) {
  requests.length = 0;
  await orchestrator.execute('id', {action, payload: {session_id: 'foreign', reason: 'authorized'}});
  forwarded[action] = requests.map(({action, session}) => ({action, session}));
}
const restrictedGate = {orchestrator: await blocked('orchestrator'), read: await blocked('read'),
  bash: await blocked('bash'), edit: await blocked('edit')};
const sections = [];
for (const configuration of configurations) {
  await orchestrator.execute('id', {action: 'configure_project', payload: {settings: configuration}});
  const prompt = {systemPromptOptions: {sections: {}}};
  await handlers.get('before_agent_start')(prompt, ctx);
  await handlers.get('agent_end')({}, ctx);
  sections.push({section: prompt.systemPromptOptions.sections.orchestrator,
    bash: await blocked('bash')});
}
await handlers.get('session_shutdown')({}, ctx);
console.log(JSON.stringify({description: orchestrator.description, forwarded, restrictedGate,
  sections}));
"""


@unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Installed Pi required")
class PiSetupToolTests(unittest.TestCase):
    ACTIONS = (
        "project_setup",
        "setup_project",
        "project_settings",
        "configure_project",
        "approve_plan",
        "resolve_hold",
        "approve_worker",
        "accept_worker",
        "approve_node",
        "cancel_worker",
    )
    CONFIGURATIONS = (
        ({"execution": {"mode": "restricted"}, "commands": {"enabled": False}}, "disabled"),
        ({"commands": {"enabled": True}}, "enabled in the OS sandbox"),
        ({"commands": {"sandbox": False}}, "enabled without an OS sandbox (host access)"),
        (
            {"execution": {"mode": "trusted"}, "commands": {"enabled": False, "sandbox": True}},
            "enabled without an OS sandbox (host access)",
        ),
    )

    def run_harness(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "setup.mjs"
            harness.write_text(PI_SETUP_HARNESS)
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
                    json.dumps([settings for settings, _ in self.CONFIGURATIONS]),
                    ",".join(self.ACTIONS),
                ],
                env=dict(environment, PI_OFFLINE="1"),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_setup_and_decision_actions_use_bridge_identity_without_widening_tools(self):
        observed = self.run_harness()
        for action in self.ACTIONS:
            with self.subTest(action=action):
                self.assertIn(action, observed["description"])
                forwarded = observed["forwarded"][action]
                self.assertIn({"action": action, "session": "bridge-session"}, forwarded)
                self.assertEqual({request["session"] for request in forwarded}, {"bridge-session"})
        self.assertEqual(
            observed["restrictedGate"],
            {"orchestrator": False, "read": False, "bash": True, "edit": True},
        )
        description = observed["description"]
        self.assertNotIn("Candidates are not automatically accepted", description)
        self.assertIn("execution.unattended=true", description)
        self.assertIn("remaining_issues", description)
        self.assertIn("commands.sandbox=false", description)

    def test_prompt_reports_effective_worker_commands_per_configuration(self):
        observed = self.run_harness()
        for (settings, expected), result in zip(
            self.CONFIGURATIONS, observed["sections"], strict=True
        ):
            with self.subTest(settings=settings):
                self.assertIn(f"worker run_command {expected}.", result["section"])
                trusted = "execution.mode=trusted" in result["section"]
                self.assertEqual(trusted, settings.get("execution", {}).get("mode") == "trusted")
                self.assertEqual(result["bash"], not trusted)
