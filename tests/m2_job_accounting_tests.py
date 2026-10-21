"""Native structure/counter failure fixtures; no process is started or killed."""
import ctypes as C
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import m2_job_accounting as job


def put(pointer, value):
    pointer._obj.low, pointer._obj.high = value & 0xffffffff, value >> 32


class Native:
    def __init__(self):
        self.api = Mock()
        self.assigned = set()
        self.monitor_member = False
        self.parent_pid = os.getpid()
        self.created = job._FILETIME_EPOCH + 100
        self.cpu = (20, 10)
        self.counts = dict(TotalUserTime=0, TotalKernelTime=0, TotalProcesses=0,
                           ActiveProcesses=0, TotalTerminatedProcesses=0)
        self.api.CreateJobObjectW.return_value = 50
        self.api.SetInformationJobObject.return_value = True
        self.api.GetCurrentProcess.return_value = 90
        self.api.GetProcessId.side_effect = lambda handle: os.getpid() if handle == 90 else 123
        self.api.CloseHandle.return_value = True
        self.api.WaitForSingleObject.return_value = 258
        self.api.CreateToolhelp32Snapshot.return_value = 70
        self.api.Process32FirstW.side_effect = self.first
        self.api.GetProcessTimes.side_effect = self.times
        self.api.GetSystemTimes.side_effect = self.system
        self.api.IsProcessInJob.side_effect = self.member
        self.api.AssignProcessToJobObject.side_effect = self.assign
        self.api.QueryInformationJobObject.side_effect = self.accounting

    def first(self, _snapshot, pointer):
        pointer._obj.pid, pointer._obj.parent_pid = 123, self.parent_pid
        return True

    def times(self, _handle, creation, exited, kernel, user):
        put(creation, self.created)
        put(exited, 0)
        put(user, self.cpu[0])
        put(kernel, self.cpu[1])
        return True

    def system(self, idle, kernel, user):
        put(idle, 750)
        put(kernel, 1000)
        put(user, 500)
        return True

    def member(self, process, _job, pointer):
        pointer._obj.value = self.monitor_member if process == 90 else process in self.assigned
        return True

    def assign(self, _job, process):
        self.assigned.add(process)
        self.counts['TotalProcesses'] += 1
        self.counts['ActiveProcesses'] += 1
        return True

    def accounting(self, _handle, information_class, pointer, size, returned):
        assert information_class == 1
        assert size == C.sizeof(job._BasicAccounting)
        for name, value in self.counts.items():
            setattr(pointer._obj, name, value)
        returned._obj.value = size
        return True


