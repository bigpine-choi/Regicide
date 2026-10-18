import datetime, json, os, subprocess, sys, time
from pathlib import Path
root=Path.cwd()
sys.path.insert(0,str(root/'scripts'))
import learning_cycle as cycle
import learning_arena as arena
out=root/'build/neural-cost-quality'
state=root/'build/learning-cycle'
marker=out/'allow-measurement.flag'
original=arena.run_arena
def gated_arena(*args,**kwargs):
    print('PREPARATION COMPLETE; WAITING FOR CLEAN MEASUREMENT WINDOW',flush=True)
    (out/'measurement-ready.json').write_text(json.dumps({'pid':os.getpid(),'ready':True,
        'utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}),encoding='utf-8')
    deadline=time.monotonic()+3600
    while not marker.exists():
        if time.monotonic()>deadline:
            raise RuntimeError('Clean measurement window was not provided; incumbent retained')
        time.sleep(1)
    print('CLEAN WINDOW RELEASED; STARTING UNCHANGED PROMOTION ARENA',flush=True)
    return original(*args,**kwargs)
arena.run_arena=gated_arena
source=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
binary=root/'build-neural-v3/regicide.exe'
old_model=root/'docs/checks/2026-10-19-neural-v2/store/runs/000003-2daceeed6656/arena/candidate.nn'
power=out/'power-state.json'
try:
    with cycle.KeepAwake():
        power.write_text(json.dumps({'active':True,'pid':os.getpid(),'source_commit':source}),encoding='utf-8')
        code=cycle.main(['--binary',str(binary),'--state',str(state),'--rounds','1'])
        if code: raise SystemExit(code)
        latest=json.loads((state/'latest.json').read_text(encoding='utf-8'))
        run=state/latest['run']
        manifests=[state/'bootstrap/manifest.json',run/'corpus/manifest.json']
        catalog=json.loads((state/'catalog.json').read_text(encoding='utf-8'))
        manifests.extend(state/item['manifest'] for item in catalog['datasets'])
        args=[sys.executable,'scripts/diagnose_neural_cost.py',str(binary),'--model',str(old_model),
              '--source-commit',source,'--output',str(out/'diagnostic'),
              '--depth','3','--nodes','10000','--movetime-ms','100','--repeats','6']
        for manifest in manifests: args+=['--exclude-manifest',str(manifest)]
        print('STARTING PREDECLARED COST/DECISION DIAGNOSTIC OF THE OLD V2',flush=True)
        subprocess.run(args,check=True)
        print('EXPERIMENTS COMPLETE; SLEEP REQUEST WILL BE RESTORED',flush=True)
finally:
    power.write_text(json.dumps({'active':False,'pid':os.getpid(),'source_commit':source,
                                'restoration_attempted':True}),encoding='utf-8')
