"""Complete owned Job CPU and separate coordinator CPU; residual is approximate.

Keep historical sampled identity/ancestry and zero external-engine CPU checks.
Job totals retain CPU of descendants which start and finish between snapshots.
Counter queries are asynchronous and quantized: midpoint readings and rolling
interpolation are point estimates, not rigorous instantaneous error bounds.
"""
from dataclasses import replace
import queue
import threading
import time

from m2_load_guard import (LoadPolicy, LoadGuardViolation, WindowsLoadProvider,
                           M2LoadGuard, GuardMonitor, SystemTimes)
from m2_job_accounting import read_monitor_cpu, read_system

METHOD = 'windows-job-plus-coordinator-cpu'
SCOPE = ('Job CPU covers active and exited owned descendants; coordinator CPU is '
         'measured separately. Residual system/background CPU is an asynchronous, '
         'quantized rolling estimate, not application attribution, OS isolation, '
         'thermal certification or a bound on individual move contention.')


def point(frame, category):
    return (frame[category + '_before']['cpu_ticks'] +
            frame[category + '_after']['cpu_ticks']) / 2


class JobWindowsProvider(WindowsLoadProvider):
    def __init__(self, policy, job):
        super().__init__(policy)
        self.job = job
        self.accounting_frame = None

    def snapshot(self, roots):
        # Identity discovery is outside the short accounting query bracket.
        self.accounting_frame = None
        snapshot = super().snapshot(roots)
        frame = {'job_before': self.job.accounting(),
                 'monitor_before': read_monitor_cpu()}
        frame['system'] = read_system()
        frame['monitor_after'] = read_monitor_cpu()
        frame['job_after'] = self.job.accounting()
        for kind in ('job', 'monitor'):
            if frame[kind + '_after']['cpu_ticks'] < frame[kind + '_before']['cpu_ticks']:
                raise RuntimeError(kind + ' CPU regressed within accounting query')
        if any(frame[k].get('coordinator_in_job', False) for k in ('job_before', 'job_after')):
            raise RuntimeError('coordinator must be outside the owned worker Job')
        a, b = frame['monitor_before'], frame['monitor_after']
        if (a['pid'], a['creation_ticks']) != (b['pid'], b['creation_ticks']):
            raise RuntimeError('coordinator identity changed within accounting query')
        self.accounting_frame = frame
        system = frame['system']
        return replace(snapshot, system=SystemTimes(system['idle_ticks'], system['kernel_ticks'], system['user_ticks']))


class JobLoadGuard(M2LoadGuard):
    def __init__(self, output_jsonl, policy, job, provider=None, *, clock=time.monotonic, sleep=time.sleep):
        self.job = job
        self.previous_accounting = None
        super().__init__(output_jsonl, policy, provider if provider is not None else JobWindowsProvider(policy, job),
                         clock=clock, sleep=sleep)
        self.summary.update(schema_version=2, accounting_method=METHOD, approximate=True, scope=SCOPE,
                            experiment_job_delta_ticks=0, coordinator_delta_ticks=0,
                            accounting_underflow_ticks=0)

    def sample(self, phase='running'):
        self._previous_snapshot_before_sample = self.previous
        return super().sample(phase)

    def _write(self, record):
        if record.get('event') == 'policy':
            record.update(schema_version=2, accounting_method=METHOD, approximate=True, scope=SCOPE)
        if record.get('event') == 'sample':
            current = getattr(self.provider, 'accounting_frame', None)
            record['native_accounting'] = current
            record['accounting_method'] = METHOD
            accepted = self.previous is not self._previous_snapshot_before_sample
            record['accounting_baseline_accepted'] = accepted
            if current is not None and accepted:
                self.previous_accounting = current
        return super()._write(record)

    def _host_window(self, start, end, total, busy, observed_owned):
        current = self.provider.accounting_frame
        previous = self.previous_accounting
        if current is None or previous is None:
            raise RuntimeError('complete native accounting baseline is required')
        a, b = previous['monitor_after'], current['monitor_before']
        if (a['pid'], a['creation_ticks']) != (b['pid'], b['creation_ticks']):
            raise RuntimeError('coordinator identity changed between samples')
        for category in ('job', 'monitor'):
            if current[category + '_before']['cpu_ticks'] < previous[category + '_after']['cpu_ticks']:
                raise RuntimeError(category + ' cumulative CPU counter regressed')
        owned = point(current, 'job') - point(previous, 'job')
        coordinator = point(current, 'monitor') - point(previous, 'monitor')
        residual = busy - owned - coordinator
        self.summary['experiment_job_delta_ticks'] += owned
        self.summary['coordinator_delta_ticks'] += coordinator
        self.summary['accounting_underflow_ticks'] += max(0, -residual)
        # Preserve signed intervals. Clip only the aggregated estimate, so
        # quantized negative intervals do not systematically inflate residual.
        self.host_intervals.append((start, end, total, residual))
        cutoff = end - self.policy.host_window_seconds
        while self.host_intervals and self.host_intervals[0][1] <= cutoff:
            self.host_intervals.popleft()
        totals = residuals = seconds = 0.
        for left, right, count, other in self.host_intervals:
            overlap = max(0., right - max(left, cutoff))
            if overlap and right > left:
                weight = overlap / (right - left)
                totals += count * weight
                residuals += other * weight
                seconds += overlap
        ready = seconds >= self.policy.host_window_seconds - 1e-6 and totals > 0
        fraction = max(0., residuals) / totals if totals > 0 else None
        if ready:
            self.summary['max_nonworker_cpu_fraction'] = max(self.summary['max_nonworker_cpu_fraction'], fraction)
        if ready and fraction > self.policy.max_nonworker_cpu_fraction:
            self._fail('host_nonworker_cpu', 'rolling residual system/background CPU estimate exceeded the declared threshold')
        return {'total_delta_ticks': total, 'busy_delta_ticks': busy,
                'owned_delta_ticks': owned, 'experiment_job_delta_ticks': owned,
                'coordinator_delta_ticks': coordinator,
                'observed_process_delta_ticks_diagnostic': observed_owned,
                'residual_signed_delta_ticks': residual, 'nonworker_delta_ticks': residual,
                'accounting_underflow_ticks': max(0, -residual),
                'window_seconds': seconds, 'window_total_ticks': totals,
                'window_nonworker_ticks': residuals, 'window_fraction': fraction,
                'window_ready': ready, 'approximate': True}


class JobGuardMonitor(GuardMonitor):
    def __init__(self, output_jsonl, policy, job, provider=None, *, clock=time.monotonic, sleep=time.sleep):
        self._guard = JobLoadGuard(output_jsonl, policy, job, provider, clock=clock, sleep=sleep)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._requests = queue.Queue()
        self._thread = None
        self._failure = None
        self._close_requested = False
        self._timer_started = False
        self._timer_samples = 0
        self._join_bound = policy.max_sample_gap_seconds
