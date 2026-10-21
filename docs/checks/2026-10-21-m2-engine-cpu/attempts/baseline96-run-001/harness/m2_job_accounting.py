"""Windows Job/monitor/system CPU counters with explicit sampling boundaries.

Own only newly created direct workers. The runner must keep each worker gated
until assign() succeeds. No correction is silently added for pre-assignment
CPU, and no atomic CPU partition or exact timing accuracy is claimed.

Microsoft references:
https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes
"""
import ctypes as C
from functools import wraps
import math
import os
import sys
import time
import threading

CPU_TICKS_PER_SECOND = 10_000_000
_FILETIME_EPOCH = 11644473600 * CPU_TICKS_PER_SECOND
_DWORD, _BOOL, _HANDLE = C.c_uint32, C.c_int32, C.c_void_p


class AccountingError(RuntimeError):
    pass


class _FileTime(C.Structure):
    _fields_ = [('low', _DWORD), ('high', _DWORD)]


class _BasicAccounting(C.Structure):
    _fields_ = [('TotalUserTime', C.c_int64), ('TotalKernelTime', C.c_int64),
                ('ThisPeriodTotalUserTime', C.c_int64), ('ThisPeriodTotalKernelTime', C.c_int64),
                ('TotalPageFaultCount', _DWORD), ('TotalProcesses', _DWORD),
                ('ActiveProcesses', _DWORD), ('TotalTerminatedProcesses', _DWORD)]


class _BasicLimits(C.Structure):
    _fields_ = [('process_time', C.c_int64), ('job_time', C.c_int64), ('flags', _DWORD),
                ('minimum_working_set', C.c_size_t), ('maximum_working_set', C.c_size_t),
                ('active_limit', _DWORD), ('affinity', C.c_size_t), ('priority', _DWORD), ('scheduling', _DWORD)]


class _ExtendedLimits(C.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', C.c_uint64 * 6),
                ('process_memory', C.c_size_t), ('job_memory', C.c_size_t),
                ('peak_process_memory', C.c_size_t), ('peak_job_memory', C.c_size_t)]


class _ProcessEntry(C.Structure):
    _fields_ = [('size', _DWORD), ('usage', _DWORD), ('pid', _DWORD), ('heap', C.c_size_t),
                ('module', _DWORD), ('threads', _DWORD), ('parent_pid', _DWORD),
                ('priority', C.c_int32), ('flags', _DWORD), ('name', C.c_uint16 * 260)]


def _error(operation):
    return AccountingError(f'{operation} failed (Windows error {getattr(C, "get_last_error", lambda: 0)()})')


