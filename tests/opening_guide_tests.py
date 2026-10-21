"""Real pinned opening names, legal previews and truthful history boundaries."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import opening_guide as guide


def position(*moves):
    board = chess.Board()
    for san in moves:
        board.push_san(san)
    return board


class OpeningGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = guide._catalog()

    def test_real_named_openings(self):
        cases = [(('e4', 'e5', 'Nf3', 'Nc6', 'Bb5'), "Ruy Lopez", "C60"),
                 (('e4', 'e5', 'Nf3', 'Nc6', 'Bc4'), "Italian Game", "C50"),
                 (('d4', 'd5', 'c4'), "Queen's Gambit", "D06")]
        for moves, name, eco in cases:
            with self.subTest(name=name):
                result = guide.get_opening_guide(position(*moves))
                self.assertEqual(result["status"], "in_catalog")
                self.assertEqual(result["matched_opening"]["name"], name)
                self.assertEqual(result["matched_opening"]["eco"], eco)
                self.assertEqual(result["matched_opening"]["match_type"], "exact")

    def test_morphy_is_immediate_but_exchange_requires_more_moves(self):
        board = position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5')
        result = guide.get_opening_guide(board)
        branch = next(row for row in result["continuations"] if row["uci"] == "a7a6")
        self.assertEqual(branch["san"], "a6")
        self.assertIn("Ruy Lopez: Morphy Defense", [row["name"] for row in branch["immediate_openings"]])
        exchange = next(row for row in branch["reachable_openings"] if row["name"] == "Ruy Lopez: Exchange Variation")
        self.assertEqual(exchange["distance_plies"], 2)
        self.assertEqual(exchange["remaining_uci"], ["a7a6", "b5c6"])
        self.assertEqual(exchange["remaining_san"], "3...a6 4. Bxc6")
        self.assertNotIn(exchange["name"], [row["name"] for row in branch["immediate_openings"]])

    def test_transposition_uses_position_identity_and_is_labeled(self):
        direct = guide.get_opening_guide(position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5'))
        transposed = guide.get_opening_guide(position('Nf3', 'Nc6', 'e4', 'e5', 'Bb5'))
        self.assertEqual(direct["position_key"], transposed["position_key"])
        self.assertEqual(transposed["matched_opening"]["name"], "Ruy Lopez")
        self.assertEqual(transposed["matched_opening"]["match_type"], "transposition")
        self.assertEqual(direct["continuations"], transposed["continuations"])

    def test_off_catalog_preserves_last_known_name_and_undo_restores(self):
        board = position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5')
        before = guide.get_opening_guide(board)
        board.push_san('h5')
        after = guide.get_opening_guide(board)
        self.assertEqual(after["status"], "beyond_catalog")
        self.assertEqual(after["current_matches"], [])
        self.assertEqual(after["matched_opening"]["name"], "Ruy Lopez")
        self.assertEqual(after["matched_opening"]["ply"], 5)
        self.assertEqual(after["continuations"], [])
        board.pop()
        self.assertEqual(guide.get_opening_guide(board), before)

    def test_custom_root_position_match_does_not_invent_move_history(self):
        original = position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5')
        board = chess.Board(original.fen())
        result = guide.get_opening_guide(board)
        self.assertEqual(result["history_scope"], "custom")
        self.assertEqual(result["history_plies"], 0)
        self.assertEqual(result["matched_opening"]["match_type"], "position")
        self.assertEqual(result["matched_opening"]["ply"], 0)
        self.assertIn("이전", result["history_note"])

    def test_unknown_custom_fen_and_initial_board_are_truthful(self):
        custom = guide.get_opening_guide(chess.Board('4k3/8/8/8/8/8/4P3/4K3 w - - 0 1'))
        self.assertEqual(custom["status"], "unknown")
        self.assertIsNone(custom["matched_opening"])
        self.assertEqual(custom["continuations_total"], 0)
        initial = guide.get_opening_guide(chess.Board())
        self.assertEqual(initial["status"], "in_catalog")
        self.assertEqual(initial["history_scope"], "standard")
        self.assertEqual(initial["current_matches"], [])
        self.assertIsNone(initial["matched_opening"])

    def test_unnamed_catalog_position_is_in_catalog(self):
        key = next(key for key in self.catalog.positions if key not in self.catalog.named
                   and key != guide.position_key(chess.Board()))
        result = guide.get_opening_guide(chess.Board(key + ' 0 42'))
        self.assertEqual(result["status"], "in_catalog")
        self.assertEqual(result["current_matches"], [])

    def test_every_preview_is_legal_and_reaches_its_advertised_name(self):
        boards = [chess.Board(), position('e4'), position('d4', 'd5'),
                  position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5'),
                  position('d4', 'Nf6', 'c4', 'e6', 'Nc3')]
        for board in boards:
            result = guide.get_opening_guide(board)
            for branch in result["continuations"]:
                self.assertIn(chess.Move.from_uci(branch["uci"]), board.legal_moves)
                for line in branch["reachable_openings"]:
                    self.assertEqual(line["remaining_uci"][0], branch["uci"])
                    self.assertEqual(line["distance_plies"], len(line["remaining_uci"]))
                    copy_board = board.copy(stack=True)
                    moves = [chess.Move.from_uci(token) for token in line["remaining_uci"]]
                    self.assertEqual(line["remaining_san"], board.variation_san(moves))
                    for move in moves:
                        self.assertIn(move, copy_board.legal_moves)
                        copy_board.push(move)
                    names = {(self.catalog.openings[i].eco, self.catalog.openings[i].name)
                             for i in self.catalog.named[guide.position_key(copy_board)]}
                    self.assertIn((line["eco"], line["name"]), names)

    def test_preview_move_numbers_come_from_actual_current_board(self):
        board = chess.Board(position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5').fen())
        board.fullmove_number = 42
        branch = next(row for row in guide.get_opening_guide(board)["continuations"] if row["uci"] == "a7a6")
        self.assertEqual(branch["reachable_openings"][0]["remaining_san"], "42...a6")

    def test_position_key_retains_turn_castling_and_legal_ep(self):
        board = chess.Board()
        changed = board.copy()
        changed.castling_rights = 0
        self.assertNotEqual(guide.position_key(board), guide.position_key(changed))
        changed = board.copy()
        changed.turn = chess.BLACK
        self.assertNotEqual(guide.position_key(board), guide.position_key(changed))
        legal = chess.Board('4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2')
        without = legal.copy()
        without.ep_square = None
        self.assertTrue(legal.has_legal_en_passant())
        self.assertNotEqual(guide.position_key(legal), guide.position_key(without))
        pinned = chess.Board('4r1k1/8/8/3pP3/8/8/8/4K3 w - d6 0 2')
        without = pinned.copy()
        without.ep_square = None
        self.assertFalse(pinned.has_legal_en_passant())
        self.assertEqual(guide.position_key(pinned), guide.position_key(without))

    def test_game_over_keeps_name_but_hides_continuations(self):
        board = position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5')
        board.halfmove_clock = 150
        result = guide.get_opening_guide(board)
        self.assertEqual(result["matched_opening"]["name"], "Ruy Lopez")
        self.assertEqual(result["continuations"], [])
        self.assertEqual(result["continuations_total"], 0)

    def test_quiet_99_is_claimable_but_149_cannot_cross_automatic_draw(self):
        board = position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5')
        normal = guide.get_opening_guide(board)
        board.halfmove_clock = 99
        claimable = guide.get_opening_guide(board)
        self.assertEqual(claimable['continuations'], normal['continuations'])
        board.halfmove_clock = 149
        before = board.fen(en_passant='fen'), list(board.move_stack)
        result = guide.get_opening_guide(board)
        branch = next(row for row in result['continuations'] if row['uci'] == 'g8f6')
        self.assertEqual(branch['reachable_total'], branch['immediate_total'])
        self.assertEqual(branch['reachable_lines_total'], branch['immediate_total'])
        self.assertFalse(branch['reachable_truncated'])
        self.assertTrue(all(line['distance_plies'] == 1 for line in branch['reachable_openings']))
        self.assertIn('Ruy Lopez: Berlin Defense', [row['name'] for row in branch['reachable_openings']])
        child = board.copy(stack=True)
        child.push_uci('g8f6')
        self.assertTrue(child.is_game_over(claim_draw=False))
        pawn_branch = next(row for row in result['continuations'] if row['uci'] == 'a7a6')
        self.assertGreater(pawn_branch['reachable_total'], pawn_branch['immediate_total'])
        self.assertEqual((board.fen(en_passant='fen'), list(board.move_stack)), before)

    def test_actual_fivefold_history_filters_future_lines_at_terminal_child(self):
        # Reach Berlin first through another move order, then visit it four
        # times without already visiting the preceding Ruy position five times.
        moves = ['Nf3', 'Nc6', 'e4', 'Nf6', 'Bb5', 'e5']
        moves += ['Ng1', 'Ng8', 'Nf3', 'Nf6'] * 3 + ['Ng1', 'Ng8', 'Nf3']
        board = position(*moves)
        self.assertFalse(board.is_game_over(claim_draw=False))
        child = board.copy(stack=True)
        child.push_uci('g8f6')
        self.assertTrue(child.is_fivefold_repetition())
        before = board.fen(en_passant='fen'), list(board.move_stack)
        result = guide.get_opening_guide(board)
        branch = next(row for row in result['continuations'] if row['uci'] == 'g8f6')
        self.assertEqual(branch['reachable_total'], 1)
        self.assertEqual(branch['reachable_openings'][0]['name'], 'Ruy Lopez: Berlin Defense')
        self.assertEqual(branch['reachable_openings'][0]['distance_plies'], 1)
        # Every displayed line is independently checked with the full Python
        # rules/history oracle, rather than the catalogue's optimized counters.
        for branch in result['continuations']:
            for line in branch['reachable_openings']:
                probe = board.copy(stack=True)
                for token in line['remaining_uci']:
                    self.assertFalse(probe.is_game_over(claim_draw=False))
                    probe.push_uci(token)
        self.assertEqual((board.fen(en_passant='fen'), list(board.move_stack)), before)

    def test_terminal_unnamed_child_drops_branch_and_filtered_counts_match_oracle(self):
        found = None
        for opening in self.catalog.openings:
            for offset, zeroing in enumerate(opening.zeroing):
                if not zeroing and opening.keys[offset + 1] not in self.catalog.named:
                    found = opening, offset
                    break
            if found:
                break
        self.assertIsNotNone(found)
        opening, offset = found
        board = chess.Board(opening.keys[offset] + ' 149 42')
        result = guide.get_opening_guide(board)
        self.assertNotIn(opening.moves[offset], [row['uci'] for row in result['continuations']])
        expected = {}
        moves = {self.catalog.openings[i].moves[j]
                 for i, j in self.catalog.positions[guide.position_key(board)]
                 if j < len(self.catalog.openings[i].moves)}
        for token in moves:
            child = board.copy(stack=True)
            child.push_uci(token)
            names, lines = set(), set()
            for line_id, child_offset in self.catalog.positions[guide.position_key(child)]:
                item = self.catalog.openings[line_id]
                suffix = (token, *item.moves[child_offset:])
                probe, valid = board.copy(stack=True), True
                for move in suffix:
                    if probe.is_game_over(claim_draw=False):
                        valid = False
                        break
                    probe.push_uci(move)
                if valid:
                    names.add((item.eco, item.name))
                    lines.add(((item.eco, item.name), suffix))
            if names:
                expected[token] = len(names), len(lines)
        self.assertEqual(result['continuations_total'], len(expected))
        for branch in result['continuations']:
            self.assertEqual((branch['reachable_total'], branch['reachable_lines_total']), expected[branch['uci']])
            self.assertEqual(branch['reachable_truncated'], branch['reachable_total'] > guide.MAX_REACHABLE_OPENINGS)

    def test_output_mutation_does_not_change_index_board_or_next_response(self):
        board = position('e4', 'e5', 'Nf3', 'Nc6', 'Bb5')
        fen, moves = board.fen(en_passant='fen'), list(board.move_stack)
        result = guide.get_opening_guide(board)
        original = copy.deepcopy(result)
        result["matched_opening"]["name"] = 'changed by caller'
        result["source"]["commit"] = 'changed by caller'
        result["continuations"][0]["reachable_openings"][0]["remaining_uci"].append('a1a8')
        self.assertEqual(guide.get_opening_guide(board), original)
        self.assertEqual(board.fen(en_passant='fen'), fen)
        self.assertEqual(board.move_stack, moves)
        json.dumps(original, ensure_ascii=False)
        with self.assertRaises(TypeError):
            self.catalog.positions['caller'] = ()

    def test_truncation_has_explicit_totals_and_does_not_mean_recommendation(self):
        with patch.object(guide, 'MAX_CONTINUATIONS', 2), patch.object(guide, 'MAX_REACHABLE_OPENINGS', 1):
            result = guide.get_opening_guide(chess.Board())
        self.assertEqual(len(result["continuations"]), 2)
        self.assertGreater(result["continuations_total"], 2)
        self.assertTrue(result["continuations_truncated"])
        self.assertTrue(any(row["reachable_truncated"] for row in result["continuations"]))
        self.assertIn("추천", result["disclaimer"])
        self.assertIn("추천 순위가 아닙니다", result["ordering"])

    def test_data_failure_is_readable_and_no_runtime_network_is_used(self):
        with patch('socket.create_connection', side_effect=AssertionError('runtime network access')):
            self.assertEqual(guide.get_opening_guide(chess.Board())["status"], "in_catalog")
        with patch.object(guide, '_catalog', side_effect=guide.CatalogError('missing')):
            result = guide.get_opening_guide(chess.Board())
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("자료", result["message"])
        self.assertEqual(result["continuations"], [])

    def test_pinned_files_and_license_hashes_are_verified(self):
        manifest = json.loads((guide.DATA_DIRECTORY / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(self.catalog.source['commit'], manifest['commit'])
        self.assertEqual(self.catalog.source['license'], 'CC0-1.0')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            for name in manifest['files']:
                (target / name).write_bytes((guide.DATA_DIRECTORY / name).read_bytes())
            with (target / 'a.tsv').open('ab') as stream:
                stream.write(b'changed')
            with self.assertRaisesRegex(guide.CatalogError, 'checksum'):
                guide._load_catalog(target)

    def test_non_object_and_malformed_manifest_fields_report_unavailable(self):
        original = json.loads((guide.DATA_DIRECTORY / 'manifest.json').read_text(encoding='utf-8'))
        cases = [None, [], 'manifest', 7, True]
        for field, value in [('commit', []), ('commit', None), ('files', []), ('files', None),
                             ('repository_url', None), ('license_url', [])]:
            cases.append({**original, field: value})
        for entry in [None, [], 'file', {**original['files']['a.tsv'], 'bytes': True},
                      {**original['files']['a.tsv'], 'bytes': -1},
                      {**original['files']['a.tsv'], 'sha256': []},
                      {**original['files']['a.tsv'], 'source_url': None}]:
            cases.append({**original, 'files': {**original['files'], 'a.tsv': entry}})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for payload in cases:
                with self.subTest(payload=payload):
                    (target / 'manifest.json').write_text(json.dumps(payload), encoding='utf-8')
                    with self.assertRaises(guide.CatalogError):
                        guide._load_catalog(target)
                    with patch.object(guide, '_catalog', side_effect=lambda: guide._load_catalog(target)):
                        result = guide.get_opening_guide(chess.Board())
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertIn('자료', result['message'])
                    self.assertEqual(result['continuations'], [])

    def test_concurrent_first_lookup_builds_one_shared_index(self):
        guide._cached_catalog.cache_clear()

        def load(_directory):
            time.sleep(.02)
            return self.catalog

        with patch.object(guide, '_load_catalog', side_effect=load) as loader:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _value: guide._catalog(), range(16)))
        self.assertEqual(loader.call_count, 1)
        self.assertTrue(all(value is self.catalog for value in results))


if __name__ == '__main__':
    unittest.main()
