"""Native endpoint identity probe, never a formal game or load-certification run."""
from __future__ import annotations

import ctypes as C
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT/'scripts'))
import chess
import chess.engine

from engine_profile import load_profile
from m2_engine_cpu import EngineCpuMeter
from m2_engine_identity import resolve_engine_endpoint, _api, _inventory


class FT(C.Structure):
    _fields_ = [('low', C.c_uint32), ('high', C.c_uint32)]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def independent_api():
    api = C.WinDLL('kernel32', use_last_error=True)
    functions = {
        'OpenProcess': ([C.c_uint32, C.c_int32, C.c_uint32], C.c_void_p),
        'GetProcessId': ([C.c_void_p], C.c_uint32),
        'GetProcessTimes': ([C.c_void_p] + [C.POINTER(FT)]*4, C.c_int32),
        'GetExitCodeProcess': ([C.c_void_p, C.POINTER(C.c_uint32)], C.c_int32),
        'WaitForSingleObject': ([C.c_void_p, C.c_uint32], C.c_uint32),
        'CloseHandle': ([C.c_void_p], C.c_int32),
    }
    for name, (arguments, result) in functions.items():
        getattr(api, name).argtypes, getattr(api, name).restype = arguments, result
    return api


def raw_cpu(api, handle, pid):
    started = time.monotonic()
    if api.GetProcessId(handle) != pid:
        raise RuntimeError('independent held handle PID differs')
    created, exited, kernel, user = (FT() for _ in range(4))
    if not api.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
        raise C.WinError(C.get_last_error())
    ticks = lambda item: int(item.low) | int(item.high) << 32
    code = C.c_uint32()
    if not api.GetExitCodeProcess(handle, C.byref(code)):
        raise C.WinError(C.get_last_error())
    state = api.WaitForSingleObject(handle, 0)
    if state not in (0, 258):
        raise C.WinError(C.get_last_error())
    return dict(pid=pid, creation_ticks=ticks(created), kernel_ticks=ticks(kernel), user_ticks=ticks(user),
        cpu_ticks=ticks(kernel)+ticks(user), exit_code_diagnostic=code.value, wait_status=state,
        read_started_monotonic=started, read_finished_monotonic=time.monotonic())


