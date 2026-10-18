import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path.cwd()
OUT = ROOT / 'build/neural-inference-opt'
sys.path.insert(0, str(ROOT / 'scripts'))
import benchmark_neural_inference as paired
from learning_arena import BROWSER_NAMES, utc_now, write_json
from learning_cycle import KeepAwake
from see_clean_guard import EngineCpuGuard, WindowsProcesses

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

source = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
paths = {
 'learned_v1': ROOT/'models/regicide-small-v1.nn',
 'previous_v2': ROOT/'docs/checks/2026-10-19-neural-cost-quality/diagnostic/learned-v2.nn',
 'quality_v2': ROOT/'docs/checks/2026-10-19-neural-cost-quality/run/model/small-v2.nn',
 'zero_v2': ROOT/'docs/checks/2026-10-19-neural-cost-quality/diagnostic/zero-v2.nn',
}
snapshots = OUT/'models'
snapshots.mkdir(exist_ok=False)
models={}
for name,path in paths.items():
    target=snapshots/(name+'.nn')
    target.write_bytes(path.read_bytes())
    models[name]=target
profile = ROOT/'build-neural-v4/regicide_profile_neural.exe'
fen = ROOT/'tests/data/evaluation_profile_2026_10_18.fens'
metadata={'source_commit':source,'baseline_commit':'00dec89b08de4e5f573b18b440a93cd82f4b3311',
 'profile_binary_sha256':sha(profile),'fen_sha256':sha(fen),
 'models':{name:{'source':str(paths[name]),'snapshot':str(path),'sha256':sha(path)} for name,path in models.items()},
 'compiler':'MSVC /std:c++20 /O2 /DNDEBUG /GL- for profile/reference, default precise FP; separate translation units without LTO',
 'sources':{str(path.relative_to(ROOT)):sha(path) for path in [ROOT/'src/neural.cpp',ROOT/'include/regicide/neural.hpp',ROOT/'tests/reference_neural_v063.cpp',ROOT/'tests/reference_neural_v063.hpp',ROOT/'scripts/profile_neural.cpp']}}
write_json(OUT/'performance-metadata.json',metadata)
power={'active':False}
try:
    with KeepAwake():
        power={'active':True,'started_utc':utc_now(),'pid':os.getpid()}
        write_json(OUT/'power-state.json',power)
        guard=EngineCpuGuard(WindowsProcesses(engine_names={'regicide.exe','stockfish.exe','lc0.exe'}|BROWSER_NAMES),OUT/'profile-resource-samples.jsonl',interval=.5)
        try:
            guard.start()
            guard.require_idle(5)
            command=[str(profile),'--fen-file',str(fen),'--output',str(OUT/'profile.json')]
            for path in models.values(): command.extend(['--model',str(path)])
            with (OUT/'profile-console.log').open('w',encoding='utf-8') as log:
                process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
                guard.register_transport(process.pid,'baseline',command)
                code=process.wait()
            guard.assert_clean()
            if code: raise RuntimeError('profile exit '+str(code))
            if len(guard.known_owned)!=1: raise RuntimeError('profile must have exactly one monitored process')
            guard.summary['valid_for_comparison']=True
        finally:
            guard.close()
            if guard.summary['contaminated']: guard.summary['valid_for_comparison']=False
            write_json(OUT/'profile-resource-guard.json',guard.summary)
        if not guard.summary['valid_for_comparison']: raise RuntimeError('profile guard invalid')
        subprocess.run([sys.executable,'scripts/profile_neural_audit.py',str(OUT/'profile.json'),'--output',str(OUT/'profile-audit.json')],check=True)
        args=[str(ROOT/'build-neural-v3/regicide.exe'),str(ROOT/'build-neural-v4/regicide.exe'),
              '--baseline-commit',metadata['baseline_commit'],'--candidate-commit',source,
              '--output',str(OUT/'paired')]
        for name,path in models.items(): args.extend(['--model',name,str(path)])
        paired.run(paired.arguments(args))
finally:
    power.update(active=False,finished_utc=utc_now())
    write_json(OUT/'power-state.json',power)
print('ALL PERFORMANCE MEASUREMENTS COMPLETE',flush=True)
