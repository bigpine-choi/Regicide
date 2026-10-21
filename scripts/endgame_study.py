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
import subprocess
import sys
import uuid
import zipfile

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
        early = report["arena"].get("status") == "rejected_early_futile"
        label = "조기 기각 전 관측 점수율(전체 계획의 기력 추정 아님)" if early else "예약 대국 점수율"
        lines += [f"- {label}: {decision.get('candidate_pair_score')}",
                  f"- 실제 완료 쌍: {decision.get('completed_pairs')}",
                  f"- 채택 하한: {decision.get('one_sided_lower_bound')}",
                  f"- 채택 기준 통과: {decision.get('passed')}"]
        if early:
            lines += ["- 남은 모든 쌍에서 만점을 얻어도 기존 최종 기준에 미달하므로 미채택했다."]
    if report.get("error"):
        lines += ["", "실패: " + report["error"]]
    lines += ["", "상세 근거: cycle.json, diagnosis/, endgame-train/, endgame-train-quality/,",
              "endgame-heldout-quality/, curve/, compute-control/, development-*/, arena/", "",
              "규칙 정확성·정답 일치·개발 대국·채택 판정을 구분한다. 기존 모델은 기준 통과 때만 바꾼다.", ""]
    (Path(run) / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def frozen_sources():
    paths = [path for directory in (ROOT / "scripts", ROOT / "tests")
             for path in directory.rglob("*.py") if path.is_file()]
    paths += list((ROOT / "src").glob("*.cpp")) + list((ROOT / "include/regicide").glob("*.hpp"))
    paths += [ROOT / "tests/data" / name for name in (
        "learning_openings_v1.json", "learning_arena_openings_v1.json", "endgame_learning_seeds_v1.json")]
    paths += list((ROOT / ".github/workflows").glob("*.yml"))
    paths += list((ROOT / ".github/workflows").glob("*.yaml"))
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
        separator = b""
        if (name == "games.pgn" and all(chunks) and
                not chunks[0].endswith((b"\n\n", b"\r\n\r\n"))):
            # A PGN result line and the next game's headers need a blank line.
            # Insert only the boundary byte; every source byte remains intact.
            separator = b"\n"
        (output / name).write_bytes(separator.join(chunks))
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


def reuse_completed_data(state, catalog, source_relative, destination, binary_sha, configuration):
    """Reuse immutable completed data from an earlier failure before any fitting.

    A new attempt is still consumed. Registered source datasets stay registered
    once, and the failed run and its incomplete augmentation remain untouched.
    This never resumes a candidate selected after observing model results.
    """
    import teacher_quality as QUALITY
    import refine_endgame_holdout as HOLDOUT
    from collect_endgame_data import DEFAULT_SEEDS, load_seeds

    state, destination = Path(state).resolve(), Path(destination).resolve()
    source = CYCLE.contained(state, source_relative)
    runs = catalog["runs"]
    if (source_relative not in runs or source == destination or
            source_relative == runs[-1] or not destination.is_relative_to(state) or
            destination.relative_to(state).as_posix() != runs[-1]):
        raise ValueError("data reuse requires an earlier registered run and the new current destination")
    origin = json.loads((source / "cycle.json").read_text(encoding="utf-8"))
    if (origin.get("schema") != CYCLE.SCHEMA or origin.get("study_schema") != "regicide.endgame.study.v1" or
            origin.get("policy") != POLICY or origin.get("status") != "failed" or
            origin.get("attempt_index") != runs.index(source_relative) + 1 or
            origin["attempt_index"] >= catalog["attempts_started"] or "candidate" in origin or
            (source / "curve").exists() or any(stage.get("name") == "nested-data-learning-curve"
                                               for stage in origin.get("stages", []))):
        raise ValueError("data reuse requires a failed earlier study before any learning-curve fitting")
    if origin.get("binary_sha256") != binary_sha:
        raise ValueError("reused data belongs to a different engine binary")
    data_fields = ("seed", "train_repeats", "teacher_quality_nodes", "quality_cp_delta", "quality_min_depth")
    if any(field not in configuration or origin.get("configuration", {}).get(field) != configuration[field]
           for field in data_fields):
        raise ValueError("reused data configuration differs from the new study")
    allowed = {"cycle.json", "summary.md", "regicide.exe" if sys.platform == "win32" else "regicide"}
    if not destination.is_dir() or any(path.name not in allowed for path in destination.iterdir()):
        raise ValueError("data reuse destination must be a new empty preparation run")
    original_binary = source / ("regicide.exe" if sys.platform == "win32" else "regicide")
    if DATA.sha256_file(original_binary) != binary_sha:
        raise ValueError("reused original engine snapshot changed")
    target_binary = destination / original_binary.name
    if target_binary.exists() and DATA.sha256_file(target_binary) != binary_sha:
        raise ValueError("new preparation engine snapshot differs from reused data")

    datasets = {name: _source(source / name / "manifest.json", allow_empty_splits=True)
                for name in ("endgame-heldout", "endgame-heldout-quality", "endgame-train", "endgame-train-quality")}
    fixture_sha = DATA.sha256_file(DEFAULT_SEEDS)
    for name, split, entries, expected_seed in (
            ("endgame-heldout", "heldout", catalog.get("endgame_holdouts", []), configuration["seed"]),
            ("endgame-train", "train", catalog["datasets"], configuration["seed"] + 1)):
        dataset = datasets[name]
        manifest = dataset["manifest"]
        reference = CYCLE.dataset_reference(state, dataset["path"])
        if sum(item == reference for item in entries) != 1:
            raise ValueError("reused raw dataset is not registered exactly once with its original hashes")
        endgame = manifest.get("endgame", {})
        seed_definition, opening_definition = load_seeds(DEFAULT_SEEDS, split)
        expected_games = len(opening_definition["openings"]) * (configuration["train_repeats"] if split == "train" else 1)
        if (manifest.get("schema") != DATA.SCHEMA or manifest.get("status") != "complete" or
                manifest.get("errors") or endgame.get("schema") != "regicide.endgame.collection.v1" or
                endgame.get("selected_split") != split or endgame.get("seed_file_sha256") != fixture_sha or
                endgame.get("seed_definition") != seed_definition or
                manifest.get("games_requested") != expected_games or len(dataset["games"]) != expected_games or
                manifest.get("seed") != expected_seed or manifest.get("engine", {}).get("sha256") != binary_sha or
                manifest.get("engine", {}).get("configured_options") != DATA.OPTIONS or
                manifest.get("engine", {}).get("selfplay_model", {}).get("enabled") is not False or
                not all(game["completed"] for game in dataset["games"])):
            raise ValueError("reused raw endgame dataset has a different role, engine, seed or collection policy")
        limits = manifest.get("limits", {})
        expected_limits = {"selfplay_nodes": 1000, "teacher_nodes": 10000, "max_plies_total": 384,
                           "sample_every": 2, "random_opening_plies": 2, "time_limit": None}
        if any(limits.get(key) != value for key, value in expected_limits.items()):
            raise ValueError("reused collection node/sampling configuration changed")
        required_splits = {"train"} if split == "train" else {"validation", "test"}
        if any(game["split"] not in required_splits for game in dataset["games"]):
            raise ValueError("reused collection contains a game with the wrong split")
        sidecar = source / (name + ".endgame-openings.json")
        if (not sidecar.is_file() or manifest.get("openings", {}).get("sha256") != DATA.sha256_file(sidecar) or
                json.loads(sidecar.read_text(encoding="utf-8")) != opening_definition):
            raise ValueError("reused authored opening sidecar changed")

    _, starts = load_seeds(DEFAULT_SEEDS, "heldout")
    expected_development = {"schema": "regicide.endgame.development.openings.v1", "positions": [
        {"name": item["id"], "fen": item["initial_fen"], "moves": list(item.get("moves", []))}
        for item in starts["openings"]]}
    if json.loads((source / "development-starts.json").read_text(encoding="utf-8")) != expected_development:
        raise ValueError("reused declared development starts changed")

    frozen_quality = {}
    for raw_name, quality_name, filename, initial_holdout in (
            ("endgame-heldout", "endgame-heldout-quality", "holdout-quality-report.json", True),
            ("endgame-train", "endgame-train-quality", "quality-report.json", False)):
        raw, refined = datasets[raw_name], datasets[quality_name]
        manifest = refined["manifest"]
        report_path = refined["path"].parent / filename
        report = json.loads(report_path.read_text(encoding="utf-8"))
        policy = QUALITY._configuration(configuration["teacher_quality_nodes"],
                                        configuration["quality_cp_delta"], configuration["quality_min_depth"])
        actual_policy = report.get("policy", {})
        critical = ("node_budgets", "max_last_two_cp_delta", "minimum_completed_depth", "teacher", "context",
                    "selection", "stronger_than_original")
        if (manifest.get("status") != "complete" or report.get("status") != "complete" or report.get("errors") or
                report.get("schema") != (HOLDOUT.SCHEMA if initial_holdout else QUALITY.SCHEMA) or
                any(actual_policy.get(key) != policy[key] for key in critical) or
                report.get("policy_sha256") != QUALITY._digest(actual_policy) or
                report.get("source_manifest_sha256") != raw["sha256"] or
                report.get("source_files_sha256") != raw["files_sha256"] or
                (not initial_holdout and manifest.get("source_files_sha256") != raw["files_sha256"]) or
                manifest.get("source_manifest_sha256", manifest.get("endgame_holdout_refinement", {}).get("source_manifest_sha256")) != raw["sha256"]):
            raise ValueError("reused teacher-quality policy or source identity changed")
        if initial_holdout:
            identity = manifest.get("endgame_holdout_refinement", {})
            if identity.get("schema") != HOLDOUT.SCHEMA or identity.get("policy_sha256") != report["policy_sha256"]:
                raise ValueError("reused initial holdout quality identity changed")
        elif (manifest.get("teacher_quality_schema") != QUALITY.SCHEMA or
              manifest.get("quality_report_sha256") != DATA.sha256_file(report_path) or
              manifest.get("policy_sha256") != report["policy_sha256"]):
            raise ValueError("reused training quality report hash changed")
        frozen = report.get("frozen_inputs_sha256", {})
        required = ({"binary", "source_manifest", "reserved_openings"} |
                    {"source/" + name for name in raw["files_sha256"]} |
                    {"scripts/" + name for name in ("teacher_quality.py", "collect_learning_data.py", "learning_corpus.py",
                                                    "train_neural.py", "neural_model.py", "classical_evaluation.py")})
        if initial_holdout:
            required |= {"initial_holdout_wrapper", "endgame_collector", "endgame_seed_definition"}
        if not required.issubset(frozen) or frozen.get("binary") != binary_sha:
            raise ValueError("reused teacher-quality frozen identities are incomplete")
        known = {"binary": original_binary, "source_manifest": raw["path"], "reserved_openings": QUALITY.RESERVED_OPENINGS,
                 "initial_holdout_wrapper": Path(HOLDOUT.__file__),
                 "endgame_collector": ROOT / "scripts/collect_endgame_data.py", "endgame_seed_definition": DEFAULT_SEEDS}
        for label, digest in frozen.items():
            path = (raw["path"].parent / label[7:] if label.startswith("source/") else
                    ROOT / label if label.startswith("scripts/") else known.get(label))
            if path is None or DATA.sha256_file(path) != digest:
                raise ValueError("reused teacher-quality frozen source changed: " + label)
        for field in ("games_path", "pgn_path"):
            if (raw["path"].parent / raw["manifest"][field]).read_bytes() != (refined["path"].parent / manifest[field]).read_bytes():
                raise ValueError("reused quality stage changed completed games or PGN bytes")
        records = report.get("records", [])
        if len(records) != len(raw["positions"]) or report.get("queries_completed") != 3 * len(records):
            raise ValueError("reused quality stage does not cover every source position")
        accepted = {}
        for index, record in enumerate(records):
            row = raw["positions"][index]
            if record.get("source_row_index") != index or record.get("history") != row["history"] or record.get("fen") != row["fen"]:
                raise ValueError("reused quality record root changed")
            stages = record.get("stages", [])
            if [stage.get("requested_nodes") for stage in stages] != policy["node_budgets"]:
                raise ValueError("reused teacher-query ladder changed")
            label, reason = QUALITY.select_label(stages, row["teacher_nodes"], policy)
            if record.get("accepted") != (reason is None) or record.get("exclusion") != reason:
                raise ValueError("reused teacher label acceptance changed")
            if reason is None:
                accepted[(row["game_id"], row["ply"])] = label
        if len(accepted) != len(refined["positions"]):
            raise ValueError("reused quality accepted-position count changed")
        for row in refined["positions"]:
            label = accepted.get((row["game_id"], row["ply"]))
            if label is None or any(row.get(key) != value for key, value in label.items()):
                raise ValueError("reused quality output differs from its final accepted teacher label")
        frozen_quality[quality_name] = DATA.sha256_file(report_path)
    if (len(datasets["endgame-train-quality"]["positions"]) < 128 or any(
            not any(row["split"] == split for row in datasets["endgame-heldout-quality"]["positions"])
            for split in ("validation", "test"))):
        raise ValueError("reused data requires >=128 reliable train positions and nonempty new validation/test")

    names = ["diagnosis", "endgame-heldout", "endgame-heldout-quality", "endgame-train", "endgame-train-quality",
             "development-starts.json", "endgame-heldout.endgame-openings.json", "endgame-train.endgame-openings.json"]
    files = {}
    for name in names:
        path = source / name
        if not path.exists() or path.is_symlink():
            raise ValueError("reusable data artifact missing or symbolic: " + name)
        for item in ([path] if path.is_file() else sorted(path.rglob("*"))):
            if item.is_symlink():
                raise ValueError("reusable data artifacts cannot contain symbolic links")
            if item.is_file():
                files[item.relative_to(source).as_posix()] = DATA.sha256_file(item)
    origin_sha = DATA.sha256_file(source / "cycle.json")
    for name in names:
        before, after = source / name, destination / name
        if before.is_dir():
            shutil.copytree(before, after)
        else:
            shutil.copyfile(before, after)
    for relative, digest in files.items():
        if DATA.sha256_file(source / relative) != digest or DATA.sha256_file(destination / relative) != digest:
            raise ValueError("reused source changed during verified copy")
    if DATA.sha256_file(source / "cycle.json") != origin_sha:
        raise ValueError("failed source run changed during data reuse")
    return {"schema": "regicide.endgame.data-reuse.v1", "source_run": source_relative,
            "source_attempt_index": origin["attempt_index"], "source_cycle_sha256": origin_sha,
            "source_configuration": {field: origin["configuration"][field] for field in data_fields},
            "files_sha256": files, "quality_reports_sha256": frozen_quality,
            "diagnosis_summary": copy.deepcopy(origin.get("diagnosis", {})),
            "catalog_policy": "source raw train/heldout registrations retained exactly once; no duplicate registration",
            "scope": "completed immutable actual games and trusted labels only; previous failed run and fitting state never resumed"}


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
              "validation_profile": ("fixed-nodes-remote-time-v1" if getattr(args, "remote_validation", False)
                                     else "original-local-time-v1"),
              "implementation_sha256": frozen_sources(),
              "candidate_policy": "first declared seed, largest feasible preregistered added-data count; never selected using test/development games/arena",
              "configuration": {"seed": args.seed, "train_repeats": args.train_repeats,
                    "add_counts": [0, 128, 512, 1024], "training_seeds": [2026101901, 2026101902],
                    "teacher_quality_nodes": [10000, 50000, 200000], "quality_cp_delta": 30,
                    "quality_min_depth": 3, "epochs": 100, "batch_size": 64,
                    "development_pairs": 16, "arena_pairs": 32, "movetime_ms": 100,
                    "development_nodes": 1000 if getattr(args, "remote_validation", False) else None,
                    "validation_location": "github-actions-windows" if getattr(args, "remote_validation", False) else "local"}}
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
        reuse_source = getattr(args, "reuse_data_run", None)
        if reuse_source:
            stage("reuse-completed-endgame-data")
            report["data_reuse"] = reuse_completed_data(state, catalog, reuse_source, run,
                                                         digest, report["configuration"])
            report["diagnosis"] = copy.deepcopy(report["data_reuse"]["diagnosis_summary"])
            save()
        else:
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
    if report.get("validation_profile") == "fixed-nodes-remote-time-v1":
        raise ValueError("This study requires fixed-node development and remote validation; no local idle-PC arena")
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


