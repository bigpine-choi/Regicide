from pathlib import Path
import json,sys,hashlib,datetime
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'scripts'))
import chess
import importlib.util
snapshot=Path(__file__).resolve().parent/"v3_sparring_play_snapshot.py"
spec=importlib.util.spec_from_file_location("v3_frozen_sparring_play",snapshot)
sparring_play=importlib.util.module_from_spec(spec);spec.loader.exec_module(sparring_play)
from audit_sparring_validation import exposure
BASE=ROOT/'docs/matches/2026-11-04-early-balance-v3'
OUT=Path(__file__).resolve().parent

def rows(path):
 out=[]
 if not path.exists():return out
 for line in path.read_text(encoding='utf-8').splitlines():
  try:out.append(json.loads(line))
  except json.JSONDecodeError:break
 return out

def native_searches(path):
 active={};done=[]
 for event in rows(path):
  key=(event['engine'],event['search'])
  if event['event']=='search':active[key]={**event,'packets':[]}
  elif event['event']=='packet' and key in active:active[key]['packets'].append(event['packet'])
  elif event['event']=='bestmove' and key in active:done.append({**active[key],'returned':event['move']})
 return done

def packet_proven(search,evidence,board,uci):
 if not isinstance(evidence,dict) or evidence.get('root')!=uci:return False
 for packet in search['packets']:
  score=packet.get('score') or {};pv=packet.get('pv')
  if (packet.get('depth')!=evidence.get('depth') or packet.get('lowerbound') or packet.get('upperbound') or not pv
    or pv!=evidence.get('pv_uci') or pv[0]!=uci or score.get('cp_white')!=evidence.get('cp')
    or score.get('mate_white')!=evidence.get('mate')):continue
  test=board.copy(stack=True)
  try:
   for move in pv:test.push_uci(move)
  except ValueError:continue
  return True
 return False

def trace_matches(searches,board,uci,evidence,allowed):
 return [s for s in searches if s['fen']==board.fen(en_passant='fen') and s['history_uci']==[m.uci() for m in board.move_stack]
  and s['ply']==len(board.move_stack) and s['returned']==uci and set(s.get('root_moves') or [])==set(allowed)
  and packet_proven(s,evidence,board,uci)]

def external(assessment,board,uci,identity):
 child=board.copy(stack=True);child.push_uci(uci)
 assert assessment['status']=='ready' and assessment['fen']==board.fen(en_passant='fen')
 assert assessment['selected_uci']==uci and assessment['history_uci']==[m.uci() for m in board.move_stack]
 assert assessment['binary_sha256']==identity
 for key,pos in [('root',board),('child',child)]:
  p=assessment[key];assert p['fen']==pos.fen(en_passant='fen')
  if p.get('terminal'):
   assert pos.outcome(claim_draw=True) is not None
  else:
   assert p['bound']=='exact' and p['depth']>=10
   replay=pos.copy(stack=True)
   for move in p['pv_uci']:replay.push_uci(move)
 return assessment['child'].get('cp_own')

