"""Ephemeral PGN round trips, strict parsing and authenticated read-only HTTP."""
import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

import human_play_tests as baseline
import chess
import chess.pgn
from pgn_preview import (MAX_PGN_BYTES, MAX_PGN_PLIES, PGNPreviewError, preview_pgn)


def read(text):
    game = chess.pgn.read_game(io.StringIO(text.lstrip('\ufeff')))
    if game is None or game.errors:
        raise AssertionError('invalid test PGN')
    return game


def long_legal_pgn(plies=MAX_PGN_PLIES):
    """Deterministic legal fixture; neither an engine nor a strength example."""
    board, rng = chess.Board(), random.Random(20261021)
    for _ in range(plies):
        candidates = list(board.legal_moves)
        rng.shuffle(candidates)
        candidates.sort(key=lambda move: board.is_zeroing(move) != (board.halfmove_clock >= 125))
        for move in candidates:
            probe = board.copy(stack=True)
            probe.push(move)
            if not probe.is_game_over(claim_draw=False):
                board.push(move)
                break
        else:
            raise AssertionError('long fixture ended early')
    return str(chess.pgn.Game.from_board(board)) + '\n'


class PGNPreviewTests(unittest.TestCase):
    def assert_frames(self, text, details):
        game = read(text)
        board = game.board()
        moves = list(game.mainline_moves())
        self.assertEqual(details['pgn'], text)
        self.assertEqual(len(details['frames']), len(moves) + 1)
        self.assertEqual(details['summary']['ply_count'], len(moves))
        self.assertEqual(details['summary']['initial_fen'], board.fen(en_passant='fen'))
        for ply, frame in enumerate(details['frames']):
            self.assertEqual(frame['ply'], ply)
            self.assertEqual(frame['fen'], board.fen(en_passant='fen'))
            self.assertEqual(frame['board'], {chess.square_name(square): piece.symbol()
                                             for square, piece in board.piece_map().items()})
            self.assertEqual(frame['turn'], 'white' if board.turn else 'black')
            self.assertEqual(frame['in_check'], board.is_check())
            self.assertEqual(frame['legal_moves'], [])
            self.assertFalse(frame['human_to_move'])
            self.assertFalse(frame['engine_to_move'])
            self.assertFalse(frame['can_undo'])
            self.assertIn('opening_guide', frame)
            if ply < len(moves):
                self.assertEqual(details['moves'][ply]['uci'], moves[ply].uci())
                self.assertEqual(details['moves'][ply]['san'], board.san(moves[ply]))
                board.push(moves[ply])
        self.assertEqual(board.fen(), game.end().board().fen())

    def test_standard_replay_and_real_opening_name(self):
        text = '1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 *\n'
        details = preview_pgn(text)
        self.assert_frames(text, details)
        self.assertEqual(details['frames'][5]['opening_guide']['matched_opening']['name'], 'Ruy Lopez')
        summary = details['summary']
        self.assertEqual(summary['title'], 'PGN 파일')
        self.assertEqual(summary['source_kind'], 'pgn')
        self.assertEqual(summary['engine_name'], 'PGN 기록')
        self.assertEqual(summary['human_color'], 'white')
        self.assertIsNone(summary['movetime_ms'])
        self.assertFalse(summary['current'])

    def test_utf8_bom_crlf_headers_comments_variations_preserved_on_file_roundtrip(self):
        text = ('\ufeff[Event "내 기보"]\r\n[Site "Local"]\r\n[Result "*"]\r\n\r\n'
                '1. e4 {사용자 메모 [%clk 0:10:00]} (1. d4 d5) e5 $1 2. Nf3 Nc6 *\r\n')
        with tempfile.TemporaryDirectory() as temporary:
            source, exported = Path(temporary) / 'source.pgn', Path(temporary) / 'exported.pgn'
            source.write_bytes(text.encode('utf-8'))
            details = preview_pgn(source.read_bytes().decode('utf-8'))
            exported.write_bytes(details['pgn'].encode('utf-8'))
            self.assertEqual(exported.read_bytes(), source.read_bytes())
            first, second = read(source.read_text(encoding='utf-8')), read(exported.read_text(encoding='utf-8'))
            self.assertEqual(dict(first.headers), dict(second.headers))
            self.assertEqual(list(first.mainline_moves()), list(second.mainline_moves()))
            self.assertEqual(first.variations[0].comment, second.variations[0].comment)
            self.assertEqual(len(second.variations), 2)
        self.assert_frames(text, details)
        self.assertTrue(details['warnings'])

    def test_setup_roots_castling_en_passant_and_all_promotions(self):
        cases = [
            ('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1', '1. O-O O-O-O *'),
            ('4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2', '2. exd6 e.p. *'),
            ('4k3/8/8/8/3Pp3/8/8/4K3 b - d3 0 2', '2... exd3 *'),
            ('7k/P7/8/8/8/8/8/4K3 w - - 0 1', '1. a8=Q+ *'),
            ('7k/P7/8/8/8/8/8/4K3 w - - 0 1', '1. a8=R+ *'),
            ('7k/P7/8/8/8/8/8/4K3 w - - 0 1', '1. a8=B *'),
            ('7k/P7/8/8/8/8/8/4K3 w - - 0 1', '1. a8=N *'),
            ('4k3/8/8/8/8/8/p7/7K b - - 0 1', '1... a1=Q+ *'),
            ('1r5k/P7/8/8/8/8/8/4K3 w - - 0 1', '1. axb8=Q+ *'),
        ]
        for fen, moves in cases:
            with self.subTest(fen=fen, moves=moves):
                text = f'[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n{moves}\n'
                self.assert_frames(text, preview_pgn(text))

    def test_results_are_reported_neutrally_without_invented_resignation(self):
        for result in ('1-0', '0-1', '1/2-1/2', '*'):
            text = f'[Result "{result}"]\n\n1. e4 e5 {result}\n'
            details = preview_pgn(text)
            self.assertEqual(details['summary']['result'], result)
            self.assertIsNone(details['summary']['termination'])
            self.assertEqual(details['frames'][-1]['result'], result)
            self.assertNotIn('Regicide', details['summary']['status'])

    def test_regicide_handicap_comment_and_player_headers_survive_preview(self):
        text = ('[White "Human"]\n[Black "Regicide test"]\n[Result "*"]\n\n'
                '1. e4 {generic comment} f6 {User-forced Regicide move (handicap)} *\n')
        details = preview_pgn(text)
        self.assertEqual(details['summary']['white'], 'Human')
        self.assertEqual(details['summary']['black'], 'Regicide test')
        self.assertEqual(details['summary']['handicap_moves'], 1)
        self.assertTrue(details['moves'][1]['forced'])
        self.assertEqual(details['moves'][1]['comment'], 'User-forced Regicide move (handicap)')
        self.assertNotIn('forced', details['moves'][0])
        self.assertEqual(details['pgn'], text)

    def test_empty_header_only_malformed_and_illegal_data_rejected(self):
        cases = ['', '   ', '{}', '[Event "Header only"]\n\n*', '1. e5 *',
                 '1. e4 garbage e5 *', '[Event missing quotes]\n1. e4 *',
                 '[Event "one"]\n[Event "two"]\n\n1. e4 *',
                 '1. e4 {not closed', '1. e4 (1. d4 *', '1. e4 ) e5 *',
                 '1. e4 () e5 *', '1. e4 (1. e5) e5 *',
                 '1. f3 e5 2. g4 Qh4# 3. a3 *', '1. e4\x00 e5 *',
                 '[Result "1-0"]\n\n1. e4 0-1', '[Result "invalid"]\n\n1. e4 *']
        for text in cases:
            with self.subTest(text=text), self.assertRaises(PGNPreviewError):
                preview_pgn(text)

    def test_multiple_games_including_same_line_are_rejected(self):
        for text in ['1. e4 *\n\n1. d4 *', '1. e4 * 1. d4 *',
                     '[Event "one"]\n\n1. e4 *\n\n[Event "two"]\n\n1. d4 *',
                     '1. e4 e5\n\n1. d4 d5 *']:
            with self.subTest(text=text), self.assertRaises(PGNPreviewError):
                preview_pgn(text)

    def test_variants_chess960_bad_roots_and_null_moves_rejected(self):
        cases = ['[Variant "Chess960"]\n\n1. e4 *', '[Variant "Crazyhouse"]\n\n1. e4 *',
                 '[SetUp "1"]\n\n1. e4 *', '[SetUp "invalid"]\n\n1. e4 *',
                 '[SetUp "1"]\n[FEN "8/8/8/8/8/8/8/8 w - - 0 1"]\n\n1. e4 *',
                 '1. -- *', '1. 0000 *', '1. Z0 *', '1. @@@@ *', '1. e4 (1. --) e5 *']
        for text in cases:
            with self.subTest(text=text), self.assertRaises(PGNPreviewError):
                preview_pgn(text)

    def test_ply_and_utf8_byte_limits_reject_before_framing(self):
        over_plies = ' '.join(['Nf3 Nf6 Ng1 Ng8'] * (MAX_PGN_PLIES // 4 + 1)) + ' *'
        over_bytes = '1. e4 {' + '\u2603' * (MAX_PGN_BYTES // 3 + 1) + '} *'
        with patch('pgn_preview.OpeningGuideCache', side_effect=AssertionError('framing started')):
            for text in (over_plies, over_bytes):
                with self.subTest(size=len(text)), self.assertRaises(PGNPreviewError):
                    preview_pgn(text)
        for value in (None, [], {}, 1):
            with self.subTest(value=value), self.assertRaises(PGNPreviewError):
                preview_pgn(value)

    def test_exact_byte_limit_and_missing_result_are_supported(self):
        prefix, suffix = '1. e4 {', '} *'
        text = prefix + 'x' * (MAX_PGN_BYTES - len(prefix + suffix)) + suffix
        self.assertEqual(len(text.encode()), MAX_PGN_BYTES)
        self.assertEqual(preview_pgn(text)['pgn'], text)
        partial = preview_pgn('1. e4 e5')
        self.assertEqual(partial['summary']['result'], '*')
        self.assertTrue(partial['warnings'])

    def test_exact_mainline_cap_has_bounded_complete_replay(self):
        text = long_legal_pgn()
        details = preview_pgn(text)
        self.assertEqual(details['summary']['ply_count'], MAX_PGN_PLIES)
        self.assertEqual(len(details['frames']), MAX_PGN_PLIES + 1)
        self.assertLess(len(json.dumps(details, ensure_ascii=False).encode('utf-8')), 4 * 1024 * 1024)
        self.assert_frames(text, details)

    def test_cr_only_text_is_parsed_but_returned_unchanged(self):
        text = '[Event "CR lines"]\r[Result "*"]\r\r1. e4 e5 *\r'
        details = preview_pgn(text)
        self.assertEqual(details['pgn'], text)
        self.assertEqual([move['uci'] for move in details['moves']], ['e2e4', 'e7e5'])

    def test_automatic_repetition_prevents_moves_after_game_end(self):
        text = '1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3 Nf6 4. Ng1 Ng8 5. Nf3 Nf6 6. Ng1 Ng8 7. Nf3 Nf6 8. Ng1 Ng8 9. a3 *'
        with self.assertRaises(PGNPreviewError):
            preview_pgn(text)


class PGNPreviewHTTPTests(unittest.TestCase):
    setUp = baseline.HumanHTTPTests.setUp
    stop_server = baseline.HumanHTTPTests.stop_server
    request = baseline.HumanHTTPTests.request

    def files(self):
        root = Path(self.folder.name)
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob('*') if path.is_file()}

    def test_auth_origin_and_content_type_checks_remain_required(self):
        payload = {'pgn': '1. e4 e5 *'}
        for options in [{'token': False}, {'headers': {'X-Regicide-Token': 'bad'}},
                        {'headers': {'Origin': 'https://external.example'}},
                        {'headers': {'Host': 'external.example'}}]:
            with self.subTest(options=options):
                self.assertEqual(self.request('/api/pgn-preview', payload, **options)[0], 403)
        self.assertEqual(self.request('/api/pgn-preview', payload,
                                     headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/pgn-preview', payload,
                                     headers={'Origin': self.server.origin})[0], 200)

    def test_valid_and_invalid_preview_do_not_touch_live_game_archive_or_engine(self):
        before, files_before = self.game.state(), self.files()
        factory_before = [(id(peer), list(peer.calls)) for peer in self.factory.instances]
        with patch.object(self.game, 'state', side_effect=AssertionError('live state read')), \
                patch.object(self.game, 'pgn', side_effect=AssertionError('live PGN read')), \
                patch.object(self.game, 'evaluation', side_effect=AssertionError('engine evaluation')), \
                patch.object(self.game, 'hint', side_effect=AssertionError('hint read')), \
                patch.object(self.server.archive, 'get_game', side_effect=AssertionError('archive read')):
            code, _, body = self.request('/api/pgn-preview', {'pgn': '1. e4 e5 *'})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['summary']['source_kind'], 'pgn')
            code, _, body = self.request('/api/pgn-preview', {'pgn': '1. e5 *'})
            self.assertEqual(code, 400)
            self.assertNotIn('state', json.loads(body))
        self.assertEqual(self.game.state(), before)
        self.assertEqual(self.files(), files_before)
        self.assertEqual([(id(peer), list(peer.calls)) for peer in self.factory.instances], factory_before)

    def test_only_preview_accepts_large_body_and_extra_path_fields_are_rejected(self):
        payload = {'pgn': '1. e4 {' + 'x' * 5000 + '} e5 *'}
        code, _, body = self.request('/api/pgn-preview', payload)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['pgn'], payload['pgn'])
        before = self.game.state()
        self.assertEqual(self.request('/api/hint', {'enabled': True, 'padding': 'x' * 5000})[0], 400)
        self.assertEqual(self.request('/api/pgn-preview', {'pgn': '1. e4 *', 'filename': 'private.pgn'})[0], 400)
        self.assertEqual(self.request('/api/pgn-preview', {'pgn': None})[0], 400)
        self.assertEqual(self.game.state(), before)


if __name__ == '__main__':
    unittest.main()
