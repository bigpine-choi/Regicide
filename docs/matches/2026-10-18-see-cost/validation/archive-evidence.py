"""Copy completed experiment evidence, preserving original bytes and hashes."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / 'docs/matches/2026-10-18-see-cost'
EVIDENCE = Path(__file__).resolve().parent
assert not TARGET.exists(), 'Preserve the original public archive'
audit = json.loads((EVIDENCE / 'independent-audit.json').read_text(encoding='utf-8'))
assert audit['status'] == 'PASS' and len(audit['matches']) == 2
sources = []
def copy(source, relative):
    source = Path(source)
    target = TARGET / relative
    assert source.is_file() and not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    assert source.read_bytes() == target.read_bytes()
    sources.append({'source': source.relative_to(ROOT).as_posix(), 'archive': target.relative_to(TARGET).as_posix()})

for attempt in sorted((ROOT / 'build/see-cost-comparison').glob('attempt-[0-9][0-9][0-9]')):
    for source in sorted(attempt.iterdir()):
        if source.is_file():
            copy(source, Path(attempt.name) / source.name)
for source in sorted((ROOT / 'build/see-cost-improvement/analysis').iterdir()):
    if source.is_file():
        copy(source, Path('analysis') / source.name)
profile = ROOT / 'build-see-cost'
manifest = json.loads((profile / 'core-manifest.json').read_text(encoding='utf-8'))
copy(profile / 'core-manifest.json', 'profile/core-manifest.json')
for row in manifest['files']:
    source = ROOT / row['path']
    if source.suffix.lower() in {'.exe', '.obj'}:
        continue  # Fingerprints retained; compiled artifacts stay in local build folders.
    assert hashlib.sha256(source.read_bytes()).hexdigest() == row['sha256'], row['path']
    relative = Path('profile') / ('source-snapshots' if source.parent != profile else '') / source.name
    copy(source, relative)
for source in sorted(EVIDENCE.iterdir()):
    if source.is_file() and source.name != 'report-draft.md':
        copy(source, Path('validation') / source.name)
for relative in ['scripts/see_clean_guard.py', 'scripts/run_see_cost_comparison.ps1',
                 'scripts/benchmark_search.py', 'scripts/compare_engines.py',
                 'tests/audit_see_cost_results.py', 'tests/audit_see_replication.py',
                 'tests/audit_see_results.py', 'tests/see_clean_guard_tests.py',
                 'tests/data/see_cost_freeze_2026-10-18.json', 'tests/data/see_cost_openings_2026-10-18.json']:
    copy(ROOT / relative, Path('validation/tool-snapshots') / Path(relative).name)
mapping = {'note': 'Original byte mapping; compiled intermediates/binaries are fingerprinted in provenance and retained in local build folders.',
           'files': sources}
(TARGET / 'source-map.json').write_text(json.dumps(mapping, indent=2) + '\n', encoding='utf-8')
hashes = {source.relative_to(TARGET).as_posix(): hashlib.sha256(source.read_bytes()).hexdigest()
          for source in sorted(TARGET.rglob('*')) if source.is_file()}
(TARGET / 'sha256.json').write_text(json.dumps(hashes, indent=2) + '\n', encoding='utf-8')
print(f'Archived {len(hashes)} evidence files plus sha256.json; original bytes preserved.')
