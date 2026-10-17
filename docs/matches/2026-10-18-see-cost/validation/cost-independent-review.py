import collections,datetime,hashlib,json,math,pathlib,random,statistics,sys
import chess
ROOT=pathlib.Path('.').resolve();OUT=ROOT/'build/see-cost-evidence'
NAMES=('depth3','depth4','see-off-depth4','loss-scenes-depth4');ROLES=('baseline','candidate')
EXPECTED={'depth3':(3,5,10,True),'depth4':(4,5,10,True),'see-off-depth4':(4,1,10,False),'loss-scenes-depth4':(4,3,5,True)}
EXPECTED_SOURCES={'baseline':'0edc06e437b934d177fc1e9b0907600b6287be72','candidate':'42444edc3ff1de358b97ac4d250b43bf342d1df0'}
EXPECTED_BINARIES={'baseline':'2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db','candidate':'722b46c3b7223a3fa354a1fc504cb4cced9be0db8ea1e49d5b796b36d434a335'}
checks=0;artifacts={};failures=[];results=[];pv_replay_plies=0;input_replay_plies=0;cached_boards={};pv_seen=set()
def check(condition,where):
 global checks
 checks+=1
 if not condition:raise AssertionError(where)
def near(a,b,where):check(math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-9),where)
def digest(path):
 path=pathlib.Path(path);raw=path.read_bytes();key=str(path.relative_to(ROOT)).replace('\\','/') if path.is_relative_to(ROOT) else str(path)
 value=hashlib.sha256(raw).hexdigest();artifacts[key]={'sha256':value,'bytes':len(raw)};return value
def timestamp(text):return datetime.datetime.fromisoformat(text)
def stats(records):
 ratios=[r['candidate']['wall_time_seconds']/r['baseline']['wall_time_seconds'] for r in records]
 engines=[r['candidate']['engine_time_seconds']/r['baseline']['engine_time_seconds'] for r in records if r['candidate']['engine_time_seconds'] is not None and r['baseline']['engine_time_seconds'] is not None and r['candidate']['engine_time_seconds']>0 and r['baseline']['engine_time_seconds']>0]
 out={'pairs':len(records),'candidate_over_baseline_wall':{'median':statistics.median(ratios),'mean':statistics.mean(ratios),'min':min(ratios),'max':max(ratios),'sample_standard_deviation':statistics.stdev(ratios) if len(ratios)>1 else None},'median_baseline_over_candidate_wall_speedup':statistics.median(1/r for r in ratios),'candidate_slower_pairs':sum(r>1 for r in ratios),'candidate_faster_pairs':sum(r<1 for r in ratios),'same_wall_pairs':sum(r==1 for r in ratios),'baseline_wall_seconds_sum':sum(r['baseline']['wall_time_seconds'] for r in records),'candidate_wall_seconds_sum':sum(r['candidate']['wall_time_seconds'] for r in records),'baseline_engine_seconds_sum':sum(r['baseline']['engine_time_seconds'] for r in records),'candidate_engine_seconds_sum':sum(r['candidate']['engine_time_seconds'] for r in records),'engine_ratio_usable_pairs':len(engines),'engine_ratio_excluded_pairs':len(records)-len(engines),'candidate_over_baseline_engine':{'median':statistics.median(engines),'mean':statistics.mean(engines),'min':min(engines),'max':max(engines)} if engines else None}
 out['ratio_of_total_wall_candidate_over_baseline']=out['candidate_wall_seconds_sum']/out['baseline_wall_seconds_sum'];return out
