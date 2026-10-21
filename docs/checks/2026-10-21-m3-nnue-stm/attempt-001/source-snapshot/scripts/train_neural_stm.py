"""Train one MSE NNUE with relative side-to-move input; no matches/selection.

Reuse the frozen B-MSE corpus, common initialization and optimizer schedule.
The only new parameters are sixteen zero-initialized side-to-move weights.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

import chess
import numpy as np

import neural_precision as precision
import neural_stm as stm
import neural_target_ab_data as data
import neural_training as base
from prepare_neural_huber import array_sha, config, order_sha, packed, read, require, sha, write

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/checks/2026-10-20-neural-target-ab/attempt-002'
REFERENCE = ROOT / 'docs/checks/2026-10-21-m3-huber-preparation/attempt-001/training/B-mse-20261020'
REFERENCE_SHA = '267f65646c24292efa28f11a5f5990311ed7da4f6c134d7c4a6b6c1e0f7be1eb'
INITIAL_SHA = '5ce9ca28e3436b125db9b307944d28c1fe7e58bbd7415048ec986aa570d4bd05'


def train(output: Path):
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=False)
    settings = dict(config('B', 'mse', 20261020), candidate_id='B-mse-stm-20261020')
    reference_metrics = read(REFERENCE / 'metrics.json')
    require({k: v for k, v in settings.items() if k != 'candidate_id'} ==
            {k: v for k, v in reference_metrics['config'].items() if k != 'candidate_id'},
            'Common training settings differ from the existing MSE reference')
    require(sha(REFERENCE / 'model.nnue') == REFERENCE_SHA, 'MSE reference changed')
    require(sha(SOURCE / 'initial-floats.npz') == INITIAL_SHA, 'Common initialization changed')
    historical_inputs = read(REFERENCE.parents[1] / 'protocol.json')['input_sha256']
    matched_data = {}
    for name in ('manifest.json', 'train.jsonl', 'validation.jsonl'):
        expected = [value for path, value in historical_inputs.items()
                    if Path(path).name == name and Path(path).parent.name == 'trainval']
        require(len(expected) == 1 and sha(SOURCE / 'trainval' / name) == expected[0],
                'Frozen reference data changed: ' + name)
        matched_data[name] = expected[0]
    loaded = data.load_dataset(SOURCE / 'trainval')
    require((len(loaded['train']), len(loaded['validation']), len(loaded['test'])) == (2209, 723, 0),
            'Unexpected frozen corpus partition sizes')
    batches = {partition: packed(loaded[partition], 'B') for partition in ('train', 'validation')}
    turns = {partition: np.array([0 if row['fen'].split()[1] == 'w' else 1 for row in loaded[partition]],
                                dtype=np.int64) for partition in batches}
    feature_hash = array_sha(batches['train'][0], '<i8')
    require(feature_hash == reference_metrics['feature_sha256'], 'Common piece inputs differ')
    order_hash = order_sha(len(loaded['train']), settings['seed'])
    require(order_hash == reference_metrics['batch_order_sha256'], 'Common batch order differs')
    common_initial = precision.load_float_parameters(SOURCE / 'initial-floats.npz')
    initial = stm.with_turn_parameters(common_initial)
    require(all(np.array_equal(initial[key], common_initial[key]) for key in precision.PARAMETER_KEYS)
            and not np.any(initial['turn_weights']), 'Initial parameters are not matched plus zero turn input')
    for partition, (indices, _) in batches.items():
        require(np.array_equal(precision.predict(precision.quantize(common_initial, 32), indices),
                               stm.predict_integer(initial, indices, 32, turns=turns[partition])),
                'Zero turn input changed common initial predictions')
    sources = [Path(__file__), ROOT / 'scripts/neural_stm.py', ROOT / 'scripts/neural_precision.py',
               ROOT / 'scripts/neural_training.py', ROOT / 'scripts/neural_qat.py',
               ROOT / 'scripts/neural_target_ab_data.py', ROOT / 'scripts/prepare_neural_huber.py']
    inputs = sources + [SOURCE / 'initial-floats.npz', SOURCE / 'freeze.json',
                        REFERENCE / 'model.nnue', REFERENCE / 'metrics.json']
    inputs += [SOURCE / 'trainval' / name for name in ('manifest.json', 'train.jsonl', 'validation.jsonl', 'test.jsonl')]
    protocol = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'config': settings,
        'hypothesis': 'Relative side-to-move hidden input can represent initiative and turn-dependent evaluation.',
        'architecture': {'legacy': '768 piece inputs, shared 16 hidden units, two perspectives',
                         'candidate': 'same plus one binary input per perspective: viewer has move',
                         'added_trainable_parameters': 16, 'turn_weight_initialization': 'all zero'},
        'selection': 'One candidate, fixed final epoch 120; no validation or match selection; no seed sweep.',
        'baseline': {'id': 'B-mse-20261020', 'path': str(REFERENCE / 'model.nnue'), 'sha256': REFERENCE_SHA,
                     'retrained': False},
        'data': {'source': str(SOURCE / 'trainval'), 'target': 'target_white_cp (B, existing self-search labels)',
                 'rows': {p: len(rows) for p, rows in loaded.items()}, 'feature_sha256': feature_hash,
                 'turn_sha256': array_sha(np.concatenate(list(turns.values())), '<i8'),
                 'batch_order_sha256': order_hash, 'initial_sha256': INITIAL_SHA,
                 'historical_data_sha256_exact': matched_data,
                 'holdout_policy': 'Existing heldout labels and prior match positions are not read or used.'},
        'optimizer': {'name': 'Adam', 'beta1': .9, 'beta2': .999, 'epsilon': 1e-8,
                      'gradient_norm_limit': 10, 'turn_lr': settings['input_bias_lr']},
        'environment': {'python': sys.version, 'numpy': np.__version__, 'chess': chess.__version__,
                        'platform': platform.platform(), 'blas_threads': 1},
        'starting_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'input_sha256': {str(path): sha(path) for path in inputs}, 'strength_promoted': False,
    }
    write(output / 'protocol.json', protocol)
    for source in sources:
        target = output / 'source-snapshot' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    np.savez(output / 'initial-floats.npz', **initial)
    learning_start = time.monotonic()
    parameters, history = stm.train_candidate(initial, *batches['train'], settings, turns=turns['train'])
    learning_seconds = time.monotonic() - learning_start
    np.savez(output / 'float_weights.npz', **parameters)
    stm.save_integer_model(output / 'model.nnue', parameters, 32)
    model = stm.load_integer_model(output / 'model.nnue')
    baseline = precision.load_model(REFERENCE / 'model.nnue')
    metrics = {}
    for partition, (indices, targets) in batches.items():
        candidate = stm.predict(model, indices, turns[partition])
        control = precision.predict(baseline, indices)
        metrics[partition] = {'count': len(targets), 'baseline': base.error_metrics(control, targets),
                              'candidate': base.error_metrics(candidate, targets),
                              'changed_predictions': int(np.count_nonzero(candidate != control))}
    integer_turn = np.asarray(model['turn_weights'])
    require(np.any(integer_turn), 'Learned turn feature remained entirely zero after quantization')
    report = {'config': settings, 'training_wall_seconds': learning_seconds,
              'total_wall_seconds': time.monotonic() - started, 'metrics': metrics,
              'turn_weights_integer': integer_turn.tolist(),
              'turn_weights_shadow': parameters['turn_weights'].tolist(),
              'parameter_changes': {key: {'shadow_changed_count': int(np.count_nonzero(parameters[key] != initial[key])),
                                           'max_abs_shadow_change': float(np.max(np.abs(parameters[key] - initial[key])))}
                                    for key in parameters},
              'common_initial_predictions_exact': True, 'baseline_retrained': False,
              'model_sha256': sha(output / 'model.nnue'), 'history': history,
              'interpretation': 'Prediction errors describe existing search-label fit, not Elo or best-move quality.'}
    write(output / 'training.json', report)
    print(json.dumps({key: report[key] for key in ('training_wall_seconds', 'metrics', 'turn_weights_integer', 'model_sha256')}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    train(parser.parse_args().output.resolve())
