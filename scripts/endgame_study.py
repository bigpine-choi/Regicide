"""Finite, preregistered endgame data study with the unchanged promotion gate.

Prepare creates one persistent attempt and stops with frozen candidates. Validate
resumes that attempt in a clean resource window. Development matches never choose
the candidate or call the champion registry. Completed training games accumulate
even if the fixed candidate is rejected.
"""

import argparse
from collections import Counter
import copy
import json
import math
from pathlib import Path
import secrets
import shutil
import sys
import uuid

import collect_learning_data as DATA
import learning_cycle as CYCLE
from learning_corpus import _source

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = ROOT / "docs/checks/2026-10-19-neural-cost-quality/run/quality/manifest.json"
POLICY = "endgame-scale-first-seed-largest-preregistered-arm-v1"


def write_summary(run, report):
    lines = ["# 종반 자료 연구", "", f"- 상태: {report['status']}",
             f"- 영속 시도: {report['attempt_index']}",
             f"- 새 모델 채택: {report.get('adopted', False)}", "",
             "후보는 첫 시드의 가장 큰 사전 자료 규모로 고정한다. 개발 성적과 시험 오차로 후보를 바꾸지 않는다.", ""]
    if "candidate" in report:
        lines += [f"- 새 학습 위치 추가량: {report['candidate']['added_train']}",
                  f"- 후보 시드: {report['candidate']['seed']}",
                  f"- 후보 SHA256: {report['candidate']['model_sha256']}"]
    for row in report.get("development", []):
        lines.append(f"- 개발 대국, 추가 {row['added_train']}위치: {row['summary']}")
    if "arena" in report:
        decision = report["arena"]["decision"]
        lines += [f"- 예약 대국 점수율: {decision.get('candidate_pair_score')}",
                  f"- 채택 하한: {decision.get('one_sided_lower_bound')}",
                  f"- 채택 기준 통과: {decision.get('passed')}"]
    if report.get("error"):
        lines += ["", "실패: " + report["error"]]
    lines += ["", "상세 근거: cycle.json, diagnosis/, endgame-train/, endgame-train-quality/,",
              "endgame-heldout-quality/, curve/, compute-control/, development-*/, arena/", "",
              "규칙 정확성·정답 일치·개발 대국·채택 판정을 구분한다. 기존 모델은 기준 통과 때만 바꾼다.", ""]
    (Path(run) / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def frozen_sources():
    paths = list((ROOT / "scripts").glob("*.py")) + list((ROOT / "tests").glob("*.py"))
    paths += list((ROOT / "src").glob("*.cpp")) + list((ROOT / "include/regicide").glob("*.hpp"))
    paths += [ROOT / "tests/data" / name for name in (
        "learning_openings_v1.json", "learning_arena_openings_v1.json", "endgame_learning_seeds_v1.json")]
    return {path.relative_to(ROOT).as_posix(): DATA.sha256_file(path) for path in sorted(paths)}


def combine_quality(train, heldout, output):
    """Append exact quality rows/games/PGNs; never rewrite held-out rows."""
    train, heldout, output = Path(train), Path(heldout), Path(output)
    sources = [_source(path, allow_empty_splits=True) for path in (heldout, train)]
    if any(row["split"] == "train" for row in sources[0]["positions"]):
        raise ValueError("endgame heldout contains training rows")
    if any(row["split"] != "train" for row in sources[1]["positions"]):
        raise ValueError("endgame training quality contains held-out rows")
    if any(game["split"] == "train" for game in sources[0]["games"]) or any(
            game["split"] != "train" for game in sources[1]["games"]):
        raise ValueError("endgame game split disagrees with its source role")
    ids = [game["game_id"] for source in sources for game in source["games"]]
    if len(ids) != len(set(ids)):
        raise ValueError("endgame source game IDs collide")
    destination = output.resolve()
    output = destination.with_name(destination.name + ".partial")
    if destination.exists() or output.exists():
        raise FileExistsError(destination if destination.exists() else output)
    if output.parent != destination.parent:
        raise ValueError("augmentation staging directory escaped its destination parent")
    output.mkdir(parents=True, exist_ok=False)
    for field, name in (("games_path", "games.jsonl"), ("positions_path", "positions.jsonl"), ("pgn_path", "games.pgn")):
        chunks = []
        for source in sources:
            relative = source["manifest"][field]
            raw = (source["path"].parent / relative).read_bytes()
            if raw and not raw.endswith(b"\n"):
                raise ValueError("source must terminate its last row without changing heldout bytes")
            chunks.append(raw)
        (output / name).write_bytes(b"".join(chunks))
    counts = Counter(row["split"] for source in sources for row in source["positions"])
    families = {}
    for source in sources:
        for family, split in source["metadata"]["family_splits"].items():
            if family in families and families[family] != split:
                raise ValueError("endgame family crosses split")
            families[family] = split
    manifest = {"schema": DATA.SCHEMA, "status": "complete", "games_path": "games.jsonl",
                "positions_path": "positions.jsonl", "samples_path": "positions.jsonl", "pgn_path": "games.pgn",
                "positions": sum(counts.values()), "positions_by_split": dict(counts),
                "completed_games": sum(game["completed"] for source in sources for game in source["games"]),
                "family_splits": families,
                "sources": [{"manifest_sha256": source["sha256"], "files_sha256": source["files_sha256"]} for source in sources],
                "scope": "new endgame augmentation; original bootstrap heldouts remain separate and unchanged",
                "files_sha256": {name: DATA.sha256_file(output / name) for name in ("games.jsonl", "positions.jsonl", "games.pgn")}}
    DATA.checkpoint_json(output / "manifest.json", manifest)
    try:
        _source(output / "manifest.json", allow_empty_splits=True)
    except BaseException:
        manifest["status"] = "failed"
        DATA.checkpoint_json(output / "manifest.json", manifest)
        raise
    output.rename(destination)
    return manifest


def verify_frozen(report, run):
    if frozen_sources() != report["implementation_sha256"]:
        raise ValueError("Study implementation changed; candidate cannot be adopted")
    for relative, digest in report.get("study_artifact_sha256", {}).items():
        path = CYCLE.contained(run, relative)
        if DATA.sha256_file(path) != digest:
            raise ValueError("Study evidence changed: " + relative)
    if DATA.sha256_file(report["installed_binary"]) != report["binary_sha256"]:
        raise ValueError("Study engine changed; candidate cannot be adopted")


def fixed_candidate(curve, report, run):
    if curve.get("status") != "complete" or not curve.get("arms"):
        raise ValueError("learning curve must be complete")
    last = curve["arms"][-1]
    sizes = [arm["added_train"] for arm in curve["arms"]]
    if (sizes != curve["feasible_added_train"] or sizes != sorted(set(sizes))
            or any(size not in curve["targets_added_train"] for size in sizes)):
        raise ValueError("preregistered arm order or sizes changed")
    first = last["runs"][0]
    expected = {"added_train": last["added_train"], "seed": curve["training_seeds"][0],
                "model_path": "curve/" + first["model_path"],
                "model_sha256": first["model_sha256"], "selected_epoch": first["selected_epoch"]}
    declared = {**curve["candidate"], "model_path": "curve/" + curve["candidate"]["model_path"]}
    if expected != declared or expected != report["candidate"] or first["seed"] != expected["seed"]:
        raise ValueError("fixed largest-arm first-seed candidate was changed")
    model = CYCLE.contained(run, expected["model_path"])
    if DATA.sha256_file(model) != expected["model_sha256"]:
        raise ValueError("fixed candidate bytes changed")
    return model


def compute_controls(curve, curve_directory, output):
    """Repeat baseline passes to approximately match largest-arm Adam work.

    These models are diagnostic only. They cannot change the fixed nominee.
    Epoch granularity leaves at most half a baseline epoch's update difference.
    """
    import train_neural as TRAIN
    from neural_model import initialize_model, export_model
    curve_directory, output = Path(curve_directory), Path(output)
    first, largest = curve["arms"][0], curve["arms"][-1]
    if first["added_train"] != 0:
        raise ValueError("compute control needs the unchanged baseline arm")
    base_path = curve_directory / "added-000000/dataset/manifest.json"
    batches, _ = TRAIN.load_dataset(base_path, model_version=2)
    extra_path = curve_directory / curve["sources"]["endgame"]["snapshot"]
    extra, _ = TRAIN.load_dataset(extra_path, model_version=2, allow_empty_splits=True)
    per_epoch = math.ceil(len(batches["train"][0]) / curve["batch_size"])
    epochs = max(1, round(largest["adam_updates"] / per_epoch))
    output.mkdir(parents=True, exist_ok=False)
    report = {"scope": "baseline-only approximate matched-update companion, no candidate selection",
              "target_updates": largest["adam_updates"], "actual_updates": epochs * per_epoch,
              "epochs": epochs, "baseline_updates_per_epoch": per_epoch, "runs": []}
    for seed in curve["training_seeds"]:
        initial = initialize_model(seed, version=2)
        model, metrics = TRAIN.train(batches, epochs=epochs, batch_size=curve["batch_size"],
            seed=seed, learning_rate=curve["learning_rate"], initial_model=initial,
            model_version=2, l2=curve["l2"])
        target = output / f"seed-{seed}.nn"
        export_model(model, target)
        report["runs"].append({"seed": seed, "model_sha256": TRAIN.sha256(target),
            "metrics": metrics, "independent_endgame": {split: {
                "samples": len(extra[split][0]), "selected_mse": TRAIN.mse(model, extra[split])}
                for split in ("validation", "test") if len(extra[split][0])}})
    DATA.checkpoint_json(output / "report.json", report)
    return report


def prepare(args, state, catalog):
    from collect_endgame_data import collect, parse_args as collection_args, load_seeds, DEFAULT_SEEDS
    from diagnose_endgames import run_diagnosis
    from refine_endgame_holdout import refine_holdout
    from teacher_quality import refine_dataset
    from endgame_learning_curve import run_curve
    from learning_registry import load_champion

    baseline = Path(args.baseline).resolve(strict=True)
    _source(baseline)
    attempt = catalog["attempts_started"] + 1
    run = state / "runs" / f"{attempt:06d}-{uuid.uuid4().hex[:12]}"
    run.mkdir(parents=True)
    catalog["attempts_started"] = attempt
    catalog["runs"].append(run.relative_to(state).as_posix())
    CYCLE.atomic_json(state / "catalog.json", catalog)
    report = {"schema": CYCLE.SCHEMA, "study_schema": "regicide.endgame.study.v1", "policy": POLICY,
              "status": "running", "attempt_index": attempt, "started_utc": DATA.utc_now(),
              "checkout": DATA.checkout_provenance(),
              "stages": [], "adopted": False, "binary_sha256": None,
              "installed_binary": str(Path(args.binary).resolve()),
              "previous_model_sha256": None,
              "implementation_sha256": frozen_sources(),
              "candidate_policy": "first declared seed, largest feasible preregistered added-data count; never selected using test/development games/arena",
              "configuration": {"seed": args.seed, "train_repeats": args.train_repeats,
                    "add_counts": [0, 128, 512, 1024], "training_seeds": [2026101901, 2026101902],
                    "teacher_quality_nodes": [10000, 50000, 200000], "quality_cp_delta": 30,
                    "quality_min_depth": 3, "epochs": 100, "batch_size": 64,
                    "development_pairs": 16, "arena_pairs": 32, "movetime_ms": 100}}
    def save():
        CYCLE.atomic_json(run / "cycle.json", report)
        CYCLE.atomic_json(state / "latest.json", {"run": run.relative_to(state).as_posix(), **report})
        write_summary(run, report)
    def stage(name):
        report["stages"].append({"name": name, "started_utc": DATA.utc_now()})
        save()
        print(name, flush=True)
    try:
        stage("snapshot-and-previous-champion")
        binary, digest = CYCLE.snapshot_binary(args.binary, run)
        champion = load_champion(state, args.binary)
        report.update(binary_sha256=digest, previous_model_sha256=champion["model_sha256"] if champion else None)
        save()
        stage("training-loss-endgame-diagnosis")
        datasets = [CYCLE.contained(state, catalog["bootstrap"]["manifest"]),
                    *(CYCLE.contained(state, item["manifest"]) for item in catalog["datasets"])]
        diagnosis = run_diagnosis(datasets, binary, run / "diagnosis", max_games=12,
                                 max_positions_per_game=2, common_depth=4, depth_nodes=200000,
                                 teacher_nodes=(50000, 200000), min_loss_cp=100, stability_cp=30)
        report["diagnosis"] = {key: diagnosis.get(key) for key in ("available_endgame_games", "queries_completed", "summary")}
        exclusions = []
        for name in catalog["runs"][:-1]:
            arena = CYCLE.contained(state, name) / "arena/arena.json"
            if arena.is_file():
                exclusions += ["--exclude-games", str(arena)]
        common = ["--binary", str(binary), "--nodes", "1000", "--teacher-nodes", "10000",
                  "--max-plies", "384", "--sample-every", "2",
                  "--exclude-heldout-dataset", str(CYCLE.contained(state, catalog["bootstrap"]["manifest"])),
                  "--exclude-dataset", str(baseline), *exclusions]
        for reference in catalog.get("endgame_holdouts", []):
            common += ["--exclude-dataset", str(CYCLE.contained(state, reference["manifest"]))]
        # These starting FENs are fixed by construction before training/results.
        _, starts = load_seeds(DEFAULT_SEEDS, "heldout")
        openings = {"schema": "regicide.endgame.development.openings.v1", "positions": [
            {"name": item["id"], "fen": item["initial_fen"], "moves": list(item.get("moves", []))}
            for item in starts["openings"]]}
        DATA.checkpoint_json(run / "development-starts.json", openings)
        stage("collect-independent-endgame-heldout")
        collect(collection_args([*common, "--output", str(run / "endgame-heldout"),
              "--seed", str(args.seed), "--split", "heldout", "--games-per-opening", "1"]))
        stage("freeze-trusted-new-heldout-labels")
        heldout_quality = refine_holdout(run / "endgame-heldout/manifest.json", binary, run / "endgame-heldout-quality")
        if any(heldout_quality.get("positions_by_split", {}).get(split, 0) < 1
               for split in ("validation", "test")):
            raise ValueError("Trusted new validation and test endgame positions are both required")
        catalog.setdefault("endgame_holdouts", []).append(
            CYCLE.dataset_reference(state, run / "endgame-heldout/manifest.json"))
        CYCLE.atomic_json(state / "catalog.json", catalog)
        stage("collect-diverse-endgame-training")
        collection = collect(collection_args([*common, "--output", str(run / "endgame-train"),
              "--seed", str(args.seed + 1), "--split", "train", "--games-per-opening", str(args.train_repeats),
              "--exclude-dataset", str(run / "endgame-heldout/manifest.json")]))
        if collection.get("status") != "complete" or collection.get("errors"):
            raise ValueError("endgame training collection incomplete")
        # Actual completed games accumulate even if the later model loses.
        catalog["datasets"].append(CYCLE.dataset_reference(state, run / "endgame-train/manifest.json"))
        CYCLE.atomic_json(state / "catalog.json", catalog)
        stage("trusted-training-labels")
        quality = refine_dataset(run / "endgame-train/manifest.json", binary, run / "endgame-train-quality",
                                 allow_train_only_endgame=True)
        if quality.get("positions_by_split", {}).get("train", 0) < 128:
            raise ValueError("Fewer than128 reliable new endgame positions; collect more before fitting")
        stage("immutable-augmentation")
        combine_quality(run / "endgame-train-quality/manifest.json", run / "endgame-heldout-quality/manifest.json", run / "augmentation")
        stage("nested-data-learning-curve")
        curve = run_curve(baseline, run / "augmentation/manifest.json", run / "curve")
        report["candidate"] = {**curve["candidate"], "model_path": "curve/" + curve["candidate"]["model_path"]}
        candidate = CYCLE.contained(run, report["candidate"]["model_path"])
        if DATA.sha256_file(candidate) != report["candidate"]["model_sha256"]:
            raise ValueError("curve candidate identity mismatch")
        stage("baseline-matched-update-companion")
        report["compute_control"] = compute_controls(curve, run / "curve", run / "compute-control")
        stage("rule-regressions-before-validation")
        report["rules"] = CYCLE.run_rule_checks(binary, candidate, run / "rules")
        report["study_artifact_sha256"] = {path.relative_to(run).as_posix(): DATA.sha256_file(path)
            for path in run.rglob("*") if path.is_file() and path.name not in ("cycle.json", "summary.md")}
        verify_frozen(report, run)
        report.update(status="ready_for_validation", finished_preparation_utc=DATA.utc_now())
        save()
        print("READY_FOR_CLEAN_VALIDATION", run, flush=True)
        return report
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error), finished_utc=DATA.utc_now())
        save()
        raise


