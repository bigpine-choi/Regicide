from pathlib import Path
import json, sys, hashlib, datetime
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'scripts'))
import chess
from audit_sparring_validation import Auditor, sha, exposure, own_cp
import sparring_play
OUT=Path(__file__).resolve().parent
BASE=ROOT/'docs/matches/2026-11-04-sparring-quality-v2'
SF=ROOT/'build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe'
CASES=[('1700-black',6,[]),('1700-black',8,[]),('1700-black',10,[]),('1700-black',12,[]),('1700-white-extra',25,['c3d5']),('1700-white',31,['a1a2','h3c8','a1d1'])]
report={'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_game_directory':str(BASE.relative_to(ROOT)),'stockfish':{'binary':str(SF.relative_to(ROOT)),'sha256':sha(SF),'threads':1,'hash_mb':32,'limit_strength':False,'analysis_ms':3000,'position_reset':'new game each position'},'cases':[]}
auditor=Auditor(SF,200000)
try:
 for game_name,ply,alternatives in CASES:
  file=BASE/game_name/'game.json'; game=json.loads(file.read_text(encoding='utf-8')); move=game['moves'][ply-1];d=move['sparring_decision'];guard=d.get('quality_guard') or {}
  board=chess.Board(game['initial_fen'])
  for uci in game['uci_moves'][:ply-1]:board.push_uci(uci)
  assert board.fen(en_passant='fen')==move['before_fen']
  root=auditor.evaluate(board,deep_ms=3000)
  row={'game':game_name,'ply':ply,'game_sha256':sha(file),'history_uci':game['uci_moves'][:ply-1],'before_fen':move['before_fen'],'selected_uci':move['uci'],'selected_san':move['san'],'original_native_baseline':d.get('baseline'),'original_reason':d.get('reason'),'original_quality':{k:guard.get(k) for k in ['status','reason','initial_verdict','final_verdict','budget_ms','elapsed_ms']},'root':root,'root_cp_own':own_cp(root,board.turn),'children':[]}
  for uci in [move['uci']]+alternatives:
   candidate=chess.Move.from_uci(uci);assert candidate in board.legal_moves
   after=board.copy(stack=True);after.push(candidate);score=auditor.evaluate(after,deep_ms=3000)
   child={'uci':uci,'san':board.san(candidate),'kind':'actual_played' if uci==move['uci'] else 'candidate_from_recorded_native_search','score':score,'cp_own':own_cp(score,board.turn),'naturalness':sparring_play.naturalness(board,candidate),'independent_exposure':exposure(board,candidate)}
   row['children'].append(child)
  report['cases'].append(row)
  (OUT/'deep-reproduce.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
  print(game_name,ply,'root',row['root_cp_own'],[(c['san'],c['cp_own'],c['score'].get('mate_white')) for c in row['children']],flush=True)
finally:auditor.close()
report['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();report['engine_closed']=True
(OUT/'deep-reproduce.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
