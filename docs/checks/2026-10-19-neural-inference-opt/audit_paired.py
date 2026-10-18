import collections
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import statistics
import subprocess
import sys

import chess

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
COMMIT = '7ee14504ea9c87fff6cef8731c86d97f23efa4b7'
BASELINE = '00dec89b08de4e5f573b18b440a93cd82f4b3311'
checks = 0
pv_plies = 0
history_plies = 0

def check(value, label):
    global checks
    checks += 1
    if not value:
        raise AssertionError(label)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def same(value, expected, label):
    check(math.isclose(value, expected, rel_tol=2e-12, abs_tol=1e-12), label)

def canonical(path, commit):
    current = (ROOT / path).read_bytes().replace(b'\r\n', b'\n')
    saved = subprocess.check_output(['git', 'show', commit + ':' + str(path).replace('\\', '/')], cwd=ROOT).replace(b'\r\n', b'\n')
    check(current == saved, 'working source differs ' + str(path))

document = load(BASE / 'paired/comparison.json')
check(document['completed'] and document['error'] is None, 'incomplete comparison')
check(document['schema'] == 'regicide.neural.inference.cost.v1', 'schema')
for role, commit in [('baseline', BASELINE), ('candidate', COMMIT)]:
    engine = document['engines'][role]
    check(engine['source_commit'] == commit, role + ' commit')
    check(sha(engine['path']) == engine['sha256'], role + ' binary')
for item in document['harness_sources']:
    check(sha(ROOT / item['path']) == item['sha256'], 'harness hash')
    canonical(item['path'], COMMIT)
fixture = document['fixture']
check(sha(fixture['path']) == fixture['sha256'], 'fixture hash')
fixture_source=load(fixture['path'])
fixture_copy=json.loads(json.dumps(fixture['document']))
for position in fixture_copy['positions']:
    position.pop('search_fen', None)
check(fixture_source == fixture_copy, 'fixture snapshot except derived search_fen')
positions = fixture['document']['positions']
check(len(positions) == 12, 'position count')
config = document['configuration']
check((config['repeats'], config['seed'], config['depth'], config['nodes'], config['movetime_ms']) == (6,20261019,3,10000,100), 'predeclared configuration')
check(config['options']['Hash'] == 16 and config['options']['Move Overhead'] == 0, 'resources')
check(all(value is True for key,value in config['options'].items() if key not in ['Hash','Move Overhead']), 'search flags')
models = document['models']
check(set(models) == {'learned_v1','previous_v2','quality_v2','zero_v2'}, 'models')
metadata = load(BASE / 'performance-metadata.json')
check(metadata['source_commit'] == COMMIT and metadata['baseline_commit'] == BASELINE, 'profile source')
for name,item in models.items():
    expected = metadata['models'][name]
    for path in [item['path'],expected['source'],expected['snapshot']]:
        check(sha(path) == item['sha256'] == expected['sha256'], 'model snapshot')
    for role in ['baseline','candidate']:
        check(document['native_model_validation'][role][name]['model_sha256'] == item['sha256'], 'parser model hash')
    check(document['native_model_validation']['baseline'][name] == document['native_model_validation']['candidate'][name], 'parser exact score')
for path,digest in metadata['sources'].items():
    check(sha(ROOT / path) == digest, 'profile source hash')
    canonical(path, COMMIT)
profile_binary = ROOT / 'build-neural-v4/regicide_profile_neural.exe'
check(sha(profile_binary) == metadata['profile_binary_sha256'], 'profile binary')
check(sha(ROOT / 'tests/data/evaluation_profile_2026_10_18.fens') == metadata['fen_sha256'], 'profile FEN')

schedule = []
rng = random.Random(20261019)
for repeat in range(6):
    indices = list(range(12))
    rng.shuffle(indices)
    for index in indices:
        order = ['baseline','candidate'] if (repeat + index)%2 == 0 else ['candidate','baseline']
        schedule.append((repeat+1,index,order))
arms = ['hce',*models]
expected_order = [(arm,mode,repeat,index,order) for arm in arms for mode in ['depth','nodes','time'] for repeat,index,order in schedule]
records = document['records']
check(len(records) == 1080, 'pair count')
fields = ['move','score','depth','seldepth','nodes','pv','tt_counters','tt_memory','search_fen']

