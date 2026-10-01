"""Doctor reports spending policy separately from resource limits."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.cli import doctor
from orchestrator.store import Store


class SpendingPolicyTests(unittest.TestCase):
    def test_doctor_closes_its_database_connections(self):
        connections = []
        original_connect = Store.connect

        def tracked_connect(store):
            connection = original_connect(store)
            connections.append(connection)
            return connection

        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.object(Store, "connect", tracked_connect),
                patch("orchestrator.cli._version", return_value={"available": False}),
                patch("orchestrator.runtime.service_status", return_value={"running": False}),
            ):
                doctor(Path(directory))
            self.assertTrue(connections)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
        finally:
            for connection in connections:
                connection.close()

    def test_doctor_reports_no_per_role_dollar_caps(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("orchestrator.cli._version", return_value={"available": False}),
            patch("orchestrator.runtime.service_status", return_value={"running": False}),
        ):
            report = doctor(Path(directory))
        self.assertEqual(report["spending_limits"], "no per-role dollar caps configured")
        self.assertNotIn("codex_budget", report)


if __name__ == "__main__":
    unittest.main()
