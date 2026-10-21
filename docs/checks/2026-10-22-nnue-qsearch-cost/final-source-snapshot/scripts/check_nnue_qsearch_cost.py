"""One sealed search-cost screen on exposed positions, with an unchanged NNUE.

Both arms retain quiet tactical threats and the same defensive responses. A
candidate passes only when all three fixed moves and a completed iteration match.
No teacher queries or fitting; the separate untouched gate controls any match.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import chess
import chess.engine

from check_nnue_tactical_search import MODEL, MODEL_SHA, SOURCE, known_cases, fingerprint
from nnue_generalization_gate import ROOT, read, require, sha, write
from stockfish_teacher import _json_info, source_board

NODES = 10000


def probe(binary, board, *, cache, incremental, ordering=False):
    started = time.perf_counter()
    options = dict(read(ROOT / 'config/engine-profiles/classical.json')['options'],
                   UseNNUEEvaluation=True, NeuralModelFile=str(MODEL),
                   UseQuiescenceThreats=True, UseIncrementalNeuralEvaluation=incremental)
    with chess.engine.SimpleEngine.popen_uci([str(binary), 'uci'], timeout=30) as engine:
        if 'UseQuiescenceThreatCache' in engine.options:
            options['UseQuiescenceThreatCache'] = cache
        else:
            require(not cache, 'Candidate lacks the quiescence cache option')
        if 'UseTacticalThreatOrdering' in engine.options:
            options['UseTacticalThreatOrdering'] = ordering
        else:
            require(not ordering, 'Candidate lacks tactical root ordering')
        engine.configure(options)
        packets, last, first_nodes = [], {}, None
        with engine.analysis(board, chess.engine.Limit(nodes=NODES), game=object(),
                             info=chess.engine.INFO_ALL) as analysis:
            for packet in analysis:
                packets.append(_json_info(packet))
                if 'score' in packet:
                    last = packet.copy()
                    if packet.get('depth') == 1 and first_nodes is None:
                        first_nodes = packet.get('nodes')
            best, total = analysis.wait().move, analysis.info
        require(best in board.legal_moves, 'Search returned an illegal move')
        require(1 <= total.get('nodes', 0) <= NODES, 'Missing/overshot total node budget')
        require('score' in last, 'Missing score/fallback packet')
        white_cp = last['score'].white().score()
        require(white_cp is not None, 'Known case unexpectedly returned mate')
        replay = board.copy(stack=True)
        pv = last.get('pv', [])
        require(bool(pv) and pv[0] == best, 'Published PV and best move differ')
        for move in pv:
            require(move in replay.legal_moves, 'Illegal PV')
            replay.push(move)
        return dict(move_uci=best.uci(), depth=last['depth'], white_cp=white_cp,
                    completed_iteration=last['depth'] >= 1, nodes=total['nodes'],
                    first_iteration_nodes=first_nodes, pv_uci=[move.uci() for move in pv],
                    options=options, score_packets=packets, raw_info=_json_info(total),
                    elapsed_seconds=time.perf_counter() - started)


def check(baseline, candidate, output):
    require(not output.exists(), 'Refuse to overwrite/repeat final known-position screen')
    require(sha(MODEL) == MODEL_SHA, 'Frozen output-only model differs')
    paths = [SOURCE, MODEL, Path(__file__).resolve(), baseline, candidate,
             ROOT / 'scripts/check_nnue_tactical_search.py',
             ROOT / 'src/search.cpp', ROOT / 'include/regicide/search.hpp', ROOT / 'src/uci.cpp',
             ROOT / 'config/engine-profiles/classical.json',
             ROOT / 'scripts/nnue_generalization_gate.py', ROOT / 'scripts/stockfish_teacher.py']
    result = dict(schema_version=1, created_utc=datetime.now(timezone.utc).isoformat(),
                  input_sha256={str(path): sha(path) for path in paths},
                  model_sha256=MODEL_SHA, nodes_per_search=NODES,
                  old_binary_sha256=sha(baseline), new_binary_sha256=sha(candidate),
                  known_position_policy='All three fixed expected moves and a completed iteration; both searches retain tactical threats and every defense',
                  source_status='Previously exposed development examples; not independent generalization',
                  training_runs=0, teacher_queries=0, cases=[])
    mismatches = 0
    for case in known_cases():
        board = source_board(case)
        searches = dict(baseline=probe(baseline, board, cache=False, incremental=True),
                        candidate_off=probe(candidate, board, cache=False, incremental=True),
                        candidate=probe(candidate, board, cache=True, incremental=True, ordering=True),
                        candidate_full=probe(candidate, board, cache=True, incremental=False, ordering=True))
        off_equal = fingerprint(searches['baseline']) == fingerprint(searches['candidate_off'])
        oracle_equal = fingerprint(searches['candidate']) == fingerprint(searches['candidate_full'])
        mismatches += int(not off_equal) + int(not oracle_equal)
        passed = (searches['candidate']['completed_iteration']
                  and searches['candidate']['move_uci'] == case['expected_move'])
        result['cases'].append(dict(source=case, searches=searches,
                                    disabled_path_equal=off_equal,
                                    full_incremental_equal=oracle_equal, passed=passed))
    critical = result['cases'][-1]['searches']['candidate']
    result.update(completed=True, native_searches=12, native_mismatches=mismatches,
                  critical_first_iteration_completed=critical['completed_iteration'],
                  critical_first_iteration_nodes=critical['first_iteration_nodes'],
                  known_position_passed=mismatches == 0 and all(row['passed'] for row in result['cases']))
    output.parent.mkdir(parents=True, exist_ok=True)
    write(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check(args.baseline.resolve(), args.candidate.resolve(), args.output.resolve())
    print(json.dumps({key: result[key] for key in ('known_position_passed', 'native_mismatches',
                                                  'critical_first_iteration_completed',
                                                  'critical_first_iteration_nodes')}))


if __name__ == '__main__':
    main()
