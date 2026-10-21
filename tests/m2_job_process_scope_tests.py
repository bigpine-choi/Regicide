"""Synthetic Job/Toolhelp/held-object tests; no real process is accessed."""
from copy import deepcopy
import ctypes as C
from dataclasses import replace
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import m2_job_process_scope as scope
from m2_load_guard import LoadPolicy
from see_clean_guard import ProcessInfo


def job_row(pids):
    pointer=C.sizeof(C.c_size_t)
    attempt=dict(success=True,windows_error=0,buffer_capacity=64,buffer_bytes=8+64*pointer,
        returned_bytes=8+len(pids)*pointer,assigned_processes=len(pids),listed_processes=len(pids),pids=sorted(pids))
    return dict(job_handle_kind='explicit-owned-job-handle',coordinator_pid=os.getpid(),coordinator_in_job=False,
        limit_flags=0x2000,limits_returned_bytes=C.sizeof(scope._ExtendedLimits),assigned_processes=len(pids),
        listed_processes=len(pids),returned_bytes=attempt['returned_bytes'],buffer_capacity=64,pointer_bytes=pointer,
        pids=sorted(pids),query_attempts=[attempt],read_started_monotonic=0.,read_finished_monotonic=0.)


class Native:
    def __init__(self):
        self.kernel=Mock()
        self.engine_names={'regicide.exe','stockfish.exe'}
        self.engine_handles={}
        self.handles={};self.objects={};self.next_handle=100
        self.queries=[];self.opens=[];self.members={10,11};self.fail_open=set();self.fail_read=set()
        self.rows={10:ProcessInfo(10,os.getpid(),'python.exe'),11:ProcessInfo(11,10,'regicide.exe')}
        self.objects={(10,1000):ProcessInfo(10,os.getpid(),'python.exe',1000,0),
                      (11,1001):ProcessInfo(11,10,'regicide.exe',1001,50)}
        self.current={10:(10,1000),11:(11,1001)}
        self.kernel.OpenProcess.side_effect=self.open
        self.kernel.CloseHandle.return_value=True
        self.kernel.GetSystemTimes.side_effect=self.system
        self.snapshot=Mock(side_effect=AssertionError('unsafe native.snapshot must not run'))

    def is_engine(self,process):return process.name.casefold().startswith(('regicide','stockfish'))
    def open(self,access,inherit,pid):
        self.opens.append((access,pid))
        if pid in self.fail_open:return 0
        if pid not in self.current:return 0
        self.next_handle+=1;self.handles[self.next_handle]=self.current[pid]
        return self.next_handle
    def _handle_times(self,handle,process):
        identity=self.handles[handle];self.queries.append(identity)
        if identity[0] in self.fail_read:return replace(process,error='synthetic CPU read failure')
        return self.objects[identity]
    def times(self,process):raise AssertionError('old unscoped timer path must not run')
    def close(self):
        for handle,_row in self.engine_handles.values():self.kernel.CloseHandle(handle)
        self.engine_handles.clear()
    def system(self,idle,kernel,user):
        idle._obj.value=100;kernel._obj.value=1000;user._obj.value=500;return True