def _current_report(args, state, catalog, status):
    run = CYCLE.contained(state, args.run)
    report = json.loads((run / "cycle.json").read_text(encoding="utf-8"))
    if (report.get("status") != status or report.get("policy") != POLICY
            or report["attempt_index"] != catalog["attempts_started"]
            or catalog["runs"][-1] != args.run):
        raise ValueError("Only the current untouched study may continue")
    verify_frozen(report, run)
    if DATA.sha256_file(args.binary) != report["binary_sha256"]:
        raise ValueError("The installed study engine changed")
    return run, report


def _save_report(state, run, report):
    CYCLE.atomic_json(run / "cycle.json", report)
    CYCLE.atomic_json(state / "latest.json", {"run": run.resolve().relative_to(Path(state).resolve()).as_posix(), **report})
    write_summary(run, report)


def develop(args, state, catalog):
    """Fixed-node development games require no idle-PC comparison window."""
    from endgame_matches import run_matches
    run, report = _current_report(args, state, catalog, "ready_for_validation")
    if report.get("validation_profile") != "fixed-nodes-remote-time-v1":
        raise ValueError("The fixed-node remote profile must be declared before fitting")
    try:
        curve = json.loads((run / "curve/report.json").read_text(encoding="utf-8"))
        fixed_candidate(curve, report, run)
        report["development"] = []
        for arm in curve["arms"]:
            model = run / "curve" / arm["runs"][0]["model_path"]
            snapshot = run / ("regicide.exe" if sys.platform == "win32" else "regicide")
            result = run_matches(snapshot, model, run / "development-starts.json",
                                 run / f"development-{arm['added_train']:06d}", nodes=1000)
            if not result["completed"] or not result["valid_for_comparison"]:
                raise ValueError("Fixed-node development games did not complete validly")
            report["development"].append({"added_train": arm["added_train"],
                "nodes_per_move": 1000, "summary": result["summary"],
                "scope": "Equal node limits, no timing or equal-time playing-strength claim"})
            _save_report(state, run, report)
        verify_frozen(report, run)
        report["study_artifact_sha256"] = {path.relative_to(run).as_posix(): DATA.sha256_file(path)
            for path in run.rglob("*") if path.is_file() and path.name not in ("cycle.json", "summary.md")}
        report.update(status="ready_for_remote_validation", finished_development_utc=DATA.utc_now())
        _save_report(state, run, report)
        return report
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error), finished_utc=DATA.utc_now())
        _save_report(state, run, report)
        raise


