"""Recheck immutable completed boss-vs-normal match artifacts; no engine search.

Run from any working directory with the project test Python environment.
Generated during independent audit; production source and match harness unchanged.
"""
import json, hashlib, sys, math, time, io
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import chess, chess.pgn, chess.engine
import boss_play, boss_opponent, boss_openings, dynamic_play, opening_variety, poisoned_piece
BASE=ROOT/'build/boss-v-normal-20261102'
FREEZE='dcc40800c9e42f652864dcc145e9a59b46272d2c'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def load(p): return json.loads(p.read_text(encoding='utf-8'))
def req(c,s):
 if not c: raise AssertionError(s)
def packet(p,b,uci=None):
 req(isinstance(p,dict) and type(p.get('depth')) is int and p['depth']>=4,'bad packet depth')
 req(type(p.get('cp')) in (int,float) and math.isfinite(p['cp']),'bad packet score')
 req(p.get('pov')==('white' if b.turn else 'black'),'packet POV')
 req(p.get('cp_white')==p['cp']*(1 if b.turn else -1),'white score sign')
 pv=p['pv_uci']; req(pv and (uci is None or pv[0]==uci),'packet root')
 tmp=b.copy(stack=True)
 for x in pv:
  move=chess.Move.from_uci(x); req(move in tmp.legal_moves,'illegal packet PV'); tmp.push(move)