class ProcessScopeTests(unittest.TestCase):
    def setUp(self):
        self.native=Native()
        self.job=SimpleNamespace(handle=77,api=Mock(),_lock=threading.RLock(),_require_open=Mock())
        def member(handle,job,output):
            self.assertEqual(job,77)
            identity=self.native.handles[handle]
            output._obj.value=int(identity[0] in self.native.members and not self.native.objects[identity].exited_during_snapshot)
            return True
        self.job.api.IsProcessInJob.side_effect=member
        self.reads=0
        self.job_hook=self.inventory_hook=None
        self.provider=scope.ScopedWindowsLoadProvider(LoadPolicy(.2),self.job,_native=self.native,
            _inventory=self.inventory,_job_reader=self.job_reader,_clock=lambda:0.)
        self.addCleanup(self.provider.close)

    def job_reader(self,job,*,clock,receipt):
        self.reads+=1
        if self.job_hook:self.job_hook(self.reads)
        receipt.update(job_row(self.native.members));return receipt
    def inventory(self,native):
        if self.inventory_hook:self.inventory_hook()
        return dict(self.native.rows)
    def snapshot(self):return self.provider.snapshot(((10,1000),))
    def add(self,pid,parent,name,creation,cpu=0,member=False):
        self.native.rows[pid]=ProcessInfo(pid,parent,name)
        value=ProcessInfo(pid,parent,name,creation,cpu)
        self.native.current[pid]=value.identity;self.native.objects[value.identity]=value
        if member:self.native.members.add(pid)
        return value

    def test_job_members_measured_and_retained_without_unsafe_global_snapshot(self):
        frame=self.snapshot()
        self.native.snapshot.assert_not_called()
        self.assertEqual(set(frame.processes),{10,11})
        self.assertEqual(set(self.provider.owned_handles),{(10,1000),(11,1001)})
        self.assertTrue(all(self.provider.is_owned_member(row) for row in frame.processes.values()))
        self.assertEqual(self.provider.last_scope['status'],'COMPLETE')
        self.assertEqual(len(self.native.opens),2)  # first query handles adopted directly

    def test_foreign_ppid_tree_is_raw_only_no_open_or_query(self):
        for pid,parent,name in ((34924,11,'csrss.exe'),(11368,34924,'winlogon.exe'),(777,11368,'dwm.exe')):
            self.native.rows[pid]=ProcessInfo(pid,parent,name)
            self.native.fail_open.add(pid)
        frame=self.snapshot()
        self.assertEqual(set(frame.processes),{10,11})
        self.assertEqual({row['pid'] for row in self.provider.last_scope['outside_parent_matches']},{34924,11368,777})
        self.assertEqual({pid for _access,pid in self.native.opens},{10,11})

    def test_named_outside_engine_and_not_its_protected_parent_is_measured(self):
        self.add(20,34924,'stockfish.exe',2000,12)
        self.native.rows[34924]=ProcessInfo(34924,11,'csrss.exe')
        frame=self.snapshot()
        self.assertIn(20,frame.processes)
        self.assertFalse(self.provider.is_owned_member(frame.processes[20]))
        self.assertNotIn(34924,{pid for _access,pid in self.native.opens})
        self.assertEqual(next(row for row in self.provider.last_scope['selection_reasons'] if row['pid']==20)['reason'],
                         'declared-external-engine')

    def test_outside_named_engine_cannot_be_owned_through_global_ppid_even_if_newer(self):
        self.add(20,11,'stockfish.exe',2000,12)
        frame=self.snapshot()
        self.assertFalse(self.provider.is_owned_member(frame.processes[20]))
        self.assertNotIn((20,2000),self.provider.owned_handles)

    def test_actual_owned_access_or_cpu_failure_is_hard(self):
        self.native.fail_open.add(11)
        with self.assertRaisesRegex(RuntimeError,'owned Job member PID 11'):self.snapshot()
        self.assertEqual(self.provider.last_scope['status'],'FAILED')
        self.assertEqual(self.provider.last_scope['offending_pid'],11)
        self.native.fail_open.clear();self.native.fail_read.add(11)
        with self.assertRaisesRegex(RuntimeError,'read owned Job member'):self.snapshot()

    def test_external_named_engine_access_failure_is_hard(self):
        self.add(20,1,'stockfish.exe',2000)
        self.native.fail_open.add(20)
        with self.assertRaises(RuntimeError):self.snapshot()
        self.assertEqual(self.provider.last_scope['status'],'FAILED')

    def test_pending_gated_nonengine_job_root_need_not_be_registered_yet(self):
        self.add(12,os.getpid(),'python.exe',1002,member=True)
        frame=self.snapshot()
        self.assertIn(12,frame.processes)
        self.assertTrue(self.provider.is_owned_member(frame.processes[12]))
        self.assertNotIn((12,1002),self.provider.owned_handles)

    def test_registration_checks_job_parent_creation_and_liveness(self):
        self.assertEqual(self.provider.identity(10),(10,1000))
        self.provider.watch_root((10,1000))
        self.assertIn((10,1000),self.provider.owned_handles)
        with self.assertRaises(RuntimeError):self.provider.watch_root((10,999))
        self.native.members.remove(10)
        with self.assertRaisesRegex(RuntimeError,'outside owned Job'):self.provider.identity(10)

    def test_retained_exited_job_nonmember_stays_owned_then_two_absences_close(self):
        self.snapshot()
        final=replace(self.native.objects[(11,1001)],cpu_ticks=75,exited_during_snapshot=True)
        self.native.objects[(11,1001)]=final
        self.native.members.remove(11);self.native.fail_open.add(11)
        frame=self.snapshot()
        self.assertTrue(self.provider.is_owned_member(frame.processes[11]))
        self.assertEqual(frame.processes[11].cpu_ticks,75)
        self.assertTrue(self.provider.last_scope['held_exit_recoveries'])
        self.provider.release_finished(((11,1001),))
        self.native.rows.pop(11)
        self.snapshot();self.assertIn((11,1001),self.provider.retired_handles)
        self.snapshot();self.assertNotIn((11,1001),self.provider.retired_handles)

    def test_missing_registered_root_uses_exact_held_final_and_unproved_old_job_pid_fails(self):
        self.snapshot()
        self.native.objects[(10,1000)]=replace(self.native.objects[(10,1000)],exited_during_snapshot=True)
        self.native.rows.pop(10)
        self.snapshot()
        self.assertEqual(self.provider.last_scope['missing_job_pids_with_held_final'][0]['pid'],10)
        self.native.members.add(99)
        with self.assertRaisesRegex(RuntimeError,'persists but is absent'):self.snapshot()

    def test_held_counter_or_identity_reuse_does_not_become_external_or_ignored(self):
        self.snapshot()
        self.native.members.remove(11)
        with self.assertRaisesRegex(RuntimeError,'held-exited'):self.snapshot()
        self.native.objects[(11,1001)]=replace(self.native.objects[(11,1001)],creation_ticks=9999)
        with self.assertRaises(RuntimeError):self.snapshot()

    def test_new_after_only_unobserved_job_pid_gets_one_bounded_inventory_retry(self):
        calls=[]
        self.provider._inventory=lambda native:calls.append(1) or dict(self.native.rows)
        self.job_hook=lambda count:self.native.members.add(99) if count==2 else None
        frame=self.snapshot()
        self.assertNotIn(99,frame.processes)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.provider.last_scope['unobserved_after_only_job_pids'][0]['pid'],99)
        self.assertNotIn(99,{pid for _access,pid in self.native.opens})

    def test_bounded_retry_keeps_second_inventory_named_external_engines(self):
        calls=[]
        def inventory(native):
            calls.append(1)
            if len(calls)==2:self.add(20,1,'stockfish.exe',2000)
            return dict(native.rows)
        self.provider._inventory=inventory
        self.job_hook=lambda count:self.native.members.add(99) if count==2 else None
        frame=self.snapshot()
        self.assertIn(20,frame.processes)
        self.assertFalse(self.provider.is_owned_member(frame.processes[20]))
        self.assertEqual(len(self.provider.last_scope['global_inventory_attempts']),2)

    def test_before_only_unobserved_exit_keeps_job_cpu_but_adopts_no_process_identity(self):
        self.native.members.add(99)
        calls=[]
        self.provider._inventory=lambda native:calls.append(1) or dict(self.native.rows)
        self.job_hook=lambda count:self.native.members.discard(99) if count==2 else None
        frame=self.snapshot()
        self.assertNotIn(99,frame.processes)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.provider.last_scope['unobserved_before_only_job_pids'][0]['pid'],99)
        self.assertNotIn(99,{pid for _access,pid in self.native.opens})

    def test_persistent_unobserved_job_member_and_unknown_registered_root_remain_fatal(self):
        self.native.members.add(99)
        with self.assertRaisesRegex(RuntimeError,'persists but is absent'):self.snapshot()
        self.native.members.discard(99);self.native.rows.pop(10)
        with self.assertRaisesRegex(RuntimeError,'registered root is absent'):self.snapshot()

    def test_after_only_missing_known_held_pid_records_explicit_final_proof(self):
        self.snapshot()
        self.native.objects[(11,1001)]=replace(self.native.objects[(11,1001)],exited_during_snapshot=True)
        self.native.rows.pop(11);self.native.members.remove(11)
        self.job_hook=lambda count:self.native.members.add(11) if count==4 else None
        self.snapshot()
        self.assertEqual(self.provider.last_scope['missing_job_pids_with_held_final'][0]['pid'],11)
        self.assertTrue(self.provider.last_scope['held_exit_recoveries'])


