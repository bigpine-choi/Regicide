"""Finite automatic self-play -> loss analysis -> replay -> training -> arena cycles.

Each attempt consumes its persistent statistical index before starting. Rejected
models never replace the champion, but verified training games remain available
for later attempts. No arena game is ever imported into the learning corpus.
"""

import argparse
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "build/learning-cycle"
BOOTSTRAP = ROOT / "docs/checks/2026-10-18-neural-v1/data/manifest.json"
SCHEMA = "regicide.learning.cycle.v1"


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, document):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                     prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class StateLock(AbstractContextManager):
    """An OS-owned lock releases on process exit, including interrupted cycles."""
    def __init__(self, state):
        self.state, self.stream = Path(state), None

    def __enter__(self):
        self.state.mkdir(parents=True, exist_ok=True)
        self.stream = (self.state / "cycle.lock").open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise RuntimeError("Another learning cycle is using this state directory.") from error
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            self.stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()


def windows_execution_state(flags):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    setter = kernel.SetThreadExecutionState
    setter.argtypes, setter.restype = [wintypes.DWORD], wintypes.DWORD
    result = setter(flags)
    if not result:
        raise OSError(ctypes.get_last_error(), "Could not hold the system awake during learning")
    return result


class KeepAwake(AbstractContextManager):
    """Keep only the running learning thread's idle-sleep request, then restore it."""
    def __enter__(self):
        self.previous = windows_execution_state(0x80000001) if sys.platform == "win32" else None
        return self

    def __exit__(self, *_):
        if self.previous is not None:
            try:
                windows_execution_state(self.previous | 0x80000000)
            except OSError as error:
                # Windows also releases thread execution requests on process exit.
                print(f"Could not restore the idle-sleep request: {error}", file=sys.stderr)


