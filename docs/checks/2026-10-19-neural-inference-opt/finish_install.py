import hashlib
import json
from pathlib import Path
import subprocess
import sys
import datetime

root=Path.cwd()
primary=root.parent/'Regicide'
out=root/'build/neural-inference-opt'
expected='7ee14504ea9c87fff6cef8731c86d97f23efa4b7'
new_sha='ff63e5c1bb1aaa863f6bdaa19bfaf61c660350b88a702916b40372249f22f4b1'
old_sha='605681320aff062ba2878eece74f453bf6ee451f1faa223e74a18caece328e5a'
def git(*args):
    return subprocess.check_output(['git','-C',str(primary),*args],text=True).strip()
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
assert git('rev-parse','HEAD')==expected
assert not git('status','--porcelain','--untracked-files=normal')
assert git('branch','--show-current')=='desktop/neural-inference-opt'
assert git('rev-parse','origin/perf/neural-evaluation-cost')==expected
assert sha(primary/'build/regicide.exe')==new_sha
assert sha(primary/'build/v063-before-neural-inference-opt/regicide.exe')==old_sha
assert git('rev-parse',':tests/data/neural_diagnostic_positions_v1.json')==git('rev-parse','HEAD:tests/data/neural_diagnostic_positions_v1.json')
subprocess.run([sys.executable,str(out/'check_protection.py'),'--installed-sha256',new_sha,'--primary-head',expected,'--output',str(out/'protection-after-install.json')],check=True)
with (out/'installed-runtime.log').open('w',encoding='utf-8') as log:
    subprocess.run([sys.executable,'tests/learning_runtime_tests.py','--binary',str(primary/'build/regicide.exe')],stdout=log,stderr=subprocess.STDOUT,check=True)
assert subprocess.check_output([str(primary/'build/regicide.exe'),'--help'],text=True).splitlines()[0]=='Regicide 0.6.4'
record={'checked_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'previous_commit':'00dec89b08de4e5f573b18b440a93cd82f4b3311',
 'previous_branch':'desktop/neural-cost-quality',
 'installed_commit':expected,'remote_commit':expected,
 'installed_branch':git('branch','--show-current'),
 'upstream':git('rev-parse','--abbrev-ref','--symbolic-full-name','@{upstream}'),
 'baseline_binary_sha256':old_sha,'installed_binary_sha256':new_sha,
 'rollback_binary':'build/v063-before-neural-inference-opt/regicide.exe',
 'learning_state_copied_or_modified':False,'learning_attempt_advanced':False,
 'model_promoted':False,'working_tree_clean':not git('status','--porcelain'),
 'repaired_preexisting_fixture_line_endings':'tests/data/neural_diagnostic_positions_v1.json',
 'first_helper_stop':'After copying canonical LF fixture the Git stat cache reported modified although HEAD/index/worktree object IDs were identical. Refreshed only that identical index entry with git add; verified no staged or working changes, then resumed verification.',
 'installed_runtime_suite_exit':0,'manual_gui_game_claimed':False}
(out/'installation.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
print(json.dumps(record,indent=2))
