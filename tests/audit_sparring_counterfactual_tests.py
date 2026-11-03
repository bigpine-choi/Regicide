"""Selection completeness and same-parent comparable-packet checks; no native engine."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
import audit_sparring_counterfactual as audit


def fixture_record(result="0-1"):
    board, rows = chess.Board(), []
    sequence = ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "d2d3", "f8c5")
    for ply, uci in enumerate(sequence, 1):
        move = chess.Move.from_uci(uci)
        alternative = next(candidate for candidate in board.legal_moves if candidate != move)
        selected = uci if ply == 5 else alternative.uci()
        row = {"ply": ply, "actor": "regicide" if ply % 2 else "opponent", "uci": uci,
               "san": board.san(move), "before_fen": board.fen(en_passant="fen"),
               "forced_opening": ply == 7, "late": ply == 3,
               "decision": {"regicide_proposal": {"selected_uci": selected},
                            "referee": {"reason": "referee_error" if ply == 3 else "kept_baseline"}}}
        board.push(move)
        row["after_fen"] = board.fen(en_passant="fen")
        rows.append(row)
    return {"game": 1, "variant": "controlled", "initial_fen": chess.STARTING_FEN,
            "sparring_color": "white", "opponent_elo": 1320, "result": result,
            "termination": "engine_error", "errors": [{"type": "TimeoutError"}],
            "moves": rows, "uci_moves": list(sequence), "final_fen": board.fen(en_passant="fen")}


class FakeAnalysis:
    def __init__(self, peer, board, limit, options):
        self.peer, self.board, self.limit, self.options = peer, board, limit, options
        self.allowed = options["root_moves"]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def __iter__(self):
        if self.peer.mode == "timeout":
            self.peer.closed_event.wait(1)
            raise chess.engine.EngineTerminatedError("controlled timeout")
        for rank, move in enumerate(self.allowed, 1):
            white_cp = 30 if rank == 1 else 300
            score = chess.engine.Cp(white_cp if self.board.turn else -white_cp)
            if self.peer.mode == "mate" and rank == 1:
                score = chess.engine.Mate(3 if self.board.turn else -3)
            row = {"depth": self.limit.depth, "score": chess.engine.PovScore(score, self.board.turn),
                   "pv": [move], "multipv": rank, "nodes": 1000}
            if self.peer.mode == "different_depth" and rank == 2:
                row["depth"] -= 1
            if self.peer.mode == "bound" and rank == 2:
                row["upperbound"] = True
            if self.peer.mode == "foreign" and rank == 2:
                row["pv"] = [next(candidate for candidate in self.board.legal_moves if candidate not in self.allowed)]
            yield row
    def wait(self):
        return chess.engine.BestMove(self.allowed[0], None)


class FakePeer:
    def __init__(self, mode="normal", hook=None):
        self.mode, self.hook = mode, hook
        self.id = {"name": "Stockfish controlled audit peer"}
        self.options = {key: object() for key in ("Threads", "Hash", "Skill Level", "UCI_LimitStrength", "Clear Hash")}
        self.calls, self.settings = [], []
        self.closed, self.closed_event = False, threading.Event()
        self.transport = SimpleNamespace(get_pid=lambda: 12345)
        self.returncode = SimpleNamespace(result=lambda timeout: 1 if self.closed else None)
    def configure(self, options):
        self.settings.append(dict(options))
    def analysis(self, board, limit, **options):
        self.calls.append({"board": board.copy(stack=True), "limit": limit, "options": options})
        if self.hook:
            self.hook()
        return FakeAnalysis(self, board, limit, options)
    def close(self):
        self.closed = True
        self.closed_event.set()


class CounterfactualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "game-001.json"
        self.binary = self.root / "stockfish.exe"
        self.binary.write_bytes(b"never executed: fixture")
        self.record = fixture_record()
        self.input.write_text(json.dumps(self.record), encoding="utf-8")
        self.peers = []
    def execute(self, mode="normal", hook=None, timeout=1):
        def factory(binary):
            value = FakePeer(mode, hook)
            self.peers.append(value)
            return value
        with contextlib.redirect_stdout(io.StringIO()):
            return audit.run([self.input], self.binary, self.root / "output", factory=factory, timeout=timeout)

    def test_filter_is_fixed_and_does_not_cherry_pick_winners_or_referee_successes(self):
        inputs, cases, errors = audit.collect_targets([self.input, self.root, self.input])
        self.assertEqual(len(inputs), 1)
        self.assertEqual([case["ply"] for case in cases], [1, 3])
        self.assertEqual(errors, [])
        self.assertEqual(cases[1]["referee_reason"], "referee_error")
        self.assertTrue(cases[1]["source_move_late"])
        self.assertTrue(all(case["source_result"] == "0-1" for case in cases))
        won = self.root / "game-002.json"
        won.write_text(json.dumps(fixture_record("1-0")), encoding="utf-8")
        _, all_cases, _ = audit.collect_targets([self.root])
        self.assertEqual(len(all_cases), 4)
        self.assertEqual({case["source_result"] for case in all_cases}, {"1-0", "0-1"})

    def test_window_forced_unchanged_and_opponent_exclusions_are_exact(self):
        row = copy.deepcopy(self.record["moves"][0])
        self.assertTrue(audit.is_target(row))
        for changes in ({"ply": 41}, {"actor": "opponent"}, {"forced_opening": True}):
            self.assertFalse(audit.is_target({**row, **changes}))
        row["decision"]["regicide_proposal"]["selected_uci"] = row["uci"]
        self.assertFalse(audit.is_target(row))

    def test_same_parent_history_two_roots_completed_depth_and_full_strength_are_preserved(self):
        report = self.execute()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["summary"]["all_selected_cases"], 2)
        self.assertEqual(report["summary"]["effect_by_D"], {"improved": 2, "worsened": 0, "tied": 0})
        self.assertEqual(len(self.peers), 1)
        peer = self.peers[0]
        self.assertEqual(peer.settings[0], {"Threads": 1, "Hash": 32, "Skill Level": 20, "UCI_LimitStrength": False})
        self.assertEqual(sum(setting == {"Clear Hash": None} for setting in peer.settings), 2)
        self.assertEqual([len(call["board"].move_stack) for call in peer.calls], [0, 2])
        for case, call in zip(report["cases"], peer.calls):
            self.assertEqual(call["board"].fen(en_passant="fen"), case["parent_fen"])
            self.assertEqual({move.uci() for move in call["options"]["root_moves"]},
                             {case["actual_uci"], case["native_proposal_uci"]})
            self.assertEqual(call["options"]["multipv"], 2)
            self.assertEqual(call["limit"].depth, 16)
        self.assertIsNot(peer.calls[0]["options"]["game"], peer.calls[1]["options"]["game"])
        self.assertTrue(report["inputs_unchanged"] and report["script_unchanged"] and report["binary_unchanged"])
        self.assertTrue(report["all_owned_processes_exited"])

    def test_incomplete_comparison_is_unknown_and_stays_in_all_selected_denominator(self):
        report = self.execute("different_depth")
        self.assertEqual(report["status"], "completed_with_unknowns")
        self.assertEqual(report["summary"]["all_selected_cases"], 2)
        self.assertEqual(report["summary"]["unknown_or_error_pairs"], 2)
        self.assertEqual(report["summary"]["D_improved_fraction_of_all_selected"], 0)

    def test_bounded_and_foreign_root_packets_are_rejected(self):
        board = chess.Board()
        allowed = [chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4")]
        good = {"depth": 16, "multipv": 1, "score": chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE), "pv": [allowed[0]]}
        for wrong in ({**good, "lowerbound": True}, {**good, "depth": True},
                      {**good, "pv": [chess.Move.from_uci("g1f3")]},
                      {**good, "pv": [chess.Move.from_uci("e2e5")]},
                      {**good, "score": chess.engine.PovScore(chess.engine.Cp(30), chess.BLACK)}):
            self.assertIsNone(audit.packet(board, wrong, allowed))

    def test_black_to_move_packet_is_normalized_to_white(self):
        board = chess.Board()
        board.push_uci("e2e4")
        allowed = [chess.Move.from_uci("e7e5"), chess.Move.from_uci("d7d5")]
        value = audit.packet(board, {"depth": 16, "multipv": 1,
            "score": chess.engine.PovScore(chess.engine.Cp(-40), chess.BLACK), "pv": [allowed[0]]}, allowed)
        self.assertEqual(value["cp_white"], 40)

    def test_mate_stays_separate_without_fake_centipawn_comparison(self):
        report = self.execute("mate")
        self.assertEqual(report["summary"]["mate_pairs_separate"], 2)
        self.assertEqual(report["summary"]["exact_finite_pairs"], 0)
        self.assertEqual(report["summary"]["unknown_or_error_pairs"], 0)
        self.assertEqual(report["summary"]["effect_by_D"], {"improved": 0, "worsened": 0, "tied": 0})
        self.assertNotIn("actual_D", report["cases"][0]["comparison"])

    def test_D_and_absolute_closeness_effects_are_distinct_inside_band(self):
        result = audit.compare_packets({"cp_white": 5, "mate_white": None}, {"cp_white": 80, "mate_white": None})
        self.assertEqual(result["effect_by_D"], "tied")
        self.assertEqual(result["effect_by_absolute_cp"], "improved")
        self.assertEqual(result["D_delta_actual_minus_native"], 0)

    def test_timeout_is_unknown_and_every_owned_peer_is_closed(self):
        report = self.execute("timeout", timeout=.02)
        self.assertEqual(report["summary"]["unknown_or_error_pairs"], 2)
        self.assertTrue(all(case["error_type"] == "TimeoutError" for case in report["cases"]))
        self.assertTrue(all(peer.closed for peer in self.peers))

    def test_invalid_full_history_does_not_silently_drop_selected_cases(self):
        self.record["moves"][3]["after_fen"] = "wrong"
        self.input.write_text(json.dumps(self.record), encoding="utf-8")
        report = self.execute()
        self.assertEqual(report["summary"]["all_selected_cases"], 2)
        self.assertEqual(report["summary"]["unknown_or_error_pairs"], 2)
        self.assertTrue(report["input_errors"])
        self.assertEqual(self.peers, [])
        self.assertTrue((self.root / "output" / "case-001.json").is_file())

    def test_input_mutation_is_detected_without_changing_the_original_case_selection(self):
        changed = []
        def hook():
            if not changed:
                self.input.write_text(self.input.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                changed.append(True)
        report = self.execute(hook=hook)
        self.assertFalse(report["inputs_unchanged"])
        self.assertEqual(report["summary"]["all_selected_cases"], 2)
        self.assertEqual(report["status"], "completed_with_unknowns")

    def test_missing_binary_after_evaluation_is_recorded_and_cleanup_still_finishes(self):
        def hook():
            if self.binary.exists():
                self.binary.unlink()
        report = self.execute(hook=hook)
        self.assertIsNone(report["binary_sha256_after"])
        self.assertFalse(report["binary_unchanged"])
        self.assertTrue(report["all_owned_processes_exited"])
        self.assertEqual(report["status"], "completed_with_unknowns")


if __name__ == "__main__":
    unittest.main()
