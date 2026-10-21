"""One post-seal default-search check on already exposed development roots.

No teacher queries, fitting, checkpoint selection or retries. The separate
frozen gate, rather than these training/tuning roots, controls any16-game match.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import chess.engine

from prepare_neural_huber import read, require, sha, write
from nnue_generalization_gate import _search
from stockfish_teacher import source_board


def check(dataset, candidate, output):
    require(not output.exists(), 'Refuse to repeat or overwrite development check')
    manifest = read(dataset / 'manifest.json')
    collection = Path(manifest.get('collection_source', str(dataset)))
    protocol = read(collection / 'protocol.json')
    decisions = read(collection / 'student-choices.json')
    gate_plan = read(manifest['gate_plan'])
    engine_path = Path(gate_plan['binary_path'])
    require(sha(engine_path) == gate_plan['binary_sha256'], 'Development binary changed')
    require((candidate.parent / 'gate-seal.json').is_file(), 'Seal candidate before inspecting development choices')
    validation = read(candidate.parent / 'validation.json')
    require(validation['python_native_full_incremental_mismatches'] == 0
            and validation['input_sha256'].get(str(candidate)) == sha(candidate), 'Candidate validation differs')
    require(len(decisions) <= protocol['maximum_default_student_searches'], 'Development query budget changed')
    inputs = [dataset / 'manifest.json', dataset / 'protocol.json',
              collection / 'protocol.json', collection / 'student-choices.json']
    inputs += [candidate, candidate.parent / 'gate-seal.json', engine_path, Path(__file__)]
    bindings = {str(path): sha(path) for path in inputs}
    result = {'started_utc': datetime.now(timezone.utc).isoformat(), 'completed': False,
              'candidate_sha256': sha(candidate), 'cases': [], 'teacher_queries': 0,
              'independent_of_training_or_selection': False, 'used_for_retraining_or_selection': False,
              'input_sha256': bindings}
    tick = time.monotonic()
    with chess.engine.SimpleEngine.popen_uci([str(engine_path), 'uci'], timeout=30) as engine:
        for entry in decisions:
            options = dict(entry['options'], NeuralModelFile=str(candidate))
            search = _search(engine, source_board(entry['root']), options, result['candidate_sha256'])
            result['cases'].append({'root_id': entry['root']['query_id'], 'family_id': entry['root']['family_id'],
                                    'partition': entry['root']['partition'], 'teacher_move': entry['teacher_move'],
                                    'warm_move': entry['student_move'], 'candidate_move': search['move_uci'],
                                    'search': search})
    groups = defaultdict(list)
    for case in result['cases']: groups[case['partition']].append(case)
    result['by_partition'] = {p: {
        'roots': len(cases),
        'warm_matches': sum(c['warm_move'] == c['teacher_move'] for c in cases),
        'candidate_matches': sum(c['candidate_move'] == c['teacher_move'] for c in cases),
        'retained_warm_matches': sum(c['warm_move'] == c['teacher_move'] == c['candidate_move'] for c in cases),
        'changed_moves': sum(c['warm_move'] != c['candidate_move'] for c in cases)} for p, cases in groups.items()}
    for path, digest in bindings.items(): require(sha(path) == digest, 'Input changed during development check')
    result.update(completed=True, actual_searches=len(result['cases']), elapsed_seconds=time.monotonic() - tick)
    write(output, result)
    print(json.dumps({'by_partition': result['by_partition'], 'searches': len(result['cases'])}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path); parser.add_argument('candidate', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    check(args.dataset.resolve(), args.candidate.resolve(), args.output.resolve())
