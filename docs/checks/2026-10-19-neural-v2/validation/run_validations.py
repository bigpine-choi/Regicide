import json,os,subprocess,sys,time
from pathlib import Path
root=Path.cwd(); out=root/'build/neural-improvement-diagnosis'
commands=[
('learning_data_tests', ['tests/learning_data_tests.py']),
('neural_training_tests',['tests/neural_training_tests.py']),
('classical_evaluation_tests',['tests/classical_evaluation_tests.py','--probe','build-neural-v2/regicide_evaluation_probe.exe','--report',str(out/'classical-native-parity.json')]),
('learning_loss_analysis_tests',['tests/learning_loss_analysis_tests.py']),
('learning_corpus_tests',['tests/learning_corpus_tests.py']),
('learning_arena_tests',['tests/learning_arena_tests.py']),
('learning_registry_tests',['tests/learning_registry_tests.py']),
('learning_cycle_tests',['tests/learning_cycle_tests.py']),
('learning_runtime_tests',['tests/learning_runtime_tests.py','--binary','build-neural-v2/regicide.exe']),
('neural_integration_tests',['tests/neural_integration_tests.py','--binary','build-neural-v2/regicide.exe']),
('human_play_tests',['tests/human_play_tests.py','build-neural-v2/regicide.exe']),
('hint_state_tests',['tests/hint_state_tests.py','build-neural-v2/regicide.exe']),
('handicap_play_tests',['tests/handicap_play_tests.py','build-neural-v2/regicide.exe']),
('game_archive_tests',['tests/game_archive_tests.py','build-neural-v2/regicide.exe']),
('godot_bootstrap_tests',['tests/godot_bootstrap_tests.py','build-neural-v2/regicide.exe']),
('game_runner_tests',['tests/game_runner_tests.py','build-neural-v2/regicide.exe']),
('compare_engines_tests',['tests/compare_engines_tests.py','build-neural-v2/regicide.exe']),
('benchmark_search_tests',['tests/benchmark_search_tests.py','build-neural-v2/regicide.exe'])]
results=[]
for name,args in commands:
 print('START',name,flush=True); start=time.monotonic()
 with (out/(name+'.log')).open('w',encoding='utf-8',newline='\n') as log:
  result=subprocess.run([sys.executable,*args],cwd=root,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1'})
 results.append({'test':name,'returncode':result.returncode,'seconds':time.monotonic()-start})
 print('PASS' if result.returncode==0 else 'FAIL',name,flush=True)
 (out/'python-validation.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
 if result.returncode: raise SystemExit(result.returncode)
print('ALL',len(results),'SUITES PASSED',flush=True)