report={'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'mode':'read-only native trace / position / naturalness cross-check without engines','auditor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'audited_source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in ['scripts/sparring_play.py','scripts/sparring_quality.py','scripts/sparring_balance.py']},'games':[]}
for folder in sorted(BASE.iterdir()):
 if not folder.is_dir() or not (folder/'moves.jsonl').exists():continue
 game_path=folder/'game.json';game=json.loads(game_path.read_text(encoding='utf-8')) if game_path.exists() else None
 metadata=json.loads((folder/'metadata.json').read_text(encoding='utf-8'));identity=metadata['binary_sha256']['stockfish']
 assert hashlib.sha256(snapshot.read_bytes()).hexdigest()==metadata['source_sha256']['scripts/sparring_play.py']
 moves=game['moves'] if game else rows(folder/'moves.jsonl');searches=native_searches(folder/'native-searches.jsonl')
 board=chess.Board(game['initial_fen'] if game else chess.STARTING_FEN);checks=[];issues=[];special=[]
 for move in moves:
  try:
   assert move['before_fen']==board.fen(en_passant='fen');assert move['ply']==len(board.move_stack)+1
   uci=move['uci'];assert chess.Move.from_uci(uci) in board.legal_moves
   d=move.get('sparring_decision') or {};q=d.get('quality_guard') or {}
   if q.get('applied'):
    assert q['before_fen']==board.fen(en_passant='fen') and q['history_uci']==[m.uci() for m in board.move_stack]
    assert q['selected_uci']==uci and q['original_uci']!=uci
    balanced=q['status'] in ['balanced','balance_improved']
    attempts=(q.get('balance') or {}).get('attempts',[]) if balanced else q.get('attempts',[])
    chosen=[a for a in attempts if (a.get('native_evidence') or {}).get('root')==uci]
    assert chosen,'missing selected native evidence'
    attempt=chosen[-1];evidence=attempt['native_evidence'];discovery=attempt.get('discovery_evidence') or evidence
    allowed=[uci] if balanced else attempt['allowed_uci'];assert set(allowed)<={m.uci() for m in board.legal_moves}
    assert trace_matches(searches,board,uci,discovery,allowed),'native discovery trace does not match'
    promoted=attempt.get('native_promotion')
    if promoted:assert trace_matches(searches,board,uci,evidence,[uci]),'native promotion trace does not match'
    else:assert evidence==discovery
    expected=evidence['cp']*(1 if board.turn else -1) if evidence.get('cp') is not None else None
    if expected is not None:assert d['selected_cp_own']==expected,'external score substituted for native score'
    assert q['initial']['binary_sha256']==identity
    cp=external(attempt['assessment'],board,uci,identity)
    mode=attempt.get('evidence_kind','native_depth_four')
    if mode=='reference_confirmed_native_candidate':
     confirm=attempt['reference_confirmation'];assert confirm['verified'] is True
     assert evidence['depth']>=2 and discovery['depth']>=2 and len(allowed)>=2
     assert abs(evidence['cp']-discovery['cp'])<=40
     second=external(confirm['assessment'],board,uci,identity)
     assert abs(cp)<=80 and abs(second)<=80 and abs(second-cp)<=60
     assert q['initial_verdict']['acceptable'] is False and q['initial_verdict']['child_cp_own']< -100
     assert len(board.move_stack)<40 and board.root().fen(en_passant='fen')==chess.STARTING_FEN
     assert promoted['status']=='reference_confirmed'
    else:assert evidence['depth']>=4
    natural=sparring_play.naturalness(board,chess.Move.from_uci(uci))
    if balanced or mode=='reference_confirmed_native_candidate':
     assert natural['admitted'] is True and not natural['material_return_exception']
     assert not exposure(board,chess.Move.from_uci(uci))
     saved=attempt.get('naturalness') if balanced else attempt['reference_confirmation']['naturalness']
     assert saved==natural,'strict naturalness receipt differs from recomputation'
     if balanced:
      balance=q['balance'];assert cp==balance['selected_child_cp_own']
      assert (abs(cp)<=80)==(balance['status']=='met')
      old=balance['original_child_cp_own']
      if old is not None:assert abs(cp-30)<abs(old-30)
     special.append({'ply':move['ply'],'uci':uci,'san':move['san'],'mode':q['status'] if balanced else mode,'native_depth':evidence['depth'],'child_cp_own':cp})
    checks.append({'ply':move['ply'],'uci':uci,'mode':mode,'status':q['status'],'native_depth':evidence['depth'],'native_origin_proven':True,'strict_naturalness':natural['admitted'] if balanced or mode=='reference_confirmed_native_candidate' else None})
   board.push_uci(move['uci']);assert move['after_fen']==board.fen(en_passant='fen')
  except (AssertionError,KeyError,ValueError,TypeError) as exc:
   issues.append({'ply':move['ply'],'san':move['san'],'error':str(exc),'type':type(exc).__name__})
   if len(board.move_stack)<move['ply']:
    try:board.push_uci(move['uci'])
    except ValueError:break
 if game is not None:
  assert moves==rows(folder/'moves.jsonl')
  assert metadata['sources_unchanged'] and metadata['binaries_unchanged']
 report['games'].append({'game':folder.name,'finished_artifact':game is not None,'completed':game.get('completed') if game else None,'plies_checked':len(moves),'applied_native_checks':checks,'special_elective_or_confirmed':special,'issues':issues,'game_sha256':hashlib.sha256(game_path.read_bytes()).hexdigest() if game else None})
report['provisional']=any(not g['finished_artifact'] for g in report['games']) or len(report['games'])!=4
report['passed']=all(not g['issues'] for g in report['games'])
(OUT/'v3-native-cross-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
print(json.dumps({'provisional':report['provisional'],'passed':report['passed'],'games':[{k:v for k,v in g.items() if k not in ['applied_native_checks','game_sha256']} for g in report['games']]},ensure_ascii=False))