class JobAccountingTests(unittest.TestCase):
    def setUp(self):
        self.native = Native()

    def owned(self):
        owned = job.OwnedJob(_kernel=self.native.api, _wall=lambda: 0)
        self.addCleanup(owned.close)
        return owned

    def process(self):
        return SimpleNamespace(pid=123, _handle=91)

    def test_native_layout_uses_fixed_windows_widths(self):
        self.assertEqual(C.sizeof(job._FileTime), 8)
        self.assertEqual(C.sizeof(job._BasicAccounting), 48)
        self.assertEqual(job._BasicAccounting.TotalProcesses.offset, 36)
        if C.sizeof(C.c_void_p) == 8:
            self.assertEqual(C.sizeof(job._ExtendedLimits), 144)
            self.assertEqual(C.sizeof(job._ProcessEntry), 568)

    def test_system_and_monitor_read_raw_counters_brackets_without_closing_pseudohandle(self):
        system = job.read_system(_kernel=self.native.api)
        self.assertEqual((system['total_ticks'], system['busy_ticks']), (1500, 750))
        monitor = job.read_monitor_cpu(_kernel=self.native.api)
        self.assertEqual((monitor['pid'], monitor['cpu_ticks']), (os.getpid(), 30))
        for row in (system, monitor):
            self.assertLessEqual(row['read_started_monotonic'], row['read_finished_monotonic'])
        self.native.api.CloseHandle.assert_not_called()

    def test_gated_new_direct_child_assignment_records_boundary_without_adding_offset(self):
        owned = self.owned()
        receipt = owned.assign(self.process())
        self.assertEqual(receipt['pre_assignment_cpu_lower_ticks'], 30)
        self.assertEqual(receipt['pre_assignment_cpu_upper_ticks'], 30)
        result = owned.accounting()
        self.assertEqual(result['cpu_ticks'], 0)
        self.assertEqual(result['pre_assignment_cpu_lower_ticks_total'], 30)
        self.assertFalse(result['coordinator_in_job'])
        self.assertEqual((result['total_processes'], result['active_processes']), (1, 1))
        self.assertEqual(self.native.api.SetInformationJobObject.call_args.args[2]._obj.basic.flags, 0x2000)
        with self.assertRaisesRegex(job.AccountingError, 'already assigned'):
            owned.assign(self.process())
        owned.close()
        closed = [call.args[0] for call in self.native.api.CloseHandle.call_args_list]
        self.assertEqual(closed, [70, 50])  # Own inventory/Job, never borrowed worker handle.

    def test_normal_exits_are_not_limit_termination_counter(self):
        owned = self.owned()
        owned.assign(self.process())
        self.native.counts.update(ActiveProcesses=0, TotalUserTime=500, TotalKernelTime=100)
        result = owned.accounting()
        self.assertEqual(result['cpu_ticks'], 600)
        self.assertEqual(result['terminated_processes_by_limit'], 0)
        self.assertIn('not all normal', result['terminated_count_semantics'])

    def test_existing_wrong_parent_self_mismatched_handle_and_exited_workers_rejected(self):
        variants = ('old', 'parent', 'self', 'handle', 'exited')
        for variant in variants:
            with self.subTest(variant=variant):
                self.native = Native()
                owned = self.owned()
                process = self.process()
                if variant == 'old': self.native.created = job._FILETIME_EPOCH - 1
                elif variant == 'parent': self.native.parent_pid = os.getpid() + 1
                elif variant == 'self': process.pid = os.getpid()
                elif variant == 'handle': self.native.api.GetProcessId.side_effect = lambda _handle: 999
                else: self.native.api.WaitForSingleObject.return_value = 0
                with self.assertRaises(job.AccountingError): owned.assign(process)
                self.native.api.AssignProcessToJobObject.assert_not_called()

    def test_assignment_or_membership_error_retains_owned_job_for_cleanup(self):
        owned = self.owned()
        self.native.api.AssignProcessToJobObject.side_effect = None
        self.native.api.AssignProcessToJobObject.return_value = False
        with self.assertRaisesRegex(job.AccountingError, 'AssignProcess'):
            owned.assign(self.process())
        self.assertEqual(owned.handle, 50)
        self.native.api.AssignProcessToJobObject.return_value = True
        with self.assertRaisesRegex(job.AccountingError, 'membership'):
            owned.assign(self.process())
        self.assertEqual(owned.handle, 50)

    def test_negative_regressing_and_inconsistent_job_counters_rejected(self):
        owned = self.owned()
        self.native.counts.update(TotalUserTime=-1)
        with self.assertRaisesRegex(job.AccountingError, 'negative'):
            owned.accounting()
        self.native.counts.update(TotalUserTime=100, ActiveProcesses=1, TotalProcesses=0)
        with self.assertRaisesRegex(job.AccountingError, 'inconsistent'):
            owned.accounting()
        self.native.counts.update(ActiveProcesses=0, TotalTerminatedProcesses=1)
        with self.assertRaisesRegex(job.AccountingError, 'inconsistent'):
            owned.accounting()
        self.native.counts.update(TotalTerminatedProcesses=0)
        self.native.counts.update(ActiveProcesses=0)
        owned.accounting()
        self.native.counts.update(TotalUserTime=99)
        with self.assertRaisesRegex(job.AccountingError, 'regressed'):
            owned.accounting()

    def test_native_failure_and_wrong_structure_length_fail_closed(self):
        owned = self.owned()
        self.native.api.QueryInformationJobObject.side_effect = None
        self.native.api.QueryInformationJobObject.return_value = False
        with self.assertRaisesRegex(job.AccountingError, 'QueryInformation'):
            owned.accounting()
        self.native.api.QueryInformationJobObject.return_value = True
        with self.assertRaisesRegex(job.AccountingError, 'structure size'):
            owned.accounting()
        self.native.api.GetProcessTimes.side_effect = None
        self.native.api.GetProcessTimes.return_value = False
        with self.assertRaisesRegex(job.AccountingError, 'GetProcessTimes'):
            job.read_monitor_cpu(_kernel=self.native.api)

    def test_monitor_membership_and_limit_setup_failures_close_only_new_job(self):
        for member in (False, True):
            with self.subTest(member=member):
                native = Native()
                native.monitor_member = member
                if not member: native.api.SetInformationJobObject.return_value = False
                with self.assertRaises(job.AccountingError): job.OwnedJob(_kernel=native.api)
                native.api.CloseHandle.assert_called_once_with(50)

    def test_close_failure_checked_and_success_idempotent(self):
        owned = self.owned()
        self.native.api.CloseHandle.return_value = False
        with self.assertRaisesRegex(job.AccountingError, 'CloseHandle'):
            owned.close()
        self.assertEqual(owned.handle, 50)
        self.native.api.CloseHandle.return_value = True
        owned.close()
        owned.close()
        with self.assertRaisesRegex(job.AccountingError, 'closed'): owned.accounting()

    def test_bad_system_and_clock_or_platform_fail_closed(self):
        self.native.api.GetSystemTimes.side_effect = lambda idle, kernel, user: (put(idle, 2), put(kernel, 1), put(user, 0), True)[-1]
        with self.assertRaisesRegex(job.AccountingError, 'idle'): job.read_system(_kernel=self.native.api)
        with self.assertRaisesRegex(job.AccountingError, 'clock'): job._bracket(2, 1)
        with self.assertRaisesRegex(job.AccountingError, 'clock'): job._bracket(float('nan'), 1)
        with patch.object(job.sys, 'platform', 'linux'):
            with self.assertRaisesRegex(job.AccountingError, 'Windows'): job.read_system()

    def test_termination_preserves_handle_for_final_accounting(self):
        owned = self.owned()
        owned.assign(self.process())
        def terminate(handle, code):
            self.assertEqual((handle, code), (50, 1))
            self.native.counts.update(ActiveProcesses=0, TotalUserTime=100)
            return True
        self.native.api.TerminateJobObject.side_effect = terminate
        final = owned.terminate_and_snapshot()
        self.assertEqual((final['active_processes'], final['cpu_ticks']), (0, 100))
        self.assertEqual(owned.handle, 50)
        self.assertEqual([call.args[0] for call in self.native.api.CloseHandle.call_args_list], [70])
        self.assertEqual(owned.accounting()['cpu_ticks'], 100)

    def test_termination_failure_and_timeout_keep_handle_and_are_explicit(self):
        owned = self.owned()
        self.native.api.TerminateJobObject.return_value = False
        with self.assertRaisesRegex(job.AccountingError, 'TerminateJobObject'):
            owned.terminate()
        self.native.api.TerminateJobObject.return_value = True
        self.native.counts.update(ActiveProcesses=1, TotalProcesses=1)
        clock = [0.]
        owned.clock = lambda: clock[0]
        with self.assertRaisesRegex(job.AccountingError, 'termination bound'):
            owned.terminate_and_snapshot(.1, .05, _sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        self.assertEqual(owned.handle, 50)

    def test_query_and_previous_update_are_serialized_across_threads(self):
        owned = self.owned()
        first_entered, release_first, second_entered = (threading.Event() for _ in range(3))
        calls, errors = [], []
        def query(handle, information, pointer, size, returned):
            index = len(calls)
            calls.append(threading.current_thread().name)
            self.native.accounting(handle, information, pointer, size, returned)
            pointer._obj.TotalUserTime = (index + 1) * 100
            if index == 0:
                first_entered.set()
                release_first.wait(timeout=2.)
            else:
                second_entered.set()
            return True
        self.native.api.QueryInformationJobObject.side_effect = query
        def read():
            try: owned.accounting()
            except Exception as error: errors.append(error)
        first = threading.Thread(target=read, name='first')
        second = threading.Thread(target=read, name='second')
        first.start()
        self.assertTrue(first_entered.wait(timeout=1.))
        second.start()
        self.assertFalse(second_entered.wait(timeout=.05))
        release_first.set()
        first.join(timeout=1.)
        second.join(timeout=1.)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertFalse(errors)
        self.assertEqual(calls, ['first', 'second'])
        self.assertEqual(owned._previous['user_ticks'], 200)

    def test_returned_accounting_cannot_mutate_previous_counter_baseline(self):
        owned = self.owned()
        self.native.counts['TotalUserTime'] = 100
        row = owned.accounting()
        row['user_ticks'] = 1_000_000
        self.native.counts['TotalUserTime'] = 200
        self.assertEqual(owned.accounting()['user_ticks'], 200)


if __name__ == '__main__':
    unittest.main()
