"""Inventory the owned Sunfish children only; default formal creation flags; no move search is performed."""
import ctypes as C
import datetime
import json
from pathlib import Path
import subprocess
import time
import traceback

from probe import ROOT, digest, independent_api, raw_cpu
import chess.engine
from engine_profile import load_profile
from m2_engine_identity import _api, _inventory


def main():
    output = Path(__file__).with_name('receipt-children-default.json')
    if output.exists():
        raise FileExistsError('never overwrite native evidence')
    report = dict(schema_version=1, status='RUNNING', formal_games=0, formal_load_validation=False,
        move_searches=0, started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        source_sha256={str(path.relative_to(ROOT)): digest(path) for path in
            (Path(__file__), Path(__file__).with_name('probe.py'), ROOT/'scripts/m2_engine_identity.py')},
        processes=[], cleanup=[])
    engine = None
    handles = {}
    api, image_api = independent_api(), _api()
    try:
        profile = load_profile(ROOT/'build-m2-opponents/profiles/sunfish.json')
        command = [profile['binary']['path'], *profile['args']]
        report.update(literal_command=command, frozen_binary=profile['binary'])
        engine = chess.engine.SimpleEngine.popen_uci(command, timeout=10.)
        transport = engine.transport.get_pid()
        rows = _inventory(image_api)
        children = sorted(pid for pid, row in rows.items() if row['parent_pid'] == transport)
        report.update(transport_pid=transport, uci_id=engine.id, direct_children=children,
                      inventory={pid: row for pid, row in rows.items() if pid == transport or pid in children})
        for pid in [transport, *children]:
            handle = api.OpenProcess(0x101000, False, pid)
            if not handle:
                raise C.WinError(C.get_last_error())
            handles[pid] = handle
            cpu = raw_cpu(api, handle, pid)
            image = C.create_unicode_buffer(32768)
            size = C.c_uint32(len(image))
            if not image_api.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)):
                raise C.WinError(C.get_last_error())
            path = str(Path(image.value).resolve(strict=True))
            report['processes'].append(dict(pid=pid, parent_pid=rows[pid]['parent_pid'],
                creation_ticks=cpu['creation_ticks'], image_path=path, image_sha256=digest(path), raw=cpu))
        transport_creation = report['processes'][0]['creation_ticks']
        if any(row['creation_ticks'] < transport_creation or row['raw']['wait_status'] != 258
               for row in report['processes']):
            raise RuntimeError('observed direct child is not alive or predates the exact parent')
        report['status'] = 'PASS'
    except BaseException as error:
        report.update(status='FAIL', error=dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc()))
    finally:
        try:
            if engine is not None:
                engine.quit()
                report['graceful_quit'] = True
            for pid, handle in handles.items():
                started = time.monotonic()
                while True:
                    row = raw_cpu(api, handle, pid)
                    if row['wait_status'] == 0:
                        break
                    if time.monotonic()-started > 2:
                        raise RuntimeError('owned tracked process remains alive after quit')
                    time.sleep(.02)
                report['cleanup'].append(dict(pid=pid, status='exact-held-process-exited', final=row))
        except BaseException as error:
            report.update(status='FAIL', cleanup_error=dict(type=type(error).__name__, message=str(error)))
            if engine is not None:
                engine.close()
        finally:
            for handle in handles.values():
                if not api.CloseHandle(handle):
                    report['status'] = 'FAIL'
                    report['close_error'] = C.get_last_error()
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with output.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
    print(json.dumps(dict(status=report['status'], processes=[dict(pid=row['pid'], image=row['image_path'])
        for row in report['processes']], receipt=str(output))))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
