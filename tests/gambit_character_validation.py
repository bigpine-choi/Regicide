"""Real production-host gambit signature and accepted-offer development fixtures.

Prefixes and opponent captures are authored fixtures, not played matches. Seeds
are selected from the book plan before any native answer is observed. Every
Regicide move is chosen by HumanGame with the real own engine. No baseline,
candidate result, gate outcome or external engine move is injected.
"""
import argparse
import copy
import hashlib
import io
import json
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
sys.path.insert(0, str(ROOT / "scripts"))
import boss_openings
import boss_opponent
import boss_play
from boss_strength_match import DisplayOnlyPeer, SOURCES, native_engine, sha, utc, write_json
from play_human import HumanGame
from runtime_engine import resolve_runtime_engine

VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
CASES = (
    {"id": "white-kings", "color": "white", "san_prefix": "e4 e5",
     "line_id": "kings", "signature_uci": "f2f4", "accept_uci": "e5f4"},
    {"id": "white-evans", "color": "white", "san_prefix": "e4 e5 Nf3 Nc6 Bc4 Bc5",
     "line_id": "evans", "signature_uci": "b2b4", "accept_uci": "c5b4"},
    {"id": "black-englund", "color": "black", "san_prefix": "d4",
     "line_id": "englund", "signature_uci": "e7e5", "accept_uci": "d4e5"},
    {"id": "black-stafford", "color": "black", "san_prefix": "e4 e5 Nf3 Nf6 Nxe5",
     "line_id": "stafford", "signature_uci": "b8c6", "accept_uci": "e5c6"},
)


def safe(value):
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, chess.engine.PovScore):
        return {"score_turn": "white" if value.turn else "black",
                "cp_white": value.white().score(), "mate_white": value.white().mate()}
    if isinstance(value, chess.engine.PovWdl):
        return {"turn": "white" if value.turn else "black", "white": list(value.white())}
    if isinstance(value, dict):
        return {str(key): safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def material(board, color):
    return sum((1 if piece.color == color else -1) * VALUES[piece.piece_type]
               for piece in board.piece_map().values())


def board_for(case):
    board = chess.Board()
    for san in case["san_prefix"].split():
        board.push_san(san)
    assert board.turn == (case["color"] == "white") and not board.is_game_over(claim_draw=True)
    return board


def prepare(case, master_seed, movetime_ms):
    """Select book-branch coverage before starting an engine, never by its move."""
    board = board_for(case)
    for index in range(4096):
        payload = [master_seed, "gambit-character-book-coverage", case["id"], index]
        seed = hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:32]
        plan = boss_play.plan(board, movetime_ms, seed)
        if (plan.get("candidate_uci") == case["signature_uci"]
                and plan.get("line_id") == case["line_id"] and plan.get("line_complete") is True):
            return {**case, "seed": seed, "seed_index": index,
                    "history_uci": [move.uci() for move in board.move_stack],
                    "before_fen": board.fen(en_passant="fen"), "predeclared_plan": plan,
                    "predeclared_gate": boss_opponent.gate(board, [], seed)}
    raise ValueError("No pre-search book coverage seed for " + case["id"])


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
            self.owner.emit({"event": "packet", "search": self.index, "packet": safe(packet)})
            yield packet

    def wait(self):
        result = self.inner.wait()
        self.owner.emit({"event": "bestmove", "search": self.index,
                         "move": safe(result.move), "ponder": safe(result.ponder)})
        return result


class TraceEngine:
    def __init__(self, inner, stream, identities, events):
        self.inner, self.stream, self.events = inner, stream, events
        self.label, self.index, self.lock = len(identities) + 1, 0, threading.Lock()
        self.identity = {"instance": self.label, "id": dict(inner.id), "configured_options": {}}
        identities.append(self.identity)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def configure(self, options):
        self.inner.configure(options)
        self.identity["configured_options"].update(options)

    def emit(self, item):
        item = {"instance": self.label, **item}
        with self.lock:
            self.events.append(item)
            self.stream.write(json.dumps(item, allow_nan=False) + "\n")
            self.stream.flush()

    def analysis(self, board, limit, **kwargs):
        self.index += 1
        self.emit({"event": "search", "search": self.index,
                   "fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
                   "requested_seconds": limit.time, "requested_depth": limit.depth,
                   "requested_nodes": limit.nodes, "root_moves": safe(kwargs.get("root_moves"))})
        return TraceAnalysis(self.inner.analysis(board, limit, **kwargs), self, self.index)