def contained(state, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("State paths must be nonempty relative paths.")
    path = (state / relative).resolve()
    if not path.is_relative_to(state) or ".." in Path(relative).parts:
        raise ValueError("State artifact path escapes its directory.")
    return path


def dataset_reference(state, manifest):
    manifest = Path(manifest).resolve(strict=True)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    hashes = {"manifest.json": sha256(manifest)}
    fields = ["games_path", "positions_path"]
    if document.get("pgn_path") is not None:
        fields.append("pgn_path")
    for field in fields:
        relative = document[field]
        path = (manifest.parent / relative).resolve(strict=True)
        if Path(relative).is_absolute() or not path.is_relative_to(manifest.parent):
            raise ValueError("Dataset files must remain inside their own directory.")
        hashes[relative] = sha256(path)
    model = document.get("engine", {}).get("selfplay_model", {})
    if model.get("enabled"):
        relative = model.get("snapshot_path")
        if not isinstance(relative, str):
            raise ValueError("A self-play model needs a saved immutable snapshot.")
        path = (manifest.parent / relative).resolve(strict=True)
        if Path(relative).is_absolute() or not path.is_relative_to(manifest.parent):
            raise ValueError("Self-play model snapshot escapes its dataset.")
        hashes[relative] = sha256(path)
        if hashes[relative] != model.get("sha256"):
            raise ValueError("Self-play model snapshot hash mismatch.")
    return {"manifest": manifest.relative_to(state).as_posix(), "files_sha256": hashes}


def bootstrap_snapshot(state):
    destination = state / "bootstrap"
    record = state / "bootstrap-snapshot.json"
    if destination.exists():
        if not record.is_file():
            raise ValueError("Bootstrap snapshot was interrupted; use a new state directory to preserve evidence.")
        reference = json.loads(record.read_text(encoding="utf-8"))
        if dataset_reference(state, destination / "manifest.json") != reference:
            raise ValueError("Frozen bootstrap snapshot changed.")
        return reference
    source = json.loads(BOOTSTRAP.read_text(encoding="utf-8"))
    destination.mkdir()
    for name in (BOOTSTRAP.name, source["games_path"], source["positions_path"], source["pgn_path"]):
        before = (BOOTSTRAP.parent / name).resolve(strict=True)
        if not before.is_relative_to(BOOTSTRAP.parent.resolve()) or Path(name).is_absolute():
            raise ValueError("Bootstrap file path escapes its dataset.")
        after = destination / name
        after.parent.mkdir(parents=True, exist_ok=True)
        digest = sha256(before)
        shutil.copy2(before, after)
        if sha256(after) != digest or sha256(before) != digest:
            raise ValueError("Bootstrap changed while taking its immutable snapshot.")
    reference = dataset_reference(state, destination / "manifest.json")
    atomic_json(record, reference)
    return reference


def catalog_read(state):
    path = state / "catalog.json"
    if not path.exists():
        if any((state / name).exists() for name in ("runs", "champion.json", "latest.json")):
            raise ValueError("Missing learning catalog with existing history; refusing to reset the attempt budget.")
        return {"schema": SCHEMA, "attempts_started": 0, "datasets": [], "runs": [],
                "bootstrap": bootstrap_snapshot(state)}
    document = json.loads(path.read_text(encoding="utf-8"))
    index = document.get("attempts_started")
    if (document.get("schema") != SCHEMA or type(index) is not int or index < 0
            or not isinstance(document.get("datasets"), list) or not isinstance(document.get("runs"), list)):
        raise ValueError("Invalid learning catalog; refusing to reset attempt history.")
    runs = document["runs"]
    if (len(runs) != index or len(set(runs)) != len(runs)
            or any(not isinstance(name, str) or not name.startswith(f"runs/{number:06d}-")
                   or not contained(state, name).is_dir() for number, name in enumerate(runs, 1))):
        raise ValueError("Attempt counter and saved run history disagree.")
    bootstrap = document.get("bootstrap")
    if (not isinstance(bootstrap, dict) or bootstrap.get("manifest") != "bootstrap/manifest.json"
            or dataset_reference(state, contained(state, bootstrap["manifest"])) != bootstrap):
        raise ValueError("Frozen bootstrap/holdout identity changed.")
    seen = set()
    for reference in document["datasets"]:
        manifest = contained(state, reference["manifest"])
        if reference["manifest"] in seen or dataset_reference(state, manifest) != {
                key: value for key, value in reference.items() if key != "diagnosis"}:
            raise ValueError("An accumulated dataset changed or was registered twice.")
        seen.add(reference["manifest"])
        if reference.get("diagnosis"):
            diagnostic = contained(state, reference["diagnosis"]["path"])
            for name, digest in reference["diagnosis"]["files_sha256"].items():
                if sha256(diagnostic / name) != digest:
                    raise ValueError("An accumulated loss analysis changed.")
    holdouts = document.get("endgame_holdouts", [])
    if not isinstance(holdouts, list):
        raise ValueError("Invalid persistent endgame holdout inventory")
    seen_holdouts = set()
    for reference in holdouts:
        if not isinstance(reference, dict) or "manifest" not in reference:
            raise ValueError("Malformed persistent endgame holdout reference")
        manifest = contained(state, reference["manifest"])
        if reference["manifest"] in seen_holdouts or dataset_reference(state, manifest) != reference:
            raise ValueError("A frozen endgame holdout changed or was registered twice")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("endgame", {}).get("selected_split") != "heldout":
            raise ValueError("Persistent endgame holdout is not a declared heldout collection")
        seen_holdouts.add(reference["manifest"])
    return document


def endgame_holdout_exclusion(state, catalog):
    if not catalog.get("endgame_holdouts"):
        return None
    from collect_endgame_data import ExclusionInventory
    inventory = ExclusionInventory()
    for reference in catalog["endgame_holdouts"]:
        inventory.add_dataset(contained(state, reference["manifest"]))
    return inventory


def snapshot_binary(binary, run):
    source = Path(binary).resolve(strict=True)
    before = sha256(source)
    target = run / ("regicide.exe" if sys.platform == "win32" else "regicide")
    shutil.copy2(source, target)
    if sha256(target) != before or sha256(source) != before:
        raise ValueError("Engine changed while taking the run snapshot.")
    return target, before


def command(binary_or_python, arguments, log, *, cwd=ROOT):
    with Path(log).open("w", encoding="utf-8", newline="\n") as stream:
        result = subprocess.run([str(binary_or_python), *map(str, arguments)], cwd=cwd,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=7200)
    if result.returncode:
        raise RuntimeError(f"Stage failed ({result.returncode}); see {log}")


def run_rule_checks(binary, candidate, output):
    """Keep independent rule and native neural/terminal checks before adoption."""
    output.mkdir()
    command(sys.executable, [ROOT / "tests/differential.py", binary], output / "differential.log")
    command(sys.executable, [ROOT / "tests/neural_integration_tests.py", "--binary", binary],
            output / "neural-integration.log")
    from learning_arena import validate_native_model
    native = validate_native_model(binary, candidate)
    result = {"status": "passed", "binary_sha256": sha256(binary),
              "candidate_sha256": native["model_sha256"],
              "checks": {"independent_rules": sha256(output / "differential.log"),
                         "native_neural_integration": sha256(output / "neural-integration.log")},
              "scope": "Rule correctness and model wiring, not playing strength."}
    atomic_json(output / "checks.json", result)
    return result


def implementation_hashes():
    # Include new evaluator helpers and their regressions automatically. A new
    # dependency such as classical_evaluation.py must not escape source freeze.
    paths = sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "tests").glob("*.py")) + [
        ROOT / "tests/data/learning_openings_v1.json",
        ROOT / "tests/data/learning_arena_openings_v1.json",
        ROOT / "tests/data/endgame_learning_seeds_v1.json"]
    return {path.relative_to(ROOT).as_posix(): sha256(path) for path in paths}


