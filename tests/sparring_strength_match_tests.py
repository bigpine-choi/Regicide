"""Production HumanGame orchestration with deterministic non-native peers."""
import contextlib
import copy
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sparring_strength_match as match
from boss_strength_match_tests import Peer


class SparringMatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='regicide-sparring-match-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.own, self.sf = self.root / 'regicide.exe', self.root / 'stockfish.exe'
        self.own.write_bytes(b'fake peer; never executed')
        self.sf.write_bytes(b'fake stockfish peer; never executed')
        self.config = {'binary': str(self.own), 'stockfish': str(self.sf), 'ratings': [1320],
                       'colors': ['white', 'black'], 'sparring_ms': 1000, 'stockfish_ms': 1000,
                       'max_plies': 220, 'seed': 'controlled-pair', 'output_dir': str(self.root / 'output')}
        self.own_peers, self.sf_peers = [], []

    def run_match(self, *, illegal=False):
        def start_own(path):
            peer = Peer()
            self.own_peers.append(peer)
            return peer
        def start_sf(path):
            peer = Peer(stockfish=True, illegal=illegal)
            self.sf_peers.append(peer)
            return peer
        with contextlib.redirect_stdout(io.StringIO()):
            return match.run_match(self.config, sparring_factory=start_own, stockfish_factory=start_sf)

    def load(self, entry):
        directory = self.root / 'output'
        return (json.loads((directory / entry['record_file']).read_text(encoding='utf-8')),
                (directory / entry['pgn_file']).read_text(encoding='utf-8'))

    def test_shards_preserve_global_color_pairs_and_predeclared_seeds(self):
        full = match.schedule([1320, 1700, 2100, 2500], 'seed')
        self.assertEqual([row['game'] for row in full], list(range(1, 9)))
        self.assertEqual(full, match.schedule([1320, 1700], 'seed') + match.schedule([2100, 2500], 'seed'))
        for first, second in zip(full[::2], full[1::2]):
            self.assertEqual(first['sparring_seed'], second['sparring_seed'])
            self.assertEqual((first['sparring_color'], second['sparring_color']), ('white', 'black'))
        self.assertEqual(match.schedule([2100], 'seed', ['black'])[0], full[5])
        for ratings in ([], [1320, 1320], [1200]):
            with self.assertRaises(ValueError):
                match.schedule(ratings, 'seed')

    def test_real_host_path_records_sparring_only_mate_options_and_legal_pgn(self):
        report = self.run_match()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['policy'], 'regicide-sparring-play-v7')
        self.assertEqual(report['protocol']['target_fullmoves'], 15)
        self.assertIn('30-ply reach', report['protocol']['measurements'])
        self.assertEqual(report['summary']['wins'], 1)
        self.assertEqual(report['summary']['losses'], 1)
        self.assertTrue(report['sources_unchanged'] and report['binaries_unchanged'] and report['catalog_unchanged'])
        for entry in report['games']:
            record, pgn = self.load(entry)
            self.assertEqual(record['termination'], 'checkmate')
            self.assertEqual(record['result'], '0-1')
            self.assertEqual(record['uci_moves'], ['f2f3', 'e7e5', 'g2g4', 'd8h4'])
            self.assertTrue(record['host']['sparring_play']['enabled'])
            self.assertTrue(all(record['host'][mode]['enabled'] is False for mode in ('boss_play', 'dynamic_play', 'opening_variety')))
            self.assertEqual(len(record['host']['sparring_play']['decisions']), 2)
            self.assertTrue(match.validate_record(record, pgn)['passed'])
            game = chess.pgn.read_game(io.StringIO(pgn))
            self.assertEqual(game.headers['SparringMode'], '1')
            self.assertEqual(game.end().board().outcome().result(), game.headers['Result'])
            sidecar = self.root / 'output' / record['host']['sidecar_file']
            self.assertEqual(match.sha(sidecar), record['host']['sidecar_sha256'])
            for row in record['moves']:
                self.assertEqual(row['baseline_budget_ms'], 1000)
                if row['actor'] == 'sparring':
                    self.assertEqual(row['advertised_extra_budget_ms'], 2000)
                    self.assertEqual(row['sparring_decision']['selected_uci'], row['uci'])
                else:
                    self.assertEqual(row['stockfish_score_packet']['source'], 'play_merged_info')
                    self.assertFalse(row['stockfish_score_packet']['is_selected_move_score'])
                    self.assertTrue(row['stockfish_score_packet']['pv_legal'])
        for peer in self.sf_peers:
            self.assertEqual(peer.configured['UCI_Elo'], 1320)
            self.assertTrue(peer.configured['UCI_LimitStrength'])
            self.assertEqual(peer.configured['Threads'], 1)
            self.assertEqual(peer.configured['Hash'], 16)
            self.assertTrue(all(call['info'] == chess.engine.INFO_ALL and call['ponder'] is False for call in peer.calls))
        self.assertTrue(all(peer.closed for peer in self.own_peers + self.sf_peers))

    def test_caps_remain_unfinished_despite_large_scores(self):
        self.config['max_plies'] = 2
        report = self.run_match()
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertEqual(report['summary']['capped'], 2)
        for entry in report['games']:
            record, pgn = self.load(entry)
            self.assertEqual(record['result'], '*')
            self.assertFalse(record['completed'] or record['legal_result'])
            self.assertTrue(match.validate_record(record, pgn)['passed'])

    def test_record_receipt_mode_and_pgn_disagreements_are_detected(self):
        report = self.run_match()
        record, pgn = self.load(report['games'][0])
        changes = []
        value = copy.deepcopy(record)
        value['moves'][0]['sparring_decision']['selected_uci'] = 'a2a3'
        changes.append(value)
        value = copy.deepcopy(record)
        value['host']['boss_play']['enabled'] = True
        changes.append(value)
        value = copy.deepcopy(record)
        value['uci_moves'][0] = 'a2a3'
        changes.append(value)
        for bad in changes:
            with self.assertRaises(ValueError):
                match.validate_record(bad, pgn)
        with self.assertRaises(ValueError):
            match.validate_record(record, pgn.replace('[Result "0-1"]', '[Result "1-0"]'))

    def test_illegal_opponent_move_and_export_failure_never_count_as_a_win(self):
        report = self.run_match(illegal=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertEqual(report['games'][0]['result'], '*')
        self.assertTrue(all(peer.closed for peer in self.own_peers + self.sf_peers))
        self.config['output_dir'] = str(self.root / 'export-failure')
        with patch.object(match, 'match_pgn', side_effect=ValueError('controlled export failure')):
            report = self.run_match()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['summary']['scored_games'], 0)
        self.assertIn('controlled export failure', report['games'][0]['error'])

    def test_draw_claims_and_terminal_precedence_over_cap(self):
        board = chess.Board()
        for uci in ['g1f3', 'g8f6', 'f3g1', 'f6g8'] * 2:
            board.push_uci(uci)
        outcome = match.end_state(board, 8)
        self.assertEqual(outcome['termination'], 'threefold_repetition')
        self.assertTrue(outcome['legal_result'])
        self.assertFalse(outcome['capped'])
        board = chess.Board('8/8/8/8/3k4/8/8/KR6 w - - 99 50')
        self.assertEqual(match.end_state(board, 220)['termination'], 'fifty_moves')

    def test_stockfish_root_score_is_separate_from_weakened_move_and_has_white_pov(self):
        board = chess.Board()
        board.push_san('e4')
        info = {'depth': 10, 'score': chess.engine.PovScore(chess.engine.Cp(45), chess.BLACK),
                'lowerbound': True, 'pv': [chess.Move.from_uci('e7e5')]}
        row = match.stockfish_score_packet(board, info, chess.Move.from_uci('c7c5'), {'UCI_Elo': 1320})
        self.assertEqual(row['cp_white'], -45)
        self.assertEqual(row['bound'], 'upper')
        self.assertTrue(row['score_pov_valid'] and row['pv_legal'])
        self.assertFalse(row['selected_matches_pv'] or row['is_selected_move_score'])
        self.assertEqual(row['iteration_identity'], 'not_guaranteed_merged_play_info')
        wrong = dict(info, score=chess.engine.PovScore(chess.engine.Cp(45), chess.WHITE))
        self.assertIsNone(match.stockfish_score_packet(board, wrong, chess.Move.from_uci('c7c5'), {})['cp_white'])

    def test_existing_results_and_unexpected_runtime_are_rejected_before_play(self):
        out = self.root / 'output'
        out.mkdir()
        sentinel = out / 'keep.txt'
        sentinel.write_text('preserve', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.run_match()
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserve')
        self.config['output_dir'] = str(self.root / 'unexpected')
        self.config['expected_binary_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.run_match()
        self.assertEqual(self.own_peers + self.sf_peers, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
