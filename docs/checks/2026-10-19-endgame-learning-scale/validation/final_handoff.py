"""Read-only final source, remote and installed-state proof."""
from pathlib import Path
import subprocess
import hashlib
import json
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[2]
PRIMARY=ROOT.parent/'Regicide'

def git(root,*args):
    return subprocess.check_output(['git',*args],cwd=root).decode('utf-8').strip()

def inventory(directory):
    return {p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}

def main():
    head=git(ROOT,'rev-parse','HEAD')
    assert git(PRIMARY,'rev-parse','HEAD')==head
    assert git(ROOT,'status','--porcelain')==git(PRIMARY,'status','--porcelain')==''
    remote=git(ROOT,'ls-remote','origin','refs/heads/feat/endgame-learning-scale').split()[0]
    assert remote==head
    tested_source=git(ROOT,'rev-parse','43ee008')
    changes=git(ROOT,'diff','--name-only',tested_source,head).splitlines()
    assert all(name=='README.md' or name.startswith('docs/') for name in changes), changes
    for root in (ROOT,PRIMARY):
        assert git(root,'rev-parse','--abbrev-ref','--symbolic-full-name','@{upstream}')=='origin/feat/endgame-learning-scale'
    assert git(PRIMARY,'rev-parse','desktop/neural-inference-opt')=='527289e74dbdfb28a98625ef961c7470e9c78ff9'
    work=ROOT/'build/endgame-learning-scale'
    installed=json.loads((work/'installation.json').read_text(encoding='utf-8-sig'))
    assert inventory(PRIMARY/'build/learning-cycle')==installed['installed_state_all_sha256']
    assert inventory(ROOT/'build/learning-cycle')==installed['installed_state_all_sha256']
    assert inventory(PRIMARY/'build/v064-before-endgame-learning-scale/learning-cycle')==installed['rollback_state_all_sha256']
    assert hashlib.sha256((PRIMARY/'build/regicide.exe').read_bytes()).hexdigest()==installed['installed_binary_sha256']
    assert not (PRIMARY/'build/learning-cycle/champion.json').exists()
    latest=json.loads((PRIMARY/'build/learning-cycle/latest.json').read_text(encoding='utf-8'))
    assert latest['status']=='complete' and latest['attempt_index']==6 and latest['adopted'] is False
    assert not json.loads((work/'power-lease.json').read_text())['active']
    result={'status':'passed','checked_utc':datetime.now(timezone.utc).isoformat(),
            'research_head':head,'installed_head':head,'remote_head':remote,
            'both_working_trees_clean':True,'installed_version':'0.6.5',
            'ci_tested_source_commit':tested_source,'subsequent_changes_only_documentation_and_evidence':True,
            'installed_binary_sha256':installed['installed_binary_sha256'],
            'complete_state_files_verified':len(installed['installed_state_all_sha256']),
            'rollback_state_files_verified':len(installed['rollback_state_all_sha256']),
            'previous_branch_preserved':True,'candidate_adopted':False,
            'default_evaluation':'classical','temporary_sleep_prevention_ended':True}
    (ROOT/'build/final-handoff-study6.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    main()
