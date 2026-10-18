from pathlib import Path, PurePosixPath
import subprocess
import json
import hashlib

ROOT = Path(__file__).resolve().parents[2]
COMMIT = 'c2c1a80eaf3f8b8ee0ecf0a4168740681bf485ba'
PARENT = '31564da7e209a3dcff755953c9a5d436524ef21d'
PREFIX = 'docs/checks/2026-10-19-endgame-learning-scale/remote-result/7a14a7b222f6/'
DEST = ROOT / 'build/endgame-learning-scale/remote-download-study6'

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)

assert git('show', '-s', '--format=%P', COMMIT).decode().strip() == PARENT
assert not DEST.exists(), 'Refuse to overwrite evidence'
entries = []
for row in git('ls-tree', '-r', '-z', COMMIT, '--', PREFIX).split(b'\0'):
    if not row:
        continue
    facts, raw_name = row.split(b'\t', 1)
    mode, kind, oid = facts.decode().split()
    name = raw_name.decode('utf-8')
    assert mode in ('100644', '100755') and kind == 'blob'
    assert name.startswith(PREFIX)
    relative = PurePosixPath(name[len(PREFIX):])
    assert not relative.is_absolute() and '..' not in relative.parts and relative.parts
    target = DEST.joinpath(*relative.parts).resolve()
    assert target.is_relative_to(DEST.resolve())
    raw = git('cat-file', 'blob', oid)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    entries.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
assert len(entries) == 7
(DEST.parent / 'remote-extraction-study6.json').write_text(json.dumps({'commit': COMMIT, 'parent': PARENT, 'files': entries}, indent=2)+'\n', encoding='utf-8')
print(json.dumps(entries, indent=2))
