"""One fixed-topology guarded fit of point MSE plus sibling score-gap MSE.

Pair child target_white_cp is authoritative and normalized teacher metadata must
agree. Frozen teacher receipts authenticate those labels separately. No post-fit
move-choice gate influences model selection.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import chess
import numpy as np

import neural_stm as stm
import neural_stm_ordering as ordering
import neural_training as base
from prepare_neural_huber import read, require, sha, special_positions, write
from train_neural_stm import ROOT
from train_nnue_diverse import diversity_summary
from train_nnue_loss_repair import load_bank, pack_replayed, replay_weights
from train_nnue_loss_repair import score_partition as _score_partition

WARM = ROOT / 'docs/checks/2026-10-22-nnue-loss-repair/attempt-001'
WARM_SHA = '02ccdf9b7fd1ac6206774bbc356081977db26cc2c8610176f732ff6ceadc9a09'
ANCHOR = ROOT / 'docs/checks/2026-10-22-nnue-diversity/data-001/tuning.jsonl'
CONFIG = {'candidate_id': 'sf18-mse-stm-ordering-20261020', 'input_scale': 32,
          'epochs': 500, 'input_bias_lr': .0002, 'output_lr': .005,
          'seed': 20261020, 'batch_size': 128, 'loss': 'mse'}
GUARDS = {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}
PARTITIONS = ('train', 'tuning', 'test')


def load_pairs(dataset, rows, manifest):
    """Authenticate sibling pairs and disallow every cross-split feature/family.

    A position can participate in several comparisons in its own partition.
    That repeated visit is not another independent position or teacher label.
    """
    identities = {p: {base.feature_identity(chess.Board(r['fen'])) for r in rows[p]}
                  for p in PARTITIONS}
    families = {p: {r['family_id'] for r in rows[p]} for p in PARTITIONS}
    pairs, hashes = {}, {}
    for partition in ('train', 'tuning'):
        name = 'pairs-' + partition + '.jsonl'
        expected = manifest.get('pair_files', {}).get(name)
        if isinstance(expected, dict):
            expected = expected.get('sha256')
        require(isinstance(expected, str) and len(expected) == 64, 'Missing pair SHA256')
        hashes[name] = sha(dataset / name)
        require(hashes[name] == expected, 'Pair bytes differ from manifest')
        records = [json.loads(line) for line in (dataset / name).read_text(encoding='utf-8').splitlines()]
        count = manifest.get('pair_rows', {}).get(partition)
        require(type(count) is int and count == len(records) and count > 0, 'Unexpected pair count')
        seen = set()
        for pair in records:
            require(pair.get('partition') == partition, 'Pair partition differs')
            family = pair.get('family_id')
            require(isinstance(family, str) and bool(family), 'Pair source family missing')
            families[partition].add(family)
            board = chess.Board(pair['initial_fen'])
            require(board.is_valid(), 'Invalid pair initial position')
            history = pair.get('root_history')
            require(isinstance(history, list), 'Pair root history missing')
            for text in history:
                move = chess.Move.from_uci(text)
                require(move in board.legal_moves, 'Illegal pair root history')
                board.push(move)
            require(board.fen(en_passant='fen') == pair['root_fen'], 'Pair root replay differs')
            require(board.is_valid() and board.outcome(claim_draw=True) is None, 'Invalid pair root')
            require(type(pair.get('root_sign')) is int and pair['root_sign'] == (1 if board.turn else -1),
                    'Pair root color sign differs')
            identities[partition].add(base.feature_identity(board))
            require(isinstance(pair.get('root_id'), str) and bool(pair['root_id']), 'Pair root id missing')
            require(pair['move_a'] != pair['move_b'], 'Pair children must be different moves')
            key = (pair['root_id'], tuple(sorted((pair['move_a'], pair['move_b']))))
            require(key not in seen, 'Duplicate sibling comparison')
            seen.add(key)
            for letter in ('a', 'b'):
                child = pair[letter]
                target = child.get('target_white_cp')
                require(type(target) is int and abs(target) <= 1500, 'Pair needs bounded exact child CP')
                require(child.get('teacher_white_cp', target) == target, 'Pair teacher target differs')
                move = chess.Move.from_uci(pair['move_' + letter])
                require(move in board.legal_moves, 'Illegal pair child move')
                following = board.copy()
                following.push(move)
                require(following.fen(en_passant='fen') == child['fen'], 'Pair child replay differs')
                require(following.is_valid() and following.outcome(claim_draw=True) is None,
                        'Invalid or terminal pair child')
                identity = base.feature_identity(following)
                require(child.get('feature_hash') == identity, 'Pair child feature identity differs')
                require(child.get('partition', partition) == partition, 'Pair child partition differs')
                require(child.get('family_id', family) == family, 'Pair child family differs')
                if 'history' in child:
                    require(child['history'] == history + [pair['move_' + letter]], 'Pair child history differs')
                if 'initial_fen' in child:
                    require(child['initial_fen'] == pair['initial_fen'], 'Pair child initial FEN differs')
                identities[partition].add(identity)
            require(type(pair.get('target_gap_white_cp')) is int and pair['target_gap_white_cp'] ==
                    pair['a']['target_white_cp'] - pair['b']['target_white_cp'], 'Pair gap differs')
        pairs[partition] = records
    require('pairs-test.jsonl' not in manifest.get('pair_files', {}) and
            not (dataset / 'pairs-test.jsonl').exists(), 'Test cannot have optimization pairs')
    for index, left in enumerate(PARTITIONS):
        for right in PARTITIONS[index + 1:]:
            require(not (identities[left] & identities[right]), 'Point/pair features cross partitions')
            require(not (families[left] & families[right]), 'Point/pair families cross partitions')
    return pairs, hashes


def pack_pairs(pairs):
    """Pack both sides together so capture pairs use the same padded width."""
    count = len(pairs)
    indices, targets, turns = stm.pack_records([p['a'] for p in pairs] + [p['b'] for p in pairs])
    return indices[:count], targets[:count], turns[:count], indices[count:], targets[count:], turns[count:]


def score_partition(rows, warm, model, *, weighted=True):
    def rename(value):
        if isinstance(value, dict):
            return {('ordering' if key == 'repair' else key): rename(item) for key, item in value.items()}
        return value
    return rename(_score_partition(rows, warm, model, weighted=weighted))


def score_pairs(pairs, warm, model):
    ia, ta, sa, ib, tb, sb = pack_pairs(pairs)
    target = ta - tb
    signs = np.array([p['root_sign'] for p in pairs])
    clear = np.abs(target) >= 50
    result = {'count': len(pairs), 'clear_gap_ge50cp': int(clear.sum())}
    for name, net in (('warm', warm), ('ordering', model)):
        gap = stm.predict(net, ia, sa) - stm.predict(net, ib, sb)
        result[name] = {**base.error_metrics(gap, target),
                        'strict_order_matches': int(np.sum(gap * target > 0)),
                        'clear_order_matches': int(np.sum((gap * target > 0) & clear)),
                        'target_root_gaps_cp': (target * signs).astype(int).tolist(),
                        'predicted_root_gaps_cp': (gap * signs).astype(int).tolist()}
    return result


def train(dataset, output):
    started = time.monotonic()
    manifest, rows, data_hashes = load_bank(dataset)
    pairs, pair_hashes = load_pairs(dataset, rows, manifest)
    anchor_ref = manifest['anchor']
    anchor_path = Path(anchor_ref['path'] if isinstance(anchor_ref, dict) else anchor_ref).resolve()
    require(anchor_path == ANCHOR.resolve(), 'Anchor must remain original diversity tuning263')
    anchor_manifest = read(anchor_path.parent / 'manifest.json')
    require(sha(anchor_path) == anchor_manifest['files']['tuning.jsonl'], 'Original anchor bytes changed')
    anchor_rows = [json.loads(line) for line in anchor_path.read_text(encoding='utf-8').splitlines()]
    require(len(anchor_rows) == 263, 'Anchor must retain 263 original positions')
    require(sha(WARM / 'model.nnue') == WARM_SHA, 'Current guarded checkpoint changed')
    previous = read(WARM / 'training.json')
    require({k: v for k, v in previous['config'].items() if k != 'candidate_id'} ==
            {k: v for k, v in CONFIG.items() if k != 'candidate_id'}, 'Optimizer settings changed')
    require(previous['guards'] == GUARDS, 'Overfit guard settings changed')
    with np.load(WARM / 'float_weights.npz', allow_pickle=False) as archive:
        initial = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    warm = stm.load_model(WARM / 'model.nnue')
    require(stm.model_checksum(stm.quantize(initial, CONFIG['input_scale'])) == stm.model_checksum(warm),
            'Warm floats differ from actual guarded integer checkpoint')
    engine = ROOT / 'build-m3-stm/regicide.exe'
    tester = engine.parent / ('regicide_neural_model_tests' + engine.suffix)
    cached_path = WARM / 'validation.json'
    cached = read(cached_path)
    require(sha(engine) == cached['input_sha256'][str(engine)], 'Unchanged native executable differs')
    sources = [Path(__file__), *[ROOT / 'scripts' / name for name in (
        'neural_stm_ordering.py', 'neural_stm.py', 'neural_precision.py', 'neural_training.py',
        'neural_qat.py', 'train_nnue_diverse.py', 'train_nnue_loss_repair.py',
        'neural_stm_guarded.py', 'prepare_neural_huber.py', 'train_neural_stm.py')]]
    inputs = sources + [dataset / 'manifest.json', *[dataset / name for name in (*data_hashes, *pair_hashes)],
                       anchor_path, anchor_path.parent / 'manifest.json', WARM / 'float_weights.npz',
                       WARM / 'model.nnue', WARM / 'training.json', cached_path, engine, tester,
                       ROOT / 'config/engine-profiles/classical.json']
    protocol = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'config': CONFIG, 'guards': GUARDS,
        'architecture': {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                         'hidden': 16, 'changed': False, 'added_parameters': 0},
        'loss_definition': 'mean(point white-cp residual squared) + 1.0 * mean(same-root sibling white-cp gap residual squared)',
        'pair_weight': 1.0, 'change': 'Explicit sibling score-gap MSE, fixed topology and one guarded fine-tune',
        'initial': {'path': str(WARM / 'float_weights.npz'), 'sha256': sha(WARM / 'float_weights.npz'),
                    'model_sha256': WARM_SHA, 'selected_epoch': previous['selection']['selected_epoch']},
        'data': {'directory': str(dataset), 'manifest_sha256': sha(dataset / 'manifest.json'),
                 'file_sha256': {**data_hashes, **pair_hashes}, 'rows': {p: len(rows[p]) for p in PARTITIONS},
                 'pair_rows': {p: len(pairs[p]) for p in pairs},
                 'coverage': {p: diversity_summary(rows[p]) for p in PARTITIONS},
                 'point_replay_weighted_rows': {p: int(replay_weights(rows[p]).sum()) for p in PARTITIONS},
                 'pair_target_policy': 'a/b target_white_cp; normalized child teacher_white_cp must agree; receipts bind teacher provenance',
                 'partition_policy': 'Source families and mirrored piece features disjoint across point/pair partitions',
                 'test_limit': manifest.get('test_limit'), 'test_policy': 'Score once after final checkpoint saving; never optimize, stop, rank, or retry'},
        'anchor': {'path': str(anchor_path), 'sha256': sha(anchor_path), 'rows': len(anchor_rows),
                   'policy': 'Old263 integer MSE <= epoch-zero MSE; no anchor gradients'},
        'selection': 'One seed/run; minimum tuning point MSE + pair-gap MSE subject to anchor guard; epoch0 protection and best restoration',
        'gate_policy': {'teacher_bestmove_minimum': 5, 'positions': 10, 'preserve_existing_correct': 2,
                        'tuning_correct_minimum': 2, 'tuning_positions': 4,
                        'used_in_training_selection': False},
        'comparison': 'Only after separately fixed development move-choice gate passes: basic classical Regicide, 16 games,100ms',
        'environment': {'python': sys.version, 'numpy': np.__version__, 'blas_threads': 1},
        'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'input_sha256': {str(path): sha(path) for path in inputs}, 'default_changed': False,
    }
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'protocol.json', protocol)
    shutil.copyfile(WARM / 'float_weights.npz', output / 'initial-floats.npz')
    for source in sources:
        target = output / 'source-snapshot' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    packed = {p: pack_replayed(rows[p]) for p in ('train', 'tuning')}
    packed_pairs = {p: pack_pairs(pairs[p]) for p in pairs}
    anchor = stm.pack_records(anchor_rows)
    tick = time.monotonic()

    def progress(epoch, _parameters, evaluation):
        print(json.dumps({'epoch': epoch, 'tuning_objective_cp2': evaluation['mse_cp2'],
                          'anchor_mse_cp2': evaluation['anchor']['mse_cp2'],
                          'eligible': evaluation['eligible'], 'elapsed_seconds': time.monotonic() - tick}), flush=True)

    parameters, history, selection = ordering.train_candidate(
        initial, packed['train'][0], packed['train'][1], CONFIG, turns=packed['train'][2],
        pairs=packed_pairs['train'], validation_indices=packed['tuning'][0],
        validation_targets=packed['tuning'][1], validation_turns=packed['tuning'][2],
        validation_pairs=packed_pairs['tuning'], anchor_indices=anchor[0], anchor_targets=anchor[1],
        anchor_turns=anchor[2], progress=progress, **GUARDS)
    learning_seconds = time.monotonic() - tick
    np.savez(output / 'float_weights.npz', **parameters)
    stm.save_integer_model(output / 'model.nnue', parameters, CONFIG['input_scale'])
    model = stm.load_model(output / 'model.nnue')
    write(output / 'selection.json', selection)
    # Test labels are scored exactly once only after the final checkpoint is saved.
    metrics = {p: score_partition(rows[p], warm, model, weighted=(p != 'test')) for p in PARTITIONS}
    pair_metrics = {p: score_pairs(pairs[p], warm, model) for p in pairs}
    anchor_metrics = score_partition(anchor_rows, warm, model, weighted=False)
    require(anchor_metrics['ordering']['mse_cp2'] <= anchor_metrics['warm']['mse_cp2'], 'Anchor guard violated')
    objective = metrics['tuning']['weighted']['ordering']['mse_cp2'] + pair_metrics['tuning']['ordering']['mse_cp2']
    initial_objective = metrics['tuning']['weighted']['warm']['mse_cp2'] + pair_metrics['tuning']['warm']['mse_cp2']
    require(objective <= initial_objective, 'Epoch-zero objective protection violated')
    report = {'config': CONFIG, 'guards': GUARDS, 'candidate_runs': 1, 'training_wall_seconds': learning_seconds,
              'selection': selection, 'metrics': metrics, 'pair_metrics': pair_metrics, 'anchor_metrics': anchor_metrics,
              'model_sha256': sha(output / 'model.nnue'), 'model_changed_from_warm': stm.model_checksum(model) != stm.model_checksum(warm),
              'architecture_unchanged': True, 'loss': 'mse', 'loss_definition': protocol['loss_definition'],
              'history': history, 'test_evaluations': 1, 'test_used_in_selection': False,
              'test_scored_after_saved_selection': True, 'move_choice_gate_used_in_selection': False,
              'interpretation': 'Point and pair MSE fit and known development move-choice regression do not prove playing strength.'}
    write(output / 'training.json', report)
    unique = {row['fen']: row for p in PARTITIONS for row in rows[p]}
    for partition in pairs.values():
        for pair in partition:
            for child in ('a', 'b'):
                unique.setdefault(pair[child]['fen'], pair[child])
    all_rows = list(unique.values())
    specials = special_positions()
    fens = [r['fen'] for r in all_rows] + specials
    indices, _, turns = stm.pack_records(all_rows + [{'fen': fen, 'target_white_cp': 0} for fen in specials])
    expected = stm.predict(model, indices, turns) * np.where(turns == 0, 1, -1)
    values = base.evaluate_engine(engine, fens, output / 'model.nnue')
    require(np.array_equal(expected, [value[1] for value in values]), 'Python/native full/incremental mismatch')
    checked = subprocess.run([str(tester), str(output / 'model.nnue')], capture_output=True, text=True, timeout=120)
    (output / 'native-model-tests.txt').write_text(checked.stdout + checked.stderr, encoding='utf-8')
    require(checked.returncode == 0 and '0 failures' in checked.stdout, 'Actual candidate model regressions failed')
    require(all(sha(Path(path)) == value for path, value in protocol['input_sha256'].items()), 'Authenticated input changed')
    write(output / 'validation.json', {
        'positions': len(fens), 'corpus_unique_positions': len(all_rows), 'special_positions': len(specials),
        'python_native_full_incremental_mismatches': 0, 'native_model_returncode': checked.returncode,
        'native_model_summary': checked.stdout.splitlines()[-1],
        'reused_legacy_validation': {'path': str(cached_path), 'sha256': sha(cached_path)},
        'input_sha256': {str(path): sha(path) for path in (engine, tester, output / 'model.nnue')},
        'elapsed_wall_seconds': time.monotonic() - started})
    print(json.dumps({'training_seconds': learning_seconds, 'selection': selection,
                      'pair_metrics': pair_metrics, 'model_sha256': report['model_sha256']}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    train(args.dataset.resolve(), args.output.resolve())
