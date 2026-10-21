"""Resolve only frozen native engines and the declared Sunfish venv launcher.

Toolhelp gives parent links; held query/wait handles establish exact creation
identities, liveness and executable paths. Two inventories reject observed tree
changes. This is sampled identity evidence, not an atomic whole-system view.
No process is terminated, configured, suspended or assigned by this module.

https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess
"""
from __future__ import annotations

import ctypes as C
import hashlib
import os
from pathlib import Path
import re
import sys
import time

from m2_job_accounting import AccountingError, _FileTime, _ProcessEntry, _ticks, _bracket

METHOD = 'frozen-image-and-held-creation-with-toolhelp-parent'


class EngineIdentityError(AccountingError):
    pass


def _last_error():
    return getattr(C, 'get_last_error', lambda: 0)()


def _error(operation):
    return EngineIdentityError(f'{operation} failed (Windows error {_last_error()})')


def _api(injected=None):
    if injected is None:
        if sys.platform != 'win32':
            raise EngineIdentityError('Windows engine identity resolution is required')
        injected = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([C.c_uint32, C.c_int32, C.c_uint32], C.c_void_p),
        'GetProcessId': ([C.c_void_p], C.c_uint32),
        'GetProcessTimes': ([C.c_void_p] + [C.POINTER(_FileTime)] * 4, C.c_int32),
        'GetExitCodeProcess': ([C.c_void_p, C.POINTER(C.c_uint32)], C.c_int32),
        'WaitForSingleObject': ([C.c_void_p, C.c_uint32], C.c_uint32),
        'QueryFullProcessImageNameW': ([C.c_void_p, C.c_uint32, C.POINTER(C.c_wchar),
                                        C.POINTER(C.c_uint32)], C.c_int32),
        'CreateToolhelp32Snapshot': ([C.c_uint32, C.c_uint32], C.c_void_p),
        'Process32FirstW': ([C.c_void_p, C.POINTER(_ProcessEntry)], C.c_int32),
        'Process32NextW': ([C.c_void_p, C.POINTER(_ProcessEntry)], C.c_int32),
        'CloseHandle': ([C.c_void_p], C.c_int32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(injected, name)
        function.argtypes, function.restype = arguments, result
    return injected


def _inventory(api):
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle in (None, 0, C.c_void_p(-1).value):
        raise _error('CreateToolhelp32Snapshot engine identity')
    rows = {}
    try:
        entry = _ProcessEntry()
        entry.size = C.sizeof(entry)
        success = api.Process32FirstW(handle, C.byref(entry))
        while success:
            pid = int(entry.pid)
            if pid in rows:
                raise EngineIdentityError('duplicate PID in process inventory')
            rows[pid] = {'pid': pid, 'parent_pid': int(entry.parent_pid)}
            success = api.Process32NextW(handle, C.byref(entry))
        if _last_error() != 18:
            raise _error('Process32NextW engine inventory did not complete')
        return rows
    finally:
        if not api.CloseHandle(handle):
            raise _error('CloseHandle engine inventory')


def _path(value, label):
    if not isinstance(value, str) or not value or any(c in value for c in '\r\n\0'):
        raise EngineIdentityError(label + ' path is missing or malformed')
    raw = Path(value)
    if not raw.is_absolute():
        raise EngineIdentityError(label + ' must use an absolute frozen path')
    try:
        return raw.resolve(strict=True)
    except OSError as error:
        raise EngineIdentityError(label + ' path is not accessible') from error


def _equal_path(left, right):
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _file(record, label):
    if not isinstance(record, dict):
        raise EngineIdentityError(label + ' frozen file record is missing')
    path = _path(record.get('path'), label)
    digest = record.get('sha256')
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-fA-F]{64}', digest) is None:
        raise EngineIdentityError(label + ' SHA256 is malformed')
    try:
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError as error:
        raise EngineIdentityError(label + ' file cannot be hashed') from error
    if actual != digest.lower():
        raise EngineIdentityError(label + ' frozen SHA256 differs')
    return {'path': str(path), 'sha256': actual}


