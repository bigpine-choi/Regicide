"""Warning CPU guard with explicit owned-Job membership before ancestry.

Historical frozen guard/provider sources stay unchanged. Only cohort-verified
processes may enter the ownership BFS; outside named engines retain the original
identity and zero-observed-CPU checks even if their numeric PPID matches a worker.
"""
import copy
from dataclasses import asdict, replace
import queue
import threading
import time

from m2_warning_load_guard import (WarningLoadGuard as _WarningLoadGuard,
    WarningGuardMonitor as _WarningGuardMonitor)
from m2_load_guard import M2LoadGuard, LoadPolicy, SystemTimes, descendant_chain, _utc
from m2_job_load_guard import METHOD, SCOPE
from m2_job_accounting import read_monitor_cpu, read_system
from m2_job_process_scope import ScopedWindowsLoadProvider, METHOD as OWNERSHIP_METHOD

SCHEMA_VERSION = 4
RESIDUAL_CPU_POLICY = 'warning-only'


class ScopedJobAccountingProvider:
    """Add the unchanged Job/coordinator/system partition to the safe provider."""
    platform = 'win32'

    def __init__(self, policy, job):
        self.scoped = ScopedWindowsLoadProvider(policy,job)
        self.job = job
        self.accounting_frame = None

    def __getattr__(self, name):
        return getattr(self.scoped,name)

    def snapshot(self, roots):
        self.accounting_frame = None
        snapshot = self.scoped.snapshot(roots)
        frame = {'job_before':self.job.accounting(),'monitor_before':read_monitor_cpu()}
        frame['system'] = read_system()
        frame['monitor_after'] = read_monitor_cpu()
        frame['job_after'] = self.job.accounting()
        for kind in ('job','monitor'):
            if frame[kind+'_after']['cpu_ticks'] < frame[kind+'_before']['cpu_ticks']:
                raise RuntimeError(kind+' CPU regressed within accounting query')
        if any(frame[key].get('coordinator_in_job',False) for key in ('job_before','job_after')):
            raise RuntimeError('coordinator must be outside the owned worker Job')
        before,after=frame['monitor_before'],frame['monitor_after']
        if (before['pid'],before['creation_ticks']) != (after['pid'],after['creation_ticks']):
            raise RuntimeError('coordinator identity changed within accounting query')
        self.accounting_frame=frame
        system=frame['system']
        return replace(snapshot,system=SystemTimes(system['idle_ticks'],system['kernel_ticks'],system['user_ticks']))


