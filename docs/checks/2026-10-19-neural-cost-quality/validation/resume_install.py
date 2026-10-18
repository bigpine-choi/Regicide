import datetime,hashlib,json,subprocess
from pathlib import Path
root=Path.cwd().resolve(); primary=Path('<INSTALLED_REPO>').resolve()
out=root/'build/neural-cost-quality'; build=primary/'build'; source=root/'build/learning-cycle'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def inventory(p): return {str(f.relative_to(p)):sha(f) for f in p.rglob('*') if f.is_file()}
old=build/'learning-cycle'; backup=build/'learning-cycle-v062-before-v063'
stage=build/'learning-cycle-v063-staging-9881183acf30'
for p in (old,backup,stage):
 assert p.resolve().is_relative_to(build.resolve())
assert not backup.exists() and stage.is_dir()
assert inventory(old)==json.loads((out/'installed-state-before.json').read_text())
expected=inventory(source); assert inventory(stage)==expected
latest=json.loads((source/'latest.json').read_text()); assert latest['status']=='complete'
assert sha(build/'regicide.exe')==latest['binary_sha256']
assert sha(build/'v062-before-neural-cost-quality/regicide.exe')=='ec71a626b8124359fe2e47ab2876e9ea0bc342d42179869158c5aeda63fd921f'
old.rename(backup)
try: stage.rename(old)
except BaseException:
 backup.rename(old)
 raise
assert inventory(old)==expected
head=subprocess.check_output(['git','-C',str(primary),'rev-parse','HEAD'],text=True).strip()
report={'version':'0.6.3','installed_at_kst':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(),
 'source_commit':head,'engine_sha256':sha(build/'regicide.exe'),'primary':str(primary),
 'branch':'desktop/neural-cost-quality','previous_branch_preserved':'desktop/neural-residual-improvement',
 'previous_engine_backup':'build/v062-before-neural-cost-quality/regicide.exe',
 'previous_state_backup':'build/learning-cycle-v062-before-v063',
 'learning_state_files':len(expected),'learning_state_byte_identical':True,
 'attempts_started':json.loads((old/'catalog.json').read_text())['attempts_started'],
 'adopted_in_validation':latest['adopted'],'default_evaluation':'classical',
 'installation_note':'Windows refused os.replace of the executable despite no running engine and write permissions. Native PowerShell Copy-Item succeeded; binary SHA verified before state replacement. No ACL or security settings changed.'}
(out/'deployment.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report,indent=2))