def _api(injected=None):
    if injected is None:
        if sys.platform != 'win32':
            raise AccountingError('Windows native Job accounting is required')
        injected = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([C.c_void_p, C.c_wchar_p], _HANDLE),
        'SetInformationJobObject': ([_HANDLE, C.c_int32, C.c_void_p, _DWORD], _BOOL),
        'QueryInformationJobObject': ([_HANDLE, C.c_int32, C.c_void_p, _DWORD, C.POINTER(_DWORD)], _BOOL),
        'AssignProcessToJobObject': ([_HANDLE, _HANDLE], _BOOL),
        'TerminateJobObject': ([_HANDLE, _DWORD], _BOOL),
        'IsProcessInJob': ([_HANDLE, _HANDLE, C.POINTER(_BOOL)], _BOOL),
        'GetCurrentProcess': ([], _HANDLE), 'GetProcessId': ([_HANDLE], _DWORD),
        'GetProcessTimes': ([_HANDLE] + [C.POINTER(_FileTime)] * 4, _BOOL),
        'GetSystemTimes': ([C.POINTER(_FileTime)] * 3, _BOOL),
        'WaitForSingleObject': ([_HANDLE, _DWORD], _DWORD),
        'CreateToolhelp32Snapshot': ([_DWORD, _DWORD], _HANDLE),
        'Process32FirstW': ([_HANDLE, C.POINTER(_ProcessEntry)], _BOOL),
        'Process32NextW': ([_HANDLE, C.POINTER(_ProcessEntry)], _BOOL),
        'CloseHandle': ([_HANDLE], _BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(injected, name)
        function.argtypes, function.restype = arguments, result
    return injected


def _ticks(value):
    result = int(value.low) | int(value.high) << 32
    if result > (1 << 63) - 1:
        raise AccountingError('invalid signed-range native time counter')
    return result


def _bracket(start, finish):
    if not all(type(value) in (int, float) and math.isfinite(value) for value in (start, finish)) or finish < start:
        raise AccountingError('native read clock regressed')
    return {'read_started_monotonic': start, 'read_finished_monotonic': finish}


def _process_cpu(api, handle, pid, clock):
    started = clock()
    if not handle or api.GetProcessId(handle) != pid:
        raise AccountingError('process handle does not identify the requested PID')
    creation, exited, kernel, user = (_FileTime() for _ in range(4))
    if not api.GetProcessTimes(handle, C.byref(creation), C.byref(exited), C.byref(kernel), C.byref(user)):
        raise _error('GetProcessTimes')
    result = {'pid': pid, 'creation_ticks': _ticks(creation), 'kernel_ticks': _ticks(kernel), 'user_ticks': _ticks(user)}
    if result['creation_ticks'] <= 0:
        raise AccountingError('invalid process creation identity')
    result['cpu_ticks'] = result['kernel_ticks'] + result['user_ticks']
    result.update(_bracket(started, clock()))
    return result


def read_monitor_cpu(*, _kernel=None, _clock=time.monotonic):
    """All threads of this coordinator; pseudo handle is never closed."""
    api = _api(_kernel)
    return _process_cpu(api, api.GetCurrentProcess(), os.getpid(), _clock)


def read_system(*, _kernel=None, _clock=time.monotonic):
    api = _api(_kernel)
    started = _clock()
    idle, kernel, user = (_FileTime() for _ in range(3))
    if not api.GetSystemTimes(C.byref(idle), C.byref(kernel), C.byref(user)):
        raise _error('GetSystemTimes')
    result = {'idle_ticks': _ticks(idle), 'kernel_ticks': _ticks(kernel), 'user_ticks': _ticks(user)}
    if result['idle_ticks'] > result['kernel_ticks']:
        raise AccountingError('idle time exceeds kernel time')
    result['total_ticks'] = result['kernel_ticks'] + result['user_ticks']
    result['busy_ticks'] = result['total_ticks'] - result['idle_ticks']
    result.update(_bracket(started, _clock()))
    return result


def _parent(api, pid):
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, 0, C.c_void_p(-1).value):
        raise _error('CreateToolhelp32Snapshot')
    try:
        entry = _ProcessEntry()
        entry.size = C.sizeof(entry)
        success = api.Process32FirstW(snapshot, C.byref(entry))
        while success:
            if entry.pid == pid:
                return int(entry.parent_pid)
            success = api.Process32NextW(snapshot, C.byref(entry))
        raise AccountingError('new worker was not present in the process inventory')
    finally:
        if not api.CloseHandle(snapshot):
            raise _error('CloseHandle process inventory')


def _serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return invoke