def run_game(folder,number):
 m=load(folder/'match.json'); p=folder/f'game-{number:03d}.json'; r=load(p)
 req(r.get('finished_at') and r['completed'] and r['legal_result'] and not r['capped'] and r['error'] is None,'not complete legal game')
 req(m['schema']=='regicide.boss-mode-match.v1','schema')
 c=m['config']; req(c['boss_ms']==c['normal_ms']==1000 and c['hash_mb']==16 and c['threads']==1 and c['classical'] is True,'config')
 req(m['source']['commit']==FREEZE and m['source']['dirty'] is False,'source freeze')
 for name,digest in m['source_sha256'].items(): req(sha(ROOT/name)==digest,'source hash '+name)
 for name,digest in m['catalog_sha256'].items(): req(sha(ROOT/name)==digest,'catalog hash '+name)
 req(sha(Path(c['binary']))==m['binary_sha256'],'binary hash')
 e=next(x for x in m['schedule'] if x['game']==number)
 req(all(r[k]==v for k,v in e.items()),'schedule entry')
 req(r['game']==2*(r['pair']-1)+(1 if r['boss_color']=='white' else 2),'color schedule')
 for mode in ('boss','normal'):
  expected=hashlib.sha256(json.dumps([str(c['seed']),'boss-vs-normal',mode,r['pair']]).encode()).hexdigest()[:32]
  req(r[mode+'_seed']==expected,'seed')
  h=r['hosts'][mode]; req(h['uci_moves']==r['uci_moves'] and h['final_fen']==r['final_fen'],'host final')
  req(h['model_sha256'] is None and h['evaluation']=='classical_activity_v1','evaluation')
  for engine in h['engines']:
   opts=engine['configured_options']; req(opts['Hash']==16 and opts['UsePieceActivity'] is True,'options')
   req(not opts.get('UseNeuralEvaluation',False) and not opts.get('UseNNUEEvaluation',False),'neural')
   if 'Threads' in engine['advertised_options']: req(opts.get('Threads')==1,'threads')
  req(bool(h['engines']),'no engine')
  for name,policy in (('boss_play',boss_play.POLICY),('dynamic_play',dynamic_play.POLICY),('opening_variety',opening_variety.POLICY)):
   meta=h[name]; req(meta['policy']==policy and meta['seed']==r[mode+'_seed'],'policy seed')
   req(meta['enabled']==((name=='boss_play' and mode=='boss') or (name=='dynamic_play' and mode=='normal')),'active policy')
   if not meta['enabled']: req(not meta['decisions'],'disabled decisions')
  side=load(folder/h['save_directory']/(h['game_id']+'.json'))
  req(side['source']==m['source'],'sidecar source')
  req(side['uci_moves']==r['uci_moves'] and side['final_fen']==r['final_fen'],'sidecar final')
  for name in ('boss_play','dynamic_play','opening_variety'): req(side[name]==h[name],'sidecar decisions')
  req(side['state']['engine']['boss_mode']==(mode=='boss'),'sidecar boss mode')
 trace=[json.loads(line) for line in (folder/f'game-{number:03d}.moves.jsonl').read_text(encoding='utf-8').splitlines()]
 req(trace==r['moves'],'trace record equality')
 pg=chess.pgn.read_game(io.StringIO((folder/r['pgn_file']).read_text(encoding='utf-8')))
 req(pg and not pg.errors and [x.uci() for x in pg.mainline_moves()]==r['uci_moves'],'PGN moves')
 req(pg.headers['White']=='Regicide '+('Boss' if r['boss_color']=='white' else 'Normal') and pg.headers['Black']=='Regicide '+('Boss' if r['boss_color']=='black' else 'Normal'),'PGN colors')
 req(pg.headers['BossMoveTimeMS']==pg.headers['NormalMoveTimeMS']=='1000','PGN budgets')
 req(pg.headers['BossSeed']==r['boss_seed'] and pg.headers['NormalSeed']==r['normal_seed'],'PGN seeds')
 b=chess.Board(); earlier=[]; seen={'boss':[],'normal':[]}; reasons={'boss':Counter(),'normal':Counter()}
 book=[]; traps=[]; signatures=[]; normal_alternatives=[]; quality=[]; candidate_turns=branches=0; capture_searches=proof_results=0; attempt_reasons=Counter()
 for i,t in enumerate(r['moves']):
  req(t['ply']==i+1 and t['before_fen']==b.fen(en_passant='fen'),'turn position')
  mode='boss' if b.turn==(r['boss_color']=='white') else 'normal'
  req(t['actor']==mode and t['uci']==r['uci_moves'][i],'turn actor')
  move=chess.Move.from_uci(t['uci']); req(move in b.legal_moves and t['san']==b.san(move),'turn legal/SAN')
  req(t['baseline_budget_ms']==1000 and t['elapsed_ms']>=0 and t['relay_elapsed_ms']>=0,'turn budget')
  expected=(boss_play.extra_budget(1000) if boss_play.phase(b,earlier,False,b.turn,1000) in ('opening','trap_hunt') else 0) if mode=='boss' else dynamic_play.extra_budget(1000,ply=i)
  req(t['advertised_extra_budget_ms']==expected,'advertised extra')
  d=t['boss_decision'] if mode=='boss' else t['dynamic_decision']
  req(t['opening_decision'] is None,'unexpected separate opening policy')
  req((t['dynamic_decision'] is None if mode=='boss' else t['boss_decision'] is None),'wrong policy turn')
  req(d is not None if mode=='boss' or i<80 else d is None,'decision coverage')
  if d:
   req(d['ply']==i+1 and d['before_fen']==b.fen(en_passant='fen') and d['history_uci']==r['uci_moves'][:i],'decision identity')
   req(d['selected_uci']==t['uci'] and d['baseline_budget_ms']==1000,'decision choice/budget')
   req(d['policy']==(boss_play.POLICY if mode=='boss' else dynamic_play.POLICY),'decision policy')
   req(not d.get('reused'),'unexpected memo reuse')
   req(d['extra_budget_ms']<=1000 and d['extra_budget_ms']>=0,'extra cap')
   if mode=='normal': req(d['extra_budget_ms']<=dynamic_play.extra_budget(1000,ply=i),'normal phase cap')
   if d['selected_uci']!=d['baseline_uci'] and d.get('reason')!='verified_poisoned_piece':
    chosen=next(p for p in d['probes'] if p['uci']==d['selected_uci'])
    req(chosen['admitted'] is True and chosen.get('error') is None,'unadmitted alternative')
    packet(d['baseline'],b,d['baseline_uci']); packet(chosen['candidate_compared'],b,d['selected_uci'])
    req(d['baseline']['depth']==chosen['candidate_compared']['depth']==chosen['compared_depth'],'alternative same depth')
    req(d['loss_cp']==max(0,d['baseline']['cp']-chosen['candidate_compared']['cp']),'alternative loss')
    raw=chosen['candidate_final_raw']; req(not raw.get('lowerbound') and not raw.get('upperbound') and raw.get('mate') is None,'alternative exact CP')
   if mode=='normal':
    weights=d['selection_weights']; pool=d['selection_pool']
    selected=opening_variety.selected_move(b,r['normal_seed'],pool,weights) if d['mode']=='opening' else dynamic_play._select(b,r['normal_seed'],weights)
    req(selected==d['selected_uci'],'normal seeded selector replay')
   seen[mode].append(d); reasons[mode][d['reason']]+=1
   if mode=='boss':
    req(d['seed']==r['boss_seed'],'boss decision seed')
    obs=d['opponent_observation']
    expected_gate=boss_opponent.gate(b,[*earlier,{'opponent_observation':obs}],r['boss_seed'])
    req(all(d['frequency'].get(k)==v for k,v in expected_gate.items()),'frequency replay')
    req(obs['budget_ms']<=225,'quality budget')
    if obs['valid']:
     prior=b.copy(stack=True); actual=prior.pop().uci()
     req(obs['ply']==i and obs['played_uci']==actual and obs['history_before']==r['uci_moves'][:i-1] and obs['before_fen']==prior.fen(en_passant='fen'),'quality prefix')
     req(obs['forced'] is False and obs['depth']>=4 and obs['loss_cp']>=0,'quality basic')
     packet(obs['baseline'],prior)
     if obs.get('candidate_compared'):
      packet(obs['candidate_compared'],prior,actual)
      req(obs['candidate_compared']['depth']==obs['baseline']['depth']==obs['depth'],'quality same depth')
      delta=obs['baseline']['cp']-obs['candidate_compared']['cp']; req(delta>=-25 and obs['loss_cp']==max(0,delta),'quality loss')
     else: req(obs['baseline']['uci']==actual and obs['loss_cp']==0,'quality baseline match')
     quality.append({'ply':obs['ply'],'uci':actual,'loss_cp':obs['loss_cp'],'depth':obs['depth']})
    if d.get('line_id'):
     source=next(x for x in boss_openings.repertoire() if x['line_id']==d['line_id'])
     req(source['moves'][:i+1]==r['uci_moves'][:i+1],'authored prefix')
     req(source['color']==r['boss_color'],'authored color')
     complete=len(source['moves'])==i+1; req(d['line_complete']==complete,'signature flag')
     book.append({'ply':i+1,'san':t['san'],'uci':t['uci'],'line_id':d['line_id'],'name':d['opening_name'],'signature':complete,'loss_cp':d['loss_cp'],'reason':d['reason']})
    if d.get('proposed_line_id'):
     source=next(x for x in boss_openings.repertoire() if x['line_id']==d['proposed_line_id'])
     if len(source['moves'])==i+1: signatures.append({'ply':i+1,'line_id':d['proposed_line_id'],'proposed_uci':d.get('candidate_uci'),'selected_uci':t['uci'],'reason':d['reason']})
    attempts=d.get('trap_attempts',[])
    if attempts: candidate_turns+=1; branches+=len(attempts)
    capture_searches+=sum('branch_budget_ms' in a for a in attempts)
    proof_results+=sum('evidence' in a for a in attempts)
    attempt_reasons.update(a.get('reason','unspecified') for a in attempts)
    if d.get('reason')=='verified_poisoned_piece':
     ev=d['trap_evidence']; req(ev and ev['verified'] is True and ev['uci']==t['uci'],'trap evidence identity')
     capture=ev['capture_uci']; post=b.copy(stack=True); post.push(move); cap=chess.Move.from_uci(capture)
     req(cap in post.legal_moves and post.is_capture(cap),'bait capture legality')
     reply=r['uci_moves'][i+1] if i+1<len(r['uci_moves']) else None
     pv=d.get('best_defense_pv_uci',[]); reference=pv[1] if len(pv)>=2 and pv[0]==t['uci'] else None
     outcome='taken' if reply==capture else 'reference_defense' if reply==reference else 'unknown' if reply else 'no_reply'
     score=chess.engine.Mate(ev['engine_mate']) if ev.get('engine_mate') is not None else chess.engine.Cp(ev['engine_cp'])
     info={'depth':ev['engine_depth'],'score':chess.engine.PovScore(score,b.turn),'pv':[chess.Move.from_uci(x) for x in ev['pv_uci']]}
     proof=poisoned_piece.validate_capture_branch(b,t['uci'],capture,info,engine_color=b.turn,max_plies=8,deadline_monotonic=time.monotonic()+2)
     req(proof['verified'] is True and proof['kind']==ev['kind'],'capture proof replay')
     traps.append({'proof_replay':{'verified':proof['verified'],'kind':proof['kind'],'proof_nodes':proof['proof_nodes'],'minimum_payoff_moves':proof.get('minimum_payoff_moves'),'maximum_payoff_moves':proof.get('maximum_payoff_moves'),'method':'Same production validator rerun on stored engine evidence; no new native search'},'ply':i+1,'san':t['san'],'uci':t['uci'],'loss_cp':d['loss_cp'],'capture_uci':capture,'actual_reply_uci':reply,'outcome':outcome,'evidence':ev})
    earlier.append(d)
   elif d['selected_uci']!=d['baseline_uci']:
    req(d['selected_uci'] in d['selection_pool'],'normal selection pool')
    limit=25 if d['mode']=='opening' else 15
    req(d['loss_cp']<=limit,'normal loss gate')
    normal_alternatives.append({'ply':i+1,'san':t['san'],'uci':t['uci'],'baseline_uci':d['baseline_uci'],'mode':d['mode'],'reason':d['reason'],'loss_cp':d['loss_cp']})
  b.push(move); req(t['after_fen']==b.fen(en_passant='fen'),'after fen')
 for mode,name in (('boss','boss_play'),('normal','dynamic_play')): req(seen[mode]==r['hosts'][mode][name]['decisions'],'metadata decision equality')
 req(len(b.move_stack)==r['plies'] and b.fen(en_passant='fen')==r['final_fen'],'final board')
 out=b.outcome(claim_draw=True); req(out is not None and out.result()==r['result'] and out.termination.name.lower()==r['termination'],'legal outcome')
 profile=boss_opponent.derive_profile(b,earlier)
 req(profile['quality_observations']==len(quality),'quality profile total')
 req(profile['trap_outcomes']=={k:sum(x['outcome']==k for x in traps) for k in ('taken','reference_defense','unknown')},'trap outcomes replay')
 return {'game':number,'pair':r['pair'],'run':folder.name,'boss_color':r['boss_color'],'result':r['result'],'termination':r['termination'],'plies':r['plies'],
  'passed':True,'boss_moves':len(seen['boss']),'normal_moves':sum(t['actor']=='normal' for t in r['moves']),
  'normal_receipts':len(seen['normal']),
  'boss_baseline_preserved':sum(d['selected_uci']==d['baseline_uci'] for d in seen['boss']),
  'boss_baseline_preserved_reasons':dict(Counter(d['reason'] for d in seen['boss'] if d['selected_uci']==d['baseline_uci'])),
  'boss_nonbaseline_reasons':dict(Counter(d['reason'] for d in seen['boss'] if d['selected_uci']!=d['baseline_uci'])),
  'normal_baseline_preserved':sum(t['actor']=='normal' for t in r['moves'])-len(normal_alternatives),
  'normal_direct_baseline_without_receipt':sum(t['actor']=='normal' and t['dynamic_decision'] is None for t in r['moves']),
  'normal_baseline_preserved_reasons':dict(Counter(d['reason'] for d in seen['normal'] if d['selected_uci']==d['baseline_uci'])),
  'boss_reasons':dict(reasons['boss']),'normal_reasons':dict(reasons['normal']),
  'boss_book_moves':book,'signature_proposals':signatures,'verified_selected_traps':traps,
  'trap_candidate_turns':candidate_turns,'trap_candidate_branches':branches,
  'trap_capture_branch_searches':capture_searches,'trap_proof_results':proof_results,'trap_attempt_reasons':dict(attempt_reasons),
  'quality_observations':len(quality),'large_quality_errors_ge150':sum(q['loss_cp']>=150 for q in quality),
  'final_boss_frequency':profile['frequency'],'trap_outcomes':profile['trap_outcomes'],
  'normal_alternatives':normal_alternatives,'input_sha256':{str(p.relative_to(BASE)):sha(p),str((folder/r['pgn_file']).relative_to(BASE)):sha(folder/r['pgn_file']),str((folder/f'game-{number:03d}.moves.jsonl').relative_to(BASE)):sha(folder/f'game-{number:03d}.moves.jsonl')}}

