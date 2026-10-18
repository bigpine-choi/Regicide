import argparse,datetime,hashlib,json,shutil,subprocess,uuid
from pathlib import Path
root=Path.cwd().resolve(); primary=Path('<INSTALLED_REPO>').resolve()
out=root/'build/neural-cost-quality'; state=root/'build/learning-cycle'
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def inventory(path): return {str(p.relative_to(path)):sha(p) for p in path.rglob('*') if p.is_file()}
def git(where,*args): return subprocess.check_output(['git','-C',str(where),*args],text=True).strip()
parser=argparse.ArgumentParser(); parser.add_argument('--install',action='store_true'); args=parser.parse_args()
latest=json.loads((state/'latest.json').read_text(encoding='utf-8'))
diagnostic=json.loads((out/'diagnostic/diagnostic.json').read_text(encoding='utf-8'))
assert latest['status']=='complete' and diagnostic['completed'] and diagnostic['resource_guard']['valid_for_comparison']
assert json.loads((out/'power-state.json').read_text())['active'] is False
assert not git(root,'status','--porcelain') and not git(primary,'status','--porcelain')
head=git(root,'rev-parse','HEAD')
remote=git(root,'ls-remote','origin','refs/heads/feat/neural-cost-quality').split()[0]
assert remote==head
old_state=primary/'build/learning-cycle'
expected=json.loads((out/'installed-state-before.json').read_text(encoding='utf-8'))
assert inventory(old_state)==expected,'Installed learning state changed; do not overwrite'
assert sha(primary/'build/regicide.exe')=='ec71a626b8124359fe2e47ab2876e9ea0bc342d42179869158c5aeda63fd921f'
backup_binary=primary/'build/v062-before-neural-cost-quality'
backup_state=primary/'build/learning-cycle-v062-before-v063'
stage=primary/('build/learning-cycle-v063-staging-'+uuid.uuid4().hex[:12])
for target in (backup_binary,backup_state,stage):
 assert target.resolve().is_relative_to((primary/'build').resolve()) and not target.exists()
assert state.is_relative_to(root) and old_state.resolve().is_relative_to((primary/'build').resolve())
print('Validated deployment target:',primary,'source:',head,'adopted:',latest['adopted'],flush=True)
if not args.install: raise SystemExit(0)
backup_binary.mkdir()
shutil.copy2(primary/'build/regicide.exe',backup_binary/'regicide.exe')
shutil.copytree(state,stage)
new_files=inventory(state)
assert inventory(stage)==new_files
git(primary,'switch','-c','desktop/neural-cost-quality','--track','origin/feat/neural-cost-quality')
new_binary=primary/'build/regicide-v063-staged.exe'
shutil.copy2(root/'build-neural-v3/regicide.exe',new_binary)
assert sha(new_binary)==latest['binary_sha256']
new_binary.replace(primary/'build/regicide.exe')
old_state.rename(backup_state)
try: stage.rename(old_state)
except BaseException:
 backup_state.rename(old_state)
 raise
assert inventory(old_state)==new_files and git(primary,'rev-parse','HEAD')==head
report={'version':'0.6.3','installed_at_kst':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(),
 'source_commit':head,'engine_sha256':sha(primary/'build/regicide.exe'),
 'primary':str(primary),'branch':git(primary,'branch','--show-current'),
 'previous_engine_backup':str(backup_binary.relative_to(primary)),
 'previous_state_backup':str(backup_state.relative_to(primary)),
 'learning_state_files':len(new_files),'learning_state_byte_identical':True,
 'attempts_started':json.loads((old_state/'catalog.json').read_text())['attempts_started'],
 'adopted_in_validation':latest['adopted'],'default_evaluation':'neural' if (old_state/'champion.json').exists() else 'classical'}
(out/'deployment.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report,indent=2))
