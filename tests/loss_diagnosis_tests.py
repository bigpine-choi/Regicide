"""Loss evidence and diagnostic-harness regressions; no playing-strength oracle.

Run pure checks: python -m unittest discover -s tests -p loss_diagnosis_tests.py
Include cheap real-process smoke: python tests/loss_diagnosis_tests.py \
    --binary path/to/regicide --probe path/to/regicide_evaluation_probe
The historical bad moves are evidence, never expected best-move assertions.
"""

import argparse
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import probe_loss_search as TOOL
import reference_loss_scan as REFERENCE

ARCHIVE = ROOT / "docs/matches/2026-10-18-lmr-idle-rematch"
SOURCE = ARCHIVE / "match-200ms.json"
CORPUS = ROOT / "tests/data/lmr_loss_positions_2026-10-18.json"
LOSS_IDS = [1, 3, 9, 13, 16]
REAL_BINARY = None
REAL_PROBE = None
CYCLE = ["g1f3", "g8f6", "f3g1", "f6g8"] * 2


def search_text(**changes):
    fields = {"bestmove": "e2e4", "score_cp": 17, "depth": 2, "nodes": 20,
              "time_ms": 0, "qnodes": 10, "seldepth": 3, "tt_probes": 12,
              "tt_hits": 3, "tt_cutoffs": 1, "tt_stores": 11,
              "tt_allocations": 1, "tt_reuses": 0, "tt_clears": 0, "tt_bytes": 1024,
              "see_calls": 0, "stopped": 0, "pv": "e2e4 e7e5"}
    for key, value in changes.items():
        if value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    return "".join(f"{name}: {value}\n" for name, value in fields.items())


def static_text(board, *, repetitions=1, **changes):
    outcome = board.outcome(claim_draw=False)
    fields = dict(zip(TOOL.STATIC_FIELDS, [
        board.fen(en_passant="fen"), "w" if board.turn else "b", 0, 0, 0, 0, 0,
        board.legal_moves.count(), int(board.is_check()),
        int(outcome is None and board.is_repetition(3)),
        int(outcome is None and board.is_fifty_moves()),
        TOOL.OUTCOME_NAMES[outcome.termination] if outcome else "ongoing", repetitions]))
    fields.update(changes)
    return "\t".join(str(fields[name]) for name in TOOL.STATIC_FIELDS) + "\n"


