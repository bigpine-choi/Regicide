"""Predeclared multi-seed gambit creativity A/B with native-only decisions.

Separate production hosts use frozen source roots and identical budget ceilings.
Only after every native selection does SF audit the union of selected moves per
position, so all seeds of one root share an exact completed audit depth.
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
FIXTURES = ROOT / "tests/data/gambit_creativity_2026_11_06.json"
BASELINE = ROOT / "build/gambit-creativity/baseline-source"
RUNTIME = ROOT / "build/runtime-engines/5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba/regicide.exe"
ACCEPTANCE = {"maximum_changed_pair_loss_increase_cp": 50,
              "changed_pair_loss_sum_must_not_increase": True,
              "minimum_roots_with_increased_unique_moves": 1,
              "minimum_selected_reply_retained_motifs": 1,
              "require_stable_native_baseline_for_variety_attribution": True,
              "require_named_motif_positive_after_reply": True,
              "require_new_unique_choice_to_be_actually_creative": True}


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
    write(destination / "source-manifest.json", {"schema": "regicide.gambit-creativity-candidate.v1",
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
    if "gambit_creativity" in sys.modules:
        modules.append(sys.modules["gambit_creativity"])
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
        baseline_reply = (next((event.get("move") for event in events if event["event"] == "bestmove"
            and event["instance"] == baseline["instance"] and event["search"] == baseline["search"]), None)
            if baseline else None)
        detail = receipt.get("creativity") or {}
        evidence = detail.get("evidence") or {}
        paired_searches_valid = True
        if detail.get("applied") and evidence.get("comparison_kind") == "paired_unrestricted_children":
            for name in ("baseline", "candidate"):
                child = evidence.get("paired_child_" + name) or {}
                witnesses = [event for event in searches.values()
                    if event["fen"] == child.get("fen") and event["history_uci"] == child.get("history_uci")
                    and event["root_moves"] is None and event["requested_depth"] == evidence.get("child_depth")]
                paired_searches_valid = paired_searches_valid and any(
                    event["event"] == "bestmove" and event["move"] == child.get("returned_uci")
                    and event["instance"] == search_event["instance"] and event["search"] == search_event["search"]
                    for search_event in witnesses for event in events)
        returned = any(event["event"] == "bestmove" and event["move"] == move.uci()
            and searches[(event["instance"], event["search"])]["fen"] == case["before_fen"] for event in events)
        replay = before.copy(stack=True)
        replay.push(move)
        row["checks"] = {"legal_exact_append": move in before.legal_moves and replay == host.board,
            "receipt_matches_root": receipt.get("before_fen") == case["before_fen"]
                and receipt.get("history_uci") == case["history_uci"],
            "receipt_matches_selection": receipt.get("selected_uci") == move.uci(),
            "selected_returned_by_own_native": returned,
            "native_baseline_receipt_matches_returned": baseline_reply is not None and receipt.get("baseline_uci") == baseline_reply,
            "paired_child_searches_unrestricted": paired_searches_valid,
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


def _cp_line(board, line, depth, first=None, minimum=False):
    if (not isinstance(line, dict) or type(line.get("cp")) is not int
            or type(line.get("depth")) is not int
            or (line["depth"] < depth if minimum else line["depth"] != depth)
            or line.get("lowerbound") or line.get("upperbound")
            or not isinstance(line.get("pv_uci"), list) or not line["pv_uci"]
            or first is not None and line["pv_uci"][0] != first):
        return False
    if "cp_white" in line and line["cp_white"] != (line["cp"] if board.turn else -line["cp"]):
        return False
    replay = board.copy(stack=True)
    try:
        for uci in line["pv_uci"]:
            replay.push_uci(uci)
    except (TypeError, ValueError):
        return False
    return True


def native_quality_evidence(arm):
    """Audit normalized native receipts without treating child CP as root CP.

    Runtime code validates the original exact packets. This accounting check
    independently binds their normalized depths, legal PVs, signs and child
    states. The raw native requests and packets remain in native-searches.jsonl.
    """
    try:
        receipt = arm["receipt"]
        detail = receipt["creativity"]
        evidence = detail["evidence"]
        board = board_for(arm["fixture"])
        baseline, common, final = detail["baseline"], evidence["candidate_compared"], evidence["candidate_final"]
        depth, base_move, selected = baseline["depth"], receipt["baseline_uci"], arm["selected_uci"]
        if (depth < 4 or not _cp_line(board, baseline, depth, base_move)
                or not _cp_line(board, common, depth, selected)
                or not _cp_line(board, final, depth, selected, minimum=True)
                or final["depth"] == depth and final["cp"] != common["cp"]):
            return {"verified": False, "reason": "root_evidence_invalid"}
        root_loss = baseline["cp"] - min(common["cp"], final["cp"])
        if final["depth"] > depth and final["cp"] < baseline["cp"]:
            return {"verified": False, "reason": "deeper_root_warning_cannot_be_erased"}
        if evidence.get("comparison_kind") != "paired_unrestricted_children":
            return {"verified": root_loss <= 0 and detail.get("loss_cp") == 0 and evidence.get("loss_cp") == 0,
                    "reason": "root_same_depth_zero_loss", "root_loss_cp": root_loss}
        child_depth = depth - 1
        if (not 0 < root_loss <= 10 or evidence.get("root_loss_cp") != root_loss
                or detail.get("root_loss_cp") != root_loss
                or evidence.get("child_depth") != child_depth or detail.get("child_depth") != child_depth
                or evidence.get("paired_loss_cp") != 0 or detail.get("paired_loss_cp") != 0
                or evidence.get("loss_cp") != 0 or detail.get("loss_cp") != 0
                or detail.get("comparison_kind") != "paired_unrestricted_children"):
            return {"verified": False, "reason": "child_admission_contract_invalid"}
        children = {}
        for name, move in (("baseline", base_move), ("candidate", selected)):
            child = board.copy(stack=True)
            child.push_uci(move)
            data = evidence["paired_child_" + name]
            exact, last = data["common"], data["final"]
            if (data.get("fen") != child.fen(en_passant="fen")
                    or data.get("history_uci") != [m.uci() for m in child.move_stack]
                    or not _cp_line(child, exact, child_depth, data.get("returned_uci"))
                    or not _cp_line(child, last, child_depth, data.get("returned_uci"))
                    or not data.get("returned_uci")
                    or last != exact):
                return {"verified": False, "reason": "child_state_or_score_invalid"}
            children[name] = data
        parent_baseline = max(-children["baseline"][key]["cp"] for key in ("common", "final"))
        parent_candidate = min(-children["candidate"][key]["cp"] for key in ("common", "final"))
        if parent_candidate < parent_baseline:
            return {"verified": False, "reason": "child_zero_loss_not_met"}
        for key, name, move, part in (("baseline_compared", "baseline", base_move, "common"),
                ("selection_compared", "candidate", selected, "common"),
                ("selection_final", "candidate", selected, "final")):
            parent_line, child_line = evidence[key], children[name][part]
            if (parent_line.get("cp") != -child_line["cp"]
                    or parent_line.get("depth") != child_line["depth"] + 1
                    or parent_line.get("child_depth") != child_line["depth"]
                    or parent_line.get("depth_basis") != "fixed_root_ply_plus_completed_child_depth"
                    or parent_line.get("pv_uci") != [move, *child_line["pv_uci"]]
                    or not _cp_line(board, parent_line, child_line["depth"] + 1, move)):
                return {"verified": False, "reason": "parent_child_evidence_mixed"}
        return {"verified": True, "reason": "paired_unrestricted_children",
                "root_loss_cp": root_loss, "child_depth": child_depth,
                "baseline_parent_cp": parent_baseline, "candidate_parent_cp": parent_candidate,
                "paired_loss_cp": 0}
    except (KeyError, TypeError, ValueError):
        return {"verified": False, "reason": "missing_native_quality_evidence"}


def creative_selection(arm):
    receipt = arm.get("receipt") or {}
    detail = receipt.get("creativity") or {}
    evidence = detail.get("evidence") or {}
    selected = (detail.get("applied") is True and detail.get("selected_uci") == arm.get("selected_uci")
                and receipt.get("selected_uci") == arm.get("selected_uci"))
    signals = evidence.get("retained_motifs") or {}
    named_signal = signals.get(evidence.get("motif"))
    retained = (selected and evidence.get("retained_after_reply") is True
                and type(named_signal) in (int, float) and named_signal > 0
                and (evidence.get("operations_evidence") or {}).get("settled") is True)
    return {"attempted_applied": detail.get("applied") is True, "actually_selected": selected,
            "reason": detail.get("reason"), "motif": evidence.get("motif"),
            "reply_retained": retained, "retained_motifs": signals,
            "native_quality": native_quality_evidence(arm) if selected else {"verified": False, "reason": "not_selected"},
            "detail_selected_uci": detail.get("selected_uci")}


def summarize(report, fixture):
    rows = report.get("rows", [])
    if not rows or any("candidate" not in row.get("arms", {}) or
                       not all(arm.get("selected_uci") for arm in row["arms"].values()) for row in rows):
        return {"status": "not_a_completed_paired_selection", "acceptance_criteria": ACCEPTANCE}
    grouped, comparisons = {}, []
    for row in rows:
        grouped.setdefault(row["fixture"]["root_id"], []).append(row)
        arms = row["arms"]
        baseline, candidate = arms["baseline"], arms["candidate"]
        changed = baseline["selected_uci"] != candidate["selected_uci"]
        audit = report.get("root_audits", {}).get(row["fixture"]["root_id"], {})
        old_loss = audit.get("loss_cp", {}).get(baseline["selected_uci"])
        new_loss = audit.get("loss_cp", {}).get(candidate["selected_uci"])
        scores = audit.get("scores", {})
        no_mate = all(type(scores.get(arm["selected_uci"], {}).get("cp_root")) is int
                      and scores[arm["selected_uci"]].get("mate_root") is None for arm in arms.values())
        comparable = (audit.get("status") == "common_exact_depth" and no_mate
                      and type(old_loss) is int and type(new_loss) is int)
        comparisons.append({"case_id": row["fixture"]["id"], "root_id": row["fixture"]["root_id"],
            "seed_index": row["fixture"]["seed_index"], "baseline_uci": baseline["selected_uci"],
            "candidate_uci": candidate["selected_uci"], "changed": changed,
            "native_baseline_same": baseline.get("receipt", {}).get("baseline_uci") is not None
                and baseline.get("receipt", {}).get("baseline_uci") == candidate.get("receipt", {}).get("baseline_uci"),
            "audit_depth": audit.get("depth"), "cp_comparable": comparable,
            "baseline_loss_cp": old_loss, "candidate_loss_cp": new_loss,
            "loss_increase_cp": new_loss - old_loss if comparable else None,
            "creativity": creative_selection(candidate)})
    diversity = []
    for root_id, group in grouped.items():
        old = sorted({row["arms"]["baseline"]["selected_uci"] for row in group})
        new = sorted({row["arms"]["candidate"]["selected_uci"] for row in group})
        native = {row["arms"][arm].get("receipt", {}).get("baseline_uci") for row in group for arm in ("baseline", "candidate")}
        stable = None not in native and len(native) == 1
        actually_creative = any(creative_selection(row["arms"]["candidate"])["reply_retained"] for row in group)
        new_choices = set(new) - set(old)
        creative_new = sorted({row["arms"]["candidate"]["selected_uci"] for row in group
            if row["arms"]["candidate"]["selected_uci"] in new_choices
            and creative_selection(row["arms"]["candidate"])["reply_retained"]})
        diversity.append({"root_id": root_id, "seeds": len(group), "baseline_moves": old, "candidate_moves": new,
            "unique_count_increase": len(new) - len(old), "all_native_baselines_same": stable,
            "native_baseline_moves": sorted(move for move in native if move is not None),
            "has_selected_retained_motif": actually_creative,
            "new_choices_with_selected_retained_motif": creative_new,
            "attributable_increase": len(new) > len(old) and stable and bool(creative_new)})
    changed = [row for row in comparisons if row["changed"]]
    comparable = all(row["cp_comparable"] for row in changed)
    old_sum = sum(row["baseline_loss_cp"] for row in changed) if comparable else None
    new_sum = sum(row["candidate_loss_cp"] for row in changed) if comparable else None
    retained_count = sum(row["creativity"]["reply_retained"] for row in comparisons)
    gates = {"complete_predeclared_sample": {row["fixture"]["id"] for row in rows} == {row["id"] for row in fixture["cases"]},
        "functional_integrity": report.get("passed") is True,
        "all_root_audits_complete": all(report.get("root_audits", {}).get(key, {}).get("status") == "common_exact_depth" for key in grouped),
        "changed_choices_have_comparable_nonmate_audits": comparable,
        "no_changed_loss_increase_over_50cp": comparable and all(row["loss_increase_cp"] <= 50 for row in changed),
        "changed_pair_loss_sum_not_increased": comparable and new_sum <= old_sum,
        "at_least_one_root_with_increased_distinct_moves": any(row["unique_count_increase"] > 0 for row in diversity),
        "at_least_one_stable_native_root_with_creative_variety": any(row["attributable_increase"] for row in diversity),
        "all_selected_creativity_has_native_quality_evidence": all(
            row["creativity"]["native_quality"]["verified"] for row in comparisons
            if row["creativity"]["actually_selected"]),
        "at_least_one_actually_selected_reply_retained_motif": retained_count >= 1}
    return {"status": "development_sample_only", "acceptance_criteria": ACCEPTANCE, "gates": gates,
        "sample_acceptance_passed": all(gates.values()), "paired_cases": len(rows), "roots": len(grouped),
        "changed_pairs": len(changed), "native_baseline_same_pairs": sum(row["native_baseline_same"] for row in comparisons),
        "changed_baseline_loss_sum_cp": old_sum, "changed_candidate_loss_sum_cp": new_sum,
        "actually_selected_creativity": sum(row["creativity"]["actually_selected"] for row in comparisons),
        "selected_reply_retained_motifs": retained_count, "diversity": diversity, "comparisons": comparisons,
        "interpretation": "Uniform fixed-seed development observations. Pair sums can repeat a move across seeds; root audits share one common completed depth. No complete games, Elo, or global no-regression claim."}


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
    parser.add_argument("--movetime-ms", type=int, default=1000)
    parser.add_argument("--case", action="append")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--audit-depth", type=int, default=18)
    parser.add_argument("--audit-ms", type=int, default=1000)
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
    report = {"schema": "regicide.gambit-creativity-host-ab.v1", "started_utc": utc(),
        "cases_declared_before_queries": cases, "source_roots": {k: str(v) for k, v in roots.items()},
        "source_manifests": source_manifests,
        "fixture_sha256": sha(args.fixtures), "harness_sha256": sha(Path(__file__)),
        "audit_helper_sha256": sha(ROOT / "tests/tactical_horizon_validation.py"),
        "native_sha256": sha(immutable_binary), "baseline_ms": args.movetime_ms,
        "environment": {"platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "logical_cpus": os.cpu_count(), "python": sys.version},
        "audit_scope": "all_roots_all_selected_moves_across_all_seeds",
        "acceptance_criteria": ACCEPTANCE,
        "scope": "Separate production HumanGame source imports, same native/options/request ceilings; not equal measured CPU or shared baseline packets. Three fixed seeds per public root, source overlaps reported separately. Development-only diversity/quality observation, no complete games/Elo.",
        "native_before_external_audit": True, "rows": [], "errors": []}
    shutil.copy2(Path(__file__), args.output / "harness-executed.py")
    shutil.copy2(args.fixtures, args.output / "fixtures-executed.json")
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
            report["root_audits"] = {}
            grouped = {}
            for row in report["rows"]:
                grouped.setdefault(row["fixture"]["root_id"], []).append(row)
            for root_id, group in grouped.items():
                if len({tuple(row["fixture"]["history_uci"]) for row in group}) != 1:
                    raise ValueError("One root identifier refers to different histories")
                moves = sorted({value["selected_uci"] for row in group for value in row["arms"].values()})
                audit = sf_audit(engine, board_for(group[0]["fixture"]), moves,
                    seconds=args.audit_ms / 1000, depth=args.audit_depth)
                report["root_audits"][root_id] = audit
                for row in group:
                    row["post_selection_audit"] = {"root_id": root_id, "status": audit["status"],
                        "depth": audit.get("depth"), "scope": "Shared root audit across all selected moves and seeds",
                        "loss_cp": {arm: audit.get("loss_cp", {}).get(value["selected_uci"])
                                    for arm, value in row["arms"].items()}}
                write(args.output / "report.json", report)
        finally:
            engine.quit()
            report["audit_exit_code"] = engine.returncode.result(timeout=3)
            engine.close()
            audit_logger.removeHandler(audit_handler)
            audit_logger.setLevel(old_level)
            audit_handler.close()
    report.update(finished_utc=utc(), binary_unchanged=sha(immutable_binary) == report["native_sha256"],
                  fixture_unchanged=sha(args.fixtures) == report["fixture_sha256"],
                  audit_helper_unchanged=sha(ROOT / "tests/tactical_horizon_validation.py") == report["audit_helper_sha256"],
                  harness_unchanged=sha(Path(__file__)) == report["harness_sha256"])
    report["sources_unchanged"] = all(sha(roots[arm] / entry["path"]) == entry["sha256"]
        for arm, manifest in source_manifests.items() for entry in manifest["files"])
    report["passed"] = (not report["errors"] and report["binary_unchanged"]
        and report["fixture_unchanged"] and report["sources_unchanged"]
        and report["audit_helper_unchanged"] and report["harness_unchanged"])
    if args.stockfish and not args.import_check:
        report["passed"] = report["passed"] and report.get("audit_exit_code") == 0 and all(
            row.get("post_selection_audit", {}).get("status") == "common_exact_depth" for row in report["rows"])
    report["experiment_summary"] = summarize(report, document)
    write(args.output / "report.json", report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
