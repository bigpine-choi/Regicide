"""Statistical correctness, index invalidation and bounded remote access."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from opening_explorer import OpeningExplorer


def pgn(moves=(), result="1-0", *, fen=None, headers=None):
    board = chess.Board(fen) if fen else chess.Board()
    game = chess.pgn.Game()
    game.setup(board)
    game.headers["Result"] = result
    if headers:
        game.headers.update(headers)
    node = game
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise AssertionError("test fixture contains illegal move: " + uci)
        node = node.add_variation(move)
        board.push(move)
    return str(game) + "\n\n"


def payload(*, white=60, draws=20, black=20, moves=None, opening=None):
    return {"white": white, "draws": draws, "black": black,
            "opening": opening, "moves": moves if moves is not None else [
                {"uci": "e2e4", "san": "e4", "white": 30, "draws": 10, "black": 10}]}


class LocalStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.explorer = OpeningExplorer(self.root)

    def write(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def test_completed_results_have_white_absolute_percentages_and_real_denominators(self):
        self.write("nested/games.pgn", pgn(["e2e4"], "1-0") + pgn(["e2e4"], "0-1") +
                   pgn(["d2d4"], "1/2-1/2") + pgn(["g1f3"], "*"))
        result = self.explorer.query(chess.STARTING_FEN)
        self.assertEqual(result["status"], "ready")
        self.assertEqual((result["white"], result["draws"], result["black"]), (1, 1, 1))
        self.assertEqual(result["total"], 3)
        self.assertEqual((result["white_pct"], result["draw_pct"], result["black_pct"]), (33.3, 33.3, 33.3))
        self.assertEqual(result["moves"][0]["uci"], "e2e4")
        self.assertEqual(result["moves"][0]["frequency_pct"], 66.7)
        self.assertEqual(result["moves"][0]["white_pct"], 50.0)
        self.assertEqual(result["moves"][0]["draw_pct"], 0.0)
        self.assertEqual(result["source_label"], "내 완료 기보")
        self.assertEqual(result["excluded_games"], 1)

    def test_move_order_transpositions_match(self):
        self.write("a.pgn", pgn(["g1f3", "d7d5", "g2g3", "g8f6"], "1-0"))
        self.write("b.pgn", pgn(["g2g3", "d7d5", "g1f3", "e7e6"], "0-1"))
        board = chess.Board()
        for move in ("g1f3", "d7d5", "g2g3"):
            board.push_uci(move)
        result = self.explorer.query(board.fen())
        self.assertEqual(result["total"], 2)
        self.assertEqual({row["uci"] for row in result["moves"]}, {"g8f6", "e7e6"})

    def test_repeated_positions_are_counted_once_per_game_with_first_continuation(self):
        self.write("repetition.pgn", pgn(["g1f3", "g8f6", "f3g1", "f6g8", "d2d4"], "1/2-1/2"))
        result = self.explorer.query(chess.STARTING_FEN)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["draws"], 1)
        self.assertEqual([row["uci"] for row in result["moves"]], ["g1f3"])

    def test_identical_pgn_copies_are_separate_records_not_silently_deduplicated(self):
        contents = pgn(["e2e4"], "1-0")
        self.write("original.pgn", contents)
        self.write("nested/copied.pgn", contents)
        result = self.explorer.query(chess.STARTING_FEN)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["indexed_games"], 2)
        self.assertEqual(result["moves"][0]["total"], 2)

    def test_counters_do_not_change_position_but_turn_and_castling_do(self):
        self.write("a.pgn", pgn(["e2e4"]))
        same = chess.STARTING_FEN.rsplit(" ", 2)[0] + " 50 100"
        self.assertEqual(self.explorer.query(same)["total"], 1)
        no_castle = chess.STARTING_FEN.replace(" KQkq ", " - ")
        self.assertEqual(self.explorer.query(no_castle)["total"], 0)
        other_turn = chess.STARTING_FEN.replace(" w ", " b ")
        self.assertEqual(self.explorer.query(other_turn)["total"], 0)

    def test_legal_en_passant_right_is_preserved(self):
        fen = "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1"
        self.write("ep.pgn", pgn(["e5d6"], fen=fen))
        self.assertEqual(self.explorer.query(fen)["total"], 1)
        self.assertEqual(self.explorer.query(fen.replace(" d6 ", " - "))["total"], 0)

    def test_irrelevant_en_passant_target_does_not_split_same_legal_position(self):
        self.write("e4.pgn", pgn(["e2e4", "e7e5"]))
        board = chess.Board()
        board.push_uci("e2e4")
        self.assertEqual(self.explorer.query(board.fen(en_passant="fen"))["total"], 1)
        self.assertEqual(self.explorer.query(board.fen(en_passant="legal"))["total"], 1)

    def test_terminal_positions_count_without_a_next_move(self):
        self.write("mate.pgn", pgn(["f2f3", "e7e5", "g2g4", "d8h4"], "0-1"))
        board = chess.Board()
        for move in ("f2f3", "e7e5", "g2g4", "d8h4"):
            board.push_uci(move)
        result = self.explorer.query(board.fen())
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["black_pct"], 100.0)
        self.assertEqual(result["moves"], [])

    def test_changed_and_deleted_files_update_index_without_replaying_unchanged_files(self):
        path = self.write("a.pgn", pgn(["e2e4"]))
        with patch.object(self.explorer, "_parse_file", wraps=self.explorer._parse_file) as parse:
            self.assertEqual(self.explorer.query(chess.STARTING_FEN)["white"], 1)
            self.explorer.query(chess.STARTING_FEN)
            self.assertEqual(parse.call_count, 1)
            path.write_text(pgn(["d2d4"], "0-1"), encoding="utf-8")
            result = self.explorer.query(chess.STARTING_FEN)
            self.assertEqual((result["white"], result["black"]), (0, 1))
            self.assertEqual(result["moves"][0]["uci"], "d2d4")
            self.assertEqual(parse.call_count, 2)
            path.unlink()
            result = self.explorer.query(chess.STARTING_FEN)
            self.assertEqual(result["status"], "empty")
            self.assertEqual(result["indexed_games"], 0)

    def test_incomplete_and_handicap_games_are_excluded(self):
        self.write("games.pgn", pgn(["e2e4"], "*") +
                   pgn(["d2d4"], "1-0", headers={"HandicapMoves": "1"}) +
                   pgn(["c2c4"], "0-1", headers={"HandicapMoves": "0"}))
        result = self.explorer.query(chess.STARTING_FEN)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["excluded_games"], 2)
        self.assertEqual(result["moves"][0]["uci"], "c2c4")

    def test_illegal_or_invalid_position_never_indexes_or_calls_remote(self):
        fetch = Mock()
        explorer = OpeningExplorer(self.root, token="secret", fetcher=fetch)
        for fen in ("not a FEN", "8/8/8/8/8/8/8/8 w - - 0 1", None):
            self.assertEqual(explorer.query(fen, "masters")["status"], "unavailable")
        fetch.assert_not_called()

    def test_empty_sample_has_null_percentages_and_json_safe_response(self):
        result = self.explorer.query(chess.STARTING_FEN)
        self.assertEqual(result["status"], "empty")
        self.assertIsNone(result["white_pct"])
        self.assertIsNone(result["draw_pct"])
        json.dumps(result, allow_nan=False)


class MastersStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_token_is_required_without_network_request(self):
        fetch = Mock()
        with patch.dict("os.environ", {}, clear=True):
            result = OpeningExplorer(self.root, fetcher=fetch).query(chess.STARTING_FEN, "masters")
        self.assertEqual(result["status"], "auth_required")
        self.assertIsNone(result["white_pct"])
        fetch.assert_not_called()

    def test_request_authenticates_only_backend_and_top_subset_keeps_total_denominator(self):
        fetch = Mock(return_value=payload(opening={"name": "King's Pawn Game", "eco": "C20"}))
        explorer = OpeningExplorer(self.root, token="secret-token", fetcher=fetch)
        result = explorer.query(chess.STARTING_FEN, "masters")
        url, headers, timeout = fetch.call_args.args
        self.assertEqual(urllib.parse.urlsplit(url).netloc, "explorer.lichess.org")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["topGames"], ["0"])
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertGreater(timeout, 0)
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["moves"][0]["frequency_pct"], 50.0)
        self.assertEqual(result["moves"][0]["white_pct"], 60.0)
        self.assertEqual(result["opening"]["eco"], "C20")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_environment_token_is_supported(self):
        fetch = Mock(return_value=payload())
        with patch.dict("os.environ", {"REGICIDE_LICHESS_TOKEN": "test-environment-token"}):
            result = OpeningExplorer(self.root, fetcher=fetch).query(chess.STARTING_FEN, "masters")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(fetch.call_args.args[1]["Authorization"], "Bearer test-environment-token")

    def test_cached_snapshots_are_detached_and_counters_share_cache(self):
        fetch = Mock(return_value=payload())
        explorer = OpeningExplorer(self.root, token="secret", fetcher=fetch)
        first = explorer.query(chess.STARTING_FEN, "masters")
        first["moves"][0]["total"] = 9999
        fen = chess.STARTING_FEN.rsplit(" ", 2)[0] + " 42 33"
        second = explorer.query(fen, "masters")
        self.assertTrue(second["cached"])
        self.assertEqual(second["fen"], fen)
        self.assertEqual(second["moves"][0]["total"], 50)
        self.assertEqual(fetch.call_count, 1)

    def test_concurrent_identical_queries_make_one_request(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(url, headers, timeout):
            entered.set()
            self.assertTrue(release.wait(2))
            return payload()
        fetch = Mock(side_effect=fetch)
        explorer = OpeningExplorer(self.root, token="secret", fetcher=fetch)
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(explorer.query, chess.STARTING_FEN, "masters")
            self.assertTrue(entered.wait(1))
            second = pool.submit(explorer.query, chess.STARTING_FEN, "masters")
            release.set()
            self.assertEqual(first.result(timeout=2)["total"], 100)
            self.assertEqual(second.result(timeout=2)["total"], 100)
        self.assertEqual(fetch.call_count, 1)

    def test_distinct_queries_are_serialized(self):
        active = maximum = 0
        lock = threading.Lock()
        def fetch(url, headers, timeout):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.02)
            with lock:
                active -= 1
            return payload(moves=[])
        explorer = OpeningExplorer(self.root, token="secret", fetcher=fetch)
        board = chess.Board()
        board.push_uci("e2e4")
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(explorer.query, fen, "masters") for fen in
                     (chess.STARTING_FEN, board.fen())]
            self.assertTrue(all(call.result(timeout=2)["status"] == "ready" for call in calls))
        self.assertEqual(maximum, 1)

    def test_rate_limit_enforces_cooldown_without_automatic_retry(self):
        error = urllib.error.HTTPError("hidden", 429, "private secret", {"Retry-After": "90"}, None)
        fetch = Mock(side_effect=error)
        explorer = OpeningExplorer(self.root, token="secret", fetcher=fetch)
        result = explorer.query(chess.STARTING_FEN, "masters")
        self.assertEqual(result["status"], "rate_limited")
        self.assertGreaterEqual(result["retry_after_seconds"], 89)
        board = chess.Board()
        board.push_uci("e2e4")
        result = explorer.query(board.fen(), "masters")
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(fetch.call_count, 1)
        self.assertNotIn("secret", json.dumps(result))

    def test_authentication_failure_and_network_failure_are_distinct_and_private(self):
        for failure, status in ((urllib.error.HTTPError("secret-url", 401, "secret", {}, None), "auth_required"),
                                (TimeoutError("secret-token"), "unavailable")):
            with self.subTest(status=status):
                explorer = OpeningExplorer(self.root, token="secret-token", fetcher=Mock(side_effect=failure))
                result = explorer.query(chess.STARTING_FEN, "masters")
                self.assertEqual(result["status"], status)
                self.assertNotIn("secret", json.dumps(result))

    def test_invalid_remote_counts_or_illegal_moves_are_rejected(self):
        invalid = [payload(white=-1), payload(white=True), payload(moves=[
            {"uci": "e2e5", "white": 1, "draws": 0, "black": 0}]), payload(moves=[
            {"uci": "e2e4", "white": 101, "draws": 0, "black": 0}]),
            payload(moves=[{"uci": "e2e4", "white": 1, "draws": 0, "black": 0}] * 2)]
        for value in invalid:
            with self.subTest(value=value):
                result = OpeningExplorer(self.root, token="secret", fetcher=Mock(return_value=value)).query(
                    chess.STARTING_FEN, "masters")
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["white_pct"])

    def test_zero_games_are_empty_instead_of_network_failure(self):
        explorer = OpeningExplorer(self.root, token="secret", fetcher=Mock(return_value=
                                   payload(white=0, draws=0, black=0, moves=[])))
        result = explorer.query(chess.STARTING_FEN, "masters")
        self.assertEqual(result["status"], "empty")
        self.assertIsNone(result["white_pct"])

    def test_each_candidate_cannot_exceed_position_result_category(self):
        for category in ("white", "draws", "black"):
            with self.subTest(category=category):
                candidate = {"uci": "e2e4", "white": 0, "draws": 0, "black": 0}
                candidate[category] = 11  # Below overall total 30, above this category 10.
                data = payload(white=10, draws=10, black=10, moves=[candidate])
                explorer = OpeningExplorer(self.root, token="secret", fetcher=Mock(return_value=data))
                result = explorer.query(chess.STARTING_FEN, "masters")
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["white_pct"])

    def test_candidate_sums_cannot_exceed_position_result_category(self):
        for category in ("white", "draws", "black"):
            with self.subTest(category=category):
                candidates = [{"uci": uci, "white": 0, "draws": 0, "black": 0}
                              for uci in ("e2e4", "d2d4")]
                for candidate in candidates:
                    candidate[category] = 6  # Each fits; sum 12 exceeds category 10.
                data = payload(white=10, draws=10, black=10, moves=candidates)
                explorer = OpeningExplorer(self.root, token="secret", fetcher=Mock(return_value=data))
                result = explorer.query(chess.STARTING_FEN, "masters")
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["white_pct"])

    def test_cache_is_bounded_and_eviction_refetches_old_position(self):
        fetch = Mock(return_value=payload(moves=[]))
        explorer = OpeningExplorer(self.root, token="secret", fetcher=fetch, cache_size=1)
        explorer.query(chess.STARTING_FEN, "masters")
        board = chess.Board()
        board.push_uci("e2e4")
        explorer.query(board.fen(), "masters")
        explorer.query(chess.STARTING_FEN, "masters")
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(len(explorer._cache), 1)


if __name__ == "__main__":
    unittest.main()
