"""Fail-closed sampled Windows load receipts for a declared worker cohort.

No process is killed here. Only proven worker descendants are subtracted from
host CPU; named external engines must retain their identities and consume zero
observed CPU. Unknown/transient workloads between samples are not excluded.
The historical see_clean_guard module and its frozen evidence remain unchanged.
"""
from collections import deque
from dataclasses import asdict, dataclass
import ctypes
from ctypes import wintypes
import datetime
import json
import math
import os
from pathlib import Path
import time

from see_clean_guard import (CPU_TICKS_PER_SECOND, DEFAULT_ENGINE_NAMES,
                             ProcessInfo, WindowsProcesses, descendant_chain)

_FILETIME_EPOCH = 11644473600 * CPU_TICKS_PER_SECOND


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class LoadPolicy:
    max_nonworker_cpu_fraction: float
    interval_seconds: float = 0.5
    max_sample_gap_seconds: float = 2.0
    host_window_seconds: float = 2.0
    engine_names: tuple = tuple(sorted(DEFAULT_ENGINE_NAMES | {'sunfish.exe'}))
    extra_engine_pids: tuple = ()

    def __post_init__(self):
        if not _positive(self.interval_seconds) or not 0.5 <= self.interval_seconds <= 1:
            raise ValueError('sample interval must be between 0.5 and 1 second')
        if not _positive(self.max_sample_gap_seconds) or self.max_sample_gap_seconds < self.interval_seconds:
            raise ValueError('sampling gap bound must be finite and at least the sample interval')
        if not _positive(self.host_window_seconds) or self.host_window_seconds < self.interval_seconds:
            raise ValueError('host window must be finite and at least the sample interval')
        if (type(self.max_nonworker_cpu_fraction) not in (int, float) or
                not math.isfinite(self.max_nonworker_cpu_fraction) or
                not 0 <= self.max_nonworker_cpu_fraction <= 1):
            raise ValueError('nonworker CPU threshold must be declared between zero and one')
        if (not isinstance(self.engine_names, (tuple, list)) or not self.engine_names or
                any(not isinstance(name, str) or not name or any(c in name for c in '\r\n\0')
                    for name in self.engine_names)):
            raise ValueError('engine_names must be a nonempty literal sequence')
        if not isinstance(self.extra_engine_pids, (tuple, list)) or any(type(pid) is not int or pid <= 0 for pid in self.extra_engine_pids):
            raise ValueError('extra_engine_pids must contain positive PIDs')
        object.__setattr__(self, 'engine_names', tuple(self.engine_names))
        object.__setattr__(self, 'extra_engine_pids', tuple(self.extra_engine_pids))


@dataclass(frozen=True)
class SystemTimes:
    idle_ticks: int
    kernel_ticks: int
    user_ticks: int

    @property
    def total_ticks(self):
        # Windows kernel includes idle time; do not add idle a second time.
        return self.kernel_ticks + self.user_ticks


@dataclass(frozen=True)
class LoadSnapshot:
    processes: dict
    system: SystemTimes
    wall_ticks: int
    retained_owned: tuple = ()


class LoadGuardViolation(RuntimeError):
    pass


