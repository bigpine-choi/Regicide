"""Read current stale parent links and preserved failure receipts; no mutations."""
import ctypes as C
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT/'scripts'))
from m2_engine_identity import _api, _inventory
from m2_job_accounting import _FileTime, _ticks


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cim():
    command = ("Get-CimInstance Win32_Process -Filter 'ParentProcessId = 1016 OR ProcessId = 1016' | "
               "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate | ConvertTo-Json -Depth 3")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    rows = json.loads(result.stdout.decode('utf-8-sig'))
    return rows if isinstance(rows, list) else [rows]


def main():
    output = Path(__file__).with_name('receipt.json')
    if output.exists():
        raise FileExistsError('preserve existing read-only evidence')
    source = ROOT/'build-m2-engine-cpu/attempt-001'
    cell = source/'main/cells/main-0040'
    games = json.loads((cell/'games.json').read_text())
    frozen = json.loads((source/'frozen-plan.json').read_text())
    report = json.loads((source/'main/report.json').read_text())
    candidate = games['affinity']['transports'][1]
    assert candidate['pid'] == 1016
    candidate_utc = datetime.datetime.fromtimestamp(candidate['creation_ticks']/10_000_000-11644473600,
                                                   datetime.timezone.utc)
    api = _api()
    api.GetSecurityInfo = None  # Advapi32 is separate; never invoke a mutation API.
    advapi = C.WinDLL('advapi32', use_last_error=True)
    advapi.GetSecurityInfo.argtypes = [C.c_void_p, C.c_int, C.c_uint32] + [C.POINTER(C.c_void_p)]*5
    advapi.GetSecurityInfo.restype = C.c_uint32
    api.LocalFree.argtypes, api.LocalFree.restype = [C.c_void_p], C.c_void_p
    first = _inventory(api)
    targets = sorted({1016, *(pid for pid, row in first.items() if row['parent_pid'] == 1016)})
    native = []
    for pid in targets:
        item = dict(pid=pid, parent_pid=first.get(pid, {}).get('parent_pid'), access=[])
        for mask, label in ((0x1000, 'query-limited'), (0x101000, 'query-limited-plus-synchronize'),
                            (0x20000, 'read-control-only'), (0x21000, 'query-limited-plus-read-control')):
            handle = api.OpenProcess(mask, False, pid)
            evidence = dict(access_mask=mask, access_label=label, opened=bool(handle))
            item['access'].append(evidence)
            if not handle:
                evidence['windows_error'] = C.get_last_error()
                continue
            try:
                evidence['held_pid'] = api.GetProcessId(handle)
                created, exited, kernel, user = (_FileTime() for _ in range(4))
                if mask & 0x1000:
                    if api.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
                        evidence['creation_ticks'] = _ticks(created)
                    else:
                        evidence['times_error'] = C.get_last_error()
                    image = C.create_unicode_buffer(32768)
                    length = C.c_uint32(len(image))
                    if api.QueryFullProcessImageNameW(handle, 0, image, C.byref(length)):
                        evidence['image_path'] = image.value
                    else:
                        evidence['image_error'] = C.get_last_error()
                if mask & 0x20000:
                    owner, group, dacl, sacl, descriptor = (C.c_void_p() for _ in range(5))
                    error = advapi.GetSecurityInfo(handle, 6, 4, C.byref(owner), C.byref(group),
                        C.byref(dacl), C.byref(sacl), C.byref(descriptor))
                    evidence['get_security_info_dacl_error'] = int(error)
                    evidence['dacl_readable'] = error == 0
                    if descriptor.value:
                        api.LocalFree(descriptor)
            finally:
                if not api.CloseHandle(handle):
                    raise C.WinError(C.get_last_error())
        native.append(item)
    current_cim = cim()
    second = _inventory(api)
    child_records = []
    for row in current_cim:
        created = datetime.datetime.fromisoformat(row['CreationDate']) if row.get('CreationDate') else None
        child_records.append(dict(**row, predates_failed_candidate=(created < candidate_utc if created else None)))
    load_summaries = {}
    for phase in ('preflight', 'main'):
        counts = {str(pid): dict(processes=0, retained_owned=0, roots=0) for pid in targets}
        samples = 0
        for line in (source/phase/'load.jsonl').open(encoding='utf-8'):
            row = json.loads(line)
            if row.get('event') == 'sample':
                samples += 1
            for group in ('processes', 'retained_owned'):
                for item in row.get(group, []):
                    if str(item.get('pid')) in counts:
                        counts[str(item['pid'])][group] += 1
            for item in row.get('roots', []):
                if str(item[0]) in counts:
                    counts[str(item[0])]['roots'] += 1
        load_summaries[phase] = dict(samples=samples, target_observation_counts=counts)
    refs = [cell/'games.json', cell/'engine-cpu.jsonl', source/'main/main-0040-worker.log',
        source/'main/report.json', source/'main/load.jsonl', source/'main/job-final-accounting.json',
        ROOT/'scripts/m2_engine_identity.py', ROOT/'scripts/m2_job_accounting.py', Path(__file__)]
    identity_source = ROOT/'scripts/m2_engine_identity.py'
    result = dict(schema_version=1, observed_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        mode='read-only-stopped-attempt-diagnosis', no_engine_started=True, no_process_or_security_mutation=True,
        candidate_transport=candidate, candidate_creation_utc=candidate_utc.isoformat(),
        failed_worker=next({k: row[k] for k in ('id', 'pid', 'creation_ticks')} for row in report['launched_cells']
                           if row['id'] == 'main-0040'),
        current_native= native, current_cim=child_records,
        native_parent_links_before={pid: row for pid, row in first.items() if pid in targets},
        native_parent_links_after={pid: row for pid, row in second.items() if pid in targets},
        raw_load_coverage=load_summaries,
        failure_trace=[json.loads(line) for line in (cell/'engine-cpu.jsonl').read_text().splitlines()
                       if json.loads(line).get('event') == 'setup_error'],
        current_identity_source_sha256=sha(identity_source),
        frozen_identity_source_sha256=frozen['harness_source_sha256'][str(identity_source)],
        source_sha256={str(path.relative_to(ROOT)): sha(path) for path in refs if path.exists()},
        limits=['Current inventories and CIM birth timestamps are later corroboration, not a replay of the failed first inventory.',
            'The failed setup receipt omitted offending child PID and inventory; historical exact culprit is unrecorded.',
            'Old load receipts contain Job aggregate counters, not JobObjectBasicProcessIdList membership snapshots.',
            'Access denial alone does not identify the exact DACL or protected-process mechanism; no privilege was changed.'])
    assert result['current_identity_source_sha256'] == result['frozen_identity_source_sha256']
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(dict(output=str(output), targets=targets, native_access=result['current_native'],
        current_cim=result['current_cim']), ensure_ascii=False))


if __name__ == '__main__':
    main()