def export_remote(args, state, catalog):
    """Bind exact native/model bytes to a source-verified separate Windows runner."""
    from learning_registry import load_champion
    run, report = _current_report(args, state, catalog, "ready_for_remote_validation")
    curve = json.loads((run / "curve/report.json").read_text(encoding="utf-8"))
    candidate = fixed_candidate(curve, report, run)
    champion = load_champion(state, args.binary)
    if (champion["model_sha256"] if champion else None) != report["previous_model_sha256"]:
        raise ValueError("Previous champion changed before remote validation")
    destination = Path(args.request_directory).resolve()
    if not destination.is_relative_to(ROOT.resolve()) or destination == ROOT.resolve():
        raise ValueError("Remote input directory must remain inside this repository")
    if destination.exists():
        raise FileExistsError(destination)
    implementation = report["checkout"]["commit"]
    if subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip() != implementation:
        raise ValueError("Commit the final implementation before preparing the study")
    source_blobs = {}
    for relative in report["implementation_sha256"]:
        subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", relative], check=True)
        raw = subprocess.check_output(["git", "-C", str(ROOT), "show", implementation + ":" + relative])
        source_blobs[relative] = __import__("hashlib").sha256(raw).hexdigest()
    destination.mkdir(parents=True, exist_ok=False)
    archive = destination / "engine.zip"
    member = zipfile.ZipInfo("regicide.exe", date_time=(1980, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    member.external_attr = 0o100644 << 16
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(member, (run / "regicide.exe").read_bytes(), compresslevel=9)
    model = destination / "candidate.nn"
    model.write_bytes(candidate.read_bytes())
    old_model = destination / "champion.nn" if champion else None
    if old_model:
        old_model.write_bytes(Path(champion["model_path"]).read_bytes())
    request = {"schema": "regicide.learning.remote-request.v1",
        "repository": "bigpine-choi/Regicide",
        "attempt_index": report["attempt_index"], "pairs": 32, "movetime_ms": 100,
        "seed": secrets.randbits(64), "binary_zip": archive.relative_to(ROOT).as_posix(),
        "binary_zip_sha256": DATA.sha256_file(archive), "binary_sha256": report["binary_sha256"],
        "candidate_model": model.relative_to(ROOT).as_posix(), "candidate_sha256": report["candidate"]["model_sha256"],
        "champion_model": old_model.relative_to(ROOT).as_posix() if old_model else None,
        "champion_sha256": report["previous_model_sha256"],
        "source_files_sha256": source_blobs, "source_hash_basis": "git_blob_bytes",
        "implementation_commit": implementation, "prepared_run": args.run,
        "output_prefix": "docs/checks/2026-10-19-endgame-learning-scale/remote-result",
        "candidate_policy": report["candidate_policy"],
        "criterion": "Unchanged full promotion gate; optional mathematically certain rejection only; never early acceptance"}
    request_path = destination / "remote-request.json"
    DATA.checkpoint_json(request_path, request)
    request_sha = DATA.sha256_file(request_path)
    report["remote_request"] = {"path": request_path.relative_to(ROOT).as_posix(), "sha256": request_sha,
        "expected_result_branch": f"validation/endgame-{report['attempt_index']}-{request_sha[:12]}",
        "payload_sha256": {path.relative_to(ROOT).as_posix(): DATA.sha256_file(path)
                           for path in destination.iterdir() if path.is_file()},
        "source_hash_basis": "Committed Git blobs; local raw-byte freeze is retained separately",
        "seed": request["seed"], "exported_utc": DATA.utc_now()}
    report["status"] = "waiting_for_remote_validation"
    _save_report(state, run, report)
    print("REMOTE_REQUEST", request_path, request_sha, flush=True)
    return report


def import_remote(args, state, catalog):
    from remote_arena_import import verify_and_import
    run, report = _current_report(args, state, catalog, "waiting_for_remote_validation")
    try:
        curve = json.loads((run / "curve/report.json").read_text(encoding="utf-8"))
        fixed_candidate(curve, report, run)
        CYCLE.catalog_read(state)
        result = verify_and_import(run, state, args.binary, report, args.remote_results, args.workflow_receipt)
        report.update(result, status="complete", finished_utc=DATA.utc_now())
        _save_report(state, run, report)
        return report
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error), finished_utc=DATA.utc_now())
        _save_report(state, run, report)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "validate", "develop", "export-remote", "import-remote"))
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=CYCLE.DEFAULT_STATE)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--run", help="state-relative prepared run path for validation")
    parser.add_argument("--seed", type=int, default=2026101950)
    parser.add_argument("--train-repeats", type=int, default=4)
    parser.add_argument("--reuse-data-run", help="Earlier failed pre-fitting run with complete registered data; never reset attempts")
    parser.add_argument("--remote-validation", action="store_true", help="Fixed-node development and a separate Windows time arena")
    parser.add_argument("--request-directory", type=Path)
    parser.add_argument("--remote-results", type=Path)
    parser.add_argument("--workflow-receipt", type=Path)
    args = parser.parse_args(argv)
    if args.seed < 0 or args.train_repeats < 1 or (args.mode != "prepare" and not args.run):
        parser.error("nonnegative seed, positive repeats and validation run path required")
    if args.mode == "export-remote" and not args.request_directory:
        parser.error("export-remote requires --request-directory")
    if args.mode == "import-remote" and (not args.remote_results or not args.workflow_receipt):
        parser.error("import-remote requires results and a verified workflow receipt")
    state = args.state.resolve()
    try:
        with CYCLE.StateLock(state), CYCLE.KeepAwake():
            catalog = CYCLE.catalog_read(state)
            {"prepare": prepare, "validate": validate, "develop": develop,
             "export-remote": export_remote, "import-remote": import_remote}[args.mode](args, state, catalog)
        return 0
    except KeyboardInterrupt:
        print("Study interrupted; previous champion retained, attempt index consumed.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"endgame study failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
