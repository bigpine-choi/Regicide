"""Verify preserved M2 engine-CPU bytes without judging experiment outcomes.

Defaults verify stored/decoded archives against their current raw originals,
sealed plans, frozen/current harness sources and profile metadata. Large local
excluded payload hashing is explicit and optional. Outputs are always new files.
Use --index-manifest MANIFEST to compare one manifest with raw staged blobs.
"""
import argparse
import ast
from datetime import datetime, timezone
import fnmatch
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CHUNK = 1024 * 1024
LEGACY_CHECKOUTS = {
    'scripts/compare_engines.py': {
        'working_sha256': 'ececbe63f6bf60b9d751655c808aac228531ad3fc79c6a665a349aef24d6a368',
        'working_size': 31961,
        'index_sha256': 'ab8b56f4298fbe2442156108ad3ade7db2e2f265a00c502f6986c25370ecab7c',
        'index_size': 31387},
    'scripts/see_clean_guard.py': {
        'working_sha256': 'f563d701cd4c2cbcc4fcf5d131a5e28bf3be3ca1aceb0eee11d6e164a9205967',
        'working_size': 26604,
        'index_sha256': 'f9d4f7121684e7bf4bbd3db50089d8955f806195e65a888dee062841cbdaf228',
        'index_size': 26099},
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_identity(path):
    path = Path(path)
    before = path.stat()
    value, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            value.update(block)
            size += len(block)
    after = path.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            and size == after.st_size, 'file changed during hashing: ' + str(path))
    return {'sha256': value.hexdigest(), 'size': size}


def json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def safe_relative(value):
    require(isinstance(value, str) and value and '\\' not in value and ':' not in value,
            'archive name must be a portable relative path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(part not in ('', '.', '..') for part in value.split('/')),
            'unsafe archive relative path: ' + value)
    return path


def child_path(directory, value):
    relative = safe_relative(value)
    directory = Path(directory).resolve()
    target = directory.joinpath(*relative.parts)
    require(target.resolve().is_relative_to(directory), 'path escaped its declared directory: ' + value)
    return target


def assert_file(path, declared_sha, declared_size=None):
    actual = file_identity(path)
    require(actual['sha256'] == declared_sha, 'SHA256 differs: ' + str(path))
    if declared_size is not None:
        require(actual['size'] == declared_size, 'file size differs: ' + str(path))
    return actual


def record_path(record, base):
    require(isinstance(record, dict) and isinstance(record.get('path'), str), 'file reference is malformed')
    path = Path(record['path'])
    return (path if path.is_absolute() else Path(base)/path).resolve()


def verify_entry(archive_dir, source_dir, row):
    target, original = child_path(archive_dir, row['archive']), child_path(source_dir, row['source'])
    assert_file(target, row['archive_sha256'], row['archive_size'])
    compression = row['compression']
    require(compression in ('none', 'gzip-mtime0'), 'unknown archive compression')
    if compression == 'gzip-mtime0':
        with target.open('rb') as stream:
            header = stream.read(10)
        require(len(header) == 10 and header[:3] == b'\x1f\x8b\x08'
                and header[4:8] == bytes(4) and not header[3] & 8,
                'gzip is not the declared mtime-zero/empty-filename format: ' + str(target))
    before = original.stat()
    opener = gzip.open if compression == 'gzip-mtime0' else open
    decoded_hash, size = hashlib.sha256(), 0
    with opener(target, 'rb') as decoded, original.open('rb') as raw:
        while True:
            block = decoded.read(CHUNK)
            require(raw.read(len(block)) == block, 'decoded archive differs from current raw: ' + str(original))
            if not block:
                require(raw.read(1) == b'', 'current raw has extra bytes: ' + str(original))
                break
            decoded_hash.update(block)
            size += len(block)
    after = original.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            'raw original changed during verification: ' + str(original))
    require(decoded_hash.hexdigest() == row['sha256'] and size == row['size'] == after.st_size,
            'decoded SHA256/size differs: ' + str(target))
    return original


def source_relative(source_dir, absolute):
    path = Path(absolute).resolve()
    require(path.is_relative_to(source_dir), 'frozen reference escaped its raw attempt')
    return path.relative_to(source_dir).as_posix()


