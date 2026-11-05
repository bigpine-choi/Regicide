"""Isolated production-host opening A/B, then optional post-selection audit.

Each arm imports its own frozen scripts/data in a fresh process. Clock-free
500ms-or-longer HumanGame policy calls select real native moves; authored
prefixes are fixtures, not completed games or an Elo measurement.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

import chess

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/data/gambit_opening_growth_2026_11_05.json"
BASELINE = ROOT / "build/gambit-opening-growth/baseline-source"
RUNTIME = ROOT / "build/runtime-engines/5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba/regicide.exe"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def utc():
    return datetime.now(timezone.utc).isoformat()


def board_for(case):
    board = chess.Board()
    for uci in case["history_uci"]:
        board.push_uci(uci)
    if (not board.is_valid() or board.is_game_over(claim_draw=True)
            or board.turn != (case["color"] == "white")
            or board.fen(en_passant="fen") != case["before_fen"]):
        raise ValueError("Invalid predeclared fixture: " + case["id"])
    return board


def studied_iteration_completed(row):
    """Predeclared audit filter; never influences the native policy."""
    detail = row.get("arms", {}).get("candidate", {}).get("receipt", {}).get("knowledge") or {}
    for attempt in detail.get("attempts", []):
        common, final = attempt.get("candidate_compared"), attempt.get("candidate_final")
        target = attempt.get("target_depth")
        if (attempt.get("loss_basis") == "baseline_included_in_same_completed_native_search"
                and isinstance(target, int) and isinstance(common, dict) and isinstance(final, dict)
                and common.get("depth") == target and final.get("depth", 0) >= target
                and attempt.get("reason") != "candidate_score_conflict"):
            return True
    return False


def snapshot_candidate(destination):
    """Snapshot only source/data paths; never copy user saves or executables."""
    destination.mkdir(parents=True, exist_ok=False)
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                             "scripts", "data", "config"], cwd=ROOT, capture_output=True,
                            text=True, check=True).stdout.splitlines()
    old = json.loads((BASELINE / "baseline-manifest.json").read_text(encoding="utf-8"))
    names = set(listed) | {row["path"] for row in old["files"] if row["path"].startswith("tests/")}
    rows = []
    for name in sorted(names):
        path = ROOT / name
        if not path.is_file() or path.suffix.lower() in (".zip", ".exe", ".obj", ".onnx", ".nnue", ".npz", ".pt"):
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        target.write_bytes(data)
        rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    write(destination / "source-manifest.json", {"schema": "regicide.opening-growth-candidate.v1",
        "recorded_utc": utc(), "files": rows, "scope": "Working-tree bytes frozen before candidate queries; source may be uncommitted."})


def worker(args):
    source = args.source_root.resolve(strict=True)
    scripts, tests = source / "scripts", source / "tests"
    # Separate processes prevent cross-arm module/cache contamination. Remove
    # the invoking checkout's module roots before importing production code.
    sys.path[:] = [str(scripts), str(tests), *[p for p in sys.path if Path(p or ".").resolve() not in (ROOT / "scripts", ROOT / "tests")]]
    import boss_play
    import gambit_operations
    import gambit_repertoire_play
    import gambit_knowledge
    import gambit_practice_memory
    import play_human
    from boss_strength_match import DisplayOnlyPeer, native_engine
    from gambit_character_validation import TraceEngine, safe

    modules = [boss_play, gambit_operations, gambit_repertoire_play, gambit_knowledge,
               gambit_practice_memory, play_human]
    imported = {module.__name__: str(Path(module.__file__).resolve()) for module in modules}
    if not all(Path(path).is_relative_to(source) for path in imported.values()):
        raise ValueError("Worker imported a policy outside its frozen source root")
    imported_scripts = {name: str(Path(module.__file__).resolve()) for name, module in tuple(sys.modules.items())
        if getattr(module, "__file__", None) and Path(module.__file__).resolve().is_relative_to(scripts)}
    cases = json.loads(args.fixtures.read_text(encoding="utf-8"))["cases"]
    case = next(row for row in cases if row["id"] == args.case[0])
    before = board_for(case)
    args.output.mkdir(parents=True, exist_ok=False)
    row = {"fixture": case, "source_root": str(source), "imports": imported,
        "imported_script_modules": imported_scripts,
        "module_sha256": {name: sha(path) for name, path in imported.items()},
        "binary_sha256": sha(args.binary), "configured_movetime_ms": args.movetime_ms,
        "extra_budget_ceiling_ms": boss_play.extra_budget(args.movetime_ms),
        "policies": {"boss": boss_play.POLICY, "operations": gambit_operations.POLICY,
            "repertoire": gambit_repertoire_play.POLICY, "knowledge": gambit_knowledge.POLICY},
        "engines": [], "error": None, "checks": {}, "scope": "One actual HumanGame move with an authored full prefix; no move injected."}
    if args.import_check:
        row.update(passed=True, import_check_only=True)
        write(args.output / "case.json", row)
        return 0
    events, engines, host = [], [], None
    stream = (args.output / "native-searches.jsonl").open("x", encoding="utf-8")
    try:
        def factory(path):
            engine = native_engine(path, boss=True)
            engines.append(engine)
            wrapped = TraceEngine(engine, stream, row["engines"], events)
            settings = {"Hash": 16, "UsePieceActivity": True,
                        "UseNeuralEvaluation": False, "UseNNUEEvaluation": False}
            if "UseQuiescenceForcing" in engine.options:
                settings["UseQuiescenceForcing"] = False
            wrapped.configure(settings)
            return wrapped
        host = play_human.HumanGame(args.binary, args.output / "host", engine_factory=factory,
            hint_engine_factory=lambda _: DisplayOnlyPeer(), evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
            neural_model=None)
        host.new_game("black" if case["color"] == "white" else "white", args.movetime_ms,
                      host.revision, boss_mode=True)
        host.board = before.copy(stack=True)
        host._opening_seed = case["seed"]
        host.revision += 1
        host._sync_evaluation()
        host._save()
        row["pre_context"] = {"knowledge": gambit_knowledge.context(before),
            "operations": gambit_operations.context(before)}
        started = time.monotonic()
        state = host.engine_move(host.revision)
        row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        if state.get("error"):
            raise RuntimeError(state["error"])
        if len(host.board.move_stack) != len(before.move_stack) + 1:
            raise AssertionError("Host did not append exactly one move")
        move = host.board.peek()
        receipt = host._boss_decisions.get(len(host.board.move_stack), {})
        row.update(selected_uci=move.uci(), selected_san=before.san(move), receipt=safe(receipt),
                   after_fen=host.board.fen(en_passant="fen"))
        searches = {(event["instance"], event["search"]): event for event in events if event["event"] == "search"}
        baseline = next((event for event in searches.values() if event["fen"] == case["before_fen"]
                         and event["root_moves"] is None), None)
        returned = any(event["event"] == "bestmove" and event["move"] == move.uci()
            and searches[(event["instance"], event["search"])]["fen"] == case["before_fen"] for event in events)
        replay = before.copy(stack=True)
        replay.push(move)
        row["checks"] = {"legal_exact_append": move in before.legal_moves and replay == host.board,
            "receipt_matches_root": receipt.get("before_fen") == case["before_fen"]
                and receipt.get("history_uci") == case["history_uci"],
            "receipt_matches_selection": receipt.get("selected_uci") == move.uci(),
            "selected_returned_by_own_native": returned,
            "full_baseline_requested": baseline is not None and baseline["requested_seconds"] == args.movetime_ms / 1000,
            "baseline_budget_preserved": receipt.get("baseline_budget_ms") == args.movetime_ms,
            "extra_budget_bounded": 0 <= receipt.get("extra_budget_ms", -1) <= boss_play.extra_budget(args.movetime_ms) + .001}
        row["native_search_count"] = len(searches)
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if host is not None:
            try:
                host.close()
            except Exception as error:
                row["error"] = row["error"] or f"cleanup: {type(error).__name__}: {error}"
        row["engine_exit_codes"] = []
        for engine in engines:
            try:
                row["engine_exit_codes"].append(engine.returncode.result(timeout=3))
            except Exception:
                row["engine_exit_codes"].append(None)
        stream.close()
    row["passed"] = (row["error"] is None and bool(row["checks"]) and all(row["checks"].values())
                     and bool(engines) and all(code == 0 for code in row["engine_exit_codes"]))
    write(args.output / "case.json", row)
    return 0 if row["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--import-check", action="store_true")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--baseline-source", type=Path, default=BASELINE)
    parser.add_argument("--candidate-source", type=Path)
    parser.add_argument("--snapshot-candidate", type=Path)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--binary", type=Path, default=RUNTIME)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--movetime-ms", type=int, default=500)
    parser.add_argument("--case", action="append")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--audit-depth", type=int, default=18)
    parser.add_argument("--audit-ms", type=int, default=1000)
    parser.add_argument("--audit-scope", choices=("all", "changed-or-studied-completed"), default="all")
    args = parser.parse_args(argv)
    if args.movetime_ms < 500:
        parser.error("Production gambit policy requires movetime-ms >=500")
    args.binary = args.binary.resolve(strict=True)
    args.fixtures = args.fixtures.resolve(strict=True)
    if args.snapshot_candidate:
        snapshot_candidate(args.snapshot_candidate.resolve())
        if args.output is None:
            print(str(args.snapshot_candidate.resolve()))
            return 0
        args.candidate_source = args.snapshot_candidate.resolve()
    if args.output is None:
        parser.error("--output is required for validation")
    args.output = args.output.resolve()
    if args.worker:
        if args.source_root is None or not args.case or len(args.case) != 1:
            parser.error("Worker needs source-root and exactly one case")
        return worker(args)
    document = json.loads(args.fixtures.read_text(encoding="utf-8"))
    cases = [case for case in document["cases"] if not args.case or case["id"] in args.case]
    if not cases or args.case and set(args.case) != {case["id"] for case in cases}:
        parser.error("Unknown or empty fixture selection")
    for case in cases:
        board_for(case)
    if not args.baseline_only and args.candidate_source is None:
        parser.error("Provide frozen candidate-source or use baseline-only")
    args.output.mkdir(parents=True, exist_ok=False)
    immutable_binary = args.output / "native.exe"
    shutil.copy2(args.binary, immutable_binary)
    roots = {"baseline": args.baseline_source.resolve(strict=True)}
    if not args.baseline_only:
        roots["candidate"] = args.candidate_source.resolve(strict=True)
    source_manifests = {}
    for arm, source in roots.items():
        manifest = source / "source-manifest.json"
        if not manifest.exists():
            manifest = source / "baseline-manifest.json"
        source_manifests[arm] = json.loads(manifest.read_text(encoding="utf-8"))
        if not all(sha(source / entry["path"]) == entry["sha256"] for entry in source_manifests[arm]["files"]):
            raise ValueError("Frozen source/data manifest changed: " + arm)
    report = {"schema": "regicide.opening-growth-host-ab.v1", "started_utc": utc(),
        "cases_declared_before_queries": cases, "source_roots": {k: str(v) for k, v in roots.items()},
        "source_manifests": source_manifests,
        "fixture_sha256": sha(args.fixtures), "harness_sha256": sha(Path(__file__)),
        "native_sha256": sha(immutable_binary), "baseline_ms": args.movetime_ms,
        "environment": {"platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "logical_cpus": os.cpu_count(), "python": sys.version},
        "audit_scope": args.audit_scope,
        "scope": "Separate production HumanGame source imports, same native/options/request ceilings; not equal measured CPU or shared baseline packets. Existing roots/new branches/training overlap reported separately. No complete games/Elo.",
        "native_before_external_audit": True, "rows": [], "errors": []}
    write(args.output / "predeclared.json", report)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for index, case in enumerate(cases):
        arms = list(roots)
        if index % 2:
            arms.reverse()
        row = {"fixture": case, "order": arms, "arms": {}}
        report["rows"].append(row)
        for arm in arms:
            output = args.output / (case["id"] + "-" + arm)
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--source-root", str(roots[arm]),
                "--binary", str(immutable_binary), "--fixtures", str(args.fixtures), "--case", case["id"],
                "--movetime-ms", str(args.movetime_ms), "--output", str(output)]
            if args.import_check:
                command.append("--import-check")
            run = subprocess.run(command, cwd=roots[arm], capture_output=True, text=True, encoding="utf-8", timeout=60, **flags)
            (args.output / (case["id"] + "-" + arm + ".stdout.txt")).write_text(run.stdout, encoding="utf-8")
            (args.output / (case["id"] + "-" + arm + ".stderr.txt")).write_text(run.stderr, encoding="utf-8")
            value = json.loads((output / "case.json").read_text(encoding="utf-8")) if (output / "case.json").exists() else {}
            row["arms"][arm] = {"returncode": run.returncode, **value}
            if run.returncode or not value.get("passed"):
                report["errors"].append({"case": case["id"], "arm": arm, "error": value.get("error") or run.stderr[-1000:]})
            write(args.output / "report.json", report)
        row["selected_move_changed"] = (row["arms"]["baseline"].get("selected_uci") != row["arms"].get("candidate", {}).get("selected_uci")) if "candidate" in roots else None
        if "candidate" in roots and not args.import_check:
            row["native_baseline_move_same"] = (row["arms"]["baseline"].get("receipt", {}).get("baseline_uci")
                == row["arms"]["candidate"].get("receipt", {}).get("baseline_uci"))
        print(json.dumps({"case": case["id"], "selected": {k: v.get("selected_uci") for k, v in row["arms"].items()},
            "passed": all(v.get("passed") for v in row["arms"].values())}), flush=True)
    report["native_selection_finished_utc"] = utc()
    if args.stockfish and not report["errors"] and not args.import_check:
        sys.path.insert(0, str(ROOT / "tests"))
        import chess.engine
        from tactical_horizon_validation import sf_audit
        audit_binary = args.stockfish.resolve(strict=True)
        report["audit_engine"] = {"sha256": sha(audit_binary), "depth_cap": args.audit_depth,
            "milliseconds_cap": args.audit_ms, "settings": {"Threads": 1, "Hash": 32,
                "UCI_LimitStrength": False, "Skill Level": 20}}
        audit_logger = logging.getLogger("chess.engine")
        old_level = audit_logger.level
        audit_handler = logging.FileHandler(args.output / "audit-raw-uci.log", encoding="utf-8")
        audit_logger.setLevel(logging.DEBUG)
        audit_logger.addHandler(audit_handler)
        engine = chess.engine.SimpleEngine.popen_uci([str(audit_binary)], timeout=10, **flags)
        try:
            engine.configure(report["audit_engine"]["settings"])
            report["audit_engine"]["id"] = dict(engine.id)
            for row in report["rows"]:
                row["studied_iteration_completed"] = studied_iteration_completed(row)
                if (args.audit_scope == "changed-or-studied-completed"
                        and not row["selected_move_changed"] and not row["studied_iteration_completed"]):
                    row["post_selection_audit"] = {"status": "skipped_by_predeclared_scope",
                        "reason": "No selected move change or completed deeper studied iteration."}
                    continue
                moves = [v["selected_uci"] for v in row["arms"].values()]
                row["post_selection_audit"] = sf_audit(engine, board_for(row["fixture"]), moves,
                    seconds=args.audit_ms / 1000, depth=args.audit_depth)
                write(args.output / "report.json", report)
        finally:
            engine.quit()
            report["audit_exit_code"] = engine.returncode.result(timeout=3)
            engine.close()
            audit_logger.removeHandler(audit_handler)
            audit_logger.setLevel(old_level)
            audit_handler.close()
    report.update(finished_utc=utc(), binary_unchanged=sha(immutable_binary) == report["native_sha256"],
                  fixture_unchanged=sha(args.fixtures) == report["fixture_sha256"])
    report["sources_unchanged"] = all(sha(roots[arm] / entry["path"]) == entry["sha256"]
        for arm, manifest in source_manifests.items() for entry in manifest["files"])
    report["passed"] = (not report["errors"] and report["binary_unchanged"]
        and report["fixture_unchanged"] and report["sources_unchanged"])
    if args.stockfish and not args.import_check:
        allowed = {"common_exact_depth"}
        if args.audit_scope == "changed-or-studied-completed":
            allowed.add("skipped_by_predeclared_scope")
        report["passed"] = report["passed"] and report.get("audit_exit_code") == 0 and all(
            row.get("post_selection_audit", {}).get("status") in allowed for row in report["rows"])
    write(args.output / "report.json", report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
