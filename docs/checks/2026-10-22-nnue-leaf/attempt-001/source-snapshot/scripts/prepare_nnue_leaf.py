"""Label bounded samples from actual native NNUE evaluation calls.

The observer samples evaluated occurrences, including unfinished iterations and
stand-pat cutoffs. These are not asserted to be the final principal-variation
leaves. Teacher scores, rather than Regicide's recorded scores, are the targets.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time

import chess
import neural_stm as stm
import neural_training as base
from prepare_neural_huber import read, require, sha, write
from prepare_nnue_generalization import jsonl, lines
from stockfish_teacher import label_rows, source_board
from train_neural_stm import ROOT
from train_nnue_loss_repair import load_bank, replay_weights
from train_nnue_ordering import load_pairs

OLD = ROOT / 'docs/checks/2026-10-22-nnue-generalization/data-001'
ROOTS = OLD / 'selected-roots.json'
WARM = ROOT / 'docs/checks/2026-10-22-nnue-ordering/attempt-001/model.nnue'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'
SEED = 2026102204
WEIGHT = 4


def witness_board(receipt, sample, root, model):
    """Authenticate an observed static call, with complete repetition history."""
    require(receipt['id'] == root['query_id'], 'Collector root id differs')
    require(receipt['initial_fen'] == root['initial_fen']
            and receipt['root_history'] == root['history'], 'Collector source history differs')
    require(receipt['root_fen'] == root['fen'], 'Collector root FEN differs')
    require(sample['history'] == root['history'] + sample['path_from_root'], 'Leaf path differs')
    require(sample['ply'] == len(sample['path_from_root']) > 0, 'Not a descendant evaluation')
    require(sample['phase'] in ('quiescence_stand_pat', 'normal_horizon'), 'Not an actual static call')
    require(not sample['in_check'] and not sample['current_draw_claim']
            and not sample['draw_claim_adjusted'], 'Checked or claim-adjusted observation')
    board = source_board({'initial_fen': root['initial_fen'], 'history': sample['history'], 'fen': sample['fen']})
    require(board.is_valid() and not board.is_check(), 'Invalid or checked leaf')
    ix, _, turns = stm.pack_records([{'fen': sample['fen'], 'target_white_cp': 0}])
    raw = int(stm.predict(model, ix, turns)[0]) * (1 if board.turn else -1)
    require(raw == sample['raw_score_cp'], 'Witness is not the recorded NNUE static evaluation')
    return board


def select_witnesses(receipts, roots, background, blocked, model):
    require(len(receipts) == len(roots), 'Missing native root receipts')
    by_id = {r['id']: r for r in receipts}
    require(len(by_id) == len(roots), 'Duplicate native root receipts')
    used = {base.feature_identity(chess.Board(r['fen'])) for rows in background.values() for r in rows}
    candidates, splits, reasons = [], defaultdict(set), Counter()
    for root in roots:
        receipt = by_id[root['query_id']]
        require(receipt['nodes_limit'] == 10000 and receipt['sampling']['capacity'] == 4
                and receipt['sampling']['seed'] == SEED, 'Collector budget or seed differs')
        require(receipt['search']['nodes'] >= 10000, 'Incomplete fixed-node collection')
        for sample in receipt['samples']:
            board = witness_board(receipt, sample, root, model)
            if board.outcome(claim_draw=True) is not None:
                reasons['terminal_or_claimable_leaf'] += 1
                continue
            identity = base.feature_identity(board)
            if identity in used or identity in blocked:
                reasons['background_or_gate_input'] += 1
                continue
            splits[identity].add(root['partition'])
            candidates.append((root, sample, identity))
    cross = {h for h, groups in splits.items() if len(groups) > 1}
    # Choose without inspecting teacher answers, two unique inputs per root.
    chosen, per_root = [], Counter()
    import hashlib
    key = lambda entry: hashlib.sha256((str(SEED) + entry[0]['query_id'] + entry[2]).encode()).hexdigest()
    for root, sample, identity in sorted(candidates, key=key):
        if identity in cross or identity in used or per_root[root['query_id']] >= 2:
            continue
        used.add(identity)
        per_root[root['query_id']] += 1
        board = chess.Board(sample['fen'])
        chosen.append({'query_id': 'leaf-' + root['query_id'] + '-' + str(per_root[root['query_id']]),
                       'family_id': root['family_id'], 'partition': root['partition'],
                       'kind': 'actual_search_static_leaf', 'initial_fen': root['initial_fen'],
                       'history': sample['history'], 'fen': sample['fen'], 'feature_hash': identity,
                       'source_root_id': root['query_id'], 'source_root_fen': root['fen'],
                       'witness': sample, 'target_white_cp': 0, 'replay_weight': WEIGHT,
                       'mover': 'white' if board.turn else 'black', 'piece_count': len(board.piece_map())})
    require(len(chosen) <= 128, 'Teacher query budget exceeded')
    counts = Counter(r['partition'] for r in chosen)
    require(counts['train'] and counts['tuning'], 'Need leaf data from both source partitions')
    return sorted(chosen, key=lambda r: r['query_id']), {'excluded_cross_split_inputs': len(cross), **reasons}


def prepare(output, gate_path, collector):
    started = time.monotonic()
    require(not output.exists(), 'Refuse to overwrite or repeat leaf collection')
    gate = read(gate_path)
    blocked_path = gate_path.parent / 'blocked-features.json'
    require(sha(blocked_path) == gate['blocked_features_sha256'], 'Fresh gate exclusion changed')
    blocked = set(read(blocked_path))
    manifest, background, _ = load_bank(OLD)
    pairs, _ = load_pairs(OLD, background, manifest)
    roots = read(ROOTS)
    require(len(roots) == 64, 'Expected fixed original32families64roots')
    model = stm.load_model(WARM)
    require(sha(WARM) == gate['warm_sha256'], 'Warm model changed')
    profile = read(PROFILE)
    teacher = Path(profile['binary']['path'])
    require(sha(teacher) == profile['binary']['sha256'], 'Stockfish binary changed')
    inputs = [Path(__file__), ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_stm.py',
              ROOT / 'scripts/neural_training.py', ROOT / 'scripts/prepare_nnue_generalization.py',
              collector, ROOT / 'scripts/collect_search_leaves.cpp', ROOT / 'src/search.cpp',
              ROOT / 'include/regicide/search.hpp', ROOTS, WARM, gate_path, blocked_path,
              gate_path.parent / 'frozen-roots.json', OLD / 'manifest.json', PROFILE, teacher,
              ROOT / 'docs/checks/2026-10-22-nnue-leaf/native-parity.json',
              *[OLD / n for n in (*manifest['files'], *manifest['pair_files'])]]
    for group in background.values():
        require(not {r['feature_hash'] for r in group} & blocked, 'Background leaks into gate')
    output.mkdir(parents=True)
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'seed': SEED,
                'change': 'Actual evaluated static-call distribution; no PV endpoint inference',
                'roots': 64, 'source_families': 32, 'native_nodes_per_root': 10000,
                'reservoir_capacity_per_root': 4, 'selected_leaves_per_root_maximum': 2,
                'maximum_teacher_queries': 128, 'teacher_nodes': 50000, 'teacher_depth': 10,
                'leaf_replay_weight': WEIGHT, 'existing_points_and_pairs_preserved': True,
                'sampling_limitations': 'Occurrence reservoir includes unfinished iterations and stand-pat cutoffs; not only final PV leaves. Checked, draw-claim and adjusted calls excluded.',
                'gate_plan': str(gate_path), 'no_refill_or_retry': True,
                'input_sha256': {str(p): sha(p) for p in inputs}}
    write(output / 'protocol.json', protocol)
    for path in inputs:
        if path.suffix in ('.py', '.cpp', '.hpp'):
            target = output / 'source-snapshot' / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    tsv = ''.join(r['query_id'] + '\t' + r['initial_fen'] + '\t' + ' '.join(r['history']) + '\n' for r in roots)
    (output / 'collector-input.tsv').write_text(tsv, encoding='utf-8')
    tick = time.monotonic()
    with (output / 'native-leaf-receipts.jsonl').open('w', encoding='utf-8', newline='\n') as stdout, \
            (output / 'collector-stderr.txt').open('w', encoding='utf-8') as stderr:
        result = subprocess.run([str(collector), '--model', str(WARM), '--nodes', '10000',
                                 '--samples', '4', '--seed', str(SEED)], input=tsv,
                                text=True, stdout=stdout, stderr=stderr, timeout=120)
    require(result.returncode == 0, 'Native leaf collector failed; preserve partial receipts')
    native_seconds = time.monotonic() - tick
    receipts = lines(output / 'native-leaf-receipts.jsonl')
    comparison_inputs = [
        {'fen': fen} for group in pairs.values() for pair in group
        for fen in (pair['root_fen'], pair['a']['fen'], pair['b']['fen'])]
    selected, exclusions = select_witnesses(receipts, roots,
        {**background, 'existing_comparisons': comparison_inputs}, blocked, model)
    write(output / 'selected-leaves.json', selected)
    print(json.dumps({'native_seconds': native_seconds, 'selected_leaf_queries': len(selected),
                      'selected_by_partition': dict(Counter(r['partition'] for r in selected))}), flush=True)
    require(sha(teacher) == profile['binary']['sha256'], 'Stockfish binary changed')
    labels, report = label_rows(teacher, selected, output / 'leaf-teacher', nodes=50000,
                                max_abs_cp=1500, max_depth=10)
    require(all(r['feature_hash'] not in blocked for r in labels), 'Leaf label overlaps gate')
    for row in labels:
        background[row['partition']].append(row)
    require(all(any(r['kind'] == 'actual_search_static_leaf' for r in background[p])
                for p in ('train', 'tuning')), 'No exact leaf labels in a source partition')
    for partition, rows in background.items():
        jsonl(output / (partition + '.jsonl'), rows)
    for partition in pairs:
        shutil.copyfile(OLD / ('pairs-' + partition + '.jsonl'), output / ('pairs-' + partition + '.jsonl'))
    require(len(pairs['train']) >= (int(replay_weights(background['train']).sum()) + 127) // 128,
            'Existing comparisons do not cover every point batch')
    result = {'schema_version': 1, 'files': {p + '.jsonl': sha(output / (p + '.jsonl')) for p in background},
              'rows': {p: len(rows) for p, rows in background.items()},
              'pair_files': manifest['pair_files'], 'pair_rows': manifest['pair_rows'],
              'anchor': manifest['anchor'], 'source_protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
              'new_leaf_rows': dict(Counter(r['partition'] for r in labels)),
              'new_leaf_families': {p: len({r['family_id'] for r in labels if r['partition'] == p}) for p in ('train', 'tuning')},
              'gate_plan': str(gate_path), 'gate_overlap': 0, 'selection_exclusions': exclusions,
              'actual_native_searches': len(receipts), 'native_seconds': native_seconds,
              'observed_static_evaluations': sum(r['sampling']['total_static_evaluations'] for r in receipts),
              'teacher_queries': len(selected), 'teacher_seconds': report['elapsed_seconds'],
              'existing_test_bytes_preserved': sha(output / 'test.jsonl') == manifest['files']['test.jsonl'],
              'test_limit': 'Old69already exposed; newly reserved gate separate from all gradients and checkpoint selection',
              'elapsed_seconds': time.monotonic() - started}
    # These existing loaders recompute actual FEN features and reject split leakage.
    write(output / 'manifest.json', result)
    _, checked, _ = load_bank(output)
    load_pairs(output, checked, result)
    for path, digest in protocol['input_sha256'].items():
        require(sha(path) == digest, 'Frozen collection input changed: ' + path)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--gate-plan', type=Path, required=True)
    parser.add_argument('--collector', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.output.resolve(), args.gate_plan.resolve(), args.collector.resolve())
