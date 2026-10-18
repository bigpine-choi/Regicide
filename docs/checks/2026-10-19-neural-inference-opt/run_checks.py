import json, os, subprocess, sys, time
from pathlib import Path
root = Path.cwd()
out = root / 'build/neural-inference-opt'
binary = 'build-neural-v4/regicide.exe'
probe = 'build-neural-v4/regicide_evaluation_probe.exe'
commands = [("teacher_quality",["tests/teacher_quality_tests.py"]),("diagnostic",["tests/neural_cost_diagnostic_tests.py"]),("neural_benchmark",["tests/neural_inference_benchmark_tests.py"]),("loss_diagnosis",["tests/loss_diagnosis_tests.py","--binary",binary,"--probe",probe]),
 ('learning_data', ['tests/learning_data_tests.py']),
 ('neural_training', ['tests/neural_training_tests.py']),
 ('classical_evaluation', ['tests/classical_evaluation_tests.py','--probe',probe]),
 ('learning_loss', ['tests/learning_loss_analysis_tests.py']),
 ('learning_corpus', ['tests/learning_corpus_tests.py']),
 ('learning_arena', ['tests/learning_arena_tests.py']),
 ('learning_registry', ['tests/learning_registry_tests.py']),
 ('learning_cycle', ['tests/learning_cycle_tests.py']),
 ('learning_runtime', ['tests/learning_runtime_tests.py','--binary',binary]),
 ('neural_integration', ['tests/neural_integration_tests.py','--binary',binary]),
 ('rules_differential', ['tests/differential.py',binary]),
 ('uci', ['tests/engine_integration.py',binary]),
 ('human_play', ['tests/human_play_tests.py',binary]),
 ('hint_state', ['tests/hint_state_tests.py',binary]),
 ('handicap', ['tests/handicap_play_tests.py',binary]),
 ('archive', ['tests/game_archive_tests.py',binary]),
 ('bootstrap', ['tests/godot_bootstrap_tests.py',binary]),
 ('game_runner', ['tests/game_runner_tests.py',binary]),
 ('compare', ['tests/compare_engines_tests.py',binary]),
 ('benchmark', ['tests/benchmark_search_tests.py',binary]),
]
results=[]
for name, args in commands:
    print('START',name,flush=True)
    start=time.monotonic()
    with (out/(name+'.log')).open('w',encoding='utf-8',newline='\n') as log:
        result=subprocess.run([sys.executable,*args],cwd=root,stdout=log,stderr=subprocess.STDOUT,
                              env={**os.environ,'PYTHONUTF8':'1'})
    results.append({'check':name,'returncode':result.returncode,'seconds':time.monotonic()-start})
    (out/'python-checks.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('PASS' if not result.returncode else 'FAIL',name,flush=True)
    if result.returncode: raise SystemExit(result.returncode)
