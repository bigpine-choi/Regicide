from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
GODOT = Path('C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe')
results = []
with tempfile.TemporaryDirectory(prefix='regicide-opening-sides-ui-') as folder:
    project = Path(folder)/'godot'
    shutil.copytree(ROOT/'godot', project, ignore=shutil.ignore_patterns('.godot', '*.tmp'))
    for label, args in [('import', ['--editor','--headless','--import','--quit']),
                        ('opening-guide-scene', ['--headless','--script','res://tests/opening_guide_tests.gd'])]:
        command = [str(GODOT),'--path',str(project),'--language','en',*args]
        options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        completed = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=100,**options)
        output = completed.stdout+completed.stderr
        (OUT/(label+'.txt')).write_text(output,encoding='utf-8')
        result = {'name':label, 'exit_code':completed.returncode, 'script_error':'SCRIPT ERROR:' in output,
                  'error':'ERROR:' in output,'mode':'headless isolated copied project; no user game, no engine'}
        results.append(result)
        print(json.dumps(result),flush=True)
        assert completed.returncode == 0 and not result['script_error'] and not result['error'], output
(OUT/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
