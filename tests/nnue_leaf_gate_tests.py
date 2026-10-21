"""Fresh-input inventory and sealed gate tests; no process or learning runs."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import neural_training as base
import nnue_generalization_gate as previous
import nnue_leaf_gate as gate


class LeafGateTests(unittest.TestCase):
    def test_case_source_context_replays_nested_native_pv_and_mirror(self):
        board = chess.Board()
        board.push_uci('e2e4')
        row = {'source': {'fen': board.fen()},
               'searches': {'candidate': {'pv_uci': ['e7e5', 'g1f3']}}}
        result = gate.position_features(row)
        for move in ('e7e5', 'g1f3'):
            board.push_uci(move)
            self.assertIn(base.feature_identity(board), result)
            self.assertIn(base.feature_identity(board.mirror()), result)

    def test_inventory_includes_teacher_probe_previous_blocks_excludes_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            checks = Path(temporary)
            past, current = checks / 'nnue-past', checks / 'nnue-current/gate-001'
            past.mkdir(); current.mkdir(parents=True)
            board = chess.Board()
            board.push_uci('e2e4')
            (past / 'teacher-queries.jsonl').write_text(json.dumps({'fen': board.fen()}) + '\n')
            board.push_uci('e7e5')
            (past / 'regression-probes.json').write_text(json.dumps({'probe': {'fen': board.fen()}}))
            sentinel = 'a' * 64
            (past / 'blocked-features.json').write_text(json.dumps([sentinel]))
            board.push_uci('g1f3')
            untouched = base.feature_identity(board)
            (current / 'frozen-roots.json').write_text(json.dumps([{'fen': board.fen()}]))
            features, paths = gate.exposure_inventory(directory=current, checks=checks)
            self.assertIn(sentinel, features)
            self.assertNotIn(untouched, features)
            self.assertEqual(len(paths), 3)

    def test_all_child_exposure_excludes_fixed_root_without_other_refill(self):
        opening = {'moves': ['e2e4', 'c7c5', 'g1f3', 'd7d6'],
                   'family_id': 'family', 'cluster_id': 'cluster'}
        board = chess.Board()
        for text in opening['moves']: board.push_uci(text)
        opening['fen_after_opening'] = board.fen(en_passant='fen')
        roots, excluded, blocked = gate.select_roots(set(), openings=[opening], indices=[0])
        self.assertEqual(len(roots), 1)
        self.assertEqual(blocked, gate.root_features(board))
        child = board.copy(); child.push(next(iter(board.legal_moves)))
        roots, excluded, blocked = gate.select_roots(
            {base.feature_identity(child)}, openings=[opening], indices=[0])
        self.assertEqual(roots, [])
        self.assertEqual(excluded[0]['extension_plies'], 0)

    def test_index27_extension_is_quiet_lexicographic_and_position_only(self):
        openings = deepcopy(gate.read(gate.OPENINGS)['openings'])
        source = openings[27]
        board = chess.Board(source.get('initial_fen', chess.STARTING_FEN))
        for text in source['moves']: board.push_uci(text)
        exposed = {base.feature_identity(board)}
        roots, excluded, blocked = gate.select_roots(exposed, openings=openings, indices=[27])
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(roots[0]['deterministic_extension_uci']), 2)
        for text in roots[0]['deterministic_extension_uci']:
            choices = [move for move in board.legal_moves
                       if not board.is_capture(move) and not board.gives_check(move)]
            self.assertEqual(text, min(choices, key=lambda move: move.uci()).uci())
            board.push_uci(text)
        self.assertEqual(board.fen(en_passant='fen'), roots[0]['fen'])
        self.assertFalse(blocked & exposed)

    def test_same_gate_criteria_and_configuration_restores_globals_on_error(self):
        self.assertEqual(gate.CRITERIA, previous.CRITERIA)
        self.assertEqual(gate.CRITERIA['games_if_pass'], 16)
        self.assertEqual(gate.CRITERIA['games_if_fail'], 0)
        old = previous.BINARY, previous.BINARY_SHA, previous.exposure_inventory
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            gate.write(directory / 'plan.json', {'criteria': gate.CRITERIA, 'teacher_depth': 10,
                       'warm_sha256': gate.WARM_SHA, 'warm_model': str(gate.WARM),
                       'binary_path': str(directory / 'new.exe'), 'binary_sha256': 'b' * 64})
            with self.assertRaises(RuntimeError):
                with gate.configured(directory):
                    self.assertEqual(previous.BINARY, directory / 'new.exe')
                    raise RuntimeError('mock failure')
        self.assertEqual((previous.BINARY, previous.BINARY_SHA, previous.exposure_inventory), old)

    def test_final_seal_rejects_mutated_reservation_input_before_fitting(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            model, binary = directory / 'warm.nnue', directory / 'native.exe'
            model.write_bytes(b'warm'); binary.write_bytes(b'binary')
            gate.write(directory / 'frozen-roots.json', [])
            gate.write(directory / 'blocked-features.json', [])
            gate.write(directory / 'reservation.json', {'input_sha256': {str(model): gate.sha(model)},
                       'frozen_roots_sha256': gate.sha(directory / 'frozen-roots.json'),
                       'blocked_features_sha256': gate.sha(directory / 'blocked-features.json')})
            model.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Reservation input changed'):
                gate.seal(directory, binary)
            self.assertFalse((directory / 'plan.json').exists())


if __name__ == '__main__':
    unittest.main()