def verify_sealed_inputs(source, indexed, archive):
    frozen, seal = json_file(source/'frozen-plan.json'), json_file(source/'seal.json')
    frozen_identity = assert_file(source/'frozen-plan.json', seal['frozen_plan_sha256'])
    require(frozen_identity['sha256'] == archive['frozen_plan_sha256'], 'archive frozen-plan SHA differs')
    assert_file(source/'source-plan.json', frozen['source_plan_sha256'])
    require(json_file(source/'source-plan.json') == frozen['plan'], 'source-plan conditions differ from frozen plan')
    source_plan = Path(frozen['source_plan_path']).resolve()
    assert_file(source_plan, frozen['source_plan_sha256'])
    sources, names = set(), set()
    for absolute, declared in frozen['harness_source_sha256'].items():
        current = Path(absolute).resolve()
        require(current.is_relative_to(ROOT), 'current harness source escaped this repository')
        name = current.name
        require(name.casefold() not in names, 'duplicate frozen harness basename')
        names.add(name.casefold())
        require('harness/'+name in indexed, 'frozen harness copy is missing from archive: ' + name)
        assert_file(current, declared)
        assert_file(source/'harness'/name, declared)
        sources.add(current)
    require(bool(sources), 'no sealed harness sources')
    copied = list(frozen.get('preregistration_artifacts', [])) + list(frozen.get('banks', {}).values())
    for record in copied:
        path = record_path(record, source)
        require(source_relative(source, path) in indexed, 'frozen bank/preregistration copy not archived')
        assert_file(path, record['sha256'])
    for key in ('candidates', 'opponents', 'preregistration_artifacts'):
        for record in frozen['plan'].get(key, []):
            assert_file(record_path(record, source_plan.parent), record['sha256'])
    for key in ('openings', 'preflight_openings'):
        record = frozen['plan'][key]
        assert_file(record_path(record, source_plan.parent), record['sha256'])
    excluded = {row['source']: row for row in archive.get('excluded', [])}
    profile_count = declared_payloads = 0
    for record in frozen['profiles'].values():
        profile_path = record_path(record, source)
        relative = source_relative(source, profile_path)
        require(relative in indexed, 'frozen profile metadata is missing from archive')
        assert_file(profile_path, record['sha256'])
        profile = json_file(profile_path)
        payloads = [profile['binary'], *profile.get('models', {}).values(), *profile.get('artifacts', [])]
        for payload in payloads:
            payload_path = record_path(payload, profile_path.parent)
            payload_relative = source_relative(source, payload_path)
            require(payload_relative in excluded and excluded[payload_relative]['sha256'] == payload['sha256'],
                    'profile payload is not declared consistently as excluded: ' + payload_relative)
            declared_payloads += 1
        profile_count += 1
    return sources, {'harness_sources': len(sources), 'profiles': profile_count,
                     'declared_profile_payloads': declared_payloads,
                     'source_plan_sha256': frozen['source_plan_sha256']}


def phase_summary(source, indexed, archive):
    phases = {}
    for phase in ('preflight', 'main'):
        if phase+'/report.json' not in indexed:
            continue
        report = json_file(source/phase/'report.json')
        summary = {key: report.get(key) for key in ('status', 'completed_games', 'scheduled_games',
                   'stop_reason', 'valid_for_comparison')}
        for key in ('completed_games', 'scheduled_games'):
            require(type(summary[key]) is int and summary[key] >= 0, 'phase game count is malformed')
        require(summary['completed_games'] <= summary['scheduled_games'], 'completed phase games exceed schedule')
        require(type(summary['valid_for_comparison']) is bool, 'phase validity is not boolean')
        if phase+'/load-summary.json' in indexed:
            summary['failure'] = json_file(source/phase/'load-summary.json').get('failure')
        require(archive.get('phase_reports', {}).get(phase) == summary,
                'archive phase summary differs from its raw report: ' + phase)
        phases[phase] = summary
    main_started = any(name.startswith('main/') for name in indexed)
    require(archive['main_started'] is main_started, 'archive main-started flag differs from archived entries')
    latest = 'main' if 'main' in phases else 'preflight' if 'preflight' in phases else None
    require(archive.get('phase') == latest, 'archive final phase differs')
    if latest is not None:
        for key, value in phases[latest].items():
            require(archive.get(key) == value, 'archive final status/count differs: ' + key)
    return {'main_started': main_started, 'phases': phases}


