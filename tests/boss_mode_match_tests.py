"""Two real application hosts, controlled engines, no native process launch."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import boss_mode_match as match
from boss_strength_match_tests import Peer


class RepeatingPeer(Peer):
    def play(self, board, limit, **kwargs):
        move = chess.Move.from_uci(('g1f3', 'g8f6', 'f3g1', 'f6g8')[len(board.move_stack) % 4])
        self.calls.append({'board': board.copy(stack=True), 'limit': limit, **kwargs})
        return chess.engine.PlayResult(move, None, {'depth': 2,
            'score': chess.engine.PovScore(chess.engine.Cp(9000), board.turn), 'pv': [move]})


class ModeMatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='regicide-mode-match-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / 'controlled.exe'
        self.binary.write_bytes(b'controlled peer; never executed')
        self.config = {'binary': str(self.binary), 'pairs': 1, 'pair_start': 1,
                       'boss_ms': 1000, 'normal_ms': 1000, 'max_plies': 300,
                       'seed': 'mode-match-tests', 'output_dir': str(self.root / 'output')}
        self.peers = {'boss': [], 'normal': []}
        self.hosts = []

    def run_match(self, *, illegal=False, repeating=False, broken_relay=False):
        owner = self
        class ObservedGame(match.HumanGame):
            def __init__(self, *args, **kwargs):
                self.engine_calls = self.human_calls = 0
                super().__init__(*args, **kwargs)
                owner.hosts.append(self)

            def engine_move(self, revision):
                self.engine_calls += 1
                return super().engine_move(revision)

            def human_move(self, uci, revision):
                self.human_calls += 1
                state = super().human_move(uci, revision)
                if broken_relay:
                    self.board.pop()
                return state

        def factory(mode):
            def create(path):
                peer = RepeatingPeer() if repeating else Peer(illegal=illegal and mode == 'normal')
                owner.peers[mode].append(peer)
                return peer
            return create
        with patch.object(match, 'HumanGame', ObservedGame), contextlib.redirect_stdout(io.StringIO()):
            return match.run_match(self.config, boss_factory=factory('boss'), normal_factory=factory('normal'))

    def record(self, row):
        return json.loads((self.root / 'output' / row['record_file']).read_text(encoding='utf-8'))

    def test_schedule_shards_equal_full_schedule_and_mode_seeds_are_paired(self):
        rows = match.schedule(8, 1, 'master')
        shards = sum((match.schedule(2, start, 'master') for start in (1, 3, 5, 7)), [])
        self.assertEqual(rows, shards)
        self.assertEqual([row['game'] for row in rows], list(range(1, 17)))
        for a, b in zip(rows[::2], rows[1::2]):
            self.assertEqual((a['boss_color'], b['boss_color']), ('white', 'black'))
            self.assertEqual((a['normal_color'], b['normal_color']), ('black', 'white'))
            self.assertEqual(a['boss_seed'], b['boss_seed'])
            self.assertEqual(a['normal_seed'], b['normal_seed'])
            self.assertNotEqual(a['boss_seed'], a['normal_seed'])
        self.assertEqual(len({row['boss_seed'] for row in rows}), 8)
        for values in ((0, 1), (1, 0), (True, 1)):
            with self.assertRaises(ValueError):
                match.schedule(*values, 'master')

    def test_two_real_hosts_use_app_modes_and_preserve_full_history_receipts(self):
        report = self.run_match()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual((report['summary']['wins'], report['summary']['losses']), (1, 1))
        self.assertTrue(report['sources_unchanged'] and report['catalog_unchanged'] and report['binary_unchanged'])
        self.assertIn('scripts/dynamic_play.py', report['source_sha256'])
        self.assertIn('data/openings/a.tsv', report['catalog_sha256'])
        for row in report['games']:
            record = self.record(row)
            self.assertEqual((row['termination'], row['plies'], row['result']), ('checkmate', 4, '0-1'))
            self.assertEqual(record['uci_moves'], ['f2f3', 'e7e5', 'g2g4', 'd8h4'])
            for mode, host in record['hosts'].items():
                self.assertEqual(host['uci_moves'], record['uci_moves'])
                self.assertEqual(host['final_fen'], record['final_fen'])
                self.assertEqual(host['boss_play']['enabled'], mode == 'boss')
                self.assertEqual(host['dynamic_play']['enabled'], mode == 'normal')
                self.assertEqual(host['boss_play']['seed'], row[mode + '_seed'])
                decisions = host['boss_play' if mode == 'boss' else 'dynamic_play']['decisions']
                self.assertEqual(len(decisions), 2)
                sidecar = self.root / 'output' / host['save_directory'] / (host['game_id'] + '.json')
                saved = json.loads(sidecar.read_text(encoding='utf-8'))
                self.assertEqual(saved['uci_moves'], record['uci_moves'])
                self.assertEqual(saved['boss_play']['enabled'], mode == 'boss')
                self.assertIn('Human practice', saved['purpose'])
            pgn = chess.pgn.read_game(io.StringIO((self.root / 'output' / row['pgn_file']).read_text()))
            self.assertEqual(pgn.end().board().outcome().result(), row['result'])
            self.assertEqual(pgn.headers['BossSeed'], row['boss_seed'])
            self.assertEqual(pgn.headers['NormalSeed'], row['normal_seed'])
            self.assertIn('Boss', pgn.headers[row['boss_color'].title()])
            trace = [json.loads(line) for line in (self.root / 'output' / f"game-{row['game']:03d}.moves.jsonl").read_text().splitlines()]
            self.assertEqual(trace, record['moves'])
            for move in record['moves']:
                self.assertEqual(move['baseline_budget_ms'], 1000)
                self.assertGreaterEqual(move['advertised_extra_budget_ms'], 0)
                self.assertGreaterEqual(move['elapsed_ms'], 0)
                self.assertGreaterEqual(move['relay_elapsed_ms'], 0)
                self.assertIsNotNone(move['boss_decision' if move['actor'] == 'boss' else 'dynamic_decision'])
        for host in self.hosts:
            self.assertTrue(host.dynamic_play_enabled and host.opening_variety_enabled)
            self.assertEqual((host.engine_calls, host.human_calls), (2, 2))
        for peer in self.peers['boss'] + self.peers['normal']:
            self.assertTrue(peer.closed)
            self.assertEqual(peer.configured, {'Hash': 16, 'Threads': 1, 'UsePieceActivity': True})
            for call in peer.calls:
                board = chess.Board()
                for move in call['board'].move_stack:
                    self.assertIn(move, board.legal_moves)
                    board.push(move)
                self.assertEqual(board.fen(), call['board'].fen())

    def test_claimable_repetition_is_legal_draw_not_evaluation_adjudication(self):
        report = self.run_match(repeating=True)
        self.assertEqual(report['summary']['draws'], 2)
        for row in report['games']:
            self.assertEqual(row['termination'], 'threefold_repetition')
            self.assertTrue(row['legal_result'])
            self.assertFalse(row['capped'])

    def test_cap_is_incomplete_and_huge_engine_score_does_not_adjudicate(self):
        self.config['max_plies'] = 2
        report = self.run_match()
        self.assertEqual(report['summary']['capped'], 2)
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertIsNone(report['summary']['score'])
        for row in report['games']:
            self.assertEqual(row['result'], '*')
            self.assertFalse(row['completed'] or row['legal_result'])

    def test_illegal_move_failure_stops_shard_without_scoring_or_leaking_peers(self):
        report = self.run_match(illegal=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(report['games']), 1)
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertEqual(report['games'][0]['result'], '*')
        self.assertEqual(report['games'][0]['termination'], 'engine_error')
        self.assertTrue(all(peer.closed for group in self.peers.values() for peer in group))

    def test_relay_history_divergence_is_detected(self):
        report = self.run_match(broken_relay=True)
        row = report['games'][0]
        self.assertIn('diverged after receiving', row['error'])
        self.assertEqual(row['result'], '*')
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertEqual(self.record(row)['failed_turn']['actor'], 'boss')
        self.assertTrue(all(peer.closed for group in self.peers.values() for peer in group))

    def test_export_failure_still_closes_both_hosts_and_is_not_a_win(self):
        with patch.object(match, 'match_pgn', side_effect=ValueError('controlled export failure')):
            report = self.run_match()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertIn('controlled export failure', report['games'][0]['error'])
        self.assertTrue(all(peer.closed for group in self.peers.values() for peer in group))
        self.assertTrue((self.root / 'output' / 'game-001.pgn').is_file())

    def test_existing_output_and_invalid_time_are_rejected_before_native_work(self):
        output = self.root / 'output'
        output.mkdir()
        marker = output / 'keep.txt'
        marker.write_text('preserved')
        with self.assertRaises(FileExistsError):
            self.run_match()
        self.assertEqual(marker.read_text(), 'preserved')
        self.assertFalse(self.peers['boss'] or self.peers['normal'])
        self.config.update(output_dir=str(self.root / 'another'), normal_ms=99)
        with self.assertRaises(ValueError):
            self.run_match()
        self.assertFalse((self.root / 'another').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
