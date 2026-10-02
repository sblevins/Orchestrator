"""An interrupted Claude turn without Stop must not stall silent background monitoring."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.cli import watch
from orchestrator.config import ConfigurationError, load_config
from orchestrator.hooks import handle_hook
from orchestrator.monitoring import begin_turn, delivery_updates, finish_turn
from orchestrator.project_settings import configure_project
from orchestrator.runtime import process_identity
from orchestrator.store import StateError, Store


class StaleForegroundTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"NO_MISTAKES_GATE": ""})
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.store = Store(self.home)
        self.store.add_project("project", str(self.home))
        self.store.open_session("writer", "claude", "project")
        live_owner = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
        self.store.set_service_value("native-owner:writer", json.dumps(live_owner))
        for target in (
            "orchestrator.hooks.resolve_claude_session",
            "orchestrator.hooks.verify_claude_owner",
        ):
            owner_patch = patch(target, return_value="writer")
            owner_patch.start()
            self.addCleanup(owner_patch.stop)
        clock = patch("orchestrator.monitoring.time.time", return_value=100)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def hook(self, event, **fields):
        return handle_hook(self.home, event, {"session_id": "native", **fields})

    def tool_call(self, name="Bash", command="make check"):
        tool = {"tool_name": name, "tool_input": {"command": command}, "tool_use_id": "tool"}
        self.hook("PreToolUse", **tool)
        self.hook("PostToolUse", **tool)

    def background_schedule(self):
        candidate = self.store.monitor_candidate("project")
        if candidate is None:
            return None
        return self.store.schedule_monitor(
            "project", candidate, "{}", load_config(self.home, "project"), 0
        )

    def blocking_finding(self):
        with self.store.transaction() as database:
            return self.store._event(
                database,
                "project",
                "monitor.findings",
                {"findings": [{"severity": "blocking", "summary": "Evidence"}]},
                notify=True,
            )

    def test_interrupted_turn_resumes_silent_background_scheduling(self):
        self.hook("UserPromptSubmit", prompt="Refactor the parser module")
        self.clock.return_value = 300
        self.tool_call()
        self.clock.return_value = 300 + 899
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 300 + 900
        self.assertIsNotNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 2000
        self.hook("UserPromptSubmit", prompt="Now update the parser tests too")
        self.clock.return_value = 2000 + 899
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 2000 + 900
        task = self.background_schedule()
        self.assertEqual(task["role"], "monitor")
        self.assertEqual(task["state"], "queued")

    def test_active_tool_heartbeats_keep_the_turn_live(self):
        self.hook("UserPromptSubmit", prompt="Run the long migration")
        self.clock.return_value = 900
        self.hook("PreToolUse", tool_name="Agent", tool_input={"prompt": "migrate"})
        self.clock.return_value = 1799
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.hook("PostToolUseFailure", tool_name="Read", tool_input={"file_path": "x"})
        self.clock.return_value = 1799 + 899
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 1799 + 900
        self.assertIsNotNone(self.background_schedule())

    def test_quiet_period_still_applies(self):
        configure_project(
            self.store,
            "writer",
            {"monitoring": {"quiet_seconds": 120, "foreground_stale_seconds": 60}},
        )
        self.hook("UserPromptSubmit", prompt="Investigate the flaky build")
        self.clock.return_value = 219
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 220
        self.assertIsNotNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 300
        self.hook("UserPromptSubmit", prompt="Investigate the flaky deploy")
        self.hook("Stop", last_assistant_message="The deploy is fixed.")
        self.clock.return_value = 419
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 420
        self.assertIsNotNone(self.store.monitor_candidate("project"))
        with self.assertRaises(ConfigurationError):
            configure_project(
                self.store, "writer", {"monitoring": {"foreground_stale_seconds": 59}}
            )

    def test_stale_completion_tokens_neither_finish_nor_refresh_the_turn(self):
        self.hook("UserPromptSubmit", prompt="Plan the storage rewrite")
        begin_turn(self.store, "writer", "old")
        self.clock.return_value = 150
        begin_turn(self.store, "writer", "new")
        self.clock.return_value = 900
        self.assertTrue(finish_turn(self.store, "writer", "old")["active"])
        self.clock.return_value = 1049
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 1050
        self.assertIsNotNone(self.store.monitor_candidate("project"))
        self.assertFalse(delivery_updates(self.store, "writer", 20)["ready"])
        self.assertFalse(finish_turn(self.store, "writer", "new")["active"])
        self.clock.return_value = 1069
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.assertFalse(delivery_updates(self.store, "writer", 20)["ready"])
        self.clock.return_value = 1070
        self.assertTrue(delivery_updates(self.store, "writer", 20)["ready"])

    def test_frontend_activity_keeps_only_its_own_turn_live(self):
        from orchestrator.api import request

        request(self.home, "writer", "foreground_start", {"turn_id": "pi-turn"})
        for second in range(130, 3000, 30):
            self.clock.return_value = second
            request(self.home, "writer", "foreground_activity", {"turn_id": "pi-turn"})
            self.assertIsNone(self.store.monitor_candidate("project"))
        self.clock.return_value = 2980 + 899
        self.assertIsNone(self.store.monitor_candidate("project"))
        self.assertFalse(delivery_updates(self.store, "writer", 20)["ready"])
        self.clock.return_value = 2980 + 900
        self.assertIsNotNone(self.store.monitor_candidate("project"))
        # Activity from a superseded turn cannot keep a newer idle turn live.
        request(self.home, "writer", "foreground_start", {"turn_id": "newer"})
        self.clock.return_value = 5000
        marker = request(self.home, "writer", "foreground_activity", {"turn_id": "pi-turn"})
        self.assertEqual((marker["token"], marker["touched"]), ("newer", 2980 + 900))
        self.assertIsNotNone(self.store.monitor_candidate("project"))
        with self.assertRaises(StateError):
            request(self.home, "writer", "foreground_activity", {"turn_id": "newer", "x": 1})

    def test_stale_bookkeeping_never_delivers_mid_response(self):
        self.hook("UserPromptSubmit", prompt="Rewrite the scheduler")
        blocking = self.blocking_finding()
        self.clock.return_value = 100_000
        self.assertIsNotNone(self.background_schedule())
        self.assertEqual(
            delivery_updates(self.store, "writer", 20),
            {"ready": False, "interrupting": [], "silent": []},
        )
        self.assertEqual(watch(self.home, "writer", 0.001), 0)
        self.assertIsNone(self.store.service_value("wake:writer"))
        self.hook("Stop", last_assistant_message="The scheduler is rewritten.")
        self.clock.return_value = 100_020
        delivered = delivery_updates(self.store, "writer", 20)["interrupting"]
        self.assertEqual([event["id"] for event in delivered], [blocking])


if __name__ == "__main__":
    unittest.main()
