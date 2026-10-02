"""Real inbox/lifecycle delivery, with controlled time and a model-free Pi SDK boundary."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.cli import watch
from orchestrator.frontends import ROOT
from orchestrator.hooks import handle_hook
from orchestrator.monitoring import begin_turn, delivery_updates, finish_turn, should_review
from orchestrator.store import StateError, Store


class MonitorDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = Store(self.home)
        self.store.add_project("project", str(self.home))
        self.store.open_session("writer", "test", "project")
        self.clock = patch("orchestrator.monitoring.time.time", return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def notify(self, kind="monitor.findings", severity="warning", project="project"):
        with self.store.transaction() as database:
            return self.store._event(
                database,
                project,
                kind,
                {"findings": [{"severity": severity, "summary": "Evidence"}]},
                notify=True,
            )

    def review_ready(self, project="project"):
        with self.store.transaction() as database:
            return should_review(database, project, 20)

    def test_dead_foreground_owner_does_not_stall_background_work(self):
        from orchestrator.runtime import process_identity

        process = subprocess.Popen(["/usr/bin/sleep", "30"])
        try:
            owner = {"pid": process.pid, "identity": process_identity(process.pid)}
            self.store.set_service_value("native-owner:writer", json.dumps(owner))
            begin_turn(self.store, "writer", "interrupted")
            self.assertFalse(self.review_ready())
            process.terminate()
            process.wait(timeout=5)
            self.assertTrue(self.review_ready())
            # A replacement live owner must not inherit a false completed state.
            self.store.set_service_value(
                "native-owner:writer",
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "identity": process_identity(os.getpid()),
                    }
                ),
            )
            self.assertFalse(self.review_ready())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_busy_completion_quiet_and_nonconsuming_reads(self):
        event_id = self.notify(severity="blocking")
        self.assertTrue(self.review_ready())  # Legacy CLI remains usable.
        begin_turn(self.store, "writer", "first")
        self.assertFalse(self.review_ready())
        self.assertEqual(
            delivery_updates(self.store, "writer"),
            {"ready": False, "interrupting": [], "silent": []},
        )
        finish_turn(self.store, "writer", "first")
        self.now.return_value = 119.999
        self.assertFalse(self.review_ready())
        self.now.return_value = 120
        self.assertTrue(self.review_ready())
        self.assertEqual(delivery_updates(self.store, "writer")["interrupting"][0]["id"], event_id)
        self.assertEqual(self.store.updates("writer")[0]["id"], event_id)
        self.assertEqual(delivery_updates(self.store, "writer")["interrupting"][0]["id"], event_id)

    def test_stale_and_duplicate_callbacks(self):
        begin_turn(self.store, "writer", "old")
        begin_turn(self.store, "writer", "new")
        self.assertTrue(finish_turn(self.store, "writer", "old")["active"])
        self.assertFalse(self.review_ready())
        finish_turn(self.store, "writer", "new")
        self.now.return_value = 120
        self.assertFalse(begin_turn(self.store, "writer", "new")["active"])
        self.assertEqual(finish_turn(self.store, "writer")["completed"], 100)
        self.assertTrue(self.review_ready())

    def test_unbound_writer_and_observer_permissions(self):
        self.store.open_session("unbound", "test")
        begin_turn(self.store, "unbound", "routing")
        finish_turn(self.store, "unbound", "routing")
        self.store.open_session("observer", "test", "project", observer=True)
        for operation in (begin_turn, finish_turn):
            with self.assertRaises(StateError):
                operation(self.store, "observer", "forbidden")
        self.assertIsNone(self.store.service_value("foreground:observer"))
        begin_turn(self.store, "writer", "busy")
        self.assertFalse(delivery_updates(self.store, "observer")["ready"])

    def test_two_projects_and_takeover_ignore_old_busy_marker(self):
        second = self.home / "second"
        second.mkdir()
        self.store.add_project("second", str(second))
        self.store.open_session("other", "test", "second")
        begin_turn(self.store, "writer", "busy")
        self.notify(severity="blocking", project="second")
        self.assertTrue(self.review_ready("second"))
        self.assertEqual(len(delivery_updates(self.store, "other")["interrupting"]), 1)
        self.assertEqual(self.store.updates("writer"), [])
        self.store.open_session("replacement", "test", "project", takeover=True)
        self.assertTrue(self.review_ready())
        self.assertFalse(delivery_updates(self.store, "writer")["ready"])
        with self.assertRaises(StateError):
            finish_turn(self.store, "writer", "busy")

    def test_warning_backlog_cannot_hide_later_blocking_or_progress(self):
        for _ in range(130):
            self.notify()
        blocking = self.notify(severity="blocking")
        progress = self.notify(kind="worker.completed")
        self.notify(kind="monitor.unavailable")
        self.notify(kind="worker.routing_recommended")
        result = delivery_updates(self.store, "writer")
        self.assertEqual([event["id"] for event in result["interrupting"]], [blocking, progress])
        self.assertEqual(len(result["silent"]), 50)
        with self.store.transaction() as database:
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM inbox WHERE acknowledged IS NULL"
                ).fetchone()[0],
                134,
            )

    def test_unavailable_and_routing_are_silent_even_with_blocking_payload(self):
        self.notify(kind="monitor.unavailable", severity="blocking")
        self.notify(kind="worker.routing_recommended", severity="blocking")
        result = delivery_updates(self.store, "writer")
        self.assertEqual(result["interrupting"], [])
        self.assertEqual(len(result["silent"]), 2)

    def test_cli_watch_warning_does_not_wake_but_blocking_does(self):
        self.notify()
        self.assertEqual(watch(self.home, "writer", 0.001), 0)
        self.assertIsNone(self.store.service_value("wake:writer"))
        self.notify(severity="blocking")
        begin_turn(self.store, "writer", "turn")
        self.assertEqual(watch(self.home, "writer", 0.001), 0)
        finish_turn(self.store, "writer", "turn")
        self.assertEqual(watch(self.home, "writer", 0.001), 0)
        self.now.return_value = 120
        with patch("sys.stderr"):
            self.assertEqual(watch(self.home, "writer", 0.1), 2)
        self.assertEqual(len(self.store.updates("writer")), 2)

    def test_claude_routine_stop_finishes_and_recursive_stop_does_not(self):
        with (
            patch("orchestrator.hooks.resolve_claude_session", return_value="writer"),
            patch("orchestrator.hooks.verify_claude_owner", return_value="writer"),
        ):
            handle_hook(self.home, "UserPromptSubmit", {"session_id": "native", "prompt": "hello"})
            self.assertFalse(self.review_ready())
            handle_hook(self.home, "Stop", {"session_id": "native", "stop_hook_active": True})
            self.assertFalse(self.review_ready())
            self.assertEqual(
                handle_hook(
                    self.home, "Stop", {"session_id": "native", "last_assistant_message": "Hello!"}
                ),
                {},
            )
            self.now.return_value = 120
            self.assertTrue(self.review_ready())
            self.notify()
            context = handle_hook(
                self.home, "UserPromptSubmit", {"session_id": "native", "prompt": "continue"}
            )
            self.assertIn("monitor.findings", json.dumps(context))
            self.assertFalse(self.review_ready())
            self.assertEqual(
                handle_hook(
                    self.home, "PostToolUse", {"session_id": "native", "tool_name": "Read"}
                ),
                {},
            )


PI_DELIVERY_HARNESS = r"""
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
const handlers = new Map(), messages = [], requests = [], timers = [];
let delivery = {ready: false, interrupting: [], silent: []}, busy = false, pending = false;
let finishResolve, failedFinishes = 0, persistedTurn;
const scenario = process.argv[4] ?? 'delivery';
const readOnly = scenario === 'observer' || scenario === 'inactive';
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = (callback, milliseconds) => {
  if (milliseconds !== 3000) return originalSetTimeout(callback, milliseconds);
  timers.push(callback); return {poll: true};
};
globalThis.clearTimeout = timer => {if (!timer?.poll) originalClearTimeout(timer);};
const api = {
  on: (name, callback) => handlers.set(name, callback),
  registerTool() {}, registerProvider() {}, registerCommand() {},
  events: {on: () => () => {}, emit() {}},
  setActiveTools() {}, setModel: async () => true, setThinkingLevel() {}, appendEntry() {},
  sendMessage: (message, options) => messages.push({message, options}),
  async exec(binary, args) {
    if (args.includes('bootstrap')) return {code: 0, stdout: JSON.stringify({
      session: {id: 'session', active: scenario === 'inactive' ? 0 : 1,
        observer: scenario === 'observer' ? 1 : 0}, instructions: 'Quiet coordination',
      config: {roles: {orchestrator: {adapter: 'claude', model: 'configured', effort: 'low', allowed_tools: ['Read']}}}
    })};
    if (args.includes('close')) return {code: 0, stdout: '{}'};
    const action = args[args.indexOf('--action') + 1];
    const payload = JSON.parse(await readFile(args[args.indexOf('--payload-file') + 1], 'utf8'));
    requests.push({action, payload});
    if (['foreground_start', 'foreground_finished'].includes(action) && readOnly)
      return {code: 1, stderr: 'Foreground lifecycle requires an active writable session'};
    if (action === 'foreground_start') persistedTurn = {token: payload.turn_id, active: true};
    if (action === 'foreground_finished') {
      if (failedFinishes > 0) {
        failedFinishes--;
        return {code: 1, stderr: 'Temporary completion failure'};
      }
      if (finishResolve) await new Promise(resolve => {finishResolve = resolve;});
      if (persistedTurn?.token === payload.turn_id) persistedTurn.active = false;
    }
    return {code: 0, stdout: JSON.stringify(action === 'delivery_updates' ? delivery : {})};
  }
};
const ctx = {mode: 'rpc', hasUI: false, isIdle: () => !busy, hasPendingMessages: () => pending,
  sessionManager: {getSessionId: () => 'session', getBranch: () => []},
  modelRegistry: {find: () => ({id: 'configured', provider: 'anthropic'}), getAll: () => []},
  abort() {throw Error('Unexpected abort');}};