def main():
    target = Path(__file__).parent
    receipt_path = target/'receipt-final.json'
    if receipt_path.exists():
        raise FileExistsError('never overwrite a native probe receipt')
    plan_path = ROOT/'docs/experiments/2026-10-21-m2-engine-cpu-plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8-sig'))
    expected_runtime = plan['worker_runtime']['executable']
    sources = [Path(__file__), ROOT/'scripts/m2_engine_identity.py', ROOT/'scripts/m2_engine_cpu.py',
               ROOT/'scripts/m2_job_accounting.py', ROOT/'scripts/engine_profile.py']
    report = dict(schema_version=1, started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        status='RUNNING', formal_games=0, formal_load_validation=False, playing_strength_claim=False,
        scope='Identity and one 100ms Sunfish functional CPU probe only; no load isolation or formal games.',
        source_sha256={str(path.relative_to(ROOT)): digest(path) for path in sources},
        plan_reference=dict(path=str(plan_path), sha256=digest(plan_path)), cases=[])
    api = independent_api()
    cases = [('sunfish', ROOT/'build-m2-opponents/profiles/sunfish.json', True),
             ('regicide', ROOT/'config/engine-profiles/classical.json', False),
             ('stockfish', ROOT/'build-m2-opponents/profiles/stockfish18.json', False)]
    try:
        for label, profile_path, play_once in cases:
            entry = dict(label=label, status='RUNNING', profile_path=str(profile_path),
                         profile_sha256=digest(profile_path), cleanup=[])
            report['cases'].append(entry)
            engine = wrapper_meter = endpoint_meter = None
            independent_handles = {}
            tracked = {}
            try:
                profile = load_profile(profile_path)
                command = [profile['binary']['path'], *profile['args']]
                entry['literal_command'] = command
                entry['configured_options'] = {}
                engine = chess.engine.SimpleEngine.popen_uci(command, timeout=10.)
                entry['uci_id'] = engine.id
                pid = engine.transport.get_pid()
                wrapper_meter = EngineCpuMeter(pid)
                creation = wrapper_meter.initial_read['creation_ticks']
                tracked[pid] = creation
                resolution = resolve_engine_endpoint(pid, creation, profile, expected_runtime)
                entry['resolution'] = resolution
                endpoint = resolution['endpoint']
                tracked[endpoint['pid']] = endpoint['creation_ticks']
                for tracked_pid in tracked:
                    handle = api.OpenProcess(0x101000, False, tracked_pid)
                    if not handle:
                        raise C.WinError(C.get_last_error())
                    independent_handles[tracked_pid] = handle
                initial = raw_cpu(api, independent_handles[endpoint['pid']], endpoint['pid'])
                if initial['creation_ticks'] != endpoint['creation_ticks'] or initial['wait_status'] != 258:
                    raise RuntimeError('independent endpoint identity/liveness differs')
                endpoint_meter = EngineCpuMeter(endpoint['pid'], endpoint['creation_ticks'])
                entry['independent_initial'] = initial
                entry['meter_initial'] = endpoint_meter.initial_read
                entry['independent_cpu_method'] = 'Separate ctypes FT structure/GetProcessTimes calls on held query+wait handles.'
                if play_once:
                    board = chess.Board()
                    before = raw_cpu(api, independent_handles[endpoint['pid']], endpoint['pid'])
                    wrapper_before = wrapper_meter.read()
                    result, cpu_receipt = endpoint_meter.measure_play(engine.play, board, chess.engine.Limit(time=.1))
                    wrapper_after = wrapper_meter.read()
                    after = raw_cpu(api, independent_handles[endpoint['pid']], endpoint['pid'])
                    if result.move is None or result.move not in board.legal_moves:
                        raise RuntimeError('probe engine did not return a legal move')
                    for key in ('user_ticks', 'kernel_ticks', 'cpu_ticks'):
                        values = [before[key], cpu_receipt['before'][key], cpu_receipt['after'][key], after[key]]
                        if values != sorted(values):
                            raise RuntimeError('independent ordered CPU counter reconciliation failed')
                    entry['play'] = dict(position=chess.STARTING_FEN, requested_movetime_ms=100,
                        returned_move=result.move.uci(), legal=True, cpu_receipt=cpu_receipt,
                        independent_before=before, independent_after=after,
                        independent_ordered_counter_reconciliation=True,
                        wrapper_before=wrapper_before, wrapper_after=wrapper_after,
                        wrapper_cpu_delta_ticks=wrapper_after['cpu_ticks']-wrapper_before['cpu_ticks'])
                entry['status'] = 'PASS'
            except BaseException as error:
                entry['status'] = 'FAIL'
                entry['error'] = dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
                raise
            finally:
                if engine is not None:
                    try:
                        engine.quit()
                        entry['graceful_quit'] = True
                    except BaseException as error:
                        entry['graceful_quit'] = False
                        entry['quit_error'] = dict(type=type(error).__name__, message=str(error))
                        # python-chess owns this transport, created in this case.
                        # No unrelated numeric PID is ever sent to a kill API.
                        engine.close()
                for meter in (endpoint_meter, wrapper_meter):
                    if meter is not None:
                        meter.close()
                for tracked_pid, handle in independent_handles.items():
                    try:
                        final = raw_cpu(api, handle, tracked_pid)
                        if final['creation_ticks'] != tracked[tracked_pid] or final['wait_status'] != 0:
                            raise RuntimeError('owned held process did not exit with its exact creation identity')
                        entry['cleanup'].append(dict(pid=tracked_pid, creation_ticks=tracked[tracked_pid],
                            status='exact-held-process-exited', final=final))
                    finally:
                        if not api.CloseHandle(handle):
                            raise C.WinError(C.get_last_error())
                # Reopen only tracked numeric PIDs for absence/reuse evidence.
                # Never terminate or alter any process returned by this check.
                for tracked_pid, expected_creation in tracked.items():
                    handle = api.OpenProcess(0x101000, False, tracked_pid)
                    if not handle:
                        code = C.get_last_error()
                        if code != 87:
                            raise RuntimeError(f'cleanup PID absence could not be established: {code}')
                        entry['cleanup'].append(dict(pid=tracked_pid, creation_ticks=expected_creation, status='absent'))
                        continue
                    try:
                        row = raw_cpu(api, handle, tracked_pid)
                        if row['creation_ticks'] != expected_creation:
                            status = 'reused-by-different-creation'
                        elif row['wait_status'] == 0:
                            status = 'exact-process-exited-object-still-open'
                        else:
                            raise RuntimeError('tracked exact process is still alive after graceful quit')
                        entry['cleanup'].append(dict(pid=tracked_pid, creation_ticks=expected_creation,
                            status=status, observed=row))
                    finally:
                        if not api.CloseHandle(handle):
                            raise C.WinError(C.get_last_error())
            final_inventory = _inventory(_api())
            entry['post_quit_inventory'] = {pid: ('absent' if pid not in final_inventory else 'present-after-proven-exit-or-reuse') for pid in tracked}
        report['status'] = 'PASS'
    except BaseException as error:
        report['status'] = 'FAIL'
        report['error'] = dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
    finally:
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with receipt_path.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    print(json.dumps(dict(status=report['status'], cases=[dict(label=row['label'], status=row['status'])
        for row in report['cases']], receipt=str(receipt_path))))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
