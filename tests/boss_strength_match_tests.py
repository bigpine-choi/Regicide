"""Actual HumanGame match orchestration with non-native deterministic peers."""
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
import boss_strength_match as match


class Peer:
    def __init__(self, *, stockfish=False, illegal=False):
        self.id = {'name': 'Stockfish controlled peer' if stockfish else 'Regicide controlled peer'}
        self.options = {
            'Hash': chess.engine.Option('Hash', 'spin', 16, 1, 1024, None),
            'Threads': chess.engine.Option('Threads', 'spin', 1, 1, 16, None),
            'UsePieceActivity': chess.engine.Option('UsePieceActivity', 'check', False, None, None, None)}
        if stockfish:
            self.options.update({
                'UCI_LimitStrength': chess.engine.Option('UCI_LimitStrength', 'check', False, None, None, None),
                'UCI_Elo': chess.engine.Option('UCI_Elo', 'spin', 1320, 1320, 3190, None)})
        self.configured, self.calls, self.closed, self.illegal = {}, [], False, illegal

    def configure(self, options):
        self.configured.update(options)

    def play(self, board, limit, **kwargs):
        self.calls.append({'board': board.copy(stack=True), 'limit': limit, **kwargs})
        if self.illegal:
            return chess.engine.PlayResult(chess.Move.from_uci('e7e4'), None, {})
        line = ['f2f3', 'e7e5', 'g2g4', 'd8h4']
        candidate = chess.Move.from_uci(line[len(board.move_stack)]) if len(board.move_stack) < 4 else next(iter(board.legal_moves))
        move = candidate if candidate in board.legal_moves else next(iter(board.legal_moves))
        # Extreme scores must not declare any game won before its legal end.
        return chess.engine.PlayResult(move, None, {'depth': 2, 'score': chess.engine.PovScore(chess.engine.Cp(9000), board.turn), 'pv': [move]})

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='regicide-boss-strength-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.boss = self.root / 'boss.exe'
        self.sf = self.root / 'stockfish.exe'
        self.boss.write_bytes(b'not executed: controlled boss peer')
        self.sf.write_bytes(b'not executed: controlled opponent peer')
        self.config = {'boss_binary': str(self.boss), 'stockfish': str(self.sf), 'stockfish_elo': 1320,
                       'boss_ms': 1000, 'stockfish_ms': 2000, 'threads': 1, 'hash_mb': 16,
                       'games': 2, 'max_plies': 300, 'cap_policy': 'incomplete', 'seed': 'controlled-pair',
                       'output_dir': str(self.root / 'output')}
        self.boss_peers, self.sf_peers = [], []

    def run_match(self, *, illegal=False):
        def boss_factory(path):
            value = Peer()
            self.boss_peers.append(value)
            return value
        def sf_factory(path):
            value = Peer(stockfish=True, illegal=illegal)
            self.sf_peers.append(value)
            return value
        with contextlib.redirect_stdout(io.StringIO()):
            return match.run_match(self.config, boss_factory=boss_factory, stockfish_factory=sf_factory,
                                   evaluation={'model': None, 'generation': None, 'warning': None})

    def test_schedule_alternates_colors_and_pairs_only_predeclared_seed(self):
        rows = match.schedule(4, 'master', 1320)
        self.assertEqual([row['boss_color'] for row in rows], ['white', 'black', 'white', 'black'])
        self.assertEqual(rows[0]['boss_seed'], rows[1]['boss_seed'])
        self.assertNotEqual(rows[0]['boss_seed'], rows[2]['boss_seed'])
        self.assertEqual(rows, match.schedule(4, 'master', 1320))
        self.assertNotEqual(rows[0]['boss_seed'], match.schedule(4, 'master', 1700)[0]['boss_seed'])
        with self.assertRaises(ValueError):
            match.schedule(3, 'master', 1320)

    def test_actual_host_path_full_history_options_mate_and_pgn(self):
        report = self.run_match()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['summary']['wins'], 1)
        self.assertEqual(report['summary']['losses'], 1)
        self.assertTrue(report['sources_unchanged'])
        self.assertTrue(report['binaries_unchanged'])
        self.assertEqual(len(self.sf_peers), 2)
        for entry in report['games']:
            self.assertEqual(entry['termination'], 'checkmate')
            self.assertEqual(entry['plies'], 4)
            self.assertTrue(entry['legal_result'])
            record = json.loads((self.root / 'output' / entry['record_file']).read_text(encoding='utf-8'))
            self.assertTrue(record['boss_play']['enabled'])
            self.assertEqual(record['boss_play']['seed'], entry['boss_seed'])
            self.assertEqual(len(record['boss_play']['decisions']), 2)
            self.assertEqual(record['uci_moves'], ['f2f3', 'e7e5', 'g2g4', 'd8h4'])
            pgn = chess.pgn.read_game(io.StringIO((self.root / 'output' / entry['pgn_file']).read_text(encoding='utf-8')))
            self.assertEqual(pgn.headers['Result'], '0-1')
            self.assertEqual(pgn.end().board().outcome().result(), '0-1')
            self.assertIn('Boss', pgn.headers['White' if entry['boss_color'] == 'white' else 'Black'])
            for row in record['moves']:
                if row['actor'] == 'boss':
                    self.assertEqual(row['boss_decision']['baseline_budget_ms'], 1000)
        for engine in self.sf_peers:
            self.assertEqual(engine.configured['UCI_Elo'], 1320)
            self.assertTrue(engine.configured['UCI_LimitStrength'])
            self.assertEqual(engine.configured['Threads'], 1)
            self.assertEqual(engine.configured['Hash'], 16)
            self.assertTrue(all(call['limit'].time == 2 for call in engine.calls))
            self.assertTrue(all(call['ponder'] is False for call in engine.calls))
            self.assertTrue(engine.closed)
        for engine in self.boss_peers:
            self.assertTrue(engine.configured['UsePieceActivity'])
            self.assertTrue(engine.closed)
            for call in engine.calls:
                board = chess.Board()
                for move in call['board'].move_stack:
                    self.assertIn(move, board.legal_moves)
                    board.push(move)
                self.assertEqual(board.fen(), call['board'].fen())

    def test_ply_caps_are_incomplete_despite_huge_engine_scores(self):
        self.config['max_plies'] = 2
        report = self.run_match()
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertEqual(report['summary']['capped'], 2)
        self.assertIsNone(report['legal_results_only']['score'])
        for row in report['games']:
            self.assertEqual(row['result'], '*')
            self.assertEqual(row['termination'], 'ply_cap')
            self.assertFalse(row['completed'])
            self.assertFalse(row['legal_result'])

    def test_explicit_cap_draw_is_separate_and_never_changes_production_sidecar(self):
        self.config.update(max_plies=2, cap_policy='draw')
        report = self.run_match()
        self.assertEqual(report['summary']['draws'], 2)
        self.assertEqual(report['legal_results_only']['scored_games'], 0)
        for entry in report['games']:
            sidecar = self.root / 'output' / 'host' / f"game-{entry['game']:03d}" / (entry['host_game_id'] + '.json')
            saved = json.loads(sidecar.read_text(encoding='utf-8'))
            self.assertIn('Human practice', saved['purpose'])
            self.assertEqual(saved['state']['result'], '*')

    def test_illegal_opponent_move_is_failure_not_boss_win(self):
        report = self.run_match(illegal=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(report['games']), 1)
        self.assertEqual(report['games'][0]['termination'], 'engine_error')
        self.assertEqual(report['games'][0]['result'], '*')
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertTrue(all(peer.closed for peer in self.boss_peers + self.sf_peers))

    def test_elo_outside_advertised_range_fails_before_playing(self):
        self.config['stockfish_elo'] = 1200
        report = self.run_match()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['games'][0]['plies'], 0)
        self.assertEqual(len(self.boss_peers), 0)
        self.assertEqual(report['summary']['failed'], 1)

    def test_export_failure_still_closes_owned_engines_and_preserves_error(self):
        with patch.object(match, 'match_pgn', side_effect=ValueError('controlled export failure')):
            report = self.run_match()
        self.assertEqual(report['status'], 'failed')
        self.assertIn('controlled export failure', report['games'][0]['error'])
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertTrue(all(peer.closed for peer in self.boss_peers + self.sf_peers))
        self.assertTrue((self.root / 'output' / 'game-001.json').is_file())

    def test_existing_run_directory_is_not_overwritten(self):
        output = self.root / 'output'
        output.mkdir()
        sentinel = output / 'keep.txt'
        sentinel.write_text('existing result', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.run_match()
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'existing result')

    def test_legal_terminal_outcome_takes_precedence_over_ply_cap(self):
        board = chess.Board()
        for move in ['f2f3', 'e7e5', 'g2g4', 'd8h4']:
            board.push_uci(move)
        result = match.end_state(board, 4)
        self.assertEqual(result['termination'], 'checkmate')
        self.assertTrue(result['legal_result'])
        self.assertFalse(result['capped'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