async function waitFor(predicate) {
  for (let attempt = 0; attempt < 1000; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => originalSetTimeout(resolve, 2));
  }
  throw Error('Timed out waiting for local SDK callback');
}
async function poll() {
  await waitFor(() => timers.length > 0);
  timers.shift()();
  await waitFor(() => timers.length > 0);
}
const event = id => ({id, kind: 'monitor.findings', payload: {findings: []}});
const prompt = () => ({systemPromptOptions: {sections: {}}});
extension(api);
await handlers.get('session_start')({}, ctx);
await waitFor(() => timers.length > 0);
assert.equal(messages.length, 0);
if (readOnly) {
  assert.deepEqual(await handlers.get('input')({source: 'user', text: 'hello'}, ctx), {action: 'continue'});
  const observerPrompt = prompt();
  await handlers.get('before_agent_start')(observerPrompt, ctx);
  assert.match(observerPrompt.systemPromptOptions.sections.orchestrator, /Quiet coordination/);
  await handlers.get('agent_end')({}, ctx);
  await poll();
  assert(!requests.some(request => ['foreground_start', 'foreground_finished'].includes(request.action)));
  assert.equal(persistedTurn, undefined);
  await handlers.get('session_shutdown')({}, ctx);
  console.log('quiet delivery passed');
  process.exit(0);
}
if (['retry-once', 'retry', 'stale-retry'].includes(scenario)) {
  await handlers.get('before_agent_start')(prompt(), ctx);
  const completedToken = persistedTurn.token;
  failedFinishes = 1;
  await handlers.get('agent_end')({}, ctx);
  assert.equal(persistedTurn.active, true);
  delivery = {ready: true, interrupting: [event(9)], silent: []};
  if (scenario !== 'stale-retry') {
    if (scenario === 'retry') {
      // A second outage must not mark local idle, even with a stale ready delivery.
      failedFinishes = 1;
      await poll();
      assert.equal(persistedTurn.active, true);
      assert.equal(messages.length, 0);
    }
    await poll();
    assert.equal(persistedTurn.active, false);
    assert.equal(messages.length, 1);
    const completions = requests.filter(request => request.action === 'foreground_finished');
    assert.equal(completions.length, scenario === 'retry' ? 3 : 2);
    assert(completions.every(request => request.payload.turn_id === completedToken));
  } else {
    finishResolve = true;
    const retryPoll = poll();
    await waitFor(() => typeof finishResolve === 'function');
    await handlers.get('before_agent_start')(prompt(), ctx);
    const newerToken = persistedTurn.token;
    assert.notEqual(newerToken, completedToken);
    finishResolve();
    await retryPoll;
    finishResolve = undefined;
    assert.deepEqual(persistedTurn, {token: newerToken, active: true});
    assert.equal(messages.length, 0);
    await poll();
    assert.equal(messages.length, 0);
    await handlers.get('agent_end')({}, ctx);
    await poll();
    assert.deepEqual(persistedTurn, {token: newerToken, active: false});
    assert.equal(messages.length, 1);
  }
  await handlers.get('session_shutdown')({}, ctx);
  console.log('quiet delivery passed');
  process.exit(0);
}
delivery = {ready: true, interrupting: [], silent: [event(1)]};
await poll();
assert.equal(messages.length, 1);
assert.equal(messages[0].message.display, false);
assert.deepEqual(messages[0].options, {deliverAs: 'nextTurn', triggerTurn: false});
await poll();
assert.equal(messages.length, 1); // Delivery is not acknowledgement, but no duplicate injection.
await handlers.get('before_agent_start')(prompt(), ctx);
const first = requests.find(request => request.action === 'foreground_start').payload.turn_id;
assert.equal(typeof first, 'string');
delivery = {ready: true, interrupting: [event(2)], silent: []};
await poll();
assert.equal(messages.length, 1); // Local busy guard rejects a stale ready response.
await handlers.get('agent_end')({}, ctx);
assert.equal(requests.find(request => request.action === 'foreground_finished').payload.turn_id, first);
delivery.ready = false;
await poll();
assert.equal(messages.length, 1); // Backend quiet period remains authoritative.
delivery.ready = true;
busy = true;
await poll();
assert.equal(messages.length, 1);
busy = false;
pending = true;
await poll();
assert.equal(messages.length, 1);
pending = false;
await poll();
assert.equal(messages.length, 2);
assert.equal(messages[1].message.display, true);
assert.deepEqual(messages[1].options, {deliverAs: 'followUp', triggerTurn: true});
// A pending old completion cannot clear the next turn's local guard.
await handlers.get('before_agent_start')(prompt(), ctx);
finishResolve = true;
const finishing = handlers.get('agent_end')({}, ctx);
await waitFor(() => typeof finishResolve === 'function');
await handlers.get('before_agent_start')(prompt(), ctx);
finishResolve();
await finishing;
finishResolve = undefined;
delivery.interrupting = [event(3)];
await poll();
assert.equal(messages.length, 2);
await handlers.get('agent_end')({}, ctx);
await poll();
assert.equal(messages.length, 3);
assert(!requests.some(request => request.action === 'acknowledge'));
await handlers.get('session_shutdown')({}, ctx);
console.log('quiet delivery passed');
"""


@unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Installed Pi is required")
class PiMonitorDeliveryTests(unittest.TestCase):
    def test_delivery_and_turn_identity_at_sdk_boundary(self):
        self.run_scenario("delivery")

    def test_failed_completion_retries_without_user_input(self):
        self.run_scenario("retry-once")

    def test_repeated_completion_failure_keeps_busy_until_success(self):
        self.run_scenario("retry")

    def test_stale_completion_retry_cannot_finish_new_turn(self):
        self.run_scenario("stale-retry")

    def test_observer_prompt_does_not_abort(self):
        self.run_scenario("observer")

    def test_inactive_prompt_does_not_write_lifecycle(self):
        self.run_scenario("inactive")

    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "delivery.mjs"
            harness.write_text(PI_DELIVERY_HARNESS)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("ORCHESTRATOR_")
            }
            result = subprocess.run(
                [
                    "node",
                    str(harness),
                    str(ROOT / ".pi/extensions/orchestrator.ts"),
                    str(Path(shutil.which("pi")).resolve()),
                    scenario,
                ],
                env=dict(environment, PI_OFFLINE="1"),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("quiet delivery passed", result.stdout)