def verify_archive(path, *, verify_excluded_payloads=False, excluded_sources=()):
    archive = json_file(path)
    require(archive.get('archive_status') == 'COMPLETE', 'incomplete archive manifest')
    require(not (path.parent/'archive-incomplete.json').exists(), 'incomplete marker remains')
    source = Path(archive['source_directory']).resolve()
    indexed, stored_names = {}, set()
    for row in archive['files']:
        safe_relative(row['source'])
        require(row['source'] not in indexed, 'duplicate archive source entry')
        folded = row['archive'].casefold()
        require(folded not in stored_names, 'duplicate stored archive entry')
        stored_names.add(folded)
        indexed[row['source']] = row
        verify_entry(path.parent, source, row)
    actual = {item.relative_to(path.parent).as_posix().casefold() for item in path.parent.rglob('*')
              if item.is_file() and '__pycache__' not in item.parts}
    require(actual == stored_names | {'archive.json'}, 'archive contains missing or unlisted files')
    sources, sealed = verify_sealed_inputs(source, indexed, archive)
    excluded_checked, excluded_skipped, matched = [], [], set()
    seen_excluded = set()
    for row in archive.get('excluded', []):
        name = row['source']
        safe_relative(name)
        require(name not in seen_excluded and name not in indexed, 'duplicate/colliding excluded payload')
        seen_excluded.add(name)
        patterns = [pattern for pattern in excluded_sources if fnmatch.fnmatchcase(name, pattern)]
        selected = verify_excluded_payloads or bool(patterns)
        matched.update(patterns)
        if selected:
            assert_file(child_path(source, name), row['sha256'], row['size'])
            excluded_checked.append(name)
        else:
            excluded_skipped.append(name)
    return {'attempt': archive['attempt'], 'verified_files': len(indexed), **sealed,
        **phase_summary(source, indexed, archive), 'excluded_payloads_hashed': excluded_checked,
        'excluded_payloads_not_hashed': excluded_skipped}, sources, matched


def manifest_paths(output, sources):
    files = set(HERE.rglob('*')) | set(sources)
    for pattern in ('m2_engine_*.py', 'm2_warning*.py', 'm2_*v[456].py', 'm2_baseline96.py', 'archive_m2_engine*.py'):
        files.update((ROOT/'scripts').glob(pattern))
    for pattern in ('m2_engine*_tests.py', 'm2_warning*_tests.py', 'm2_formal_v[456]_tests.py', 'm2_job_process_scope_tests.py', 'm2_baseline96_tests.py'):
        files.update((ROOT/'tests').glob(pattern))
    for runner in sorted((ROOT/'scripts').glob('m2_formal_v[456].py')):
        for node in ast.parse(runner.read_text(encoding='utf-8-sig')).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'HARNESS' for t in node.targets):
                files.update(ROOT/'scripts'/name for name in ast.literal_eval(node.value))
    files.update((ROOT/'docs/experiments').glob('2026-10-21-m2-engine-cpu*'))
    files.update((ROOT/'docs/experiments').glob('2026-10-21-m2-baseline96*'))
    files.update((ROOT/'.gitattributes', ROOT/'docs/ROADMAP.md', ROOT/'docs/EXPERIMENT_POLICY.md'))
    selected = sorted(path for path in files if path.is_file() and '__pycache__' not in path.parts
                      and path.resolve() != Path(output).resolve())
    names = [path.relative_to(ROOT).as_posix() for path in selected]
    ignored = subprocess.run(['git', 'check-ignore', '--stdin'], cwd=ROOT,
        input=('\n'.join(names)+'\n').encode('utf-8'), capture_output=True)
    require(ignored.returncode in (0, 1), 'git check-ignore failed: '+ignored.stderr.decode('utf-8', errors='replace'))
    excluded = set(ignored.stdout.decode('utf-8').splitlines())
    return {name: file_identity(ROOT/name) for name in names if name not in excluded}, sorted(excluded)


