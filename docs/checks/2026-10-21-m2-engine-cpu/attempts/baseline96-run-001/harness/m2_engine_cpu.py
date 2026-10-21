"""Held-handle CPU accounting for the actual UCI transport process.

GetProcessTimes includes every thread of the identified process, but not child
processes. CPU/wall ratios are diagnostics, never minimum-utilization gates:
native counter quantization and legitimate immediate moves can yield zero CPU.
The response elapsed time brackets only the supplied callable. Counter reads
and Python receipt construction are outside that response interval.

https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes
"""
from __future__ import annotations

import ctypes as C
import math
import sys
import threading
import time

from m2_job_accounting import AccountingError, CPU_TICKS_PER_SECOND, _FileTime, _process_cpu


class EngineCpuError(AccountingError):
    """No usable CPU receipt can be established; the experiment must fail closed."""


def _api(injected=None):
    if injected is None:
        if sys.platform != 'win32':
            raise EngineCpuError('Windows process CPU accounting is required')
        injected = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([C.c_uint32, C.c_int32, C.c_uint32], C.c_void_p),
        'GetProcessId': ([C.c_void_p], C.c_uint32),
        'GetProcessTimes': ([C.c_void_p] + [C.POINTER(_FileTime)] * 4, C.c_int32),
        'CloseHandle': ([C.c_void_p], C.c_int32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(injected, name)
        function.argtypes, function.restype = arguments, result
    return injected


def _error(operation):
    code = getattr(C, 'get_last_error', lambda: 0)()
    return EngineCpuError(f'{operation} failed (Windows error {code})')


class EngineCpuMeter:
    """Retain one query-only handle; close() never terminates the process.

    launch() should pass the separately verified transport creation identity.
    A held handle remains bound to the original process even after its exit or
    reuse of its numeric PID. Every read nevertheless rechecks PID and creation.
    last_measurement is set even when a measured call fails, for failure logs.
    Private injected API/clock parameters exist only for deterministic fixtures.
    """
    scope = 'actual UCI transport process, all threads; descendants excluded'

    def __init__(self, pid, expected_creation_ticks=None, *, _kernel=None, _clock=time.monotonic):
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise EngineCpuError('transport PID must be a positive Windows DWORD')
        if expected_creation_ticks is not None and (
                type(expected_creation_ticks) is not int or expected_creation_ticks <= 0):
            raise EngineCpuError('expected transport creation identity must be a positive integer')
        self.pid, self.expected_creation_ticks = pid, expected_creation_ticks
        self.api, self.clock = _api(_kernel), _clock
        self.handle = None
        self._last_read = None
        self._lock = threading.RLock()
        self.last_measurement = None
        self.sequence = 0
        # PROCESS_QUERY_LIMITED_INFORMATION: no terminate, VM or write access.
        self.handle = self.api.OpenProcess(0x1000, False, pid)
        if not self.handle:
            self.handle = None
            raise _error('OpenProcess for actual UCI transport')
        try:
            self.initial_read = self.read()
            self.expected_creation_ticks = self.initial_read['creation_ticks']
        except BaseException:
            self.close()
            raise

    def read(self):
        with self._lock:
            if self.handle is None:
                raise EngineCpuError('transport CPU handle is closed')
            try:
                result = _process_cpu(self.api, self.handle, self.pid, self.clock)
            except AccountingError as exc:
                raise EngineCpuError(str(exc)) from exc
            expected = self.expected_creation_ticks
            if expected is not None and result['creation_ticks'] != expected:
                raise EngineCpuError('transport PID creation identity changed or differs from launch')
            previous = self._last_read
            if previous is not None:
                if result['creation_ticks'] != previous['creation_ticks']:
                    raise EngineCpuError('transport process creation identity changed')
                if any(result[key] < previous[key] for key in ('kernel_ticks', 'user_ticks', 'cpu_ticks')):
                    raise EngineCpuError('transport CPU counter regressed')
                if result['read_started_monotonic'] < previous['read_finished_monotonic']:
                    raise EngineCpuError('transport CPU read clock regressed between reads')
            self._last_read = result
            # Return an independent copy so a caller cannot alter the oracle.
            return dict(result)

    def measure_play(self, function, *args, **kwargs):
        """Return (call result, receipt); on failure preserve last_measurement.

        If the call raises, its exception is reraised after the final CPU read.
        If accounting itself fails, EngineCpuError takes precedence and carries
        the partial receipt as .cpu_receipt. No fabricated delta is emitted.
        """
        with self._lock:
            self.sequence += 1
            receipt = {'schema_version': 1, 'sequence': self.sequence,
                       'scope': self.scope, 'status': 'ERROR', 'outcome': 'accounting_failed',
                       'before': None, 'after': None,
                       'call_started_monotonic': None, 'call_finished_monotonic': None,
                       'response_elapsed_seconds': None, 'cpu_per_wall_ratio': None,
                       'ratio_is_diagnostic_only': True,
                       'counter_resolution_note': 'Native quantization; zero CPU delta is valid.'}
            self.last_measurement = receipt
            try:
                receipt['before'] = self.read()
            except BaseException as exc:
                self._attach(exc, receipt)
                raise
            call_error = None
            result = None
            try:
                started = self.clock()
                receipt['call_started_monotonic'] = started
            except BaseException as exc:
                self._attach(exc, receipt)
                raise
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                call_error = exc
            try:
                finished = self.clock()
                receipt['call_finished_monotonic'] = finished
                if (not all(type(value) in (int, float) and math.isfinite(value)
                            for value in (started, finished)) or finished < started
                        or started < receipt['before']['read_finished_monotonic']):
                    raise EngineCpuError('response clock regressed or is not finite')
                receipt['response_elapsed_seconds'] = finished - started
                receipt['after'] = self.read()
                if receipt['after']['read_started_monotonic'] < finished:
                    raise EngineCpuError('final CPU read precedes response completion')
                for name in ('kernel', 'user', 'cpu'):
                    delta = receipt['after'][name + '_ticks'] - receipt['before'][name + '_ticks']
                    receipt[name + '_delta_ticks'] = delta
                    receipt[name + '_seconds'] = delta / CPU_TICKS_PER_SECOND
                # A zero elapsed interval has no meaningful ratio.
                elapsed = receipt['response_elapsed_seconds']
                receipt['cpu_per_wall_ratio'] = receipt['cpu_seconds'] / elapsed if elapsed > 0 else None
                receipt['status'] = 'ERROR' if call_error is not None else 'OK'
                receipt['outcome'] = 'call_failed' if call_error is not None else 'complete'
                if call_error is not None:
                    receipt['call_error_type'] = type(call_error).__name__
            except BaseException as exc:
                receipt['status'] = 'ERROR'
                receipt['outcome'] = 'accounting_failed'
                if call_error is not None:
                    receipt['call_error_type'] = type(call_error).__name__
                self._attach(exc, receipt)
                raise
            if call_error is not None:
                self._attach(call_error, receipt)
                raise call_error
            return result, receipt

    @staticmethod
    def _attach(error, receipt):
        receipt['error_type'] = type(error).__name__
        receipt['error_message'] = str(error)
        try:
            error.cpu_receipt = receipt
        except (AttributeError, TypeError):
            pass

    def close(self):
        with self._lock:
            if self.handle is None:
                return
            if not self.api.CloseHandle(self.handle):
                raise _error('CloseHandle for actual UCI transport')
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
