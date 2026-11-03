"""Before/after native Gambit continuation diagnostics in isolated games.

The predeclared prefixes and first opponent responses are development fixtures.
After that, a separate unrestricted Regicide supplies opponent moves. Stockfish
only audits played moves; none of its moves is used by either player. Capped
continuations remain unfinished, and these few branches do not measure Elo.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
CASES = (
    {"id": "evans-accepted", "family": "evans", "split": "untrained_family",
     "color": "white", "san_prefix": "e4 e5 Nf3 Nc6 Bc4 Bc5",
     "line_id": "evans", "signature_uci": "b2b4", "first_reply": "c5b4"},
    {"id": "evans-declined", "family": "evans", "split": "untrained_family",
     "color": "white", "san_prefix": "e4 e5 Nf3 Nc6 Bc4 Bc5",
     "line_id": "evans", "signature_uci": "b2b4", "first_reply": "c5b6"},
    {"id": "evans-defense", "family": "evans", "split": "untrained_family",
     "color": "white", "san_prefix": "e4 e5 Nf3 Nc6 Bc4 Bc5",
     "line_id": "evans", "signature_uci": "b2b4", "first_reply": "c5b4",
     "conditional_replies": {"c2c3": "b4a5"}},
    {"id": "evans-unusual-defense", "family": "evans", "split": "untrained_family",
     "color": "white", "san_prefix": "e4 e5 Nf3 Nc6 Bc4 Bc5",
     "line_id": "evans", "signature_uci": "b2b4", "first_reply": "c5e7"},
    {"id": "stafford-accepted", "family": "stafford", "split": "untrained_family",
     "color": "black", "san_prefix": "e4 e5 Nf3 Nf6 Nxe5",
     "line_id": "stafford", "signature_uci": "b8c6", "first_reply": "e5c6"},
    {"id": "stafford-declined", "family": "stafford", "split": "untrained_family",
     "color": "black", "san_prefix": "e4 e5 Nf3 Nf6 Nxe5",
     "line_id": "stafford", "signature_uci": "b8c6", "first_reply": "e5f3"},
    {"id": "stafford-defense", "family": "stafford", "split": "untrained_family",
     "color": "black", "san_prefix": "e4 e5 Nf3 Nf6 Nxe5",
     "line_id": "stafford", "signature_uci": "b8c6", "first_reply": "e5c6",
     "conditional_replies": {"d7c6": "d2d3"}},
    {"id": "benko-accepted", "family": "benko", "split": "training_family_functional",
     "color": "black", "san_prefix": "d4 Nf6 c4 c5 d5 b5 cxb5"},
    {"id": "morra-declined", "family": "morra", "split": "tuning_family_functional",
     "color": "white", "san_prefix": "e4 c5 d4 cxd4 c3 Nf6"},
    {"id": "blackmar-transposed", "family": "blackmar", "split": "training_family_functional",
     "color": "white", "san_prefix": "d4 d5 Nc3 Nf6 e4 dxe4"},
)


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8", newline="\n")
    temporary.replace(path)


def safe(value):
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, chess.engine.PovScore):
        return {"score_turn": "white" if value.turn else "black",
                "cp_white": value.white().score(), "mate_white": value.white().mate()}
    if isinstance(value, chess.engine.PovWdl):
        return {"turn": "white" if value.turn else "black", "white": list(value.white())}
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe(v) for v in value]
    return value if isinstance(value, (str, int, float, bool)) or value is None else repr(value)


def board_for(case):
    board = chess.Board()
    for san in case["san_prefix"].split():
        board.push_san(san)
    assert board.turn == (case["color"] == "white") and board.is_valid()
    return board


def prepare(case, boss_play, seed, movetime_ms):
    board = board_for(case)
    indexes = range(4096) if case.get("signature_uci") else range(1)
    for index in indexes:
        key = hashlib.sha256(json.dumps([seed, case["id"], index]).encode()).hexdigest()[:32]
        plan = boss_play.plan(board, movetime_ms, key)
        if not case.get("signature_uci") or (plan.get("line_id") == case["line_id"]
                and plan.get("candidate_uci") == case["signature_uci"] and plan.get("line_complete")):
            return {**case, "seed": key, "seed_index": index,
                    "history_uci": [m.uci() for m in board.move_stack],
                    "before_fen": board.fen(en_passant="fen"), "predeclared_plan": plan}
    raise ValueError("No pre-search signature coverage seed: " + case["id"])


class DisplayPeer:
    id = {"name": "No-search diagnostic display peer"}
    options = {}

    def play(self, board, limit, **kwargs):
        return chess.engine.PlayResult(next(iter(board.legal_moves), None), None, {})

    def quit(self):
        pass

    def close(self):
        pass


class TraceAnalysis:
    def __init__(self, inner, owner, index):
        self.inner, self.owner, self.index = inner, owner, index

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *args):
        return self.inner.__exit__(*args)

    def __iter__(self):
        for packet in self.inner:
            self.owner.emit(event="packet", search=self.index, packet=safe(packet))
            yield packet

    def wait(self):
        result = self.inner.wait()
        self.owner.emit(event="bestmove", search=self.index, move=safe(result.move))
        return result


class TraceEngine:
    def __init__(self, inner, stream, events, instance):
        self.inner, self.stream, self.events, self.instance = inner, stream, events, instance
        self.index, self.lock = 0, threading.Lock()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def emit(self, **value):
        value = {"instance": self.instance, **value}
        with self.lock:
            self.events.append(value)
            self.stream.write(json.dumps(value, allow_nan=False) + "\n")
            self.stream.flush()

    def analysis(self, board, limit, **kwargs):
        self.index += 1
        self.emit(event="search", search=self.index, fen=board.fen(en_passant="fen"),
                  history_uci=[m.uci() for m in board.move_stack], seconds=limit.time,
                  depth=limit.depth, nodes=limit.nodes, root_moves=safe(kwargs.get("root_moves")))
        return TraceAnalysis(self.inner.analysis(board, limit, **kwargs), self, self.index)


def native(binary):
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=15, **flags)
    engine.configure({k: v for k, v in {"Threads": 1, "Hash": 16, "UsePieceActivity": True}.items()
                      if k in engine.options})
    return engine


def audit_move(engine, before, move):
    if engine is None:
        return {"status": "unavailable"}
    queries = []
    for roots in (None, [move]):
        if "Clear Hash" in engine.options:
            engine.configure({"Clear Hash": None})
        packets = {}
        with engine.analysis(before, chess.engine.Limit(time=.25, depth=12),
                             root_moves=roots, info=chess.engine.INFO_ALL) as stream:
            for packet in stream:
                score, depth = packet.get("score"), packet.get("depth")
                if (isinstance(score, chess.engine.PovScore) and type(depth) is int
                        and not packet.get("lowerbound") and not packet.get("upperbound")):
                    packets[depth] = packet
            result = stream.wait()
        queries.append({"root_moves": safe(roots), "returned": safe(result.move),
                        "packets": packets})
    common = sorted(set(queries[0]["packets"]) & set(queries[1]["packets"]))
    if not common:
        return {"status": "no_common_depth", "queries": safe(queries)}
    depth = common[-1]
    best, played = [row["packets"][depth]["score"].pov(before.turn) for row in queries]
    cp = [value.score() for value in (best, played)]
    return {"status": "exact_common_depth", "depth": depth,
            "reference_cp_own": cp[0], "played_cp_own": cp[1],
            "reference_mate_own": best.mate(), "played_mate_own": played.mate(),
            "loss_cp": max(0, cp[0] - cp[1]) if all(v is not None for v in cp) else None,
            "queries": safe(queries), "selection_use": "independent audit only"}


def run_case(case, args, HumanGame, boss_play, audit_engine):
    directory = args.output / case["id"]
    directory.mkdir()
    events, hosts, opponent, host = [], [], None, None
    row = {"case": case, "own_turns": [], "opponent_turns": [], "checks": {}, "error": None}
    with (directory / "native-searches.jsonl").open("x", encoding="utf-8") as stream:
        try:
            def factory(path):
                engine = native(path)
                hosts.append(engine)
                return TraceEngine(engine, stream, events, len(hosts))
            host = HumanGame(args.binary, directory / "host", engine_factory=factory,
                             hint_engine_factory=lambda _: DisplayPeer(),
                             evaluation_engine_factory=lambda _: DisplayPeer(), neural_model=None)
            host.new_game("black" if case["color"] == "white" else "white", args.movetime_ms,
                          host.revision, boss_mode=True)
            host.board = board_for(case)
            host._opening_seed = case["seed"]
            host.revision += 1
            host._sync_evaluation()
            host._save()
            opponent = native(args.binary)
            first_reply_used = False
            for index in range(args.own_turns):
                if host.state()["result"] != "*":
                    break
                before, start = host.board.copy(stack=True), len(events)
                started = time.monotonic()
                phase = host.state()["engine"]["boss_phase"]
                state = host.engine_move(host.revision)
                if state.get("error"):
                    raise RuntimeError(state["error"])
                assert len(host.board.move_stack) == len(before.move_stack) + 1
                move = host.board.peek()
                receipt = copy.deepcopy(host._boss_decisions.get(len(host.board.move_stack), {}))
                relevant = events[start:]
                searches = {(v["instance"], v["search"]): v for v in relevant if v["event"] == "search"}
                returned = [v for v in relevant if v["event"] == "bestmove" and v["move"] == move.uci()]
                baseline = [v for v in searches.values() if v["root_moves"] is None
                            and v["fen"] == before.fen(en_passant="fen")]
                checks = {"legal_move": move in before.legal_moves,
                    "actual_native_return": any(searches[(v["instance"], v["search"])]["fen"] == before.fen(en_passant="fen") for v in returned),
                    "full_baseline_preserved": bool(baseline) and baseline[0]["seconds"] == args.movetime_ms / 1000,
                    "receipt_context": receipt.get("before_fen") == before.fen(en_passant="fen")
                         and receipt.get("history_uci") == [m.uci() for m in before.move_stack],
                    "receipt_matches_actual": receipt.get("selected_uci") == move.uci(),
                    "receipt_current_policy": receipt.get("policy") == boss_play.POLICY,
                    "bounded_extra": 0 <= receipt.get("extra_budget_ms", -1) <= boss_play.extra_budget(args.movetime_ms) + 1}
                if index == 0 and case.get("signature_uci"):
                    checks["actual_authored_signature"] = move.uci() == case["signature_uci"]
                own = {"ply": len(host.board.move_stack), "san": before.san(move), "uci": move.uci(),
                    "phase_before": phase, "wall_ms": round((time.monotonic() - started) * 1000, 3),
                    "before_fen": before.fen(en_passant="fen"), "after_fen": host.board.fen(en_passant="fen"),
                    "material_before_own_cp": sum((1 if p.color == before.turn else -1) * VALUES[p.piece_type] for p in before.piece_map().values()),
                    "material_after_own_cp": sum((1 if p.color == before.turn else -1) * VALUES[p.piece_type] for p in host.board.piece_map().values()),
                    "receipt": receipt, "checks": checks}
                # Audit starts only after the host has irrevocably chosen and saved its move.
                own["audit"] = audit_move(audit_engine, before, move)
                row["own_turns"].append(own)
                if host.state()["result"] != "*" or index + 1 >= args.own_turns:
                    break
                board = host.board.copy(stack=True)
                reply = None
                role = "unrestricted_native_opponent"
                if not first_reply_used and case.get("first_reply"):
                    reply = chess.Move.from_uci(case["first_reply"])
                    first_reply_used = True
                    role = "predeclared_legal_response"
                elif index == 1 and case.get("conditional_replies", {}).get(move.uci()):
                    reply = chess.Move.from_uci(case["conditional_replies"][move.uci()])
                    role = "predeclared_conditional_defense"
                if reply is None:
                    request = {"event": "opponent-search", "fen": board.fen(en_passant="fen"),
                               "history_uci": [m.uci() for m in board.move_stack], "milliseconds": args.opponent_ms}
                    stream.write(json.dumps(request) + "\n")
                    answer = opponent.play(board, chess.engine.Limit(time=args.opponent_ms / 1000), info=chess.engine.INFO_ALL)
                    reply = answer.move
                    stream.write(json.dumps({"event": "opponent-bestmove", "move": safe(reply), "info": safe(answer.info)}) + "\n")
                assert reply is not None and reply in board.legal_moves
                row["opponent_turns"].append({"ply": len(board.move_stack) + 1, "uci": reply.uci(),
                    "san": board.san(reply), "source": role, "is_capture": board.is_capture(reply)})
                state = host.human_move(reply.uci(), host.revision)
                assert not state.get("error")
            pgn = host.pgn()
            (directory / "game.pgn").write_text(pgn, encoding="utf-8", newline="\n")
            replay = chess.pgn.read_game(io.StringIO(pgn))
            row.update(final_fen=host.board.fen(en_passant="fen"),
                       result=host.state()["result"], termination=host.state().get("termination"),
                       history_uci=[m.uci() for m in host.board.move_stack])
            row["checks"] = {"all_turn_contracts": all(all(t["checks"].values()) for t in row["own_turns"]),
                "pgn_exact_replay": replay is not None and not replay.errors
                    and list(replay.mainline_moves()) == host.board.move_stack,
                "continuation_beyond_signature": len(row["own_turns"]) >= 2,
                "no_external_move_selection": all(v["source"] != "stockfish" for v in row["opponent_turns"])}
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
            row["all_owned_engines_closed"] = all(e.returncode.done() for e in hosts)
    row["passed"] = not row["error"] and all(row["checks"].values()) and row["all_owned_engines_closed"]
    write(directory / "result.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--movetime-ms", type=int, default=1000)
    parser.add_argument("--opponent-ms", type=int, default=750)
    parser.add_argument("--own-turns", type=int, default=4)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    args.source_root = args.source_root.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.source_root / "scripts"))
    import boss_play
    from play_human import HumanGame
    cases = (json.loads(args.fixtures.read_text(encoding="utf-8"))["cases"] if args.fixtures else
             [prepare(case, boss_play, "2026-11-04-gambit-knowledge-v1", args.movetime_ms) for case in CASES])
    paths = list((args.source_root / "scripts").rglob("*.py"))
    if (args.source_root / "data/gambits").exists():
        paths += [p for p in (args.source_root / "data/gambits").rglob("*") if p.is_file()]
    hashes = {p.relative_to(args.source_root).as_posix(): sha(p) for p in paths}
    manifest = {"schema": "regicide.gambit-knowledge-native.v1", "started_utc": utc(),
        "source_root": str(args.source_root), "source_sha256": hashes,
        "binary": str(args.binary), "binary_sha256": sha(args.binary), "policy": boss_play.POLICY,
        "config": {"movetime_ms": args.movetime_ms, "extra_cap_ms": boss_play.extra_budget(args.movetime_ms),
                   "opponent_ms": args.opponent_ms, "own_turns": args.own_turns},
        "python": platform.python_version(), "python_chess": chess.__version__, "platform": platform.platform(),
        "cases": cases, "results": [], "scope": "Authored prefixes/first responses followed by native play; unfinished capped continuations, not complete matches or Elo"}
    write(args.output / "fixtures.json", {"cases": cases})
    write(args.output / "report.json", manifest)
    if args.prepare_only:
        print("Prepared", len(cases), "predeclared cases")
        return 0
    audit_engine = None
    try:
        if args.stockfish:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            audit_engine = chess.engine.SimpleEngine.popen_uci([str(args.stockfish.resolve(strict=True))], timeout=15, **flags)
            audit_engine.configure({k: v for k, v in {"Threads": 1, "Hash": 32, "Skill Level": 20,
                "UCI_LimitStrength": False}.items() if k in audit_engine.options})
            manifest["audit_engine"] = {"id": dict(audit_engine.id), "sha256": sha(args.stockfish), "role": "Independent move audit only; never a player"}
        for case in cases:
            row = run_case(case, args, HumanGame, boss_play, audit_engine)
            manifest["results"].append(row)
            write(args.output / "report.json", manifest)
            print(json.dumps({"case": case["id"], "passed": row["passed"],
                              "moves": [t["san"] for t in row["own_turns"]], "error": row["error"]}), flush=True)
    finally:
        if audit_engine is not None:
            audit_engine.quit()
    manifest.update(finished_utc=utc(), sources_unchanged=all(sha(args.source_root / n) == h for n, h in hashes.items()),
                    binary_unchanged=sha(args.binary) == manifest["binary_sha256"],
                    passed=sum(r["passed"] for r in manifest["results"]),
                    failed=sum(not r["passed"] for r in manifest["results"]))
    write(args.output / "report.json", manifest)
    return 0 if manifest["failed"] == 0 and manifest["sources_unchanged"] and manifest["binary_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
