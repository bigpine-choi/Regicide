"""Actual native post-sacrifice operations in isolated before/after snapshots.

Start prefixes are declared legal fixtures, never attributed to the engine.
Both players then use native Regicide. Stockfish audits only already played
moves. Capped branches stay unfinished and do not measure playing strength.
"""
import argparse
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "docs/checks/2026-11-04-gambit-operations"
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def load_support(path):
    spec = importlib.util.spec_from_file_location("operations_native_support", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def balance(board, owner):
    return sum((1 if piece.color == owner else -1) * VALUES[piece.piece_type]
               for piece in board.piece_map().values())


def features(board, owner, dynamic_play):
    result = dict(dynamic_play._features(board, owner))
    square = board.king(owner)
    ring = chess.SquareSet(chess.BB_KING_ATTACKS[square] | chess.BB_SQUARES[square]) if square is not None else []
    result["king_ring_enemy_attacks"] = sum(len(board.attackers(not owner, target)) for target in ring)
    result["material_balance_cp"] = balance(board, owner)
    return result


def board_for(case):
    board = chess.Board()
    for uci in case["history_uci"]:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("Illegal declared prefix " + case["id"])
        board.push(move)
    if board.fen(en_passant="fen") != case["initial_fen"]:
        raise ValueError("Declared FEN disagrees with full prefix")
    owner = case["color"] == "white"
    if not board.is_valid() or board.turn != owner or balance(board, owner) > -100:
        raise ValueError("Practice must start on the sacrificing side with actual material debt")
    return board


def blocks(value):
    """Collect operational receipts without depending on one UI envelope."""
    found = []
    if isinstance(value, dict):
        policy = str(value.get("policy", ""))
        if ("operations" in policy or "practice-memory" in policy
                or value.get("source") == "native_practice_memory"):
            found.append(copy.deepcopy(value))
        for item in value.values():
            found.extend(blocks(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(blocks(item))
    return found


def run_case(case, args, support, HumanGame, boss_play, dynamic_play, audit_engine):
    directory = args.output / case["id"]
    directory.mkdir()
    events, owned, host, opponent = [], [], None, None
    owner = case["color"] == "white"
    row = {"case": case, "own_turns": [], "opponent_turns": [], "checks": {}, "error": None}
    started_case = time.monotonic()
    with (directory / "native-searches.jsonl").open("x", encoding="utf-8") as stream:
        try:
            def factory(path):
                engine = support.native(path)
                owned.append(engine)
                return support.TraceEngine(engine, stream, events, len(owned))
            host = HumanGame(args.binary, directory / "host", engine_factory=factory,
                hint_engine_factory=lambda _: support.DisplayPeer(),
                evaluation_engine_factory=lambda _: support.DisplayPeer(), neural_model=None)
            host.new_game("black" if owner else "white", args.movetime_ms, host.revision, boss_mode=True)
            host.board = board_for(case)
            host._opening_seed = case["seed"]
            host.revision += 1
            host._sync_evaluation()
            host._save()
            opponent = support.native(args.binary)
            owned.append(opponent)
            initial = features(host.board, owner, dynamic_play)
            row["initial_features"] = initial
            for index in range(args.own_turns):
                if host.state()["result"] != "*":
                    break
                before, first_event = host.board.copy(stack=True), len(events)
                if before.turn != owner:
                    raise ValueError("Opponent turn reached as an own turn")
                before_features = features(before, owner, dynamic_play)
                before_decisions = copy.deepcopy(list(host._boss_decisions.values()))
                graph_rows = boss_play.gambit_knowledge.proposals(before, case["seed"], decisions=before_decisions)
                phase = host.state()["engine"]["boss_phase"]
                started = time.monotonic()
                state = host.engine_move(host.revision)
                if state.get("error"):
                    raise RuntimeError(state["error"])
                if len(host.board.move_stack) != len(before.move_stack) + 1:
                    raise ValueError("One native action must append exactly one move")
                move = host.board.peek()
                receipt = copy.deepcopy(host._boss_decisions.get(len(host.board.move_stack), {}))
                relevant = events[first_event:]
                searches = {(value["instance"], value["search"]): value
                            for value in relevant if value["event"] == "search"}
                returned = [value for value in relevant if value["event"] == "bestmove" and value["move"] == move.uci()]
                baseline = [value for value in searches.values() if value["root_moves"] is None
                            and value["fen"] == before.fen(en_passant="fen")]
                checks = {"legal_move": move in before.legal_moves,
                    "actual_native_return": any(searches[(value["instance"], value["search"])]
                        ["fen"] == before.fen(en_passant="fen") for value in returned),
                    "full_baseline_preserved": bool(baseline) and baseline[0]["seconds"] == args.movetime_ms / 1000,
                    "receipt_context": receipt.get("before_fen") == before.fen(en_passant="fen")
                        and receipt.get("history_uci") == [m.uci() for m in before.move_stack],
                    "receipt_matches_actual": receipt.get("selected_uci") == move.uci(),
                    "receipt_current_policy": receipt.get("policy") == boss_play.POLICY,
                    "bounded_extra": 0 <= receipt.get("extra_budget_ms", -1) <= boss_play.extra_budget(args.movetime_ms) + 1}
                after_features = features(host.board, owner, dynamic_play)
                own = {"ply": len(host.board.move_stack), "san": before.san(move), "uci": move.uci(),
                    "phase_before": phase, "wall_ms": round((time.monotonic() - started) * 1000, 3),
                    "before_fen": before.fen(en_passant="fen"), "after_fen": host.board.fen(en_passant="fen"),
                    "before_features": before_features, "after_features": after_features,
                    "material_delta_cp": after_features["material_balance_cp"] - before_features["material_balance_cp"],
                    "debt_remaining_cp": max(0, -after_features["material_balance_cp"]),
                    "book_candidates_before": [v["uci"] for v in graph_rows], "outside_book": not graph_rows,
                    "beyond_old_initiative_window": len(before.move_stack) - case["offer_ply"] > 20,
                    "practice_receipts": blocks(receipt), "receipt": receipt, "checks": checks}
                own["audit"] = support.audit_move(audit_engine, before, move)
                row["own_turns"].append(own)
                if host.state()["result"] != "*" or index + 1 >= args.own_turns:
                    break
                board = host.board.copy(stack=True)
                stream.write(json.dumps({"event": "opponent-search", "fen": board.fen(en_passant="fen"),
                    "history_uci": [m.uci() for m in board.move_stack], "milliseconds": args.opponent_ms}) + "\n")
                answer = opponent.play(board, chess.engine.Limit(time=args.opponent_ms / 1000), info=chess.engine.INFO_ALL)
                reply = answer.move
                stream.write(json.dumps({"event": "opponent-bestmove", "move": support.safe(reply),
                                         "info": support.safe(answer.info)}) + "\n")
                stream.flush()
                if reply is None or reply not in board.legal_moves:
                    raise ValueError("Native opponent returned no legal move")
                response = {"ply": len(board.move_stack) + 1, "uci": reply.uci(), "san": board.san(reply),
                    "source": "unrestricted_native_opponent", "is_capture": board.is_capture(reply),
                    "material_before_cp": balance(board, owner)}
                state = host.human_move(reply.uci(), host.revision)
                if state.get("error"):
                    raise RuntimeError(state["error"])
                response["material_after_cp"] = balance(host.board, owner)
                if index == 0 or (index + 1) % args.audit_opponent_stride == 0:
                    response["audit"] = support.audit_move(audit_engine, board, reply)
                row["opponent_turns"].append(response)
            pgn = host.pgn()
            (directory / "game.pgn").write_text(pgn, encoding="utf-8", newline="\n")
            replay = chess.pgn.read_game(io.StringIO(pgn))
            final = features(host.board, owner, dynamic_play)
            row.update(final_fen=host.board.fen(en_passant="fen"), result=host.state()["result"],
                termination=host.state().get("termination"), history_uci=[m.uci() for m in host.board.move_stack],
                final_features=final, elapsed_seconds=round(time.monotonic() - started_case, 3))
            row["descriptive_observations"] = {"debt_change_cp": final["material_balance_cp"] - initial["material_balance_cp"],
                "debt_fully_recovered": final["material_balance_cp"] >= 0,
                "development_change": final["developed_minors"] - initial["developed_minors"],
                "pressure_change": final["coordinated_pressure"] - initial["coordinated_pressure"],
                "own_king_ring_attack_change": final["king_ring_enemy_attacks"] - initial["king_ring_enemy_attacks"],
                "outside_book_turns": sum(t["outside_book"] for t in row["own_turns"]),
                "beyond_old_window_turns": sum(t["beyond_old_initiative_window"] for t in row["own_turns"])}
            row["checks"] = {"all_turn_contracts": all(all(t["checks"].values()) for t in row["own_turns"]),
                "pgn_exact_replay": replay is not None and not replay.errors and list(replay.mainline_moves()) == host.board.move_stack,
                "post_sacrifice_continuation": len(row["own_turns"]) >= 2,
                "actual_initial_debt": initial["material_balance_cp"] <= -100,
                "no_external_move_selection": all(t["source"] == "unrestricted_native_opponent" for t in row["opponent_turns"])}
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
        finally:
            if opponent is not None:
                try:
                    opponent.quit()
                except Exception:
                    opponent.close()
            if host is not None:
                host.close()
            row["all_owned_engines_closed"] = all(engine.returncode.done() for engine in owned)
    row["passed"] = not row["error"] and all(row["checks"].values()) and row["all_owned_engines_closed"]
    support.write(directory / "result.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=PROOF / "predeclared/fixtures.json")
    parser.add_argument("--support", type=Path, default=PROOF / "native-support-v1.py")
    parser.add_argument("--movetime-ms", type=int, default=1000)
    parser.add_argument("--opponent-ms", type=int, default=1000)
    parser.add_argument("--own-turns", type=int, default=10)
    parser.add_argument("--audit-opponent-stride", type=int, default=3)
    args = parser.parse_args()
    args.source_root = args.source_root.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=False)
    support = load_support(args.support)
    fixture = json.loads(args.fixtures.read_text(encoding="utf-8"))
    for case in fixture["cases"]:
        board_for(case)
    if support.sha(args.support) != fixture["support_sha256"]:
        raise ValueError("Pinned native support changed")
    sys.path.insert(0, str(args.source_root / "scripts"))
    import boss_play
    import dynamic_play
    from play_human import HumanGame
    paths = list((args.source_root / "scripts").rglob("*.py"))
    paths += [p for d in ["data/openings", "data/gambits"] for p in (args.source_root / d).rglob("*") if p.is_file()]
    hashes = {p.relative_to(args.source_root).as_posix(): support.sha(p) for p in paths}
    result = {"schema": "regicide.gambit-operations-native.v1", "started_utc": support.utc(),
        "source_root": str(args.source_root), "source_sha256": hashes, "policy": boss_play.POLICY,
        "binary": str(args.binary), "binary_sha256": support.sha(args.binary),
        "harness_sha256": support.sha(Path(__file__)), "support_sha256": support.sha(args.support),
        "fixtures_sha256": support.sha(args.fixtures), "python": platform.python_version(),
        "platform": platform.platform(), "python_chess": chess.__version__,
        "config": {"own_turns": args.own_turns, "movetime_ms": args.movetime_ms,
                   "opponent_ms": args.opponent_ms, "extra_cap_ms": boss_play.extra_budget(args.movetime_ms),
                   "audit_opponent_stride": args.audit_opponent_stride},
        "scope": fixture["scope"], "results": []}
    support.write(args.output / "report.json", result)
    audit_engine = None
    try:
        if args.stockfish:
            import os
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            audit_engine = chess.engine.SimpleEngine.popen_uci([str(args.stockfish.resolve(strict=True))], timeout=15, **flags)
            audit_engine.configure({k: v for k, v in {"Threads": 1, "Hash": 32,
                "Skill Level": 20, "UCI_LimitStrength": False}.items() if k in audit_engine.options})
            result["audit_engine"] = {"id": dict(audit_engine.id), "sha256": support.sha(args.stockfish),
                                      "role": "Independent audit only; never a player"}
        for case in fixture["cases"]:
            row = run_case(case, args, support, HumanGame, boss_play, dynamic_play, audit_engine)
            result["results"].append(row)
            support.write(args.output / "report.json", result)
            print(json.dumps({"case": case["id"], "passed": row["passed"],
                "moves": [t["san"] for t in row["own_turns"]], "error": row["error"]}), flush=True)
    finally:
        if audit_engine is not None:
            audit_engine.quit()
    result.update(finished_utc=support.utc(), sources_unchanged=all(support.sha(args.source_root / n) == h for n, h in hashes.items()),
        binary_unchanged=support.sha(args.binary) == result["binary_sha256"],
        all_owned_engines_closed=all(r["all_owned_engines_closed"] for r in result["results"]),
        passed=sum(r["passed"] for r in result["results"]), failed=sum(not r["passed"] for r in result["results"]))
    support.write(args.output / "report.json", result)
    return 0 if not result["failed"] and result["sources_unchanged"] and result["binary_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
