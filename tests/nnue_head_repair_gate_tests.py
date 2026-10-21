"""Fresh repair gate tests; no native, teacher or fitting processes."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import neural_training as base
import nnue_head_repair_gate as gate
import nnue_generalization_gate as previous


def opening():
    moves = ['e2e4', 'e7e5', 'g1f3', 'b8c6']
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return {'moves': moves, 'fen_after_opening': board.fen(en_passant='fen'),
            'family_id': 'fixed-family', 'cluster_id': 'fixed-cluster'}


def root_at(length):
    source = opening()
    board = chess.Board()
    for text in source['moves']:
        board.push_uci(text)
    for _ in range(length):
        quiet = [move for move in board.legal_moves
                 if not board.is_capture(move) and not board.gives_check(move)]
        board.push(min(quiet, key=lambda move: move.uci()))
    return board


class RepairGateTests(unittest.TestCase):
    def test_fixed_odd_families_and_unchanged_pass_criteria(self):
        self.assertEqual(gate.INDICES, list(range(1, 32, 2)))
        self.assertEqual(gate.EXTENSION_PLIES, (4, 6, 8, 10))
        self.assertEqual(gate.CRITERIA, previous.CRITERIA)
        self.assertEqual(gate.CRITERIA['games_if_pass'], 16)
        self.assertEqual(gate.CRITERIA['games_if_fail'], 0)

    def test_exact_four_quiet_lexicographic_moves_and_all_children_reserved(self):
        rows, excluded, blocked = gate.select_roots(set(), openings=[opening()], indices=[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['deterministic_extension_uci']), 4)
        self.assertEqual(rows[0]['fen'], root_at(4).fen(en_passant='fen'))
        self.assertEqual(blocked, gate.root_features(root_at(4)))
        self.assertEqual(excluded, [])

    def test_only_exposure_allows_predeclared_extensions_no_other_refill(self):
        exposed = {base.feature_identity(root_at(n)) for n in (4, 6)}
        rows, excluded, blocked = gate.select_roots(exposed, openings=[opening()], indices=[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['deterministic_extension_uci']), 8)
        self.assertFalse(blocked & exposed)
        self.assertEqual([row['extension_plies'] for row in excluded], [4, 6])
        # In this fixture the lexicographic path cycles at ten plies. The
        # feature identity excludes clocks and correctly refuses that refill.
        exposed.add(base.feature_identity(root_at(8)))
        rows, excluded, blocked = gate.select_roots(exposed, openings=[opening()], indices=[0])
        self.assertEqual(rows, [])
        self.assertEqual(blocked, set())
        self.assertEqual([row['extension_plies'] for row in excluded], [4, 6, 8, 10])

    def test_root_fen_and_wrapped_root_pvs_are_exposure(self):
        board = root_at(0)
        first = next(iter(board.legal_moves))
        board.push(first)
        second = next(iter(board.legal_moves))
        board.push(second)
        end = base.feature_identity(board)
        start = root_at(0).fen(en_passant='fen')
        moves = [first.uci(), second.uci()]
        for row in ({'root_fen': start, 'search': {'pv': moves}},
                    {'root': {'fen': start}, 'candidate': {'pv_uci': moves}},
                    {'source': {'fen': start}, 'warm': {'pv': ' '.join(moves)}}):
            with self.subTest(row=row):
                self.assertIn(end, gate.position_features(row))
                self.assertIn(base.feature_identity(root_at(0)), gate.position_features(row))

    def test_local_child_fen_overrides_parent_context_and_legal_prefix_survives(self):
        root = root_at(0)
        child = root.copy()
        child.push(next(iter(child.legal_moves)))
        move = next(iter(child.legal_moves))
        end = child.copy()
        end.push(move)
        row = {'root_fen': root.fen(), 'child': {'fen': child.fen(),
               'search': {'pv': [move.uci(), 'a1a8', 'bad']}}}
        self.assertIn(base.feature_identity(end), gate.position_features(row))
        self.assertIn(base.feature_identity(end.mirror()), gate.position_features(row))

    def test_complete_history_prefix_is_exposure(self):
        row = {'initial_fen': chess.STARTING_FEN,
               'root_history': ['e2e4', 'e7e5', 'g1f3', 'illegal']}
        board = chess.Board()
        for move in row['root_history'][:3]:
            board.push_uci(move)
            self.assertIn(base.feature_identity(board), gate.position_features(row))

    def test_inventory_excludes_own_directory_and_snapshots_retains_old_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            checks = Path(temporary)
            old, own = checks / 'nnue-old', checks / 'nnue-new' / 'gate-001'
            old.mkdir(); own.mkdir(parents=True)
            gate.write(old / 'blocked-features.json', ['a' * 64])
            gate.write(old / 'probe.json', {'root_fen': root_at(0).fen(),
                                         'search': {'pv': ['a2a3']}})
            gate.write(own / 'frozen-roots.json', [{'fen': root_at(4).fen()}])
            snapshot = old / 'source-snapshot'; snapshot.mkdir()
            gate.write(snapshot / 'data.json', [{'fen': root_at(6).fen()}])
            exposed, paths = gate.exposure_inventory(directory=own, checks=checks)
            self.assertIn('a' * 64, exposed)
            self.assertIn(base.feature_identity(root_at(0)), exposed)
            self.assertNotIn(base.feature_identity(root_at(4)), exposed)
            self.assertNotIn(base.feature_identity(root_at(6)), exposed)
            self.assertEqual(len(paths), 2)

    def test_configuration_binds_latest_warm_and_restores_globals_on_error(self):
        names = ('BINARY', 'BINARY_SHA', 'WARM', 'WARM_SHA', 'exposure_inventory')
        old = {name: getattr(previous, name) for name in names}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            gate.write(directory / 'plan.json', {'criteria': gate.CRITERIA, 'teacher_depth': 10,
                       'warm_model': str(gate.WARM), 'warm_sha256': gate.WARM_SHA,
                       'binary_path': str(directory / 'native.exe'), 'binary_sha256': 'b' * 64})
            with self.assertRaises(RuntimeError):
                with gate.configured(directory):
                    self.assertEqual(previous.WARM, gate.WARM)
                    self.assertEqual(previous.WARM_SHA, gate.WARM_SHA)
                    self.assertEqual(previous.BINARY, directory / 'native.exe')
                    with patch.object(gate, 'exposure_inventory', return_value=({'feature'}, [])) as inventory:
                        self.assertEqual(previous.exposure_inventory(), ({'feature'}, []))
                        inventory.assert_called_once_with(directory=directory)
                    raise RuntimeError('mock failure')
        self.assertEqual({name: getattr(previous, name) for name in names}, old)

    def test_configuration_rejects_stale_warm_or_changed_criteria(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            plan = {'criteria': gate.CRITERIA, 'teacher_depth': 10,
                    'warm_model': str(gate.WARM), 'warm_sha256': previous.WARM_SHA}
            gate.write(directory / 'plan.json', plan)
            with self.assertRaisesRegex(ValueError, 'Head repair warm model differs'):
                with gate.configured(directory):
                    self.fail('stale model accepted')
            plan['warm_sha256'] = gate.WARM_SHA
            plan['criteria'] = dict(gate.CRITERIA, minimum_bestmove_match_gain=0)
            gate.write(directory / 'plan.json', plan)
            with self.assertRaisesRegex(ValueError, 'criteria/teacher depth changed'):
                with gate.configured(directory):
                    self.fail('changed criteria accepted')


if __name__ == '__main__':
    unittest.main()
