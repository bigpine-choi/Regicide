"""One guarded MSE repair using bounded replay weights for actual failed positions.

Architecture, Adam settings and stopping stay unchanged. Integer replay weights
repeat arrays only for training and tuning; immutable files contain unique rows.
Test predictions occur once after checkpoint saving and never select a model.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from collections import Counter
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
import neural_stm_guarded as guarded
import neural_training as base
from prepare_neural_huber import read, require, sha, special_positions, write
from train_neural_stm import ROOT
from train_nnue_diverse import load_bank as load_unique_bank, diversity_summary

WARM = ROOT / 'docs/checks/2026-10-22-nnue-diversity/attempt-001'
WARM_SHA = 'c73b510cd83ca221829eb9e1bf781b86d28c5b9c47ed922d8f9fa30e08385c86'
CONFIG = {'candidate_id': 'sf18-mse-stm-loss-repair-20261020', 'input_scale': 32,
          'epochs': 500, 'input_bias_lr': .0002, 'output_lr': .005,
          'seed': 20261020, 'batch_size': 128, 'loss': 'mse'}
GUARDS = {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}
PARTITIONS = ('train', 'tuning', 'test')


def replay_weights(rows: list[dict]) -> np.ndarray:
    values = [row.get('replay_weight', 1) for row in rows]
    require(all(type(value) is int and 1 <= value <= 4 for value in values),
            'Replay weight must be an integer in [1, 4]')
    return np.array(values, dtype=np.int64)


def load_bank(dataset: Path) -> tuple[dict, dict[str, list[dict]], dict[str, str]]:
    manifest, rows, hashes = load_unique_bank(dataset)
    for partition in PARTITIONS:
        weights = replay_weights(rows[partition])
        if partition == 'test':
            require(np.all(weights == 1), 'Test must stay unweighted')
    return manifest, rows, hashes


def pack_replayed(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Equivalent to bounded weighted MSE, without duplicating frozen rows."""
    indices, targets, turns = stm.pack_records(rows)
    weights = replay_weights(rows)
    return tuple(np.repeat(value, weights, axis=0) for value in (indices, targets, turns))


def score_partition(rows: list[dict], warm: dict, model: dict, *, weighted: bool = True) -> dict:
    indices, targets, turns = stm.pack_records(rows)
    predictions = {name: stm.predict(net, indices, turns)
                   for name, net in (('warm', warm), ('repair', model))}
    weights = replay_weights(rows) if weighted else np.ones(len(rows), dtype=np.int64)
    metrics = {'count': len(rows), **{name: base.error_metrics(values, targets)
                                    for name, values in predictions.items()}}
    metrics['weighted'] = {'count': int(weights.sum()), **{
        name: base.error_metrics(np.repeat(values, weights), np.repeat(targets, weights))
        for name, values in predictions.items()}}
    groups = {'white_to_move': turns == 0, 'black_to_move': turns == 1,
              'repair_replay': replay_weights(rows) > 1,
              'background': replay_weights(rows) == 1}
    piece_counts = np.array([len(chess.Board(row['fen']).piece_map()) for row in rows])
    groups.update({'25plus_pieces': piece_counts >= 25,
                   '13to24_pieces': (piece_counts >= 13) & (piece_counts <= 24),
                   '2to12_pieces': piece_counts <= 12})
    metrics['by_group'] = {name: {'count': int(mask.sum()), **{
        model_name: base.error_metrics(values[mask], targets[mask])
        for model_name, values in predictions.items()}}
        for name, mask in groups.items() if np.any(mask)}
    return metrics


