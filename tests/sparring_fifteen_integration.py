"""Eight real production-host boundary turns; not games or strength evidence.

The legal standard-start history is the existing generated late-game fixture.
Only the next move is newly searched at each prefix. No preferred move, fake
past skill observation, or user game is supplied. The real Stockfish referee
participates in production sparring selection and final quality checks; this
is explicitly assisted sparring, not an independent Regicide benchmark.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import play_human
import sparring_play
from boss_strength_match import DisplayOnlyPeer, native_engine
from gambit_character_validation import TraceEngine, safe
from referee_engine import resolve_referee_engine
from sparring_referee import SparringReferee
from sparring_state_tests import late_game_board

NATIVE_SHA = "5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba"
SF_SHA = "9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6"
PREFIXES = (28, 29, 30, 31, 96, 97, 98, 99)
MOVE_MS = 1000
SCOPE = __doc__.strip()


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(safe(value), ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def fixtures():
    rows = []
    for ply in PREFIXES:
        board = late_game_board(ply)
        expected_move = ply // 2 + 1
        assert board.is_valid() and not board.is_game_over(claim_draw=True)
        assert board.fullmove_number == expected_move
        color = "white" if board.turn else "black"
        rows.append({"id": f"move{expected_move}-{color}", "prefix_plies": ply,
            "move_number": expected_move, "color": color,
            "expected_phase": "sparring" if expected_move == 15 else "finish" if expected_move == 50 else "ramp",
            "before_fen": board.fen(en_passant="fen"),
            "history_uci": [m.uci() for m in board.move_stack],
            "seed": hashlib.sha256(f"sparring-fifteen-v1|{ply}".encode()).hexdigest()[:32]})
    return {"schema": "regicide.sparring-fifteen-fixed-inputs.v1", "scope": SCOPE,
        "fixture_origin": "tests/sparring_state_tests.py:LATE_GAME_HISTORY_UCI; existing generated test history, not current user play",
        "fixture_source_sha256": sha(ROOT / "tests/sparring_state_tests.py"),
        "native_sha256": NATIVE_SHA, "referee_sha256": SF_SHA,
        "baseline_requested_ms": MOVE_MS, "clock": "disabled; same requested baseline in all eight cases",
        "extra_searches": "Unchanged production sparring/referee/safety/quality budgets; actual requests and elapsed time recorded separately.",
        "criteria": ["eight fixed phase boundaries, both colors", "legal exact one-move append",
            "actual native baseline and final selected move have native trace evidence",
            "full baseline requested", "final quality receipt matches actual selected move",
            "finish disables concessions; later safety replacement is recorded separately",
            "normal owned-process exits", "sources and pinned binaries unchanged"],
        "quality_limits": "Unverified or mitigated quality status remains explicit; a legal completed turn is not proof of safe play, balance, Elo, or lower reversal probability.",
        "cases": rows}


def source_hashes():
    names = {p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts").glob("*.py")}
    names.update(("tests/sparring_fifteen_integration.py", "tests/sparring_state_tests.py",
                  "tests/gambit_character_validation.py"))
    return {name: sha(ROOT / name) for name in sorted(names)}


def decision_chain(receipt):
    """Keep concession decisions distinguishable from later safety repairs."""
    rows, seen = [], set()
    def visit(row):
        if not isinstance(row, dict) or id(row) in seen:
            return
        seen.add(id(row))
        rows.append(row)
        for key in ("regicide_proposal", "pre_quality_selection", "pre_safety_selection"):
            visit(row.get(key))
    visit(receipt)
    return rows


def run_case(case, binary, referee_binary, folder):
    folder.mkdir(parents=True, exist_ok=False)
    events, identities, engines = [], [], []
    registry_lock = threading.Lock()
    stream = (folder / "engine-searches.jsonl").open("w", encoding="utf-8")
    host = referee = None
    result = {"fixture": case, "error": None, "checks": {}, "engines": identities,
              "scope": SCOPE}

    def factory(role):
        def create(path):
            inner = native_engine(path, boss=role == "Regicide")
            with registry_lock:
                engines.append((role, inner))
                traced = TraceEngine(inner, stream, identities, events)
                traced.identity.update(role=role, pid=inner.transport.get_pid())
            if role == "Regicide":
                options = {"Hash": 16, "Threads": 1, "UsePieceActivity": True,
                           "UseNeuralEval": False, "UseNNUE": False,
                           "UseQuiescenceForcing": False}
                traced.configure({k: v for k, v in options.items() if k in inner.options})
            return traced
        return create

    try:
        info = resolve_referee_engine(ROOT, referee_binary)
        if not info.get("available"):
            raise RuntimeError("Pinned local referee unavailable")
        referee = SparringReferee(referee_binary, engine_factory=factory("Stockfish"))
        host = play_human.HumanGame(binary, folder / "fixture-games",
            engine_factory=factory("Regicide"), referee=referee, referee_info=info,
            hint_engine_factory=lambda _: DisplayOnlyPeer(),
            evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
            opening_variety_enabled=False, dynamic_play_enabled=True)
        board = chess.Board()
        for uci in case["history_uci"]:
            board.push_uci(uci)
        assert board.fen(en_passant="fen") == case["before_fen"]
        host.new_game("black" if board.turn else "white", MOVE_MS, host.revision,
                      sparring_mode=True)
        host.board = board.copy(stack=True)
        host._opening_seed = case["seed"]
        host._sync_evaluation()
        host._save()
        start = host.state()
        result["before_state"] = {"phase": start["engine"]["sparring"]["phase"],
            "engine_to_move": start["engine_to_move"],
            "analysis_only": start["engine"]["sparring_analysis_only"],
            "extra_budget_ms": start["engine"]["extra_budget_ms"],
            "requested_movetime_ms": start["engine"]["movetime_ms"]}
        offset, started = len(events), time.monotonic()
        state = host.engine_move(host.revision)
        result["engine_move_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        if state.get("error"):
            raise RuntimeError(state["error"])
        selected = host.board.peek()
        receipt = copy.deepcopy(host._sparring_decisions[case["prefix_plies"] + 1])
        result.update(selected_uci=selected.uci(), selected_san=board.san(selected),
                      after_fen=host.board.fen(en_passant="fen"), decision=receipt)
        trace = events[offset:]
        own_instances = {i["instance"] for i in identities if i["role"] == "Regicide"}
        searches = {(e["instance"], e["search"]): e for e in trace if e["event"] == "search"}
        roots = {key: e for key, e in searches.items() if e["instance"] in own_instances
                 and e["fen"] == case["before_fen"] and e["history_uci"] == case["history_uci"]}
        baseline = next((e for e in roots.values() if e["root_moves"] is None), None)
        baseline_uci = next((e["move"] for e in trace if baseline and e["event"] == "bestmove"
            and e["instance"] == baseline["instance"] and e["search"] == baseline["search"]), None)
        witnesses = [e for e in trace if (e["instance"], e["search"]) in roots
            and ((e["event"] == "bestmove" and e["move"] == selected.uci())
                 or (e["event"] == "packet" and (e["packet"].get("pv") or [None])[0] == selected.uci()
                     and type(e["packet"].get("depth")) is int and e["packet"]["depth"] > 0))]
        result["native_original_baseline_uci"] = baseline_uci
        result["selected_native_witnesses"] = witnesses
        result["search_counts"] = {role: sum(e["event"] == "search" and e["instance"] in {
            i["instance"] for i in identities if i["role"] == role} for e in trace)
            for role in ("Regicide", "Stockfish")}
        quality = receipt.get("quality_guard") or {}
        chain = decision_chain(receipt)
        original = next((d for d in chain if d.get("reason") == "finish_full_strength"
            and d.get("baseline_uci") == baseline_uci), None)
        result["finish_concession_decision"] = ({k: original.get(k) for k in (
            "reason", "selected_uci", "baseline_uci", "analysis_only", "candidate_budget_ms",
            "discovery", "probes")} if original else None)
        result["final_differs_from_original_native_baseline"] = selected.uci() != baseline_uci
        replay = board.copy(stack=True)
        replay.push(selected)
        final_verdict = quality.get("final_verdict") or {}
        result["quality_observation"] = {"status": quality.get("status"), "reason": quality.get("reason"),
            "reference_verified": final_verdict.get("verified") is True,
            "acceptable": final_verdict.get("acceptable") is True,
            "applied": quality.get("applied") is True,
            "verdict": final_verdict}
        result["checks"] = {
            "phase_before_matches_boundary": result["before_state"]["phase"] == case["expected_phase"],
            "phase_receipt_matches_boundary": receipt.get("phase") == case["expected_phase"],
            "analysis_only_follows_finish_boundary": result["before_state"]["analysis_only"] == (case["expected_phase"] == "finish"),
            "engine_turn": start["engine_to_move"] is True,
            "legal_exact_append": selected in board.legal_moves and replay == host.board
                and len(host.board.move_stack) == case["prefix_plies"] + 1
                and [m.uci() for m in host.board.move_stack] == [*case["history_uci"], selected.uci()],
            "native_unrestricted_full_baseline_requested": baseline is not None and baseline["requested_seconds"] == MOVE_MS / 1000,
            "native_baseline_return_observed": baseline_uci is not None,
            "native_baseline_receipt_matches_return": any(d.get("baseline_uci") == baseline_uci for d in chain),
            "selected_move_has_native_root_evidence": bool(witnesses),
            "decision_matches_actual_move_and_history": receipt.get("selected_uci") == selected.uci()
                and receipt.get("history_uci") == case["history_uci"] and receipt.get("before_fen") == case["before_fen"],
            "quality_receipt_matches_actual_move": quality.get("selected_uci") == selected.uci()
                and quality.get("before_fen") == case["before_fen"] and quality.get("history_uci") == case["history_uci"],
            "quality_reference_actually_attempted": bool(quality.get("initial")) and result["search_counts"]["Stockfish"] > 0,
            "quality_reference_identity": (quality.get("initial") or {}).get("binary_sha256") == SF_SHA
                and (quality.get("initial") or {}).get("fen") == case["before_fen"]
                and (quality.get("initial") or {}).get("history_uci") == case["history_uci"]
                and (quality.get("initial") or {}).get("selected_uci") == quality.get("original_uci"),
            "finish_concessions_disabled": case["expected_phase"] != "finish" or original is not None
                and original.get("selected_uci") == baseline_uci and original.get("analysis_only") is True
                and original.get("candidate_budget_ms") == 0 and original.get("discovery") == original.get("probes") == [],
            "ramp_acceptable_reference_never_uses_noise_to_cross_zero": not (
                case["expected_phase"] == "ramp" and final_verdict.get("acceptable") is True
                and type(final_verdict.get("root_cp_own")) in (int, float)
                and final_verdict["root_cp_own"] >= 0) or final_verdict.get("reason") == "winning_mate_verified"
                or (type(final_verdict.get("child_cp_own")) in (int, float) and final_verdict["child_cp_own"] >= 0),
        }
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if host is not None:
                host.close()
            elif referee is not None:
                referee.close()
        except Exception as exc:
            result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        result["engine_exit_codes"] = []
        for role, engine in engines:
            try:
                code = engine.returncode.result(timeout=4)
            except Exception:
                code = None
            result["engine_exit_codes"].append({"role": role, "code": code})
        stream.close()
    result["passed"] = (result["error"] is None and not result.get("cleanup_error")
        and bool(result["checks"]) and all(result["checks"].values())
        and bool(engines) and all(row["code"] == 0 for row in result["engine_exit_codes"]))
    write(folder / "case.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path, help="Write fixed inputs without launching engines")
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--binary", type=Path, default=ROOT / "build/runtime-engines" / NATIVE_SHA / "regicide.exe")
    parser.add_argument("--stockfish", type=Path, default=ROOT / "build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe")
    args = parser.parse_args()
    if args.prepare:
        if args.prepare.exists():
            parser.error("Refusing to overwrite a predeclared fixture")
        write(args.prepare, fixtures())
        print(json.dumps({"prepared": str(args.prepare), "sha256": sha(args.prepare), "native_queries": 0}))
        return 0
    if not args.fixtures or not args.output:
        parser.error("--fixtures and --output are required for real execution")
    document = json.loads(args.fixtures.read_text(encoding="utf-8"))
    if document != fixtures():
        parser.error("Frozen input differs from the declared existing fixture and criteria")
    binary, referee = args.binary.resolve(strict=True), args.stockfish.resolve(strict=True)
    if sha(binary) != NATIVE_SHA or sha(referee) != SF_SHA:
        parser.error("Pinned native/referee binary mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    before = source_hashes()
    report = {"schema": "regicide.sparring-fifteen-real-host.v1", "scope": SCOPE,
        "started_utc": utc(), "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": before, "fixture_sha256": sha(args.fixtures), "inputs": document,
        "binary_sha256": sha(binary), "referee_sha256": sha(referee),
        "environment": {"platform": platform.platform(), "python": sys.version,
            "cpu_isolated": False, "user_application_concurrent_load": "not controlled; no user game read",
            "display_helpers": "existing DisplayOnlyPeer; no display search affects actual move selection",
            "stockfish_role": "real production referee during selection/quality; not independent after-the-fact strength audit"},
        "cases": [], "passed": False}
    write(args.output / "predeclared.json", report)
    for name in before:
        target = args.output / "source-executed" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
        if sha(target) != before[name]:
            raise RuntimeError("Source changed while freezing the execution snapshot")
    (args.output / "harness-executed.py").write_bytes(Path(__file__).read_bytes())
    (args.output / "fixtures-executed.json").write_bytes(args.fixtures.read_bytes())
    for case in document["cases"]:
        row = run_case(case, binary, referee, args.output / case["id"])
        report["cases"].append(row)
        write(args.output / "report.json", report)
        print(json.dumps({"case": case["id"], "passed": row["passed"], "selected": row.get("selected_uci"),
                          "quality": row.get("quality_observation"), "error": row["error"]}, ensure_ascii=True), flush=True)
    report.update(finished_utc=utc(), source_sha256_after=source_hashes())
    report["integrity"] = {"sources_unchanged": before == report["source_sha256_after"],
        "fixtures_unchanged": sha(args.fixtures) == report["fixture_sha256"],
        "native_unchanged": sha(binary) == NATIVE_SHA, "referee_unchanged": sha(referee) == SF_SHA}
    report["passed"] = len(report["cases"]) == 8 and all(c["passed"] for c in report["cases"]) and all(report["integrity"].values())
    report["quality_summary"] = {"reference_verified": sum(c.get("quality_observation", {}).get("reference_verified") is True for c in report["cases"]),
        "acceptable": sum(c.get("quality_observation", {}).get("acceptable") is True for c in report["cases"]),
        "limits": "Functional pass is separate from reference verification and acceptability; no complete game or strength inference."}
    write(args.output / "report.json", report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
