"""Application isolation and lifecycle checks using controlled UCI peers only."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import chess
import chess.engine

import human_play_tests as base
from opening_variety_tests import Peer

host = base.play_human
policy = host.dynamic_play
MIDDLE = 'e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7 f1e1 b7b5 a4b3 d7d6 c2c3 e8g8'.split()


def packet(board, uci, cp=40, depth=4, *, mate=None):
    replay = board.copy(stack=True)
    move = chess.Move.from_uci(uci)
    pv = [move]
    replay.push(move)
    while len(pv) < depth and not replay.is_game_over(claim_draw=True):
        move = sorted(replay.legal_moves, key=lambda item: item.uci())[0]
        pv.append(move)
        replay.push(move)
    score = chess.engine.Cp(cp) if mate is None else chess.engine.Mate(mate)
    return {'depth': depth, 'score': chess.engine.PovScore(score, board.turn), 'pv': pv}


class DynamicPeer(Peer):
    def __init__(self):
        super().__init__()
        self.baseline_move = 'h2h3'
        self.baseline_cp = 40
        self.candidate_cp = 30
        self.candidate_depth = 4
        self.probe_gate = None
        self.fail_probe_number = None
        self.mate = None
        self.probes = 0

    def analysis(self, board, limit, *, game=None, info=None, root_moves=None):
        if root_moves:
            self.probes += 1
            uci = root_moves[0].uci()
            script = {'move': uci, 'packets': [packet(board, uci, self.candidate_cp, self.candidate_depth)]}
            if self.probes == 1 and self.probe_gate is not None:
                script['gate'] = self.probe_gate
            if self.probes == self.fail_probe_number:
                script['failure'] = TimeoutError('Controlled candidate failure')
        else:
            uci = self.baseline_move
            script = {'move': uci, 'packets': [packet(board, uci, self.baseline_cp, mate=self.mate)]}
        self.scripts.append(script)
        return super().analysis(board, limit, game=game, info=info, root_moves=root_moves)


class DynamicApplicationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='regicide-dynamic-app-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.binary = self.root / 'never-run.exe'
        self.binary.write_bytes(b'Controlled test peer; never executed')
        self.peer = DynamicPeer()
        self.game = host.HumanGame(self.binary, self.root / 'games', lambda _: self.peer,
                                  hint_engine_factory=base.FakeFactory(),
                                  evaluation_engine_factory=base.FakeFactory(),
                                  dynamic_play_enabled=True)
        self.addCleanup(self.game.close)
        self.game.new_game('black', 1000, self.game.revision)
        self.game._opening_seed = '00' * 16
        self.at_middle()

    def at_middle(self):
        with self.game._lock:
            self.game.board = chess.Board()
            for move in MIDDLE:
                self.game.board.push_uci(move)
            self.game.revision += 1
            self.game._sync_evaluation()
            self.game._save()

    def play(self):
        return self.game.engine_move(self.game.revision)

    def test_middle_has_full_baseline_then_bounded_exact_depth_probes(self):
        before = self.game.state()
        self.assertTrue(before['engine']['dynamic_play_enabled'])
        self.assertFalse(before['engine']['opening_variety_enabled'])
        self.assertEqual(before['engine']['extra_budget_ms'], 500)
        after = self.play()
        self.assertIsNone(after['error'])
        self.assertEqual(self.peer.searches[0]['limit'].time, 1)
        self.assertIsNone(self.peer.searches[0]['root_moves'])
        self.assertIsNone(self.peer.searches[0]['limit'].depth)
        self.assertGreater(len(self.peer.searches), 1)
        self.assertLessEqual(len(self.peer.searches), 5)
        for call in self.peer.searches[1:]:
            self.assertEqual(call['limit'].depth, 4)
            self.assertLessEqual(call['limit'].time, .5 / 3)
            self.assertEqual([move.uci() for move in call['board'].move_stack], MIDDLE)
        decision = self.game._dynamic_decisions[17]
        self.assertEqual(decision['policy'], policy.POLICY)
        self.assertEqual(decision['baseline_budget_ms'], 1000)
        self.assertEqual(decision['extra_budget_ms'], 500)
        self.assertEqual(decision['selected_uci'], after['last_move'])
        self.assertEqual(self.game._opening_decisions, {})
        self.assertEqual(self.game._engine_movetimes[17], 1000)

    def test_restricted_probe_score_does_not_replace_position_evaluation(self):
        gate = threading.Event()
        self.peer.probe_gate = gate
        self.peer.candidate_cp = 999
        before = self.game.state()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.play)
            try:
                from hint_state_tests import wait_for
                wait_for(lambda: len(self.peer.searches) == 2)
                value = self.game.evaluation()
                self.assertEqual((value['cp'], value['depth']), (40, 4))
                self.assertEqual(value['source_fen'], before['fen'])
                self.assertFalse(future.done())
            finally:
                gate.set()
            after = future.result(timeout=2)
        self.assertEqual(after['last_move'], self.peer.baseline_move)

    def test_failed_or_incomplete_probe_preserves_baseline(self):
        for failure in ('exception', 'depth', 'loss'):
            with self.subTest(failure=failure):
                self.game.new_game('black', 1000, self.game.revision)
                self.at_middle()
                self.peer.closed, self.peer.probes = False, 0
                self.peer.fail_probe_number = 2 if failure == 'exception' else None
                self.peer.candidate_depth = 3 if failure == 'depth' else 4
                self.peer.candidate_cp = 24 if failure == 'loss' else 30
                after = self.play()
                self.assertIsNone(after['error'])
                self.assertEqual(after['last_move'], self.peer.baseline_move)
                self.assertEqual(self.game._dynamic_decisions[17]['selection_pool'], [self.peer.baseline_move])
                if failure == 'exception':
                    self.assertTrue(self.peer.closed)
                    self.assertIsNone(self.game._engine)

    def test_mate_keeps_primary_choice_without_spending_extra_time(self):
        self.peer.mate = 4
        after = self.play()
        self.assertEqual(after['last_move'], self.peer.baseline_move)
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.game._dynamic_decisions[17]['baseline_budget_ms'], 1000)

    def test_clear_conversion_does_not_spend_extra_time(self):
        self.peer.baseline_cp = 300
        after = self.play()
        self.assertEqual(after['last_move'], self.peer.baseline_move)
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.game._dynamic_decisions[17]['reason'], 'conversion_priority')

    def test_terminal_principal_variation_does_not_spend_extra_time(self):
        self.game.new_game('white', 1000, self.game.revision)
        with self.game._lock:
            # Delay the mating pattern without repetition so it belongs to the
            # middle-game style phase, not the preserved first sixteen plies.
            for uci in ('f2f3 e7e5 g2g4 b8c6 a2a3 a7a6 a3a4 b7b6 b2b3 b6b5 '
                        'c2c3 g7g6 c3c4 d7d6 b3b4 h7h6 h2h3').split():
                self.game.board.push_uci(uci)
            self.game.revision += 1
            self.game._sync_evaluation()
        self.peer.baseline_move = 'd8h4'
        after = self.play()
        self.assertEqual(after['last_move'], 'd8h4')
        self.assertEqual(after['result'], '0-1')
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.game._dynamic_decisions[18]['reason'], 'forcing_line_preserved')

    def test_opening_selection_and_25cp_gate_are_exactly_preserved(self):
        self.game.new_game('white', 1000, self.game.revision)
        self.game._opening_seed = '00' * 16
        self.game.human_move('e2e4', self.game.revision)
        board = self.game.board.copy(stack=True)
        self.peer.baseline_move = 'c7c5'
        self.peer.candidate_cp = 20  # 20cp loss passes the opening 25cp gate.
        after = self.play()
        actual = self.game._dynamic_decisions[2]
        opening = host.opening_variety
        expected = opening.receipt(board, opening.plan(board, 1000, self.game._opening_seed),
                                   chess.Move.from_uci('c7c5'), packet(board, 'c7c5'),
                                   actual['probes'], self.game._opening_seed)
        self.assertGreater(len(expected['selection_pool']), 1)
        self.assertEqual(after['last_move'], expected['selected_uci'])
        for key in ('selected_uci', 'selection_pool', 'selection_weights', 'reason'):
            self.assertEqual(actual[key], expected[key])
        self.assertEqual(actual['selection_rule'], 'opening_variety_preserved')

    def test_cancelled_probe_watchdog_keeps_legal_baseline(self):
        self.peer.probe_gate = threading.Event()
        self.peer.timeout = 10
        with ThreadPoolExecutor(max_workers=1) as pool:
            after = pool.submit(self.play).result(timeout=2)
        self.assertEqual(after['last_move'], self.peer.baseline_move)
        self.assertIsNone(after['error'])
        self.assertTrue(self.peer.closed)
        self.assertEqual(self.game._dynamic_decisions[17]['reason'], 'candidate_failed')

    def test_undo_reuses_receipt_with_same_history_and_budget(self):
        after = self.play()
        choice = after['last_move']
        count = len(self.peer.searches)
        seed = self.game._opening_seed
        self.game.undo(self.game.revision)
        self.assertEqual(self.game._dynamic_decisions, {})
        self.game.human_move(MIDDLE[-1], self.game.revision)
        replayed = self.play()
        self.assertEqual(replayed['last_move'], choice)
        self.assertEqual(len(self.peer.searches), count)
        self.assertTrue(self.game._dynamic_decisions[17]['reused'])
        self.assertEqual(self.game._opening_seed, seed)

    def test_setting_change_during_probe_keeps_captured_budget_and_updates_schedule(self):
        gate = threading.Event()
        self.peer.probe_gate = gate
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.play)
            try:
                from hint_state_tests import wait_for
                wait_for(lambda: len(self.peer.searches) == 2)
                changed = self.game.set_think_time(3000, self.game.game_id, self.game._settings_revision)
                self.assertEqual(changed['engine']['active_movetime_ms'], 1000)
                self.assertEqual(changed['engine']['active_extra_budget_ms'], 500)
                self.assertEqual(changed['engine']['extra_budget_ms'], 750)
                self.assertEqual(changed['engine']['extra_budget_schedule'],
                                 [{'until_ply': 16, 'budget_ms': 2000}, {'until_ply': 80, 'budget_ms': 750}])
            finally:
                gate.set()
            future.result(timeout=2)
        self.assertEqual(self.game._engine_movetimes[17], 1000)
        self.assertEqual(self.game._dynamic_decisions[17]['extra_budget_ms'], 500)

    def test_handicap_cancel_restores_and_forced_move_disables_style(self):
        self.play()
        original = copy.deepcopy(self.game._dynamic_decisions)
        memo = copy.deepcopy(self.game._dynamic_memo)
        self.game.begin_handicap(17, self.game.revision)
        self.assertEqual(self.game._dynamic_decisions, {})
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.game._dynamic_decisions, original)
        self.assertEqual(self.game._dynamic_memo, memo)
        self.game.begin_handicap(17, self.game.revision)
        self.game.forced_move('h2h3', self.game.revision)
        self.game.human_move('a6a5', self.game.revision)
        self.peer.baseline_move = 'a2a3'
        before = len(self.peer.searches)
        self.play()
        self.assertEqual(len(self.peer.searches), before + 1)
        self.assertEqual(self.game._dynamic_decisions[19]['reason'], 'user_forced_game')

    def test_fast_setting_uses_single_search_and_new_game_clears_history(self):
        old_seed = self.game._opening_seed
        self.game.new_game('black', 100, self.game.revision)
        self.assertNotEqual(self.game._opening_seed, old_seed)
        self.assertEqual(self.game._dynamic_decisions, {})
        self.assertEqual(self.game._dynamic_memo, {})
        self.peer.baseline_move = 'e2e4'
        after = self.play()
        self.assertEqual(after['last_move'], 'e2e4')
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.peer.searches[0]['limit'].time, .1)
        self.assertEqual(self.game.state()['engine']['extra_budget_ms'], 0)

    def test_records_and_archive_preserve_dynamic_identity_without_v3_mislabel(self):
        self.play()
        record = json.loads((self.game.save_dir / (self.game.game_id + '.json')).read_text(encoding='utf-8'))
        self.assertTrue(record['dynamic_play']['enabled'])
        self.assertFalse(record['opening_variety']['enabled'])
        self.assertEqual(record['opening_variety']['decisions'], [])
        self.assertEqual(record['dynamic_play']['decisions'][0]['selected_uci'], self.game.board.peek().uci())
        for pgn in (self.game.pgn(), host.GameArchive(self.game.save_dir).get_pgn(self.game.game_id)):
            parsed, _board = base.read_pgn(pgn)
            self.assertEqual(parsed.headers['DynamicPlayPolicy'], policy.POLICY)
            self.assertEqual(parsed.headers['DynamicPlaySeed'], self.game._opening_seed)
            self.assertNotIn('OpeningVarietyPolicy', parsed.headers)

    def test_production_entry_enables_style(self):
        args = ['play_human.py', '--binary', str(self.binary), '--save-dir', str(self.root / 'entry'), '--no-browser']
        with patch.object(sys, 'argv', args), patch.object(host, 'GameArchive'), \
             patch.object(host, 'runtime_evaluation', return_value={'warning': None, 'model': None, 'generation': None}), \
             patch.object(host, 'HumanGame', side_effect=RuntimeError('Do not start a server')) as constructor:
            with self.assertRaisesRegex(RuntimeError, 'Do not start a server'):
                host.main()
        self.assertIs(constructor.call_args.kwargs['dynamic_play_enabled'], True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
