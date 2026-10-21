"""One guarded output-only MSE fit with default-profile repair comparisons.

Pair child target_white_cp is authoritative and normalized teacher metadata must
agree. Frozen teacher receipts authenticate those labels separately. No post-fit
move-choice gate influences model selection. Piece inputs, hidden biases and
turn inputs retain their initial float bytes and deployed integer values.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import chess
import numpy as np

import neural_stm as stm
import neural_stm_output_only as ordering
import neural_training as base
from prepare_neural_huber import read, require, sha, special_positions, write
from train_neural_stm import ROOT
from train_nnue_diverse import diversity_summary
from train_nnue_loss_repair import load_bank, pack_replayed, replay_weights
from train_nnue_ordering import pack_pairs, score_partition, score_pairs
from nnue_live_pairs import load_pairs

WARM = ROOT / 'docs/checks/2026-10-22-nnue-output-only/attempt-001'
WARM_SHA = '4e100302474cc3861ee5ef2ec69a641dad0e08cc94c1b685f80c3bb2e8883700'
ANCHOR = ROOT / 'docs/checks/2026-10-22-nnue-diversity/data-001/tuning.jsonl'
CONFIG = {'candidate_id': 'sf18-mse-stm-head-repair-20261020', 'input_scale': 32,
          'epochs': 500, 'input_bias_lr': .0002, 'output_lr': .005,
          'seed': 20261020, 'batch_size': 128, 'loss': 'mse'}
GUARDS = {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}
PARTITIONS = ('train', 'tuning', 'test')
PRESERVATION_WEIGHT = 4.0
PRESERVATION_BANK = ROOT / 'docs/checks/2026-10-22-nnue-generalization/data-001'
FROZEN_KEYS = ('input_weights', 'hidden_bias', 'turn_weights')


def array_receipt(values):
    array = np.asarray(values)
    return {'shape': list(array.shape), 'dtype': array.dtype.str,
            'sha256_c_contiguous_bytes': hashlib.sha256(array.tobytes(order='C')).hexdigest()}


def freeze_metrics(initial, parameters, warm, model):
    """Reject changes to any frozen shadow bytes or deployed integer values."""
    require(set(parameters) == set(initial) == set(stm.PARAMETER_KEYS), 'Parameter keys changed')
    require(initial['output_weights'].shape == parameters['output_weights'].shape == (16,),
            'Exactly sixteen existing output weights may be trained')
    frozen = {}
    for key in FROZEN_KEYS:
        before, after = array_receipt(initial[key]), array_receipt(parameters[key])
        integer_before, integer_after = array_receipt(warm[key]), array_receipt(model[key])
        require(before == after, f'Frozen float bytes changed: {key}')
        require(integer_before == integer_after, f'Frozen integer values changed: {key}')
        frozen[key] = {'float_initial': before, 'float_selected': after,
                       'integer_initial': integer_before, 'integer_selected': integer_after,
                       'float_bytes_identical': True, 'quantized_values_identical': True}
    return {'trainable_keys': ['output_weights'], 'trainable_parameters': 16,
            'frozen_keys': list(FROZEN_KEYS),
            'frozen_parameters': sum(int(initial[key].size) for key in FROZEN_KEYS),
            'all_frozen_float_bytes_identical': True,
            'all_frozen_quantized_values_identical': True, 'parameters': frozen,
            'output_weights': {'float_initial': array_receipt(initial['output_weights']),
                               'float_selected': array_receipt(parameters['output_weights']),
                               'integer_initial': array_receipt(warm['output_weights']),
                               'integer_selected': array_receipt(model['output_weights']),
                               'changed_float_values': int(np.count_nonzero(
                                   parameters['output_weights'] != initial['output_weights'])),
                               'changed_quantized_values': int(np.count_nonzero(
                                   model['output_weights'] != warm['output_weights']))}}


def train(dataset, output, gate_plan):
    started = time.monotonic()
    fresh_gate = read(gate_plan)
    require(isinstance(fresh_gate, dict), 'Fresh gate plan must be a JSON object')
    require(fresh_gate.get('warm_sha256') == WARM_SHA, 'Fresh gate must bind the current output-only warm')
    manifest, rows, data_hashes = load_bank(dataset)
    require(manifest.get('live_warm_model', {}).get('sha256') == WARM_SHA,
            'Live collection must bind this output-only warm')
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
            and parity['old_binary_sha256'] == sha(old_engine)
            and cached['input_sha256'][str(engine)] == sha(engine)
            and parity['new_binary_sha256'] == sha(engine) == fresh_gate['binary_sha256'],
            'Observer build must preserve the authenticated prior search behavior')
    sources = [Path(__file__), *[ROOT / 'scripts' / name for name in (
        'neural_stm_output_only.py', 'nnue_live_pairs.py', 'prepare_nnue_head_repair.py', 'finalize_nnue_head_repair_cached.py', 'collect_live_choice.cpp', 'live_choice_support.hpp', 'choice_leaf_support.hpp', 'build_live_choice.ps1', 'nnue_choice_pairs.py', 'prepare_nnue_choice.py', 'neural_stm_preservation_step.py', 'neural_stm_preservation.py', 'train_nnue_leaf.py', 'prepare_nnue_leaf.py', 'prepare_nnue_generalization.py', 'train_nnue_ordering.py', 'neural_stm_ordering.py', 'neural_stm.py', 'neural_precision.py', 'neural_training.py',
        'neural_qat.py', 'train_nnue_diverse.py', 'train_nnue_loss_repair.py',
        'neural_stm_guarded.py', 'prepare_neural_huber.py', 'train_neural_stm.py')]]
    inputs = sources + [gate_plan, blocked_path, roots_path, dataset / 'manifest.json',
                       dataset / 'protocol.json', dataset / 'assembly-receipt.json',
                       *[dataset / name for name in (*data_hashes, *pair_hashes)],
                       preservation_path, PRESERVATION_BANK / 'manifest.json',
                       anchor_path, anchor_path.parent / 'manifest.json', WARM / 'float_weights.npz',
                       WARM / 'model.nnue', WARM / 'training.json', cached_path, old_engine, parity_path, engine, tester,
                       ROOT / 'config/engine-profiles/classical.json']
    protocol = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'config': CONFIG, 'guards': GUARDS,
        'architecture': {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                         'hidden': 16, 'changed': False, 'added_parameters': 0},
        'training_mask': {'trainable_keys': ['output_weights'], 'trainable_parameters': 16,
                          'frozen_keys': list(FROZEN_KEYS),
                          'policy': 'Freeze initial float bytes and quantized values of piece inputs, hidden biases and turn inputs. Only existing sixteen output weights enter Adam.'},
        'loss_definition': 'point teacher MSE + 1.0 * mean(old sibling gaps and teacher root-child gaps through verified support leaves) + 4.0 * old-TRAIN frozen-warm-prediction MSE; all normalized by10000 for gradients',
        'pair_weight': 1.0, 'change': 'Fit only existing output weights with actual-default-profile matched-horizon comparison witnesses and exposed tactical counterexamples; preserve old data, MSE and all guards',
        'initial': {'path': str(WARM / 'float_weights.npz'), 'sha256': sha(WARM / 'float_weights.npz'),
                    'model_sha256': WARM_SHA, 'selected_epoch': previous['selection']['selected_epoch']},
        'data': {'directory': str(dataset), 'manifest_sha256': sha(dataset / 'manifest.json'),
                 'file_sha256': {**data_hashes, **pair_hashes}, 'rows': {p: len(rows[p]) for p in PARTITIONS},
                 'pair_rows': {p: len(pairs[p]) for p in pairs},
                 'coverage': {p: diversity_summary(rows[p]) for p in PARTITIONS},
                 'point_replay_weighted_rows': {p: int(replay_weights(rows[p]).sum()) for p in PARTITIONS},
                 'pair_target_policy': 'Old pairs keep their original provenance. New live comparison gaps are teacher root-child differences through observed completed-PV support endpoints, NOT literal teacher labels at those endpoints. The forced branch matches completed depth and default options but is a frozen-search surrogate.',
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
        'comparison': 'Only after the newly reserved-position move-choice gate passes: basic classical Regicide,16 games,100ms',
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
        freeze_metrics(initial, _parameters, warm, stm.quantize(_parameters, CONFIG['input_scale']))
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
    quantized = stm.quantize(parameters, CONFIG['input_scale'])
    frozen_metrics = freeze_metrics(initial, parameters, warm, quantized)
    np.savez(output / 'float_weights.npz', **parameters)
    stm.save_integer_model(output / 'model.nnue', parameters, CONFIG['input_scale'])
    model = stm.load_model(output / 'model.nnue')
    require(stm.model_checksum(model) == stm.model_checksum(quantized), 'Saved output-only checkpoint changed')
    with np.load(output / 'float_weights.npz', allow_pickle=False) as archive:
        saved_parameters = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    require(all(array_receipt(saved_parameters[key]) == array_receipt(parameters[key])
                for key in stm.PARAMETER_KEYS), 'Saved float checkpoint changed')
    freeze_metrics(initial, saved_parameters, warm, model)
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
    live_metrics = {p: score_pairs([pair for pair in pairs[p] if pair.get('kind') == 'live_search_backed_gap'], warm, model) for p in pairs}
    backed_metrics = {p: score_pairs([pair for pair in pairs[p] if pair.get('kind') == 'search_backed_gap'], warm, model) for p in pairs}
    report = {'live_pair_metrics': live_metrics, 'frozen_parameter_metrics': frozen_metrics, 'backed_pair_metrics': backed_metrics, 'preservation_metrics': preservation_metrics, 'config': CONFIG, 'guards': GUARDS, 'candidate_runs': 1, 'training_wall_seconds': learning_seconds,
              'selection': selection, 'metrics': metrics, 'pair_metrics': pair_metrics, 'anchor_metrics': anchor_metrics,
              'leaf_metrics': {p: score_partition([r for r in rows[p] if r.get('kind') == 'actual_search_static_leaf'], warm, model) for p in ('train', 'tuning')},
              'model_sha256': sha(output / 'model.nnue'), 'model_changed_from_warm': stm.model_checksum(model) != stm.model_checksum(warm),
              'architecture_unchanged': True, 'loss': 'mse', 'loss_definition': protocol['loss_definition'],
              'history': history, 'test_evaluations': 1, 'test_used_in_selection': False,
              'test_scored_after_saved_selection': True, 'move_choice_gate_used_in_selection': False,
              'interpretation': 'Only output weights may change; input/hidden representation and NNUE architecture stay fixed. Old fixed-depth and new default-profile comparison receipts retain separate model provenance. New forced branches match the completed default horizon; observed endpoint agreement is a frozen-search surrogate, not a full causal trace. Only fresh move-choice testing establishes transfer.'}
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
        'frozen_parameter_metrics': frozen_metrics,
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