class WindowsLoadProvider:
    """Reuse Toolhelp/GetProcessTimes; add aggregate CPU and held worker handles."""
    platform = 'win32'

    def __init__(self, policy):
        # WindowsProcesses rejects unsupported hosts before loading kernel32.
        self.native = WindowsProcesses(policy.engine_names, policy.extra_engine_pids)
        self.engine_names = self.native.engine_names
        self.owned_handles = {}
        self.native.kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
        self.native.kernel.GetSystemTimes.restype = wintypes.BOOL

    def is_engine(self, process):
        return self.native.is_engine(process)

    def _retain(self, process):
        if process.identity in self.owned_handles:
            handle, _old = self.owned_handles[process.identity]
            self.owned_handles[process.identity] = handle, process
            return
        handle = self.native.kernel.OpenProcess(0x101000, False, process.pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'cannot retain owned process CPU handle')
        measured = self.native._handle_times(handle, process)
        if measured.error or measured.identity != process.identity:
            self.native.kernel.CloseHandle(handle)
            raise RuntimeError('owned process identity changed before retaining its handle')
        self.owned_handles[process.identity] = handle, measured

    def identity(self, pid):
        process = self.native.snapshot().get(pid)
        if process is None:
            raise RuntimeError('worker PID is not present')
        measured = self.native.times(process)
        if measured.error or measured.creation_ticks is None or measured.exited_during_snapshot:
            raise RuntimeError('cannot establish live worker identity')
        return measured.identity

    def watch_root(self, identity):
        processes = self.native.snapshot()
        process = processes.get(identity[0])
        if process is None:
            raise RuntimeError('worker exited before registration')
        process = self.native.times(process)
        if process.error or process.identity != identity or process.exited_during_snapshot:
            raise RuntimeError('worker identity differs at registration')
        if process.parent_pid != os.getpid():
            raise RuntimeError('worker root must be a direct subprocess of the monitor')
        self._retain(process)

    def snapshot(self, roots):
        wall_ticks = time.time_ns() // 100 + _FILETIME_EPOCH
        processes = self.native.snapshot()
        possible = {pid for pid, _created in roots if pid in processes}
        changed = True
        while changed:
            added = {process.pid for process in processes.values()
                     if process.parent_pid in possible and process.pid not in possible}
            changed = bool(added)
            possible.update(added)
        for pid in possible:
            process = processes[pid]
            if process.creation_ticks is None or process.cpu_ticks is None:
                processes[pid] = self.native.times(process)
        for pid in possible:
            process = processes[pid]
            if (not process.error and process.creation_ticks is not None and
                    any(descendant_chain(process, processes, root) is not None for root in roots)):
                self._retain(process)
        retained = tuple(self.native._handle_times(handle, process)
                         for handle, process in self.owned_handles.values())
        idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
        if not self.native.kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError(ctypes.get_last_error(), 'GetSystemTimes failed')
        def ticks(value):
            return value.dwLowDateTime | value.dwHighDateTime << 32
        return LoadSnapshot(processes, SystemTimes(ticks(idle), ticks(kernel), ticks(user)), wall_ticks, retained)

    def release_finished(self, identities):
        """Release only already-accounted, signaled exact process objects."""
        for identity in identities:
            held = self.owned_handles.get(identity)
            if held is None:
                raise RuntimeError('finished owned process has no retained handle')
            handle, process = held
            final = self.native._handle_times(handle, process)
            if final.error or final.identity != identity or not final.exited_during_snapshot:
                raise RuntimeError('cannot release an unverified or live owned process')
            if not self.native.kernel.CloseHandle(handle):
                raise OSError(ctypes.get_last_error(), 'cannot close finished owned process handle')
            del self.owned_handles[identity]
            cached = self.native.engine_handles.get(identity[0])
            if cached is not None and cached[1].identity == identity:
                if not self.native.kernel.CloseHandle(cached[0]):
                    raise OSError(ctypes.get_last_error(), 'cannot close finished engine timer handle')
                del self.native.engine_handles[identity[0]]

    def close(self):
        for handle, _process in self.owned_handles.values():
            self.native.kernel.CloseHandle(handle)
        self.owned_handles.clear()
        self.native.close()


