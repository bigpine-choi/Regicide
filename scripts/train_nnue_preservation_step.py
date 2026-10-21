"""One guarded MSE fit preserving old TRAIN predictions while learning actual leaves.

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
import neural_stm_preservation_step as ordering
import neural_training as base
from prepare_neural_huber import read, require, sha, special_positions, write
from train_neural_stm import ROOT
from train_nnue_diverse import diversity_summary
from train_nnue_loss_repair import load_bank, pack_replayed, replay_weights
from train_nnue_ordering import load_pairs, pack_pairs, score_partition, score_pairs

WARM = ROOT / 'docs/checks/2026-10-22-nnue-ordering/attempt-001'
WARM_SHA = '4e54305bce4a495ff3bb0d961e397f78eac12d387ff30e7766202fcdf7d3bdee'
ANCHOR = ROOT / 'docs/checks/2026-10-22-nnue-diversity/data-001/tuning.jsonl'
CONFIG = {'candidate_id': 'sf18-mse-stm-preservation-step-20261020', 'input_scale': 32,
          'epochs': 500, 'input_bias_lr': .0002, 'output_lr': .005,
          'seed': 20261020, 'batch_size': 128, 'loss': 'mse'}
GUARDS = {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}
PARTITIONS = ('train', 'tuning', 'test')
PRESERVATION_WEIGHT = 4.0
PRESERVATION_BANK = ROOT / 'docs/checks/2026-10-22-nnue-generalization/data-001'


def train(dataset, output, gate_plan):
    started = time.monotonic()
    fresh_gate = read(gate_plan)
    require(isinstance(fresh_gate, dict), 'Fresh gate plan must be a JSON object')
    manifest, rows, data_hashes = load_bank(dataset)
    pairs, pair_hashes = load_pairs(dataset, rows, manifest)
    preservation_path = PRESERVATION_BANK / 'train.jsonl'
    preservation_manifest = read(PRESERVATION_BANK / 'manifest.json')
    require(sha(preservation_path) == preservation_manifest['files']['train.jsonl'], 'Old TRAIN bytes changed')
    preservation_rows = [json.loads(line) for line in preservation_path.read_text(encoding='utf-8').splitlines()]
    require(len(preservation_rows) == 1426, 'Preserve the fixed old TRAIN1426 inputs only')
    require({r['feature_hash'] for r in preservation_rows} <= {r['feature_hash'] for r in rows['train']}, 'Preservation inputs must remain TRAIN-only')
    blocked_path = gate_plan.parent / 'blocked-features.json'
    roots_path = gate_plan.parent / 'frozen-roots.json'
    require(sha(blocked_path) == fresh_gate['blocked_features_sha256']
            and sha(roots_path) == fresh_gate['frozen_roots_sha256'], 'Sealed gate inputs changed')
    blocked = set(read(blocked_path))
    supervised = {row['feature_hash'] for group in rows.values() for row in group}
    for group in pairs.values():
        for pair in group:
            supervised.update(base.feature_identity(chess.Board(fen))
                              for fen in (pair['root_fen'], pair['a']['fen'], pair['b']['fen']))
    require(not supervised & blocked, 'Fresh gate root/child leaked into point or pair supervision')
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
    engine = Path(fresh_gate['binary_path'])
    tester = engine.parent / ('regicide_neural_model_tests' + engine.suffix)
    cached_path = WARM / 'validation.json'
    cached = read(cached_path)
    old_engine = ROOT / 'build-m3-stm/regicide.exe'
    parity_path = ROOT / 'docs/checks/2026-10-22-nnue-leaf/native-parity.json'
    parity = read(parity_path)
    require(parity['passed'] and parity['parity_cases'] == 36
            and parity['old_binary_sha256'] == sha(old_engine) == cached['input_sha256'][str(old_engine)]
            and parity['new_binary_sha256'] == sha(engine) == fresh_gate['binary_sha256'],
            'Observer build must preserve the authenticated prior search behavior')
    sources = [Path(__file__), *[ROOT / 'scripts' / name for name in (
        'neural_stm_preservation_step.py', 'neural_stm_preservation.py', 'train_nnue_leaf.py', 'prepare_nnue_leaf.py', 'prepare_nnue_generalization.py', 'train_nnue_ordering.py', 'neural_stm_ordering.py', 'neural_stm.py', 'neural_precision.py', 'neural_training.py',
        'neural_qat.py', 'train_nnue_diverse.py', 'train_nnue_loss_repair.py',
        'neural_stm_guarded.py', 'prepare_neural_huber.py', 'train_neural_stm.py')]]
    inputs = sources + [gate_plan, blocked_path, roots_path, dataset / 'manifest.json', *[dataset / name for name in (*data_hashes, *pair_hashes)],
                       preservation_path, PRESERVATION_BANK / 'manifest.json',
                       anchor_path, anchor_path.parent / 'manifest.json', WARM / 'float_weights.npz',
                       WARM / 'model.nnue', WARM / 'training.json', cached_path, old_engine, parity_path, engine, tester,
                       ROOT / 'config/engine-profiles/classical.json']
    protocol = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'config': CONFIG, 'guards': GUARDS,
        'architecture': {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                         'hidden': 16, 'changed': False, 'added_parameters': 0},
        'loss_definition': 'point teacher MSE + 1.0 * sibling teacher gap MSE + 4.0 * old-TRAIN frozen-warm-prediction MSE; all normalized by10000 for gradients',
        'pair_weight': 1.0, 'change': 'Same fixed preservation MSE; bound each Adam proposal by full TRAIN deployed-integer objective nonincrease with scales1..1/32, otherwise skip; no heldout step choice',
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
        'preservation': {'weight': PRESERVATION_WEIGHT, 'rows': len(preservation_rows),
                         'path': str(preservation_path), 'sha256': sha(preservation_path),
                         'target': 'Frozen initial quantized white-cp output, not replacement teacher labels',
                         'gradients': 'Old TRAIN1426 only; complete normalized preservation mean per point minibatch',
                         'heldout_inputs_or_labels_in_gradients': False},
        'anchor': {'path': str(anchor_path), 'sha256': sha(anchor_path), 'rows': len(anchor_rows),
                   'policy': 'Old263 integer MSE <= epoch-zero MSE; no anchor gradients'},
        'selection': 'One seed/run; minimum tuning point MSE + pair-gap MSE subject to anchor guard; epoch0 protection and best restoration',
        'gate_policy': fresh_gate,
        'gate_plan': {'path': str(gate_plan), 'sha256': sha(gate_plan),
                      'used_in_training_selection': False},
        'comparison': 'Only after separately frozen unseen-family move-choice gate passes: basic classical Regicide,16 games,100ms',
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
    preservation = stm.pack_records(preservation_rows)
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
        anchor_turns=anchor[2], progress=progress,
        pair_family_ids=[pair['family_id'] for pair in pairs['train']],
        preservation_indices=preservation[0], preservation_turns=preservation[2],
        preservation_weight=PRESERVATION_WEIGHT, **GUARDS)
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
    old_warm = stm.predict(warm, preservation[0], preservation[2])
    old_new = stm.predict(model, preservation[0], preservation[2])
    drift = old_new - old_warm
    preservation_metrics = {**base.error_metrics(old_new, old_warm),
                            'rows': len(preservation_rows), 'weight': PRESERVATION_WEIGHT,
                            'changed_predictions': int(np.sum(drift != 0)), 'max_absolute_change_cp': int(np.max(np.abs(drift))),
                            'old_teacher_metrics': score_partition(preservation_rows, warm, model)}
    report = {'preservation_metrics': preservation_metrics, 'config': CONFIG, 'guards': GUARDS, 'candidate_runs': 1, 'training_wall_seconds': learning_seconds,
              'selection': selection, 'metrics': metrics, 'pair_metrics': pair_metrics, 'anchor_metrics': anchor_metrics,
              'leaf_metrics': {p: score_partition([r for r in rows[p] if r.get('kind') == 'actual_search_static_leaf'], warm, model) for p in ('train', 'tuning')},
              'model_sha256': sha(output / 'model.nnue'), 'model_changed_from_warm': stm.model_checksum(model) != stm.model_checksum(warm),
              'architecture_unchanged': True, 'loss': 'mse', 'loss_definition': protocol['loss_definition'],
              'history': history, 'test_evaluations': 1, 'test_used_in_selection': False,
              'test_scored_after_saved_selection': True, 'move_choice_gate_used_in_selection': False,
              'interpretation': 'Point/pair MSE and a finite unseen-family move-choice screen do not establish playing strength.'}
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
        'observer_build_parity': {'path': str(parity_path), 'sha256': sha(parity_path), 'cases': 36},
        'input_sha256': {str(path): sha(path) for path in (engine, tester, output / 'model.nnue')},
        'elapsed_wall_seconds': time.monotonic() - started})
    print(json.dumps({'training_seconds': learning_seconds, 'selection': selection,
                      'pair_metrics': pair_metrics, 'model_sha256': report['model_sha256']}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--gate-plan', type=Path, required=True)
    args = parser.parse_args()
    train(args.dataset.resolve(), args.output.resolve(), args.gate_plan.resolve())
