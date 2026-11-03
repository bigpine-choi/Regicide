from pathlib import Path
import json,sys,hashlib,datetime
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'scripts'))
import chess
snapshot_dir=Path(__file__).resolve().parent/'final-source-snapshot'
sys.path.insert(0,str(snapshot_dir))
import sparring_play
import sparring_material
import pending_exchange
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


def normalized(value):
 if isinstance(value,dict):return {k:normalized(v) for k,v in value.items() if k!='elapsed_ms' and not (k=='pending_exchange' and v is None)}
 if isinstance(value,list):return [normalized(v) for v in value]
 return value

def independent_gain(board,square):
 own=board.turn;values=sparring_material.VALUES;nodes=0
 def material(pos):return sum(values[p.piece_type]*(1 if p.color==own else -1) for p in pos.piece_map().values())
 initial=material(board)
 def solve(pos):
  nonlocal nodes
  nodes+=1
  assert nodes<=2048,'independent exchange audit limit'
  options=[material(pos)-initial]
  for move in list(pos.legal_moves):
   if move.to_square!=square or not pos.is_capture(move):continue
   pos.push(move)
   try:options.append(solve(pos))
   finally:pos.pop()
  return max(options) if pos.turn==own else min(options)
 return solve(board.copy(stack=False)),nodes

def check_see(board,move,natural):
 strict=natural.get('strict_naturalness') or natural;after=board.copy(stack=True);after.push(move)
 for row in strict.get('vulnerable_captures',[]):
  exchange=row.get('recapture_exchange')
  assert exchange is not None
  assert 0<=exchange['nodes']<=exchange['node_cap']<=1024
  assert 0<=exchange['cache_entries']<=exchange['nodes'] and exchange['time_cap_ms']<=100
  if exchange['complete']:
   branch=after.copy(stack=True);reply=chess.Move.from_uci(row['reply_uci']);assert reply in branch.legal_moves;branch.push(reply)
   gain,nodes=independent_gain(branch,reply.to_square)
   assert gain==exchange['gain_cp']==row['recapture_cp'],'recursive exchange differs from independent material minimax'
  else:
   assert exchange['gain_cp']==0 and exchange['exhausted'] is True

def check_pending(board,move,natural):
 proof=natural.get('pending_exchange')
 if proof is None:return
 assert proof['kind']=='pending_equal_exchange' and 2<=len(board.move_stack)<40
 assert board.root().fen(en_passant='fen')==chess.STARTING_FEN
 assert not board.is_check() and not board.is_capture(move) and not move.promotion and not board.gives_check(move)
 before=board.copy(stack=True);declined=before.pop();captured=before.pop()
 piece=before.piece_at(captured.from_square);victim=before.piece_at(captured.to_square)
 assert before.is_capture(captured) and not captured.promotion and piece.color==board.turn
 assert piece.piece_type not in (chess.PAWN,chess.KING)
 assert sparring_material.VALUES[victim.piece_type]>=sparring_material.VALUES[piece.piece_type]
 assert proof['capture_uci']==captured.uci() and proof['declined_reply_uci']==declined.uci()
 assert proof['capture_ply']==len(board.move_stack)-1 and proof['piece']==piece.symbol()
 square=captured.to_square
 assert proof['piece_square']==chess.square_name(square) and move.from_square!=square
 assert board.piece_at(square)==piece
 before.push(captured)
 assert declined in before.legal_moves and not before.is_capture(declined) and not declined.promotion
 original={reply.uci() for reply in before.legal_moves if reply.to_square==square and before.is_capture(reply)}
 existing=board.copy(stack=False);existing.turn=not board.turn;existing.ep_square=None
 existing_replies={reply.uci() for reply in existing.legal_moves if reply.to_square==square and existing.is_capture(reply)}
 assert proof['original_legal_recaptures']==sorted(original)
 after=board.copy(stack=True);after.push(move);assert after.piece_at(square)==piece
 rows=natural['strict_naturalness']['vulnerable_captures'];assert rows
 assert proof['permitted_replies']==sorted(row['reply_uci'] for row in rows)
 credit=sparring_material.VALUES[piece.piece_type]
 assert proof['maximum_exchange_credit_cp']==credit and proof['captured_value_cp']==sparring_material.VALUES[victim.piece_type]
 for row in rows:
  assert row['reply_uci'] in original & existing_replies
  assert chess.Move.from_uci(row['reply_uci']).to_square==square
  assert row['victim_cp']==credit and row['net_material_cp']>=-credit
  assert row['recapture_exchange']['complete'] is True