class WarningLoadGuard(_WarningLoadGuard):
    def __init__(self, output_jsonl, policy, job, provider=None, *, clock=time.monotonic, sleep=time.sleep):
        selected = provider if provider is not None else ScopedJobAccountingProvider(policy,job)
        super().__init__(output_jsonl,policy,job,selected,clock=clock,sleep=sleep)
        self.summary.update(schema_version=SCHEMA_VERSION,process_ownership_method=OWNERSHIP_METHOD,
                            process_ownership_scope_count=0)

    def _write(self, record):
        if record.get('event') == 'policy':
            record.update(schema_version=SCHEMA_VERSION,accounting_method=METHOD,
                approximate=True,scope=SCOPE,residual_cpu_policy=RESIDUAL_CPU_POLICY,
                process_ownership_method=OWNERSHIP_METHOD)
            return M2LoadGuard._write(self,record)
        if record.get('event') == 'sample':
            record['process_ownership_scope']=copy.deepcopy(getattr(self.provider,'process_ownership_scope',None))
            record['process_ownership_method']=OWNERSHIP_METHOD
            accepted=self.previous is not self._previous_snapshot_before_sample
            if accepted:
                self.summary['process_ownership_scope_count']+=1
                record['process_ownership_scope_index']=self.summary['process_ownership_scope_count']
            else:
                record['process_ownership_scope_index']=None
        return super()._write(record)

    def sample(self, phase='running'):
        self._previous_snapshot_before_sample = self.previous
        if self.closed:
            raise RuntimeError('load guard is closed')
        started = self.clock()
        finished = ()
        record = {'event': 'sample', 'utc': _utc(), 'phase': phase, 'roots': list(self.roots.values()), 'processes': []}
        try:
            frame = self.provider.snapshot(tuple(self.roots.values()))
            ownership = self.provider.process_ownership_scope
            if (not isinstance(ownership,dict) or ownership.get('method') != OWNERSHIP_METHOD
                    or ownership.get('status') != 'COMPLETE'):
                raise RuntimeError('complete explicit owned-Job process scope is required')
            counters = (frame.system.idle_ticks, frame.system.kernel_ticks, frame.system.user_ticks)
            if (any(type(value) is not int or value < 0 for value in counters) or
                    frame.system.idle_ticks > frame.system.kernel_ticks or
                    type(frame.wall_ticks) is not int or frame.wall_ticks <= 0):
                raise RuntimeError('invalid native host counters or creation-clock timestamp')
            record.update(system=asdict(frame.system), wall_ticks=frame.wall_ticks)
            external, owned = {}, {}
            possible = set(self.roots)
            for _ in frame.processes:
                added = {p.pid for p in frame.processes.values() if p.parent_pid in possible and self.provider.is_owned_member(p)} - possible
                if not added:
                    break
                possible.update(added)
            for process in frame.processes.values():
                member = self.provider.is_owned_member(process)
                if (process.pid in possible and member) or self.provider.is_engine(process):
                    if (process.error or type(process.creation_ticks) is not int or process.creation_ticks <= 0 or
                            type(process.cpu_ticks) is not int or process.cpu_ticks < 0):
                        self._fail('process_access', f'cannot measure process PID {process.pid}: {process.error}')
                        record['processes'].append({'pid': process.pid, 'parent_pid': process.parent_pid,
                                                    'name': process.name, 'creation_ticks': process.creation_ticks,
                                                    'cpu_ticks': process.cpu_ticks, 'error': process.error})
                        continue
                    root = self.known_owned.get(process.identity) if member else None
                    chain = None
                    if root is None and member:
                        for identity in self.roots.values():
                            chain = descendant_chain(process, frame.processes, identity)
                            if chain is not None:
                                root = identity
                                self.known_owned[process.identity] = root
                                break
                    is_owned = root is not None
                    if is_owned:
                        owned[process.identity] = process
                    elif self.provider.is_engine(process):
                        external[process.identity] = process
                        if process.exited_during_snapshot:
                            self._fail('external_identity', 'external engine exited during the CPU snapshot')
                    record['processes'].append({'pid': process.pid, 'parent_pid': process.parent_pid,
                        'name': process.name, 'creation_ticks': process.creation_ticks, 'cpu_ticks': process.cpu_ticks,
                        'owned': is_owned, 'job_scope_member': member, 'worker_root': root, 'ancestry': chain,
                        'exited': process.exited_during_snapshot})
            for process in frame.retained_owned:
                if (process.error or process.identity not in self.known_owned or
                        type(process.cpu_ticks) is not int or process.cpu_ticks < 0):
                    self._fail('owned_timer', 'retained owned process timer could not be verified')
                    continue
                owned[process.identity] = process
                if self.known_owned[process.identity] not in self.roots.values() and not process.exited_during_snapshot:
                    self._fail('owned_orphan', 'an owned process is still alive after its worker was detached')
            for identity in self.roots.values():
                current = frame.processes.get(identity[0])
                retained = owned.get(identity)
                if current is not None and current.identity != identity:
                    self._fail('worker_identity', 'active worker PID was reused or its identity cannot be verified')
                elif current is None and (retained is None or not retained.exited_during_snapshot):
                    self._fail('worker_identity', 'active worker disappeared without a retained final timer')
            if self.previous_external is not None:
                if set(external) != set(self.previous_external):
                    self._fail('external_identity', 'external known-engine identity set changed')
                for identity in set(external) & set(self.previous_external):
                    delta = external[identity].cpu_ticks - self.previous_external[identity].cpu_ticks
                    if delta < 0:
                        self._fail('external_counter', 'external engine CPU counter decreased')
                    if delta > 0:
                        self.summary['external_cpu_increase_ticks'] += delta
                        self._fail('external_cpu', 'external known engine consumed CPU')
            self.previous_external = external
            ended = self.clock()
            if self.host_progress_end is None:
                self.host_progress_end = ended
            if self.previous is not None:
                total = frame.system.total_ticks - self.previous.system.total_ticks
                idle = frame.system.idle_ticks - self.previous.system.idle_ticks
                if (total < 0 or idle < 0 or idle > total or
                        frame.system.kernel_ticks < self.previous.system.kernel_ticks or
                        frame.system.user_ticks < self.previous.system.user_ticks or frame.wall_ticks < self.previous.wall_ticks):
                    raise RuntimeError('host counters or wall clock regressed')
                if total > 0:
                    self.host_progress_end = ended
                elif ended - self.host_progress_end >= self.policy.interval_seconds:
                    raise RuntimeError('aggregate host CPU counter did not advance for a sample interval')
                owned_delta = 0
                for identity, process in owned.items():
                    if identity in self.previous_owned:
                        delta = process.cpu_ticks - self.previous_owned[identity].cpu_ticks
                        if delta < 0:
                            raise RuntimeError('owned process CPU counter regressed')
                        owned_delta += delta
                    elif process.creation_ticks >= self.previous.wall_ticks:
                        owned_delta += process.cpu_ticks
                    # An old process first observed now has no previous CPU
                    # baseline: subtract zero, conservatively counting it as
                    # nonworker load for that interval instead of guessing.
                record['host'] = self._host_window(self.previous_end, ended, total, total - idle, owned_delta)
            self.previous, self.previous_owned = frame, owned
            finished = tuple(identity for identity, process in owned.items()
                             if process.exited_during_snapshot and identity not in self.roots.values())
            self.summary['snapshot_count'] += 1
        except Exception as error:
            self._fail('monitor_error', f'load guard failed closed: {type(error).__name__}: {error}')
            ended = self.clock()
        duration = ended - started
        gap = ended - self.previous_end if self.previous_end is not None else duration
        if duration < 0 or gap < 0 or max(duration, gap) > self.policy.max_sample_gap_seconds:
            self._fail('sampling_gap', 'sample duration or sampling gap exceeded its declared bound')
        self.summary['max_sample_gap_seconds'] = max(self.summary['max_sample_gap_seconds'], gap)
        self.summary['max_snapshot_duration_seconds'] = max(self.summary['max_snapshot_duration_seconds'], duration)
        self.previous_end = ended
        record.update(gap_seconds=gap, snapshot_duration_seconds=duration, failure=self.stop_reason,
                      retiring_owned=list(finished),
                      retirement_events=list(frame.retirement_events) if 'frame' in locals() else [],
                      retired_handle_count=frame.retired_handle_count if 'frame' in locals() else 0,
                      retained_owned=[{'pid': p.pid, 'creation_ticks': p.creation_ticks, 'cpu_ticks': p.cpu_ticks,
                                       'exited': p.exited_during_snapshot} for p in frame.retained_owned]
                      if 'frame' in locals() else [])
        try:
            self._write(record)
            self._release_finished(finished)
        except Exception as error:
            self._fail('receipt_error', f'cannot persist load receipt: {error}')
        record['failure'] = self.stop_reason
        return record


class WarningGuardMonitor(_WarningGuardMonitor):
    """Reuse the frozen bounded actor with the explicit-Job guard implementation."""
    def __init__(self, output_jsonl, policy, job, provider=None, *, clock=time.monotonic, sleep=time.sleep):
        self._guard=WarningLoadGuard(output_jsonl,policy,job,provider,clock=clock,sleep=sleep)
        self._lock=threading.RLock()
        self._stop=threading.Event()
        self._wake=threading.Event()
        self._requests=queue.Queue()
        self._thread=None
        self._failure=None
        self._close_requested=False
        self._timer_started=False
        self._timer_samples=0
        self._join_bound=policy.max_sample_gap_seconds