def move_step(host, events, movetime_ms):
    before = host.board.copy(stack=True)
    phase_before = host.state()["engine"]["boss_phase"]
    event_start, started = len(events), time.monotonic()
    state = host.engine_move(host.revision)
    if state.get("error"):
        raise RuntimeError(state["error"])
    if len(host.board.move_stack) != len(before.move_stack) + 1:
        raise AssertionError("Production host did not append exactly one move")
    move = host.board.peek()
    assert move in before.legal_moves
    expected = before.copy(stack=True)
    expected.push(move)
    assert expected.fen(en_passant="fen") == host.board.fen(en_passant="fen")
    decision = copy.deepcopy(host._boss_decisions.get(len(host.board.move_stack), {}))
    searches = [e for e in events[event_start:] if e["event"] == "search"]
    requests = {(e["instance"], e["search"]): e for e in searches}
    returned = [e for e in events[event_start:] if e["event"] == "bestmove"]
    native_returned_selected = any(e["move"] == move.uci()
        and requests[(e["instance"], e["search"])]["fen"] == before.fen(en_passant="fen") for e in returned)
    baseline = next((e for e in searches if e["fen"] == before.fen(en_passant="fen")
                     and e["root_moves"] is None), None)
    checks = {"legal_and_exact_append": True,
              "matching_policy_receipt": decision.get("policy") == boss_play.POLICY,
              "matching_selected_move": decision.get("selected_uci") == move.uci(),
              "matching_receipt_context": decision.get("before_fen") == before.fen(en_passant="fen")
                  and decision.get("history_uci") == [m.uci() for m in before.move_stack],
              "full_unrestricted_baseline": baseline is not None and baseline["requested_seconds"] == movetime_ms / 1000,
              "selected_returned_by_native_search": native_returned_selected,
              "configured_budget_preserved": decision.get("baseline_budget_ms") == movetime_ms,
              "bounded_extra_budget": 0 <= decision.get("extra_budget_ms", -1) <= boss_play.extra_budget(movetime_ms) + .001}
    return {"ply": len(host.board.move_stack), "san": before.san(move), "uci": move.uci(),
            "before_fen": before.fen(en_passant="fen"), "after_fen": host.board.fen(en_passant="fen"),
            "phase_before": phase_before, "phase_after": state["engine"]["boss_phase"],
            "wall_ms": round((time.monotonic() - started) * 1000, 3),
            "native_queries": len(searches), "receipt": decision, "checks": checks}


