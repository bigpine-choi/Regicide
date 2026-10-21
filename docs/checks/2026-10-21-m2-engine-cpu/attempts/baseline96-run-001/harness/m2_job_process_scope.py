"""Read owned Job membership before selecting any process CPU query.

The frozen providers' Toolhelp-to-global-parent CPU discovery is not called.
Only actual Job members, named external engines, and already held exact owned
exit objects are measured. External ancestry is unnecessary for the zero-CPU
external-engine rule. Held final timers and two-absent-snapshot retirement use
the original lifecycle implementation.
"""
from dataclasses import asdict
import ctypes as C
import os
import time

from m2_job_accounting import _ExtendedLimits, _bracket
from m2_load_guard import WindowsLoadProvider, LoadSnapshot, SystemTimes, _FILETIME_EPOCH
from see_clean_guard import WindowsProcesses, ProcessInfo, descendant_chain

METHOD='explicit-owned-job-membership-before-process-query'


def read_owned_job_scope(job, *, clock=time.monotonic, receipt=None):
    """Read-only bounded complete PID list from the explicit owned Job handle."""
    receipt={} if receipt is None else receipt
    started=clock()
    receipt.update(job_handle_kind='explicit-owned-job-handle',coordinator_pid=os.getpid(),
                   read_started_monotonic=started,query_attempts=[])
    with job._lock:
        job._require_open()
        api=job.api
        member=C.c_int32()
        if not api.IsProcessInJob(api.GetCurrentProcess(),job.handle,C.byref(member)) or member.value:
            raise RuntimeError('coordinator is inside the owned Job or membership query failed')
        receipt['coordinator_in_job']=False
        limits,returned=_ExtendedLimits(),C.c_uint32()
        if not api.QueryInformationJobObject(job.handle,9,C.byref(limits),C.sizeof(limits),C.byref(returned)):
            raise RuntimeError('owned Job limit query failed')
        receipt.update(limit_flags=int(limits.basic.flags),limits_returned_bytes=int(returned.value))
        if returned.value!=C.sizeof(limits) or limits.basic.flags & 0x1800:
            raise RuntimeError('owned Job limit length differs or breakaway is permitted')
        capacity=64
        for _ in range(8):
            size=8+capacity*C.sizeof(C.c_size_t)
            buffer,returned=C.create_string_buffer(size),C.c_uint32()
            success=bool(api.QueryInformationJobObject(job.handle,3,buffer,size,C.byref(returned)))
            error=0 if success else getattr(C,'get_last_error',lambda:0)()
            counts=(C.c_uint32*2).from_buffer(buffer)
            assigned,listed=map(int,counts)
            pids=list((C.c_size_t*listed).from_buffer(buffer,8)) if listed<=capacity else []
            receipt['query_attempts'].append(dict(success=success,windows_error=error,buffer_capacity=capacity,
                buffer_bytes=size,returned_bytes=int(returned.value),assigned_processes=assigned,
                listed_processes=listed,pids=pids))
            if listed>capacity or listed>assigned or assigned>4096:
                raise RuntimeError('owned Job PID counts exceed buffer or bound')
            if not success and error not in (122,234):
                raise RuntimeError(f'owned Job PID query failed: {error}')
            if success and listed==assigned:
                if (not 8+listed*C.sizeof(C.c_size_t)<=returned.value<=size
                        or any(type(pid) is not int or not 0<pid<=0xffffffff for pid in pids)
                        or len(set(pids))!=len(pids) or os.getpid() in pids):
                    raise RuntimeError('owned Job PID list is malformed, truncated, duplicated or contains coordinator')
                receipt.update(assigned_processes=assigned,listed_processes=listed,pids=sorted(pids),
                    returned_bytes=int(returned.value),buffer_capacity=capacity,pointer_bytes=C.sizeof(C.c_size_t),
                    **_bracket(started,clock()))
                return receipt
            capacity=max(capacity*2,assigned,(returned.value+C.sizeof(C.c_size_t)-1)//C.sizeof(C.c_size_t))
            if capacity>4096:break
    raise RuntimeError('owned Job PID list did not become complete within bounded queries')


def raw_inventory(native):
    """Only Toolhelp enumeration; never call WindowsProcesses.snapshot()."""
    api=native.kernel
    handle=api.CreateToolhelp32Snapshot(2,0)
    if handle in (None,0,C.c_void_p(-1).value):
        raise RuntimeError('raw process inventory handle failed')
    rows={}
    try:
        entry=native.entry_type();entry.dwSize=C.sizeof(entry)
        success=api.Process32FirstW(handle,C.byref(entry))
        while success:
            process=ProcessInfo(int(entry.th32ProcessID),int(entry.th32ParentProcessID),str(entry.szExeFile))
            if process.pid in rows:raise RuntimeError('raw inventory has duplicate PID')
            rows[process.pid]=process
            success=api.Process32NextW(handle,C.byref(entry))
        if getattr(C,'get_last_error',lambda:0)()!=18:
            raise RuntimeError('raw process inventory did not complete')
    finally:
        if not api.CloseHandle(handle):raise RuntimeError('cannot close raw inventory handle')
    return rows


class ScopedWindowsLoadProvider(WindowsLoadProvider):
    """Preserve held lifecycle, replace all unsafe discovery entry points."""
    def __init__(self,policy,job,*,_native=None,_inventory=None,_job_reader=None,_clock=time.monotonic):
        self.native=_native if _native is not None else WindowsProcesses(policy.engine_names,policy.extra_engine_pids)
        self.engine_names=self.native.engine_names
        self.job,self.clock=job,_clock
        self.owned_handles,self.retired_handles,self.retirement_events={},{},[]
        self._inventory=_inventory or raw_inventory
        self._job_reader=_job_reader or read_owned_job_scope
        self._temporary={}
        self._proven_owned=set()
        self.last_scope=self.process_ownership_scope=None
        self.native.kernel.GetSystemTimes.argtypes=[C.c_void_p]*3
        self.native.kernel.GetSystemTimes.restype=C.c_int32

    def is_owned_member(self,process):
        return process.identity in self._proven_owned

    def _scope(self,roots,operation):
        self._proven_owned=set()
        value=dict(schema_version=1,method=METHOD,status='RUNNING',operation=operation,
            phase='job-before',offending_pid=None,
            coordinator_pid=os.getpid(),roots=[list(row) for row in roots],job_before={},job_after={},
            global_inventory=[],selected_processes=[],selection_reasons=[],queries=[],
            global_inventory_attempts=[],unobserved_after_only_job_pids=[],
            unobserved_before_only_job_pids=[],
            held_exit_recoveries=[],retained_owned=[],outside_parent_matches=[],excluded_nonengine_pids=[],
            missing_job_pids_with_held_final=[])
        self.last_scope=self.process_ownership_scope=value
        return value

    def _held(self,pid):
        rows=[(identity,handle,process) for identity,(handle,process) in self.owned_handles.items() if identity[0]==pid]
        rows += [(identity,entry['handle'],entry['final']) for identity,entry in self.retired_handles.items() if identity[0]==pid]
        rows += [(identity,handle,process) for identity,(handle,process) in self._temporary.items() if identity[0]==pid]
        if len(rows)>1:raise RuntimeError('ambiguous exact held owned PID')
        return rows[0] if rows else None

    def _held_exit(self,pid,raw=None):
        held=self._held(pid)
        if held is None:return None
        identity,handle,previous=held
        final=self.native._handle_times(handle,previous)
        if (final.error or final.identity!=identity or not final.exited_during_snapshot
                or raw is not None and (raw.name.casefold(),raw.parent_pid)!=(previous.name.casefold(),previous.parent_pid)):
            raise RuntimeError(f'PID {pid} lacks consistent exact held-exited ownership proof')
        retired=self.retired_handles.get(identity)
        if retired is not None and final.cpu_ticks!=retired['final'].cpu_ticks:
            raise RuntimeError('accounted retired owned CPU timer changed')
        self._proven_owned.add(identity)
        self.last_scope['held_exit_recoveries'].append(dict(identity=list(identity),final=asdict(final),proof='exact-held-exited-process'))
        return final

    def _query_member(self,process,*,allow_external=False):
        held=self._held(process.pid)
        borrowed=held is not None
        handle=held[1] if borrowed else self.native.kernel.OpenProcess(0x101000,False,process.pid)
        if not handle:raise RuntimeError(f'cannot open actual owned Job member PID {process.pid}')
        keep=False
        try:
            measured=self.native._handle_times(handle,process)
            if measured.error or measured.creation_ticks is None or measured.cpu_ticks is None:
                raise RuntimeError(f'cannot read owned Job member PID {process.pid}: {measured.error}')
            if held is not None and measured.identity!=held[0]:
                raise RuntimeError('held owned PID identity changed')
            included=C.c_int32()
            with self.job._lock:
                self.job._require_open()
                if not self.job.api.IsProcessInJob(handle,self.job.handle,C.byref(included)):
                    raise RuntimeError('actual owned process Job membership query failed')
            if included.value!=1:
                if borrowed:
                    recovered=self._held_exit(process.pid,process)
                    if recovered is not None:return recovered,False
                if allow_external:
                    cached=self.native.engine_handles.get(process.pid)
                    if cached is None or cached[1].identity!=measured.identity:
                        if cached is not None and not self.native.kernel.CloseHandle(cached[0]):
                            raise RuntimeError('cannot close changed external engine cache handle')
                        self.native.engine_handles[process.pid]=(handle,measured)
                        keep=True
                    return measured,False
                raise RuntimeError(f'PID {process.pid} left owned Job without exact held-exit proof')
            self._proven_owned.add(measured.identity)
            if not borrowed:
                self._temporary[measured.identity]=(handle,measured)
                keep=True
            return measured,True
        finally:
            if not borrowed and not keep:
                self.native.kernel.CloseHandle(handle)

    def _retain(self,process):
        temporary=self._temporary.pop(process.identity,None)
        if temporary is not None:
            self.owned_handles[process.identity]=temporary[0],process
            return
        super()._retain(process)

    def _close_temporary(self):
        errors=[]
        for handle,_process in self._temporary.values():
            if not self.native.kernel.CloseHandle(handle):errors.append(handle)
        self._temporary.clear()
        if errors:raise RuntimeError('cannot close temporary owned query handles')

    def _discover(self,roots,operation='snapshot',registration_pid=None):
        scope=self._scope(roots,operation)
        try:
            self._job_reader(self.job,clock=self.clock,receipt=scope['job_before'])
            scope['phase']='global-inventory'
            raw=self._inventory(self.native)
            scope['global_inventory']=[asdict(row) for row in raw.values()]
            scope['global_inventory_attempts'].append(list(scope['global_inventory']))
            before=set(scope['job_before']['pids'])
            selected={}
            def select(pid,reason):
                scope.update(phase='process-query',offending_pid=pid)
                process=raw.get(pid)
                if process is None:
                    final=self._held_exit(pid)
                    if final is None:raise RuntimeError(f'Job PID {pid} is absent without exact held final timer')
                    scope['missing_job_pids_with_held_final'].append(dict(pid=pid,identity=list(final.identity)))
                    return
                if reason=='current-job-member':
                    measured,included=self._query_member(process)
                    scope['queries'].append(dict(pid=pid,reason=reason,in_owned_job_at_read=included,result=asdict(measured)))
                    if not included:reason='known-held-owned-exited'
                elif reason=='known-held-owned-exited':
                    measured=self._held_exit(pid,process)
                    if measured is None:raise RuntimeError('held selection has no exact owned handle')
                else:
                    measured,included=self._query_member(process,allow_external=True)
                    scope['queries'].append(dict(pid=pid,reason=reason,in_owned_job_at_read=included,result=asdict(measured)))
                    if included:reason='current-job-member'
                    if measured.error or measured.creation_ticks is None or measured.cpu_ticks is None:
                        raise RuntimeError(f'cannot measure declared external engine PID {pid}: {measured.error}')
                selected[pid]=measured
                scope['selection_reasons'].append(dict(pid=pid,creation_ticks=measured.creation_ticks,reason=reason))
            initial=before if registration_pid is None else {registration_pid}
            if registration_pid is not None and registration_pid not in before:
                raise RuntimeError('worker registration PID is outside owned Job')
            root_pids={pid for pid,_created in roots}
            for pid in sorted(initial):
                if pid not in raw and self._held(pid) is None and registration_pid is None:
                    if pid in root_pids:raise RuntimeError('registered root is absent without an exact held identity')
                    continue  # Resolve a birth/exit boundary after Job-after.
                select(pid,'current-job-member')
            if registration_pid is None:
                for pid,process in raw.items():
                    if pid in selected:continue
                    held=self._held(pid)
                    if held is not None:select(pid,'known-held-owned-exited')
                    elif self.is_engine(process):select(pid,'declared-external-engine')
            scope.update(phase='job-after',offending_pid=None)
            self._job_reader(self.job,clock=self.clock,receipt=scope['job_after'])
            after=set(scope['job_after']['pids'])
            if registration_pid is None:
                missing_unheld={pid for pid in (before|after)-set(raw) if self._held(pid) is None}
                if missing_unheld:
                    scope.update(phase='bounded-global-inventory-retry',offending_pid=None)
                    later=self._inventory(self.native)
                    scope['global_inventory_attempts'].append([asdict(row) for row in later.values()])
                    # Preserve every initially observed external name; changed
                    # identities remain subject to the unchanged hard checks.
                    for pid,process in later.items():
                        if pid in raw and (raw[pid].name.casefold(),raw[pid].parent_pid)!=(process.name.casefold(),process.parent_pid):
                            if pid in selected or self.is_engine(raw[pid]) or self.is_engine(process):
                                raise RuntimeError('selected or named process raw identity changed during bounded discovery')
                        raw[pid]=process
                    scope['global_inventory']=[asdict(row) for row in raw.values()]
                for pid in sorted((before|after)-set(selected)):
                    if pid not in raw and self._held(pid) is not None:
                        final=self._held_exit(pid)
                        if final is None:raise RuntimeError('missing Job PID lost its held final ownership proof')
                        if not any(row['pid']==pid for row in scope['missing_job_pids_with_held_final']):
                            scope['missing_job_pids_with_held_final'].append(dict(pid=pid,identity=list(final.identity)))
                        continue
                    if pid not in raw:
                        if pid in before and pid in after:
                            raise RuntimeError(f'Job PID {pid} persists but is absent without exact held proof')
                        if pid in before:
                            scope['unobserved_before_only_job_pids'].append(dict(pid=pid,
                                reason='ended-between-inventories-identity-not-adopted',in_job_before=True,in_job_after=False))
                        else:
                            scope['unobserved_after_only_job_pids'].append(dict(pid=pid,
                                reason='born-after-inventory-identity-not-adopted',in_job_before=False,in_job_after=True))
                        continue
                    select(pid,'current-job-member')
                for pid,process in raw.items():
                    if pid not in selected and self.is_engine(process):select(pid,'declared-external-engine')
            if registration_pid is not None and registration_pid not in after:
                raise RuntimeError('worker left owned Job during registration')
            for pid,process in list(selected.items()):
                if pid in after and process.identity not in self._proven_owned:
                    # A named own engine can be born between Job-before and
                    # Toolhelp. Bind it to an actual held Job-member query.
                    selected.pop(pid)
                    scope['selection_reasons']=[row for row in scope['selection_reasons'] if row['pid']!=pid]
                    select(pid,'current-job-member')
                elif process.identity in self._proven_owned and pid not in after:
                    selected[pid]=self._held_exit(pid,raw[pid]) or process
            members=before|after
            possible=set(pid for pid,_created in roots)
            for _ in raw:
                added={row.pid for row in raw.values() if row.parent_pid in possible}-possible
                if not added:break
                possible.update(added)
            scope['outside_parent_matches']=[dict(pid=pid,parent_pid=raw[pid].parent_pid,name=raw[pid].name,
                reason='outside-owned-job-nonengine-parent-match') for pid in sorted(possible-set(selected))
                if pid in raw and pid not in members and not self.is_engine(raw[pid])]
            scope['excluded_nonengine_pids']=sorted(pid for pid,process in raw.items()
                if pid not in selected and pid not in members and not self.is_engine(process))
            scope['selected_processes']=[asdict(row) for row in selected.values()]
            scope.update(status='COMPLETE',phase='complete',offending_pid=None)
            return selected
        except BaseException as error:
            scope.update(status='FAILED',error=f'{type(error).__name__}: {error}')
            raise

    def identity(self,pid):
        try:
            processes=self._discover((),'identity',pid)
            process=processes.get(pid)
            if process is None or process.exited_during_snapshot or process.parent_pid!=os.getpid():
                raise RuntimeError('worker is missing, exited or not a direct coordinator child')
            return process.identity
        finally:self._close_temporary()

    def watch_root(self,identity):
        try:
            process=self._discover((identity,),'watch_root',identity[0]).get(identity[0])
            if process is None or process.identity!=identity or process.exited_during_snapshot or process.parent_pid!=os.getpid():
                raise RuntimeError('registered worker identity/parent/liveness differs')
            self._retain(process)
        finally:self._close_temporary()

    def snapshot(self,roots):
        try:
            wall_ticks=time.time_ns()//100+_FILETIME_EPOCH
            processes=self._discover(roots)
            self._reap_retired(processes)
            for process in processes.values():
                if self.is_owned_member(process) and any(descendant_chain(process,processes,root) is not None for root in roots):
                    self._retain(process)
            retained=[]
            for identity,(handle,previous) in tuple(self.owned_handles.items()):
                measured=self.native._handle_times(handle,previous)
                if measured.error is None and measured.identity==identity:
                    self.owned_handles[identity]=handle,measured
                retained.append(measured)
            self.last_scope['retained_owned']=[asdict(row) for row in retained]
            idle,kernel,user=(C.c_uint64() for _ in range(3))
            if not self.native.kernel.GetSystemTimes(C.byref(idle),C.byref(kernel),C.byref(user)):
                raise RuntimeError('GetSystemTimes failed')
            events=tuple(self.retirement_events)
            self.retirement_events.clear()
            return LoadSnapshot(processes,SystemTimes(idle.value,kernel.value,user.value),wall_ticks,
                                tuple(retained),events,len(self.retired_handles))
        except BaseException as error:
            if self.last_scope is not None:self.last_scope.update(status='FAILED',error=f'{type(error).__name__}: {error}')
            raise
        finally:self._close_temporary()
