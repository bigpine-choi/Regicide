"""Pure fake lifecycle/error regressions: no native API or process is used."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import chess
from m2_engine_cpu import EngineCpuError
import m2_engine_tracking_v2 as tracking
from m2_engine_receipts_v2_tests import fixture
from m2_engine_identity_v2 import EngineIdentityError


class FakeEngine:
    def __init__(self, role, events):
        self.role, self.events = role, events
        self.timeout = 10.
        self.quit_called = self.closed = False
        self.play_error = None

    def play(self, board, *args, **kwargs):
        self.events.append((self.role, 'play', self.timeout))
        if self.play_error is not None:
            raise self.play_error
        return SimpleNamespace(move=chess.Move.from_uci('e2e4' if board.turn else 'e7e5'))

    def quit(self):
        self.quit_called = True
        self.events.append((self.role, 'quit'))

    def close(self):
        self.closed = True
        self.events.append((self.role, 'engine_close'))


class FakeMeter:
    def __init__(self, process, receipts, engine, events):
        self.process, self.receipts, self.engine, self.events = process, receipts, engine, events
        self.reads, self.closed = 0, False
        self.final_error = self.before_error = self.close_error = self.baseline_error = None
        self.last_measurement = None

    def read(self):
        self.reads += 1
        role = self.process['role']
        self.events.append((role, 'meter_read', self.reads))
        if self.reads == 1:
            if self.baseline_error is not None:
                raise self.baseline_error
            return deepcopy(self.process['baseline'])
        if not self.engine.quit_called:
            raise AssertionError('final CPU must be read after engine quit')
        if self.final_error is not None:
            raise self.final_error
        return deepcopy(self.process['final'])

    def measure_play(self, function, *args, **kwargs):
        if self.before_error is not None:
            self.last_measurement = {'status': 'ERROR', 'outcome': 'accounting_failed',
                                     'before': None, 'after': None, 'response_elapsed_seconds': None}
            raise self.before_error
        receipt = deepcopy(self.receipts.pop(0))
        self.last_measurement = receipt
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            receipt.update(status='ERROR', outcome='call_failed', call_error_type=type(error).__name__)
            raise
        return result, receipt

    def close(self):
        self.events.append((self.process['role'], 'meter_close'))
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class EngineTrackingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.metadata, self.cell = fixture(child=True)
        self.cell['id'] = 'preflight-0001'
        self.metadata['errors'] = []
        self.events = []
        self.engines = {role: FakeEngine(role, self.events) for role in ('baseline', 'candidate')}
        self.meters = {}
        for process in self.metadata['engine_cpu_processes']:
            role = process['role']
            rows = [row for row in self.metadata['engine_cpu_trace'] if row['role'] == role]
            self.meters[role] = FakeMeter(deepcopy(process), deepcopy(rows), self.engines[role], self.events)
        self.original = deepcopy(self.metadata)
        self.clock = [value for receipt in self.original['engine_cpu_trace']
                      for value in (receipt['envelope_started_monotonic'], receipt['envelope_finished_monotonic'])]

    def context(self, clocks=None):
        def resolve(pid, creation, profile, runtime):
            for process in self.original['engine_cpu_processes']:
                if process['transport_pid'] == pid:
                    self.assertEqual(process['transport_creation_ticks'], creation)
                    return deepcopy(process['endpoint_resolution'])
            raise AssertionError('unknown fake transport')
        def meter(pid, creation):
            for value in self.meters.values():
                endpoint = value.process['endpoint_resolution']['endpoint']
                if endpoint['pid'] == pid:
                    self.assertEqual(endpoint['creation_ticks'], creation)
                    return value
            raise AssertionError('unknown fake endpoint')
        self.enterContext(patch.object(tracking, 'resolve_engine_endpoint', side_effect=resolve))
        self.enterContext(patch.object(tracking, 'EngineCpuMeter', side_effect=meter))
        self.enterContext(patch.object(tracking.time, 'monotonic', side_effect=clocks or self.clock))
        return tracking.EngineCpuTrace(self.directory, self.cell, {'path': '/frozen/basepython.exe', 'sha256': 'c' * 64})

    def wrap(self, tracker):
        return {role: tracker.wrap(self.engines[role], self.original['affinity']['transports'][index], index, {'id': role})
                for index, role in enumerate(('baseline', 'candidate'))}

    def quit(self):
        for engine in self.engines.values():
            engine.quit()
            engine.close()

    def play_all(self, wrappers):
        for game in self.metadata['games']:
            board, token = chess.Board(), object()
            for move in game['search_moves']:
                reply = wrappers[move['role']].play(board, None, game=token)
                board.push(reply.move)

    def records(self):
        return [json.loads(line) for line in (self.directory / 'engine-cpu.jsonl').read_text(encoding='utf-8').splitlines()]

    def complete(self):
        with self.context() as tracker:
            wrappers = self.wrap(tracker)
            self.play_all(wrappers)
            self.quit()
        metadata, code = tracker.attach(self.metadata, 0)
        return tracker, metadata, code

    def test_full_pair_binds_context_final_after_quit_closes_and_replays_raw_trace(self):
        tracker, metadata, code = self.complete()
        self.assertEqual(code, 0, tracker.errors)
        self.assertTrue(metadata['engine_cpu_summary']['valid'])
        self.assertEqual([row['ordinal'] for row in tracker.trace], [1, 2, 3, 4])
        self.assertEqual([row['game_index'] for row in tracker.trace], [0, 0, 1, 1])
        self.assertEqual([row['ply'] for row in tracker.trace], [1, 2, 1, 2])
        self.assertEqual(tracker.trace[0]['pid'], 201)  # actual Sunfish child, transport 101
        self.assertEqual(tracker.trace[0]['transport_pid'], 101)
        # Actual pair audits read games.json: native PID dictionary keys have
        # their JSON string representation on both recorded sides.
        saved_metadata = json.loads(json.dumps(metadata))
        self.assertEqual(tracking.validate_trace_file(saved_metadata, self.directory)['status'], 'PASS')
        for role, meter in self.meters.items():
            self.assertTrue(meter.closed)
            self.assertLess(self.events.index((role, 'quit')), self.events.index((role, 'meter_read', 2)))
            self.assertLess(self.events.index((role, 'meter_read', 2)), self.events.index((role, 'meter_close')))
            self.assertEqual(self.engines[role].timeout, 10.)
        initial = [row['record'] for row in self.records() if row['event'] == 'endpoint']
        self.assertTrue(all(row['final'] is None and row['meter_closed'] is False for row in initial))

    def test_before_cpu_failure_preserves_original_exception_partial_search_and_cleanup(self):
        error = EngineCpuError('fake before-read failure')
        self.meters['baseline'].before_error = error
        with self.assertRaises(EngineCpuError) as caught:
            with self.context() as tracker:
                wrappers = self.wrap(tracker)
                try:
                    wrappers['baseline'].play(chess.Board(), None, game=object())
                finally:
                    self.quit()
        self.assertIs(caught.exception, error)
        search = [row['record'] for row in self.records() if row['event'] == 'search']
        self.assertEqual(len(search), 1)
        self.assertEqual(search[0]['outcome'], 'accounting_failed')
        self.assertIsNone(search[0]['response_elapsed_seconds'])
        self.assertIsNone(search[0]['move'])
        self.assertTrue(all(meter.closed for meter in self.meters.values()))

    def test_engine_exception_preserves_failed_search_original_error_and_timeout(self):
        error = TimeoutError('fake engine response failure')
        self.engines['baseline'].play_error = error
        with self.assertRaises(TimeoutError) as caught:
            with self.context() as tracker:
                wrappers = self.wrap(tracker)
                try:
                    wrappers['baseline'].play(chess.Board(), None, game=object())
                finally:
                    self.quit()
        self.assertIs(caught.exception, error)
        search = [row['record'] for row in self.records() if row['event'] == 'search']
        self.assertEqual(search[0]['status'], 'ERROR')
        self.assertEqual(search[0]['outcome'], 'call_failed')
        self.assertEqual(search[0]['call_error_type'], 'TimeoutError')
        self.assertEqual(self.engines['baseline'].timeout, 10.)

    def test_envelope_deadline_failure_is_persisted_even_when_callable_is_fast(self):
        with self.assertRaisesRegex(TimeoutError, 'deadline'):
            with self.context(clocks=[1., 1.46]) as tracker:
                wrappers = self.wrap(tracker)
                try:
                    wrappers['baseline'].play(chess.Board(), None, game=object())
                finally:
                    self.quit()
        search = [row['record'] for row in self.records() if row['event'] == 'search']
        self.assertTrue(search[0]['deadline_exceeded'])
        self.assertLess(search[0]['response_elapsed_seconds'], .45)
        self.assertGreater(search[0]['envelope_elapsed_seconds'], .45)

    def test_final_read_failure_closes_every_meter_and_invalidates_metadata(self):
        self.meters['baseline'].final_error = EngineCpuError('fake final-read failure')
        tracker, metadata, code = self.complete()
        self.assertEqual(code, 2)
        self.assertFalse(metadata['engine_cpu_summary']['valid'])
        self.assertTrue(all(meter.closed for meter in self.meters.values()))
        self.assertTrue(any('final CPU' in error for error in tracker.errors))

    def test_close_failure_is_reported_and_other_meter_still_closes(self):
        self.meters['baseline'].close_error = EngineCpuError('fake CloseHandle failure')
        tracker, metadata, code = self.complete()
        self.assertEqual(code, 2)
        self.assertFalse(tracker.processes[0]['meter_closed'])
        self.assertTrue(tracker.processes[1]['meter_closed'])
        self.assertTrue(self.meters['candidate'].closed)
        self.assertTrue(any('CPU handle' in error for error in tracker.errors))

    def test_setup_baseline_failure_closes_unregistered_meter(self):
        self.meters['baseline'].baseline_error = EngineCpuError('fake baseline read')
        with self.assertRaisesRegex(EngineCpuError, 'baseline read'):
            with self.context() as tracker:
                self.wrap(tracker)
        self.assertTrue(self.meters['baseline'].closed)
        self.assertEqual(tracker.meters, [])
        self.assertTrue(any(row['event'] == 'setup_error' for row in self.records()))

    def test_failed_identity_keeps_raw_job_receipt_and_original_exception(self):
        error = EngineIdentityError('synthetic child access denied')
        proof = {'phase':'resolve-child','offending_pid':1396,
                 'endpoint_job_scope':{'schema_version':1,'worker_pid':900,
                     'global_inventories':[[{'pid':1396,'parent_pid':1016}]],
                     'job_snapshots':[{'pids':[900,1016],'limit_flags':8192}]},
                 'windows_error':5}
        error.identity_receipt = deepcopy(proof)
        with self.assertRaises(EngineIdentityError) as caught:
            with self.context() as tracker:
                with patch.object(tracking,'resolve_engine_endpoint',side_effect=error):
                    self.wrap(tracker)
        self.assertIs(caught.exception,error)
        setup = [row['record'] for row in self.records() if row['event']=='setup_error']
        self.assertEqual(len(setup),1)
        self.assertEqual(setup[0]['identity_receipt'],proof)
        self.assertEqual(setup[0]['role'],'baseline')
        self.assertEqual(setup[0]['transport_pid'],101)
        self.assertEqual(tracker.meters,[])
        self.assertTrue(tracker.closed)

    def test_attach_requires_finalization_and_detects_missing_search_receipt(self):
        with self.context() as tracker:
            with self.assertRaisesRegex(RuntimeError, 'finalize'): tracker.attach(self.metadata, 0)
            self.wrap(tracker)
            self.quit()
        metadata, code = tracker.attach(self.metadata, 0)
        self.assertEqual(code, 2)
        self.assertFalse(metadata['engine_cpu_summary']['valid'])
        self.assertTrue(any('CPU calls' in error for error in tracker.errors))

    def test_trace_hash_or_failed_event_cannot_pass_completed_audit(self):
        tracker, metadata, code = self.complete()
        original = tracker.path.read_bytes()
        tracker.path.write_bytes(original + b'\n')
        with self.assertRaisesRegex(ValueError, 'identity'): tracking.validate_trace_file(metadata, self.directory)
        tracker.path.write_bytes(original + b'{"event":"accounting_errors","record":{}}\n')
        metadata['engine_cpu_trace_file']['sha256'] = hashlib.sha256(tracker.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'failed or unknown'): tracking.validate_trace_file(metadata, self.directory)

    def test_process_final_write_failure_does_not_skip_other_handle_cleanup(self):
        tracker = self.context()
        original_write = tracker.write
        raised = False
        def fail_first_final(event, record):
            nonlocal raised
            if event == 'process_final' and not raised:
                raised = True
                raise OSError('fake final log storage failure')
            return original_write(event, record)
        with patch.object(tracker, 'write', side_effect=fail_first_final):
            try:
                with tracker:
                    self.wrap(tracker)
                    self.quit()
            except OSError:
                pass
        self.assertTrue(all(meter.closed for meter in self.meters.values()))
        self.assertTrue(tracker.closed)

    def test_worker_error_write_failure_does_not_skip_any_handle_cleanup(self):
        tracker = self.context()
        original_write = tracker.write
        def fail_worker_error(event, record):
            if event == 'worker_error':
                raise OSError('fake error-log storage failure')
            return original_write(event, record)
        with patch.object(tracker, 'write', side_effect=fail_worker_error):
            try:
                with tracker:
                    self.wrap(tracker)
                    self.quit()
                    raise RuntimeError('fake worker failure')
            except (OSError, RuntimeError):
                pass
        self.assertTrue(all(meter.closed for meter in self.meters.values()))
        self.assertTrue(tracker.closed)


if __name__ == '__main__':
    unittest.main()
