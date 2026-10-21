"""Teach root move differences through verified fixed-search support leaves.

Teacher targets describe the two ROOT CHILD values. Only their gap is learned
through the student's frozen minimax support leaves; these are not invented
Stockfish labels for the leaf positions. Existing point labels stay unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json
import shutil
import subprocess
import time

import chess
import chess.engine

import neural_training as base
import neural_stm as stm
from prepare_neural_huber import read, require, sha, write
from prepare_nnue_generalization import lines, jsonl
from stockfish_teacher import label_rows, source_board, _json_info
from train_neural_stm import ROOT
from train_nnue_loss_repair import load_bank
from train_nnue_ordering import load_pairs as old_pairs
from nnue_choice_pairs import validate_pair, load_pairs

OLD = ROOT / 'docs/checks/2026-10-22-nnue-leaf/data-001'
ROOTS = ROOT / 'docs/checks/2026-10-22-nnue-generalization/data-001/selected-roots.json'
WARM = ROOT / 'docs/checks/2026-10-22-nnue-preservation/attempt-002/model.nnue'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'


def prepare(output, gate_path, collector):
    started = time.monotonic()
    require(not output.exists(), 'Refuse to overwrite or repeat collection')
    gate = read(gate_path)
    blocked_path = gate_path.parent / 'blocked-features.json'
    require(sha(blocked_path) == gate['blocked_features_sha256'], 'Gate block changed')
    blocked = set(read(blocked_path))
    require(sha(WARM) == gate['warm_sha256'], 'Current research model changed')
    model = stm.load_model(WARM)
    old_manifest, rows, _ = load_bank(OLD)
    existing, _ = old_pairs(OLD, rows, old_manifest)
    roots = read(ROOTS)
    require(len(roots) == 64, 'Keep fixed32sourcefamilies64roots')
    profile = read(PROFILE)
    teacher = Path(profile['binary']['path'])
    require(sha(teacher) == profile['binary']['sha256'], 'Teacher binary changed')
    engine_path = Path(gate['binary_path'])
    require(sha(engine_path) == gate['binary_sha256'], 'Native binary changed')
    sources = [Path(__file__), ROOT / 'scripts/nnue_choice_pairs.py',
               ROOT / 'scripts/collect_choice_leaves.cpp', ROOT / 'scripts/choice_leaf_support.hpp',
               ROOT / 'scripts/build_choice.ps1', ROOT / 'scripts/stockfish_teacher.py',
               ROOT / 'scripts/neural_stm.py', ROOT / 'scripts/neural_training.py']
    inputs = [*sources, ROOTS, WARM, collector, teacher, PROFILE, engine_path, gate_path,
              blocked_path, gate_path.parent / 'frozen-roots.json', OLD / 'manifest.json',
              *[OLD / name for name in (*old_manifest['files'], *old_manifest['pair_files'])]]
    output.mkdir(parents=True)
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(),
                'fixed_roots': 64, 'source_family_split': 'Original24train/8tuning; two roots per family',
                'selection': 'Only root teacher/current default student disagreements; no outcome-driven refill',
                'maximum_teacher_queries': 192, 'teacher_nodes': 100000, 'teacher_depth': 10,
                'maximum_default_student_searches': 64, 'default_student_nodes': 10000,
                'maximum_forced_branch_searches': 128, 'branch_depth': 3, 'branch_nodes_ceiling': 10000,
                'branch_profile': 'TT/PVS/LMR off; fixed-depth forced root; actual static support witness required',
                'target': 'Gap of exact Stockfish white CP after the two root moves',
                'prediction': 'Difference of verified support leaf NNUE white CP = frozen student branch search gap',
                'limitation': 'Frozen minimax support surrogate, not literal Stockfish static leaf labels; collection profile differs from live default search',
                'point_data_and_old_pairs_byte_unchanged': True, 'no_gate_training_or_refill': True,
                'input_sha256': {str(p): sha(p) for p in inputs}}
    write(output / 'protocol.json', protocol)
    write(output / 'selected-roots.json', roots)
    for path in sources:
        dest = output / 'source-snapshot' / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    root_labels, root_report = label_rows(teacher, roots, output / 'root-teacher',
        nodes=100000, max_depth=10, max_abs_cp=1500)
    root_receipts = {r['source_query_id']: r for r in lines(output / 'root-teacher/teacher-queries.jsonl')}
    options = {**read(ROOT / 'config/engine-profiles/classical.json')['options'],
               'UseNNUEEvaluation': True, 'NeuralModelFile': str(WARM)}
    branches, decisions, rejected = [], [], Counter()
    tick = time.monotonic()
    with chess.engine.SimpleEngine.popen_uci([str(engine_path), 'uci'], timeout=30) as engine:
        engine.configure(options)
        for root in root_labels:
            board = source_board(root)
            require(base.feature_identity(board) not in blocked, 'Root overlaps gate')
            engine.configure({'Clear Hash': None})
            info = engine.analyse(board, chess.engine.Limit(nodes=10000), game=object(), info=chess.engine.INFO_ALL)
            require(info.get('pv') and info['pv'][0] in board.legal_moves, 'Student selection missing or illegal')
            student = info['pv'][0].uci()
            recommended = root_receipts[root['query_id']]['bestmove_uci']
            decisions.append({'root': root, 'teacher_move': recommended, 'student_move': student,
                              'options': options, 'raw_info': _json_info(info)})
            if student == recommended:
                rejected['already_teacher_first_move'] += 1
                continue
            branches.append({'root': root, 'move_a': recommended, 'move_b': student})
    default_seconds = time.monotonic() - tick
    write(output / 'student-choices.json', decisions)
    tsv = ''.join(root['root']['query_id'] + '-' + side + '\t' + root['root']['initial_fen'] + '\t'
                  + ' '.join(root['root']['history']) + '\t' + root['move_' + side] + '\n'
                  for root in branches for side in ('a', 'b'))
    (output / 'collector-input.tsv').write_text(tsv, encoding='utf-8')
    tick = time.monotonic()
    with (output / 'branch-receipts.jsonl').open('w', encoding='utf-8', newline='\n') as stdout, \
            (output / 'collector-stderr.txt').open('w', encoding='utf-8') as stderr:
        result = subprocess.run([str(collector), '--model', str(WARM), '--depth', '3', '--nodes', '10000'],
                                input=tsv, text=True, stdout=stdout, stderr=stderr, timeout=120)
    require(result.returncode == 0, 'Branch collector failed; partial outputs retained')
    branch_seconds = time.monotonic() - tick
    native = {r['id']: r for r in lines(output / 'branch-receipts.jsonl')}
    require(len(native) == len(branches) * 2, 'Missing or duplicate forced branch receipts')
    children, usable = [], []
    for pair in branches:
        root = pair['root']
        support = [native[root['query_id'] + '-' + side] for side in ('a', 'b')]
        if not all(r['accepted'] for r in support):
            rejected['unproved_or_incomplete_branch'] += 1
            continue
        usable.append(pair)
        for side in ('a', 'b'):
            child = source_board(root); child.push_uci(pair['move_' + side])
            require(base.feature_identity(child) not in blocked, 'Root child overlaps gate')
            children.append({'query_id': root['query_id'] + '-' + side,
                             'family_id': root['family_id'], 'partition': root['partition'],
                             'initial_fen': root['initial_fen'], 'history': root['history'] + [pair['move_' + side]],
                             'fen': child.fen(en_passant='fen'), 'target_white_cp': 0,
                             'kind': 'root_child_teacher_value'})
    write(output / 'selected-root-children.json', children)
    print(json.dumps({'default_student_searches': len(decisions), 'forced_branch_searches': len(native),
                      'complete_branch_pairs': len(usable), 'root_child_teacher_queries': len(children)}), flush=True)
    child_labels, child_report = label_rows(teacher, children, output / 'child-teacher',
        nodes=100000, max_depth=10, max_abs_cp=1500)
    labels = {r['query_id']: r for r in child_labels}
    receipts = {r['source_query_id']: r for r in lines(output / 'child-teacher/teacher-queries.jsonl')}
    candidates = []
    for pair in usable:
        root = pair['root']; record = {'kind': 'search_backed_gap', 'root_id': root['query_id'],
            'initial_fen': root['initial_fen'], 'root_history': root['history'], 'root_fen': root['fen'],
            'family_id': root['family_id'], 'partition': root['partition'],
            'root_sign': 1 if source_board(root).turn else -1, 'move_a': pair['move_a'], 'move_b': pair['move_b']}
        if not all(root['query_id'] + '-' + side in labels for side in ('a', 'b')):
            rejected['missing_exact_root_child'] += 1
            continue
        for side in ('a', 'b'):
            key = root['query_id'] + '-' + side
            branch = {**native[key], 'model_sha256': sha(WARM)}
            leaf = branch['support_leaf']; label = labels[key]
            record[side] = {'fen': leaf['fen'], 'history': leaf['history'], 'initial_fen': root['initial_fen'],
                            'feature_hash': base.feature_identity(chess.Board(leaf['fen'])),
                            'family_id': root['family_id'], 'partition': root['partition'],
                            'target_white_cp': label['target_white_cp'],
                            'teacher_root_child_white_cp': label['target_white_cp'],
                            'target_semantics': 'teacher_root_child_value_surrogate_at_backed_leaf',
                            'root_child': {'fen': label['fen'], 'history': label['history'], 'teacher_receipt': receipts[key]},
                            'support_receipt': branch}
        record['target_gap_white_cp'] = record['a']['target_white_cp'] - record['b']['target_white_cp']
        if record['a']['feature_hash'] == record['b']['feature_hash']:
            rejected['same_leaf_input'] += 1
            continue
        features = validate_pair(record, model)
        candidates.append((record, features))
    baseline = {p: {r['feature_hash'] for r in group} for p, group in rows.items()}
    for part, group in existing.items():
        baseline[part].update(base.feature_identity(chess.Board(fen)) for pair in group
            for fen in (pair['root_fen'], pair['a']['fen'], pair['b']['fen']))
    feature_splits = defaultdict(set)
    for pair, features in candidates:
        for identity in features: feature_splits[identity].add(pair['partition'])
    cross = {h for h, parts in feature_splits.items() if len(parts) > 1}
    backed = {'train': [], 'tuning': []}
    comparison_keys = {p: {(r['root_id'], tuple(sorted((r['move_a'], r['move_b']))))
                          for r in group} for p, group in existing.items()}
    for pair, features in candidates:
        part = pair['partition']
        key = (pair['root_id'], tuple(sorted((pair['move_a'], pair['move_b']))))
        if key in comparison_keys[part]:
            rejected['duplicate_old_or_new_comparison'] += 1
            continue
        other = set().union(*(group_ids for p, group_ids in baseline.items() if p != part))
        if features & (blocked | cross | other):
            rejected['gate_or_cross_partition_input'] += 1
            continue
        backed[part].append(pair)
        comparison_keys[part].add(key)
    require(all(backed.values()), 'Need verified new comparisons in both source partitions')
    for name in (*old_manifest['files'], *old_manifest['pair_files']):
        shutil.copyfile(OLD / name, output / name)
    for part in backed: jsonl(output / ('pairs-backed-' + part + '.jsonl'), backed[part])
    manifest = {**old_manifest, 'source_protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
                'gate_plan': str(gate_path), 'gate_overlap': 0,
                'warm_model': {'path': str(WARM), 'sha256': sha(WARM)},
                'gate': {'blocked_features': str(blocked_path), 'blocked_features_sha256': sha(blocked_path)},
                'backed_pair_files': {'pairs-backed-' + p + '.jsonl': sha(output / ('pairs-backed-' + p + '.jsonl')) for p in backed},
                'backed_pair_rows': {p: len(v) for p, v in backed.items()},
                'combined_pair_rows': {p: len(v) + len(existing[p]) for p, v in backed.items()},
                'backed_pair_target': protocol['target'], 'backed_pair_prediction': protocol['prediction'],
                'selection_exclusions': dict(rejected), 'default_student_searches': len(decisions),
                'forced_branch_searches': len(native), 'default_student_seconds': default_seconds,
                'forced_branch_seconds': branch_seconds, 'teacher_queries': len(roots) + len(children),
                'teacher_seconds': root_report['elapsed_seconds'] + child_report['elapsed_seconds'],
                'elapsed_seconds': time.monotonic() - started}
    write(output / 'manifest.json', manifest)
    _, checked, _ = load_bank(output); load_pairs(output, checked, manifest)
    for path, digest in protocol['input_sha256'].items(): require(sha(path) == digest, 'Collection input changed: ' + path)
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path); parser.add_argument('--gate-plan', type=Path, required=True)
    parser.add_argument('--collector', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.output.resolve(), args.gate_plan.resolve(), args.collector.resolve())