def validate_result(result, position, mode, node_budget=10000):
    global pv_plies,history_plies
    board = chess.Board(position['fen'])
    check(board.is_valid(), 'initial legality')
    for text in position.get('moves',[]):
        move = chess.Move.from_uci(text)
        check(move in board.legal_moves, 'history legality')
        board.push(move)
        history_plies += 1
    check(board.fen() == position['search_fen'] == result['search_fen'], 'history position')
    check(result['pv'] and result['move'] == result['pv'][0], 'best move PV')
    for text in result['pv']:
        move = chess.Move.from_uci(text)
        check(move in board.legal_moves, 'PV legality')
        board.push(move)
        pv_plies += 1
    raw = result['raw_score_event']
    tokens = raw['raw_info'].split()
    score_index = tokens.index('score')
    check(tokens[score_index+1] == raw['kind'] and int(tokens[score_index+2]) == raw['value'], 'raw score event')
    check(raw['kind'] in ['cp','mate'] and result['score'][raw['kind']] == raw['value'], 'score value')
    check(result['score']['pov'] == 'mover', 'score POV')
    for flag in ['lowerbound','upperbound']:
        check(result['score'][flag] == raw[flag] == (flag in tokens), 'score bound')
    for field in ['nodes','depth','seldepth']:
        check(int(tokens[tokens.index(field)+1]) == result[field], 'raw search field')
    check(tokens[tokens.index('pv')+1:] == result['pv'], 'raw PV')
    check(result['forced_root_move'] is None, 'unforced move')
    check(result['wall_time_seconds'] > 0 and result['engine_time_seconds'] >= 0, 'elapsed time')
    limit = result['requested_limit']
    expected = {'depth':None,'nodes':None,'time':None}
    expected[mode] = {'depth':3,'nodes':node_budget,'time':.1}[mode]
    check(limit == expected, 'requested limit')
    if mode == 'depth': check(result['depth'] == 3, 'completed depth')
    if mode == 'nodes': check(result['nodes'] >= node_budget, 'node budget')
    if mode == 'time': check(result['wall_time_seconds'] <= .2, 'time allowance')

for row, expected in zip(records,expected_order):
    arm,mode,repeat,index,order = expected
    check((row['arm'],row['mode'],row['repeat'],row['position_index'],row['order']) == expected, 'scheduler order')
    check(row['position'] == positions[index]['name'], 'position name')
    for role in order:
        validate_result(row[role],positions[index],mode)
    if mode != 'time':
        check(row['differences'] == [], 'declared differences')
        for field in fields:
            check(row['baseline'][field] == row['candidate'][field], 'fixed work ' + field)
check(len(document['warmups']) == 20, 'warmup count')
for warmup in document['warmups']:
    check(warmup['arm'] in arms and warmup['role'] in ['baseline','candidate'] and warmup['number'] in [1,2], 'warmup identity')
    check(warmup['result']['requested_limit'] == {'depth':None,'nodes':1000,'time':None}, 'warmup node budget')
    validate_result(warmup['result'],positions[0],'nodes',1000)

summary = {}
for arm in arms:
    summary[arm] = {}
    for mode in ['depth','nodes','time']:
        rows = [row for row in records if row['arm']==arm and row['mode']==mode]
        check(collections.Counter(tuple(row['order']) for row in rows) == {('baseline','candidate'):36,('candidate','baseline'):36}, 'balanced order')
        wall = [row['candidate']['wall_time_seconds']/row['baseline']['wall_time_seconds'] for row in rows]
        nodes = [row['candidate']['nodes']/row['baseline']['nodes'] for row in rows]
        declared = document['summary']['arms'][arm][mode]
        check(declared['pairs'] == 72 and declared['wall_ratios'] == wall and declared['node_ratios'] == nodes, 'summary raw ratios')
        same(declared['candidate_baseline_wall_median'], statistics.median(wall), 'wall median')
        same(declared['candidate_baseline_nodes_median'], statistics.median(nodes), 'node median')
        if mode!='time': check(declared['unequal_fixed_work_pairs']==0, 'fixed work summary')
        position_wall = [statistics.median(wall[i] for i,row in enumerate(rows) if row['position_index']==index) for index in range(12)]
        position_nodes = [statistics.median(nodes[i] for i,row in enumerate(rows) if row['position_index']==index) for index in range(12)]
        summary[arm][mode] = {'wall_ratio_median':statistics.median(wall),'nodes_ratio_median':statistics.median(nodes),
            'wall_time_reduction_pct':100*(1-statistics.median(wall)), 'nodes_increase_pct':100*(statistics.median(nodes)-1),
            'position_median_wall_range':[min(position_wall),max(position_wall)],
            'position_median_nodes_range':[min(position_nodes),max(position_nodes)]}

