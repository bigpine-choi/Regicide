"""Read-only loss interference diagnosis of the rejected leaf fit.

No engine, teacher or move-choice gate is opened. One fixed five-epoch replay
reconstructs an already observed checkpoint; no model or candidate is saved.
Anchor labels are evaluation-only and never enter a gradient.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import chess
import numpy as np

import neural_stm as stm
import neural_stm_ordering as ordering
import neural_training as base
from prepare_neural_huber import read, require, sha, write
from train_neural_stm import ROOT
from train_nnue_leaf import ANCHOR, CONFIG, GUARDS, WARM, WARM_SHA
from train_nnue_loss_repair import load_bank, pack_replayed, replay_weights
from train_nnue_ordering import load_pairs, pack_pairs

DATA = ROOT / 'docs/checks/2026-10-22-nnue-leaf/data-001'
PREVIOUS = ROOT / 'docs/checks/2026-10-22-nnue-leaf/attempt-001'


def _gradient(parameters, packed):
    ix, targets, turns = packed
    return stm.qat_gradients(parameters, ix, targets, CONFIG['input_scale'], turns=turns)


def _flat(values):
    return np.concatenate([values[key].ravel() for key in stm.PARAMETER_KEYS])


def _cosine(left, right):
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(left @ right / denominator) if denominator else None


def _relations(gradients):
    result = {}
    for name, grad in gradients.items():
        result[name] = {'global_l2': float(np.linalg.norm(_flat(grad))),
                        'per_parameter_l2': {key: float(np.linalg.norm(value))
                                             for key, value in grad.items()}}
    for left, right in (('old_point', 'leaf'), ('old_point', 'pair'), ('leaf', 'pair')):
        result[left + '_vs_' + right] = {
            'cosine': _cosine(_flat(gradients[left]), _flat(gradients[right])),
            'per_parameter_cosine': {key: _cosine(gradients[left][key].ravel(),
                                                 gradients[right][key].ravel())
                                     for key in stm.PARAMETER_KEYS}}
    return result


def _metrics(parameters, packed):
    ix, targets, turns = packed
    integer, cache = stm.qat_forward(parameters, ix, CONFIG['input_scale'], turns=turns)
    shadow = stm._float_predictions(parameters, ix, turns, True)
    return {'integer': base.error_metrics(integer, targets),
            'shadow_float': base.error_metrics(shadow, targets),
            'integer_minus_shadow_mse_cp2': float(np.mean((integer - shadow) ** 2)),
            'integer_output_truncation_mse_cp2': float(np.mean((integer - cache['raw_scores']) ** 2))}


def _drift(parameters, initial, packed):
    ix, targets, turns = packed
    before, _ = stm.qat_forward(initial, ix, CONFIG['input_scale'], turns=turns)
    after, _ = stm.qat_forward(parameters, ix, CONFIG['input_scale'], turns=turns)
    old_residual, drift = before - targets, after - before
    return {'changed_predictions': int(np.sum(drift != 0)),
            'mean_drift_cp': float(drift.mean()), 'mean_abs_drift_cp': float(np.abs(drift).mean()),
            'max_abs_drift_cp': float(np.max(np.abs(drift))),
            'delta_mse_cp2': float(np.mean((after - targets) ** 2 - old_residual ** 2)),
            'drift_squared_cp2': float(np.mean(drift ** 2)),
            'residual_drift_cross_term_cp2': float(2 * np.mean(old_residual * drift)),
            'regression_decomposition': 'delta MSE = 2*mean(old residual*prediction drift) + mean(drift squared)'}


def _feature_coverage(left, right):
    # Shared input rows are reused by both board perspectives; exclude padding768.
    a = set(stm.pack_records(left)[0].ravel()) - {768}
    b = set(stm.pack_records(right)[0].ravel()) - {768}
    return {'left_piece_rows': len(a), 'right_piece_rows': len(b),
            'shared_piece_rows': len(a & b),
            'right_rows_previously_active_fraction': len(a & b) / len(b)}


def _quantized_changes(parameters, initial):
    a = stm.quantize(initial, CONFIG['input_scale'])
    b = stm.quantize(parameters, CONFIG['input_scale'])
    return {key: {'changed': int(np.sum(a[key] != b[key])), 'total': int(a[key].size),
                  'max_integer_step': int(np.max(np.abs(a[key] - b[key])))}
            for key in stm.PARAMETER_KEYS}


def _first_adam_step(initial, gradient):
    norm = float(np.linalg.norm(_flat(gradient)))
    scale = min(1., 10. / max(norm, 1e-30))
    bounds = stm._bounds(CONFIG['input_scale'])
    result = {}
    for key, values in initial.items():
        # At step one bias-corrected Adam is g / (abs(g)+epsilon).
        grad = gradient[key] * scale
        rate = CONFIG['output_lr'] if key == 'output_weights' else CONFIG['input_bias_lr']
        result[key] = np.clip(values - rate * grad / (np.abs(grad) + 1e-8), -bounds[key], bounds[key])
    return result


def diagnose(output):
    tick = time.monotonic()
    manifest, rows, _ = load_bank(DATA)
    pairs, _ = load_pairs(DATA, rows, manifest)
    require(sha(WARM / 'model.nnue') == WARM_SHA, 'Warm checkpoint changed')
    with np.load(WARM / 'float_weights.npz', allow_pickle=False) as archive:
        initial = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    old = {p: [r for r in rows[p] if r.get('kind') != 'actual_search_static_leaf']
           for p in ('train', 'tuning')}
    leaf = {p: [r for r in rows[p] if r.get('kind') == 'actual_search_static_leaf']
            for p in ('train', 'tuning')}
    anchor_rows = [json.loads(line) for line in ANCHOR.read_text(encoding='utf-8').splitlines()]
    packed = {name: pack_replayed(group) for name, group in (
        ('old_train', old['train']), ('leaf_train', leaf['train']),
        ('old_tuning', old['tuning']), ('leaf_tuning', leaf['tuning']))}
    packed['anchor'] = stm.pack_records(anchor_rows)
    train = pack_replayed(rows['train'])
    tuning = pack_replayed(rows['tuning'])
    packed_pairs = {p: pack_pairs(pairs[p]) for p in pairs}
    point_gradient = _gradient(initial, train)
    combined = ordering.qat_gradients(initial, train[0], train[1], CONFIG['input_scale'],
                                     turns=train[2], pairs=packed_pairs['train'])
    gradients = {'old_point': _gradient(initial, packed['old_train']),
                 'leaf': _gradient(initial, packed['leaf_train']),
                 'pair': {key: combined[key] - point_gradient[key] for key in stm.PARAMETER_KEYS}}
    total_point_count = len(train[1])
    old_fraction, leaf_fraction = (len(packed[name][1]) / total_point_count
                                  for name in ('old_train', 'leaf_train'))
    mixed = {key: old_fraction * gradients['old_point'][key]
                  + leaf_fraction * gradients['leaf'][key] + gradients['pair'][key]
             for key in stm.PARAMETER_KEYS}
    require(all(np.allclose(mixed[key], combined[key], atol=1e-12) for key in stm.PARAMETER_KEYS),
            'Point/leaf/pair gradient decomposition does not match')
    initial_metrics = {name: _metrics(initial, group) for name, group in packed.items()}
    first_step = {}
    for name, gradient in {**gradients, 'combined': combined}.items():
        values = _first_adam_step(initial, gradient)
        first_step[name] = {'quantized_changes': _quantized_changes(values, initial),
                            'group_drift': {group: _drift(values, initial, batch)
                                            for group, batch in packed.items()}}

    # Exact bounded replay of only the already observed first five epochs.
    # No branch, grid, seed changes, candidate file, checkpoint or gate lookup.
    observed = {}
    def progress(epoch, parameters, evaluation):
        observed['epoch'] = epoch
        observed['evaluation'] = evaluation
        observed['metrics'] = {name: _metrics(parameters, group) for name, group in packed.items()}
        observed['drift'] = {name: _drift(parameters, initial, group) for name, group in packed.items()}
        observed['quantized_changes'] = _quantized_changes(parameters, initial)
        observed['one_parameter_group_reverted_to_warm'] = {}
        for key in stm.PARAMETER_KEYS:
            reverted = {name: value.copy() for name, value in parameters.items()}
            reverted[key] = initial[key].copy()
            observed['one_parameter_group_reverted_to_warm'][key] = {
                'metrics': {name: _metrics(reverted, packed[name])
                            for name in ('old_train', 'leaf_tuning', 'anchor')},
                'interpretation': 'One-group counterfactual at fixed epoch5; effects interact and cannot be added as causal shares'}
        ia, ta, sa, ib, tb, sb = packed_pairs['train']
        before_a = stm.predict(stm.quantize(initial, 32), ia, sa)
        before_b = stm.predict(stm.quantize(initial, 32), ib, sb)
        after_a = stm.predict(stm.quantize(parameters, 32), ia, sa)
        after_b = stm.predict(stm.quantize(parameters, 32), ib, sb)
        gap_before, gap_after, gap_target = before_a - before_b, after_a - after_b, ta - tb
        observed['old_train_pair_retention'] = {
            'warm_strict_order_matches': int(np.sum(gap_before * gap_target > 0)),
            'epoch5_strict_order_matches': int(np.sum(gap_after * gap_target > 0)),
            'pairs': len(ta), 'gap_prediction_drift_mse_cp2': float(np.mean((gap_after-gap_before)**2))}
        anchor_delta = (stm.predict(stm.quantize(parameters, 32), packed['anchor'][0], packed['anchor'][2])
                        - packed['anchor'][1]) ** 2
        initial_delta = (stm.predict(stm.quantize(initial, 32), packed['anchor'][0], packed['anchor'][2])
                          - packed['anchor'][1]) ** 2
        worst = np.argsort(-(anchor_delta - initial_delta))[:10]
        observed['largest_anchor_squared_error_increases'] = [
            {'row': int(i), 'family_id': anchor_rows[i]['family_id'],
             'feature_hash': anchor_rows[i]['feature_hash'],
             'squared_error_increase_cp2': float(anchor_delta[i]-initial_delta[i])}
            for i in worst]

    config = dict(CONFIG, epochs=5)
    _, history, _ = ordering.train_candidate(
        initial, train[0], train[1], config, turns=train[2], pairs=packed_pairs['train'],
        validation_indices=tuning[0], validation_targets=tuning[1], validation_turns=tuning[2],
        validation_pairs=packed_pairs['tuning'], anchor_indices=packed['anchor'][0],
        anchor_targets=packed['anchor'][1], anchor_turns=packed['anchor'][2],
        pair_family_ids=[pair['family_id'] for pair in pairs['train']], progress=progress, **GUARDS)
    previous = read(PREVIOUS / 'selection.json')
    original_epoch5 = previous['validation_evaluations'][1]
    require(observed['evaluation'] == original_epoch5, 'Five-epoch replay differs from recorded checkpoint')
    previous_training = read(PREVIOUS / 'training.json')
    require(history == previous_training['history'][:5], 'Five-epoch replay history differs')
    sources = [Path(__file__), *[ROOT / 'scripts' / n for n in (
        'neural_stm.py', 'neural_stm_ordering.py', 'train_nnue_leaf.py',
        'train_nnue_ordering.py', 'train_nnue_loss_repair.py')]]
    inputs = [*sources, WARM / 'float_weights.npz', WARM / 'model.nnue',
              PREVIOUS / 'selection.json', PREVIOUS / 'training.json', DATA / 'manifest.json',
              *[DATA / name for name in ('train.jsonl', 'tuning.jsonl', 'test.jsonl',
                                        'pairs-train.jsonl', 'pairs-tuning.jsonl')], ANCHOR]
    result = {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'purpose': 'Diagnose already rejected leaf fit; no new candidate, no teacher/engine/gate calls',
        'bounded_replay': {'epochs': 5, 'optimizer_steps': history[-1]['optimizer_steps'],
                           'same_seed_and_config_as_previous': True,
                           'exact_previous_epoch5_evaluation_and_history': True,
                           'models_or_checkpoints_saved': 0},
        'data': {'old_train': len(old['train']), 'new_leaf_train': len(leaf['train']),
                 'old_train_weighted': len(packed['old_train'][1]),
                 'new_leaf_train_weighted': len(packed['leaf_train'][1]),
                 'total_weighted': total_point_count, 'leaf_point_fraction': leaf_fraction,
                 'old_tuning': len(old['tuning']), 'new_leaf_tuning': len(leaf['tuning']),
                 'anchor': len(anchor_rows), 'pair_train': len(pairs['train'])},
        'feature_overlap_old_train_to_leaf_train': _feature_coverage(old['train'], leaf['train']),
        'initial_metrics': initial_metrics,
        'full_training_gradient_components': _relations(gradients),
        'full_batch_first_adam_step_controls': first_step,
        'exact_previous_epoch5_replay': observed,
        'evidence_limits': [
            'Full-batch gradients are local STE diagnostics; Adam normalization means norm ratios are not causal strength shares.',
            'First-step controls are fixed local counterfactuals, not additional candidate training or a hyperparameter sweep.',
            'The five-epoch replay reconstructs a previously observed checkpoint, not an independently tested model.',
            'Shared feature coverage demonstrates parameter coupling, not a proof that fixed topology cannot generalize.',
            'Anchor/tuning only evaluate; old69 test labels are not scored and reserved16 gate files are not read.',
            'An old-training-output MSE preservation term would target function drift directly; its value remains an experimental hypothesis.'],
        'recommendation': 'Keep original Stockfish MSE and sibling-gap MSE; add warm-output MSE only on existing TRAIN inputs, independently sampled/replayed each update. Preserve anchor evaluation-only and every existing selection guard.',
        'input_sha256': {str(p): sha(p) for p in inputs},
        'elapsed_seconds': time.monotonic() - tick}
    output.parent.mkdir(parents=True, exist_ok=True)
    write(output, result)
    print(json.dumps({'output': str(output), 'data': result['data'],
                      'epoch5_drift': observed['drift'], 'elapsed_seconds': result['elapsed_seconds']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    diagnose(parser.parse_args().output.resolve())
