"""Frozen diverse-bank authentication, leakage, and post-selection score checks."""
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
import train_nnue_diverse as training


def row(moves, family, target=100):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return {'fen': board.fen(en_passant='fen'), 'family_id': family,
            'feature_hash': base.feature_identity(board), 'target_white_cp': target,
            'teacher_white_cp': target}


class DiverseBankTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.rows = {'train': [row(['e2e4'], 'family-1')],
                     'tuning': [row(['d2d4', 'd7d5'], 'family-2')],
                     'test': [row(['c2c4', 'g8f6', 'b1c3'], 'family-3')]}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self):
        files = {}
        for partition, rows in self.rows.items():
            path = self.directory / (partition + '.jsonl')
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            files[path.name] = sha(path)
        manifest = {'files': files, 'rows': {key: len(value) for key, value in self.rows.items()}}
        (self.directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_accepts_authenticated_family_and_piece_disjoint_partitions(self):
        self.write()
        manifest, actual, hashes = training.load_bank(self.directory)
        self.assertEqual(actual, self.rows)
        self.assertEqual(hashes, manifest['files'])

    def test_hash_authentication_catches_position_file_changed_after_freeze(self):
        self.write()
        with (self.directory / 'test.jsonl').open('a', encoding='utf-8') as stream:
            stream.write('\n')
        with self.assertRaisesRegex(ValueError, 'Dataset bytes differ'):
            training.load_bank(self.directory)

    def test_same_family_may_not_cross_train_and_tuning(self):
        self.rows['tuning'][0]['family_id'] = 'family-1'
        self.write()
        with self.assertRaisesRegex(ValueError, 'Source families cross'):
            training.load_bank(self.directory)

    def test_mirror_copy_may_not_cross_train_and_test(self):
        board = chess.Board(self.rows['train'][0]['fen']).mirror()
        self.rows['test'] = [{**self.rows['test'][0], 'fen': board.fen(en_passant='fen'),
                              'feature_hash': base.feature_identity(board)}]
        self.write()
        with self.assertRaisesRegex(ValueError, 'Piece inputs cross'):
            training.load_bank(self.directory)

    def test_false_target_and_feature_receipts_are_rejected(self):
        self.rows['test'][0]['target_white_cp'] = True
        self.write()
        with self.assertRaisesRegex(ValueError, 'exact bounded Stockfish'):
            training.load_bank(self.directory)
        self.rows['test'][0]['target_white_cp'] = 100
        self.rows['test'][0]['feature_hash'] = '0' * 64
        self.write()
        with self.assertRaisesRegex(ValueError, 'feature identity differs'):
            training.load_bank(self.directory)

    def test_before_after_metrics_include_side_and_piece_groups_without_training(self):
        parameters = stm.with_turn_parameters({
            'input_weights': np.zeros((768, 16)), 'hidden_bias': np.zeros(16),
            'output_weights': np.zeros(16)})
        model = stm.quantize(parameters, 32)
        records = [row(['e2e4'], 'family-1', 100), row(['d2d4', 'd7d5'], 'family-2', -50)]
        result = training.score_partition(records, model, model)
        self.assertEqual(result['warm']['mse_cp2'], 6250)
        self.assertEqual(result['warm'], result['diverse'])
        self.assertEqual(result['by_group']['black_to_move']['count'], 1)
        self.assertEqual(result['by_group']['white_to_move']['count'], 1)
        self.assertEqual(result['by_group']['25plus_pieces']['count'], 2)
        self.assertEqual(training.diversity_summary(records)['families'], 2)


if __name__ == '__main__':
    unittest.main()