class WindowsOwnedJob:
    """Kill-on-close Job for new gated direct children, not existing processes.

    Pre-assignment root CPU is recorded as a diagnostic interval only. Job CPU
    may exclude some startup CPU; no exact additive correction is assumed.
    TotalTerminatedProcesses counts limit violations, not all normal exits.
    """
    def __init__(self, *, _kernel=None, _clock=time.monotonic, _wall=time.time_ns):
        self._lock = threading.RLock()
        self.api, self.clock = _api(_kernel), _clock
        self.created_wall_ticks = _wall() // 100 + _FILETIME_EPOCH
        self.handle = self.api.CreateJobObjectW(None, None)
        self.assignments = []
        self._identities = set()
        self._previous = None
        self._pre_lower, self._pre_upper = 0, 0
        if not self.handle:
            raise _error('CreateJobObjectW')
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
                raise _error('SetInformationJobObject')
            if self.monitor_in_job():
                raise AccountingError('coordinator must remain outside the worker Job')
        except BaseException:
            self.close()
            raise

    @_serialized
    def monitor_in_job(self):
        self._require_open()
        included = _BOOL()
        if not self.api.IsProcessInJob(self.api.GetCurrentProcess(), self.handle, C.byref(included)):
            raise _error('IsProcessInJob coordinator')
        return bool(included.value)

    def _require_open(self):
        if self.handle is None:
            raise AccountingError('owned Job is closed')

    @_serialized
    def accounting(self):
        self._require_open()
        started = self.clock()
        if self.monitor_in_job():
            raise AccountingError('coordinator unexpectedly belongs to the worker Job')
        native, returned = _BasicAccounting(), _DWORD()
        if not self.api.QueryInformationJobObject(self.handle, 1, C.byref(native), C.sizeof(native), C.byref(returned)):
            raise _error('QueryInformationJobObject basic accounting')
        if returned.value != C.sizeof(native):
            raise AccountingError('Job accounting returned an unexpected structure size')
        if (native.TotalUserTime < 0 or native.TotalKernelTime < 0 or
                native.ActiveProcesses > native.TotalProcesses or native.TotalTerminatedProcesses > native.TotalProcesses):
            raise AccountingError('negative or inconsistent Job accounting counters')
        result = {'user_ticks': int(native.TotalUserTime), 'kernel_ticks': int(native.TotalKernelTime),
            'cpu_ticks': int(native.TotalUserTime + native.TotalKernelTime),
            'total_processes': int(native.TotalProcesses), 'active_processes': int(native.ActiveProcesses),
            'terminated_processes_by_limit': int(native.TotalTerminatedProcesses),
            'terminated_processes': int(native.TotalTerminatedProcesses),
            'terminated_count_semantics': 'Job-limit terminations only; not all normal process exits.',
            'coordinator_in_job': False, 'pre_assignment_cpu_lower_ticks_total': self._pre_lower,
            'pre_assignment_cpu_upper_ticks_total': self._pre_upper,
            'offset_semantics': 'Diagnostics only. Not automatically added to Job CPU.',
            **_bracket(started, self.clock())}
        if self._previous is not None and any(result[key] < self._previous[key] for key in
                ('user_ticks', 'kernel_ticks', 'total_processes', 'terminated_processes_by_limit')):
            raise AccountingError('cumulative Job accounting counter regressed')
        self._previous = dict(result)
        return result

    @_serialized
    def assign(self, process):
        """Runner must publish the startup gate only after this returns."""
        self._require_open()
        pid, handle = getattr(process, 'pid', None), getattr(process, '_handle', None)
        if type(pid) is not int or pid <= 0 or pid == os.getpid() or handle is None:
            raise AccountingError('a newly launched direct-worker Popen object is required')
        handle = int(handle)
        before = _process_cpu(self.api, handle, pid, self.clock)
        identity = pid, before['creation_ticks']
        if identity in self._identities:
            raise AccountingError('worker identity is already assigned')
        if before['creation_ticks'] < self.created_wall_ticks:
            raise AccountingError('worker predates this Job; existing processes cannot be assigned')
        if _parent(self.api, pid) != os.getpid():
            raise AccountingError('worker must be a direct child of this coordinator')
        if self.api.WaitForSingleObject(handle, 0) != 258:
            raise AccountingError('worker must remain alive and gated before assignment')
        job_before = self.accounting()
        if not self.api.AssignProcessToJobObject(self.handle, handle):
            raise _error('AssignProcessToJobObject')
        self._identities.add(identity)
        included = _BOOL()
        if not self.api.IsProcessInJob(handle, self.handle, C.byref(included)) or not included.value:
            raise AccountingError('worker Job membership could not be verified')
        after = _process_cpu(self.api, handle, pid, self.clock)
        if after['creation_ticks'] != before['creation_ticks'] or any(after[key] < before[key] for key in ('kernel_ticks', 'user_ticks')):
            raise AccountingError('worker creation/CPU identity changed across assignment')
        self._pre_lower += before['cpu_ticks']
        self._pre_upper += after['cpu_ticks']
        job_after = self.accounting()
        receipt = {'pid': pid, 'creation_ticks': before['creation_ticks'], 'parent_pid': os.getpid(),
            'process_before': before, 'process_after': after, 'job_before': job_before, 'job_after': job_after,
            'pre_assignment_cpu_lower_ticks': before['cpu_ticks'],
            'pre_assignment_cpu_upper_ticks': after['cpu_ticks'],
            'boundary_semantics': 'Popen/gate interval, not suspended startup. Prior root CPU and inclusion in Job totals are not assumed exact.'}
        self.assignments.append(receipt)
        return receipt

    @_serialized
    def close(self):
        if self.handle is not None:
            if not self.api.CloseHandle(self.handle):
                raise _error('CloseHandle owned Job')
            self.handle = None

    @_serialized
    def terminate(self, exit_code=1):
        """Terminate only this owned Job; keep its accounting handle open."""
        self._require_open()
        if type(exit_code) is not int or not 0 <= exit_code <= 0xffffffff:
            raise ValueError('exit_code must be an unsigned DWORD')
        if not self.api.TerminateJobObject(self.handle, exit_code):
            raise _error('TerminateJobObject')

    @_serialized
    def terminate_and_snapshot(self, timeout_seconds=5., poll_seconds=.05, *, _sleep=time.sleep):
        """Bounded exit wait; caller persists final CPU before calling close()."""
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (timeout_seconds, poll_seconds)) or poll_seconds > timeout_seconds:
            raise ValueError('positive finite bounded termination wait is required')
        started = self.clock()
        self.terminate()
        while True:
            result = self.accounting()
            if result['active_processes'] == 0:
                return dict(result, termination_wait_started_monotonic=started,
                            termination_wait_finished_monotonic=self.clock())
            remaining = timeout_seconds - (self.clock() - started)
            if remaining <= 0:
                raise AccountingError('owned Job did not reach zero active processes within the termination bound')
            _sleep(min(poll_seconds, remaining))


OwnedJob = WindowsOwnedJob
