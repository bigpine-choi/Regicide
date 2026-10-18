import hashlib,json,os,re,subprocess,sys
from pathlib import Path
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'): os.environ[n]='1'
root=Path(__file__).resolve().parents[2]; sys.path.insert(0,str(root/'scripts'))
import chess
from neural_model import load_model,evaluate_cp
state=root/'build/learning-cycle'; latest=json.loads((state/'latest.json').read_text()); run=state/latest['run']; training=json.loads((run/'model/report.json').read_text())
modelpath=run/'model'/training['model_path']; model=load_model(modelpath); binary=root.parent/'Regicide/build/regicide.exe'
rows=[json.loads(x) for x in (run/'data/positions.jsonl').read_text().splitlines()]; boards=[chess.Board(rows[i*(len(rows)-1)//31]['fen']) for i in range(32)]
boards += [chess.Board('7k/8/8/BB6/NN6/RR6/Q1QQ4/KQQQQQQ1 w - - 0 1'),chess.Board()]
checks=[]
for board in boards:
 for shield,structure in ((True,True),(True,False),(False,True),(False,False)):
  args=[str(binary),'eval','--eval-file',str(modelpath),'--fen',board.fen(en_passant='fen')]
  if not shield: args.append('--no-pawn-shield')
  if not structure: args.append('--no-pawn-structure')
  p=subprocess.run(args,check=True,capture_output=True,text=True,timeout=30)
  actual=int(re.search(r'score_cp: (-?\d+)',p.stdout).group(1)); expected=evaluate_cp(board,model,shield,structure)
  assert abs(actual-expected)<=1,(board.fen(),shield,structure,actual,expected)
  checks.append(abs(actual-expected))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(modelpath)==latest['candidate_sha256']
report={'status':'passed','positions':len(boards),'option_pairs':4,'comparisons':len(checks),'max_cp_difference':max(checks),'model_sha256':sha(modelpath),'installed_binary_sha256':sha(binary),'scope':'Actual selected V2 model static Python/native parity only; not playing strength.'}
(root/'build/neural-improvement-diagnosis/installed-trained-model-parity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(report)
