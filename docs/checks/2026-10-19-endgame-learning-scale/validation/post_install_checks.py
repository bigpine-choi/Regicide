"""Verify the installed runtime using isolated regression fixtures and read-only state checks."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
PRIMARY = ROOT.parent/'Regicide'
WORK = ROOT/'build/endgame-learning-scale'
PYTHON = PRIMARY/'.venv-tests/Scripts/python.exe'

def inventory(directory):
    return {p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}

def main():
    state = PRIMARY/'build/learning-cycle'
    before = inventory(state)
    result = {'started_utc':datetime.now(timezone.utc).isoformat(), 'checks':[],
              'scope':'Actual installed executable, isolated runtime test fixtures; read-only real registry selection; no manual GUI game or strength claim.'}
    commands = [
        ('installed-runtime', [str(PYTHON),'tests/learning_runtime_tests.py','--binary','build/regicide.exe']),
        ('installed-version', [str(PRIMARY/'build/regicide.exe'),'--help']),
        ('installed-selection', [str(PYTHON),'-c',
            "from pathlib import Path; import sys,json; sys.path.insert(0,'scripts'); from play_human import runtime_evaluation; r=runtime_evaluation(Path('build/regicide.exe').resolve(),learning_state=Path('build/learning-cycle').resolve()); assert r=={'model':None,'generation':None,'warning':None},r; print(json.dumps(r))"]),
    ]
    for name, command in commands:
        executed = subprocess.run(command,cwd=PRIMARY,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                  env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
        log = WORK/(name+'.log')
        log.write_bytes(executed.stdout)
        result['checks'].append({'name':name,'returncode':executed.returncode,'log':log.name})
        assert executed.returncode == 0, executed.stdout.decode('utf-8',errors='replace')
        if name == 'installed-version':
            assert b'0.6.5' in executed.stdout
    after = inventory(state)
    assert before == after, 'Runtime verification changed real learning state'
    result.update(finished_utc=datetime.now(timezone.utc).isoformat(),status='passed',
                  real_learning_state_unchanged=True,state_files=len(after),
                  installed_binary_sha256=hashlib.sha256((PRIMARY/'build/regicide.exe').read_bytes()).hexdigest())
    (WORK/'post-install-checks.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__ == '__main__':
    main()
