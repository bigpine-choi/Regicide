"""Small synthetic sibling-data contracts; no teacher, fit, engine or match."""
from __future__ import annotations

import copy
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
import train_nnue_loss_repair as previous
import train_nnue_ordering as ordering


def row(board, family, partition, target=100):
    return {'fen': board.fen(en_passant='fen'), 'feature_hash': base.feature_identity(board),
            'family_id': family, 'partition': partition, 'target_white_cp': target,
            'teacher_white_cp': target, 'replay_weight': 1}


def pair(history, moves, partition, family, *, initial=chess.STARTING_FEN):
    board = chess.Board(initial)
    for text in history:
        board.push_uci(text)
    result = {'root_id': family + '-root', 'root_fen': board.fen(en_passant='fen'),
              'initial_fen': initial, 'root_history': history, 'partition': partition,
              'family_id': family, 'root_sign': 1 if board.turn else -1,
              'move_a': moves[0], 'move_b': moves[1], 'target_gap_white_cp': 200}
    for child, move, target in zip(('a', 'b'), moves, (100, -100)):
        following = board.copy()
        following.push_uci(move)
        result[child] = {**row(following, family, partition, target),
                         'initial_fen': initial, 'history': history + [move]}
    return result


def fixture():
    points = {}
    for partition, move, family in (('train', 'c2c4', 'one'), ('tuning', 'f2f4', 'two'),
                                    ('test', 'b2b4', 'three')):
        board = chess.Board()
        board.push_uci(move)
        points[partition] = [row(board, family, partition)]
    pairs = {'train': [pair(['e2e4', 'e7e5'], ('g1f3', 'b1c3'), 'train', 'one')],
             'tuning': [pair(['d2d4', 'd7d5'], ('g1f3', 'b1c3'), 'tuning', 'two')]}
    return points, pairs


def write_bank(path, points, pairs):
    files, pair_files = {}, {}
    for partition, values in points.items():
        file = path / (partition + '.jsonl')
        file.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')
        files[file.name] = sha(file)
    for partition, values in pairs.items():
        file = path / ('pairs-' + partition + '.jsonl')
        file.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')
        pair_files[file.name] = sha(file)
    manifest = {'files': files, 'rows': {p: len(v) for p, v in points.items()},
                'pair_files': pair_files, 'pair_rows': {p: len(v) for p, v in pairs.items()}}
    (path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


class OrderingDataTests(unittest.TestCase):
    def test_legal_siblings_load_and_repeated_same_partition_features_are_allowed(self):
        points, pairs = fixture()
        duplicate = copy.deepcopy(pairs['train'][0])
        # One shared child can participate in another different sibling comparison.
        board = chess.Board(duplicate['root_fen'])
        board.push_uci('f1c4')
        duplicate['move_b'] = 'f1c4'
        duplicate['b'] = {**row(board, 'one', 'train', -100),
                          'history': duplicate['root_history'] + ['f1c4']}
        pairs['train'].append(duplicate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest = write_bank(path, points, pairs)
            _, actual, _ = ordering.load_bank(path)
            loaded, hashes = ordering.load_pairs(path, actual, manifest)
            self.assertEqual(len(loaded['train']), 2)
            self.assertEqual(set(hashes), {'pairs-train.jsonl', 'pairs-tuning.jsonl'})

    def test_pair_bytes_are_authenticated(self):
        points, pairs = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest = write_bank(path, points, pairs)
            with (path / 'pairs-train.jsonl').open('a') as stream:
                stream.write('\n')
            with self.assertRaisesRegex(ValueError, 'Pair bytes differ'):
                ordering.load_pairs(path, points, manifest)

    def test_root_history_child_move_color_gap_and_exact_cp_are_checked(self):
        for field in ('history', 'child', 'sign', 'gap', 'bounded', 'teacher'):
            points, pairs = fixture()
            item = pairs['train'][0]
            if field == 'history':
                item['root_history'] = ['e2e5']
            elif field == 'child':
                item['a']['fen'] = item['b']['fen']
            elif field == 'sign':
                item['root_sign'] = -1
            elif field == 'gap':
                item['target_gap_white_cp'] = -200
            elif field == 'bounded':
                item['a']['target_white_cp'] = True
            else:
                item['a']['teacher_white_cp'] = 101
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                manifest = write_bank(path, points, pairs)
                with self.assertRaises(ValueError):
                    ordering.load_pairs(path, points, manifest)

    def test_pair_child_mirror_and_family_cannot_leak_into_other_point_split(self):
        for kind in ('feature', 'family'):
            points, pairs = fixture()
            if kind == 'feature':
                mirrored = chess.Board(pairs['train'][0]['a']['fen']).mirror()
                points['test'][0] = row(mirrored, 'three', 'test')
            else:
                points['test'][0]['family_id'] = 'one'
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                manifest = write_bank(path, points, pairs)
                with self.assertRaisesRegex(ValueError, 'cross partitions'):
                    ordering.load_pairs(path, points, manifest)

    def test_capture_pairs_pack_both_sides_in_same_shape_and_preserve_target_sign(self):
        value = pair([], ('e4d5', 'e4e5'), 'train', 'capture',
                     initial='r3k2r/ppp2ppp/2n5/3p4/4P3/2N5/PPP2PPP/R3K2R w KQkq - 0 10')
        packed = ordering.pack_pairs([value])
        self.assertEqual(packed[0].shape, packed[3].shape)
        self.assertEqual(float(packed[1][0] - packed[4][0]), 200)
        self.assertEqual(int(packed[2][0]), 1)
        np.testing.assert_array_equal(packed[2], packed[5])

    def test_current_mse_optimizer_and_guards_are_retained_and_test_pairs_rejected(self):
        self.assertEqual({k: v for k, v in ordering.CONFIG.items() if k != 'candidate_id'},
                         {k: v for k, v in previous.CONFIG.items() if k != 'candidate_id'})
        self.assertEqual(ordering.GUARDS, previous.GUARDS)
        self.assertEqual(ordering.WARM_SHA, '02ccdf9b7fd1ac6206774bbc356081977db26cc2c8610176f732ff6ceadc9a09')
        points, pairs = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest = write_bank(path, points, pairs)
            (path / 'pairs-test.jsonl').write_text('', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Test cannot have'):
                ordering.load_pairs(path, points, manifest)


if __name__ == '__main__':
    unittest.main()
