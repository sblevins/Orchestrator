import json
from pathlib import Path
import tempfile
import unittest

from orchestrator.intake import needs_review
from orchestrator.store import StateError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = Store(self.home)
        (self.home / 'alpha').mkdir()
        (self.home / 'beta').mkdir()
        self.store.add_project('alpha', str(self.home / 'alpha'))
        self.store.add_project('beta', str(self.home / 'beta'))
        self.store.open_session('session-a', 'test', 'alpha')
        self.store.open_session('session-b', 'test', 'beta')

    def task(self, role='planner', **kwargs):
        return self.store.enqueue('alpha', 'session-a', role, 'Read-only task', {}, **kwargs)

    def finish_next(self, text='result'):
        task = self.store.claim_next(3)
        self.assertIsNotNone(task)
        self.assertTrue(self.store.runner_started(task['id'], task['token'], 123, 'identity'))
        self.assertTrue(self.store.finish(task['id'], task['token'], text=text))
        return self.store.task(task['id'])

    def test_project_and_session_isolation(self):
        with self.assertRaises(StateError):
            self.store.open_session('session-a', 'test', 'beta')
        with self.assertRaises(StateError):
            self.store.open_session('other', 'test', 'alpha')
        self.store.open_session('observer', 'test', 'alpha', observer=True)
        with self.assertRaises(StateError):
            self.store.require_writer('observer')
        self.store.open_session('new-owner', 'test', 'alpha', takeover=True)
        with self.assertRaises(StateError):
            self.store.record('session-a', 'decision.recorded', {'text': 'stale'})
        self.assertEqual(self.store.require_writer('session-b')['project_id'], 'beta')

    def test_rebind_and_project_alias_rejected(self):
        with self.assertRaises(StateError):
            self.store.add_project('alias', str(self.home / 'alpha' / '..' / 'alpha'))
        with self.assertRaises(StateError):
            self.store.add_project('../escape', str(self.home))
        with self.assertRaises(StateError):
            self.store.add_project('alpha', str(self.home / 'beta'))

    def test_idempotency_and_fencing(self):
        first = self.task(idempotency_key='once')
        self.assertEqual(first['id'], self.task(idempotency_key='once')['id'])
        with self.assertRaises(StateError):
            self.store.enqueue('alpha', 'session-a', 'critic', 'other', {}, idempotency_key='once')
        claimed = self.store.claim_next(1)
        self.assertIsNone(self.store.claim_next(1))
        self.assertFalse(self.store.runner_started(first['id'], 'wrong', 123, 'x'))
        self.assertTrue(self.store.runner_started(first['id'], claimed['token'], 123, 'x'))
        self.assertFalse(self.store.finish(first['id'], 'wrong', text='untrusted'))
        self.assertTrue(self.store.finish(first['id'], claimed['token'], text='actual'))
        self.assertFalse(self.store.finish(first['id'], claimed['token'], text='late duplicate'))
        self.assertEqual(self.store.task(first['id'])['result'], 'actual')

    def test_dependencies_and_worker_dispatch_disabled(self):
        parent = self.task()
        child = self.task('critic', depends_on=(parent['id'],))
        self.assertEqual(self.store.claim_next(3)['id'], parent['id'])
        self.assertIsNone(self.store.claim_next(3))
        parent = self.store.task(parent['id'])
        self.store.finish(parent['id'], parent['token'], text='done')
        self.assertEqual(self.store.claim_next(3)['id'], child['id'])
        with self.assertRaises(StateError):
            self.task('worker')
        with self.assertRaises(StateError):
            self.task(depends_on=('missing',))
        other = self.store.enqueue('beta', 'session-b', 'planner', 'other', {})
        with self.assertRaises(StateError):
            self.task(depends_on=(other['id'],))

    def test_notifications_survive_restart_and_need_ack(self):
        self.task()
        self.finish_next()
        pending = self.store.updates('session-a')
        self.assertEqual(len(pending), 1)
        self.store = Store(self.home)
        self.assertEqual(pending, self.store.updates('session-a'))
        self.assertEqual(self.store.updates('session-b'), [])
        with self.assertRaises(StateError):
            self.store.acknowledge('session-b', [pending[0]['id']])
        self.store.acknowledge('session-a', [pending[0]['id']])
        self.store.acknowledge('session-a', [pending[0]['id']])
        self.assertEqual(self.store.updates('session-a'), [])

    def test_takeover_recovers_unhandled_notifications(self):
        self.task()
        self.finish_next()
        self.store.open_session('replacement', 'test', 'alpha', takeover=True)
        self.assertEqual(len(self.store.updates('replacement')), 1)

    def test_cancel_running_and_queued(self):
        task = self.task()
        self.store.cancel('session-a', task['id'])
        self.assertEqual(self.store.task(task['id'])['state'], 'cancelled')
        task = self.task()
        claimed = self.store.claim_next(1)
        self.store.runner_started(task['id'], claimed['token'], 123, 'x')
        self.store.cancel('session-a', task['id'])
        self.assertFalse(self.store.heartbeat(task['id'], claimed['token']))
        self.store.finish(task['id'], claimed['token'], text='late')
        self.assertEqual(self.store.task(task['id'])['state'], 'cancelled')

    def test_notes_compare_and_swap(self):
        note = self.store.read_note('alpha', 'BRIEF.md')
        self.store.write_note('session-a', 'BRIEF.md', 'new', note['revision'])
        with self.assertRaises(StateError):
            self.store.write_note('session-a', 'BRIEF.md', 'lost update', note['revision'])
        self.assertEqual(self.store.read_note('alpha', 'BRIEF.md')['text'], 'new')
        self.assertEqual(self.store.read_note('beta', 'BRIEF.md')['text'], '')
        with self.assertRaises(StateError):
            self.store.read_note('alpha', '../secret')

    def test_monitor_cursor_strict_and_findings_hold(self):
        candidate = self.store.monitor_candidate('alpha')
        self.store.schedule_monitor('alpha', candidate, 'inspect', {}, 0)
        monitor = self.finish_next(json.dumps({'reviewed_through': candidate['cursor'], 'findings': []}))
        with self.assertRaises(StateError):
            self.store.apply_monitor(monitor, candidate['cursor'] + 1, [])
        self.store.apply_monitor(monitor, candidate['cursor'], [{'severity': 'blocking', 'summary': 'Evidence missing'}])
        self.assertEqual(len(self.store.snapshot('alpha')['holds']), 1)
        self.store.apply_monitor(monitor, candidate['cursor'], [{'severity': 'blocking', 'summary': 'duplicate'}])
        self.assertEqual(len(self.store.snapshot('alpha')['holds']), 1)
        self.assertIsNone(self.store.monitor_candidate('alpha'))

    def test_routine_prompts_recorded_without_waking_monitor(self):
        candidate = self.store.monitor_candidate('alpha')
        task = self.store.schedule_monitor('alpha', candidate, 'inspect', {}, 0)
        task = self.finish_next()
        self.store.apply_monitor(task, candidate['cursor'], [])
        prompt = "What's the status?"
        self.assertFalse(needs_review(prompt))
        self.store.record('session-a', 'user.message', {'prompt': prompt}, review_required=False)
        self.assertIsNone(self.store.monitor_candidate('alpha'))
        self.assertTrue(needs_review('What is the status? Also change the target.'))
        self.store.record('session-a', 'user.message', {'prompt': 'Change the target'})
        candidate = self.store.monitor_candidate('alpha')
        self.assertEqual(len([e for e in candidate['events'] if e['kind'] == 'user.message']), 2)

    def test_new_plan_supersedes_old_critique(self):
        plan = self.store.create_plan('session-a', 'first', {})
        planner = self.finish_next()
        self.store.attach_critic(planner, 'critique', {})
        critic = self.finish_next()
        latest = self.store.create_plan('session-a', 'second', {})
        self.store.apply_critique(critic, 'approved', [])
        self.assertEqual(self.store.plan(plan['id'])['status'], 'superseded')
        self.assertEqual(self.store.plan(latest['id'])['status'], 'drafting')
        with self.assertRaises(StateError):
            self.store.approve_plan(plan['id'])

    def test_database_integrity_and_backup(self):
        backup = self.store.backup()
        self.assertTrue(backup.exists())
        with self.store.transaction() as database:
            self.assertEqual(database.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            with self.assertRaises(Exception):
                database.execute("UPDATE events SET kind='changed'")
        self.assertEqual(self.store.data.stat().st_mode & 0o777, 0o700)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
