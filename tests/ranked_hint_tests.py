"""Coherent same-depth own-engine hint ranking; deterministic protocol peers.

Scores here control ordering/error boundaries, not objective chess strength.
The independent real UCI reproduction lives in ranked_hint_real_tests.py.
"""
import copy
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from move_hints import MoveHints
from hint_state_tests import wait_for


class Clock:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now


class RankedStream:
    def __init__(self, owner, board, limit, roots, action):
        self.owner, self.action = owner, action
        legal = sorted(board.legal_moves, key=lambda move: move.uci())
        allowed = list(roots) if roots is not None else legal
        self.move = chess.Move.from_uci(action["move"]) if "move" in action else allowed[0]
        result = action.get("return_move", self.move)
        self.result = chess.Move.from_uci(result) if isinstance(result, str) else result
        score = action.get("score", chess.engine.Cp(0))
        pv = action.get("pv", [self.move])
        pv = [chess.Move.from_uci(move) if isinstance(move, str) else move for move in pv] if isinstance(pv, list) else pv
        packet = {"depth": action.get("depth", min(limit.depth or 1, owner.max_depth)),
                  "score": chess.engine.PovScore(score, action.get("pov", board.turn)), "pv": pv}
        packet.update(action.get("extra", {}))
        for key in action.get("omit", ()):
            packet.pop(key, None)
        self.packets = action.get("packets", [packet])
        self.info = {}
        self.waits = 0
        self.stops = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def __iter__(self):
        for packet in self.packets:
            if isinstance(packet, BaseException):
                raise packet
            self.info.update(packet)
            yield copy.deepcopy(packet)
        if self.action.get("blocked"):
            self.owner.waiting.set()
            if not self.owner.release.wait(3):
                raise TimeoutError("test stream was not released")
        if self.owner.clock is not None:
            self.owner.clock.now += self.action.get("cost", self.owner.cost)

    def wait(self):
        self.waits += 1
        return chess.engine.BestMove(self.result, None)

    def stop(self):
        self.stops += 1
        if self.owner.close_releases:
            self.owner.release.set()


class RankedPeer:
    def __init__(self, actions=(), *, max_depth=1, clock=None, cost=0):
        self.actions = list(actions)
        self.max_depth, self.clock, self.cost = max_depth, clock, cost
        self.calls, self.streams = [], []
        self.closed = False
        self.close_releases = True
        self.waiting, self.release, self.close_event = threading.Event(), threading.Event(), threading.Event()

    def analysis(self, board, limit, *, root_moves=None, game=None, info=None):
        self.calls.append({"board": board.copy(stack=True), "limit": limit,
                           "roots": list(root_moves) if root_moves is not None else None,
                           "game": game, "at": self.clock.now if self.clock else time.monotonic()})
        action = self.actions[len(self.calls) - 1] if len(self.calls) <= len(self.actions) else {}
        if isinstance(action, BaseException):
            raise action
        stream = RankedStream(self, board, limit, root_moves, action)
        self.streams.append(stream)
        return stream

    def play(self, *_args, **_kwargs):
        raise AssertionError("real-like peer must use its raw analysis packets")

    def close(self):
        self.closed = True
        self.close_event.set()
        if self.close_releases:
            self.release.set()


