"""Check a trained STM model against native full refresh and incremental inference.

Uses only the existing train/validation corpus and synthetic rule fixtures.
Also checks legacy evaluation against the untouched pre-change executable.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
import copy
from pathlib import Path
import subprocess
import sys

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_precision as precision
import neural_stm as stm
import neural_target_ab_data as data
import neural_training as base
from prepare_neural_huber import require, sha, special_positions, write
from train_neural_stm import SOURCE, REFERENCE, REFERENCE_SHA


def check(engine, old_engine, output):
    loaded = data.load_dataset(SOURCE / 'trainval')
    rows = loaded['train'] + loaded['validation']
    corpus = [row['fen'] for row in rows]
    fixtures = special_positions()
    # Both turn values on the exact same valid placement: not training data.
    paired = []
    for fen in corpus:
        board = chess.Board(fen)
        if board.is_check():
            continue
        for turn in (chess.WHITE, chess.BLACK):
            board.turn = turn
            board.ep_square = None
            if board.is_valid():
                paired.append(board.fen(en_passant='fen'))
        if len(paired) >= 100:
            break
    fens = corpus + fixtures + paired
    model_path = output / 'model.nnue'
    model = stm.load_model(model_path)
    expected = np.array([stm.quantized_white(chess.Board(fen), model) for fen in fens])
    signs = np.array([1 if fen.split()[1] == 'w' else -1 for fen in fens])
    values = base.evaluate_engine(engine, fens, model_path)
    actual = np.array([row[1] for row in values])
    incremental = np.array([row[2] for row in values])
    require(np.array_equal(expected * signs, actual) and np.array_equal(actual, incremental),
            'Trained v3 Python/native full/incremental scores differ')
    require(sha(REFERENCE / 'model.nnue') == REFERENCE_SHA, 'Frozen baseline changed')
    legacy = precision.load_model(REFERENCE / 'model.nnue')
    legacy_expected = np.array([precision.quantized_white(chess.Board(fen), legacy) for fen in fens]) * signs
    old_values = base.evaluate_engine(old_engine, fens, REFERENCE / 'model.nnue')
    new_values = base.evaluate_engine(engine, fens, REFERENCE / 'model.nnue')
    require(old_values == new_values, 'Old model native scores changed in the new executable')
    require(np.array_equal(legacy_expected, [row[1] for row in new_values]), 'Legacy Python/native scores differ')
    ablated = copy.deepcopy(model)
    ablated['turn_weights'].fill(0)
    no_turn = np.array([stm.quantized_white(chess.Board(fen), ablated) for fen in fens])
    tester = engine.parent / ('regicide_neural_model_tests' + engine.suffix)
    result = subprocess.run([str(tester), str(model_path)], text=True, capture_output=True, timeout=120)
    (output / 'trained-native-model-tests.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
    require(result.returncode == 0 and '0 failures' in result.stdout, 'Actual trained model regressions failed')
    report = {'positions': len(fens), 'corpus_positions': len(corpus), 'special_positions': len(fixtures),
              'same_placement_turn_fixtures': len(paired), 'python_native_full_incremental_mismatches': 0,
              'legacy_old_new_python_mismatches': 0, 'actual_trained_model_tests_returncode': result.returncode,
              'actual_trained_model_test_summary': result.stdout.splitlines()[-1],
              'turn_weight_nonzero_count': int(np.count_nonzero(model['turn_weights'])),
              'positions_changed_by_turn_input': int(np.count_nonzero(expected != no_turn)),
              'maximum_absolute_turn_contribution_cp': int(np.max(np.abs(expected - no_turn))),
              'input_sha256': {str(p): sha(p) for p in (engine, old_engine, tester, model_path, REFERENCE / 'model.nnue')},
              'core_regressions_before_training': {'nnue_model': '13 groups, 30348 assertions, 0 failures',
                                                    'nnue': '10 groups, 51270 assertions, 0 failures',
                                                    'search': '61 groups, 21389 assertions, 0 failures'}}
    write(output / 'validation.json', report)
    print(report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('engine', type=Path)
    parser.add_argument('old_engine', type=Path)
    parser.add_argument('output', type=Path)
    arguments = parser.parse_args()
    check(arguments.engine.resolve(), arguments.old_engine.resolve(), arguments.output.resolve())