rows=[]; runs=[]; input_sha={}
for folder in sorted(BASE.glob('pairs-*')):
 m=load(folder/'match.json')
 req(m['status']=='completed' and m['sources_unchanged'] and m['catalog_unchanged'] and m['binary_unchanged'],'shard not complete unchanged')
 req(len(m['games'])==4,'shard game count')
 for g in m['games']:
  rows.append(run_game(folder,g['game']))
 input_sha[str((folder/'match.json').relative_to(BASE)).replace('\\','/')]=sha(folder/'match.json')
 runs.append({'run':folder.name,'source':m['source'],'source_sha256':m['source_sha256'],'catalog_sha256':m['catalog_sha256'],'binary_sha256':m['binary_sha256'],'sources_unchanged':m['sources_unchanged'],'catalog_unchanged':m['catalog_unchanged'],'binary_unchanged':m['binary_unchanged']})
rows.sort(key=lambda r:r['game'])
req([r['game'] for r in rows]==list(range(1,17)),'all 16 game IDs required')
for r in rows:
 for k,v in r['input_sha256'].items(): input_sha[k.replace('\\','/')]=v
for folder in sorted(BASE.glob('pairs-*')):
 for p in (folder/'host').glob('game-*/*/*.json'):
  input_sha[str(p.relative_to(BASE)).replace('\\','/')]=sha(p)
