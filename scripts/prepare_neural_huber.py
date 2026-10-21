"""Prepare, reproduce and freeze twelve MSE/H100 NNUE candidates; no matches.

Only the existing attempt-002 train/validation corpus and six MSE reference
checkpoints are read. Existing heldout/match directories are never opened.
All outputs are new; native validation uses private executable copies.
"""
from __future__ import annotations

import os
for _variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_variable] = '1'

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

import chess
import numpy as np

import neural_precision as precision
import neural_training as base
import neural_target_ab_data as data

ROOT = Path(__file__).resolve().parents[1]
SEEDS = (20261020, 20261021, 20261022)
TARGETS = {'A': 'classical_static_white_cp', 'B': 'target_white_cp'}
LOSSES = ('mse', 'h100')


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def config(arm, loss, seed):
    require(arm in TARGETS and loss in LOSSES and seed in SEEDS, 'Unknown preregistered condition')
    return {'candidate_id': f'{arm}-{loss}-{seed}', 'input_scale': 32, 'epochs': 120,
            'input_bias_lr': .00002, 'output_lr': .005, 'seed': seed, 'batch_size': 128, 'loss': loss}


def packed(rows, arm):
    require(arm in TARGETS, 'Unknown explicit target')
    for row in rows:
        require(all(type(row.get(key)) is int and abs(row[key]) <= 28000 for key in TARGETS.values()),
                'Invalid paired target; no fallback or clipping is allowed')
    return base.pack_records([{**row, 'target_white_cp': row[TARGETS[arm]]} for row in rows])


def array_sha(array, dtype):
    return hashlib.sha256(np.asarray(array, dtype=dtype).tobytes(order='C')).hexdigest()


def order_sha(count, seed):
    generator, checksum = np.random.default_rng(seed), hashlib.sha256()
    for _ in range(120):
        checksum.update(generator.permutation(count).astype('<i8').tobytes())
    return checksum.hexdigest()


def metrics(predictions, targets):
    return {'count': len(targets), **base.error_metrics(predictions, targets),
            'mse_pawn2': float(precision.loss_terms(predictions, targets, 'mse').mean()),
            'h100': float(precision.loss_terms(predictions, targets, 'h100').mean())}


def observations(parameters, initial, loaded, packed_rows):
    model = precision.quantize(parameters, 32)
    result = {}
    for partition in ('train', 'validation'):
        indices, _ = packed_rows[partition]['A']
        prediction = precision.predict(model, indices)
        groups = defaultdict(list)
        for index, row in enumerate(loaded[partition]):
            gap = abs(row[TARGETS['A']] - row[TARGETS['B']])
            groups['all'].append(index)
            groups['kind:' + row['kind']].append(index)
            groups['target_gap:' + ('lt50' if gap < 50 else '50to299' if gap < 300 else 'ge300')].append(index)
        result[partition] = {group: {arm: metrics(prediction[selected], packed_rows[partition][arm][1][selected])
                                    for arm in TARGETS} for group, selected in groups.items()}
    result['parameter_movement'] = {
        key: {'max_abs_shadow_change': float(np.max(np.abs(parameters[key] - initial[key]))),
              'shadow_changed_count': int(np.count_nonzero(parameters[key] != initial[key])),
              'integer_changed_count': int(np.count_nonzero(model[key] != precision.quantize(initial, 32)[key]))}
        for key in precision.PARAMETER_KEYS}
    return result


def special_positions():
    result = []
    fixtures = [('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1', 'e1g1'),
                ('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1', 'e1c1'),
                ('r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1', 'e8g8'),
                ('r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1', 'e8c8'),
                ('7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1', 'e5d6'),
                ('7k/8/8/8/3pP3/8/8/6K1 b - e3 0 1', 'd4e3'), (chess.STARTING_FEN, 'e2e4')]
    for promotion in 'qrbn':
        fixtures.extend([('7k/P7/8/8/8/8/8/6K1 w - - 0 1', 'a7a8' + promotion),
                         ('1r5k/P7/8/8/8/8/8/6K1 w - - 0 1', 'a7b8' + promotion),
                         ('7k/8/8/8/8/8/p7/6K1 b - - 0 1', 'a2a1' + promotion),
                         ('7k/8/8/8/8/8/p7/1R4K1 b - - 0 1', 'a2b1' + promotion)])
    for fen, uci in fixtures:
        board = chess.Board(fen)
        require(board.is_valid() and chess.Move.from_uci(uci) in board.legal_moves, 'Invalid special fixture')
        result.append(board.fen(en_passant='fen'))
        board.push_uci(uci)
        result.append(board.fen(en_passant='fen'))
        board.pop()
        require(board.fen(en_passant='fen') == fen, 'Fixture undo differs')
    return result


