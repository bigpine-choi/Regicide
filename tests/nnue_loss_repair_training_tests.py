"""Bounded replay, unique-bank safety and unweighted reporting contracts."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
import neural_training as base
from prepare_neural_huber import sha
import train_nnue_diverse as previous
import train_nnue_loss_repair as repair


def row(moves, family, target=100, weight=1):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return {'fen': board.fen(en_passant='fen'), 'family_id': family,
            'feature_hash': base.feature_identity(board), 'target_white_cp': target,
            'teacher_white_cp': target, 'replay_weight': weight}


class LossRepairTrainingTests(unittest.TestCase):
    def test_replay_repeats_all_arrays_in_lockstep_and_keeps_rows_unique(self):
        records = [row(['e2e4'], 'one', 100, 1),
                   row(['d2d4', 'd7d5'], 'two', -50, 4)]
        original = stm.pack_records(records)
        actual = repair.pack_replayed(records)
        self.assertEqual(len(records), 2)
        for old, new in zip(original, actual):
            np.testing.assert_array_equal(new, old[[0, 1, 1, 1, 1]])

    def test_replay_rejects_unbounded_and_noninteger_weights(self):
        for invalid in (0, -1, 5, True, 1.0, '4', None):
            with self.subTest(weight=invalid), self.assertRaisesRegex(ValueError, 'integer in'):
                repair.replay_weights([{'replay_weight': invalid}])
        np.testing.assert_array_equal(repair.replay_weights([{}, {'replay_weight': 4}]), [1, 4])

    def test_weighted_mse_is_analytic_and_unique_metrics_remain_unweighted(self):
        model = stm.quantize(stm.with_turn_parameters({
            'input_weights': np.zeros((768, 16)), 'hidden_bias': np.zeros(16),
            'output_weights': np.zeros(16)}), 32)
        records = [row(['e2e4'], 'one', 100, 1),
                   row(['d2d4', 'd7d5'], 'two', -50, 4)]
        actual = repair.score_partition(records, model, model)
        self.assertEqual(actual['count'], 2)
        self.assertEqual(actual['warm']['mse_cp2'], 6250)
        self.assertEqual(actual['weighted']['count'], 5)
        self.assertEqual(actual['weighted']['repair']['mse_cp2'], 4000)
        self.assertEqual(actual['by_group']['repair_replay']['count'], 1)
        self.assertEqual(actual['by_group']['background']['count'], 1)
        unweighted = repair.score_partition(records, model, model, weighted=False)
        self.assertEqual(unweighted['weighted']['count'], 2)
        self.assertEqual(unweighted['weighted']['repair'], unweighted['repair'])

    def test_unique_loader_rejects_mirror_leakage_before_replay(self):
        records = {'train': [row(['e2e4'], 'one', weight=4)],
                   'tuning': [row(['d2d4'], 'two', weight=4)],
                   'test': [row(['c2c4'], 'three')]}
        board = chess.Board(records['train'][0]['fen']).mirror()
        records['test'][0].update(fen=board.fen(), feature_hash=base.feature_identity(board))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.write_bank(path, records)
            with self.assertRaisesRegex(ValueError, 'Piece inputs cross'):
                repair.load_bank(path)

    def test_test_bank_requires_weight_one_and_hashes_remain_authenticated(self):
        records = {'train': [row(['e2e4'], 'one', weight=4)],
                   'tuning': [row(['d2d4'], 'two', weight=4)],
                   'test': [row(['c2c4'], 'three', weight=4)]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.write_bank(path, records)
            with self.assertRaisesRegex(ValueError, 'Test must stay unweighted'):
                repair.load_bank(path)
            records['test'][0]['replay_weight'] = 1
            self.write_bank(path, records)
            _, actual, _ = repair.load_bank(path)
            self.assertEqual(actual, records)
            with (path / 'train.jsonl').open('a') as stream:
                stream.write('\n')
            with self.assertRaisesRegex(ValueError, 'Dataset bytes differ'):
                repair.load_bank(path)

    def test_existing_optimizer_and_guard_settings_stay_exact(self):
        self.assertEqual({k: v for k, v in repair.CONFIG.items() if k != 'candidate_id'},
                         {k: v for k, v in previous.CONFIG.items() if k != 'candidate_id'})
        self.assertEqual(repair.GUARDS, previous.GUARDS)
        self.assertEqual(repair.CONFIG['loss'], 'mse')
        self.assertEqual(repair.WARM_SHA, 'c73b510cd83ca221829eb9e1bf781b86d28c5b9c47ed922d8f9fa30e08385c86')

    @staticmethod
    def write_bank(path, records):
        files = {}
        for partition, values in records.items():
            file = path / (partition + '.jsonl')
            file.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')
            files[file.name] = sha(file)
        (path / 'manifest.json').write_text(json.dumps({
            'files': files, 'rows': {key: len(value) for key, value in records.items()}}), encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