class DiagnosticParserTests(unittest.TestCase):
    def test_replay_preserves_claim_history_and_rejects_invalid_or_postterminal_moves(self):
        board = TOOL.replay_position(chess.STARTING_FEN, CYCLE)
        self.assertEqual(len(board.move_stack), 8)
        self.assertTrue(board.is_repetition(3))
        self.assertFalse(chess.Board(board.fen()).is_repetition(3))
        invalid = [(chess.STARTING_FEN, ["e2e5"], None),
                   (chess.STARTING_FEN, ["0000"], None),
                   (chess.STARTING_FEN, [], board.fen()),
                   ("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1", ["h8h7"], None)]
        for fen, history, expected in invalid:
            with self.subTest(history=history), self.assertRaises(ValueError):
                TOOL.replay_position(fen, history, expected)

    def test_cp_mate_sign_and_missing_bound_flags_remain_explicit(self):
        board = chess.Board()
        value = TOOL.parse_search_output(search_text(), board, 20, 2)
        self.assertEqual((value["score_cp"], value["score_mate"], value["score_pov"]),
                         (17, None, "mover"))
        self.assertTrue(value["depth_completed"])
        self.assertTrue(value["node_limit_reached"])
        self.assertIn("no bound flags", value["score_report"])
        self.assertNotIn("lmr_reductions", value)
        black = chess.Board(chess.STARTING_FEN.replace(" w ", " b "))
        self.assertEqual(TOOL.parse_search_output(
            search_text(bestmove="e7e5", pv="e7e5 e2e4", score_cp=-17), black)["score_cp"], -17)
        for mate in (-3, 0, 3):
            value = TOOL.parse_search_output(search_text(score_cp=None, score_mate=mate), board)
            self.assertIsNone(value["score_cp"])
            self.assertEqual(value["score_mate"], mate)
            self.assertEqual(value["mate_zero_sign_ambiguous"], mate == 0)
        limited = TOOL.parse_search_output(search_text(depth=0, stopped=1, pv=""), board, 20, 2)
        self.assertFalse(limited["depth_completed"])
        self.assertTrue(limited["stopped"])

    def test_cli_rejects_corrupt_partial_illegal_and_overbudget_replies(self):
        cases = ["banner\n" + search_text(), search_text() + "nodes: 20\n",
                 search_text(nodes=None), search_text(score_mate=2), search_text(score_cp="1.5"),
                 search_text(stopped=2), search_text(nodes=-1), search_text(qnodes=21),
                 search_text(tt_hits=13), search_text(lmr_reductions=1, lmr_researches=2),
                 search_text(pvs_probes=1, pvs_researches=2), search_text(pv="d2d4"),
                 search_text(pv="e2e4 e7e6 e4e6"), search_text(bestmove="0000", pv=""),
                 search_text(nodes=21), search_text(depth=3)]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                TOOL.parse_search_output(text, chess.Board(), 20, 2)

    def test_terminal_reply_and_legal_pv_endpoint(self):
        board = chess.Board()
        parsed = TOOL.parse_search_output(search_text(), board)
        endpoint = TOOL.replay_position(chess.STARTING_FEN, ["e2e4", "e7e5"])
        self.assertEqual(parsed["pv_end_fen"], endpoint.fen(en_passant="fen"))
        terminal = chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
        value = TOOL.parse_search_output(search_text(bestmove="0000", pv="", score_cp=None,
                                                   score_mate=0, depth=0), terminal)
        self.assertIsNone(value["bestmove"])
        self.assertEqual(value["pv"], [])

    def test_static_parser_checks_history_and_keeps_four_configurations_separate(self):
        board = TOOL.replay_position(chess.STARTING_FEN, CYCLE)
        text = static_text(board, repetitions=3, eval_shield_on_structure_on_cp=5,
                           eval_shield_on_structure_off_cp=3,
                           eval_shield_off_structure_on_cp=4)
        row = TOOL.parse_static_probe(text, [board])[0]
        self.assertEqual(row["repetitions"], 3)
        self.assertTrue(row["claim_threefold_now"])
        self.assertEqual([row[key] for key in TOOL.STATIC_SCORE_FIELDS], [0, 5, 3, 4, 0])
        self.assertEqual(row["score_pov"], "mover")
        invalid = [text.rstrip("\n") + "\textra\n", text.replace("\tw\t", "\tx\t"),
                   static_text(board, repetitions=3, in_check=2),
                   static_text(board, repetitions=3, legal_count=0),
                   static_text(board, repetitions=3, claim_threefold_now=0),
                   static_text(board, repetitions=3, outcome="unknown")]
        for value in invalid:
            with self.subTest(text=value), self.assertRaises(ValueError):
                TOOL.parse_static_probe(value, [board])
        with self.assertRaises(ValueError):
            TOOL.parse_static_probe(text, [chess.Board(board.fen())])

    def test_fixed_work_equivalence_ignores_clock_and_added_lmr_fields_only(self):
        left = TOOL.parse_search_output(search_text())
        right = TOOL.parse_search_output(search_text(time_ms=3, lmr_reductions=0, lmr_researches=0))
        self.assertEqual(TOOL.search_graph_differences(left, right), [])
        right["nodes"] += 1
        self.assertEqual(TOOL.search_graph_differences(left, right), ["nodes"])

    def test_process_failure_and_malformed_stdout_preserve_raw_evidence(self):
        case = {"initial_fen": chess.STARTING_FEN, "history": [], "fen": chess.STARTING_FEN}
        query = {"depth": 2, "nodes": 20}
        responses = [subprocess.CompletedProcess([], 1, "partial output\n", "failure\n"),
                     subprocess.CompletedProcess([], 0, "invalid output\n", "")]
        for response in responses:
            with self.subTest(returncode=response.returncode), \
                    patch.object(TOOL.subprocess, "run", return_value=response):
                record = TOOL.execute_search(Path("never-executed"), ["--no-lmr"], case, query)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["stdout"], response.stdout)
            self.assertEqual(record["stderr"], response.stderr)
            self.assertIn("--no-lmr", record["command"])

    def test_static_root_pov_sign_and_failed_probe_preserve_raw_evidence(self):
        case = {"initial_fen": chess.STARTING_FEN, "history": [], "fen": chess.STARTING_FEN}
        child = TOOL.replay_position(chess.STARTING_FEN, ["e2e4"])
        requests = [{"id": "child", "continuation": ["e2e4"]}]
        reply = subprocess.CompletedProcess([], 0, static_text(child, material_cp=-17), "")
        with patch.object(TOOL.subprocess, "run", return_value=reply):
            record = TOOL.static_observations(Path("never-executed"), case, requests)
        self.assertEqual(record["status"], "complete")
        observation = record["observations"][0]
        self.assertEqual(observation["root_pov_sign"], -1)
        self.assertEqual(observation["root_pov_scores"]["material_cp"], 17)
        self.assertIn("not an asserted searched leaf", observation["interpretation"])
        reply = subprocess.CompletedProcess([], 1, "partial TSV\n", "line 2: failure\n")
        with patch.object(TOOL.subprocess, "run", return_value=reply):
            record = TOOL.static_observations(Path("never-executed"), case, requests)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["stdout"], reply.stdout)
        self.assertEqual(record["stderr"], reply.stderr)

    def test_diagnostic_output_cannot_replace_input_or_existing_result(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            source.write_text("unchanged", encoding="utf-8")
            args = [flag for name in ("screen", "baseline", "candidate", "probe")
                    for flag in ("--" + name, str(source))]
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                TOOL.arguments([*args, "--output", str(source)])
            output = Path(folder) / "result.json"
            output.write_text("old evidence", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                TOOL.arguments([*args, "--output", str(output)])
            self.assertEqual(output.read_text(encoding="utf-8"), "old evidence")


def reference_score(cp=None, mate=None, *, upperbound=False, lowerbound=False):
    kind, value = ("cp", cp) if cp is not None else ("mate", mate)
    suffix = " upperbound" if upperbound else " lowerbound" if lowerbound else ""
    event = REFERENCE.parse_score_event(f"info depth 8 score {kind} {value}{suffix} nodes 50000")
    return {"score": {"cp": cp, "mate": mate}, "upperbound": upperbound,
            "lowerbound": lowerbound, "final_score_event": event, "score_events_observed": 1}


def decision(game, ply, best_cp=0, forced_cp=-100):
    best, forced = reference_score(cp=best_cp), reference_score(cp=forced_cp)
    return {"id": f"g{game}-p{ply}", "game": game, "ply": ply, "best": best,
            "forced_played": forced, "cp_gap": REFERENCE.cp_gap(best, forced)}


class ReferenceBoundCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_accumulated_python_chess_bound_is_reproduced_and_latest_raw_event_corrects_it(self):
        for old_bound in ("lowerbound", "upperbound"):
            accumulated = chess.engine.AnalysisResult()
            accumulated.post({"score": chess.engine.PovScore(chess.engine.Cp(10), chess.WHITE),
                              old_bound: True})
            accumulated.post({"score": chess.engine.PovScore(chess.engine.Cp(25), chess.WHITE)})
            self.assertIs(accumulated.info[old_bound], True)
            event = REFERENCE.parse_score_event("info depth 9 score cp 25 nodes 50000 pv e2e4")
            corrected = REFERENCE.apply_score_event(accumulated.info, event, chess.WHITE)
            self.assertFalse(corrected["lowerbound"])
            self.assertFalse(corrected["upperbound"])
            self.assertIs(accumulated.info[old_bound], True)  # correction copies rather than mutates
            self.assertEqual(corrected["score"].white().score(), 25)


class ReferenceEvidenceTests(unittest.TestCase):
    def test_raw_score_captures_cp_mate_bounds_together_and_rejects_ambiguity(self):
        for kind, value, bound in (("cp", -45, "lowerbound"), ("mate", -3, "upperbound"),
                                   ("mate", 0, "")):
            raw = f"info multipv 1 depth 6 score {kind} {value} {bound} nodes 100"
            event = REFERENCE.parse_score_event(raw)
            score = chess.engine.Cp(value) if kind == "cp" else chess.engine.Mate(value)
            info = {"score": chess.engine.PovScore(score, chess.BLACK),
                    "lowerbound": True, "upperbound": True}
            corrected = REFERENCE.apply_score_event(info, event, chess.BLACK)
            self.assertEqual(corrected["lowerbound"], bound == "lowerbound")
            self.assertEqual(corrected["upperbound"], bound == "upperbound")
            self.assertEqual(event["raw_info"], raw)
        for ignored in ("bestmove e2e4", "info nodes 5", "info string score cp 12 lowerbound",
                        "info multipv 2 score cp 12"):
            self.assertIsNone(REFERENCE.parse_score_event(ignored))
        for malformed in ("info score", "info score cp", "info score cp text",
                          "info score pawns 12", "info score cp 12 lowerbound upperbound"):
            with self.subTest(raw=malformed), self.assertRaises(ValueError):
                REFERENCE.parse_score_event(malformed)
        info = {"score": chess.engine.PovScore(chess.engine.Cp(45), chess.BLACK)}
        with self.assertRaises(ValueError):
            REFERENCE.apply_score_event(info, REFERENCE.parse_score_event("info score cp 44"), chess.BLACK)
        with self.assertRaises(ValueError):
            REFERENCE.apply_score_event(info, None, chess.BLACK)

    def test_reference_gaps_require_exact_cp_and_never_convert_mate_to_cp(self):
        best, forced = reference_score(cp=50), reference_score(cp=-100)
        self.assertEqual(REFERENCE.cp_gap(best, forced), 150)
        for left, right in ((reference_score(cp=50, lowerbound=True), forced),
                            (best, reference_score(cp=-100, upperbound=True)),
                            (reference_score(mate=3), forced),
                            (best, reference_score(mate=-3))):
            self.assertIsNone(REFERENCE.cp_gap(left, right))
        for best, expected in ((reference_score(cp=0), True), (reference_score(cp=-1), False),
                               (reference_score(mate=2), True), (reference_score(mate=0), False),
                               (reference_score(mate=-2), False)):
            row = {"best": best, "forced_played": reference_score(mate=-3), "cp_gap": None}
            self.assertEqual(REFERENCE.significant(row), expected)
        bounded = {"best": reference_score(cp=500, lowerbound=True),
                   "forced_played": forced, "cp_gap": None}
        self.assertFalse(REFERENCE.significant(bounded))

    def test_selection_is_earliest_then_largest_earliest_tie_at_most_two_and_deduplicated(self):
        records = [decision(game, ply, forced_cp=-gap) for game in LOSS_IDS
                   for ply, gap in ((10, 100), (20, 250), (30, 250))]
        selected = REFERENCE.select_records(list(reversed(records)))
        self.assertEqual([(row["game"], row["ply"]) for row in selected],
                         [(game, ply) for game in LOSS_IDS for ply in (10, 20)])
        singleton = [decision(game, 10) for game in LOSS_IDS]
        selected = REFERENCE.select_records(singleton)
        self.assertEqual(len(selected), 5)
        self.assertTrue(all(len(row["selection_reasons"]) == 2 for row in selected))
        falling = [decision(game, 10, best_cp=-400, forced_cp=-600) for game in LOSS_IDS]
        self.assertTrue(all("fallback" in row["selection_reasons"][0]
                            for row in REFERENCE.select_records(falling)))
        no_finite = [dict(decision(game, 10), best=reference_score(mate=-3),
                          forced_played=reference_score(mate=-2), cp_gap=None) for game in LOSS_IDS]
        with self.assertRaises(ValueError):
            REFERENCE.select_records(no_finite)

    def test_confirm_rejects_partial_old_bound_policy_and_inconsistent_raw_event(self):
        source = json.loads(SOURCE.read_text(encoding="utf-8"))
        games = REFERENCE.source_games(source)
        records = REFERENCE.primary_records(games)
        for row in records:
            row.update(best=reference_score(cp=0), forced_played=reference_score(cp=-10),
                       cp_gap=10, status="complete")
        selected = REFERENCE.select_records(records)
        report = {"source": {"sha256": TOOL.digest(SOURCE)},
                  "reference": {"binary_sha256": "a" * 64}}
        screen = {"phase": "screen", "status": "complete", "exit_code": 0, "errors": [],
                  "source": copy.deepcopy(report["source"]),
                  "reference": {**report["reference"], "validated_options": REFERENCE.REFERENCE_OPTIONS},
                  "selection_policy": REFERENCE.SELECTION_POLICY,
                  "score_bounds_policy": REFERENCE.SCORE_BOUNDS_POLICY,
                  "records": records, "selected": selected,
                  "selected_ids": [row["id"] for row in selected],
                  "auxiliary": [REFERENCE.auxiliary_record(games)]}
        checked, _ = REFERENCE.checked_screen(screen, report, games)
        self.assertEqual([row["id"] for row in checked], screen["selected_ids"])
        for changes in ({"status": "running"}, {"exit_code": 2}, {"errors": ["failed"]},
                        {"score_bounds_policy": "old accumulated dictionary flags"}):
            bad = {**screen, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                REFERENCE.checked_screen(bad, report, games)
        bad = copy.deepcopy(screen)
        bad["records"][0]["status"] = "running"
        with self.assertRaises(ValueError):
            REFERENCE.checked_screen(bad, report, games)
        bad = copy.deepcopy(screen)
        bad["records"][0]["best"]["final_score_event"]["raw_info"] = \
            "info depth 8 score cp 0 lowerbound nodes 50000"
        with self.assertRaises(ValueError):
            REFERENCE.checked_screen(bad, report, games)


class LossSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(SOURCE.read_text(encoding="utf-8"))

    def test_admissible_source_hashes_and_exact_five_losses(self):
        hashes = {"match-200ms.json": "bd8b540ba276ea4673dfaabbb8f0bab09910735e0c30e310832d814d042dd38a",
                  "match-200ms.pgn": "f7db2cf93aa61f32e5a4f43d5317eef05013058602763e8a70e9da3449d6c483"}
        for name, expected in hashes.items():
            self.assertEqual(hashlib.sha256((ARCHIVE / name).read_bytes()).hexdigest(), expected)
        provenance = json.loads((ARCHIVE / "provenance.json").read_text(encoding="utf-8"))
        self.assertTrue(provenance["primary_result_admissible"])
        self.assertEqual(provenance["attempt"], 2)
        self.assertEqual(len(self.document["games"]), 16)
        self.assertEqual([game["game"] for game in self.document["games"]
                          if game["completed"] and game["candidate_points"] == 0], LOSS_IDS)

    def test_loss_pgn_and_every_selected_history_and_pv_replay_legally(self):
        with (ARCHIVE / "match-200ms.pgn").open(encoding="utf-8") as stream:
            games = [chess.pgn.read_game(stream) for _ in range(16)]
            self.assertIsNone(chess.pgn.read_game(stream))
        for source in self.document["games"]:
            if source["game"] not in LOSS_IDS:
                continue
            pgn = games[source["game"] - 1]
            self.assertFalse(pgn.errors)
            self.assertEqual([move.uci() for move in pgn.mainline_moves()], source["uci_moves"])
            self.assertEqual(pgn.headers["Result"], source["result"])
            board = chess.Board(source["initial_fen"])
            searches = {row["ply"]: row for row in source["search_moves"]}
            for ply, text in enumerate(source["uci_moves"], 1):
                if ply in searches:
                    row = searches[ply]
                    self.assertEqual(row["fen_before"], board.fen(en_passant="fen"))
                    self.assertEqual(row["move"], text)
                    self.assertEqual(row["role"], source["white"] if board.turn else source["black"])
                    score = row["score"]
                    self.assertEqual(score["pov"], "mover")
                    self.assertNotEqual(score["cp"] is None, score["mate"] is None)
                    self.assertIs(score["lowerbound"], False)
                    self.assertIs(score["upperbound"], False)
                    pv_board = board.copy(stack=True)
                    for pv_move in row["pv"]:
                        move = chess.Move.from_uci(pv_move)
                        self.assertIn(move, pv_board.legal_moves)
                        pv_board.push(move)
                move = chess.Move.from_uci(text)
                self.assertIn(move, board.legal_moves)
                board.push(move)
            self.assertEqual(board.fen(en_passant="fen"), source["final_fen"])
            self.assertEqual(board.result(claim_draw=True), source["result"])

    def test_committed_positions_trace_to_source_without_pinning_bad_move_as_best(self):
        if not CORPUS.exists():
            self.skipTest("diagnostic corpus has not been generated yet; rerun after generation")
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        self.assertEqual(corpus["schema_version"], 1)
        self.assertEqual(corpus["loss_game_ids"], LOSS_IDS)
        self.assertEqual(corpus["source"]["path"], SOURCE.relative_to(ROOT).as_posix())
        self.assertEqual(corpus["source"]["sha256"], hashlib.sha256(SOURCE.read_bytes()).hexdigest())
        self.assertTrue(corpus["records"])
        primary_games = set()
        ids = set()
        for item in corpus["records"]:
            self.assertNotIn(item["id"], ids)
            ids.add(item["id"])
            source = self.document["games"][item["game"] - 1]
            self.assertEqual(item["initial_fen"], source["initial_fen"])
            self.assertEqual(item["history"], source["uci_moves"][:item["ply"] - 1])
            self.assertEqual(item["played"], source["uci_moves"][item["ply"] - 1])
            board = TOOL.replay_position(item["initial_fen"], item["history"], item["fen"])
            self.assertEqual(item["candidate_color"], "w" if board.turn else "b")
            self.assertEqual(item["san"], board.san(chess.Move.from_uci(item["played"])))
            self.assertIsInstance(item["classification"], str)
            goal = item["quality_goal"]
            self.assertIn(goal["status"], ("unresolved", "search_budget_recovers", "not_selected"))
            self.assertIsInstance(goal["note"], str)
            for move in goal["target_moves"]:
                self.assertIn(chess.Move.from_uci(move), board.legal_moves)
            if item["game"] in LOSS_IDS:
                primary_games.add(item["game"])
        self.assertEqual(primary_games, set(LOSS_IDS))


class RealDiagnosticSmoke(unittest.TestCase):
    def test_actual_core_replays_every_committed_diagnostic_position_and_keeps_pv_legal(self):
        if REAL_BINARY is None or REAL_PROBE is None:
            self.skipTest("--binary and --probe not supplied")
        if not CORPUS.exists():
            self.skipTest("diagnostic corpus has not been generated yet; rerun after generation")
        cases = json.loads(CORPUS.read_text(encoding="utf-8"))["records"]
        self.assertTrue(cases)
        boards = [TOOL.replay_position(item["initial_fen"], item["history"], item["fen"])
                  for item in cases]
        text = "".join(item["initial_fen"] + "\t" + " ".join(item["history"]) + "\n" for item in cases)
        response = subprocess.run([str(REAL_PROBE)], input=text, capture_output=True, text=True,
                                  encoding="utf-8", timeout=30)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(response.stderr, "")
        self.assertEqual(len(TOOL.parse_static_probe(response.stdout, boards)), len(cases))
        for item, board in zip(cases, boards):
            with self.subTest(case=item["id"]):
                command = [str(REAL_BINARY), "search", "2", "--nodes", "5000", "--no-lmr",
                           "--fen", item["initial_fen"], "--play", *item["history"]]
                response = subprocess.run(command, capture_output=True, text=True,
                                          encoding="utf-8", timeout=30)
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertEqual(response.stderr, "")
                parsed = TOOL.parse_search_output(response.stdout, board, 5000, 2)
                self.assertEqual(parsed["lmr_reductions"], 0)
                self.assertEqual(parsed["lmr_researches"], 0)

    def test_actual_cli_full_history_and_depth_zero_cancellation_parse(self):
        if REAL_BINARY is None:
            self.skipTest("--binary not supplied")
        board = TOOL.replay_position(chess.STARTING_FEN, CYCLE)
        for nodes in (1, 5000):
            command = [str(REAL_BINARY), "search", "2", "--nodes", str(nodes),
                       "--fen", chess.STARTING_FEN, "--no-lmr", "--play", *CYCLE]
            response = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(response.returncode, 0, response.stderr)
            self.assertEqual(response.stderr, "")
            parsed = TOOL.parse_search_output(response.stdout, board, nodes, 2)
            self.assertEqual(parsed["lmr_reductions"], 0)
            if nodes == 1:
                self.assertTrue(parsed["stopped"])
                self.assertFalse(parsed["depth_completed"])

    def test_actual_static_probe_history_claims_turn_and_four_configurations(self):
        if REAL_PROBE is None:
            self.skipTest("--probe not supplied")
        examples = [(chess.STARTING_FEN, []), (chess.STARTING_FEN, CYCLE),
                    ("7k/8/8/8/8/8/4Q3/6K1 w - - 0 1", []),
                    ("7k/8/8/8/8/8/4Q3/6K1 b - - 0 1", []),
                    ("7k/8/8/3P4/8/8/8/7K w - - 100 1", []),
                    ("rnbqkbnr/pppppppp/8/8/8/8/5P1P/6K1 w kq - 0 1", [])]
        boards = [TOOL.replay_position(fen, history) for fen, history in examples]
        text = "".join(fen + "\t" + " ".join(history) + "\n" for fen, history in examples)
        response = subprocess.run([str(REAL_PROBE)], input=text, capture_output=True, text=True,
                                  encoding="utf-8", timeout=20)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(response.stderr, "")
        rows = TOOL.parse_static_probe(response.stdout, boards)
        self.assertEqual([rows[0][key] for key in TOOL.STATIC_SCORE_FIELDS], [0] * 5)
        self.assertEqual(rows[1]["repetitions"], 3)
        self.assertTrue(rows[1]["claim_threefold_now"])
        self.assertEqual(rows[2]["material_cp"], 900)
        for key in TOOL.STATIC_SCORE_FIELDS:
            self.assertEqual(rows[2][key], -rows[3][key])
        self.assertTrue(rows[4]["claim_fifty_moves_now"])
        self.assertNotEqual(rows[4]["eval_shield_off_structure_on_cp"],
                            rows[4]["eval_shield_off_structure_off_cp"])
        self.assertNotEqual(rows[5]["eval_shield_on_structure_off_cp"],
                            rows[5]["eval_shield_off_structure_off_cp"])

    def test_actual_static_probe_fails_at_invalid_line_and_preserves_prior_output(self):
        if REAL_PROBE is None:
            self.skipTest("--probe not supplied")
        good = chess.STARTING_FEN + "\t\n"
        malformed = [chess.STARTING_FEN, chess.STARTING_FEN + "\t\t",
                     chess.STARTING_FEN + "\te2e5", "invalid FEN\t"]
        for bad in malformed:
            response = subprocess.run([str(REAL_PROBE)], input=good + bad + "\n" + good,
                                      capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(response.returncode, 1)
            self.assertIn("line 2:", response.stderr)
            self.assertEqual(len(TOOL.parse_static_probe(response.stdout, [chess.Board()])), 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--probe", type=Path)
    args, remaining = parser.parse_known_args()
    REAL_BINARY = args.binary.resolve(strict=True) if args.binary else None
    REAL_PROBE = args.probe.resolve(strict=True) if args.probe else None
    unittest.main(argv=[sys.argv[0], *remaining])