class JobListTests(unittest.TestCase):
    def setUp(self):
        self.api=Mock();self.flags=0x2000;self.pids=[10,11]
        self.api.GetCurrentProcess.return_value=999
        self.api.IsProcessInJob.side_effect=lambda handle,job,out:setattr(out._obj,'value',0) or True
        self.api.QueryInformationJobObject.side_effect=self.query
        self.job=SimpleNamespace(handle=77,api=self.api,_lock=threading.RLock(),_require_open=Mock())
    def query(self,handle,kind,buffer,size,returned):
        self.assertEqual(handle,77)
        if kind==9:
            buffer._obj.basic.flags=self.flags;returned._obj.value=size;return True
        capacity=(size-8)//C.sizeof(C.c_size_t)
        counts=(C.c_uint32*2).from_buffer(buffer);counts[0]=len(self.pids);counts[1]=min(capacity,len(self.pids))
        values=(C.c_size_t*counts[1]).from_buffer(buffer,8)
        for index,pid in enumerate(self.pids[:capacity]):values[index]=pid
        returned._obj.value=8+counts[1]*C.sizeof(C.c_size_t);return True
    def test_explicit_handle_complete_pid_list_and_limits_read(self):
        row=scope.read_owned_job_scope(self.job)
        self.assertEqual(row['pids'],[10,11]);self.assertFalse(row['coordinator_in_job'])
        self.assertEqual(row['job_handle_kind'],'explicit-owned-job-handle')
    def test_breakaway_queryerror_duplicate_truncation_and_coordinator_membership_fatal(self):
        self.flags=0x800
        with self.assertRaises(RuntimeError):scope.read_owned_job_scope(self.job)
        self.flags=0x2000;self.pids=[10,10]
        with self.assertRaises(RuntimeError):scope.read_owned_job_scope(self.job)
        self.pids=[os.getpid()]
        with self.assertRaises(RuntimeError):scope.read_owned_job_scope(self.job)
        self.api.QueryInformationJobObject.side_effect=lambda *args:False
        with self.assertRaises(RuntimeError):scope.read_owned_job_scope(self.job)
    def test_bounded_pid_buffer_grows_for_complete_list(self):
        self.pids=list(range(100,180))
        row=scope.read_owned_job_scope(self.job)
        self.assertEqual(len(row['query_attempts']),2)
        self.assertEqual(row['listed_processes'],80)


if __name__=='__main__':unittest.main()
