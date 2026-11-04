"""Native, predeclared tactical-selector diagnostics; never Chess.com grading.

Every case retains its complete available history. Synthetic FEN controls have
no known history before that FEN. Candidate moves are discovered by the real
selector, not injected by this driver. No user game or runtime install changes.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gambit_candidate_probe import configure, safe, validate_packet

FIXTURE_PATH = ROOT / "docs/checks/2026-11-04-gambit-chesscom-target/predeclared-fixtures.json"


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_document():
    # This list was accepted before any native answers were requested.
    rows = [
        {"id": "refutable-legal-nc6", "history_uci": "e2e4 e7e5 g1f3 b8c6 f1c4 d7d6 b1c3 c8g4".split(),
         "reference_candidate_uci": "f3e5", "role": "refutable_conditional_lure",
         "source": "tests/poisoned_piece_tests.py::test_unsound_offer_still_has_forced_punishment_if_queen_is_taken",
         "note": "The opponent can refuse the queen bait with ...Nxe5. Conditional mate after ...Bxd1 is not soundness."},
        {"id": "legal-d6-g6", "history_uci": "e2e4 e7e5 g1f3 d7d6 f1c4 c8g4 b1c3 g7g6".split(),
         "reference_candidate_uci": "f3e5", "role": "existing_tactical_opportunity",
         "source": "tests/poisoned_piece_tests.py::legal_trap; tests/boss_trap_integration.py::LEGAL",
         "note": "Existing legal project example. No claim that a particular response or engine choice is forced."},
        {"id": "damiano", "history_uci": "e2e4 e7e5 g1f3 f7f6".split(),
         "reference_candidate_uci": "f3e5", "role": "declared_tactical_opportunity",
         "source": "Project-authored legal opening fixture requested for this experiment",
         "note": "New declared fixture; reference move is descriptive and never passed into candidate selection."},
        {"id": "synthetic-ray-gain", "initial_fen": "k5r1/5p2/8/8/5N2/1B6/8/7K w - - 0 1", "history_uci": [],
         "reference_candidate_uci": "f4g6", "role": "synthetic_local_exchange_opportunity",
         "source": "tests/poisoned_piece_tests.py::test_pawn_taking_knight_opens_bishop_ray_to_rook",
         "note": "Synthetic local motif, not a played game or an all-defense winning proof."},
        {"id": "synthetic-ray-recapture", "initial_fen": "6rk/5p2/8/8/5N2/1B6/8/7K w - - 0 1", "history_uci": [],
         "reference_candidate_uci": "f4g6", "role": "synthetic_negative_control",
         "source": "tests/poisoned_piece_tests.py::test_rook_gain_is_rejected_when_defender_can_recapture_bishop",
         "note": "The defender can recapture the bishop; the superficially similar payoff is insufficient."},
        {"id": "synthetic-equal-exchange", "initial_fen": "7k/8/8/8/8/1r6/B7/1R5K w - - 0 1", "history_uci": [],
         "reference_candidate_uci": "h1g1", "role": "synthetic_equal_exchange_control",
         "source": "tests/gambit_initiative_tests.py::test_a_complete_equal_exchange_is_not_mislabeled_a_donation",
         "note": "An equal exchange is not a new sound-sacrifice claim. Other native moves remain legal choices."},
        {"id": "already-winning-control", "history_uci": "e2e4 e7e5 g1f3 d8h4 f3h4 b8c6".split(),
         "reference_candidate_uci": "h4g6", "role": "already_winning_unnecessary_sacrifice_control",
         "source": "Project-authored legal history: Black has already lost its queen",
         "note": "New declared control, not a human game. An already winning position is not a special-move grade."},
        {"id": "initial-control", "history_uci": [], "role": "ordinary_position_control",
         "source": "Standard initial position", "note": "No move or geometry outcome is injected."},
    ]
    for row in rows:
        row.setdefault("initial_fen", chess.STARTING_FEN)
        board = chess.Board(row["initial_fen"])
        for uci in row["history_uci"]:
            board.push_uci(uci)
        if not board.is_valid() or board.outcome(claim_draw=False) is not None:
            raise ValueError("Invalid predeclared root: " + row["id"])
        row["before_fen"] = board.fen(en_passant="fen")
        row["history_scope"] = ("complete standard-start history" if row["initial_fen"] == chess.STARTING_FEN
                                else "all available history; earlier history before synthetic FEN is unknown")
        if row.get("reference_candidate_uci") and chess.Move.from_uci(row["reference_candidate_uci"]) not in board.legal_moves:
            raise ValueError("Illegal reference move: " + row["id"])
    return {"schema": "regicide.gambit-tactics-predeclared.v1", "cases": rows,
            "selection": "Eight roots accepted before native execution; all existing development patterns or explicitly marked new authored controls.",
            "candidate_injection": False, "chesscom_grades_evaluated": False,
            "conditions": {"baseline_ms": 1000, "extra_ms": 1000, "classical_piece_activity": True,
                           "Hash": 16, "UseLMR": True},
            "success_definition": "Functional execution and evidence integrity; zero qualified alternatives remains a valid observed outcome, never a claim of tactical improvement."}


def replay(row):
    board = chess.Board(row["initial_fen"])
    for uci in row["history_uci"]:
        board.push_uci(uci)
    if not board.is_valid() or board.fen(en_passant="fen") != row["before_fen"]:
        raise ValueError("Predeclared root disagrees with legal history")
    return board


def run_case(binary, fixture, selector):
    board = replay(fixture)
    original = board.fen(en_passant="fen"), list(board.move_stack)
    row = {"case": fixture, "queries": [], "error": None, "engine_closed": False,
           "process_exit_code": None, "chesscom_grade": "not_evaluated"}
    engine = None
    group = object()
    group_id = 0
    try:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **flags)
        row["engine_id"] = dict(engine.id)
        row["configured_options"] = configure(engine, True)

        def search(budget, *, root_move=None, root_moves=None, fresh=False, depth=None, position=None, publish=False):
            nonlocal group, group_id
            if root_move is not None and root_moves is not None:
                raise ValueError("Conflicting root restrictions")
            if fresh:
                group, group_id = object(), group_id + 1
            current = board if position is None else position
            roots = [root_move] if root_move is not None else root_moves
            query = {"requested_ms": budget, "requested_depth": depth, "game_token_group": group_id,
                     "history_uci": [move.uci() for move in current.move_stack],
                     "initial_fen": current.root().fen(en_passant="fen"),
                     "before_fen": current.fen(en_passant="fen"),
                     "root_moves": [move.uci() for move in roots] if roots is not None else None,
                     "fresh": fresh, "publish": publish, "packets": [], "status": "failed"}
            row["queries"].append(query)
            began = time.monotonic()
            expired = threading.Event()
            def expire():
                expired.set()
                engine.close()
            timer = threading.Timer(max(0, budget) / 1000 + .5, expire)
            timer.daemon = True
            timer.start()
            try:
                packets = {}
                with engine.analysis(current.copy(stack=True), chess.engine.Limit(time=budget / 1000, depth=depth),
                                     root_moves=roots, game=group, info=chess.engine.INFO_ALL) as stream:
                    for packet in stream:
                        query["packets"].append(safe(packet))
                        if validate_packet(current, packet) is not None:
                            packets[packet["depth"]] = packet
                    answer = stream.wait()
                if answer.move not in current.legal_moves or roots is not None and answer.move not in roots:
                    raise ValueError("Native returned an illegal or disallowed root")
                matching = [packet for packet in packets.values() if packet["pv"][0] == answer.move]
                if not matching:
                    raise ValueError("No exact completed legal PV for returned root")
                final = max(matching, key=lambda packet: packet["depth"])
                query.update(status="complete", returned_uci=answer.move.uci(), completed_packet=safe(final))
                return chess.engine.BestMove(answer.move, answer.ponder), final, packets
            except Exception as error:
                query["error"] = f"{type(error).__name__}: {error}"
                raise
            finally:
                timer.cancel()
                timer.join()
                query["elapsed_ms"] = round((time.monotonic() - began) * 1000, 3)
                query["safety_timeout_expired"] = expired.is_set()
                if expired.is_set():
                    query.update(status="failed", error="Native callback safety timeout")
                    raise TimeoutError("Native callback safety timeout")

        baseline_started = time.monotonic()
        baseline, info, packets = search(1000)
        row.update(baseline_uci=baseline.move.uci(), baseline_san=board.san(baseline.move),
                   baseline_info=safe(info), baseline_packets=safe(packets),
                   baseline_elapsed_ms=round((time.monotonic() - baseline_started) * 1000, 3))
        extra_started = time.monotonic()
        detail, discard = selector.choose(board, baseline.move, info, packets, search, engine.close, budget_ms=1000)
        selected = chess.Move.from_uci(detail["selected_uci"])
        row.update(detail=safe(detail), discard_engine=discard, selected_uci=selected.uci(),
                   selected_san=board.san(selected), extra_elapsed_ms=round((time.monotonic() - extra_started) * 1000, 3))
        row["checks"] = {"selected_legal": selected in board.legal_moves,
            "selected_matches_detail": selected.uci() == detail["selected_uci"],
            "unapplied_preserves_baseline": bool(detail.get("applied")) or selected == baseline.move,
            "all_native_queries_complete": all(query["status"] == "complete" and not query["safety_timeout_expired"] for query in row["queries"])}
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if engine is not None:
            try:
                engine.quit()
            except Exception as error:
                row["shutdown_error"] = f"{type(error).__name__}: {error}"
                engine.close()
            try:
                row["process_exit_code"] = engine.returncode.result(timeout=3)
                row["engine_closed"] = True
            except Exception as error:
                row["shutdown_error"] = f"{type(error).__name__}: {error}"
        row["position_unchanged"] = original == (board.fen(en_passant="fen"), list(board.move_stack))
        row["passed"] = (row["error"] is None and row["position_unchanged"] and row["engine_closed"]
                         and row["process_exit_code"] == 0 and all(row.get("checks", {"missing": False}).values()))
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures-only", action="store_true")
    args = parser.parse_args(argv)
    declared = fixture_document()
    if args.fixtures_only:
        print(json.dumps(declared, ensure_ascii=False, indent=2))
        return 0
    if args.binary is None or args.output is None:
        parser.error("Native execution requires --binary and --output; inspect --fixtures-only first")
    import gambit_tactics
    import sound_sacrifice
    args.binary = args.binary.resolve(strict=True)
    fixed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if fixed != declared:
        parser.error("Predeclared fixture document differs from this harness; preserve a separate case/version")
    args.output.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__), Path(gambit_tactics.__file__), Path(sound_sacrifice.__file__),
             ROOT / "scripts/gambit_candidate_probe.py", ROOT / "scripts/sparring_material.py", args.binary, FIXTURE_PATH]
    hashes = {str(path): digest(path) for path in paths}
    (args.output / "input-fixtures.json").write_text(json.dumps(declared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    handler = logging.FileHandler(args.output / "raw-uci.log", encoding="utf-8")
    logger = logging.getLogger("chess.engine")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    report = {"schema": "regicide.gambit-tactics-native.v1", "started_utc": utc(),
        "source_sha256": hashes, "python": platform.python_version(), "python_chess": chess.__version__,
        "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "conditions": declared["conditions"], "selection": declared["selection"],
        "scope": "Actual own-engine selector diagnostics; not host integration, completed games, external grading, Elo, or playing-strength evidence",
        "chesscom_grades_evaluated": False, "offline_audit": "not performed by this harness; full roots and selections retained for independent follow-up",
        "cases": []}
    try:
        for fixture in declared["cases"]:
            row = run_case(args.binary, fixture, gambit_tactics)
            report["cases"].append(row)
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"id": fixture["id"], "passed": row["passed"], "baseline": row.get("baseline_uci"),
                "selected": row.get("selected_uci"), "qualified": row.get("detail", {}).get("qualified"),
                "reason": row.get("detail", {}).get("reason"), "error": row["error"]}), flush=True)
    finally:
        report.update(finished_utc=utc(), sources_unchanged=all(digest(path) == value for path, value in hashes.items()))
        report["all_owned_engines_closed"] = all(row["engine_closed"] for row in report["cases"])
        report["qualified_cases"] = sum(row.get("detail", {}).get("qualified") is True for row in report["cases"])
        report["applied_cases"] = sum(row.get("detail", {}).get("applied") is True for row in report["cases"])
        report["passed"] = (len(report["cases"]) == len(declared["cases"]) and report["sources_unchanged"]
                            and report["all_owned_engines_closed"] and all(row["passed"] for row in report["cases"]))
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        handler.close()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
