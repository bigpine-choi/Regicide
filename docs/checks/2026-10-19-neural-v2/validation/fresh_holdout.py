"""Post-selection report on fresh nontraining samples; never a selection signal."""
import os
for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'): os.environ[name]='1'
import hashlib,json,math,sys
from pathlib import Path
import chess
root=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(root/'scripts'))
from neural_model import evaluate_cp,load_model,feature_key
from classical_evaluation import classical_cp_stm
state=root/'build/learning-cycle'; latest=json.loads((state/'latest.json').read_text(encoding='utf-8'))
assert latest['status']=='complete' and latest['attempt_index']>=3
run=state/latest['run']; corpus=json.loads((run/'corpus/manifest.json').read_text(encoding='utf-8'))
report=json.loads((run/'model/report.json').read_text(encoding='utf-8'))
newmodel=run/'model'/report['model_path']; oldmodel=state/'runs/000002-95a3500d5501/model/small-v1.nn'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(newmodel)==report['model_sha256']
assert sha(oldmodel)=='4b9dc188bb289d83f6563e9a996c27eaca4aef886c3421a8125c06dd108c3256'
models={'old_v1':load_model(oldmodel),'new_v2':load_model(newmodel)}
seen=set()
for p in (state/'bootstrap/positions.jsonl',state/'runs/000001-88bbee0e1ae3/data/positions.jsonl',state/'runs/000002-95a3500d5501/data/positions.jsonl',run/'corpus/positions.jsonl'):
 for line in p.read_text(encoding='utf-8').splitlines():
  row=json.loads(line); seen.add(feature_key(chess.Board(row['fen'])))
values=[]; excluded=0
for line in (run/'data/positions.jsonl').read_text(encoding='utf-8').splitlines():
 row=json.loads(line)
 if row['split']=='train': continue
 board=chess.Board(row['fen']); key=feature_key(board)
 if key in seen: excluded+=1; continue
 values.append({'split':row['split'],'turn':row['turn'],'teacher_cp_stm':row['teacher_cp_stm'],'classical':classical_cp_stm(board),**{name:evaluate_cp(board,model) for name,model in models.items()}})
assert values,'No fresh held-out samples remain'
metrics={}
for name in ('classical','old_v1','new_v2'):
 errors=[row[name]-row['teacher_cp_stm'] for row in values]
 metrics[name]={'mae_cp':sum(abs(e) for e in errors)/len(errors),'rmse_cp':math.sqrt(sum(e*e for e in errors)/len(errors)),
  'teacher_only_tanh_mse':sum((math.tanh(row[name]/600)-math.tanh(row['teacher_cp_stm']/600))**2 for row in values)/len(values)}
payload={'schema':'regicide.neural.fresh-reporting-holdout.v1','status':'complete','candidate_sha256':sha(newmodel),'old_model_sha256':sha(oldmodel),'data_sha256':sha(run/'data/positions.jsonl'),'corpus_sha256':sha(run/'corpus/manifest.json'),
 'sample_count':len(values),'excluded_seen_exact_neural_inputs':excluded,'turn_counts':{side:sum(row['turn']==side for row in values) for side in ('w','b')},'metrics':metrics,
 'policy':'New collection validation/test families only; exclude exact neural inputs in any previous labeled source or current corpus. Read only after candidate selection and arena complete. Reporting only, no retraining or tuning follows. Same teacher cp target for all models; no old/new training-loss metric comparison.',
 'limitations':'Finite 10000-node own-engine teacher, small correlated samples, no independent truth or playing-strength claim.'}
output=root/'build/neural-improvement-diagnosis/fresh-holdout.json'; output.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
print(json.dumps(payload,indent=2))