totals={k:sum(r[k] for r in rows) for k in ('plies','boss_moves','normal_moves','normal_receipts','boss_baseline_preserved','normal_baseline_preserved','normal_direct_baseline_without_receipt','trap_candidate_turns','trap_candidate_branches','trap_capture_branch_searches','trap_proof_results','quality_observations','large_quality_errors_ge150')}
totals.update(games=len(rows),boss_book_moves=sum(len(r['boss_book_moves']) for r in rows),boss_signature_moves=sum(x['signature'] for r in rows for x in r['boss_book_moves']),boss_signature_games=sum(any(x['signature'] for x in r['boss_book_moves']) for r in rows),boss_verified_selected_traps=sum(len(r['verified_selected_traps']) for r in rows),boss_verified_trap_games=sum(bool(r['verified_selected_traps']) for r in rows),normal_opening_alternatives=sum(x['mode']=='opening' for r in rows for x in r['normal_alternatives']),normal_middlegame_alternatives=sum(x['mode']=='middlegame' for r in rows for x in r['normal_alternatives']))
for key in ('boss_baseline_preserved_reasons','boss_nonbaseline_reasons','normal_baseline_preserved_reasons','boss_reasons','normal_reasons','trap_outcomes','trap_attempt_reasons'):
 counter=Counter()
 for r in rows: counter.update(r[key])
 totals[key]=dict(counter)
