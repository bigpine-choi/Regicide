"""Keep source-family splits, legal histories, and strict mirror separation."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_diverse_bank as bank
from neural_search_data.schema import feature_hash


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def fixture(root):
    """Three legal source games share an initial position across all splits."""
    assignments = {'f-train': 'train', 'f-validation': 'validation', 'f-test': 'test'}
    sources = {'trainval': [('train', ['e2e4', 'e7e5', 'g1f3', 'b8c6']),
                            ('validation', ['d2d4', 'd7d5', 'c2c4', 'e7e6'])],
               'heldout': [('test', ['c2c4', 'g8f6', 'b1c3', 'e7e6'])]}
    for name, games in sources.items():
        directory = root / name
        directory.mkdir()
        (directory / 'bank.json').write_text(json.dumps({'master_partition': assignments}), encoding='utf-8')
        game_rows, query_rows = [], []
        for game_id, (partition, moves) in enumerate(games):
            game = {'game_id': f'g{game_id}', 'family_id': f'f-{partition}', 'partition': partition,
                    'initial_fen': chess.STARTING_FEN, 'moves_uci': moves}
            game_rows.append(game)
            board, history = chess.Board(), []
            for ply in range(len(moves)):
                query_rows.append({'query_id': f'q{game_id}-{ply}', 'game_id': game['game_id'],
                                   'family_id': game['family_id'], 'partition': partition,
                                   'initial_fen': chess.STARTING_FEN, 'history': history.copy(),
                                   'fen': board.fen(en_passant='fen'), 'feature_hash': feature_hash(board),
                                   'kind': 'selfplay_start', 'subtype': 'normal'})
                board.push_uci(moves[ply])
                history.append(moves[ply])
        write_rows(directory / 'games.jsonl', game_rows)
        write_rows(directory / 'queries.jsonl', query_rows)


class DiverseBankTests(unittest.TestCase):
    def test_original_groups_and_mirrors_and_deterministic_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            rows, manifest = bank.build_bank(root, [], counts={'train': 2, 'validation': 2, 'test': 2})
            repeat, repeated_manifest = bank.build_bank(root, [], counts={'train': 2, 'validation': 2, 'test': 2})
            self.assertEqual(rows, repeat)
            self.assertEqual(manifest, repeated_manifest)
            self.assertEqual(len(rows), 6)
            self.assertGreaterEqual(manifest['exclusions']['cross_partition_piece_or_mirror'], 3)
            identities = {}
            for row in rows:
                board = chess.Board(row['initial_fen'])
                for move in row['history']:
                    board.push_uci(move)
                self.assertEqual(board.fen(en_passant='fen'), row['fen'])
                self.assertEqual(manifest['family_assignments'][row['family_id']], row['partition'])
                self.assertEqual(feature_hash(board), feature_hash(board.mirror()))
                self.assertNotIn(row['feature_hash'], identities)
                identities[row['feature_hash']] = row['partition']
                self.assertEqual(row['source_history_plies'], len(row['history']))

    def test_reused_and_match_prefix_are_excluded(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            reused = json.loads((root / 'trainval/queries.jsonl').read_text().splitlines()[1])
            match_path = root / 'match.json'
            match_path.write_text(json.dumps({'openings': [{'moves': ['d2d4', 'd7d5']}]}), encoding='utf-8')
            rows, manifest = bank.build_bank(root, [reused], counts={'train': 1, 'validation': 1, 'test': 1},
                                             match_openings_path=match_path)
            self.assertNotIn(reused['feature_hash'], {row['feature_hash'] for row in rows})
            self.assertEqual(manifest['exclusions']['reused_piece_or_mirror'], 1)
            self.assertEqual(manifest['exclusions']['match_opening_prefix_piece_or_mirror'], 2)

    def test_color_rank_mirror_collision_removes_both_partitions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            games_path = root / 'trainval/games.jsonl'
            queries_path = root / 'trainval/queries.jsonl'
            games, queries = bank._rows(games_path), bank._rows(queries_path)
            original = chess.Board('7k/8/8/8/8/8/P7/K7 w - - 0 1')
            for index, (partition, board, move) in enumerate((
                    ('train', original, 'a1b1'), ('validation', original.mirror(), 'a8b8'))):
                game = {'game_id': f'mirror{index}', 'family_id': f'f-{partition}',
                        'partition': partition, 'initial_fen': board.fen(en_passant='fen'),
                        'moves_uci': [move]}
                games.append(game)
                queries.append({'query_id': f'mirror-q{index}', 'game_id': game['game_id'],
                                'family_id': game['family_id'], 'partition': partition,
                                'initial_fen': game['initial_fen'], 'history': [],
                                'fen': game['initial_fen'], 'feature_hash': feature_hash(board),
                                'kind': 'synthetic_endgame', 'subtype': 'pawn'})
            write_rows(games_path, games)
            write_rows(queries_path, queries)
            rows, manifest = bank.build_bank(root, [], counts={'train': 1, 'validation': 1, 'test': 1})
            self.assertEqual(manifest['exclusions']['cross_partition_piece_or_mirror'], 5)
            self.assertNotIn(feature_hash(original), {row['feature_hash'] for row in rows})

    def test_source_history_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            path = root / 'trainval/queries.jsonl'
            rows = bank._rows(path)
            rows[1]['history'] = ['d2d4']
            write_rows(path, rows)
            with self.assertRaisesRegex(ValueError, 'original game'):
                bank.build_bank(root, [], counts={'train': 1, 'validation': 1, 'test': 1})

    def test_reused_partition_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            row = bank._rows(root / 'trainval/queries.jsonl')[1]
            row['partition'] = 'test'
            with self.assertRaisesRegex(ValueError, 'original source-family partition'):
                bank.build_bank(root, [row], counts={'train': 1, 'validation': 1, 'test': 1})

    def test_small_budget_and_unavailable_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture(root)
            rows, _ = bank.build_bank(root, [], counts={'train': 1, 'validation': 0, 'test': 0})
            self.assertEqual(len(rows), 1)
            with self.assertRaisesRegex(ValueError, 'Insufficient'):
                bank.build_bank(root, [], counts={'train': 100, 'validation': 1, 'test': 1})


if __name__ == '__main__':
    unittest.main()