def validate(args, state, catalog):
    from endgame_matches import run_matches
    from learning_arena import run_arena
    from learning_registry import load_champion, promote
    run = CYCLE.contained(state, args.run)
    report = json.loads((run / "cycle.json").read_text(encoding="utf-8"))
    if (report.get("status") != "ready_for_validation" or report.get("policy") != POLICY
            or report["attempt_index"] != catalog["attempts_started"]
            or catalog["runs"][-1] != args.run):
        raise ValueError("validation must resume the current untouched prepared attempt")
    def save():
        CYCLE.atomic_json(run / "cycle.json", report)
        CYCLE.atomic_json(state / "latest.json", {"run": args.run, **report})
        write_summary(run, report)
    try:
        verify_frozen(report, run)
        binary = run / ("regicide.exe" if sys.platform == "win32" else "regicide")
        if DATA.sha256_file(args.binary) != report["binary_sha256"]:
            raise ValueError("validation installed binary differs from prepared engine")
        champion = load_champion(state, args.binary)
        if (champion["model_sha256"] if champion else None) != report["previous_model_sha256"]:
            raise ValueError("previous champion changed")
        curve = json.loads((run / "curve/report.json").read_text(encoding="utf-8"))
        candidate = fixed_candidate(curve, report, run)
        report["development"] = []
        for arm in curve["arms"]:
            model = run / "curve" / arm["runs"][0]["model_path"]
            match = run_matches(binary, model, run / "development-starts.json",
                                run / f"development-{arm['added_train']:06d}", movetime_ms=100)
            if not match["completed"] or not match["valid_for_comparison"]:
                raise ValueError("development match incomplete or contaminated")
            report["development"].append({"added_train": arm["added_train"], "summary": match["summary"]})
            save()
        verify_frozen(report, run)
        report["arena_seed"] = secrets.randbits(64)
        report["arena_seed_policy"] = "Fresh entropy after the fixed candidate weights; development results never select model or seed."
        save()
        candidate = fixed_candidate(curve, report, run)
        arena = run_arena(binary, candidate, Path(champion["model_path"]) if champion else None,
                          run / "arena", pairs=32, movetime_ms=100,
                          seed=report["arena_seed"], attempt_index=report["attempt_index"])
        if arena["status"] != "complete":
            raise ValueError("reserved promotion arena incomplete or dirty")
        report["arena"] = {"status": arena["status"], "decision": arena["decision"]}
        verify_frozen(report, run)
        CYCLE.catalog_read(state)
        if arena["promotion_passed"]:
            selected = promote(state, args.binary, candidate, arena,
                               expected_champion_sha=report["previous_model_sha256"])
            report.update(adopted=True, selected_model_sha256=selected["model_sha256"], generation=selected["generation"])
        else:
            report.update(adopted=False, selected_model_sha256=report["previous_model_sha256"],
                          retained_reason="Fixed candidate failed unchanged promotion criterion")
        report.update(status="complete", finished_utc=DATA.utc_now())
        save()
        return report
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error), finished_utc=DATA.utc_now())
        save()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "validate"))
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=CYCLE.DEFAULT_STATE)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--run", help="state-relative prepared run path for validation")
    parser.add_argument("--seed", type=int, default=2026101950)
    parser.add_argument("--train-repeats", type=int, default=4)
    args = parser.parse_args(argv)
    if args.seed < 0 or args.train_repeats < 1 or (args.mode == "validate" and not args.run):
        parser.error("nonnegative seed, positive repeats and validation run path required")
    state = args.state.resolve()
    try:
        with CYCLE.StateLock(state), CYCLE.KeepAwake():
            catalog = CYCLE.catalog_read(state)
            (prepare if args.mode == "prepare" else validate)(args, state, catalog)
        return 0
    except KeyboardInterrupt:
        print("Study interrupted; previous champion retained, attempt index consumed.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"endgame study failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
