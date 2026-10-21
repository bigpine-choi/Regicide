"""Read-only Sunfish inventory follow-up; preserves the first failed probe."""
import ctypes as C
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

from probe import ROOT, digest, independent_api, raw_cpu
import chess
import chess.engine
from engine_profile import load_profile
from m2_engine_cpu import EngineCpuMeter
from m2_engine_identity import _api, _inventory


def main():
    output = Path(__file__).with_name('receipt-inventory.json')
    if output.exists():
        raise FileExistsError('never overwrite an existing probe')
    report = dict(schema_version=1, status='RUNNING', formal_games=0, formal_load_validation=False,
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        source_sha256={str(path.relative_to(ROOT)): digest(path) for path in
            (Path(__file__), Path(__file__).with_name('probe.py'), ROOT/'scripts/m2_engine_identity.py',
             ROOT/'scripts/m2_engine_cpu.py')}, cleanup=[])
    engine = meter = None
    handle = None
    api = independent_api()
    try:
        profile = load_profile(ROOT/'build-m2-opponents/profiles/sunfish.json')
        command = [profile['binary']['path'], *profile['args']]
        report.update(literal_command=command, frozen_binary=profile['binary'])
        engine = chess.engine.SimpleEngine.popen_uci(command, timeout=10., creationflags=subprocess.CREATE_NO_WINDOW)
        pid = engine.transport.get_pid()
        report.update(transport_pid=pid, uci_id=engine.id)
        meter = EngineCpuMeter(pid)
        handle = api.OpenProcess(0x101000, False, pid)
        if not handle:
            raise C.WinError(C.get_last_error())
        creation = meter.initial_read['creation_ticks']
        report['creation_ticks'] = creation
        named_api = _api()
        buffer = C.create_unicode_buffer(32768)
        size = C.c_uint32(len(buffer))
        if not named_api.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(size)):
            raise C.WinError(C.get_last_error())
        image = str(Path(buffer.value).resolve(strict=True))
        report.update(image_path=image, image_sha256=digest(image))
        if Path(image) != Path(profile['binary']['path']) or digest(image) != profile['binary']['sha256']:
            raise RuntimeError('transport image does not match frozen Sunfish executable')
        rows = _inventory(named_api)
        related = {p: row for p, row in rows.items() if p == pid or row['parent_pid'] == pid}
        report.update(completed_inventory_process_count=len(rows), related_inventory_before=related)
        children = [p for p, row in rows.items() if row['parent_pid'] == pid]
        report['direct_children'] = children
        if children:
            raise RuntimeError('this follow-up validates only a measured zero-child transport variant')
        before = raw_cpu(api, handle, pid)
        result, cpu = meter.measure_play(engine.play, chess.Board(), chess.engine.Limit(time=.1))
        after = raw_cpu(api, handle, pid)
        if result.move not in chess.Board().legal_moves:
            raise RuntimeError('probe returned an illegal move')
        for key in ('kernel_ticks', 'user_ticks', 'cpu_ticks'):
            values = [before[key], cpu['before'][key], cpu['after'][key], after[key]]
            if values != sorted(values):
                raise RuntimeError('independent CPU reconciliation failed')
        later = _inventory(named_api)
        after_related = {p: row for p, row in later.items() if p == pid or row['parent_pid'] == pid}
        report.update(related_inventory_after=after_related,
            play=dict(requested_movetime_ms=100, returned_move=result.move.uci(), legal=True,
                cpu_receipt=cpu, independent_before=before, independent_after=after,
                independent_ordered_counter_reconciliation=True))
        if related != after_related:
            raise RuntimeError('Sunfish observed child tree changed during the functional probe')
        report['status'] = 'PASS'
    except BaseException as error:
        report.update(status='FAIL', error=dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc()))
    finally:
        try:
            if engine is not None:
                engine.quit()
                report['graceful_quit'] = True
            if meter is not None:
                meter.close()
            if handle is not None:
                final = raw_cpu(api, handle, report['transport_pid'])
                if final['wait_status'] != 0 or final['creation_ticks'] != report['creation_ticks']:
                    raise RuntimeError('held exact Sunfish process did not exit')
                report['cleanup'].append(dict(status='exact-held-process-exited', final=final))
                if not api.CloseHandle(handle):
                    raise C.WinError(C.get_last_error())
                handle = None
                current = api.OpenProcess(0x101000, False, report['transport_pid'])
                if not current:
                    code = C.get_last_error()
                    if code != 87:
                        raise RuntimeError('cleanup PID status unknown')
                    report['cleanup'].append(dict(status='absent', pid=report['transport_pid']))
                else:
                    try:
                        final = raw_cpu(api, current, report['transport_pid'])
                        if final['creation_ticks'] != report['creation_ticks']:
                            status = 'reused-by-different-creation'
                        elif final['wait_status'] == 0:
                            status = 'exact-process-exited-object-still-open'
                        else:
                            raise RuntimeError('own exact process remains alive')
                        report['cleanup'].append(dict(status=status, final=final))
                    finally:
                        api.CloseHandle(current)
        except BaseException as error:
            report.update(status='FAIL', cleanup_error=dict(type=type(error).__name__, message=str(error)))
            if engine is not None:
                engine.close()
        finally:
            if handle is not None:
                api.CloseHandle(handle)
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with output.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
    print(json.dumps(dict(status=report['status'], receipt=str(output))))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