class M2LoadGuard:
    def __init__(self, output_jsonl, policy, provider=None, *, clock=time.monotonic, sleep=time.sleep):
        if not isinstance(policy, LoadPolicy):
            raise TypeError('a frozen LoadPolicy is required')
        self.policy = policy
        self.provider = provider if provider is not None else WindowsLoadProvider(policy)
        if getattr(self.provider, 'platform', None) != 'win32':
            raise RuntimeError('M2 formal load guard supports Windows only')
        self.clock, self.sleep = clock, sleep
        self.output = Path(output_jsonl)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.output.open('x', encoding='utf-8', newline='\n')
        self.roots, self.known_owned = {}, {}
        self.previous, self.previous_end, self.previous_owned = None, None, {}
        self.previous_external = None
        self.host_progress_end = None
        self.host_intervals = deque()
        self.closed = False
        self.summary = {'schema_version': 1, 'started_utc': _utc(), 'finished_utc': None,
                        'policy': asdict(policy), 'monitor_pid': os.getpid(), 'platform': 'win32',
                        'cpu_ticks_per_second': CPU_TICKS_PER_SECOND, 'external_cpu_tolerance_ticks': 0,
                        'snapshot_count': 0, 'max_sample_gap_seconds': 0., 'max_snapshot_duration_seconds': 0.,
                        'external_cpu_increase_ticks': 0, 'max_nonworker_cpu_fraction': 0.,
                        'preflight_complete': False, 'completed_idle_seconds': 0., 'failure': None,
                        'workers': [], 'valid_for_load_check': False,
                        'scope': 'Sampled known-engine CPU/identity and aggregate nonworker CPU estimate; '
                                 'unknown or transient work between samples, power, thermals and OS isolation are not certified.'}
        self._write({'event': 'policy', 'utc': _utc(), 'policy': asdict(policy),
                     'monitor_pid': os.getpid(), 'platform': 'win32',
                     'cpu_ticks_per_second': CPU_TICKS_PER_SECOND, 'scope': self.summary['scope']})

    @property
    def stop_reason(self):
        return self.summary['failure']

    def _fail(self, kind, reason):
        if self.stop_reason is None:
            self.summary['failure'] = {'kind': kind, 'reason': reason, 'utc': _utc()}
        self.summary['valid_for_load_check'] = False

    def _write(self, record):
        try:
            self.stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
            self.stream.flush()
        except Exception as error:
            self._fail('receipt_error', f'cannot persist load receipt: {error}')
            raise LoadGuardViolation(self.stop_reason['reason']) from error

    def assert_clean(self):
        if self.stop_reason is not None:
            raise LoadGuardViolation(self.stop_reason['reason'])

    def identify_worker(self, pid):
        self.assert_clean()
        try:
            return self.provider.identity(pid)
        except Exception as error:
            self._fail('worker_registration', f'cannot identify worker: {type(error).__name__}: {error}')
            self.assert_clean()

    def attach_worker(self, pid, creation_ticks):
        self.assert_clean()
        if not self.summary['preflight_complete']:
            raise RuntimeError('complete an idle preflight before starting workers')
        if any(type(value) is not int or value <= 0 for value in (pid, creation_ticks)) or pid == os.getpid():
            raise ValueError('worker identity requires a positive subprocess PID and creation ticks')
        if pid in self.roots:
            raise ValueError('worker PID is already attached')
        identity = pid, creation_ticks
        try:
            self.provider.watch_root(identity)
        except Exception as error:
            self._fail('worker_registration', f'worker registration failed: {type(error).__name__}: {error}')
            self._write({'event': 'attach_failed', 'utc': _utc(), 'identity': identity, 'failure': self.stop_reason})
            self.assert_clean()
        self.roots[pid] = identity
        self.known_owned[identity] = identity
        self.summary['workers'].append({'pid': pid, 'creation_ticks': creation_ticks, 'attached_utc': _utc(), 'detached_utc': None})
        self._write({'event': 'attach', 'utc': _utc(), 'identity': identity})

    def detach_worker(self, pid):
        if pid not in self.roots:
            raise ValueError('worker PID is not attached')
        identity = self.roots[pid]
        final = self.previous_owned.get(identity)
        if final is None or not final.exited_during_snapshot:
            self._fail('worker_detach', 'sample the exited worker final CPU timer before detaching it')
            self.assert_clean()
        del self.roots[pid]
        for entry in reversed(self.summary['workers']):
            if (entry['pid'], entry['creation_ticks']) == identity:
                entry['detached_utc'] = _utc()
                break
        self._write({'event': 'detach', 'utc': _utc(), 'identity': identity})
        self._release_finished((identity,))

    def _release_finished(self, identities):
        if not identities:
            return
        try:
            self.provider.release_finished(identities)
        except Exception as error:
            self._fail('owned_retirement', f'cannot retire finished process timers: {type(error).__name__}: {error}')
            self._write({'event': 'retire_failed', 'utc': _utc(), 'identities': identities, 'failure': self.stop_reason})

    def _host_window(self, start, end, total, busy, owned):
        nonworker = max(0, busy - owned)
        if total > 0:
            self.host_intervals.append((start, end, total, nonworker))
        cutoff = end - self.policy.host_window_seconds
        while self.host_intervals and self.host_intervals[0][1] <= cutoff:
            self.host_intervals.popleft()
        totals = nonworkers = seconds = 0.
        for left, right, count, other in self.host_intervals:
            overlap = max(0., right - max(left, cutoff))
            if overlap and right > left:
                weight = overlap / (right - left)
                totals += count * weight
                nonworkers += other * weight
                seconds += overlap
        ready = seconds >= self.policy.host_window_seconds - 1e-6 and totals > 0
        fraction = nonworkers / totals if totals > 0 else None
        if ready:
            self.summary['max_nonworker_cpu_fraction'] = max(self.summary['max_nonworker_cpu_fraction'], fraction)
        if ready and fraction > self.policy.max_nonworker_cpu_fraction:
            self._fail('host_nonworker_cpu', 'rolling nonworker host CPU fraction exceeded the declared threshold')
        return {'total_delta_ticks': total, 'busy_delta_ticks': busy, 'owned_delta_ticks': owned,
                'nonworker_delta_ticks': nonworker, 'accounting_underflow_ticks': max(0, owned - busy),
                'window_seconds': seconds, 'window_total_ticks': totals, 'window_nonworker_ticks': nonworkers,
                'window_fraction': fraction, 'window_ready': ready}

    def sample(self, phase='running'):
        if self.closed:
            raise RuntimeError('load guard is closed')
        started = self.clock()
        finished = ()
        record = {'event': 'sample', 'utc': _utc(), 'phase': phase, 'roots': list(self.roots.values()), 'processes': []}
        try:
            frame = self.provider.snapshot(tuple(self.roots.values()))
            counters = (frame.system.idle_ticks, frame.system.kernel_ticks, frame.system.user_ticks)
            if (any(type(value) is not int or value < 0 for value in counters) or
                    frame.system.idle_ticks > frame.system.kernel_ticks or
                    type(frame.wall_ticks) is not int or frame.wall_ticks <= 0):
                raise RuntimeError('invalid native host counters or creation-clock timestamp')
            record.update(system=asdict(frame.system), wall_ticks=frame.wall_ticks)
            external, owned = {}, {}
            possible = set(self.roots)
            for _ in frame.processes:
                added = {p.pid for p in frame.processes.values() if p.parent_pid in possible} - possible
                if not added:
                    break
                possible.update(added)
            for process in frame.processes.values():
                if process.pid in possible or self.provider.is_engine(process):
                    if (process.error or type(process.creation_ticks) is not int or process.creation_ticks <= 0 or
                            type(process.cpu_ticks) is not int or process.cpu_ticks < 0):
                        self._fail('process_access', f'cannot measure process PID {process.pid}: {process.error}')
                        record['processes'].append({'pid': process.pid, 'error': process.error})
                        continue
                    root = self.known_owned.get(process.identity)
                    chain = None
                    if root is None:
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
                        'owned': is_owned, 'worker_root': root, 'ancestry': chain,
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

    def require_idle(self, seconds):
        if not _positive(seconds) or seconds < self.policy.host_window_seconds or self.roots:
            raise ValueError('idle preflight must precede workers and cover the complete host window')
        self.assert_clean()
        self.sample('idle-start')
        self.assert_clean()
        start = self.clock()
        record = None
        while self.clock() - start < seconds:
            self.sleep(min(self.policy.interval_seconds, seconds - (self.clock() - start)))
            record = self.sample('idle')
            self.assert_clean()
        if record is None or not record.get('host', {}).get('window_ready'):
            self._fail('host_window_incomplete', 'idle preflight did not produce a complete host CPU window')
            self.assert_clean()
        self.summary['completed_idle_seconds'] = self.clock() - start
        self.summary['preflight_complete'] = True

    def close(self):
        if self.closed:
            return
        if self.roots:
            self._fail('active_workers_on_close', 'detach every finished worker before closing the load guard')
        try:
            self.sample('closed')
        finally:
            self.closed = True
            try:
                self.stream.close()
                self.provider.close()
            except Exception as error:
                self._fail('monitor_close', f'load monitor could not close cleanly: {error}')
            self.summary['finished_utc'] = _utc()
            self.summary['valid_for_load_check'] = self.stop_reason is None and self.summary['preflight_complete']