def model_version(path):
    from neural_model import load_model
    return load_model(path).version


def evaluation_design(version):
    if version == 2:
        return {"model_version": 2, "format": "REGICIDE_NN_V2 768 32 16 600 100",
                "score": "classical_cp_stm + 100*tanh(z)", "residual_bound_cp": 100,
                "target": "tanh(teacher_cp_stm/600); no game-outcome mixture",
                "default_l2": .0001}
    return {"model_version": 1, "format": "REGICIDE_NN_V1 768 32 16 600",
            "score": "material_cp_stm + 600*z", "residual_bound_cp": None,
            "target": "0.8*tanh(teacher_cp_stm/600)+0.2*outcome_stm", "default_l2": 0.0}


def run_teacher_quality(dataset, binary, output, *, node_budgets, max_cp_delta, min_depth):
    from teacher_quality import refine_dataset
    original = json.loads(Path(dataset).read_text(encoding="utf-8"))
    result = refine_dataset(dataset, binary, output, node_budgets=tuple(node_budgets),
                            max_cp_delta=max_cp_delta, min_depth=min_depth)
    if result.get("status") != "complete" or result.get("positions_by_split", {}).get("train", 0) < 1:
        raise ValueError("Teacher-quality stage produced no reliable training positions.")
    for split in ("validation", "test"):
        expected = original.get("holdout_rows_sha256", {}).get(split)
        if (not isinstance(expected, str) or len(expected) != 64
                or result.get("holdout_rows_sha256", {}).get(split) != expected):
            raise ValueError("Teacher-quality stage changed the frozen holdout identity.")
    return result


def learning_artifact_hashes(run):
    """Freeze the derived training evidence as well as the candidate weights."""
    return {path.relative_to(run).as_posix(): sha256(path)
            for directory in (run / "corpus", run / "quality")
            for path in sorted(directory.rglob("*")) if path.is_file()}


