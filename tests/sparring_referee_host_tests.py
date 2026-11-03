"""Independent host/archive checks with a controlled separate referee.

No real Stockfish, user games, network services or engine resolver is used.
Regicide and referee evidence deliberately disagree to expose source mixing.
"""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

import chess
import chess.pgn

import human_play_tests as base
from opening_variety_tests import Peer, info
import sparring_state_tests as state_fixtures
from game_archive import GameArchive
import sparring_referee

host = base.play_human
REFEREE_SHA = "b" * 64
REFEREE_NAME = "Stockfish 18 controlled referee"


class ControlledReferee:
    def __init__(self):
        self.updates, self.screens = [], []
        self.closed = 0
        self.accepted = True
        self.error = None
        self.on_screen = None
        self.overrides = {}
        self.value = {"available": True, "engine_name": REFEREE_NAME,
                      "binary_sha256": REFEREE_SHA, "status": "idle", "message": "controlled referee",
                      "source": sparring_referee.SOURCE, "policy": sparring_referee.POLICY,
                      "cp": None, "mate": None, "depth": None, "bound": None, "white_share": None}

    def snapshot(self):
        return copy.deepcopy(self.value)

    def update_position(self, *, game_id, revision, board, active=True, budget_ms=500):
        self.updates.append({"game_id": game_id, "revision": revision, "board": board.copy(stack=True),
                             "active": active, "budget_ms": budget_ms})
        self.value.update(game_id=game_id, revision=revision, fen=board.fen(en_passant="fen"),
                          turn="white" if board.turn else "black", status="ready" if active else "idle",
                          cp=777 if active else None, mate=None, depth=12 if active else None,
                          bound="exact" if active else None, white_share=.83 if active else None)
        return self.snapshot()

    def screen_candidates(self, **kwargs):
        self.screens.append({**kwargs, "board": kwargs["board"].copy(stack=True)})
        if self.on_screen:
            self.on_screen()
        if self.error:
            raise self.error
        baseline = kwargs["baseline_move"]
        proposal = next((move for move in kwargs["candidates"] if move != baseline), baseline)
        receipt = {"status": "ready", "source": sparring_referee.SOURCE, "policy": sparring_referee.POLICY,
                   "game_id": kwargs["game_id"], "revision": kwargs["revision"],
                   "fen": kwargs["board"].fen(en_passant="fen"), "baseline_uci": baseline.uci(),
                   "selected_uci": (proposal if self.accepted else baseline).uci(),
                   "accepted": self.accepted, "reason": "controlled_acceptance" if self.accepted else "controlled_rejection",
                   "engine_name": REFEREE_NAME, "binary_sha256": REFEREE_SHA,
                   "cp_white": 777, "mate_white": None, "depth": 12,
                   "candidates": [{"uci": move.uci(), "cp_white": 777, "mate_white": None, "depth": 12}
                                  for move in kwargs["candidates"]],
                   "postmove": {"status": "ready", "cp_white": 777, "depth": 12},
                   "budget_ms": kwargs["budget_ms"], "elapsed_ms": 10}
        receipt.update(copy.deepcopy(self.overrides))
        return receipt

    def close(self):
        self.closed += 1


class SparringRefereeHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-referee-host-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "controlled-regicide.exe"
        self.binary.write_bytes(b"controlled own engine; never executed")
        self.peer = Peer()
        self.referee = ControlledReferee()
        self.game = host.HumanGame(self.binary, self.root / "games", lambda _: self.peer,
                                  hint_engine_factory=base.FakeFactory(),
                                  evaluation_engine_factory=base.FakeFactory(),
                                  opening_variety_enabled=False, referee=self.referee,
                                  referee_info={"name": REFEREE_NAME, "sha256": REFEREE_SHA,
                                                "source_url": "https://github.com/official-stockfish/Stockfish",
                                                "license": "GPL-3.0-or-later"})
        self.addCleanup(self.game.close)

    def prepare(self):
        self.game.new_game("white", 1000, self.game.revision, sparring_mode=True)
        self.game.human_move("e2e4", self.game.revision)
        board = self.game.board.copy(stack=True)
        self.peer.scripts = [{"move": "c7c5", "packets": [info(board, "c7c5", 40, depth) for depth in (4, 5, 6)]}]
        return board

    def proposal(self, board, selected="e7e5"):
        return state_fixtures.SparringStateTests.decision(self, board, selected)

    def play(self):
        original = host.sparring_play.choose
        def choose(board, *args, **kwargs):
            # Let the real policy produce its own baseline-only receipt after
            # a rejection, rather than faking the fallback implementation.
            if kwargs.get("movetime_ms") == 0:
                return original(board, *args, **kwargs)
            return self.proposal(board), False
        with mock.patch.object(host.sparring_play, "choose", side_effect=choose):
            return self.game.engine_move(self.game.revision)

    def decision(self):
        return self.game._sparring_decisions[max(self.game._sparring_decisions)]

    def record(self):
        return json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))

    def test_referee_evaluation_is_a_separate_channel_and_never_changes_own_score(self):
        board = self.prepare()
        self.game._sync_evaluation(status="searching")
        for depth in (4, 5, 6):
            self.game._publish_evaluation(info(board, "c7c5", 40, depth))
        own = self.game.evaluation()
        self.assertEqual(own["cp"], -40)
        packet = self.game.evaluation_with_reference()
        self.assertEqual(packet["cp"], -40)
        self.assertEqual(packet["reference_evaluation"]["cp"], 777)
        self.assertEqual(packet["reference_evaluation"]["source"], sparring_referee.SOURCE)
        self.assertTrue(packet["sparring_referee"]["assisted"])
        packet["reference_evaluation"]["cp"] = 12345
        self.assertEqual(self.game.evaluation(), own)
        self.assertEqual(self.referee.snapshot()["cp"], 777)
        self.assertNotIn("reference_evaluation", self.game.evaluation())

    def test_only_regicide_baseline_and_verified_proposal_are_screened_and_recorded(self):
        board = self.prepare(); own_proposal = self.proposal(board)
        state = self.play()
        self.assertIsNone(state["error"]); self.assertEqual(state["last_move"], "e7e5")
        self.assertEqual(len(self.referee.screens), 1)
        query = self.referee.screens[0]
        self.assertEqual(query["baseline_move"].uci(), "c7c5")
        self.assertEqual([move.uci() for move in query["candidates"]], ["c7c5", "e7e5"])
        self.assertEqual(query["board"].fen(), board.fen())
        for key, value in own_proposal.items():
            self.assertEqual(self.decision()["regicide_proposal"][key], value)
        self.assertNotIn("referee", self.decision()["regicide_proposal"])
        self.assertEqual(self.decision()["referee"]["source"], sparring_referee.SOURCE)
        self.assertEqual(self.decision()["referee"]["selected_uci"], "e7e5")
        self.assertEqual(state["engine"]["name"], self.peer.id["name"])
        self.assertEqual(state["engine"]["binary_sha256"], self.game.binary_sha256)
        self.assertNotEqual(state["engine"]["binary_sha256"], REFEREE_SHA)

    def test_rejected_proposal_retains_own_baseline_and_original_separate_receipt(self):
        self.prepare(); self.referee.accepted = False
        state = self.play()
        self.assertIsNone(state["error"]); self.assertEqual(state["last_move"], "c7c5")
        receipt = self.decision()
        self.assertEqual(receipt["regicide_proposal"]["selected_uci"], "e7e5")
        self.assertEqual(receipt["selected_uci"], "c7c5")
        self.assertFalse(receipt["referee"]["accepted"])
        self.assertEqual(receipt["referee"]["cp_white"], 777)
        # The external 777cp belongs only to the nested referee evidence.
        self.assertNotEqual(receipt.get("selected_score", {}).get("cp_white"), 777)

    def test_referee_can_choose_another_verified_native_probe_without_mixing_scores(self):
        board = self.prepare()
        proposal = self.proposal(board)
        baseline = host.boss_play.score_line(board, info(board, "c7c5", 40, 6), chess.Move.from_uci("c7c5"))
        line = host.boss_play.score_line(board, info(board, "e7e6", 20, 6), chess.Move.from_uci("e7e6"))
        proposal.update(baseline=baseline, compared_depth=6, verification_depth=6,
            probes=[{"uci": "e7e6", "admitted": True, "naturalness": {"admitted": True},
                "compared_depth": 6, "candidate_compared": line, "candidate_final": line,
                "reference_delta_cp": 20, "loss_cp": 20}])
        self.referee.overrides = {"selected_uci": "e7e6"}
        with mock.patch.object(self, "proposal", return_value=proposal):
            state = self.play()
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "e7e6")
        decision = self.decision()
        self.assertEqual(decision["regicide_proposal"]["selected_uci"], "e7e5")
        self.assertEqual(decision["selected_cp_own"], 20)
        self.assertEqual(decision["selected_cp_white"], -20)
        self.assertEqual(decision["referee"]["cp_white"], 777)
        self.assertEqual([row["uci"] for row in decision["referee"]["native_pool"]], ["c7c5", "e7e5", "e7e6"])
        self.assertFalse(decision["postmove_verification"]["valid"])
        self.assertIsNone(decision["band_control"]["selected_postmove_cp"])
        self.assertEqual(decision["verification_policy"], sparring_referee.POLICY)
        replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        self.assertEqual(replay["referee_decisions"][0]["selected_uci"], "e7e6")

    def test_historical_v1_referee_identity_and_policy_survive_reconstruction(self):
        self.prepare(); self.play(); record = self.record()
        old = "regicide-stockfish-referee-v1"
        for meta in (record["sparring_referee"], record["state"]["engine"]["sparring_referee"]):
            meta["policy"] = old
        for receipt in record["sparring_referee"]["decisions"]:
            receipt["policy"] = old
        for decision in record["sparring_play"]["decisions"]:
            decision["referee"]["policy"] = old
        path = self.game.save_dir / (self.game.game_id + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        self.assertTrue(replay["summary"]["assisted_sparring"])
        self.assertEqual(replay["summary"]["sparring_referee"]["policy"], old)
        self.assertEqual(replay["referee_decisions"][0]["policy"], old)
        self.assertIn('[RefereePolicy "' + old + '"]', replay["pgn"])

    def test_referee_exception_retains_own_baseline_without_breaking_the_game(self):
        self.prepare(); self.referee.error = TimeoutError("controlled referee timeout")
        state = self.play()
        self.assertIsNone(state["error"]); self.assertEqual(state["last_move"], "c7c5")
        self.assertEqual(self.decision()["referee"]["status"], "error")
        self.assertEqual(self.decision()["regicide_proposal"]["selected_uci"], "e7e5")

    def test_reference_update_or_snapshot_failure_never_breaks_the_own_evaluation(self):
        board = self.prepare()
        with mock.patch.object(self.referee, "update_position", side_effect=RuntimeError("controlled update failure")):
            self.game._sync_evaluation(status="searching")
            self.game._publish_evaluation(info(board, "c7c5", 40, 6))
            value = self.game.evaluation_with_reference()
            self.assertEqual(value["cp"], -40)
            self.assertEqual(value["reference_evaluation"]["status"], "error")
            self.assertFalse(value["reference_evaluation"]["available"])
            self.assertTrue(value["sparring_referee"]["assisted"])
            self.assertIsNone(self.game.state()["error"])
        self.game._sync_evaluation(status="searching")
        self.game._publish_evaluation(info(board, "c7c5", 40, 6))
        with mock.patch.object(self.referee, "snapshot", side_effect=RuntimeError("controlled snapshot failure")):
            value = self.game.evaluation_with_reference()
            self.assertEqual(value["cp"], -40)
            self.assertEqual(value["reference_evaluation"]["status"], "error")
            self.assertIsNone(self.game.state()["error"])

    def test_stale_or_foreign_receipts_cannot_authorize_a_concession(self):
        invalid = [{"game_id": "other"}, {"revision": -1}, {"fen": chess.STARTING_FEN},
                   {"selected_uci": "d7d5"}, {"status": "loading"}, {"accepted": False},
                   {"policy": "unrelated-policy"}, {"source": "Regicide"},
                   {"binary_sha256": "c" * 64}]
        for override in invalid:
            with self.subTest(override=override):
                self.prepare(); self.referee.overrides = override
                state = self.play()
                self.assertIsNone(state["error"]); self.assertEqual(state["last_move"], "c7c5")

    def test_obsolete_game_token_cannot_append_a_move_or_referee_decision(self):
        self.prepare()
        self.referee.on_screen = lambda: setattr(self.game, "_game_token", object())
        state = self.play()
        self.assertIsNone(state["error"])
        self.assertEqual([move.uci() for move in self.game.board.move_stack], ["e2e4"])
        self.assertEqual(self.game._sparring_decisions, {})

    def test_play_now_cancels_referee_concession_and_keeps_a_legal_baseline(self):
        self.prepare()
        self.referee.on_screen = lambda: self.game.skip_engine_wait(self.game.revision)
        state = self.play()
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c7c5")
        self.assertTrue(self.referee.screens[-1]["cancelled"]())
        self.assertFalse(self.decision()["referee"]["applied"])

    def test_referee_budget_reduces_with_the_opponent_clock_and_skips_under_ten_seconds(self):
        board = self.prepare()
        for remaining, budget in ((600_000, 1500), (170_000, 800), (50_000, 350), (9_000, 0)):
            with self.subTest(remaining=remaining):
                self.referee.screens.clear()
                clock = {"enabled": True, "white_ms": 600_000, "black_ms": remaining}
                with mock.patch.object(self.game._clock, "snapshot", return_value=clock):
                    result = self.game._apply_sparring_referee(
                        board, self.proposal(board), info(board, "c7c5", 40, 6), {},
                        mock.Mock(side_effect=AssertionError("no extra own search")),
                        self.game.game_id, self.game._game_token, self.game.revision)
                self.assertEqual(len(self.referee.screens), int(budget > 0))
                if budget:
                    self.assertEqual(self.referee.screens[0]["budget_ms"], budget)
                else:
                    self.assertEqual(result["selected_uci"], "c7c5")
                    self.assertEqual(result["referee"]["status"], "skipped")

    def test_each_colors_fiftieth_move_preserves_native_baseline_without_a_referee_search(self):
        self.prepare()
        for ply in (98, 99):
            with self.subTest(ply=ply):
                board = state_fixtures.late_game_board(ply)
                baseline, proposal = list(board.legal_moves)[:2]
                self.referee.screens.clear()
                result = self.game._apply_sparring_referee(
                    board, {"baseline_uci": baseline.uci(), "selected_uci": proposal.uci()},
                    info(board, baseline.uci(), 40, 6), {},
                    mock.Mock(side_effect=AssertionError("no extra own search")),
                    self.game.game_id, self.game._game_token, self.game.revision)
                self.assertEqual(result["selected_uci"], baseline.uci())
                self.assertEqual(result["referee"]["reason"], "full_strength_phase")
                self.assertEqual(self.referee.screens, [])

    def test_referee_is_inactive_outside_sparring_and_in_user_forced_handicap(self):
        for mode in ({}, {"boss_mode": True}):
            with self.subTest(mode=mode):
                self.game.new_game("white", 1000, self.game.revision, **mode)
                self.assertFalse(self.referee.updates[-1]["active"])
                self.assertNotIn("reference_evaluation", self.game.evaluation_with_reference())
                self.assertFalse(self.game.state()["engine"]["sparring_referee"]["assisted"])
        self.prepare(); self.play(); count = len(self.referee.screens)
        self.game.begin_handicap(2, self.game.revision)
        self.assertFalse(self.referee.updates[-1]["active"])
        self.assertEqual(len(self.referee.screens), count)

    def test_undo_resets_reference_identity_and_close_stops_its_owned_service(self):
        self.prepare(); self.play(); self.game.undo(self.game.revision)
        value = self.game.evaluation_with_reference()["reference_evaluation"]
        self.assertEqual(value["fen"], self.game.board.fen(en_passant="fen"))
        self.assertEqual(value["revision"], self.game.revision)
        self.assertEqual(self.game._sparring_decisions, {})
        self.game.close(); self.assertEqual(self.referee.closed, 1)

    def test_json_pgn_and_reconstructed_archive_preserve_both_engine_identities(self):
        self.prepare(); self.play(); record = self.record()
        self.assertTrue(record["sparring_referee"]["assisted"])
        self.assertEqual(record["sparring_referee"]["identity"]["binary_sha256"], REFEREE_SHA)
        self.assertEqual(record["sparring_referee"]["decisions"][0]["source"], sparring_referee.SOURCE)
        self.assertEqual(record["sparring_play"]["decisions"][0]["regicide_proposal"]["selected_uci"], "e7e5")
        archive = GameArchive(self.game.save_dir)
        replay = archive.get_game(self.game.game_id)
        self.assertTrue(replay["summary"]["assisted_sparring"])
        self.assertEqual(replay["frames"][-1]["engine"]["sparring_referee"]["name"], REFEREE_NAME)
        self.assertEqual(replay["frames"][-1]["engine"]["name"], self.game.engine_name)
        self.assertEqual(replay["referee_decisions"][0]["selected_uci"], "e7e5")
        self.assertEqual(replay["referee_decisions"][0]["source"], sparring_referee.SOURCE)
        for pgn in (self.game.pgn(), replay["pgn"], archive.get_pgn(self.game.game_id)):
            parsed = chess.pgn.read_game(io.StringIO(pgn))
            self.assertFalse(parsed.errors)
            self.assertEqual(parsed.headers["SparringReferee"], REFEREE_NAME)
            self.assertEqual(parsed.headers["RefereeBinarySHA256"], REFEREE_SHA)
            self.assertEqual(parsed.headers["AssistedSparring"], "true")
            self.assertEqual(parsed.headers["RefereePolicy"], sparring_referee.POLICY)
            self.assertEqual(parsed.headers["EngineSHA256"], self.game.binary_sha256)
            self.assertEqual(parsed.headers["Black"], self.game.engine_name)

    def test_old_or_inconsistent_optional_referee_metadata_never_relabels_a_game(self):
        self.prepare(); self.play(); original = self.record()
        path = self.game.save_dir / (self.game.game_id + ".json")
        malformed = [None, {**original["sparring_referee"], "policy": "unknown"},
                     {**original["sparring_referee"], "identity": {"name": "Other", "binary_sha256": REFEREE_SHA}},
                     {**original["sparring_referee"], "binary_sha256": "not-a-hash"},
                     {**original["sparring_referee"], "enabled": False}]
        for metadata in malformed:
            with self.subTest(metadata=metadata):
                record = copy.deepcopy(original)
                if metadata is None:
                    record.pop("sparring_referee"); record["state"]["engine"].pop("sparring_referee")
                else:
                    record["sparring_referee"] = metadata
                path.write_text(json.dumps(record), encoding="utf-8")
                replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
                self.assertFalse(replay["summary"]["assisted_sparring"])
                self.assertNotIn("AssistedSparring", replay["pgn"])
                self.assertEqual(replay["frames"][-1]["fen"], self.game.board.fen(en_passant="fen"))

    def test_referee_failure_after_use_keeps_assisted_attribution_in_archives(self):
        self.prepare(); self.play()
        self.referee.value.update(available=False, status="error", message="controlled referee unavailable")
        self.game._save()
        replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        self.assertTrue(replay["summary"]["assisted_sparring"])
        metadata = replay["frames"][-1]["engine"]["sparring_referee"]
        self.assertFalse(metadata["available"]); self.assertTrue(metadata["historical"])
        self.assertEqual(metadata["status"], "error")
        self.assertIn('[AssistedSparring "true"]', replay["pgn"])

    def test_archive_discards_receipts_for_other_moves_without_changing_recorded_board(self):
        self.prepare(); self.play(); record = self.record()
        record["sparring_referee"]["decisions"][0]["selected_uci"] = "d7d5"
        record["sparring_play"]["decisions"][0]["referee"]["selected_uci"] = "d7d5"
        path = self.game.save_dir / (self.game.game_id + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        self.assertEqual(replay["referee_decisions"], [])
        self.assertEqual(replay["frames"][-1]["last_move"], "e7e5")
        self.assertTrue(replay["summary"]["assisted_sparring"])

    def test_http_evaluation_preserves_own_cp_and_adds_reference_channel(self):
        board = self.prepare()
        self.game._sync_evaluation(status="searching")
        self.game._publish_evaluation(info(board, "c7c5", 40, 6))
        server = host.HumanServer(self.game, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            request = urllib.request.Request(server.origin + "/api/evaluation", headers={"X-Regicide-Token": server.token})
            with urllib.request.urlopen(request, timeout=3) as response:
                value = json.load(response)
            self.assertEqual(value["cp"], -40)
            self.assertEqual(value["reference_evaluation"]["cp"], 777)
            self.assertTrue(value["sparring_referee"]["assisted"])
            self.assertEqual(self.referee.screens, [])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_injected_test_engine_never_auto_resolves_or_starts_stockfish(self):
        other = base.FakeFactory()
        with mock.patch("referee_engine.resolve_referee_engine", side_effect=AssertionError("no automatic test referee")):
            game = host.HumanGame(self.binary, self.root / "isolated", other,
                                  hint_engine_factory=base.FakeFactory(), opening_variety_enabled=False)
        try:
            game.new_game("white", 1000, game.revision, sparring_mode=True)
            self.assertFalse(game.state()["engine"]["sparring_referee"]["available"])
            self.assertFalse(game.state()["engine"]["sparring_referee"]["assisted"])
        finally:
            game.close()


if __name__ == "__main__":
    unittest.main()
