"""Warning-only residual CPU policy with unchanged fail-closed Job accounting.

The historical 20 percent residual threshold remains in LoadPolicy and every
raw window. Crossing it records a warning; it does not establish engine-level
contention and therefore does not invalidate a run. Identity, native counters,
external engine CPU, receipt persistence and sampling coverage remain fatal.
"""
import queue
import threading
import time

from m2_job_load_guard import JobLoadGuard, JobGuardMonitor, LoadPolicy, METHOD, SCOPE
from m2_load_guard import M2LoadGuard

SCHEMA_VERSION = 3
RESIDUAL_CPU_POLICY = 'warning-only'


class WarningLoadGuard(JobLoadGuard):
    def __init__(self, output_jsonl, policy, job, provider=None, *, clock=time.monotonic, sleep=time.sleep):
        self._warning_active = False
        super().__init__(output_jsonl, policy, job, provider, clock=clock, sleep=sleep)
        self.summary.update(schema_version=SCHEMA_VERSION, residual_cpu_policy=RESIDUAL_CPU_POLICY,
            residual_load_ready_window_count=0, residual_load_warning_window_count=0,
            residual_load_warning_episode_count=0, residual_load_warning_peak_fraction=0.)

    def _fail(self, kind, reason):
        # JobLoadGuard has one historical fatal residual-threshold branch.
        # Suppress only that branch; never suppress native/identity/gap errors.
        if kind != 'host_nonworker_cpu':
            return super()._fail(kind, reason)

    def _write(self, record):
        if record.get('event') == 'policy':
            record.update(schema_version=SCHEMA_VERSION, accounting_method=METHOD,
                approximate=True, scope=SCOPE, residual_cpu_policy=RESIDUAL_CPU_POLICY)
            # The parent writes schema 2 for its historical strict policy.
            return M2LoadGuard._write(self, record)
        if record.get('event') == 'sample':
            record['residual_cpu_policy'] = RESIDUAL_CPU_POLICY
        return super()._write(record)

    def _host_window(self, start, end, total, busy, observed_owned):
        host = super()._host_window(start, end, total, busy, observed_owned)
        ready = host['window_ready']
        warning = ready and host['window_fraction'] > self.policy.max_nonworker_cpu_fraction
        episode_started = warning and not self._warning_active
        if ready:
            self.summary['residual_load_ready_window_count'] += 1
        if warning:
            self.summary['residual_load_warning_window_count'] += 1
            self.summary['residual_load_warning_episode_count'] += int(episode_started)
            self.summary['residual_load_warning_peak_fraction'] = max(
                self.summary['residual_load_warning_peak_fraction'], host['window_fraction'])
        self._warning_active = warning
        host.update(residual_cpu_policy=RESIDUAL_CPU_POLICY,
            residual_load_warning_threshold_fraction=self.policy.max_nonworker_cpu_fraction,
            residual_load_warning=warning, residual_load_warning_episode_started=episode_started)
        return host


class WarningGuardMonitor(JobGuardMonitor):
    def __init__(self, output_jsonl, policy, job, provider=None, *, clock=time.monotonic, sleep=time.sleep):
        self._guard = WarningLoadGuard(output_jsonl, policy, job, provider, clock=clock, sleep=sleep)
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