def guard_audit(guard,path,owned):
    lines=[json.loads(line) for line in path.read_text().splitlines()]
    check(guard['snapshot_count']==len(lines), 'guard snapshot count')
    check(len(guard['owned_engines']) == len(guard['transport_engines']) == owned, 'guard transport count')
    check(guard['valid_for_comparison'] is True and guard['contaminated'] is False and guard['failure'] is None, 'guard validity')
    check(guard['external_cpu_increase_ticks']==0 and guard['external_engine_identities']==[], 'external identities')
    maximum=max(line['gap_seconds'] for line in lines)
    same(guard['max_snapshot_gap_seconds'],maximum,'guard maximum gap')
    check(maximum < guard['max_allowed_snapshot_gap_seconds']==2, 'guard gap limit')
    check(all(line['external_cpu_delta_ticks']==0 and line['contaminated'] is False for line in lines), 'external CPU')
    check(all(process['owned'] is True for line in lines for process in line['processes']), 'external named process')
    observed={(process['pid'],process['creation_ticks']) for line in lines for process in line['processes']}
    expected={(process['pid'],process['creation_ticks']) for process in guard['owned_engines']}
    check(observed == expected, 'owned process identities')
    check(lines[0]['phase']=='initial' and lines[-1]['phase']=='finished', 'guard boundary')
    return {'snapshots':len(lines),'max_gap_seconds':maximum,'external_cpu_ticks':0,'owned_transports':owned,'valid':True}

guards = {'paired':guard_audit(document['resource_guard'],BASE/'paired/resource-samples.jsonl',2),
          'profile':guard_audit(load(BASE/'profile-resource-guard.json'),BASE/'profile-resource-samples.jsonl',1)}
spec = importlib.util.spec_from_file_location('profile_audit', ROOT/'scripts/profile_neural_audit.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
profile=load(BASE/'profile.json')
profile_audit=module.audit(profile)
check(profile_audit == load(BASE/'profile-audit.json'), 'profile audit reproducibility')
profile_summary=[{'model_filename':model['model_filename'],'old_ns':model['medians']['old_v063_ns'],
                  'new_ns':model['medians']['new_ns'],'reduction_pct':100*(1-model['medians']['paired_new_over_old']),
                  'block_ratio_range':[model['block_ratio_min'],model['block_ratio_max']]} for model in profile['models']]
check(load(BASE/'power-state.json')['active'] is False, 'keepawake restored')
latest=load(ROOT/'build/learning-cycle/latest.json')
check(latest['attempt_index']==4 and latest['status']=='complete', 'learning attempt unchanged')
check(not (ROOT/'build/learning-cycle/champion.json').exists(), 'no champion adoption')
for path in ['scripts/learning_arena.py','scripts/learning_cycle.py','src/search.cpp','src/board.cpp','src/evaluation.cpp']:
    canonical(path,BASELINE)
result={'schema':'regicide-neural-inference-independent-audit-v1','passed':True,'checks':checks,
        'pair_count':len(records),'fixed_work_exact_pairs':720,'searches':2160,'warmup_searches':20,
        'legal_pv_plies':pv_plies,'history_plies_replayed':history_plies,'source_commit':COMMIT,
        'guards':guards,'profile':profile_summary,'paired_summary':summary,
        'attempt_index':4,'champion_present':False,
        'limits':'Small developer-composed corpus, 6 repeated measurements per position; descriptive ranges are not confidence intervals. Full evaluator includes HCE in V2. No strength, Elo, or promotion inference. Unknown host load and between-sample transient processes not excluded.'}
(BASE/'paired-audit.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,indent=2))
