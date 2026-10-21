"""Temporary-only archive fixtures; launches no engine and archives no actual run."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / 'scripts/archive_m2_job_attempt.py'
spec = importlib.util.spec_from_file_location('archive_m2_job_attempt', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
passed = []


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    passed.append(name)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def rejects(call, error):
    try:
        call()
    except error:
        return True
    return False


started = time.monotonic()
with tempfile.TemporaryDirectory(prefix='m2-archive-fixture-') as temporary:
    root = Path(temporary)
    source = root / 'attempt-001'
    (source / 'harness').mkdir(parents=True)
    frozen_bytes = b'old frozen bytes\r\n'
    (source / 'harness/runner.py').write_bytes(frozen_bytes)
    write_json(source / 'frozen-plan.json', {'harness_source_sha256': {
        'C:/live/runner.py': hashlib.sha256(frozen_bytes).hexdigest()}})
    write_json(source / 'seal.json', {'frozen_plan_sha256': module.sha256(source / 'frozen-plan.json')})
    write_json(source / 'main/report.json', {'status': 'STOPPED', 'completed_games': 0,
        'scheduled_games': 768, 'stop_reason': 'fixture', 'padding': 'r' * 100_001})
    write_json(source / 'main/load-summary.json', {'failure': 'fixture'})
    (source / 'main/large.jsonl').write_bytes(b'x' * 100_001)
    (source / 'main/equal.log').write_bytes(b'e' * 100_000)
    (source / 'models').mkdir()
    (source / 'models/weird.txt').write_bytes(b'REGICIDE_NNUE 2 weights')
    (source / 'renamed').write_bytes(b'MZ executable')
    (source / 'profiles/engine/artifacts').mkdir(parents=True)
    (source / 'profiles/engine/binary.exe').write_bytes(b'MZ exe')
    (source / 'profiles/engine/artifacts/profile.json').write_bytes(b'not metadata')
    write_json(source / 'profiles/engine/profile.json', {'binary': {'path': 'binary.exe'}, 'models': {}})
    destination = root / 'saved-a'
    manifest = module.archive_attempt(source, destination)
    module.archive_attempt(source, root / 'saved-b')
    entries = {entry['source']: entry for entry in manifest['files']}
    compressed = (destination / 'main/large.jsonl.gz').read_bytes()
    check(compressed == (root / 'saved-b/main/large.jsonl.gz').read_bytes()
          and gzip.decompress(compressed) == b'x' * 100_001 and compressed[4:8] == b'\0' * 4,
          'deterministic gzip and exact decompression')
    check(entries['main/equal.log']['compression'] == 'none', 'strict 100000 byte boundary')
    check(len(manifest['excluded']) == 4 and 'profiles/engine/profile.json' in entries,
          'profile metadata retained and executable/model payloads excluded')
    check(all(module.sha256(source / item['source']) == item['sha256']
              and (source / item['source']).stat().st_size == item['size']
              and module.sha256(destination / item['archive']) == item['archive_sha256']
              and (destination / item['archive']).stat().st_size == item['archive_size']
              for item in manifest['files'])
          and all(module.sha256(source / item['source']) == item['sha256']
                  for item in manifest['excluded']), 'original and preserved hashes and sizes')
    check((destination / 'harness/runner.py').read_bytes() == frozen_bytes,
          'frozen source copied verbatim')
    check(manifest['phase'] == 'main' and manifest['status'] == 'STOPPED'
          and manifest['failure'] == 'fixture'
          and entries['main/report.json']['compression'] == 'gzip-mtime0'
          and manifest['frozen_plan_sha256'] == entries['frozen-plan.json']['sha256'],
          'phase summary reads preserved compressed report')
    check(not (destination / 'archive-incomplete.json').exists(), 'completed marker lifecycle')
    check(rejects(lambda: module.archive_attempt(source, destination), FileExistsError),
          'existing destination refused')
    (source / 'main/large.jsonl.gz').write_bytes(b'collision')
    check(rejects(lambda: module.archive_attempt(source, root / 'collision'), module.ArchiveError)
          and not (root / 'collision').exists(), 'compressed path collision refused before writing')
    (source / 'main/large.jsonl.gz').unlink()
    original_copy = module._copy

    def changing_copy(incoming, outgoing, compressed):
        result = original_copy(incoming, outgoing, compressed)
        if incoming.name == 'equal.log':
            incoming.write_bytes(b'changed')
        return result

    module._copy = changing_copy
    check(rejects(lambda: module.archive_attempt(source, root / 'changed'), module.ArchiveError)
          and (root / 'changed/archive-incomplete.json').exists()
          and not (root / 'changed/archive.json').exists(), 'source mutation keeps incomplete marker')
    module._copy = original_copy
    (source / 'harness/runner.py').write_bytes(b'tampered')
    check(rejects(lambda: module.archive_attempt(source, root / 'tampered'), module.ArchiveError),
          'frozen source tamper refused')

print(json.dumps({'status': 'PASS', 'checks': passed, 'check_count': len(passed),
    'elapsed_seconds': round(time.monotonic() - started, 6),
    'source_sha256': module.sha256(SOURCE),
    'scope': 'Temporary fixture files only; no actual experiment or process execution.'}, indent=2))
