"""Verify new M2 archived bytes, frozen sources and current evidence files."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify(output):
    archives, sources = [], set()
    for path in sorted((HERE/'attempts').glob('*/archive.json')):
        record = json.loads(path.read_text(encoding='utf-8'))
        decoded = {}
        for row in record['files']:
            target = path.parent/row['archive']
            assert target.resolve().is_relative_to(path.parent.resolve())
            stored = target.read_bytes()
            assert len(stored) == row['archive_size'] and digest(stored) == row['archive_sha256'], str(target)
            if row['compression'] == 'gzip-mtime0':
                assert stored[4:8] == bytes(4)
                data = gzip.decompress(stored)
            else:
                assert row['compression'] == 'none'
                data = stored
            assert len(data) == row['size'] and digest(data) == row['sha256'], str(target)
            original = Path(record['source_directory'])/row['source']
            assert original.is_file() and original.read_bytes() == data, str(original)
            decoded[row['source']] = data
        frozen = json.loads(decoded['frozen-plan.json'])
        seal = json.loads(decoded['seal.json'])
        assert digest(decoded['frozen-plan.json']) == seal['frozen_plan_sha256']
        for absolute, sha in frozen['harness_source_sha256'].items():
            source = Path(absolute)
            assert digest(source.read_bytes()) == sha
            assert digest(decoded['harness/'+source.name]) == sha
            sources.add(source)
        assert record['status'] == 'STOPPED' and record['valid_for_comparison'] is False
        assert record['main_started'] is False
        assert not (Path(record['source_directory'])/'main'/'run-started.json').exists()
        archives.append({'attempt': path.parent.name, 'verified_files': len(record['files']),
                         'completed_preflight_games': record['completed_games'],
                         'valid_for_comparison': False, 'main_started': False})
    files = set(HERE.rglob('*')) | sources
    files.update((ROOT/'scripts').glob('m2_job_*.py'))
    files.update((ROOT/'scripts').glob('m2_formal*v[23].py'))
    files.update((ROOT/'tests').glob('m2_*_tests.py'))
    files.add(ROOT/'tests/m2_job_load_guard_integration.py')
    files.add(ROOT/'scripts/archive_m2_job_attempt.py')
    files.update((ROOT/'docs/experiments').glob('2026-10-21-m2-job-accounting*'))
    files.update((ROOT/'.gitattributes', ROOT/'docs/ROADMAP.md'))
    files = sorted(p for p in files if p.is_file() and '__pycache__' not in p.parts and p.resolve() != output.resolve())
    names = [p.relative_to(ROOT).as_posix() for p in files]
    ignored = subprocess.run(['git', 'check-ignore', '--stdin'], cwd=ROOT,
        input=('\n'.join(names)+'\n').encode('utf-8'), capture_output=True)
    assert ignored.returncode in (0, 1), ignored.stderr
    excluded = set(ignored.stdout.decode('utf-8').splitlines())
    entries = {p.relative_to(ROOT).as_posix(): {'sha256': digest(p.read_bytes()), 'size': p.stat().st_size}
               for p in files if p.relative_to(ROOT).as_posix() not in excluded}
    return {'status': 'PASS_FOR_BYTE_INTEGRITY_ONLY', 'verified_utc': datetime.now(timezone.utc).isoformat(),
            'main_started': False, 'main_completed_games': 0, 'main_scheduled_games': 768,
            'archives': archives, 'unchanged_frozen_current_sources': len(sources), 'files': entries,
            'ignored_files': sorted(excluded),
            'scope': 'Byte/gzip/frozen-source preservation only. Both preflights remain invalid; no strength, Elo or complete hermetic distribution claim.'}


def verify_index(manifest_path):
    """Keep pre-existing checkout CRLF distinct from newly preserved raw bytes."""
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    names = sorted(manifest['files'])
    data = subprocess.check_output(['git', 'cat-file', '--batch'], cwd=ROOT,
        input=b''.join((':'+name+'\n').encode('utf-8') for name in names))
    inherited = {'scripts/compare_engines.py', 'scripts/see_clean_guard.py'}
    offset, normalized = 0, []
    for name in names:
        stop = data.index(b'\n', offset)
        header = data[offset:stop].split()
        assert len(header) == 3, (name, header)
        size = int(header[2])
        blob = data[stop+1:stop+1+size]
        offset = stop+size+2
        working = (ROOT/name).read_bytes()
        assert digest(working) == manifest['files'][name]['sha256']
        assert len(working) == manifest['files'][name]['size']
        if blob != working:
            # These two unmodified historical sources already use Git's
            # checkout CRLF conversion. Their frozen copies remain exact raw
            # bytes, independently verified above. Do not rewrite old sources.
            assert name in inherited and working.replace(b'\r\n', b'\n') == blob, name
            subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', name], cwd=ROOT, check=True)
            normalized.append({'path': name, 'index_sha256': digest(blob),
                               'working_and_frozen_sha256': digest(working),
                               'scope': 'Pre-existing Git LF/checkout CRLF only; source unchanged from HEAD.'})
    assert offset == len(data)
    return {'status': 'PASS', 'manifest_files_checked': len(names),
            'exact_index_byte_files': len(names)-len(normalized),
            'preexisting_checkout_line_endings': normalized,
            'unexpected_mismatches': [],
            'scope': 'All working bytes match the current manifest. New artifact/source index blobs are exact; two unchanged inherited source checkouts retain their existing LF/CRLF conversion.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('preserve the existing manifest')
    result = verify(args.output)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'status': result['status'], 'files': len(result['files']), 'archives': result['archives']}))