def write_summary(run, report):
    collection, analysis = report.get("collection", {}), report.get("loss_analysis", {})
    counts = report.get("corpus", {})
    arena = report.get("arena", {}).get("decision", {})
    lines = [f"# 자체 학습 순환 {report['attempt_index']}", "",
             f"상태: {report['status']}. 새 모델 채택: {'예' if report['adopted'] else '아니오'}.", "",
             f"- 이번 자체 대국: 완결 {collection.get('completed_games', '?')}판, 표본 {collection.get('positions', '?')}개",
             f"- 패배 분석: {analysis.get('analyzed_positions', '?')}위치, 학습용 교정 {analysis.get('training_corrections', '?')}개",
             f"- 누적 학습/검증/시험 표본: {counts.get('train', '?')} / {counts.get('validation', '?')} / {counts.get('test', '?')}",
             f"- 교사 안정성 검사를 통과한 학습 위치: {report.get('teacher_quality', {}).get('positions_by_split', {}).get('train', '?')}개",
             f"- 평가 모델: V{report.get('evaluation_design', {}).get('model_version', '?')} — {report.get('evaluation_design', {}).get('score', '?')}",
             f"- 후보 초기화: {report.get('initialization', {}).get('mode', '?')}",
             f"- 대국 검증: 완료 {arena.get('completed_pairs', '?')}쌍, 새 모델 점수 {arena.get('candidate_pair_score')}",
             f"- 채택 기준의 단측 점수 하한: {arena.get('one_sided_lower_bound')} (0.5 초과 필요)", "",
             "기준을 통과하지 못하면 기존 채택 모델을 유지한다. 완료한 학습 자료는 다음 순환에 누적한다.",
             "평가 오차 감소나 이 예약 대국의 통과가 모든 상대·시간 조건에서의 기력 향상을 보장하지는 않는다.", "",
             "상세 기록: cycle.json, loss-analysis/loss-report.json, corpus/manifest.json, quality/manifest.json, quality/quality-report.json, model/report.json, arena/arena.json.", ""]
    (run / "summary.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def run_round(args, state, catalog):
    from collect_learning_data import collect, DEFAULT_OPENINGS
    from analyze_learning_losses import run_analysis
    from learning_corpus import build_corpus
    from learning_arena import run_arena, validate_native_model
    from learning_registry import load_champion, promote

    attempt = catalog["attempts_started"] + 1
    run_name = f"{attempt:06d}-{uuid.uuid4().hex[:12]}"
    runs = state / "runs"
    runs.mkdir(exist_ok=True)
    run = runs / run_name
    run.mkdir()
    catalog["attempts_started"] = attempt
    catalog["runs"].append(run.relative_to(state).as_posix())
    # Persist before any expensive work: failures and interruptions consume alpha.
    atomic_json(state / "catalog.json", catalog)
    report = {"schema": SCHEMA, "status": "running", "attempt_index": attempt,
              "started_utc": utc_now(), "seed": args.seed + attempt * 1009,
              "stages": [], "adopted": False,
              "evaluation_design": evaluation_design(args.model_version),
              "power_policy": {"system_required_during_cycle": sys.platform == "win32",
                               "scope": "Thread-scoped Windows idle-sleep request restored on exit; does not override explicit user sleep or lid closure."},
              "policy": "Losses analyzed; accumulated training only; frozen bootstrap heldouts; reserved arena excluded; old champion kept unless strict paired arena passes."}
    report_path = run / "cycle.json"

    def stage(name):
        report["stages"].append({"name": name, "started_utc": utc_now()})
        atomic_json(report_path, report)
        print(f"[{attempt}] {name}", flush=True)

    try:
        stage("snapshot-and-champion")
        binary, digest = snapshot_binary(args.binary, run)
        champion = load_champion(state, binary)
        model = Path(champion["model_path"]) if champion else None
        previous_sha = champion["model_sha256"] if champion else None
        incumbent_version = model_version(model) if model is not None else None
        compatible = model is not None and incumbent_version == args.model_version
        report["initialization"] = {
            "mode": "warm_start" if compatible else "seeded_random",
            "requested_model_version": args.model_version, "incumbent_model_version": incumbent_version,
            "initial_model_sha256": previous_sha if compatible else None,
            "reason": "compatible_incumbent" if compatible else
                      "incompatible_model_format; incumbent retained for self-play and arena" if model else
                      "no_adopted_neural_model",
            "incumbent_kept_for_selfplay_and_arena": model is not None}
        report.update(binary_sha256=digest, previous_model_sha256=previous_sha,
                      implementation_sha256=implementation_hashes())
        stage("self-play")
        collection = argparse.Namespace(binary=binary, output=run / "data", seed=report["seed"],
                    games_per_opening=args.games_per_opening, nodes=args.nodes, teacher_nodes=args.teacher_nodes,
                    max_plies=args.max_plies, sample_every=args.sample_every, openings=DEFAULT_OPENINGS,
                    source_commit=None, model=model)
        endgame_exclusion = endgame_holdout_exclusion(state, catalog)
        collected = (collect(collection, position_exclusion=endgame_exclusion.reason)
                     if endgame_exclusion is not None else collect(collection))
        if endgame_exclusion is not None:
            endgame_exclusion.verify_sources()
            report["protected_endgame_holdouts"] = endgame_exclusion.sources
        if collected.get("status") != "complete" or collected.get("errors"):
            raise ValueError("Self-play collection did not complete without errors.")
        report["collection"] = {key: collected.get(key) for key in ("completed_games", "positions", "unfinished_games")}
        reference = dataset_reference(state, run / "data/manifest.json")
        catalog["datasets"].append(reference)
        atomic_json(state / "catalog.json", catalog)
        stage("loss-analysis")
        analysis = run_analysis(run / "data/manifest.json", binary, run / "loss-analysis",
                                analysis_nodes=args.analysis_nodes,
                                max_positions_per_game=args.loss_positions_per_game,
                                min_loss_cp=args.min_loss_cp)
        if analysis.get("status") not in ("complete", "completed"):
            raise ValueError("Loss analysis did not complete.")
        report["loss_analysis"] = {"analyzed_positions": len(analysis.get("records", [])),
                                   "training_corrections": analysis.get("positions", 0)}
        reference["diagnosis"] = {"path": (run / "loss-analysis").relative_to(state).as_posix(),
                                  "files_sha256": {name: sha256(run / "loss-analysis" / name)
                                      for name in ("loss-report.json", "positions.jsonl")}}
        atomic_json(state / "catalog.json", catalog)
        stage("accumulated-replay")
        datasets = [contained(state, catalog["bootstrap"]["manifest"]),
                    *(contained(state, item["manifest"]) for item in catalog["datasets"])]
        diagnoses = [contained(state, item["diagnosis"]["path"]) for item in catalog["datasets"]
                     if item.get("diagnosis")]
        corpus_options = {"diagnosis_paths": diagnoses, "fixed_holdout": True}
        if endgame_exclusion is not None:
            corpus_options["position_exclusion"] = endgame_exclusion.reason
        corpus = build_corpus(datasets, run / "corpus", **corpus_options)
        report["corpus"] = corpus.get("positions_by_split", {})
        report["accumulated_datasets"] = len(datasets)
        report["corpus_manifest_sha256"] = sha256(run / "corpus/manifest.json")
        stage("teacher-quality")
        quality = run_teacher_quality(run / "corpus/manifest.json", binary, run / "quality",
                                      node_budgets=args.quality_nodes,
                                      max_cp_delta=args.quality_max_cp_delta,
                                      min_depth=args.quality_min_depth)
        report["teacher_quality"] = {
            "positions_by_split": quality["positions_by_split"],
            "manifest_sha256": sha256(run / "quality/manifest.json"),
            "report_sha256": sha256(run / "quality/quality-report.json"),
            "node_budgets": args.quality_nodes,
            "max_cp_delta": args.quality_max_cp_delta, "min_depth": args.quality_min_depth}
        report["learning_artifact_sha256"] = learning_artifact_hashes(run)
        stage("retraining")
        training_args = [ROOT / "scripts/train_neural.py", "--dataset", run / "quality/manifest.json",
                         "--output", run / "model", "--epochs", args.epochs,
                         "--batch-size", args.batch_size, "--seed", report["seed"],
                         "--model-version", args.model_version]
        if compatible:
            training_args += ["--initial-model", model]
        command(sys.executable, training_args, run / "training.log")
        training = json.loads((run / "model/report.json").read_text(encoding="utf-8"))
        candidate = contained(run / "model", training["model_path"])
        if (training["status"] != "complete" or sha256(candidate) != training["model_sha256"]
                or training["model_version"] != args.model_version or model_version(candidate) != args.model_version):
            raise ValueError("Candidate and training report identities disagree.")
        report["training"] = {key: training.get(key) for key in
                              ("model_version", "model_path", "target", "loss", "regularization", "l2")}
        validate_native_model(binary, candidate)
        report["candidate_sha256"] = sha256(candidate)
        stage("rule-regressions")
        report["rules"] = run_rule_checks(binary, candidate, run / "rules")
        if implementation_hashes() != report["implementation_sha256"]:
            raise ValueError("Learning implementation changed during this attempt; candidate was not adopted.")
        if learning_artifact_hashes(run) != report["learning_artifact_sha256"]:
            raise ValueError("Learning evidence changed before the arena; adoption was blocked.")
        if sha256(candidate) != report["candidate_sha256"] or sha256(binary) != digest:
            raise ValueError("Candidate or frozen engine changed before the arena; adoption was blocked.")
        stage("previous-champion-arena")
        report["arena_seed"] = secrets.randbits(64)
        report["arena_seed_policy"] = "Fresh entropy drawn after the candidate weights were fixed; seed saved for replay."
        atomic_json(report_path, report)
        arena = run_arena(binary, candidate, model, run / "arena", pairs=args.arena_pairs,
                          movetime_ms=args.movetime_ms, seed=report["arena_seed"],
                          attempt_index=attempt)
        report["arena"] = {"status": arena["status"], "decision": arena["decision"]}
        if arena["status"] != "complete":
            raise ValueError("Arena invalid or interrupted; previous champion was retained.")
        stage("adoption-decision")
        if endgame_exclusion is not None:
            endgame_exclusion.verify_sources()
        if implementation_hashes() != report["implementation_sha256"]:
            raise ValueError("Learning implementation changed during the arena; candidate was not adopted.")
        if learning_artifact_hashes(run) != report["learning_artifact_sha256"]:
            raise ValueError("Learning evidence changed during the arena; adoption was blocked.")
        if sha256(candidate) != report["candidate_sha256"] or sha256(binary) != digest:
            raise ValueError("Candidate or frozen engine changed during the arena; adoption was blocked.")
        if arena["promotion_passed"]:
            if sha256(args.binary) != digest:
                raise ValueError("Installed engine changed during the cycle; revalidation required.")
            selected = promote(state, args.binary, candidate, arena, expected_champion_sha=previous_sha)
            report.update(adopted=True, selected_model_sha256=selected["model_sha256"],
                          generation=selected["generation"])
        else:
            report.update(selected_model_sha256=previous_sha,
                          retained_reason="The candidate did not pass the predeclared strength criterion.")
        report.update(status="complete", finished_utc=utc_now())
        atomic_json(report_path, report)
        write_summary(run, report)
        atomic_json(state / "latest.json", {"run": run.relative_to(state).as_posix(), **report})
        print(f"[{attempt}] complete; adopted={report['adopted']}; report={report_path}", flush=True)
        return report
    except BaseException as error:
        report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                      error_type=type(error).__name__, error=str(error), finished_utc=utc_now())
        atomic_json(report_path, report)
        write_summary(run, report)
        atomic_json(state / "latest.json", {"run": run.relative_to(state).as_posix(), **report})
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--binary", type=Path, default=ROOT / ("build/regicide.exe" if sys.platform == "win32" else "build/regicide"))
    result.add_argument("--state", type=Path, default=DEFAULT_STATE)
    result.add_argument("--rounds", type=int, default=1, help="Finite complete cycles; Ctrl+C stops with the champion preserved")
    result.add_argument("--games-per-opening", type=int, default=2)
    result.add_argument("--nodes", type=int, default=1000)
    result.add_argument("--teacher-nodes", type=int, default=10000)
    result.add_argument("--analysis-nodes", type=int, default=50000)
    result.add_argument("--quality-nodes", type=int, nargs=3, default=[10000, 50000, 200000],
                        help="Increasing classical teacher budgets; train labels only, frozen holdouts unchanged")
    result.add_argument("--quality-max-cp-delta", type=int, default=30)
    result.add_argument("--quality-min-depth", type=int, default=3)
    result.add_argument("--loss-positions-per-game", type=int, default=8)
    result.add_argument("--min-loss-cp", type=int, default=100)
    result.add_argument("--max-plies", type=int, default=256)
    result.add_argument("--sample-every", type=int, default=4)
    result.add_argument("--epochs", type=int, default=100)
    result.add_argument("--batch-size", type=int, default=64)
    result.add_argument("--model-version", type=int, choices=(1, 2), default=2,
                        help="2 keeps classical evaluation plus a bounded learned correction; 1 reproduces the legacy model")
    result.add_argument("--seed", type=int, default=20261018)
    result.add_argument("--arena-pairs", type=int, default=32, help="Fewer than 32 pairs are smoke tests and can never promote")
    result.add_argument("--movetime-ms", type=int, default=100)
    return result


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    for name in ("rounds", "games_per_opening", "nodes", "teacher_nodes", "analysis_nodes",
                 "loss_positions_per_game", "min_loss_cp", "max_plies", "sample_every", "epochs",
                 "batch_size", "arena_pairs", "movetime_ms"):
        if getattr(args, name) < 1:
            argument_parser.error(name.replace("_", "-") + " must be positive")
    if (args.quality_nodes[0] < 1000 or any(a >= b for a, b in zip(args.quality_nodes, args.quality_nodes[1:]))
            or args.quality_max_cp_delta < 0 or args.quality_min_depth < 1):
        argument_parser.error("quality budgets must increase from at least 1000, delta must be nonnegative, and depth positive")
    if args.seed < 0:
        argument_parser.error("seed must be nonnegative")
    if not args.binary.is_file():
        argument_parser.error("engine binary is missing")
    state = args.state.resolve()
    try:
        with StateLock(state), KeepAwake():
            catalog = catalog_read(state)
            for _ in range(args.rounds):
                run_round(args, state, catalog)
        return 0
    except KeyboardInterrupt:
        print("Learning stopped; completed data and the previously adopted model were preserved.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Learning cycle failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
