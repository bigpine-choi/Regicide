"""One bounded MSE fine-tune on a larger, diverse frozen Stockfish bank.

Keep the current STM architecture, optimizer and validation-stop guard.  Source
families and even mirrored piece-only inputs must stay in one partition.  The
fine-tune-held-out test is scored only after the tuning-selected checkpoint has
been saved. Its sources were previously available, so it is not an entirely
unseen development test. Neither test results nor games select a checkpoint.
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

WARM = ROOT / 'docs/checks/2026-10-22-nnue-overfit/attempt-001'
WARM_SHA = '1b431332c5983d3a12e3fc3bdef9d85a55946e01806323d97b30c1cc6666e058'
CONFIG = {'candidate_id': 'sf18-mse-stm-diverse-20261020', 'input_scale': 32,
          'epochs': 500, 'input_bias_lr': .0002, 'output_lr': .005,
          'seed': 20261020, 'batch_size': 128, 'loss': 'mse'}
GUARDS = {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}
PARTITIONS = ('train', 'tuning', 'test')


def load_bank(dataset: Path) -> tuple[dict, dict[str, list[dict]], dict[str, str]]:
    """Authenticate files and reject family/feature leakage before optimization."""
    manifest = read(dataset / 'manifest.json')
    require(isinstance(manifest.get('files'), dict), 'Manifest requires file SHA256s')
    require(isinstance(manifest.get('rows'), dict), 'Manifest requires partition counts')
    rows, identities, families, hashes = {}, {}, {}, {}
    for partition in PARTITIONS:
        name = partition + '.jsonl'
        path = dataset / name
        expected = manifest['files'].get(name)
        if isinstance(expected, dict):
            expected = expected.get('sha256')
        require(isinstance(expected, str) and len(expected) == 64,
                'Missing partition SHA256: ' + name)
        hashes[name] = sha(path)
        require(hashes[name] == expected, 'Dataset bytes differ from manifest: ' + name)
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        count = manifest['rows'].get(partition)
        require(type(count) is int and count == len(records) and count > 0,
                'Unexpected partition count: ' + partition)
        seen, groups = set(), set()
        for row in records:
            require(isinstance(row, dict) and isinstance(row.get('fen'), str), 'Invalid position record')
            board = chess.Board(row['fen'])
            require(board.is_valid() and board.outcome(claim_draw=True) is None,
                    'Dataset position must be legal and nonterminal')
            target = row.get('target_white_cp')
            require(type(target) is int and abs(target) <= 1500,
                    'Require exact bounded Stockfish white CP, without clipping')
            require(row.get('teacher_white_cp', target) == target, 'Teacher target differs')
            family = row.get('family_id')
            require(isinstance(family, str) and bool(family), 'A source family is required')
            identity = base.feature_identity(board)
            require(row.get('feature_hash') == identity, 'Position feature identity differs')
            require(identity not in seen, 'Duplicate or mirrored piece inputs within partition')
            seen.add(identity)
            groups.add(family)
        rows[partition], identities[partition], families[partition] = records, seen, groups
    for index, left in enumerate(PARTITIONS):
        for right in PARTITIONS[index + 1:]:
            require(not (identities[left] & identities[right]), 'Piece inputs cross partitions')
            require(not (families[left] & families[right]), 'Source families cross partitions')
    return manifest, rows, hashes


def diversity_summary(rows: list[dict]) -> dict:
    """Describe coverage without using labels for selection."""
    counts, sides, buckets, sources = Counter(), Counter(), Counter(), Counter()
    for row in rows:
        board = chess.Board(row['fen'])
        pieces = len(board.piece_map())
        counts[str(pieces)] += 1
        sides['white' if board.turn else 'black'] += 1
        buckets['25plus_pieces' if pieces >= 25 else '13to24_pieces' if pieces >= 13 else '2to12_pieces'] += 1
        sources[str(row.get('source_kind', row.get('source', 'unspecified')))] += 1
    return {'rows': len(rows), 'families': len({r['family_id'] for r in rows}),
            'side_to_move': dict(sides), 'piece_count': dict(counts),
            'piece_count_buckets': dict(buckets), 'source_kinds': dict(sources)}


def score_partition(rows: list[dict], warm: dict, model: dict) -> dict:
    indices, targets, turns = stm.pack_records(rows)
    predictions = {name: stm.predict(net, indices, turns)
                   for name, net in (('warm', warm), ('diverse', model))}
    metrics = {'count': len(rows), **{name: base.error_metrics(values, targets)
                                    for name, values in predictions.items()}}
    groups = {'white_to_move': turns == 0, 'black_to_move': turns == 1}
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
               ROOT / 'scripts/neural_precision.py', ROOT / 'scripts/neural_training.py', ROOT / 'scripts/neural_qat.py']
    inputs = sources + [dataset / 'manifest.json', *[dataset / (p + '.jsonl') for p in PARTITIONS],
                        WARM / 'float_weights.npz', WARM / 'model.nnue', WARM / 'training.json',
                        cached_path, engine, tester, ROOT / 'config/engine-profiles/classical.json']
    protocol = {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'config': CONFIG, 'guards': GUARDS,
        'architecture': {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                         'hidden': 16, 'changed': False, 'added_parameters': 0},
        'change': 'Expand diverse Stockfish data; continue the current guarded MSE checkpoint once',
        'initial': {'path': str(WARM / 'float_weights.npz'), 'sha256': sha(WARM / 'float_weights.npz'),
                    'model_sha256': WARM_SHA, 'selected_epoch': previous['selection']['selected_epoch']},
        'data': {'directory': str(dataset), 'manifest_sha256': sha(dataset / 'manifest.json'),
                 'file_sha256': data_hashes, 'rows': {p: len(rows[p]) for p in PARTITIONS},
                 'coverage': {p: diversity_summary(rows[p]) for p in PARTITIONS},
                 'partition_policy': 'Disjoint source families and mirrored piece inputs; train only gradients; tuning only checkpoint selection',
                 'fine_tune_test_policy': 'Previously available source; held out from this fine-tune. Read for leakage validation, score warm/candidate once after saved selection; never optimizer, stopping, ranking, or retraining',
                 'test_limit': manifest.get('test_limit', 'Previously available sources; independence applies to this fine-tune only')},
        'selection': 'One seed, one run; minimum tuning integer MSE, epoch0 protected, earliest tie',
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
    packed = {p: stm.pack_records(rows[p]) for p in ('train', 'tuning')}
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
    metrics = {p: score_partition(rows[p], warm, model) for p in PARTITIONS}
    require(metrics['tuning']['diverse']['mse_cp2'] <= metrics['tuning']['warm']['mse_cp2'],
            'Best restore violated epoch-zero protection')
    report = {'config': CONFIG, 'guards': GUARDS, 'candidate_runs': 1,
              'training_wall_seconds': learning_seconds, 'selection': selection, 'metrics': metrics,
              'model_sha256': sha(output / 'model.nnue'),
              'model_changed_from_warm': stm.model_checksum(model) != stm.model_checksum(warm),
              'architecture_unchanged': True, 'loss': 'mse', 'history': history,
              'test_evaluations': 1, 'test_used_in_selection': False,
              'test_scored_after_saved_selection': True,
              'interpretation': 'Tuning MSE selects this checkpoint. One fine-tune-held-out comparison on previously available sources measures target fit, not entirely unseen generalization or Elo.'}
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