totals['boss_baseline_preserved_fraction']=totals['boss_baseline_preserved']/totals['boss_moves']
totals['normal_baseline_preserved_fraction']=totals['normal_baseline_preserved']/totals['normal_moves']
totals['boss_nonbaseline_moves']=totals['boss_moves']-totals['boss_baseline_preserved']
totals['normal_nonbaseline_moves']=totals['normal_moves']-totals['normal_baseline_preserved']
report={'schema':'regicide.boss-v-normal.decision-audit.v1','created_utc':datetime.now(timezone.utc).isoformat(),'passed':True,'source_freeze':FREEZE,
 'auditor':'Independent read-only record audit; no native engine rerun and no production mutation.',
 'checks':['All 16 games completed legally, with no cap or error; individual PGN and full trace/record moves agree',
 'Each actor/color matches the board side to move; both final hosts and active-game production sidecars contain the exact same full game history',
 'Boss effective policy enabled, normal dynamic policy enabled, inactive policy decision lists empty',
 'Both hosts use classical_activity_v1, no neural model, Hash16 and UsePieceActivity=true; optional Threads=1',
 'Global pair number, boss/normal independent deterministic seeds and swapped colors match the preregistered schedule',
 'Every applicable decision before-FEN/history/selected-UCI/ply matches actual play',
 'Configured 1000ms baseline and policy-specific advertised/recorded extra budgets checked at each turn',
 'Every selected nonbaseline authored/normal alternative has an admitted exact-CP same-depth own-engine comparison and matching recorded loss',
 'All normal seeded weighted choices recomputed from stored candidate weights',
 'Valid opponent quality observations verified against actual preceding opponent position/move and same-depth own-engine score packets',
 'Boss opponent frequency/profile reproduced from exact surviving history and observations at every boss turn',
 'Authored book move legal full prefix and side checked; setup moves counted separately from signature cutoff',
 'Chosen bait captures, if any, checked for legality and actual reply; proof rerun using the same production validator and stored engine evidence, no fresh native search',
 'Source, catalog and binary SHA256 verified against disk; each completed shard reports unchanged true',
 'Sidecar source version, metadata and active policy flags match match records'],
 'definitions':{'baseline_preserved':'Actual selected UCI equals the current position full-baseline-search UCI. This is a move identity observation, not a causal strength attribution.',
 'normal_direct_baseline_without_receipt':'At or after ply80 the production normal path returns the baseline directly and has no policy receipt.',
 'trap_candidate_branches':'Enumerated bait/capture attempts; some are rejected before any after-capture search. trap_capture_branch_searches counts returned after-capture engine searches.',
 'normal_opening_enabled':'Both constructor flags true; normal effective dynamic policy wraps opening variety during the first16plies. Separate opening_variety.enabled is false.',
 'book_setup_vs_signature':'A recorded authored-gambit reason can refer only to e4/d4 setup; signature requires exact full authored line reaching its final move.',
 'quality':'Same-game heuristic relative move loss; not calibrated human skill/Elo.',
 'time':'Configured policy budgets are nominal. Wall time and CPU use are not equalized.'},
 'totals':totals,'games':rows,'signature_proposals':[dict(game=r['game'],**x) for r in rows for x in r['signature_proposals']],
 'runs':runs,'input_sha256':input_sha,
 'limitations':['This audit reproduces recorded choices and legal state, not the strength correctness of the search evaluations.',
 'Proof replays use the same production branch validator on stored native evidence, not a second engine or independent implementation.',
 'No wins are attributed to openings, traps, or alternative selection without a controlled follow-up comparison.']}
target=BASE/'all16-decisions-audit.json'
target.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
print(json.dumps({'path':str(target),'sha256':sha(target),'totals':totals},ensure_ascii=False))

