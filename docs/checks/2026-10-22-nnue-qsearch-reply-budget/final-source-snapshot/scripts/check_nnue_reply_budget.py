"""One final bounded-reply screen on exposed QGA positions; never trains.

Cost recovery and correct choices are separate results. Only both together,
plus native/full/incremental consistency, authorize the untouched position gate.
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
BF5_FIRST_NODES_MAXIMUM = 2577


def probe(binary, board, *, candidate, incremental):
    started = time.perf_counter()
    options = dict(read(ROOT / 'config/engine-profiles/classical.json')['options'],
                   UseNNUEEvaluation=True, NeuralModelFile=str(MODEL),
                   UseQuiescenceThreats=True, UseQuiescenceThreatCache=True,
                   UseTacticalThreatOrdering=not candidate,
                   UseIncrementalNeuralEvaluation=incremental)
    with chess.engine.SimpleEngine.popen_uci([str(binary), 'uci'], timeout=30) as engine:
        if 'UseQuiescenceReplyBudget' in engine.options:
            options['UseQuiescenceReplyBudget'] = candidate
        else:
            require(not candidate, 'Candidate lacks optional reply budget')
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
        require(1 <= total.get('nodes', 0) <= NODES, 'Missing/overshot node budget')
        require('score' in last, 'Missing score/fallback packet')
        white_cp = last['score'].white().score()
        require(white_cp is not None, 'Unexpected mate in fixed exposed screen')
        replay, pv = board.copy(stack=True), last.get('pv', [])
        require(bool(pv) and pv[0] == best, 'Best move and PV differ')
        for move in pv:
            require(move in replay.legal_moves, 'Illegal PV')
            replay.push(move)
        return dict(move_uci=best.uci(), depth=last['depth'], white_cp=white_cp,
                    completed_iteration=last['depth'] >= 1, nodes=total['nodes'],
                    first_iteration_nodes=first_nodes, pv_uci=[move.uci() for move in pv],
                    options=options, score_packets=packets, raw_info=_json_info(total),
                    elapsed_seconds=time.perf_counter() - started)


def check(baseline, candidate, output):
    require(not output.exists(), 'Final known-position screen may not be overwritten/repeated')
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
                  known_position_policy='All three expected moves plus completed first iteration at <=10k and Bf5 first <=2577',
                  source_status='Exposed development examples; not independent generalization',
                  training_runs=0, teacher_queries=0, cases=[])
    mismatches = 0
    for case in known_cases():
        board = source_board(case)
        searches = dict(baseline=probe(baseline, board, candidate=False, incremental=True),
                        candidate_off=probe(candidate, board, candidate=False, incremental=True),
                        candidate=probe(candidate, board, candidate=True, incremental=True),
                        candidate_full=probe(candidate, board, candidate=True, incremental=False))
        off_equal = fingerprint(searches['baseline']) == fingerprint(searches['candidate_off'])
        oracle_equal = fingerprint(searches['candidate']) == fingerprint(searches['candidate_full'])
        mismatches += int(not off_equal) + int(not oracle_equal)
        chosen = searches['candidate']
        cost_passed = (chosen['completed_iteration']
                       and isinstance(chosen['first_iteration_nodes'], int)
                       and 1 <= chosen['first_iteration_nodes'] <= NODES)
        if case['name'] == 'white_reply_after_Bf5':
            cost_passed = cost_passed and chosen['first_iteration_nodes'] <= BF5_FIRST_NODES_MAXIMUM
        move_passed = chosen['completed_iteration'] and chosen['move_uci'] == case['expected_move']
        result['cases'].append(dict(source=case, searches=searches,
                                    disabled_path_equal=off_equal,
                                    full_incremental_equal=oracle_equal,
                                    cost_passed=cost_passed, move_passed=move_passed,
                                    passed=cost_passed and move_passed))
    critical = result['cases'][-1]['searches']['candidate']
    costs = mismatches == 0 and all(row['cost_passed'] for row in result['cases'])
    result.update(completed=True, native_searches=12, native_mismatches=mismatches,
                  cost_gate_passed=costs, Bf5_first_nodes_maximum=BF5_FIRST_NODES_MAXIMUM,
                  critical_first_iteration_completed=critical['completed_iteration'],
                  critical_first_iteration_nodes=critical['first_iteration_nodes'],
                  known_position_passed=costs and all(row['move_passed'] for row in result['cases']))
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
    print(json.dumps({key: result[key] for key in ('cost_gate_passed', 'known_position_passed',
                      'native_mismatches', 'critical_first_iteration_nodes')}))


if __name__ == '__main__':
    main()