def train(dataset: Path, output: Path):
    started = time.monotonic()
    manifest, rows, data_hashes = load_bank(dataset)
    require(sha(WARM / 'model.nnue') == WARM_SHA, 'Current guarded checkpoint changed')
    previous = read(WARM / 'training.json')
    require({k: v for k, v in previous['config'].items() if k != 'candidate_id'} ==
            {k: v for k, v in CONFIG.items() if k != 'candidate_id'}, 'Optimizer settings changed')
    require(previous['guards'] == GUARDS, 'Overfit guard settings changed')
    with np.load(WARM / 'float_weights.npz', allow_pickle=False) as archive:
        initial = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    warm = stm.load_model(WARM / 'model.nnue')
    require(stm.model_checksum(stm.quantize(initial, CONFIG['input_scale'])) == stm.model_checksum(warm),
            'Warm float weights and actual guarded integer checkpoint differ')
    engine = ROOT / 'build-m3-stm/regicide.exe'
    tester = engine.parent / ('regicide_neural_model_tests' + engine.suffix)
    cached_path = WARM / 'validation.json'
    cached = read(cached_path)
    require(sha(engine) == cached['input_sha256'][str(engine)], 'Unchanged native executable differs')
    sources = [Path(__file__), ROOT / 'scripts/neural_stm_guarded.py', ROOT / 'scripts/neural_stm.py',
               ROOT / 'scripts/neural_precision.py', ROOT / 'scripts/neural_training.py', ROOT / 'scripts/neural_qat.py',
               ROOT / 'scripts/train_nnue_diverse.py', ROOT / 'scripts/prepare_neural_huber.py',
               ROOT / 'scripts/train_neural_stm.py']
    inputs = sources + [dataset / 'manifest.json', *[dataset / (p + '.jsonl') for p in PARTITIONS],
                        WARM / 'float_weights.npz', WARM / 'model.nnue', WARM / 'training.json',
                        cached_path, engine, tester, ROOT / 'config/engine-profiles/classical.json']
    protocol = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'config': CONFIG, 'guards': GUARDS,
        'architecture': {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                         'hidden': 16, 'changed': False, 'added_parameters': 0},
        'change': 'Repair actual failed positions using Stockfish sibling positions and bounded replay; one fine-tune',
        'replay_policy': {'maximum_integer_weight': 4, 'background_weight': 1,
                          'repair_weight': 4, 'training': 'Repeat arrays by row replay_weight',
                          'selection': 'Minimum tuning weighted integer MSE; epoch zero protected',
                          'test': 'Unique unweighted rows, evaluated after checkpoint saving',
                          'weighted_rows': {p: int(replay_weights(rows[p]).sum()) for p in PARTITIONS}},
        'initial': {'path': str(WARM / 'float_weights.npz'), 'sha256': sha(WARM / 'float_weights.npz'),
                    'model_sha256': WARM_SHA, 'selected_epoch': previous['selection']['selected_epoch']},
        'data': {'directory': str(dataset), 'manifest_sha256': sha(dataset / 'manifest.json'),
                 'file_sha256': data_hashes, 'rows': {p: len(rows[p]) for p in PARTITIONS},
                 'coverage': {p: diversity_summary(rows[p]) for p in PARTITIONS},
                 'partition_policy': 'Disjoint source families and mirrored piece inputs; train only gradients; tuning only checkpoint selection',
                 'fine_tune_test_policy': 'Previously available source; held out from this fine-tune. Read for leakage validation, score warm/candidate once after saved selection; never optimizer, stopping, ranking, or retraining',
                 'test_limit': manifest.get('test_limit', 'Previously available sources; independence applies to this fine-tune only')},
        'selection': 'One seed, one run; minimum tuning weighted integer MSE, epoch0 protected, earliest tie',
        'comparison': 'Basic classical Regicide, same8starts bothcolors100ms, exactly16games; no match-based selection',
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
    tick = time.monotonic()

    def progress(epoch, _parameters, evaluation):
        print(json.dumps({'epoch': epoch, 'tuning_rmse_cp': evaluation['rmse_cp'],
                          'elapsed_seconds': time.monotonic() - tick}), flush=True)

    parameters, history, selection = guarded.train_candidate(
        initial, packed['train'][0], packed['train'][1], CONFIG, turns=packed['train'][2],
        validation_indices=packed['tuning'][0], validation_targets=packed['tuning'][1],
        validation_turns=packed['tuning'][2], progress=progress, **GUARDS)
    learning_seconds = time.monotonic() - tick
    np.savez(output / 'float_weights.npz', **parameters)
    stm.save_integer_model(output / 'model.nnue', parameters, CONFIG['input_scale'])
    model = stm.load_model(output / 'model.nnue')
    write(output / 'selection.json', selection)
    # The test labels reach the metric routine only after final checkpoint publication.
    metrics = {p: score_partition(rows[p], warm, model, weighted=(p != 'test')) for p in PARTITIONS}
    require(metrics['tuning']['weighted']['repair']['mse_cp2'] <= metrics['tuning']['weighted']['warm']['mse_cp2'],
            'Best restore violated epoch-zero protection')
    report = {'config': CONFIG, 'guards': GUARDS, 'candidate_runs': 1,
              'training_wall_seconds': learning_seconds, 'selection': selection, 'metrics': metrics,
              'model_sha256': sha(output / 'model.nnue'),
              'model_changed_from_warm': stm.model_checksum(model) != stm.model_checksum(warm),
              'architecture_unchanged': True, 'loss': 'mse', 'history': history,
              'replay_policy': protocol['replay_policy'],
              'test_evaluations': 1, 'test_used_in_selection': False,
              'test_scored_after_saved_selection': True,
              'interpretation': 'Weighted tuning MSE selects this checkpoint. Unique unweighted and replay-weighted metrics are separate. Actual failed match positions and known openings are development data; neither lower MSE nor a short reused-start match proves general strength.'}
    write(output / 'training.json', report)
    all_rows = [row for p in PARTITIONS for row in rows[p]]
    specials = special_positions()
    fens = [row['fen'] for row in all_rows] + specials
    indices, _, turns = stm.pack_records(all_rows + [{'fen': fen, 'target_white_cp': 0} for fen in specials])
    expected = stm.predict(model, indices, turns) * np.where(turns == 0, 1, -1)
    values = base.evaluate_engine(engine, fens, output / 'model.nnue')
    require(np.array_equal(expected, [value[1] for value in values]), 'Python/native full/incremental mismatch')
    checked = subprocess.run([str(tester), str(output / 'model.nnue')], capture_output=True, text=True, timeout=120)
    (output / 'native-model-tests.txt').write_text(checked.stdout + checked.stderr, encoding='utf-8')
    require(checked.returncode == 0 and '0 failures' in checked.stdout, 'Actual candidate model regressions failed')
    # Confirm the authenticated bank and warm/native inputs have not changed during the run.
    require(all(sha(Path(path)) == value for path, value in protocol['input_sha256'].items()),
            'An authenticated input changed during training or verification')
    validation = {'positions': len(fens), 'corpus_positions': len(all_rows), 'special_positions': len(specials),
                  'python_native_full_incremental_mismatches': 0, 'native_model_returncode': checked.returncode,
                  'native_model_summary': checked.stdout.splitlines()[-1],
                  'reused_legacy_validation': {'path': str(cached_path), 'sha256': sha(cached_path)},
                  'input_sha256': {str(path): sha(path) for path in (engine, tester, output / 'model.nnue')},
                  'elapsed_wall_seconds': time.monotonic() - started}
    write(output / 'validation.json', validation)
    print(json.dumps({'training_seconds': learning_seconds, 'selection': selection,
                      'metrics': metrics, 'model_sha256': report['model_sha256']}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('output', type=Path)
    arguments = parser.parse_args()
    train(arguments.dataset.resolve(), arguments.output.resolve())
