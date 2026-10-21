"""Meter failure fixtures; native CPU load requires an explicit --native flag."""
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from m2_engine_cpu import EngineCpuError, EngineCpuMeter

NATIVE = '--native' in sys.argv
if NATIVE:
    sys.argv.remove('--native')
RECEIPT_DIR = None
if '--receipt-dir' in sys.argv:
    offset = sys.argv.index('--receipt-dir')
    RECEIPT_DIR = Path(sys.argv[offset + 1]).resolve()
    del sys.argv[offset:offset + 2]
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)


def put(pointer, value):
    pointer._obj.low, pointer._obj.high = value & 0xffffffff, value >> 32


class NativeFixture:
    def __init__(self):
        self.api = Mock()
        self.pid, self.creation = 123, 987654321
        self.kernel, self.user = 20, 30
        self.api.OpenProcess.return_value = 55
        self.api.GetProcessId.side_effect = lambda _handle: self.pid
        self.api.GetProcessTimes.side_effect = self.times
        self.api.CloseHandle.return_value = True

    def times(self, _handle, creation, exited, kernel, user):
        put(creation, self.creation)
        put(exited, 0)
        put(kernel, self.kernel)
        put(user, self.user)
        return True


class EngineCpuTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeFixture()

    def meter(self, **kwargs):
        meter = EngineCpuMeter(123, self.native.creation, _kernel=self.native.api, **kwargs)
        self.addCleanup(meter.close)
        return meter

    def test_query_only_held_handle_raw_ticks_and_identity(self):
        meter = self.meter()
        self.native.api.OpenProcess.assert_called_once_with(0x1000, False, 123)
        row = meter.read()
        self.assertEqual((row['pid'], row['creation_ticks'], row['kernel_ticks'], row['user_ticks'], row['cpu_ticks']),
                         (123, 987654321, 20, 30, 50))
        self.assertLessEqual(row['read_started_monotonic'], row['read_finished_monotonic'])
        row['cpu_ticks'] = 999999
        self.assertEqual(meter.read()['cpu_ticks'], 50)
        meter.close()
        meter.close()
        self.native.api.CloseHandle.assert_called_once_with(55)
        with self.assertRaisesRegex(EngineCpuError, 'closed'):
            meter.read()
        self.assertFalse(self.native.api.TerminateProcess.called)

    def test_pid_creation_mismatch_and_failed_reads_are_closed_at_construction(self):
        for kind in ('wrong_pid', 'wrong_creation', 'failed_read', 'zero_creation'):
            with self.subTest(kind=kind):
                native = NativeFixture()
                if kind == 'wrong_pid': native.pid = 124
                if kind == 'failed_read': native.api.GetProcessTimes.side_effect = None; native.api.GetProcessTimes.return_value = False
                if kind == 'zero_creation': native.creation = 0
                expected = 1 if kind == 'wrong_creation' else 987654321
                with self.assertRaises(EngineCpuError):
                    EngineCpuMeter(123, expected, _kernel=native.api)
                native.api.CloseHandle.assert_called_once_with(55)

    def test_open_failure_and_invalid_identity_fail_before_call(self):
        for pid in (0, -1, True, '123', 1 << 32):
            with self.assertRaises(EngineCpuError):
                EngineCpuMeter(pid, _kernel=self.native.api)
        for creation in (0, -1, True, '123'):
            with self.assertRaises(EngineCpuError):
                EngineCpuMeter(123, creation, _kernel=self.native.api)
        self.native.api.OpenProcess.return_value = 0
        with self.assertRaisesRegex(EngineCpuError, 'OpenProcess'):
            self.meter()
        self.native.api.CloseHandle.assert_not_called()

    def test_creation_and_each_cpu_counter_regression_fail(self):
        for kind in ('creation', 'kernel', 'user', 'pid', 'access'):
            with self.subTest(kind=kind):
                self.native = NativeFixture()
                meter = self.meter()
                if kind == 'creation': self.native.creation += 1
                if kind == 'kernel': self.native.kernel -= 1
                if kind == 'user': self.native.user -= 1
                if kind == 'pid': self.native.pid += 1
                if kind == 'access': self.native.api.GetProcessTimes.side_effect = None; self.native.api.GetProcessTimes.return_value = False
                with self.assertRaises(EngineCpuError): meter.read()

    def test_clock_regression_within_and_between_reads_fail(self):
        for values in ((3., 2.), (3., 4., 2., 5.)):
            clock = iter(values)
            if len(values) == 2:
                with self.assertRaises(EngineCpuError): self.meter(_clock=lambda: next(clock))
            else:
                meter = self.meter(_clock=lambda: next(clock))
                with self.assertRaises(EngineCpuError): meter.read()

    def test_measure_play_uses_only_callable_wall_and_raw_cpu_delta(self):
        clock = iter((0., .1, 1., 1.1, 2., 4., 5., 5.1))
        meter = self.meter(_clock=lambda: next(clock))
        def play(value):
            self.native.kernel += 2_000_000
            self.native.user += 8_000_000
            return value
        result, row = meter.measure_play(play, 'bestmove')
        self.assertEqual(result, 'bestmove')
        self.assertEqual((row['status'], row['outcome']), ('OK', 'complete'))
        self.assertEqual(row['response_elapsed_seconds'], 2.)
        self.assertEqual(row['kernel_seconds'], .2)
        self.assertEqual(row['user_seconds'], .8)
        self.assertEqual(row['cpu_seconds'], 1.)
        self.assertEqual(row['cpu_per_wall_ratio'], .5)
        self.assertTrue(row['ratio_is_diagnostic_only'])
        self.assertIs(row, meter.last_measurement)

    def test_zero_cpu_and_zero_elapsed_are_valid(self):
        clock = iter((0., 0., 1., 1., 2., 2., 3., 3.))
        meter = self.meter(_clock=lambda: next(clock))
        result, row = meter.measure_play(lambda: 'mate')
        self.assertEqual(result, 'mate')
        self.assertEqual((row['status'], row['outcome']), ('OK', 'complete'))
        self.assertEqual(row['cpu_delta_ticks'], 0)
        self.assertEqual(row['response_elapsed_seconds'], 0)
        self.assertIsNone(row['cpu_per_wall_ratio'])

    def test_callable_failure_preserves_cpu_receipt_and_same_exception(self):
        meter = self.meter()
        error = TimeoutError('fixture response deadline')
        def play():
            self.native.user += 100
            raise error
        with self.assertRaises(TimeoutError) as caught:
            meter.measure_play(play)
        self.assertIs(caught.exception, error)
        self.assertEqual((error.cpu_receipt['status'], error.cpu_receipt['outcome']), ('ERROR', 'call_failed'))
        self.assertEqual(error.cpu_receipt['cpu_delta_ticks'], 100)
        self.assertEqual(error.cpu_receipt['call_error_type'], 'TimeoutError')

    def test_final_accounting_failure_never_fabricates_delta(self):
        meter = self.meter()
        def play():
            self.native.user -= 1
            raise TimeoutError('fixture')
        with self.assertRaises(EngineCpuError) as caught:
            meter.measure_play(play)
        row = caught.exception.cpu_receipt
        self.assertEqual((row['status'], row['outcome']), ('ERROR', 'accounting_failed'))
        self.assertEqual(row['call_error_type'], 'TimeoutError')
        self.assertIsNone(row['after'])
        self.assertNotIn('cpu_delta_ticks', row)

    def test_bad_response_clock_is_error_and_preserves_partial_receipt(self):
        for finish in (1., float('inf'), float('nan')):
            with self.subTest(finish=finish):
                self.native = NativeFixture()
                clock = iter((0., .1, 1., 1.1, 2., finish))
                meter = self.meter(_clock=lambda: next(clock))
                with self.assertRaises(EngineCpuError) as caught:
                    meter.measure_play(lambda: 'bestmove')
                row = caught.exception.cpu_receipt
                self.assertEqual((row['status'], row['outcome']), ('ERROR', 'accounting_failed'))
                self.assertIsNone(row['after'])
                self.assertNotIn('cpu_delta_ticks', row)

    def test_failed_before_read_does_not_run_callable(self):
        meter = self.meter()
        self.native.creation += 1
        play = Mock()
        with self.assertRaises(EngineCpuError) as caught:
            meter.measure_play(play)
        play.assert_not_called()
        self.assertEqual((caught.exception.cpu_receipt['status'], caught.exception.cpu_receipt['outcome']),
                         ('ERROR', 'accounting_failed'))
        self.assertIsNone(caught.exception.cpu_receipt['call_started_monotonic'])

    def test_close_failure_is_explicit_and_can_retry(self):
        meter = self.meter()
        self.native.api.CloseHandle.return_value = False
        with self.assertRaisesRegex(EngineCpuError, 'CloseHandle'): meter.close()
        self.assertEqual(meter.handle, 55)
        self.native.api.CloseHandle.return_value = True
        meter.close()
        self.assertIsNone(meter.handle)


