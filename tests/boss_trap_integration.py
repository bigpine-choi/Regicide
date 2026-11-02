"""Small fixed opening-trap conversion suite; this does not estimate Elo.

The authored continuations below are legal examples, not forced opening lines.
Expected moves are enumerated independently with python-chess before Regicide
is queried. Every declared case is reported, including failures. No engine,
model, repertoire, or player state is modified.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_engine import resolve_runtime_engine

ENGLUND = "d4 e5 dxe5 Nc6 Nf3 Qe7 Bf4 Qb4+ Bd2 Qxb2 Bc3 Bb4 Qd2 Bxc3 Qxc3 Qc1#"
STAFFORD = "e4 e5 Nf3 Nf6 Nxe5 Nc6 Nxc6 dxc6 d3 Bc5 Bg5 Nxe4 Bxd8 Bxf2+ Ke2 Bg4#"
BUDAPEST = "d4 Nf6 c4 e5 dxe5 Ng4 Nf3 Nc6 Bf4 Bb4+ Nbd2 Qe7 a3 Ngxe5 axb4 Nd3#"
LEGAL = "e4 e5 Nf3 d6 Bc4 Bg4 Nc3 g6 Nxe5 Bxd1 Bxf7+ Ke7 Nd5#"

# Fix the suite before seeing the native engine's answers. The source links
# identify opening/pattern definitions; the oracle verifies these actual boards.
CASES = (
    ("englund-mate-one", ENGLUND, 15, "mate_in_one", "https://www.chess.com/openings/englund-gambit"),
    ("stafford-mate-two", STAFFORD, 13, "mate_in_two", "https://www.chess.com/openings/Petrovs-Defense-Classical-Stafford-Gambit"),
    ("stafford-mate-one", STAFFORD, 15, "mate_in_one", "https://www.chess.com/openings/Petrovs-Defense-Classical-Stafford-Gambit"),
    ("budapest-smothered-mate", BUDAPEST, 15, "mate_in_one", "https://www.chess.com/openings/Budapest-Gambit"),
    ("legal-mate-two", LEGAL, 10, "mate_in_two", "https://www.chess.com/terms/legals-mate-chess"),
    ("legal-mate-one", LEGAL, 12, "mate_in_one", "https://www.chess.com/terms/legals-mate-chess"),
    ("englund-avoid-greedy-recapture", ENGLUND, 14, "avoid_mate_in_one", "https://www.chess.com/openings/englund-gambit"),
    ("budapest-avoid-bishop-bait", BUDAPEST, 14, "avoid_mate_in_one", "https://www.chess.com/openings/Budapest-Gambit"),
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mate_one_moves(board):
    result = []
    for move in list(board.legal_moves):
        board.push(move)
        if board.is_checkmate():
            result.append(move.uci())
        board.pop()
    return result


def mate_two_moves(board):
    """All moves that mate now or force mate after every one-ply defense."""
    result = []
    for move in list(board.legal_moves):
        board.push(move)
        forced = board.is_checkmate()
        replies = list(board.legal_moves)
        if replies and not board.is_game_over(claim_draw=True):
            forced = True
            for reply in replies:
                board.push(reply)
                if not mate_one_moves(board):
                    forced = False
                board.pop()
                if not forced:
                    break
        board.pop()
        if forced:
            result.append(move.uci())
    return result


def avoiding_mate_one(board):
    result = []
    for move in list(board.legal_moves):
        board.push(move)
        if not mate_one_moves(board):
            result.append(move.uci())
        board.pop()
    return result


def fixtures():
    result = []
    for case_id, san_text, offset, criterion, url in CASES:
        complete = chess.Board()
        for san in san_text.split():
            complete.push_san(san)
        if not complete.is_checkmate():
            raise AssertionError("Authored terminal line is not mate: " + case_id)
        board = chess.Board()
        for move in complete.move_stack[:offset]:
            board.push(move)
        oracle = {"mate_in_one": mate_one_moves, "mate_in_two": mate_two_moves,
                  "avoid_mate_in_one": avoiding_mate_one}[criterion]
        expected = sorted(oracle(board))
        if not expected:
            raise AssertionError("Empty independent solution set: " + case_id)
        result.append((board, {"id": case_id, "criterion": criterion, "source_url": url,
                               "source_scope": "opening or mating pattern definition",
                               "authorship": "project-authored legal continuation and fixture selection",
                               "history_uci": [m.uci() for m in board.move_stack],
                               "fen": board.fen(en_passant="fen"),
                               "expected_uci": expected,
                               "oracle": "all legal moves; mate-in-two checks every legal defense"}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--movetime-ms", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures-only", action="store_true")
    args = parser.parse_args()
    if args.movetime_ms <= 0:
        parser.error("movetime must be positive")
    if not args.fixtures_only and args.output is None:
        parser.error("native runs require --output so failures remain recorded")
    if args.output and args.output.exists():
        parser.error("output already exists; use a fresh report path")
    started = datetime.now(timezone.utc).isoformat()
    cases = fixtures()
    if args.fixtures_only:
        print(json.dumps({"fixtures": [row for _, row in cases]}, indent=2))
        return 0
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    report = {"schema": "regicide.boss.trap-diagnostic.v1", "started_utc": started,
              "binary": str(binary), "binary_sha256": sha(binary),
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "driver_sha256": sha(__file__), "python": platform.python_version(),
              "python_chess": chess.__version__, "platform": platform.platform(),
              "movetime_ms": args.movetime_ms,
              "execution": "fresh hidden native engine per case, sequential; no opening proposals or screen helpers",
              "limitations": ["Small authored development suite, not a held-out strength benchmark or Elo estimate.",
                              "Avoid-mate cases test one-ply mating danger only, not overall best-move quality.",
                              "Requested time is a search limit; a solved mate may finish early."],
              "cases": []}
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for board, fixture in cases:
        row = dict(fixture)
        try:
            with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **creation) as engine:
                options = {"UsePieceActivity": True} if "UsePieceActivity" in engine.options else {}
                engine.configure(options)
                row["engine_id"], row["configured_options"] = dict(engine.id), options
                row["default_options"] = {name: option.default for name, option in engine.options.items()}
                began = time.monotonic()
                reply = engine.play(board.copy(stack=True), chess.engine.Limit(time=args.movetime_ms / 1000),
                                    game=object(), info=chess.engine.INFO_ALL)
                row["wall_ms"] = round((time.monotonic() - began) * 1000, 3)
                move = reply.move
                row["selected_uci"] = move.uci() if move else None
                row["legal"] = move in board.legal_moves if move else False
                row["selected_san"] = board.san(move) if row["legal"] else None
                row["depth"], row["nodes"] = reply.info.get("depth"), reply.info.get("nodes")
                score = reply.info.get("score")
                row["mate_score"] = score.pov(board.turn).mate() if score else None
                row["cp_score"] = score.pov(board.turn).score() if score else None
                row["pv_uci"] = [m.uci() for m in reply.info.get("pv", [])]
                pv_board, pv_legal = board.copy(stack=True), True
                for pv_move in reply.info.get("pv", []):
                    if pv_move not in pv_board.legal_moves:
                        pv_legal = False
                        break
                    pv_board.push(pv_move)
                row["pv_legal"] = pv_legal
                row["passed"] = row["legal"] and pv_legal and row["selected_uci"] in row["expected_uci"]
        except Exception as exc:
            row.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        report["cases"].append(row)
        print(json.dumps({"id": row["id"], "passed": row["passed"],
                          "selected_uci": row.get("selected_uci"), "expected_uci": row["expected_uci"]}), flush=True)
    report["passed"] = sum(row["passed"] for row in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    if sha(binary) != report["binary_sha256"]:
        raise AssertionError("Native engine changed during the diagnostic")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