def native_check(engine, tester, model_path, rows, packed_all, output):
    model = precision.load_model(model_path)
    specials = special_positions()
    fens = [row['fen'] for row in rows] + specials
    values = base.evaluate_engine(engine, fens, model_path)
    expected = list(precision.predict(model, packed_all))
    expected.extend(precision.quantized_white(chess.Board(fen), model) for fen in specials)
    for fen, white, (_, full, incremental) in zip(fens, expected, values, strict=True):
        desired = int(white) * (1 if chess.Board(fen).turn else -1)
        require(full == incremental == desired, 'Python/native full/incremental score differs')
    started = time.monotonic()
    checked = subprocess.run([str(tester), str(model_path)], text=True, capture_output=True, timeout=120)
    (output / 'native-model-tests.txt').write_text(checked.stdout + checked.stderr, encoding='utf-8')
    require(checked.returncode == 0 and '0 failures' in checked.stdout, 'Native model/undo/cancellation regression failed')
    restored = {key: model[key].astype(np.float64) / (8 if key == 'output_weights' else 32) for key in precision.PARAMETER_KEYS}
    requantized = precision.quantize(restored, 32)
    require(all(np.array_equal(model[key], requantized[key]) for key in precision.PARAMETER_KEYS), 'Requantization changed integers')
    return {'positions': len(fens), 'special_move_positions': len(specials), 'python_full_incremental_mismatches': 0,
            'requantization_exact': True, 'native_model_test_returncode': checked.returncode,
            'native_model_test_wall_seconds': time.monotonic() - started,
            'native_model_test_output': checked.stdout.splitlines()}


def source_inputs(attempt, binary, tester):
    paths = [Path(__file__), ROOT / 'scripts/neural_precision.py', ROOT / 'scripts/neural_training.py',
             ROOT / 'scripts/neural_qat.py', ROOT / 'scripts/neural_target_ab_data.py',
             ROOT / 'tests/neural_huber_tests.py', ROOT / 'docs/experiments/2026-10-21-neural-target-ab-diagnosis.md',
             ROOT / 'docs/experiments/2026-10-21-m3-huber-preparation.md',
             attempt / 'freeze.json', attempt / 'initial-floats.npz', attempt / 'training/models.json', binary, tester,
             ROOT / 'docs/checks/2026-10-21-m1-integration/build-provenance.json']
    paths.extend((ROOT / 'scripts/neural_search_data').glob('*.py'))
    paths.extend((attempt / 'trainval').iterdir())
    for seed in SEEDS:
        for arm in TARGETS:
            paths.extend(attempt / 'training' / f'{arm}-{seed}' / name
                         for name in ('metrics.json', 'float_weights.npz', 'model.nnue'))
    paths.extend((ROOT / 'src').glob('*.cpp'))
    paths.extend((ROOT / 'include/regicide').glob('*.hpp'))
    # Never traverse attempt/heldout or either original match directory.
    return {str(path.resolve()): sha(path) for path in paths if path.is_file()}


