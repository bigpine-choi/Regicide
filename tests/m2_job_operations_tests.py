"""Mock owned-Job waits only; no native API or OS process is launched/killed."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import m2_job_operations as operations


class BoundedJobCallTests(unittest.TestCase):
    def test_value_arguments_and_single_daemon_execution(self):
        caller = threading.current_thread()
        observed = []

        def assign(process, *, mode):
            observed.append((process, mode, threading.current_thread()))
            return {'assigned': True}

        process = object()
        result = operations.bounded_job_call(assign, process, mode='gated', timeout_seconds=1.)
        self.assertEqual(result, {'assigned': True})
        self.assertEqual(len(observed), 1)
        self.assertIs(observed[0][0], process)
        self.assertEqual(observed[0][1], 'gated')
        self.assertIsNot(observed[0][2], caller)
        self.assertTrue(observed[0][2].daemon)

    def test_false_none_and_exception_results_are_not_confused(self):
        for value in (None, False, 0, ''):
            self.assertIs(operations.bounded_job_call(lambda: value, timeout_seconds=1.), value)
        for error in (RuntimeError('native query failed'), KeyboardInterrupt(), SystemExit(3)):
            def fail():
                raise error
            with self.assertRaises(type(error)) as caught:
                operations.bounded_job_call(fail, timeout_seconds=1.)
            self.assertIs(caught.exception, error)

    def test_invalid_bounds_never_start_operation(self):
        operation = Mock()
        for bound in (0, -1, True, False, float('nan'), float('inf'), '2', None,
                      threading.TIMEOUT_MAX * 2):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                operations.bounded_job_call(operation, timeout_seconds=bound)
        operation.assert_not_called()
        with self.assertRaises(TypeError):
            operations.bounded_job_call(None, timeout_seconds=1.)

    def test_thread_start_error_is_propagated_without_calling_operation(self):
        operation = Mock()
        error = RuntimeError('thread creation failed')
        with patch.object(operations.threading.Thread, 'start', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                operations.bounded_job_call(operation, timeout_seconds=1.)
        self.assertIs(caught.exception, error)
        operation.assert_not_called()

    def test_native_hang_times_out_without_cancel_or_duplicate_then_finishes(self):
        entered, release, finished = (threading.Event() for _ in range(3))
        calls = []

        def native_query():
            calls.append(threading.current_thread())
            entered.set()
            release.wait(timeout=3.)
            finished.set()
            return 'late result'

        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, 'cleanup is unconfirmed'):
                operations.bounded_job_call(native_query, timeout_seconds=.05)
            self.assertLess(time.monotonic() - started, 1.)
            self.assertTrue(entered.is_set())
            self.assertFalse(finished.is_set())
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].daemon)
        finally:
            release.set()
            self.assertTrue(finished.wait(timeout=1.))

    def test_sampler_holds_job_lock_assign_and_close_each_return_within_bound(self):
        # The actual failure path: native sampler work owns the Job lock while
        # caller-side assignment/finalization attempts must not block forever.
        lock = threading.RLock()
        sampler_entered, release_sampler, sampler_done = (threading.Event() for _ in range(3))
        assignment_done, close_done = threading.Event(), threading.Event()
        events = []

        def sampler():
            with lock:
                events.append('sampler-enter')
                sampler_entered.set()
                release_sampler.wait(timeout=3.)
                events.append('sampler-exit')
            sampler_done.set()

        def assign():
            with lock:
                events.append('assign')
                assignment_done.set()

        def close():
            with lock:
                events.append('close')
                close_done.set()

        thread = threading.Thread(target=sampler, daemon=True)
        thread.start()
        self.assertTrue(sampler_entered.wait(timeout=1.))
        try:
            for method in (assign, close):
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    operations.bounded_job_call(method, timeout_seconds=.05)
                self.assertLess(time.monotonic() - started, 1.)
            self.assertFalse(assignment_done.is_set() or close_done.is_set())
        finally:
            release_sampler.set()
            for completed in (sampler_done, assignment_done, close_done):
                self.assertTrue(completed.wait(timeout=1.))
            thread.join(timeout=1.)
        self.assertLess(events.index('sampler-exit'), events.index('assign'))
        self.assertLess(events.index('sampler-exit'), events.index('close'))
        self.assertEqual(events.count('assign'), 1)
        self.assertEqual(events.count('close'), 1)

    def test_termination_timeout_can_finish_later_without_claiming_success(self):
        entered, release, finished = (threading.Event() for _ in range(3))

        def terminate_and_snapshot(timeout_seconds):
            self.assertEqual(timeout_seconds, 5.)
            entered.set()
            release.wait(timeout=3.)
            finished.set()
            return {'active_processes': 0}

        try:
            # An operation-specific bound is passed positionally; the wrapper's
            # own timeout controls how long the coordinator waits.
            with self.assertRaises(TimeoutError):
                operations.bounded_job_call(terminate_and_snapshot, 5., timeout_seconds=.05)
            self.assertTrue(entered.is_set())
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            self.assertTrue(finished.wait(timeout=1.))


if __name__ == '__main__':
    unittest.main()