def verify(output, *, verify_excluded_payloads=False, excluded_sources=()):
    archives, sources, matched = [], set(), set()
    for path in sorted((HERE/'attempts').glob('*/archive.json')):
        summary, dependencies, selections = verify_archive(path,
            verify_excluded_payloads=verify_excluded_payloads, excluded_sources=excluded_sources)
        archives.append(summary)
        sources.update(dependencies)
        matched.update(selections)
    require(not archives or matched == set(excluded_sources), 'an excluded payload selector matched no archived entry')
    files, ignored = manifest_paths(output, sources)
    return {'schema_version': 1, 'status': 'PASS_FOR_BYTE_INTEGRITY_ONLY',
        'verified_utc': datetime.now(timezone.utc).isoformat(), 'archives': archives,
        'unchanged_frozen_current_sources': len(sources), 'files': files, 'ignored_files': ignored,
        'excluded_payload_policy': {'verify_all': verify_excluded_payloads, 'selectors': list(excluded_sources),
            'unmatched_selectors_without_archives': sorted(set(excluded_sources)-matched)},
        'scope': 'Stored/decoded/current raw bytes, sealed inputs and evidence files only. Phase outcomes are reported from records, including successful main completion; this is not a strength pass, Elo estimate or hermetic distribution claim.'}


def verify_index(manifest_path):
    manifest_path = Path(manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode('utf-8-sig'))
    names = sorted(manifest['files'])
    normalized = []
    process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=ROOT, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for name in names:
            safe_relative(name)
            declared = manifest['files'][name]
            working = ROOT/name
            actual = file_identity(working)
            require(actual == declared, 'working file differs from manifest: ' + name)
            process.stdin.write((':'+name+'\n').encode('utf-8'))
            process.stdin.flush()
            header = process.stdout.readline().rstrip(b'\n').split()
            require(len(header) == 3 and header[1] == b'blob', 'staged file missing or malformed: ' + name)
            size = int(header[2])
            if name in LEGACY_CHECKOUTS:
                blob = process.stdout.read(size)
                legacy = LEGACY_CHECKOUTS[name]
                require(actual == {'sha256': legacy['working_sha256'], 'size': legacy['working_size']}
                        and size == legacy['index_size'] and digest(blob) == legacy['index_sha256'],
                        'inherited source differs from its fixed historical byte identities: ' + name)
                normalized.append({'path': name, **legacy,
                    'scope': 'Exactly the two fixed original checkout/index hashes; no general line-ending normalization exception.'})
            else:
                require(size == actual['size'], 'staged size differs: ' + name)
                remaining = size
                with working.open('rb') as stream:
                    while remaining:
                        count = min(CHUNK, remaining)
                        blob = process.stdout.read(count)
                        require(len(blob) == count and stream.read(count) == blob, 'staged raw bytes differ: ' + name)
                        remaining -= count
            require(process.stdout.read(1) == b'\n', 'invalid staged blob terminator: ' + name)
        process.stdin.close()
        require(process.wait(timeout=30) == 0, 'git cat-file failed')
        require(process.stdout.read() == b'', 'unexpected extra staged blob output')
        require(manifest_path.read_bytes() == manifest_bytes, 'input manifest changed during index verification')
    finally:
        if process.poll() is None:
            process.kill()  # Only this newly created Git subprocess handle.
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    return {'status': 'PASS', 'manifest_sha256': digest(manifest_bytes),
        'manifest_files_checked': len(names), 'exact_index_byte_files': len(names)-len(normalized),
        'preexisting_checkout_line_endings': normalized, 'unexpected_mismatches': [],
        'scope': 'Raw staged blobs match manifest working bytes; only two fixed historical source checkout/index SHA pairs are allowed to differ.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-excluded-payloads', action='store_true')
    parser.add_argument('--excluded-source', action='append', default=[])
    parser.add_argument('--index-manifest', type=Path)
    args = parser.parse_args(argv)
    require(not args.output.exists(), 'preserve the existing output')
    if args.index_manifest:
        require(not args.verify_excluded_payloads and not args.excluded_source, 'payload options do not apply to index mode')
        result = verify_index(args.index_manifest)
    else:
        result = verify(args.output, verify_excluded_payloads=args.verify_excluded_payloads,
                        excluded_sources=args.excluded_source)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'status': result['status'], 'files': len(result.get('files', {})),
                      'archives': len(result.get('archives', [])), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
