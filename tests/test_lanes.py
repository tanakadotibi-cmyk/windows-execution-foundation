from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from windows_execution_foundation.core import ExecutionFoundation, atomic_write_json, stable_key, exclusive_lock, FoundationError
from windows_execution_foundation.cli import work_cycle
from windows_execution_foundation.codex_local_job import execute
from windows_execution_foundation.airtable_local_exec import ingest_queued, sync_results, _records
from windows_execution_foundation.lanes import (
    acquire_resource, admit_request, conflicts, load_config, owns_record,
    release_resource, resource_descriptor, validate_resource,
)


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a = self.foundation('A')
        self.b = self.foundation('B')

    def foundation(self, lane):
        return ExecutionFoundation({
            'lane_id': lane, 'state_dir': str(self.root / lane),
            'shared_resource_dir': str(self.root / 'shared'),
            'actions': {'fixture': {'command': [sys.executable, '-c', 'print("ok")'], 'timeout_seconds': 5}},
        })

    def job(self, foundation, request, key='repo:x', workdir=None):
        foundation.enqueue(action='fixture', idempotency_key=request,
                           metadata={'resource': {'key': key, 'workdir': workdir}})
        return json.loads((foundation.pending / (stable_key(request) + '.json')).read_text())

    def test_distinct_resources_admit_b_while_a_lease_is_held(self):
        a = acquire_resource(self.a, self.job(self.a, 'a', 'repo:a'))
        b = acquire_resource(self.b, self.job(self.b, 'b', 'repo:b'))
        self.assertTrue(a and b)
        self.assertNotEqual(a['token'], b['token'])
        release_resource(self.a, a)
        release_resource(self.b, b)

    def test_same_resource_wait_keeps_pending_without_attempt_then_runs(self):
        a = acquire_resource(self.a, self.job(self.a, 'a'))
        self.job(self.b, 'b')
        self.assertEqual(self.b.work_once()['status'], 'RESOURCE_WAIT')
        waiting = json.loads(next(self.b.pending.glob('*.json')).read_text())
        self.assertEqual(waiting['attempt'], 0)
        self.assertFalse(list(self.b.inflight.glob('*.json')))
        release_resource(self.a, a)
        self.assertEqual(self.b.work_once()['status'], 'SUCCESS')

    def test_unknown_resource_blocks_known_and_known_blocks_unknown(self):
        for first, second in [('*', 'repo:b'), ('repo:a', '*')]:
            lease = acquire_resource(self.a, self.job(self.a, first, first))
            self.assertIsNone(acquire_resource(self.b, self.job(self.b, second, second)))
            release_resource(self.a, lease)

    def test_same_or_parent_workdir_blocks_different_keys(self):
        a = {'key': 'repo:a', 'workdir': str(self.root)}
        b = {'key': 'repo:b', 'workdir': str(self.root / 'sub')}
        self.assertTrue(conflicts(a, b))
        self.assertTrue(conflicts(b, a))

    def test_lease_survives_worker_object_loss_without_age_reclaim(self):
        lease = acquire_resource(self.a, self.job(self.a, 'old'))
        del self.a
        self.a = self.foundation('A')
        self.assertIsNone(acquire_resource(self.b, self.job(self.b, 'new')))
        release_resource(self.a, lease)

    def test_owner_mismatch_cannot_release(self):
        lease = acquire_resource(self.a, self.job(self.a, 'a'))
        with self.assertRaisesRegex(FoundationError, 'RESOURCE_OWNER_MISMATCH'):
            release_resource(self.b, {**lease, 'lane_id': 'B'})
        self.assertIsNone(acquire_resource(self.b, self.job(self.b, 'b')))
        release_resource(self.a, lease)

    def test_timeout_fails_closed_retains_lease_and_never_requeues(self):
        self.a.config['actions']['fixture']['command'] = [sys.executable, '-c', 'import time;time.sleep(3)']
        self.a.config['actions']['fixture']['timeout_seconds'] = 1
        self.job(self.a, 'timeout')
        outcome = self.a.work_once()
        self.assertEqual(outcome['status'], 'FAILED_REQUEUE_RELEASE')
        self.assertTrue(outcome['error'].startswith('RESOURCE_RELEASE_UNSAFE:TIMEOUT:'))
        self.assertFalse(list(self.a.pending.glob('*.json')))
        self.assertEqual(len(list(self.a.failed.glob('*.json'))), 1)
        self.job(self.b, 'blocked')
        self.assertEqual(self.b.work_once()['status'], 'RESOURCE_WAIT')

    def test_safe_retry_releases_old_lease_before_pending_in_both_lanes(self):
        for foundation, lane in ((self.a, 'A'), (self.b, 'B')):
            with self.subTest(lane=lane):
                foundation.config['retry_base_seconds'] = 0
                self.job(foundation, 'retry-' + lane, 'repo:' + lane)
                path, job = foundation._claim()
                old_token = job['resource_lease']['token']
                outcome = foundation._retry(path, job, 'SYNTHETIC_FAILURE',
                                            safe_to_release=True)
                self.assertEqual(outcome['status'], 'RETRY_SPOOLED')
                self.assertFalse((self.root / 'shared' / 'active' / (old_token + '.json')).exists())
                pending = json.loads(next(foundation.pending.glob('*.json')).read_text())
                self.assertNotIn('resource_lease', pending)
                self.assertNotIn('worker_pid', pending)
                self.assertNotIn('claimed_at', pending)
                self.assertEqual(len(list((self.root / 'shared' / 'active').glob('*.json'))), 0)
                claimed_path, claimed = foundation._claim()
                self.assertNotEqual(claimed['resource_lease']['token'], old_token)
                release_resource(foundation, claimed['resource_lease'])
                claimed_path.unlink()

    def test_release_failure_fails_closed_without_pending_or_duplicate_lease(self):
        self.job(self.a, 'release-failure')
        self.a.config['actions']['fixture']['requires_network'] = True
        with mock.patch('windows_execution_foundation.lanes.release_resource',
                        side_effect=FoundationError('SYNTHETIC_RELEASE_FAILURE')), \
             mock.patch.object(self.a, '_network_ready', return_value=False):
            outcome = self.a.work_once()
        self.assertEqual(outcome['status'], 'FAILED_REQUEUE_RELEASE')
        self.assertEqual(outcome['error'],
                         'RESOURCE_RELEASE_FAILED:FoundationError:SYNTHETIC_RELEASE_FAILURE')
        self.assertFalse(list(self.a.pending.glob('*.json')))
        leases = list((self.root / 'shared' / 'active').glob('*.json'))
        self.assertEqual(len(leases), 1)
        failed = json.loads(next(self.a.failed.glob('*.json')).read_text())
        self.assertEqual(json.loads(leases[0].read_text())['token'],
                         failed['resource_lease']['token'])

    def test_live_or_unconfirmed_owner_fails_closed_and_preserves_lease(self):
        self.job(self.a, 'unsafe-retry')
        path, job = self.a._claim()
        old_lease = copy.deepcopy(job['resource_lease'])
        outcome = self.a._retry(path, job, 'CHILD_TERMINATION_UNCONFIRMED',
                                safe_to_release=False)
        self.assertEqual(outcome['status'], 'FAILED_REQUEUE_RELEASE')
        self.assertEqual(outcome['error'],
                         'RESOURCE_RELEASE_UNSAFE:CHILD_TERMINATION_UNCONFIRMED')
        self.assertFalse(list(self.a.pending.glob('*.json')))
        self.assertEqual(json.loads(next(self.a.failed.glob('*.json')).read_text())['resource_lease'],
                         old_lease)
        active = self.root / 'shared' / 'active' / (old_lease['token'] + '.json')
        self.assertEqual(json.loads(active.read_text()), old_lease)

    def test_failed_requeue_requires_prior_exact_lease_rescue_in_both_lanes(self):
        for foundation, lane in ((self.a, 'A'), (self.b, 'B')):
            with self.subTest(lane=lane):
                request = 'failed-requeue-' + lane
                self.job(foundation, request, 'repo:shared')
                path, job = foundation._claim()
                old_lease = copy.deepcopy(job['resource_lease'])
                foundation._fail_final(path, job, 'SYNTHETIC_TERMINAL_FAILURE')

                with self.assertRaisesRegex(
                        FoundationError, 'FAILED_REQUEUE_RESOURCE_LEASE_ACTIVE'):
                    foundation.enqueue(action='fixture', idempotency_key=request,
                                       metadata={'resource': {'key': 'repo:shared', 'workdir': None}})
                self.assertFalse(list(foundation.pending.glob('*.json')))
                self.assertEqual(json.loads(
                    self.root.joinpath('shared', 'active', old_lease['token'] + '.json').read_text()),
                    old_lease)

                # Synthetic equivalent of the existing exact dead-owner rescue contract.
                release_resource(foundation, old_lease)
                outcome = foundation.enqueue(
                    action='fixture', idempotency_key=request,
                    metadata={'resource': {'key': 'repo:shared', 'workdir': None}})
                self.assertEqual(outcome['status'], 'ENQUEUED')
                pending = json.loads(next(foundation.pending.glob('*.json')).read_text())
                self.assertNotIn('resource_lease', pending)
                self.assertEqual(len(list((self.root / 'shared' / 'active').glob('*.json'))), 0)
                claimed_path, claimed = foundation._claim()
                self.assertNotEqual(claimed['resource_lease']['token'], old_lease['token'])
                release_resource(foundation, claimed['resource_lease'])
                claimed_path.unlink()

    def test_shared_request_identity_rejects_changed_lane_or_hash(self):
        admit_request(self.a, 'id', 'hash')
        admit_request(self.a, 'id', 'hash')
        for foundation, digest in [(self.b, 'hash'), (self.a, 'changed')]:
            with self.assertRaisesRegex(ValueError, 'REQUEST_ID_INPUT_MISMATCH'):
                admit_request(foundation, 'id', digest)

    def test_validation_rejects_invalid_lane_or_partial_resource(self):
        for value in [{'lane': 'C'}, {'lane': []}, {'resource_key': 'x'},
                      {'resource_scope': 'repo', 'resource_key': '../ bad'}]:
            with self.assertRaises(ValueError):
                validate_resource(value)
        validate_resource({})
        validate_resource({'lane': 'B', 'resource_scope': 'repo', 'resource_key': 'x'})

    def test_config_b_is_derived_and_isolated_with_one_bridge(self):
        config = json.loads((Path(__file__).parents[1] / 'local/config.json').read_text())
        config['lanes'] = {'enabled': True, 'worker_b_task_name': 'ExecutionFoundation-Worker-B'}
        path = self.root / 'config.json'
        atomic_write_json(path, config)
        a, b = load_config(path), load_config(path, 'B')
        self.assertEqual(a['state_dir'], config['state_dir'])
        self.assertNotEqual(a['state_dir'], b['state_dir'])
        self.assertEqual(a['shared_resource_dir'], b['shared_resource_dir'])
        self.assertEqual(a['codex_local_job']['bridge_root'], b['codex_local_job']['bridge_root'])
        self.assertNotEqual(a['actions']['codex-local-job']['receipt_source'], b['actions']['codex-local-job']['receipt_source'])
        self.assertEqual(list(b['actions']), ['codex-local-job'])
        self.assertEqual(config, json.loads(path.read_text()))

    def test_ingestion_and_sync_do_not_touch_other_lane(self):
        fields = {k: k for k in ('action_type', 'action_json', 'request_id', 'state')}
        a = {'fields': fields, 'action_map': {}, 'lane_id': 'A'}
        b = {**a, 'lane_id': 'B'}
        row = {'id': 'recB', 'fields': {'action_type': 'CODEX_LOCAL_JOB', 'state': 'EF_QUEUED',
                                     'action_json': json.dumps({'request': {'lane': 'B'}})}}
        with mock.patch('windows_execution_foundation.airtable_local_exec._patch') as patch:
            self.assertEqual(ingest_queued(self.a, a, [row])['enqueued'], 0)
            self.assertEqual(sync_results(self.a, a, [row])['succeeded'], 0)
            patch.assert_not_called()
        self.assertTrue(owns_record(b, row['fields']))
        self.assertFalse(owns_record(b, {'action_type': 'OTHER'}))
        self.assertTrue(owns_record(a, {'action_type': 'CODEX_LOCAL_JOB'}))

    def test_pagination_does_not_starve_b_behind_a_rows(self):
        config = {'schema_version': 'EF_v0.1', 'device_id': 'test'}
        with mock.patch('windows_execution_foundation.airtable_local_exec._request', side_effect=[
            {'records': [{'id': 'a'}], 'offset': 'page2'}, {'records': [{'id': 'b'}]}]) as fetch:
            self.assertEqual([r['id'] for r in _records(config)], ['a', 'b'])
            self.assertEqual(fetch.call_count, 2)

    def test_drain_runs_two_jobs_and_replay_executes_zero(self):
        self.job(self.a, 'one')
        self.job(self.a, 'two')
        result = work_cycle(self.a, 2)
        self.assertEqual([x['status'] for x in result['outcomes']], ['SUCCESS', 'SUCCESS'])
        self.assertEqual(self.a.enqueue(action='fixture', idempotency_key='one')['status'], 'NOOP_RECEIPT')
        self.assertEqual(self.a.work_once()['status'], 'IDLE')

    def test_windows_file_lock_contention_preserves_first_owner(self):
        with exclusive_lock(self.root / 'lock'):
            with self.assertRaisesRegex(FoundationError, 'SINGLE_WORKER_LOCKED'):
                with exclusive_lock(self.root / 'lock'):
                    self.fail('double acquired')

    def test_real_worker_processes_overlap_for_distinct_resources(self):
        processes = []
        for f, lane in [(self.a, 'A'), (self.b, 'B')]:
            marker = self.root / (lane + '-started')
            release = self.root / 'finish'
            script = "import pathlib,time;pathlib.Path(%r).write_text('started');\nwhile not pathlib.Path(%r).exists(): time.sleep(.03)" % (str(marker), str(release))
            f.config['actions']['fixture']['command'] = [sys.executable, '-c', script]
            self.job(f, lane, 'repo:' + lane)
            config = self.root / (lane + '-config.json')
            atomic_write_json(config, f.config)
            code = "import json;from pathlib import Path;from windows_execution_foundation.core import ExecutionFoundation;print(json.dumps(ExecutionFoundation(json.loads(Path(%r).read_text())).work_once()))" % str(config)
            processes.append(subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not all((self.root / (x + '-started')).exists() for x in ('A','B')):
                time.sleep(.03)
            self.assertTrue(all((self.root / (x + '-started')).exists() for x in ('A','B')))
            self.assertTrue(all(p.poll() is None for p in processes))
        finally:
            (self.root / 'finish').touch()
            outputs = [p.communicate(timeout=7) for p in processes]
        for p, (stdout, stderr) in zip(processes, outputs):
            self.assertEqual(p.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)['status'], 'SUCCESS')

    def execution_fixture(self):
        try:
            from tests.test_codex_local_job import CodexLocalJobTests
        except ImportError:
            from test_codex_local_job import CodexLocalJobTests
        helper = CodexLocalJobTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        config = json.loads((Path(__file__).parents[1] / 'local/config.json').read_text())
        config['state_dir'] = str(self.root / 'configured-state')
        config['local_exec_airtable'] = helper.config
        config['codex_local_job'].update(bridge_root=str(self.root), default_workdir=str(self.root), approved_roots=[str(self.root)])
        config['lanes'] = {'enabled': True, 'worker_b_task_name': 'B'}
        path = self.root / 'config.json'
        atomic_write_json(path, config)
        foundation = ExecutionFoundation.from_path(path, 'B')
        row = helper.record('b-live')
        env = json.loads(row['fields']['action_json'])
        env['request'].update(lane='B', resource_scope='fixture', resource_key='b')
        row['fields']['action_json'] = json.dumps(env)
        with mock.patch('windows_execution_foundation.airtable_local_exec._patch'):
            result = ingest_queued(foundation, foundation.config['local_exec_airtable'], [row])
        self.assertEqual(result['enqueued'], 1)
        row['fields']['state'] = 'EF_RUNNING'
        return path, foundation, row

    def test_b_invokes_existing_bridge_directly_and_reports_lane(self):
        path, foundation, row = self.execution_fixture()
        _, job = foundation._claim()
        job_id = 'codex-local-' + stable_key('b-live')
        def run(command, **kwargs):
            self.assertIn('Run-CodexManagedJob.ps1', command[4])
            self.assertEqual(kwargs['env']['TANTAN_EXECUTION_LANE'], 'B')
            atomic_write_json(self.root / 'runtime/jobs' / job_id / 'status.json', {
                'status':'VERIFIED', 'reason_code':'VERIFIED',
                'codex':{'launch_count':1,'exit_code':0}, 'verifier':{'launch_count':1,'exit_code':0},
                'notification':{'state':'SENT','attempt_count':1,'http_status':200}})
            return subprocess.CompletedProcess(command, 0, json.dumps({'ok':True,'job_id':job_id}), '')
        with mock.patch('windows_execution_foundation.codex_local_job._request', return_value=row), \
             mock.patch('windows_execution_foundation.codex_local_job.subprocess.run', side_effect=run):
            result = execute(path, row['id'], 'b-live', self.root / 'result.json', 'B')
        self.assertEqual(result['lane_id'], 'B')
        self.assertTrue(result['resource_release_safe'])
        release_resource(foundation, job['resource_lease'])

    def test_b_cannot_launch_without_core_resource_lease(self):
        path, foundation, row = self.execution_fixture()
        row['fields']['state'] = 'EF_QUEUED'
        with mock.patch('windows_execution_foundation.codex_local_job._request', return_value=row), \
             mock.patch('windows_execution_foundation.codex_local_job._patch') as patch, \
             mock.patch('windows_execution_foundation.codex_local_job.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'RESOURCE_LEASE_REQUIRED'):
                execute(path, row['id'], 'b-live', self.root / 'result.json', 'B')
            run.assert_not_called()
            patch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