report={'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'mode':'read-only native trace / position / naturalness cross-check without engines','auditor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'audited_source_sha256':json.loads((snapshot_dir/'manifest.json').read_text(encoding='utf-8'))['source_sha256'],'games':[]}
for folder in sorted(BASE.iterdir()):
 if not folder.is_dir() or not folder.name.startswith('pair3-') or not (folder/'moves.jsonl').exists():continue
 game_path=folder/'game.json';game=json.loads(game_path.read_text(encoding='utf-8')) if game_path.exists() else None
 metadata=json.loads((folder/'metadata.json').read_text(encoding='utf-8'));identity=metadata['binary_sha256']['stockfish']
 for name in ['sparring_play.py','sparring_material.py','pending_exchange.py','sparring_balance.py','sparring_quality.py']:
  assert hashlib.sha256((snapshot_dir/name).read_bytes()).hexdigest()==metadata['source_sha256']['scripts/'+name]
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
    natural=pending_exchange.naturalness(board,chess.Move.from_uci(uci))
    recorded_natural=(q.get('final_verdict') or {}).get('naturalness')
    if recorded_natural is not None:
     assert normalized(recorded_natural)==normalized(natural),'final quality naturalness differs from recomputation'
     check_pending(board,chess.Move.from_uci(uci),natural)
     check_see(board,chess.Move.from_uci(uci),natural)
    if balanced or mode=='reference_confirmed_native_candidate':
     assert natural['admitted'] is True and not natural['material_return_exception']
     if natural.get('pending_exchange') is None:assert not exposure(board,chess.Move.from_uci(uci))
     saved=attempt.get('naturalness') if balanced else attempt['reference_confirmation']['naturalness']
     assert normalized(saved)==normalized(natural),'naturalness receipt differs from recomputation'
     check_pending(board,chess.Move.from_uci(uci),natural)
     check_see(board,chess.Move.from_uci(uci),natural)
     if balanced:
      balance=q['balance'];assert cp==balance['selected_child_cp_own']
      assert (abs(cp)<=80)==(balance['status']=='met')
      old=balance['original_child_cp_own']
      if old is not None:assert abs(cp-30)<abs(old-30)
     special.append({'ply':move['ply'],'uci':uci,'san':move['san'],'mode':q['status'] if balanced else mode,'native_depth':evidence['depth'],'child_cp_own':cp,'pending_equal_exchange':natural.get('pending_exchange') is not None})
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
  for key,field in [('regicide','binary'),('stockfish','stockfish')]:
   assert hashlib.sha256(Path(metadata['config'][field]).read_bytes()).hexdigest()==metadata['binary_sha256'][key]
  assert all(e['configured_options'].get('Hash')==16 and e['configured_options'].get('UsePieceActivity') is True for e in game['engines'])
 report['games'].append({'game':folder.name,'finished_artifact':game is not None,'completed':game.get('completed') if game else None,'plies_checked':len(moves),'applied_native_checks':checks,'special_elective_or_confirmed':special,'issues':issues,'game_sha256':hashlib.sha256(game_path.read_bytes()).hexdigest() if game else None,'binary_sha256_verified':metadata['binary_sha256'] if game else None,'native_engine_options':[e['configured_options'] for e in game['engines']] if game else None})
report['provisional']=any(not g['finished_artifact'] for g in report['games']) or len(report['games'])!=2
report['passed']=all(not g['issues'] for g in report['games'])
(OUT/'final-native-cross-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
print(json.dumps({'provisional':report['provisional'],'passed':report['passed'],'games':[{k:v for k,v in g.items() if k not in ['applied_native_checks','game_sha256']} for g in report['games']]},ensure_ascii=False))