for name in NAMES:
 try:
  raw_path=OUT/(name+'.json');monitor_path=OUT/(name+'.monitor.json');rows_path=OUT/(name+'.monitor.jsonl')
  for p in (raw_path,monitor_path,rows_path):digest(p)
  d=json.loads(raw_path.read_text());m=json.loads(monitor_path.read_text());monitor=[json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
  depth,repeats,count,use_see=EXPECTED[name]
  check((d['depth'],d['repeats'],len(d['positions_file']['document']['positions']))==(depth,repeats,count),name+' declared controls')
  check(d['error'] is None and d['exit_code']==0 and d['require_same_search'] is True and d['allow_score_differences'] is False,name+' completion flags')
  check(d['seed']==20261020 and d['warmup']==1,name+' seed/warmup')
  check(d['environment']['harness_checkout']['commit']=='25f73cacafe4a831f0b3b258fbf5b19632c369b2' and d['environment']['harness_checkout']['status_porcelain']=='',name+' frozen clean harness')
  position_path=pathlib.Path(d['positions_file']['path']);check(digest(position_path)==d['positions_file']['sha256'],name+' source position file hash')
  actual_position_document=json.loads(position_path.read_text(encoding='utf-8-sig'));positions=d['positions_file']['document']['positions']
  check(len(actual_position_document['positions'])==len(positions),name+' position document length')
  position_boards=[]
  for index,p in enumerate(positions):
   source_p=actual_position_document['positions'][index]
   check({k:v for k,v in p.items() if k!='search_fen'}=={k:v for k,v in source_p.items() if k!='search_fen'},name+' position metadata '+str(index))
   board_key=(source_p.get('fen',chess.STARTING_FEN),tuple(source_p.get('moves',[])))
   if board_key not in cached_boards:
    b=chess.Board(board_key[0])
    for move in board_key[1]:b.push_uci(move);input_replay_plies+=1
    cached_boards[board_key]=b
   b=cached_boards[board_key]
   check(b.fen(en_passant='fen')==p['search_fen'],name+' reconstructed FEN '+str(index))
   if 'expected_fen' in p:check(b.fen(en_passant='fen')==p['expected_fen'],name+' supplied expected FEN '+str(index))
   position_boards.append(b)
  effective={}
  for role in ROLES:
   e=d['engines'][role]
   check(e['declared_source_commit']==EXPECTED_SOURCES[role],name+' source '+role)
   check(e['sha256']==EXPECTED_BINARIES[role] and digest(pathlib.Path(e['path']))==e['sha256'],name+' binary hash '+role)
   check(e['configured_options']=={'Hash':16,'Move Overhead':20,'PVS':True,'UseSEE':use_see},name+' configured options '+role)
   effective[role]={k:v['default'] for k,v in e['advertised_options'].items() if v['type']!='button'};effective[role].update(e['configured_options'])
   check(all(effective[role][k] is True for k in ['UsePositionalEvaluation','UseQuiescence','UseTranspositionTable','UseFastMoves','UseQuietMoveOrdering','UseQuiescenceChecks','PVS']),name+' evaluation/search defaults '+role)
  check(effective['baseline']==effective['candidate'],name+' same effective options')
  records=d['records'];check(len(records)==count*repeats,name+' total pairs')
  rng=random.Random(d['seed']);schedule=[]
  for rep in range(repeats):
   indices=list(range(count));rng.shuffle(indices)
   schedule.extend((rep+1,index,list(ROLES if (rep+index)%2==0 else reversed(ROLES))) for index in indices)
  field_mismatches=collections.Counter();stable={};first_role_counts=collections.Counter();measured_totals={role:collections.Counter() for role in ROLES};position_repeats=collections.Counter()
  for j,(r,expected_schedule) in enumerate(zip(records,schedule)):
   check((r['repetition'],r['position_index'],r['order'])==expected_schedule,name+' exact random schedule '+str(j));first_role_counts[r['order'][0]]+=1
   index=r['position_index'];p=positions[index];check(r['position']==p['name'],name+' position name '+str(j));position_repeats[index]+=1
   for field in ['move','depth','seldepth','nodes','score','pv','tt_counters','tt_memory','search_fen']:
    if r['baseline'][field]!=r['candidate'][field]:field_mismatches[field]+=1
   check(r['scores_equal'] and r['search_equal'] and r['search_differences']==[],name+' recorded equality '+str(j))
   for role in ROLES:
    v=r[role];check(v['depth']==depth,name+' completed requested depth '+str(j)+role)
    check(v['nodes']>0 and v['wall_time_seconds']>0 and v['engine_time_seconds'] is not None and v['engine_time_seconds']>=0,name+' timing/node validity '+str(j)+role)
    check(v['search_fen']==p['search_fen'],name+' measured FEN '+str(j)+role)
    check(v['process_search_index']==j+2,name+' warmup excluded from measured search indices '+str(j)+role)
    check(v['tt_memory']=={'allocations':0,'reuses':1,'clears':0,'bytes':16777208},name+' measured TT reuse '+str(j)+role)
    for k in ('probes','hits','cutoffs','stores'):measured_totals[role][k]+=v['tt_counters'][k]
    signature={k:v[k] for k in ('move','depth','seldepth','nodes','score','pv','tt_counters')};stable_key=(index,role)
    if stable_key in stable:check(stable[stable_key]==signature,name+' deterministic repeated result '+str(j)+role)
    else:stable[stable_key]=signature
    pv_key=(tuple(p.get('moves',[])),p.get('fen',chess.STARTING_FEN),tuple(v['pv']))
    if pv_key not in pv_seen:
     pv_seen.add(pv_key);b=position_boards[index].copy()
     for move in v['pv']:b.push_uci(move);pv_replay_plies+=1
     check(v['pv'] and v['pv'][0]==v['move'],name+' PV first move '+str(j)+role)
   check(r['baseline']['score']==r['candidate']['score'],name+' actual score '+str(j))
  check(not field_mismatches,name+' actual recorded search fields all equal '+str(field_mismatches));check(all(v==repeats for v in position_repeats.values()),name+' all positions repeat count')
  warms=d['warmup_records'];check(len(warms)==2,name+' two warmups')
  for w,role in zip(warms,ROLES):
   check(w['role']==role and w['warmup_index']==1 and w['position_index']==0 and w['position']==positions[0]['name'],name+' warmup role/index')
   check(w['result']['depth']==depth and w['result']['process_search_index']==1 and w['result']['tt_memory']=={'allocations':1,'reuses':0,'clears':0,'bytes':16777208},name+' warmup allocation')
  timings=stats(records);summary=d['summary']
  check(summary['completed_pairs']==len(records) and summary['score_mismatches']==0 and summary['search_graph_mismatches']==0,name+' summary completion')
  near(summary['median_paired_wall_speedup_baseline_over_candidate'],timings['median_baseline_over_candidate_wall_speedup'],name+' summary wall excludes warmup')
  check(summary['median_paired_node_ratio_candidate_over_baseline']==1.0,name+' summary node ratio')
  near(summary['median_paired_engine_speedup_baseline_over_candidate'],statistics.median(r['baseline']['engine_time_seconds']/r['candidate']['engine_time_seconds'] for r in records if r['baseline']['engine_time_seconds']>0 and r['candidate']['engine_time_seconds']>0),name+' summary engine time excludes invalid zero pairs')
  check(summary['engine_time_ratio_pairs_excluding_zero_or_missing_time']==timings['engine_ratio_usable_pairs'],name+' summary engine pair count')
  per_positions=[]
  for index,p in enumerate(positions):
   prs=[r for r in records if r['position_index']==index];ps=stats(prs);ps.update(index=index,name=p['name']);per_positions.append(ps)
   declared=next(row for row in summary['per_position'] if row['name']==p['name'])
   check(declared['pairs']==repeats,name+' per-position pairs '+str(index));near(declared['median_paired_wall_speedup_baseline_over_candidate'],ps['median_baseline_over_candidate_wall_speedup'],name+' per-position median '+str(index))
   for role in ROLES:
    near(declared[role]['median_wall_time_seconds'],statistics.median(r[role]['wall_time_seconds'] for r in prs),name+' per-position wall '+str(index)+role)
    check(declared[role]['median_nodes']==statistics.median(r[role]['nodes'] for r in prs),name+' per-position nodes '+str(index)+role)
  for role in ROLES:
   check(summary['transposition_counters'][role]=={'samples_reporting':len(records),**dict(measured_totals[role])},name+' TT summary excludes warmups '+role)
   check(summary['transposition_memory'][role]=={'samples_reporting':len(records),'allocations':0,'reuses':len(records),'clears':0,'bytes_max':16777208},name+' TT allocation summary excludes warmups '+role)
  check(m['valid_for_comparison'] is True and not m['contaminated'] and m['failure'] is None and m['external_cpu_increase_ticks']==0 and not m['observed_external_engine_cpu_increase'],name+' guard clean flags')
  check(m['poll_seconds']==.5 and m['external_cpu_tolerance_ticks']==0 and m['max_allowed_snapshot_gap_seconds']==2 and m['completed_idle_seconds']>=10,name+' guard policy and idle')
  check(m['snapshot_count']==len(monitor) and len(monitor)>20,name+' raw snapshot count')
  transports=m['transport_engines'];owned_ids={(p['pid'],p['creation_ticks']) for p in m['owned_engines']}
  check(set(transports)==set(ROLES) and owned_ids=={(v['pid'],v['creation_ticks']) for v in transports.values()} and len(owned_ids)==2,name+' exactly two transport identities')
  root_id=(m['monitor_root_pid'],m['monitor_root_creation_ticks']);proven_owned=set();external_initial=None;previous_external=None;previous_elapsed=None;deltas=[];gaps=[];durations=[]
  for n,row in enumerate(monitor):
   check(not row['contaminated'] and row['external_cpu_delta_ticks']==0,name+' raw clean '+str(n));gap=row['gap_seconds'];duration=row['snapshot_duration_seconds'];gaps.append(gap);durations.append(duration)
   check(0<=duration<=2 and 0<=gap<=2,name+' native sampling bounds '+str(n));near(gap,duration if previous_elapsed is None else row['elapsed_seconds']-previous_elapsed,name+' sampling elapsed consistency '+str(n));previous_elapsed=row['elapsed_seconds']
   ext={};seen=set()
   for p in row['processes']:
    check('error' not in p and p['cpu_ticks'] is not None and p['creation_ticks'] is not None,name+' readable native timer '+str(n));identity=(p['pid'],p['creation_ticks']);check(identity not in seen,name+' unique identity '+str(n));seen.add(identity)
    if p['owned']:
     check(identity in owned_ids,name+' only true transported engines excluded '+str(n));chain=p['ancestry_to_monitor'];proof=p['ancestry_processes']
     if chain is not None:
      check(chain[0]==p['pid'] and chain[-1]==root_id[0] and len(chain)==len(set(chain)) and [v['pid'] for v in proof]==chain,name+' owned ancestry shape '+str(n))
      check((proof[-1]['pid'],proof[-1]['creation_ticks'])==root_id,name+' owned actual monitor '+str(n))
      for child,parent in zip(proof,proof[1:]):check(child['parent_pid']==parent['pid'] and parent['creation_ticks']<=child['creation_ticks'],name+' owned ancestry times '+str(n))
      proven_owned.add(identity)
     else:check(identity in proven_owned,name+' owned remembered identity previously proven '+str(n))
    else:
     check(identity not in owned_ids and not p['exited_during_snapshot'],name+' valid external identity '+str(n));ext[identity]=p['cpu_ticks']
   check(len(ext)==5,name+' five external engines '+str(n))
   if external_initial is None:external_initial=dict(ext)
   check(ext==external_initial,name+' exact external identities and counters unchanged '+str(n))
   delta=0 if previous_external is None else sum(ext[k]-previous_external[k] for k in ext);deltas.append(delta);check(delta==row['external_cpu_delta_ticks'],name+' raw CPU delta recomputed '+str(n));previous_external=ext
  check(proven_owned==owned_ids,name+' each owned identity independently proven')
  expected_external={(x['pid'],x['creation_ticks']):x['initial_cpu_ticks'] for x in m['external_engine_identities']};check(expected_external==external_initial,name+' summary external identities exact')
  near(max(gaps),m['max_snapshot_gap_seconds'],name+' summary gap max');near(max(durations),m['max_snapshot_duration_seconds'],name+' summary duration max')
  check(monitor[0]['phase']=='initial' and monitor[-1]['phase']=='finished' and any(row['phase']=='idle-complete' for row in monitor),name+' monitor phase coverage')
  check(timestamp(monitor[0]['utc'])<=timestamp(d['started_utc']) and timestamp(monitor[-1]['utc'])>=timestamp(d['finished_utc']),name+' full benchmark time covered')
  results.append({'name':name,'passed':True,'depth':depth,'repeats':repeats,'positions':count,'pairs':len(records),'actual_equal_fields':['move','score','nodes','depth','seldepth','pv','tt_counters','tt_memory','search_fen'],'field_mismatches':dict(field_mismatches),'warmups_separately_recorded_and_excluded':len(warms),'first_role_counts':dict(first_role_counts),'effective_options':effective['baseline'],'timing':timings,'per_position':per_positions,'guard':{'snapshots':len(monitor),'external_engines':len(external_initial),'external_cpu_increase_ticks':sum(deltas),'max_gap_seconds':max(gaps),'min_gap_seconds':min(gaps),'max_snapshot_duration_seconds':max(durations),'completed_idle_seconds':m['completed_idle_seconds'],'transport_identities':{k:{'pid':v['pid'],'creation_ticks':v['creation_ticks']} for k,v in transports.items()},'external_initial':m['external_engine_identities']}})
 except Exception as error:
  failures.append({'dataset':name,'error':repr(error)})
report={'schema_version':1,'role':'Independent read-only arithmetic and recorded-data audit; no engine searches, builds, tests, benchmark reruns or tracked edits. Does not import the benchmark summarizer or guard implementation.','passed':not failures,'checks':checks,'total_pairs':sum(r['pairs'] for r in results),'unique_input_positions_replayed':len(cached_boards),'input_replay_plies':input_replay_plies,'unique_pvs_replayed':len(pv_seen),'pv_replay_plies':pv_replay_plies,'failures':failures,'datasets':results,'artifacts':artifacts,'limits':['Matching recorded score/move/nodes/PV/depth/TT fields supports unchanged observed search behavior; no full visited-tree hash exists.','Wall time includes UCI reset/transaction and excludes process startup. At millisecond scales host scheduling and transport noise remain; zero named external-engine CPU growth does not exclude other host work or unknown/transient engines.','Engine time has millisecond granularity; zero or missing engine-time ratios are omitted exactly as declared.','Fixed-depth same-work timing is separate from timed-game strength. The 7.17% clock-instrumented SEE share and 402 exact-value microprofile queries are earlier distinct measurements and are not counted as these 125 fixed-depth pairs.','No timing benefit should be claimed for every position or every individual sample.']}
path=OUT/'cost-independent-review.json';path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
lines=['# SEE 비용 독립 감사','','판정: '+('PASS' if report['passed'] else 'FAIL')+f". {report['checks']:,}개 자료 검산. 엔진·빌드·벤치마크·테스트 실행 없음.",'',f"고정 깊이 {report['total_pairs']}쌍의 실제 결과를 비교했다. 입력 {len(cached_boards)}포지션의 {input_replay_plies}수를 가볍게 복원하고, 고유 PV {len(pv_seen)}개의 {pv_replay_plies}수를 합법성 확인했다.",'','시간비는 **수정 후 / 수정 전**이다. 1보다 크면 느림. 아래 중앙값은 각 쌍의 비율 중앙값이며, 원시간 합의 비율과 구분한다.','','| 자료 | 쌍 | 시간비 중앙값 | 평균 | 최소–최대 | 원시간 합 수정 전→후(s) | 합의 시간비 | 느린 쌍 |','|---|---:|---:|---:|---:|---:|---:|---:|']
for r in results:
 t=r['timing'];v=t['candidate_over_baseline_wall'];lines.append(f"| {r['name']} | {r['pairs']} | {v['median']:.6f} | {v['mean']:.6f} | {v['min']:.6f}–{v['max']:.6f} | {t['baseline_wall_seconds_sum']:.6f}→{t['candidate_wall_seconds_sum']:.6f} | {t['ratio_of_total_wall_candidate_over_baseline']:.6f} | {t['candidate_slower_pairs']}/{r['pairs']} |")
lines+=['','두 엔진의 점수·최선 수·노드·PV·완료 깊이·선택 깊이·전이표 횟수와 메모리가 모든 쌍에서 일치했다. 각 위치의 반복 결과도 일치했고, 모든 요청 깊이가 완료되었다. 저장된 무작위 위치 순서와 선실행 역할 순서를 시드에서 독립 재생성했다.','', '각 자료의 워밍업 2회는 별도 기록되어 측정 합계와 시간 요약에서 제외되었다. 첫 탐색은 역할별 TT 1회 할당, 본 측정은 역할별 할당 0·재사용 1이었다. 바이너리와 위치 파일 SHA256을 현재 바이트와 대조했다.','', '| 자료 | 감시 행 | 외부 엔진 | 외부 CPU 증가(ticks) | 최대 감시 간격(s) | 최대 스냅샷 소요(s) | 엔진 시간 비율 제외 쌍 |','|---|---:|---:|---:|---:|---:|---:|']
for r in results:
 g=r['guard'];lines.append(f"| {r['name']} | {g['snapshots']} | {g['external_engines']} | {g['external_cpu_increase_ticks']} | {g['max_gap_seconds']:.6f} | {g['max_snapshot_duration_seconds']:.6f} | {r['timing']['engine_ratio_excluded_pairs']} |")
lines+=['', '외부 5개의 PID·생성 시각·누적 CPU가 모든 감시 행에서 그대로였다. 제외된 두 자체 엔진은 실제 역할별 PID·생성 시각과 모니터 부모 계보가 일치했다. 각 자료는 10초 이상 사전 대기했고, 첫 행 간격은 해당 스냅샷 소요 시간, 이후 간격은 완료 시각 차이와 일치했다. 전체 벤치마크 구간을 감시 기록이 덮는다.','','## 위치별 시간 편차','','아래 각 위치를 모두 표시했다. 느린 쌍과 느린 중앙값을 제외하거나 재선택하지 않았다.','','| 자료·위치 | 쌍 | 시간비 중앙값 | 평균 | 최소–최대 | 느린 쌍 |','|---|---:|---:|---:|---:|---:|']
for r in results:
 for p in r['per_position']:
  v=p['candidate_over_baseline_wall'];lines.append(f"| {r['name']} · {p['name']} | {p['pairs']} | {v['median']:.6f} | {v['mean']:.6f} | {v['min']:.6f}–{v['max']:.6f} | {p['candidate_slower_pairs']}/{p['pairs']} |")
lines+=['','## 해석 범위','','- 깊이 3에서는 중앙값 기준 느려졌고, 깊이 4와 손실 장면에서는 조금 빨라졌다. 모든 위치에서 이득이라는 근거는 없다.','- 밀리초 단위의 짧은 탐색은 UCI 전달·리셋 및 운영체제 스케줄링 잡음의 영향이 크다. 외부 체스 엔진 CPU 0이 전체 PC 부하 0을 뜻하지 않는다.','- 일치한 기록 필드는 관찰된 탐색 동작이 같다는 근거다. 전체 방문 트리의 해시를 비교한 것은 아니다.','- 앞서 측정한 SEE 계측 비중 7.17%와 원본/수정 SEE 값 402개 일치는 별도 실험이다. 이 감사의 125쌍에 합산하지 않는다.','- 고정 깊이 실행 비용은 같은 시간 조건의 경기력 결과와 별도로 해석한다.']
if failures:lines+=['','## 실패',json.dumps(failures,ensure_ascii=False,indent=2)]
(OUT/'cost-independent-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'passed':report['passed'],'checks':checks,'pairs':report['total_pairs'],'failures':failures,'datasets':[{k:r[k] for k in ['name','pairs','timing','guard']} for r in results]},ensure_ascii=False,indent=2))
if failures:sys.exit(1)
