"""Check publication byte mappings and the exact staged Git blobs."""
import hashlib
import json
import re
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT/'docs/checks/2026-10-19-endgame-learning-scale'

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def main():
    manifest = json.loads((DEST/'publication-manifest.json').read_text(encoding='utf-8'))
    checked = 0
    for entry in manifest['files']:
        path = DEST/entry['path']
        raw = path.read_bytes()
        assert sha(raw) == entry['published_sha256'], entry['path']
        assert len(raw) == entry['bytes'], entry['path']
        staged = subprocess.check_output(['git','show',':'+path.relative_to(ROOT).as_posix()],cwd=ROOT)
        assert staged == raw, 'Git blob differs: '+entry['path']
        if path.suffix != '.nn':
            assert re.search(rb'c:[/\\]+users[/\\]+hanso', raw, re.I) is None, entry['path']
        checked += 1
    for entry in manifest['remote_inputs']:
        path = DEST/entry['path']
        raw = path.read_bytes()
        assert sha(raw) == entry['sha256']
        assert subprocess.check_output(['git','show',':'+path.relative_to(ROOT).as_posix()],cwd=ROOT) == raw
    assert checked == manifest['file_count']
    result = {'status':'passed','published_files_checked':checked,
              'remote_inputs_checked':len(manifest['remote_inputs']),
              'every_declared_git_blob_matches_publication':True,
              'all_texts_checked_for_private_user_prefix':True,
              'manifest_sha256':sha((DEST/'publication-manifest.json').read_bytes()),
              'scope':'Declared raw study, failed attempt, validation artifacts and remote inputs. README and audit summary are separate authored report files.'}
    (ROOT/'build/publication-audit-study6.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__ == '__main__':
    main()