def prepare(attempt, output, binary, tester):
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=False)
    inputs = source_inputs(attempt, binary, tester)
    protocol = {'schema_version': 1, 'status': 'PREPARATION_ONLY', 'created_utc': now(),
                'conditions': [config(arm, loss, seed) for loss in LOSSES for seed in SEEDS for arm in TARGETS],
                'selection': 'No selection; all twelve last-epoch models are retained',
                'initial': {'path': str(attempt / 'initial-floats.npz'), 'sha256': sha(attempt / 'initial-floats.npz')},
                'source_attempt': str(attempt), 'input_sha256': inputs,
                'loss': {'mse': 'mean(((prediction_cp-target_cp)/100)^2)',
                         'h100': 'mean(r^2 if abs(r)<=1 else 2*abs(r)-1); r=(prediction_cp-target_cp)/100',
                         'delta_cp': 100, 'normalization': 'Twice standard Huber; inner gradient equals historical MSE'},
                'holdout_policy': 'Existing 687-row holdout and 96 matches are neither read nor used for selection',
                'confirmation_policy': 'No new confirmation bank, labels, ranking panel, or 192-game match in preparation scope',
                'future_plan': {'reference': '2026-10-21-neural-target-ab-diagnosis.md', 'namespace': 'ab-loss-v1',
                                'bank_seed': 20261031, 'bank_generated': False,
                                'limitation': 'Original full experiment calls for a new bank before training; this bounded preparation does not complete that confirmation design'},
                'environment': {'python': sys.version, 'numpy': np.__version__, 'chess': chess.__version__,
                                'platform': platform.platform(), 'blas_threads': {key: os.environ[key] for key in
                                ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS')}},
                'git_head': subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True,
                                           capture_output=True, check=True).stdout.strip(),
                'strength_promoted': False, 'default_evaluator_changed': False}
    write(output / 'protocol.json', protocol)
    snapshots = output / 'source-snapshot'
    for path, expected in inputs.items():
        source = Path(path)
        if source.suffix == '.py' and source.is_relative_to(ROOT):
            target = snapshots / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            require(sha(target) == expected, 'Source snapshot differs')
    loaded = data.load_dataset(attempt / 'trainval')
    require(len(loaded['train']) == 2209 and len(loaded['validation']) == 723 and not loaded['test'], 'Unexpected historical corpus')
    initial = precision.load_float_parameters(attempt / 'initial-floats.npz')
    require(sha(attempt / 'initial-floats.npz') == read(attempt / 'freeze.json')['initial']['sha256'], 'Original initial checkpoint changed')
    require(all(np.array_equal(initial[key], base.bootstrap_float()[key]) for key in precision.PARAMETER_KEYS), 'Initial arrays are not original bootstrap')
    batches = {name: {arm: packed(loaded[name], arm) for arm in TARGETS} for name in ('train', 'validation')}
    require(all(np.array_equal(batches[name]['A'][0], batches[name]['B'][0]) for name in batches), 'A/B inputs differ')
    rows = loaded['train'] + loaded['validation']
    packed_all = packed(rows, 'B')[0]
    references = {item['model_id']: item for item in read(attempt / 'training/models.json')['models']}
    models = []
    training_seconds = native_seconds = 0.
    try:
        with tempfile.TemporaryDirectory(prefix='regicide-m3-native-') as temporary:
            private_engine, private_tester = Path(temporary) / binary.name, Path(temporary) / tester.name
            for source, target in ((binary, private_engine), (tester, private_tester)):
                shutil.copy2(source, target)
                require(sha(target) == inputs[str(source)], 'Private executable copy differs')
            for loss in LOSSES:  # Reproduce all six MSE references before any H100 training.
                for seed in SEEDS:
                    for arm in TARGETS:
                        cfg = config(arm, loss, seed)
                        folder = output / 'training' / cfg['candidate_id']
                        folder.mkdir(parents=True, exist_ok=False)
                        checkpoints = []
                        def observe(epoch, parameters, entry):
                            checkpoints.append({'epoch': epoch, 'optimizer': entry,
                                                'metrics': observations(parameters, initial, loaded, batches)})
                        x, y = batches['train'][arm]
                        print(json.dumps({'started': cfg['candidate_id'], 'utc': now()}), flush=True)
                        tick = time.monotonic()
                        parameters, history = precision.train_candidate(initial, x, y, cfg, progress=observe)
                        elapsed = time.monotonic() - tick
                        require(elapsed <= 900, 'Model exceeded original per-model 15-minute cap')
                        training_seconds += elapsed
                        model = precision.quantize(parameters, 32)
                        precision.save_model(model, folder / 'model.nnue')
                        np.savez(folder / 'float_weights.npz', **parameters)
                        write(folder / 'history.json', history)
                        write(folder / 'checkpoints.json', checkpoints)
                        require([record['epoch'] for record in checkpoints] == list(range(10, 121, 10)), 'Checkpoint schedule differs')
                        record = {'id': cfg['candidate_id'], 'arm': arm, 'loss': loss, 'seed': seed, 'config': cfg,
                                  'feature_sha256': array_sha(x, '<i8'), 'batch_order_sha256': order_sha(len(y), seed),
                                  'initial_sha256': protocol['initial']['sha256'], 'train_rows': len(y),
                                  'validation_rows': len(loaded['validation']), 'training_wall_seconds': elapsed,
                                  'float_array_sha256': {key: array_sha(parameters[key], '<f8') for key in parameters},
                                  'model_sha256': sha(folder / 'model.nnue'), 'model_checksum': precision.model_checksum(model),
                                  'final_metrics': observations(parameters, initial, loaded, batches), 'historical_mse_exact': None}
                        if loss == 'mse':
                            prior = attempt / 'training' / f'{arm}-{seed}'
                            old_parameters = precision.load_float_parameters(prior / 'float_weights.npz')
                            old_model = precision.load_model(prior / 'model.nnue')
                            old_metrics = read(prior / 'metrics.json')
                            require(all(np.array_equal(parameters[key], old_parameters[key]) for key in parameters), 'Historical MSE float arrays differ')
                            require(all(np.array_equal(model[key], old_model[key]) for key in precision.PARAMETER_KEYS), 'Historical MSE integer arrays differ')
                            require(np.array_equal(precision.predict(model, packed_all), precision.predict(old_model, packed_all)), 'Historical MSE predictions differ')
                            require(record['model_sha256'] == references[f'{arm}-{seed}']['sha256'] == old_metrics['model_sha256']
                                    if 'model_sha256' in old_metrics else record['model_sha256'] == references[f'{arm}-{seed}']['sha256'],
                                    'Historical MSE serialized model differs')
                            require(record['feature_sha256'] == old_metrics['feature_sha256'] and
                                    record['batch_order_sha256'] == old_metrics['batch_order_sha256'], 'Historical MSE inputs/order differ')
                            record['historical_mse_exact'] = {'float_arrays': True, 'integer_arrays': True,
                                                              'train_validation_integer_predictions': True, 'model_bytes': True}
                        tick = time.monotonic()
                        record['native_validation'] = native_check(private_engine, private_tester, folder / 'model.nnue', rows, packed_all, folder)
                        native_seconds += time.monotonic() - tick
                        write(folder / 'metrics.json', record)
                        models.append(record)
                        print(json.dumps({'completed': cfg['candidate_id'], 'train_seconds': round(elapsed, 3),
                                          'native_positions': record['native_validation']['positions']}), flush=True)
        require(inputs == {path: sha(path) for path in inputs}, 'Original input/source/binary changed during preparation')
        require(len(models) == 12 and sum(record['loss'] == 'mse' for record in models) == 6, 'Missing conditions')
        for seed in SEEDS:
            paired = [record for record in models if record['seed'] == seed]
            require(len({record['feature_sha256'] for record in paired}) == len({record['batch_order_sha256'] for record in paired}) == 1,
                    'Paired conditions changed inputs or minibatch order')
        artifacts = {path.relative_to(output).as_posix(): sha(path) for path in output.rglob('*') if path.is_file()}
        freeze = {'schema_version': 1, 'status': 'FROZEN_PREPARATION_ONLY', 'completed_utc': now(),
                  'model_count': len(models), 'models': models, 'artifact_sha256': artifacts,
                  'protocol_sha256': sha(output / 'protocol.json'), 'inputs_unchanged': True,
                  'historical_mse_models_reproduced_exactly': 6,
                  'training_wall_seconds_sum': training_seconds, 'native_validation_wall_seconds_sum': native_seconds,
                  'total_wall_seconds': time.monotonic() - started, 'confirmation_data_generated': False,
                  'matches_run': 0, 'selected_model': None, 'strength_promoted': False,
                  'default_evaluator_changed': False, 'private_native_processes_closed': True}
        write(output / 'freeze.json', freeze)
        return freeze
    except BaseException as error:
        write(output / 'failure.json', {'status': 'FAILED_PREPARATION', 'utc': now(), 'error': f'{type(error).__name__}: {error}',
                                       'completed_models': [record['id'] for record in models], 'strength_promoted': False})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-attempt', type=Path, default=ROOT / 'docs/checks/2026-10-20-neural-target-ab/attempt-002')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'build/regicide.exe')
    parser.add_argument('--model-tests', type=Path, default=ROOT / 'build/regicide_neural_model_tests.exe')
    args = parser.parse_args()
    result = prepare(args.source_attempt.resolve(), args.output.resolve(), args.binary.resolve(), args.model_tests.resolve())
    print(json.dumps({key: result[key] for key in ('status', 'model_count', 'historical_mse_models_reproduced_exactly',
                                                  'matches_run', 'strength_promoted', 'total_wall_seconds')}, indent=2))


if __name__ == '__main__':
    main()
