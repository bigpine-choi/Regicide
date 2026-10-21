"""One frozen move-choice gate before authorizing a sixteen-game screen.

The ten known loss roots were exposed during development. This is a local
regression gate, not independent evidence of strength. Reuse the authenticated
02cc warm-model searches and run only ten new cold 10,000-node searches. A failed
gate ends this attempt; its answers must not select another checkpoint or retry.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import chess
import chess.engine

import neural_stm as stm
from stockfish_teacher import _json_info, source_board

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = ROOT / 'docs/checks/2026-10-22-nnue-loss-repair'
CACHE = PREVIOUS / 'diagnosis/search-recheck.json'
CACHE_SHA = '17d3d739631a1e628cefd76fba34fe8ff41db0e38fb3e6b3f40ca59065b2cdf1'
WARM = PREVIOUS / 'attempt-001/model.nnue'
WARM_SHA = '02ccdf9b7fd1ac6206774bbc356081977db26cc2c8610176f732ff6ceadc9a09'
BINARY = ROOT / 'build-m3-stm/regicide.exe'
BINARY_SHA = '97140c4ad1c40a9f2c67e2fb9a8c0718fe70722e83dcbe50a7011b71c97558d1'
GAMES = ROOT / 'docs/checks/2026-10-22-nnue-diversity/attempt-001/games.json'
GAMES_SHA = 'af9eb1f88641cb79ffef14c001e177869c849fd049ecdad8a51a82d4c383c8e2'
NODES = 10_000
TUNING_PAIRS = (7, 8)
GATE_POLICY = {
    'known_positions': 10,
    'warm_teacher_bestmove_matches': 2,
    'original_diversity_teacher_bestmove_matches': 4,
    'minimum_candidate_teacher_bestmove_matches': 5,
    'preserve_all_warm_teacher_bestmove_matches': True,
    'tuning_pairs': list(TUNING_PAIRS),
    'warm_tuning_teacher_bestmove_matches': 1,
    'minimum_candidate_tuning_teacher_bestmove_matches': 2,
    'gate_is_independent_holdout': False,
    'retry_or_checkpoint_selection_from_gate': False,
}
CRITERIA = GATE_POLICY


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]


def write(path, document):
    Path(path).write_text(json.dumps(document, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def legal_pv(board, pv, bestmove):
    require(isinstance(pv, list) and pv and pv[0] == bestmove, 'PV/bestmove mismatch')
    replay = board.copy(stack=True)
    for text in pv:
        require(replay.outcome(claim_draw=False) is None, 'PV continues after terminal position')
        move = chess.Move.from_uci(text)
        require(move in replay.legal_moves, 'Illegal PV move')
        replay.push(move)


def score_packet(packet, board, *, exact_cp=False):
    """Validate the actual score-bearing packet, including black-mover sign."""
    require(isinstance(packet, dict), 'Missing score-bearing packet')
    require(type(packet.get('nodes')) is int and packet['nodes'] > 0, 'Invalid score nodes')
    require(type(packet.get('depth')) is int and packet['depth'] > 0, 'Invalid score depth')
    score = packet.get('score', {})
    require(score.get('pov') == ('white' if board.turn else 'black'), 'Score POV differs from mover')
    cp, mate = score.get('white_cp'), score.get('white_mate')
    require((type(cp) is int) != (type(mate) is int), 'Require CP or mate, separately')
    if exact_cp:
        require(type(cp) is int and mate is None, 'Teacher must be CP')
        require(not packet.get('lowerbound', False) and not packet.get('upperbound', False),
                'Teacher final score is a bound')
        require(abs(cp) <= 1500, 'Teacher score out of range')
    if cp is not None:
        require(int(score['relative']) == (cp if board.turn else -cp), 'CP sign mismatch')
    return cp, mate


def validate_teacher(source, receipt, board):
    """Authenticate root label against the final exact teacher packet."""
    require(receipt.get('status') == 'accepted' and receipt.get('reasons') == [],
            'Teacher receipt is not accepted')
    require(receipt.get('source_query_id') == source['query_id'], 'Teacher source ID mismatch')
    require(receipt.get('query_id') == source['teacher_query_id'], 'Teacher query ID mismatch')
    require(receipt.get('fen') == source['fen'], 'Teacher FEN mismatch')
    require(receipt.get('family_id') == source['family_id'], 'Teacher family mismatch')
    require(receipt.get('partition') == source['partition'], 'Teacher partition mismatch')
    require(receipt.get('history_plies') == len(board.move_stack), 'Teacher history mismatch')
    packet = receipt['raw_score_info_packet']
    cp, _ = score_packet(packet, board, exact_cp=True)
    require(receipt['score']['kind'] == 'cp' and receipt['score']['white_cp'] == cp,
            'Teacher final score differs from receipt')
    require(receipt['score']['stm_cp'] == (cp if board.turn else -cp), 'Teacher mover sign mismatch')
    require(source['teacher_white_cp'] == source['target_white_cp'] == cp, 'Teacher label mismatch')
    require(source['teacher_stm_cp'] == receipt['score']['stm_cp'], 'Teacher label mover sign mismatch')
    require(receipt['score_nodes'] == packet['nodes'], 'Teacher final packet node mismatch')
    require(receipt['depth'] == packet['depth'], 'Teacher final packet depth mismatch')
    require(receipt['pv_uci'] == packet['pv'], 'Teacher final packet PV mismatch')
    legal_pv(board, packet['pv'], receipt['bestmove_uci'])


def validate_search(search, board, options, model_hash):
    require(search.get('model_sha256') == model_hash, 'Search model hash mismatch')
    require(search.get('options') == options, 'Search options mismatch')
    require(search.get('nodes') == NODES, 'Search must complete the fixed node request')
    raw = search['raw_info']
    require(raw.get('nodes') == search['nodes'] and raw.get('depth') == search['depth'],
            'Search counters differ from packet')
    cp, mate = score_packet(search.get('raw_score_info_packet', raw), board)
    require(search['white_cp'] == cp and search['white_mate'] == mate, 'Search score mismatch')
    require(raw.get('pv') == search['pv_uci'], 'Search PV differs from packet')
    legal_pv(board, search['pv_uci'], search['move_uci'])
    require(search['move_san'] == board.san(chess.Move.from_uci(search['move_uci'])),
            'Search SAN mismatch')


def evaluate_gate(cases):
    require(len(cases) == CRITERIA['known_positions'], 'Require the frozen ten cases')
    keys = [case['source']['query_id'] for case in cases]
    require(len(set(keys)) == len(keys), 'Duplicate gate case')
    warm = {case['source']['query_id'] for case in cases
            if case['searches']['warm']['move_uci'] == case['teacher_bestmove_uci']}
    candidate = {case['source']['query_id'] for case in cases
                 if case['searches']['candidate']['move_uci'] == case['teacher_bestmove_uci']}
    tuning = {case['source']['query_id'] for case in cases
              if case['source']['source_pair'] in TUNING_PAIRS}
    require(len(warm) == CRITERIA['warm_teacher_bestmove_matches'], 'Warm total baseline changed')
    require(len(tuning) == 4 and len(warm & tuning) == 1, 'Warm tuning baseline changed')
    conditions = {
        'at_least_five_teacher_bestmoves': len(candidate) >= 5,
        'preserves_both_existing_teacher_bestmoves': warm <= candidate,
        'at_least_two_tuning_teacher_bestmoves': len(candidate & tuning) >= 2,
    }
    return {'passed': all(conditions.values()), 'conditions': conditions,
            'warm_matches': len(warm), 'candidate_matches': len(candidate),
            'warm_tuning_matches': len(warm & tuning),
            'candidate_tuning_matches': len(candidate & tuning),
            'new_matches': sorted(candidate - warm), 'lost_matches': sorted(warm - candidate),
            'match_authorized_by_gate': all(conditions.values()),
            'retry_authorized': False}


def authenticated_cases():
    require(sha(CACHE) == CACHE_SHA, 'Frozen search cache bytes changed')
    require(sha(BINARY) == BINARY_SHA and sha(WARM) == WARM_SHA, 'Frozen engine/model changed')
    require(sha(GAMES) == GAMES_SHA, 'Frozen source games changed')
    cached = read(CACHE)
    require(cached['completed'] is True and cached['selected_positions'] == 10,
            'Frozen cache is incomplete')
    require(cached['requested_nodes_per_search'] == NODES, 'Cached work budget differs')
    require(cached['model_paths']['after'] == str(WARM), 'Cached warm model path differs')
    for path, expected in cached['input_sha256'].items():
        require(sha(path) == expected, 'Cached input bytes changed: ' + path)
    profile = read(ROOT / 'config/engine-profiles/classical.json')['options']
    common = dict(profile, UseNNUEEvaluation=True)
    require(common == cached['common_options'], 'Cached common options differ')
    warm_options = dict(common, NeuralModelFile=str(WARM))
    labels_path = PREVIOUS / 'data-001/roots-teacher/labels.jsonl'
    receipts_path = PREVIOUS / 'data-001/roots-teacher/teacher-queries.jsonl'
    labels = {row['query_id']: row for row in lines(labels_path)}
    receipts = {row['source_query_id']: row for row in lines(receipts_path)}
    games = {game['game']: game for game in read(GAMES)['games']}
    cases = []
    for item in cached['cases']:
        source = item['source']
        require(source == labels[source['query_id']], 'Cached source differs from frozen label')
        receipt = receipts[source['query_id']]
        require(item['teacher_receipt'] == receipt, 'Cached teacher receipt differs')
        board = source_board(source)
        game = games[source['source_game']]
        loss_result = '0-1' if game['white'] == 'candidate' else '1-0'
        require(game['result'] == loss_result, 'Gate source game was not a candidate loss')
        require(source['initial_fen'] == game['initial_fen'], 'Loss initial position mismatch')
        require(source['history'] == game['uci_moves'][:source['source_ply'] - 1],
                'Loss history mismatch')
        require(board.turn == (source['candidate_sign'] == 1), 'Loss candidate color mismatch')
        validate_teacher(source, receipt, board)
        require(item['teacher_bestmove_uci'] == receipt['bestmove_uci'], 'Teacher bestmove differs')
        validate_search(item['searches']['after'], board, warm_options, WARM_SHA)
        cases.append({'source': deepcopy(source), 'teacher_receipt': deepcopy(receipt),
                      'teacher_bestmove_uci': receipt['bestmove_uci'],
                      'searches': {'warm': deepcopy(item['searches']['after'])}})
    require(sum(item['searches']['warm']['move_uci'] == item['teacher_bestmove_uci']
                for item in cases) == 2, 'Authenticated warm total is not two')
    require(sum(item['searches']['before']['move_uci'] == item['teacher_bestmove_uci']
                for item in cached['cases']) == 4, 'Authenticated original diversity total is not four')
    return common, cases, cached


def run(candidate_path, output_path):
    candidate, output = Path(candidate_path).resolve(), Path(output_path).resolve()
    require(not output.exists(), 'Refuse to overwrite or retry a gate')
    require(candidate != WARM.resolve(), 'Candidate path must be separate from warm model')
    model = stm.load_model(candidate)
    require(model.get('format_version') == 3 and model.get('input_scale') == 32,
            'Require the unchanged v3/S32 architecture')
    candidate_hash = sha(candidate)
    common, cases, cached = authenticated_cases()
    options = dict(common, NeuralModelFile=str(candidate))
    bound_paths = [Path(__file__), CACHE, GAMES, BINARY, WARM, candidate,
                   *map(Path, cached['input_sha256'])]
    result = {'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
              'purpose': 'Known loss-root move-choice gate before at most sixteen games',
              'limitations': 'Previously exposed development positions; not independent strength evidence',
              'criteria_fixed_before_search': deepcopy(CRITERIA), 'completed': False,
              'warm_model_sha256': WARM_SHA, 'candidate_model_sha256': candidate_hash,
              'binary_sha256': BINARY_SHA,
              'input_sha256': {str(path): sha(path) for path in bound_paths},
              'requested_nodes_per_search': NODES, 'new_searches_maximum': 10,
              'reused_warm_searches': 10, 'teacher_queries': 0, 'training_runs': 0,
              'state_policy': 'Complete initial_fen/history, Clear Hash, distinct game per search',
              'command': [str(BINARY), 'uci'], 'candidate_options': options,
              'cases': cases}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False) + '\n')
    started = time.perf_counter()
    with chess.engine.SimpleEngine.popen_uci([str(BINARY), 'uci'], timeout=30.0) as engine:
        engine.configure(options)
        for case in cases:
            board = source_board(case['source'])
            require(sha(candidate) == candidate_hash, 'Candidate changed during gate')
            engine.configure({'Clear Hash': None})
            tick, last_score = time.perf_counter(), {}
            with engine.analysis(board, chess.engine.Limit(nodes=NODES), game=object(),
                                 info=chess.engine.INFO_ALL) as analysis:
                for packet in analysis:
                    if 'score' in packet:
                        last_score = packet.copy()
                bestmove = analysis.wait().move
                aggregate = analysis.info
            require(bestmove is not None, 'Engine omitted bestmove')
            score = last_score.get('score')
            require(isinstance(score, chess.engine.PovScore), 'Engine omitted final score')
            search = {
                'model_sha256': candidate_hash, 'options': options,
                'move_uci': bestmove.uci(), 'move_san': board.san(bestmove),
                'matches_teacher_bestmove': bestmove.uci() == case['teacher_bestmove_uci'],
                'white_cp': score.white().score(), 'white_mate': score.white().mate(),
                'lowerbound': last_score.get('lowerbound', False),
                'upperbound': last_score.get('upperbound', False),
                'depth': last_score.get('depth'), 'nodes': aggregate.get('nodes'),
                'pv_uci': [move.uci() for move in aggregate.get('pv', [])],
                'elapsed_seconds': time.perf_counter() - tick,
                'raw_info': _json_info(aggregate),
                'raw_score_info_packet': _json_info(last_score)}
            validate_search(search, board, options, candidate_hash)
            require(search['pv_uci'] == search['raw_score_info_packet']['pv'],
                    'Final score packet and final PV disagree')
            case['searches']['candidate'] = search
            write(output, result)
    result.update(completed=True, finished_at=datetime.now(timezone.utc).isoformat(),
                  elapsed_seconds=time.perf_counter() - started,
                  actual_new_searches=len(cases), gate=evaluate_gate(cases))
    write(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = run(args.candidate, args.output)
    print(json.dumps({'completed': result['completed'], 'gate': result['gate'],
                      'elapsed_seconds': result['elapsed_seconds']}, indent=2))


if __name__ == '__main__':
    main()