@unittest.skipUnless(NATIVE and sys.platform == 'win32', 'explicit --native flag on Windows required')
class EngineCpuNativeTests(unittest.TestCase):
    """Independent WinAPI definitions, including final reads after child exit."""
    def record(self, name, value):
        if RECEIPT_DIR is not None:
            target = RECEIPT_DIR / (name + '.json')
            with target.open('x', encoding='utf-8', newline='\n') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')

    def independent(self, process):
        class Filetime(C.Structure):
            _fields_ = [('low', C.c_uint32), ('high', C.c_uint32)]
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.GetProcessTimes.argtypes = [C.c_void_p] + [C.POINTER(Filetime)] * 4
        kernel.GetProcessTimes.restype = C.c_int32
        creation, exited, system, user = (Filetime() for _ in range(4))
        if not kernel.GetProcessTimes(int(process._handle), C.byref(creation), C.byref(exited), C.byref(system), C.byref(user)):
            raise OSError(C.get_last_error(), 'independent GetProcessTimes')
        ticks = lambda value: value.low | value.high << 32
        return {'pid': process.pid, 'creation_ticks': ticks(creation), 'kernel_ticks': ticks(system),
                'user_ticks': ticks(user), 'cpu_ticks': ticks(system) + ticks(user)}

    def child(self, duration, threads=1):
        code = '''import sys,time,threading,os,json
print(json.dumps({"status":"ready","pid":os.getpid(),"executable":sys.executable}),flush=True)
sys.stdin.readline()
def burn():
    limit=time.thread_time()+float(sys.argv[1])
    while time.thread_time()<limit: pass
workers=[threading.Thread(target=burn) for _ in range(int(sys.argv[2]))]
for worker in workers: worker.start()
for worker in workers: worker.join()
print("done",flush=True)
sys.stdin.readline()
'''
        # A Windows venv python.exe is a redirector. Use the actual base process
        # so the PID being metered is the CPU-consuming Python endpoint.
        executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
        process = subprocess.Popen([executable, '-s', '-c', code, str(duration), str(threads)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        identity = json.loads(process.stdout.readline())
        self.assertEqual(identity['status'], 'ready')
        self.assertEqual(identity['pid'], process.pid)
        self.assertEqual(Path(identity['executable']).resolve(), Path(executable).resolve())
        process.regicide_endpoint_identity = identity
        return process

    def test_self_process_matches_independently_bracketed_getprocesstimes(self):
        process = SimpleNamespace(pid=os.getpid(), _handle=-1)
        before = self.independent(process)
        with EngineCpuMeter(os.getpid(), before['creation_ticks']) as meter:
            measured = meter.read()
            after = self.independent(process)
        for name in ('kernel_ticks', 'user_ticks', 'cpu_ticks'):
            self.assertLessEqual(before[name], measured[name])
            self.assertLessEqual(measured[name], after[name])
        self.record('self-process', {'independent_before': before, 'meter_read': measured,
                                    'independent_after': after, 'handle_closed': meter.handle is None,
                                    'result': 'PASS'})

    def test_short_and_multithread_long_children_match_independent_final_counters(self):
        for duration, threads in ((.001, 1), (.08, 2)):
            with self.subTest(duration=duration, threads=threads):
                process = self.child(duration, threads)
                initial = self.independent(process)
                meter = EngineCpuMeter(process.pid, initial['creation_ticks'])
                self.addCleanup(meter.close)
                def play():
                    process.stdin.write('go\n'); process.stdin.flush()
                    return process.stdout.readline().strip()
                result, receipt = meter.measure_play(play)
                self.assertEqual(result, 'done')
                process.stdin.write('exit\n'); process.stdin.flush()
                process.wait(timeout=10)
                reference = self.independent(process)
                final = meter.read()
                self.assertEqual({name: final[name] for name in reference}, reference)
                self.assertEqual(receipt['after']['creation_ticks'], reference['creation_ticks'])
                self.assertGreaterEqual(final['cpu_ticks'], receipt['after']['cpu_ticks'])
                if duration == .08:
                    self.assertGreater(receipt['cpu_seconds'], .1)
                # Creation identity/counters remain readable after exit.
                repeated = meter.read()
                self.assertEqual(repeated['cpu_ticks'], final['cpu_ticks'])
                meter.close()
                self.record(f'child-{duration}-{threads}', {
                    'endpoint_identity': process.regicide_endpoint_identity,
                    'duration_cpu_seconds_per_thread': duration, 'threads': threads,
                    'independent_initial': initial, 'play_receipt': receipt,
                    'independent_final_after_exit': reference, 'meter_final_after_exit': final,
                    'meter_repeated_after_exit': repeated, 'returncode': process.returncode,
                    'handle_closed': meter.handle is None, 'result': 'PASS'})

    def test_closing_meter_never_terminates_live_child(self):
        process = self.child(.001)
        meter = EngineCpuMeter(process.pid)
        initial = meter.initial_read
        meter.close()
        self.assertIsNone(process.poll())
        with self.assertRaises(EngineCpuError): meter.read()
        process.stdin.write('go\n'); process.stdin.flush()
        self.assertEqual(process.stdout.readline().strip(), 'done')
        process.stdin.write('exit\n'); process.stdin.flush()
        self.assertEqual(process.wait(timeout=10), 0)
        self.record('close-preserves-live-child', {
            'endpoint_identity': process.regicide_endpoint_identity, 'initial_read': initial,
            'running_after_close': True, 'handle_closed': meter.handle is None,
            'independent_final_after_exit': self.independent(process),
            'returncode': process.returncode, 'result': 'PASS'})


if __name__ == '__main__':
    unittest.main()