def run_case(case, binary, output, movetime_ms):
    directory = output / case["id"]
    directory.mkdir()
    row = {**case, "engines": [], "turns": [], "checks": {}, "error": None}
    events, host = [], None
    stream = (directory / "native-searches.jsonl").open("x", encoding="utf-8")
    try:
        def factory(path):
            engine = TraceEngine(native_engine(path, boss=True), stream, row["engines"], events)
            engine.configure({key: value for key, value in (("Threads", 1), ("Hash", 16)) if key in engine.options})
            return engine
        host = HumanGame(binary, directory / "host", engine_factory=factory,
                         hint_engine_factory=lambda _: DisplayOnlyPeer(),
                         evaluation_engine_factory=lambda _: DisplayOnlyPeer(), neural_model=None)
        host.new_game("black" if case["color"] == "white" else "white", movetime_ms, host.revision, boss_mode=True)
        host.board = board_for(case)
        host._opening_seed = case["seed"]
        host.revision += 1
        host._sync_evaluation()
        host._save()
        row["material_before_own_cp"] = material(host.board, host.board.turn)
        first = move_step(host, events, movetime_ms)
        row["turns"].append(first)
        decision = first["receipt"]
        row["checks"].update(first["checks"])
        row["checks"]["signature_selected"] = (first["uci"] == case["signature_uci"]
            and decision.get("line_id") == case["line_id"] and decision.get("line_complete") is True)
        row["checks"]["completed_depth_at_least_four"] = (type(decision.get("compared_depth")) is int
            and decision["compared_depth"] >= boss_play.MIN_DEPTH)
        if first["uci"] != decision.get("baseline_uci") and row["checks"]["signature_selected"]:
            matching = next((p for p in decision.get("probes", []) if p.get("uci") == first["uci"] and p.get("admitted")), {})
            row["checks"]["same_completed_depth_comparison"] = (
                (matching.get("candidate_compared") or {}).get("depth") == (decision.get("baseline") or {}).get("depth")
                == decision.get("compared_depth"))
        if row["checks"]["signature_selected"]:
            before = host.board.copy(stack=True)
            accepted = chess.Move.from_uci(case["accept_uci"])
            victim = before.piece_at(accepted.to_square)
            capture_valid = accepted in before.legal_moves and before.is_capture(accepted) and victim is not None
            row["checks"]["authored_acceptance_legal_capture"] = capture_valid
            if capture_valid:
                state = host.human_move(accepted.uci(), host.revision)
                if state.get("error"):
                    raise RuntimeError(state["error"])
                row["accepted_capture"] = {"uci": accepted.uci(), "san": before.san(accepted),
                    "victim": victim.symbol(), "victim_cp": VALUES[victim.piece_type],
                    "after_fen": host.board.fen(en_passant="fen"),
                    "material_after_own_cp": material(host.board, not before.turn),
                    "scope": "authored legal opponent capture; not an observed human acceptance rate"}
                followup = move_step(host, events, movetime_ms)
                row["turns"].append(followup)
                row["checks"]["followup_legal_and_native"] = all(followup["checks"].values())
                row["checks"]["initiative_entered_after_acceptance"] = (
                    followup["phase_before"] == "initiative" and followup["receipt"].get("mode") == "initiative")
                row["material_after_followup_own_cp"] = material(host.board, case["color"] == "white")
        else:
            row["followup_not_run"] = "signature was not selected; no forced replacement or opponent capture"
        row["host_game_id"], row["final_fen"] = host.game_id, host.board.fen(en_passant="fen")
        pgn = host.pgn()
        (directory / "case.pgn").write_text(pgn, encoding="utf-8")
        replay = chess.pgn.read_game(io.StringIO(pgn))
        row["checks"]["pgn_replays_exactly"] = (replay is not None and not replay.errors
            and list(replay.mainline_moves()) == host.board.move_stack
            and replay.end().board().fen(en_passant="fen") == host.board.fen(en_passant="fen"))
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if host is not None:
            try:
                host.close()
            except Exception as error:
                row["cleanup_error"] = f"{type(error).__name__}: {error}"
                row["error"] = row["error"] or row["cleanup_error"]
        stream.close()
    row["passed"] = row["error"] is None and bool(row["checks"]) and all(row["checks"].values())
    row["native_query_count"] = sum(e["event"] == "search" for e in events)
    write_json(directory / "case.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--movetime-ms", type=int, default=1000)
    parser.add_argument("--seed", default="2026-11-04-gambit-character-fixtures-v1")
    parser.add_argument("--case", action="append", dest="cases", choices=[c["id"] for c in CASES])
    args = parser.parse_args()
    if not 500 <= args.movetime_ms <= 5000:
        parser.error("movetime-ms must be 500..5000")
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # The entire case/seed manifest is frozen before the first native instance.
    cases = [prepare(case, args.seed, args.movetime_ms) for case in CASES if not args.cases or case["id"] in args.cases]
    source_paths = sorted(set(SOURCES) | {Path(__file__).relative_to(ROOT).as_posix()})
    sources = {name: sha(ROOT / name) for name in source_paths}
    report = {"schema": "regicide.gambit-character-development.v1", "started_at": utc(),
              "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_sha256": sources, "binary": str(binary), "binary_sha256": sha(binary),
              "python": platform.python_version(), "python_chess": chess.__version__, "platform": platform.platform(),
              "policies": {"boss": boss_play.POLICY, "opening": boss_openings.POLICY, "opponent": boss_opponent.POLICY,
                  "initiative": getattr(sys.modules.get("gambit_initiative"), "POLICY", None)},
              "configured_movetime_ms": args.movetime_ms, "extra_budget_cap_ms": boss_play.extra_budget(args.movetime_ms),
              "seed_master": args.seed, "seed_selection": "First SHA256-derived seed with the authored book plan's signature and line_id; all fixed before any native answer. No gate override or native-result-based resampling.",
              "execution": "Production HumanGame and real native queries; no external engine; no baseline injection; isolated saves and no-search display peers.",
              "limitations": ["Authored prefixes and accepted captures, not played games or human behavior data.",
                  "Book-branch coverage seeds are selected development fixtures, not a natural gambit-frequency sample.",
                  "Signature selection, actual capture, material debt and initiative are distinct checks; none proves objective soundness or Elo."],
              "predeclared_cases": cases, "cases": []}
    write_json(output / "report.json", report)
    for case in cases:
        row = run_case(case, binary, output, args.movetime_ms)
        report["cases"].append(row)
        write_json(output / "report.json", report)
        print(json.dumps({"case": case["id"], "passed": row["passed"], "moves": [r["san"] for r in row["turns"]],
                          "checks": row["checks"], "error": row["error"]}), flush=True)
    report.update(finished_at=utc(), passed=sum(row["passed"] for row in report["cases"]),
                  failed=sum(not row["passed"] for row in report["cases"]),
                  source_sha256_after={name: sha(ROOT / name) for name in sources})
    report["sources_unchanged"] = report["source_sha256_after"] == sources
    report["binary_unchanged"] = sha(binary) == report["binary_sha256"]
    write_json(output / "report.json", report)
    return 0 if not report["failed"] and report["sources_unchanged"] and report["binary_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
