"""Archive a completed/failing Job-accounting attempt without input executables.

Usage: python scripts/archive_m2_job_attempt.py --attempt attempt-001
Large raw receipts use deterministic gzip. Existing destinations are refused;
an interrupted archive retains an incomplete marker for inspection.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / 'build-m2-job-accounting'
EVIDENCE_ROOT = ROOT / 'docs/checks/2026-10-21-m2-job-accounting/attempts'
COMPRESS_AFTER_BYTES = 100_000
RAW_SUFFIXES = {'.json', '.jsonl', '.pgn', '.log', '.txt'}
INPUT_SUFFIXES = {'.exe', '.dll', '.pyd', '.so', '.dylib', '.obj', '.o', '.a', '.lib', '.pdb',
                  '.nn', '.nnue', '.bin', '.onnx', '.npz', '.npy', '.pt', '.pth', '.safetensors',
                  '.h5', '.hdf5', '.weights', '.pickle', '.pkl', '.pyc', '.pyo', '.zip', '.7z', '.rar'}


class ArchiveError(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _inventory(directory):
    files = []
    for path in sorted(directory.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ArchiveError('archive input cannot contain a symlink or junction')
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ArchiveError('archive input contains an unsupported filesystem object')
    return files


def _sealed_inputs(source):
    seal, frozen = read_json(source / 'seal.json'), read_json(source / 'frozen-plan.json')
    if sha256(source / 'frozen-plan.json') != seal['frozen_plan_sha256']:
        raise ArchiveError('frozen plan does not match its seal')
    if 'source_plan_sha256' in frozen and sha256(source / 'source-plan.json') != frozen['source_plan_sha256']:
        raise ArchiveError('source-plan copy changed')
    entries = frozen.get('harness_source_sha256')
    if not isinstance(entries, dict) or not entries:
        raise ArchiveError('frozen harness hash declarations are required')
    names = set()
    for original, digest in entries.items():
        name = PureWindowsPath(original).name if '\\' in original or re.match(r'^[A-Za-z]:', original) else Path(original).name
        if name.casefold() in names:
            raise ArchiveError('frozen harness basename collision')
        names.add(name.casefold())
        # Read the frozen copy, never the current live workspace source.
        if sha256(source / 'harness' / name) != digest:
            raise ArchiveError('frozen harness copy changed: ' + name)
    return frozen


def _declared_payloads(source, files):
    declared = set()
    for path in files:
        relative = path.relative_to(source)
        if not _is_profile(relative):
            continue
        profile = read_json(path)
        records = [profile.get('binary', {})] + list(profile.get('models', {}).values())
        for item in records:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise ArchiveError('invalid frozen profile payload declaration')
            payload = Path(item['path'])
            declared.add((payload if payload.is_absolute() else path.parent / payload).resolve())
    return declared


def _is_profile(relative):
    return (len(relative.parts) == 3 and relative.parts[0].casefold() == 'profiles'
            and relative.name.casefold() == 'profile.json')


def _exclude(path, relative, declared):
    if relative.parts[0].casefold() == 'profiles' and not _is_profile(relative):
        return 'profile input payload; preserve profile.json metadata only'
    if path.resolve() in declared:
        return 'declared engine binary or model payload'
    if path.suffix.casefold() in INPUT_SUFFIXES:
        return 'executable, compiled artifact, model or input container suffix'
    # Also refuse renamed/extensionless executables and Regicide weights.
    with path.open('rb') as stream:
        magic = stream.read(128)
    if magic.startswith((b'MZ', b'\x7fELF', b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf',
                         b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe')):
        return 'executable magic'
    if magic.startswith((b'REGICIDE_NN_V', b'REGICIDE_NNUE')):
        return 'Regicide model magic'
    return None


def _copy(source, destination, compressed):
    digest, size = hashlib.sha256(), 0
    with source.open('rb') as incoming, destination.open('xb') as raw:
        if compressed:
            outgoing = gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=9, mtime=0)
        else:
            outgoing = raw
        try:
            for block in iter(lambda: incoming.read(1024 * 1024), b''):
                digest.update(block)
                size += len(block)
                outgoing.write(block)
        finally:
            if compressed:
                outgoing.close()
    return digest.hexdigest(), size


def _archived_json(destination, entry):
    path = destination / entry['archive']
    data = gzip.decompress(path.read_bytes()) if entry['compression'] == 'gzip-mtime0' else path.read_bytes()
    return json.loads(data.decode('utf-8-sig'))


def _phase_summary(destination, entries):
    indexed = {entry['source']: entry for entry in entries}
    phases = {}
    for phase in ('preflight', 'main'):
        report_entry = indexed.get(phase + '/report.json')
        if report_entry:
            report = _archived_json(destination, report_entry)
            summary = {key: report.get(key) for key in ('status', 'completed_games', 'scheduled_games',
                                                      'stop_reason', 'valid_for_comparison')}
            load_entry = indexed.get(phase + '/load-summary.json')
            if load_entry:
                summary['failure'] = _archived_json(destination, load_entry).get('failure')
            phases[phase] = summary
    return phases


def archive_attempt(source_dir, destination_dir):
    source, destination = Path(source_dir), Path(destination_dir)
    if source.is_symlink() or (hasattr(source, 'is_junction') and source.is_junction()):
        raise ArchiveError('attempt root cannot be a link')
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise ArchiveError('attempt input directory does not exist')
    if destination == source or destination.is_relative_to(source):
        raise ArchiveError('archive destination cannot be inside the source attempt')
    if destination.exists():
        raise FileExistsError('preserve existing archive destination: ' + str(destination))
    files = _inventory(source)
    _sealed_inputs(source)
    declared = _declared_payloads(source, files)
    planned, excluded, names = [], [], {'archive.json', 'archive-incomplete.json'}
    for path in files:
        relative = path.relative_to(source)
        reason = _exclude(path, relative, declared)
        if reason:
            before = path.stat()
            digest = sha256(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ArchiveError('excluded payload changed during hashing: ' + str(relative))
            excluded.append({'source': relative.as_posix(), 'sha256': digest,
                             'size': after.st_size, 'reason': reason})
            continue
        compressed = path.suffix.casefold() in RAW_SUFFIXES and path.stat().st_size > COMPRESS_AFTER_BYTES
        archived = relative.with_name(relative.name + '.gz') if compressed else relative
        folded = archived.as_posix().casefold()
        if folded in names:
            raise ArchiveError('archive filename collision: ' + archived.as_posix())
        names.add(folded)
        planned.append((path, relative, archived, compressed))
    destination.mkdir(parents=True, exist_ok=False)
    incomplete = destination / 'archive-incomplete.json'
    incomplete.write_text(json.dumps({'status': 'INCOMPLETE', 'source': str(source)}) + '\n', encoding='utf-8')
    entries = []
    for path, relative, archived, compressed in planned:
        before = path.stat()
        output = destination / archived
        output.parent.mkdir(parents=True, exist_ok=True)
        digest, size = _copy(path, output, compressed)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
            raise ArchiveError('source changed while being archived: ' + str(relative))
        entries.append({'source': relative.as_posix(), 'archive': archived.as_posix(),
            'sha256': digest, 'size': size, 'archive_sha256': sha256(output), 'archive_size': output.stat().st_size,
            'compression': 'gzip-mtime0' if compressed else 'none'})
    if [p.relative_to(source) for p in _inventory(source)] != [p.relative_to(source) for p in files]:
        raise ArchiveError('attempt input inventory changed during archive')
    for entry in entries + excluded:
        if sha256(source / entry['source']) != entry['sha256']:
            raise ArchiveError('attempt input changed before finalizing: ' + entry['source'])
    _sealed_inputs(source)
    phases = _phase_summary(destination, entries)
    indexed = {entry['source']: entry for entry in entries}
    last_phase = 'main' if 'main' in phases else 'preflight' if 'preflight' in phases else None
    manifest = {'schema_version': 2, 'attempt': source.name, 'source_directory': str(source),
        'archive_status': 'COMPLETE', **phases.get(last_phase, {'status': 'FROZEN_ONLY'}),
        'phase': last_phase, 'main_started': any(entry['source'].startswith('main/') for entry in entries),
        'phase_reports': phases, 'files': entries, 'excluded': excluded,
        'frozen_plan_sha256': indexed['frozen-plan.json']['sha256'],
        'harness_copy_policy': 'Frozen copies verified and copied as bytes; live workspace sources are not substituted.',
        'payload_policy': 'Do not distribute engine binaries/models/profile payloads; keep profile.json provenance.',
        'compression_policy': {'suffixes': sorted(RAW_SUFFIXES), 'strictly_larger_than_bytes': COMPRESS_AFTER_BYTES,
                               'gzip_mtime': 0, 'gzip_filename': '', 'compression_level': 9}}
    with (destination / 'archive.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    incomplete.unlink()
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'attempt-[0-9]{3,}', args.attempt):
        parser.error('attempt must be a single name such as attempt-001')
    manifest = archive_attempt(SOURCE_ROOT / args.attempt, EVIDENCE_ROOT / args.attempt)
    print(json.dumps({'attempt': manifest['attempt'], 'files': len(manifest['files']),
                      'excluded': len(manifest['excluded']), 'phase': manifest['phase']}))


if __name__ == '__main__':
    main()