def _profile(profile, expected_base_runtime):
    if not isinstance(profile, dict):
        raise EngineIdentityError('a loaded frozen engine profile is required')
    binary = _file(profile.get('binary'), 'profile binary')
    name = Path(binary['path']).name.casefold()
    source = profile.get('source', {})
    if not isinstance(source, dict):
        raise EngineIdentityError('frozen engine source metadata is malformed')
    args = profile.get('args')
    if (profile.get('kind') == 'regicide' and name.startswith('regicide')
            and name.endswith('.exe') and args == ['uci']):
        return 'native-direct', binary, None, None
    if (profile.get('kind') == 'external' and source.get('name', '').casefold() == 'stockfish'
            and name.startswith('stockfish') and name.endswith('.exe') and args == []):
        return 'native-direct', binary, None, None
    if not (profile.get('kind') == 'external' and source.get('name') == 'Sunfish'
            and source.get('source_url') == 'https://github.com/thomasahle/sunfish'
            and name == 'python.exe' and isinstance(args, list) and len(args) == 2 and args[0] == '-u'):
        raise EngineIdentityError('profile is outside the declared native/Sunfish endpoint scope')
    script = _path(args[1], 'Sunfish script argument')
    artifacts = profile.get('artifacts')
    if not isinstance(artifacts, list):
        raise EngineIdentityError('Sunfish frozen source artifacts are missing')
    bindings = [item for item in artifacts if isinstance(item, dict) and item.get('role') == 'engine-source'
                and isinstance(item.get('path'), str)
                and _equal_path(_path(item['path'], 'Sunfish source artifact'), script)]
    if script.name != 'sunfish.py' or len(bindings) != 1:
        raise EngineIdentityError('Sunfish script must bind exactly one frozen engine-source artifact')
    frozen_script = _file(bindings[0], 'Sunfish script')
    runtime = _file(expected_base_runtime, 'Sunfish expected base runtime')
    if Path(runtime['path']).name.casefold() != 'python.exe' or _equal_path(
            Path(runtime['path']), Path(binary['path'])):
        raise EngineIdentityError('Sunfish child must use the separately frozen base Python executable')
    return 'sunfish-venv-child', binary, frozen_script, runtime


def _read_live(api, handle, pid, expected_creation, expected_image, clock):
    started = clock()
    if api.GetProcessId(handle) != pid:
        raise EngineIdentityError('held process handle PID differs')
    creation, exited, kernel, user = (_FileTime() for _ in range(4))
    if not api.GetProcessTimes(handle, C.byref(creation), C.byref(exited), C.byref(kernel), C.byref(user)):
        raise _error('GetProcessTimes engine identity')
    created = _ticks(creation)
    status = C.c_uint32()
    if not api.GetExitCodeProcess(handle, C.byref(status)):
        raise _error('GetExitCodeProcess engine identity')
    if created <= 0 or expected_creation is not None and created != expected_creation:
        raise EngineIdentityError('process creation identity differs')
    # A live process's GetProcessTimes exit field is undefined, and its exit
    # code 259 can also be a real exit code. Only a nonsignaled process object
    # is liveness evidence. SYNCHRONIZE adds wait access, no mutation rights.
    wait_status = api.WaitForSingleObject(handle, 0)
    if wait_status == 0:
        raise EngineIdentityError('engine identity process has exited')
    if wait_status != 258:
        raise _error('WaitForSingleObject engine identity')
    buffer = C.create_unicode_buffer(32768)
    length = C.c_uint32(len(buffer))
    if not api.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(length)):
        raise _error('QueryFullProcessImageNameW engine identity')
    if not 0 < length.value < len(buffer):
        raise EngineIdentityError('native image path length is invalid')
    path = _path(buffer.value, 'native process image')
    allowed = expected_image if isinstance(expected_image, list) else [expected_image]
    matched = next((item for item in allowed if _equal_path(path, Path(item['path']))), None)
    if matched is None:
        raise EngineIdentityError('native process image path differs from its frozen executable')
    return {'pid': pid, 'creation_ticks': created, 'image_path': str(path),
            'image_sha256': matched['sha256'], 'exit_code_diagnostic': int(status.value),
            'wait_status': int(wait_status), 'liveness_method': 'nonsignaled-held-process-object',
            **_bracket(started, clock())}


