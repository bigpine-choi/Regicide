"""Boss selection through the live host with controlled, non-playing peers."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import chess

import human_play_tests as base
from opening_variety_tests import Peer
from boss_play_tests import after, packet, seed_for

host = base.play_human


def admitted_seed(board, candidate):
    for number in range(4096):
        seed = f'{number:032x}'
        if (host.boss_play.plan(board, 1000, seed)['candidate_uci'] == candidate
                and host.boss_play.boss_opponent.gate(board, [], seed)['draw'] < .05):
            return seed
    raise AssertionError('no deterministic admitted seed')


class BossStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='regicide-boss-state-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        binary = root / 'fake.exe'
        binary.write_bytes(b'controlled test peer')
        self.peer = Peer()
        self.game = host.HumanGame(binary, root / 'games', lambda _: self.peer,
                                  hint_engine_factory=base.FakeFactory(),
                                  evaluation_engine_factory=base.FakeFactory(), dynamic_play_enabled=True)
        self.addCleanup(self.game.close)
        self.game.new_game('white', 1000, self.game.revision, True)
        self.game._opening_seed = admitted_seed(after('d2d4'), 'e7e5')

    def prepare(self, *, cp=-150, failure=None, gate=None):
        board = after('d2d4')
        self.peer.scripts = [
            {'move': 'g8f6', 'packets': [packet(board, 'g8f6', 25)]},
            {'move': 'd2d4', 'packets': [packet(chess.Board(), 'd2d4', 0)]},
            {'move': 'e7e5', 'packets': [packet(board, 'e7e5', cp)], 'failure': failure, 'gate': gate}]
        return self.game.human_move('d2d4', self.game.revision)

    def reply(self):
        return self.game.engine_move(self.game.revision)

    def test_full_baseline_then_one_checked_englund_and_exact_continuation(self):
        before = self.prepare()
        self.assertTrue(before['engine']['boss_mode'])
        self.assertFalse(before['engine']['dynamic_play_enabled'])
        self.assertFalse(before['engine']['opening_variety_enabled'])
        result = self.reply()
        self.assertEqual(result['last_move'], 'e7e5')
        self.assertIsNone(result['error'])
        self.assertEqual(result['engine']['boss_phase'], 'trap_hunt')
        self.assertEqual(result['engine']['extra_budget_ms'], 1000)
        self.assertEqual(len(self.peer.searches), 3)
        self.assertEqual(self.peer.searches[0]['limit'].time, 1)
        self.assertIsNone(self.peer.searches[0]['root_moves'])
        self.assertEqual(self.peer.searches[2]['limit'].depth, 4)
        self.assertLessEqual(self.peer.searches[2]['limit'].time, 1)
        self.assertIsNot(self.peer.searches[0]['game'], self.peer.searches[2]['game'])
        self.assertEqual(self.peer.searches[1]['board'].move_stack, [])
        self.assertLessEqual(self.peer.searches[1]['limit'].time, .225)
        self.game.human_move('d4e5', self.game.revision)
        before_human = after('d2d4', 'e7e5')
        self.peer.scripts = [{'move': 'b8c6', 'packets': [packet(self.game.board, 'b8c6', -80)]},
                             {'move': 'd4e5', 'packets': [packet(before_human, 'd4e5', 0)]}]
        with patch('poisoned_piece.enumerate_offers', return_value=[]):
            next_result = self.reply()
        self.assertEqual(next_result['last_move'], 'b8c6')
        self.assertEqual(len(self.peer.searches), 5)
        self.assertIsNone(self.peer.searches[-2]['root_moves'])
        self.assertEqual(self.peer.searches[-2]['limit'].time, 1)

    def test_json_and_pgn_preserve_explicit_opening_loss(self):
        self.prepare()
        self.reply()
        record = json.loads((self.game.save_dir / f'{self.game.game_id}.json').read_text(encoding='utf-8'))
        row = record['boss_play']['decisions'][0]
        self.assertEqual(row['history_uci'], ['d2d4'])
        self.assertEqual(row['line_id'], 'englund')
        self.assertEqual(row['loss_cp'], 175)
        self.assertTrue(row['line_complete'])
        self.assertTrue(row['opponent_observation']['valid'])
        self.assertEqual(row['frequency']['profile']['quality_observations'], 1)
        self.assertEqual(record['boss_play']['opponent_profile']['quality_observations'], 1)
        self.assertFalse(record['dynamic_play']['enabled'])
        self.assertFalse(record['opening_variety']['enabled'])
        self.assertIn('[BossMode "1"]', self.game.pgn())
        self.assertNotIn('DynamicPlayPolicy', self.game.pgn())

    def test_conservative_gate_keeps_risky_signature_at_full_baseline(self):
        board = after('d2d4')
        self.game._opening_seed = next(f'{n:032x}' for n in range(4096)
            if host.boss_play.plan(board, 1000, f'{n:032x}')['candidate_uci'] == 'e7e5'
            and host.boss_play.boss_opponent.gate(board, [], f'{n:032x}')['draw'] > .5)
        self.prepare()
        result = self.reply()
        row = self.game._boss_decisions[2]
        self.assertEqual(result['last_move'], 'g8f6')
        self.assertFalse(row['frequency']['attempt_trap'])
        self.assertEqual(row['reason'], 'opponent_frequency_hold')
        self.assertEqual(len(self.peer.searches), 2)
        self.assertEqual(self.peer.searches[0]['limit'].time, 1)

    def test_closed_gate_allows_only_checked_near_reference_gambit_setup(self):
        board = chess.Board()
        seed = next(f'{n:032x}' for n in range(4096)
            if host.boss_play.plan(board, 1000, f'{n:032x}')['candidate_uci'] == 'e2e4'
            and host.boss_play.boss_opponent.gate(board, [], f'{n:032x}')['draw'] > .5)
        for cp, expected in [(15, 'e2e4'), (14, 'b1c3')]:
            with self.subTest(cp=cp):
                self.game.new_game('black', 1000, self.game.revision, True)
                self.game._opening_seed = seed
                self.peer.scripts = [{'move': 'b1c3', 'packets': [packet(board, 'b1c3', 50)]},
                                     {'move': 'e2e4', 'packets': [packet(board, 'e2e4', cp)]}]
                result = self.reply()
                row = self.game._boss_decisions[1]
                self.assertEqual(result['last_move'], expected)
                self.assertFalse(row['frequency']['attempt_trap'])
                self.assertTrue(row['conservative_setup'])
                self.assertEqual(row['setup_max_loss_cp'], 35)
                self.assertEqual(row['frequency']['profile']['trap_opportunities'], 0)

    def test_current_human_error_changes_this_turn_gate_without_changing_baseline_time(self):
        board = after('d2d4')
        self.game._opening_seed = next(f'{n:032x}' for n in range(4096)
            if host.boss_play.plan(board, 1000, f'{n:032x}')['candidate_uci'] == 'e7e5'
            and .1 < host.boss_play.boss_opponent.gate(board, [], f'{n:032x}')['draw'] < .18)
        self.prepare()
        self.peer.scripts[1:2] = [
            {'move': 'e2e4', 'packets': [packet(chess.Board(), 'e2e4', 100)]},
            {'move': 'd2d4', 'packets': [packet(chess.Board(), 'd2d4', -100)]}]
        result = self.reply()
        row = self.game._boss_decisions[2]
        self.assertEqual(result['last_move'], 'e7e5')
        self.assertEqual(row['opponent_observation']['loss_cp'], 200)
        self.assertTrue(row['frequency']['attempt_trap'])
        self.assertGreater(row['frequency']['probability'], .1)
        self.assertEqual(self.peer.searches[0]['limit'].time, 1)
        self.assertEqual(self.peer.searches[1]['board'].move_stack, [])
        self.assertEqual(self.peer.searches[2]['board'].move_stack, [])
        self.assertEqual(self.peer.searches[2]['root_moves'][0].uci(), 'd2d4')
        self.game.undo(self.game.revision)
        self.assertEqual(self.game.state()['engine']['boss_opponent']['quality_observations'], 0)

    def test_undo_removes_decisions_and_replays_same_cached_choice(self):
        self.prepare()
        self.reply()
        self.game.undo(self.game.revision)
        self.assertEqual(self.game._boss_decisions, {})
        self.assertEqual(self.game.state()['engine']['boss_phase'], 'opening')
        self.game.human_move('d2d4', self.game.revision)
        result = self.reply()
        self.assertEqual(result['last_move'], 'e7e5')
        self.assertEqual(len(self.peer.searches), 3)
        self.assertTrue(self.game._boss_decisions[2]['reused'])

    def test_handicap_cancel_restores_metadata_and_forced_move_disables_repertoire(self):
        self.prepare()
        self.reply()
        saved = copy.deepcopy(self.game._boss_decisions)
        self.game.begin_handicap(2, self.game.revision)
        self.assertEqual(self.game._boss_decisions, {})
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.game._boss_decisions, saved)
        self.game.begin_handicap(2, self.game.revision)
        self.game.forced_move('d7d5', self.game.revision)
        self.game.human_move('c2c4', self.game.revision)
        self.peer.scripts = [{'move': 'e7e6', 'packets': [packet(self.game.board, 'e7e6')]}]
        result = self.reply()
        self.assertEqual(result['last_move'], 'e7e6')
        self.assertEqual(result['engine']['boss_phase'], 'full_strength')
        self.assertEqual(len(self.peer.searches), 4)

    def test_speculative_opening_accepts_loss_but_failed_probe_keeps_baseline(self):
        for cp, error, expected in [(-600, None, 'e7e5'), (-150, TimeoutError('controlled timeout'), 'g8f6')]:
            with self.subTest(cp=cp, error=error):
                self.game.new_game('white', 1000, self.game.revision, True)
                self.game._opening_seed = admitted_seed(after('d2d4'), 'e7e5')
                self.prepare(cp=cp, failure=error)
                result = self.reply()
                self.assertEqual(result['last_move'], expected)
                self.assertIsNone(result['error'])
                self.assertEqual(self.game._boss_decisions[2]['line_id'], 'englund' if expected == 'e7e5' else None)

    def test_short_budget_no_probe_and_new_game_defaults_to_normal(self):
        self.game.new_game('white', 100, self.game.revision, True)
        self.game.human_move('d2d4', self.game.revision)
        self.peer.scripts = [{'move': 'g8f6', 'packets': [packet(self.game.board, 'g8f6')]}]
        result = self.reply()
        self.assertEqual(result['last_move'], 'g8f6')
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.peer.searches[0]['limit'].time, .1)
        result = self.game.new_game('white', 1000, self.game.revision)
        self.assertFalse(result['engine']['boss_mode'])
        self.assertTrue(result['engine']['dynamic_play_enabled'])
        self.assertEqual(self.game._boss_decisions, {})
        self.assertEqual(self.game._boss_memo, {})
        self.assertNotIn('[BossMode', self.game.pgn())

    def test_mode_requires_actual_boolean_without_replacing_game(self):
        original = self.game.game_id
        for value in (1, 0, 'true', None, []):
            with self.assertRaises(ValueError):
                self.game.new_game('white', 1000, self.game.revision, value)
            self.assertEqual(self.game.game_id, original)

    def test_settings_change_during_probe_preserves_captured_budget(self):
        from hint_state_tests import wait_for
        gate = threading.Event()
        before = self.prepare(gate=gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.reply)
            try:
                wait_for(lambda: len(self.peer.searches) == 3)
                self.game.set_think_time(2000, self.game.game_id, before['engine']['settings_revision'])
                evaluation = self.game.evaluation()
                self.assertEqual(evaluation['cp'], -25)
            finally:
                gate.set()
            result = future.result(timeout=3)
        self.assertEqual(result['last_move'], 'e7e5')
        self.assertEqual(self.game._boss_decisions[2]['baseline_budget_ms'], 1000)
        self.assertEqual(self.game._engine_movetimes[2], 1000)
        self.assertEqual(result['engine']['movetime_ms'], 2000)

    def test_stale_search_does_not_append_move_or_boss_receipt(self):
        from hint_state_tests import wait_for
        gate = threading.Event()
        before = self.prepare(gate=gate)
        record_path = self.game.save_dir / f'{self.game.game_id}.json'
        saved_record = record_path.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.reply)
            try:
                wait_for(lambda: len(self.peer.searches) == 3)
                with self.game._lock:
                    self.game._game_token = object()
            finally:
                gate.set()
            result = future.result(timeout=3)
        self.assertIsNone(result['error'], 'obsolete search does not attach an error to the current state')
        self.assertEqual(result['revision'], before['revision'])
        self.assertEqual([move.uci() for move in self.game.board.move_stack], ['d2d4'])
        self.assertEqual(self.game._boss_decisions, {})
        self.assertEqual(self.game._boss_memo, {})
        self.assertEqual(record_path.read_bytes(), saved_record)


class BossHTTPTests(unittest.TestCase):
    setUp = base.HumanHTTPTests.setUp
    stop_server = base.HumanHTTPTests.stop_server
    request = base.HumanHTTPTests.request

    def test_new_game_endpoint_preserves_explicit_mode_and_rejects_nonboolean(self):
        code, _, body = self.request('/api/new', {'color': 'white', 'movetime_ms': 1000,
                                    'revision': self.game.revision, 'boss_mode': True})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['engine']['boss_mode'])
        game_id = self.game.game_id
        code, _, _ = self.request('/api/new', {'color': 'white', 'movetime_ms': 1000,
                                   'revision': self.game.revision, 'boss_mode': 'true'})
        self.assertEqual(code, 400)
        self.assertEqual(self.game.game_id, game_id)
        code, _, body = self.request('/api/new', {'color': 'white', 'movetime_ms': 1000,
                                    'revision': self.game.revision})
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)['engine']['boss_mode'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