class RankedHintTests(unittest.TestCase):
    def start(self, peer, board=None, milliseconds=1000, *, factory=None):
        board = board or chess.Board()
        hints = MoveHints("not-executed", factory or (lambda _: peer), timeout_grace=.05)
        self.addCleanup(hints.close)
        hints.update_position(game_id="ranked", revision=1, board=board, human_color=board.turn,
                              movetime_ms=milliseconds, engine_name="Regicide test", active=True)
        hints.set_enabled(True)
        return hints

    def done(self, hints):
        return wait_for(lambda: hints.snapshot() if hints.snapshot()["status"] in ("ready", "unavailable") else None)

    def assert_coherent(self, packet, board, count=3, depth=1):
        self.assertEqual(packet["status"], "ready")
        self.assertEqual(len(packet["candidates"]), count)
        self.assertEqual([row["rank"] for row in packet["candidates"]], list(range(1, count + 1)))
        self.assertEqual(len({row["move"] for row in packet["candidates"]}), count)
        for row in packet["candidates"]:
            self.assertEqual(row["depth"], depth)
            move = chess.Move.from_uci(row["move"])
            self.assertIn(move, board.legal_moves)
            self.assertEqual(row["san"], board.san(move))
            self.assertEqual((row["from"], row["to"]), (row["move"][:2], row["move"][2:4]))
        for key in ("move", "san", "from", "to", "promotion", "depth"):
            self.assertEqual(packet[key], packet["candidates"][0][key])

    def test_excluded_roots_and_same_depth_round_are_sorted_by_exact_scores(self):
        peer = RankedPeer([{"move": "e2e4", "score": chess.engine.Cp(10)},
                           {"move": "c2c4", "score": chess.engine.Cp(40)},
                           {"move": "d2d4", "score": chess.engine.Cp(30)}])
        board = chess.Board()
        packet = self.done(self.start(peer, board))
        self.assert_coherent(packet, board)
        self.assertEqual([row["move"] for row in packet["candidates"]], ["c2c4", "d2d4", "e2e4"])
        self.assertIsNone(peer.calls[0]["roots"])
        self.assertNotIn(chess.Move.from_uci("e2e4"), peer.calls[1]["roots"])
        self.assertNotIn(chess.Move.from_uci("c2c4"), peer.calls[2]["roots"])
        self.assertEqual([call["limit"].depth for call in peer.calls], [1, 1, 1, 2])
        self.assertTrue(peer.closed)

    def test_black_scores_are_ranked_for_black_and_equal_scores_use_uci_ties(self):
        board = chess.Board(); board.push_uci("e2e4")
        peer = RankedPeer([{"move": move, "score": chess.engine.Cp(cp), "pov": chess.WHITE}
                           for move, cp in (("e7e5", -10), ("c7c5", -40), ("d7d5", -30))])
        packet = self.done(self.start(peer, board))
        self.assert_coherent(packet, board)
        self.assertEqual([row["move"] for row in packet["candidates"]], ["c7c5", "d7d5", "e7e5"])
        tied = RankedPeer([{"move": move} for move in ("e2e4", "d2d4", "c2c4")])
        packet = self.done(self.start(tied))
        self.assertEqual([row["move"] for row in packet["candidates"]], ["c2c4", "d2d4", "e2e4"])

    def test_mate_ordering_uses_score_semantics_not_arbitrary_centipawn_conversion(self):
        peer = RankedPeer([{"move": "e2e4", "score": chess.engine.Mate(3)},
                           {"move": "c2c4", "score": chess.engine.Mate(1)},
                           {"move": "d2d4", "score": chess.engine.Cp(1000000)}])
        packet = self.done(self.start(peer))
        self.assertEqual([row["move"] for row in packet["candidates"]], ["c2c4", "e2e4", "d2d4"])

    def test_incomplete_or_failed_later_round_retains_only_previous_completed_depth(self):
        for bad in ({"depth": 1}, {"extra": {"upperbound": True}}, RuntimeError("later search failed")):
            with self.subTest(bad=bad):
                peer = RankedPeer([{}, {}, {}, {}, bad], max_depth=2)
                packet = self.done(self.start(peer))
                self.assert_coherent(packet, chess.Board(), depth=1)
                self.assertEqual(len(peer.calls), 5)

    def test_invalid_first_round_never_fabricates_a_partial_list(self):
        cases = [
            [{"omit": ["score"]}], [{"omit": ["pv"]}], [{"depth": 0}],
            [{"extra": {"lowerbound": True}}], [{"extra": {"upperbound": True}}],
            [{"move": "e2e4", "pv": ["e2e4", "e7e5", "e4e6"]}],
            [{"move": "e2e4", "return_move": "d2d4"}], [{"return_move": None}],
            [{"move": "e2e4"}, {"move": "e2e4"}],
            [{}, {"depth": 2}], [{}, {}, {"omit": ["score"]}],
        ]
        for actions in cases:
            with self.subTest(actions=actions):
                packet = self.done(self.start(RankedPeer(actions)))
                self.assertEqual(packet["status"], "unavailable")
                self.assertEqual(packet["candidates"], [])
                self.assertIsNone(packet["move"])

    def test_merged_depth_only_info_cannot_inherit_an_older_score_or_pv(self):
        packets = [{"depth": 0, "score": chess.engine.PovScore(chess.engine.Cp(90), chess.WHITE),
                    "pv": [chess.Move.from_uci("e2e4")]}, {"depth": 1}]
        peer = RankedPeer([{"move": "e2e4", "packets": packets}])
        packet = self.done(self.start(peer))
        self.assertEqual(peer.streams[0].info["depth"], 1)
        self.assertIn("score", peer.streams[0].info)  # Accumulated info looks complete, but is not.
        self.assertEqual(packet["status"], "unavailable")
        self.assertEqual(packet["candidates"], [])

    def test_single_budget_includes_each_command_and_keeps_latest_complete_round(self):
        clock = Clock()
        peer = RankedPeer(max_depth=64, clock=clock, cost=.03)
        with mock.patch("move_hints.time", clock):
            packet = self.done(self.start(peer, milliseconds=100))
        self.assert_coherent(packet, chess.Board(), depth=1)
        self.assertEqual(len(peer.calls), 4)
        for call in peer.calls:
            self.assertAlmostEqual(call["limit"].time, 10.1 - call["at"], places=9)
        self.assertLess(peer.calls[-1]["limit"].time, .011)
        # A first rank consuming the deadline leaves no coherent list to publish.
        clock = Clock()
        peer = RankedPeer(max_depth=64, clock=clock, cost=.11)
        with mock.patch("move_hints.time", clock):
            packet = self.done(self.start(peer, milliseconds=100))
        self.assertEqual(len(peer.calls), 1)
        self.assertEqual((packet["status"], packet["candidates"]), ("unavailable", []))

    def test_depth_cap_stops_a_zero_cost_peer_without_unbounded_work(self):
        peer = RankedPeer(max_depth=64)
        packet = self.done(self.start(peer))
        self.assert_coherent(packet, chess.Board(), depth=64)
        self.assertEqual(len(peer.calls), 64 * 3)

    def test_one_or_two_legal_moves_produce_only_that_many_ranks(self):
        for fen, count in (("8/8/8/8/8/8/5k2/r6K w - - 0 1", 1),
                           ("7r/8/8/8/5k2/8/8/7K w - - 0 1", 2)):
            board = chess.Board(fen)
            self.assertEqual(board.legal_moves.count(), count)
            peer = RankedPeer()
            packet = self.done(self.start(peer, board))
            self.assert_coherent(packet, board, count=count)
            self.assertEqual(len(peer.calls), count + 1)

    def test_distinct_promotions_keep_piece_choice_and_san(self):
        board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
        peer = RankedPeer([{"move": move, "score": chess.engine.Cp(cp)}
                           for move, cp in (("a7a8q", 900), ("a7a8r", 500), ("a7a8n", 300))])
        packet = self.done(self.start(peer, board))
        self.assert_coherent(packet, board)
        self.assertEqual([row["promotion"] for row in packet["candidates"]], ["q", "r", "n"])
        self.assertEqual([row["san"] for row in packet["candidates"]], ["a8=Q+", "a8=R+", "a8=N"])

    def test_cancellation_mid_later_rank_clears_even_an_earlier_complete_round(self):
        peer = RankedPeer([{}, {}, {}, {}, {"blocked": True}], max_depth=2)
        hints = self.start(peer)
        self.assertTrue(peer.waiting.wait(2))
        self.assertEqual(hints.snapshot()["candidates"], [])  # No progressive partial publication.
        off = hints.set_enabled(False)
        self.assertEqual((off["status"], off["candidates"]), ("disabled", []))
        self.assertTrue(peer.close_event.wait(2))
        wait_for(lambda: hints._cancel is None)
        self.assertEqual(hints.snapshot(), off)

    def test_watchdog_mid_later_rank_preserves_complete_round_but_reaps_helper(self):
        peer = RankedPeer([{}, {}, {}, {}, {"blocked": True}], max_depth=2)
        hints = self.start(peer, milliseconds=100)
        packet = self.done(hints)
        self.assert_coherent(packet, chess.Board(), depth=1)
        self.assertTrue(peer.closed)
        self.assertGreater(peer.streams[-1].stops, 0)

    def test_stale_generation_cannot_replace_new_position_or_its_history(self):
        old = RankedPeer([{}, {"blocked": True}])
        old.close_releases = False
        latest = RankedPeer()
        peers = iter((old, latest))
        board = chess.Board()
        hints = self.start(old, board, factory=lambda _: next(peers))
        self.assertTrue(old.waiting.wait(2))
        board.push_uci("e2e4"); board.push_uci("e7e5")
        before = board.fen(), tuple(board.move_stack)
        hints.update_position(game_id="ranked", revision=2, board=board, human_color=board.turn,
                              movetime_ms=1000, engine_name="Regicide test", active=True)
        self.assertEqual(hints.snapshot()["candidates"], [])
        self.assertTrue(old.close_event.wait(2))
        old.release.set()
        packet = self.done(hints)
        self.assert_coherent(packet, board)
        self.assertEqual((packet["revision"], packet["fen"]), (2, board.fen(en_passant="fen")))
        self.assertEqual((board.fen(), tuple(board.move_stack)), before)
        for call in latest.calls:
            self.assertEqual(tuple(call["board"].move_stack), before[1])
            self.assertEqual(call["board"].root().fen(), chess.STARTING_FEN)

    def test_nested_snapshots_are_defensive_and_ranked_queries_do_not_publish_evaluation(self):
        hints = self.start(RankedPeer())
        packet = self.done(hints)
        original = copy.deepcopy(packet)
        packet["candidates"][0]["move"] = "e2e5"
        packet["candidates"].append({"rank": 999})
        snapshot, evaluation = hints.snapshot_with_evaluation()
        self.assertEqual(snapshot, original)
        self.assertEqual(evaluation, {})
        snapshot["candidates"].clear()
        preference = hints.set_enabled(True)
        preference["candidates"][0]["san"] = "forged"
        self.assertEqual(hints.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