def resolve_engine_endpoint(transport_pid, transport_creation, profile, expected_base_runtime,
                            *, expected_console_helper=None, _kernel=None, _clock=time.monotonic):
    """Return a serializable exact endpoint receipt; close only our read handles.

    Callers must immediately open their CPU meter with endpoint pid AND creation
    ticks. A reused PID between this function and meter creation then fails.
    Sunfish allows one direct base-Python child or its measured child-free direct
    variant. Only an explicitly frozen console helper may be excluded, at most
    once; every observed computational endpoint/helper must have no children.
    """
    if type(transport_pid) is not int or not 0 < transport_pid <= 0xffffffff:
        raise EngineIdentityError('transport PID must be a positive Windows DWORD')
    if type(transport_creation) is not int or transport_creation <= 0:
        raise EngineIdentityError('transport creation ticks must be a positive integer')
    mode, binary, script, runtime = _profile(profile, expected_base_runtime)
    helper_file = None if expected_console_helper is None else _file(expected_console_helper, 'expected console helper')
    if helper_file is not None and Path(helper_file['path']).name.casefold() != 'conhost.exe':
        raise EngineIdentityError('only an explicitly frozen conhost.exe helper is allowed')
    api = _api(_kernel)
    started = _clock()
    handles = []
    try:
        first = _inventory(api)
        if transport_pid not in first:
            raise EngineIdentityError('transport is missing from the process inventory')
        transport_handle = api.OpenProcess(0x101000, False, transport_pid)
        if not transport_handle:
            raise _error('OpenProcess query/wait-only transport identity')
        handles.append(transport_handle)
        transport = _read_live(api, transport_handle, transport_pid, transport_creation, binary, _clock)
        transport['parent_pid'] = first[transport_pid]['parent_pid']
        children = sorted(pid for pid, row in first.items() if row['parent_pid'] == transport_pid)
        endpoint_handle = transport_handle
        endpoint = dict(transport)
        child_endpoints, helpers = [], []
        for child_pid in children:
            if child_pid == transport_pid or any(row['parent_pid'] == child_pid for row in first.values()):
                raise EngineIdentityError('engine child has an unexpected descendant process')
            child_handle = api.OpenProcess(0x101000, False, child_pid)
            if not child_handle:
                raise _error('OpenProcess query/wait-only engine child identity')
            handles.append(child_handle)
            allowed = [item for item in (runtime, helper_file) if item is not None]
            child = _read_live(api, child_handle, child_pid, None, allowed, _clock)
            child['parent_pid'] = transport_pid
            if child['creation_ticks'] < transport_creation:
                raise EngineIdentityError('engine child predates its exact transport identity')
            if helper_file is not None and _equal_path(Path(child['image_path']), Path(helper_file['path'])):
                helpers.append((child, child_handle))
            elif runtime is not None and _equal_path(Path(child['image_path']), Path(runtime['path'])):
                child_endpoints.append((child, child_handle))
            else:
                raise EngineIdentityError('engine child is outside the declared endpoint/helper scope')
        if len(helpers) > 1:
            raise EngineIdentityError('engine has multiple console helpers')
        if mode == 'native-direct' and child_endpoints:
            raise EngineIdentityError('native engine has an unexpected computational child process')
        if mode == 'sunfish-venv-child':
            if len(child_endpoints) > 1:
                raise EngineIdentityError('Sunfish has multiple base-Python endpoints')
            if child_endpoints:
                endpoint, endpoint_handle = child_endpoints[0]
            else:
                mode = 'native-direct'
        second = _inventory(api)
        relevant = {transport_pid, endpoint['pid'], *(row['pid'] for row, _ in helpers)}
        before = {pid: row for pid, row in first.items() if pid in relevant or row['parent_pid'] in relevant}
        after = {pid: row for pid, row in second.items() if pid in relevant or row['parent_pid'] in relevant}
        if before != after:
            raise EngineIdentityError('engine parent/child inventory changed during endpoint resolution')
        transport_after = _read_live(api, transport_handle, transport_pid, transport_creation, binary, _clock)
        endpoint_after = (dict(transport_after) if mode == 'native-direct' else
            _read_live(api, endpoint_handle, endpoint['pid'], endpoint['creation_ticks'], runtime, _clock))
        helpers_after = [_read_live(api, handle, row['pid'], row['creation_ticks'], helper_file, _clock)
                         for row, handle in helpers]
        return {'schema_version': 1, 'method': METHOD, 'profile_id': profile.get('id'), 'mode': mode,
            'transport': transport, 'endpoint': endpoint, 'transport_after': transport_after,
            'endpoint_after': endpoint_after, 'frozen_binary': binary, 'frozen_script': script,
            'expected_base_runtime': runtime, 'observed_direct_children': children,
            'expected_console_helper': helper_file,
            'console_helpers': [row for row, _ in helpers], 'console_helpers_after': helpers_after,
            'matching_inventories': [before, after], 'query_access_mask': 0x101000,
            'allowed_scope': (('Frozen Sunfish -u script direct variant using the frozen profile image itself.'
                if script is not None else 'Frozen native profile image itself.') if mode == 'native-direct' else
                'Only the frozen Sunfish -u script venv launcher and its unique direct frozen base-Python child; wrapper startup CPU excluded from endpoint CPU and retained in owned Job CPU.')
                + ' At most one explicitly frozen conhost helper; helper CPU excluded from endpoint CPU and retained in owned Job CPU. No other observed descendants.',
            'scope': 'Sampled exact image/creation/parent proof; held during resolution. CPU meter must recheck pid and creation. No whole-system or future-descendant guarantee.',
            **_bracket(started, _clock())}
    finally:
        close_failed = False
        for handle in reversed(handles):
            if not api.CloseHandle(handle):
                close_failed = True
        if close_failed:
            raise _error('CloseHandle engine identity query')
