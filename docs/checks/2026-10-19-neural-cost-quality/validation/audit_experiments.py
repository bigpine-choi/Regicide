"""Independent, read-only raw-evidence audit. Run only after all timed work ends.

Uses standard-library arithmetic and python-chess, not production acceptance,
summary, label-selection, replay, neural loader or promotion helpers. It never
launches an engine, changes a checkpoint, or selects a model/hyperparameter.
"""
import collections
import datetime
import hashlib
import io
import itertools
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import traceback

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
import chess
import chess.engine
import chess.pgn
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build/neural-cost-quality"
STATE = ROOT / "build/learning-cycle"
SOURCE = "426830231d5291991a589cf73b058c88b653d1fe"
BASE = "25d37d8173da9e72bbc4b20178e2933ac4640911"
SPLITS = ("train", "validation", "test")
ROLES = ("hce", "zero_v2", "learned_v2")
OPTIONS = {"Hash": 16, "Move Overhead": 0, "UsePositionalEvaluation": True,
           "UseQuiescence": True, "UseTranspositionTable": True,
           "UseFastMoves": True, "UseQuietMoveOrdering": True,
           "UseQuiescenceChecks": True, "PVS": True, "UseSEE": True,
           "UsePawnShield": True, "UsePawnStructure": True, "UseLMR": True}
ASSERTIONS = 0
SECTIONS = []


def check(condition, message):
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(message)


def eq(one, two, message):
    check(one == two, message)


def close(one, two, message):
    check(one is not None and two is not None and math.isclose(one, two, rel_tol=1e-12, abs_tol=1e-12), message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def doc(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines()]


def position_key(board):
    return " ".join(board.fen(en_passant="legal").split()[:4])


def neural_key(board):
    return tuple(sorted((piece.piece_type - 1 + (0 if piece.color == board.turn else 6)) * 64
                        + (square if board.turn else square ^ 56)
                        for square, piece in board.piece_map().items()))


def replay(initial, moves, *, check_claim=False):
    board = chess.Board(initial)
    check(board.is_valid(), "valid initial board")
    for text in moves:
        check(board.outcome(claim_draw=check_claim) is None, "history must stop at rule ending")
        move = chess.Move.from_uci(text)
        check(move in board.legal_moves, "legal history/PV move " + text)
        board.push(move)
    return board


def pv_check(board, pv):
    check(isinstance(pv, list) and bool(pv), "nonempty PV")
    probe = board.copy(stack=True)
    for text in pv:
        check(probe.outcome(claim_draw=False) is None, "PV stops at automatic ending")
        move = chess.Move.from_uci(text)
        check(move in probe.legal_moves, "legal PV move " + text)
        probe.push(move)


def raw_fields(event):
    check(isinstance(event, dict), "raw score event required")
    tokens = event["raw_info"].split()
    eq(tokens[0], "info", "raw event starts info")
    score_index = tokens.index("score")
    kind, value = tokens[score_index + 1], int(tokens[score_index + 2])
    eq(kind, event["kind"], "raw kind")
    eq(value, event["value"], "raw value")
    bound = tokens[score_index + 3] if score_index + 3 < len(tokens) else None
    eq(bound == "upperbound", event["upperbound"], "upperbound flag")
    eq(bound == "lowerbound", event["lowerbound"], "lowerbound flag")
    return {"kind": kind, "value": value, "upperbound": event["upperbound"],
            "lowerbound": event["lowerbound"],
            "depth": int(tokens[tokens.index("depth") + 1]),
            "nodes": int(tokens[tokens.index("nodes") + 1]),
            "pv": tokens[tokens.index("pv") + 1:] if "pv" in tokens else []}


def declared_files(directory, manifest):
    for name, expected in manifest["files_sha256"].items():
        path = (directory / name).resolve()
        check(path.is_relative_to(directory.resolve()), "manifest path contained")
        eq(sha(path), expected, "declared file SHA256 " + name)


def section(name, function):
    before = ASSERTIONS
    try:
        details = function()
        result = {"name": name, "passed": True, "assertions": ASSERTIONS - before,
                  "details": details}
    except Exception as error:
        result = {"name": name, "passed": False, "assertions": ASSERTIONS - before,
                  "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()}
    SECTIONS.append(result)
    print(name + ": " + ("PASS" if result["passed"] else "FAIL " + result["error"]), flush=True)


def source_and_history():
    original = doc(OUT / "original-state-sha256.json")
    eq(len(original), 77, "original inventory size")
    immutable = {name: value for name, value in original.items()
                 if name not in ("catalog.json", "latest.json", "cycle.lock")}
    for name, expected in immutable.items():
        eq(sha(STATE / name), expected, "historical bytes unchanged " + name)
    protected = ["scripts/learning_arena.py", "scripts/learning_registry.py",
                 "tests/data/learning_arena_openings_v1.json", "src/board.cpp", "src/search.cpp",
                 "src/evaluation.cpp", "src/neural.cpp", "src/see.cpp", "src/transposition.cpp",
                 *[path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "include/regicide").glob("*.hpp"))]]
    for name in protected:
        expected = subprocess.check_output(["git", "rev-parse", BASE + ":" + name], cwd=ROOT, text=True).strip()
        actual = subprocess.check_output(["git", "hash-object", "--path", name, name], cwd=ROOT, text=True).strip()
        eq(actual, expected, "protected source unchanged " + name)
    catalog = doc(STATE / "catalog.json")
    eq(catalog["attempts_started"], 4, "persistent attempt is 4")
    eq(len(catalog["runs"]), 4, "four persistent attempts")
    eq(len(catalog["datasets"]), 4, "four immutable source datasets")
    eq(catalog["runs"][-1], RUN.relative_to(STATE).as_posix(), "latest attempt path")
    for reference in [catalog["bootstrap"], *catalog["datasets"]]:
        manifest = STATE / reference["manifest"]
        check("quality" not in reference and "arena" not in reference["manifest"], "catalog contains original learning sources only")
        for name, expected in reference["files_sha256"].items():
            eq(sha(manifest.parent / name), expected, "catalog source identity")
        if "diagnosis" in reference:
            for name, expected in reference["diagnosis"]["files_sha256"].items():
                eq(sha(STATE / reference["diagnosis"]["path"] / name), expected, "catalog diagnosis identity")
    for name, expected in CYCLE["implementation_sha256"].items():
        eq(sha(ROOT / name), expected, "frozen implementation " + name)
    derived = {path.relative_to(RUN).as_posix(): sha(path)
               for directory in (RUN / "corpus", RUN / "quality")
               for path in directory.rglob("*") if path.is_file()}
    eq(derived, CYCLE["learning_artifact_sha256"], "all derived learning evidence frozen")
    eq(sha(RUN / "regicide.exe"), CYCLE["binary_sha256"], "run binary frozen")
    eq(CYCLE["attempt_index"], 4, "report attempt index")
    eq(CYCLE["seed"], 20261018 + 4 * 1009, "training seed from persistent attempt")
    eq(CYCLE["status"], "complete", "cycle complete")
    eq(doc(STATE / "latest.json")["run"], RUN.relative_to(STATE).as_posix(), "latest pointer")
    return {"protected_source_files": len(protected), "historical_files_unchanged": len(immutable),
            "persistent_attempt_before": 3, "persistent_attempt_after": 4,
            "implementation_files": len(CYCLE["implementation_sha256"])}


def quality_data():
    source_dir, target_dir = RUN / "corpus", RUN / "quality"
    source, target = doc(source_dir / "manifest.json"), doc(target_dir / "manifest.json")
    report = doc(target_dir / "quality-report.json")
    eq(report["status"], "complete", "quality report complete")
    eq(target["status"], "complete", "quality manifest complete")
    eq(report["source_manifest_sha256"], sha(source_dir / "manifest.json"), "quality source manifest identity")
    eq(report["source_files_sha256"], source["files_sha256"], "quality source file identities")
    eq(target["source_manifest_sha256"], report["source_manifest_sha256"], "quality manifest source identity")
    eq(target["quality_report_sha256"], sha(target_dir / "quality-report.json"), "quality report identity")
    eq(CYCLE["teacher_quality"]["manifest_sha256"], sha(target_dir / "manifest.json"), "cycle quality identity")
    declared_files(source_dir, source)
    declared_files(target_dir, target)
    for name, expected in report["frozen_inputs_sha256"].items():
        if name == "binary":
            path = RUN / "regicide.exe"
        elif name == "source_manifest":
            path = source_dir / "manifest.json"
        elif name == "reserved_openings":
            path = ROOT / "tests/data/learning_arena_openings_v1.json"
        elif name.startswith("source/"):
            path = source_dir / name[len("source/"):]
        else:
            path = ROOT / name
        eq(sha(path), expected, "quality frozen input " + name)
    check(report["freeze_rechecked"], "quality end freeze rechecked")
    source_rows, output_rows = rows(source_dir / source["positions_path"]), rows(target_dir / target["positions_path"])
    source_lines = (source_dir / source["positions_path"]).read_bytes().splitlines(keepends=True)
    output_lines = (target_dir / target["positions_path"]).read_bytes().splitlines(keepends=True)
    for field in ("games_path", "pgn_path"):
        eq((source_dir / source[field]).read_bytes(), (target_dir / target[field]).read_bytes(), "all game/PGN bytes preserved")
    for split in ("validation", "test"):
        before = [row for row in source_rows if row["split"] == split]
        after = [row for row in output_rows if row["split"] == split]
        eq(after, before, "entire holdout dictionaries preserved")
        eq(b"".join(line for row, line in zip(source_rows, source_lines) if row["split"] == split),
           b"".join(line for row, line in zip(output_rows, output_lines) if row["split"] == split), "exact holdout JSONL bytes preserved")
        eq(canonical_sha(before), source["holdout_rows_sha256"][split], "source holdout hash recomputed")
        eq(canonical_sha(after), target["holdout_rows_sha256"][split], "quality holdout hash recomputed")
        historical = doc(STATE / "runs/000003-2daceeed6656/corpus/manifest.json")
        eq(target["holdout_rows_sha256"][split], historical["holdout_rows_sha256"][split], "historical holdout identity retained")
    games = {game["game_id"]: game for game in rows(source_dir / source["games_path"])}
    eq(len(games), len(rows(source_dir / source["games_path"])), "unique games")
    for game in games.values():
        board = replay(game["initial_fen"], game["history"])
        check(game["completed"], "corpus contains completed source games")
        outcome = board.outcome(claim_draw=True)
        check(outcome is not None, "completed game supported by rules")
        eq(outcome.result(), game["result"], "completed game result")
    policy = report["policy"]
    eq(policy["node_budgets"], [10000, 50000, 200000], "prespecified quality ladder")
    eq(policy["minimum_completed_depth"], 3, "prespecified minimum depth")
    eq(policy["max_last_two_cp_delta"], 30, "prespecified stability threshold")
    eq(canonical_sha(policy), report["policy_sha256"], "policy hash independently recomputed")
    eq(report["engine"]["configured_options"], dict(OPTIONS, UseNeuralEvaluation=False), "classical teacher options")
    records = report["records"]
    train_indices = [i for i, row in enumerate(source_rows) if row["split"] == "train"]
    eq(len(records), 1681, "all prespecified training rows inspected")
    eq([r["source_row_index"] for r in records], train_indices, "exactly train rows searched once")
    eq(report["queries_completed"], 3 * len(records), "three queries each train position")
    selected = {(row["game_id"], row["ply"]): row for row in output_rows if row["split"] == "train"}
    accepted, excluded, pv_plies, seen_feature_splits = 0, collections.Counter(), 0, {}
    for record in records:
        index = record["source_row_index"]
        original = source_rows[index]
        game = games[original["game_id"]]
        eq(original["split"], "train", "only train searched")
        for field in ("split", "family", "initial_fen"):
            eq(original[field], game[field], "sample belongs to actual game " + field)
        for field in ("game_id", "ply", "turn", "fen", "initial_fen", "history"):
            eq(record[field], original[field], "record source field " + field)
        eq(original["history"], game["history"][:original["ply"]], "source history is actual game prefix")
        board = replay(original["initial_fen"], original["history"])
        eq(board.fen(en_passant="fen"), original["fen"], "actual full-history board")
        eq("w" if board.turn else "b", original["turn"], "actual turn preserved")
        values = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
                  chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
        white_material = sum(values[p.piece_type] * (1 if p.color else -1)
                             for p in board.piece_map().values())
        eq(original["material_cp_stm"], white_material if board.turn else -white_material, "material orientation independently recomputed")
        white_result = {"1-0": 1, "0-1": -1, "1/2-1/2": 0}[game["result"]]
        eq(original["outcome_stm"], white_result if board.turn else -white_result, "actual result orientation")
        check(not board.is_check() and board.outcome(claim_draw=True) is None, "eligible nonterminal training root")
        eq(record["original_teacher_cp_stm"], original["teacher_cp_stm"], "original CP provenance")
        eq(record["original_teacher_nodes"], original["teacher_nodes"], "original work provenance")
        stages = record["stages"]
        eq(len(stages), 3, "three saved ladder searches")
        stage_reasons = []
        for stage_index, stage in enumerate(stages):
            budget = [10000, 50000, 200000][stage_index]
            eq(stage["requested_nodes"], budget, "requested ladder budget")
            check(chess.Move.from_uci(stage["best_move"]) in board.legal_moves, "legal teacher best move")
            raw = raw_fields(stage["raw_score_event"])
            eq(raw["depth"], stage["completed_depth"], "same-event completed depth")
            eq(raw["nodes"], stage["reported_nodes"], "same-event nodes")
            eq(raw["pv"], stage["pv"], "same-event PV")
            if stage["pv"]:
                eq(stage["pv"][0], stage["best_move"], "PV starts with chosen move")
                pv_check(board, stage["pv"])
                pv_plies += len(stage["pv"])
            if raw["kind"] == "mate":
                eq(raw["value"], stage["mate_stm"], "mate score perspective")
                reason = "mate_score"
            else:
                eq(raw["value"], stage["cp_stm"], "CP score perspective")
                check(stage["mate_stm"] is None, "finite CP has no mate")
                reason = "bound_score" if raw["upperbound"] or raw["lowerbound"] else None
                if reason is None and stage["completed_depth"] < 1:
                    reason = "no_completed_depth"
                if reason is None and stage["reported_nodes"] < 1:
                    reason = "missing_nodes"
                if reason is None and not stage["pv"]:
                    reason = "missing_pv"
                if reason is None and abs(stage["cp_stm"]) >= 29904:
                    reason = "mate_range_cp"
                if reason is None and stage["reported_nodes"] < budget:
                    reason = "budget_not_exhausted"
            eq(stage["exclusion"], reason, "stage exclusion independently recomputed")
            if reason is None:
                label = stage["label"]
                eq(label["teacher_cp_stm"], stage["cp_stm"], "label CP")
                eq(label["teacher_nodes"], stage["reported_nodes"], "label nodes")
                eq(label["teacher_depth"], stage["completed_depth"], "label depth")
                eq(label["teacher_pv"], stage["pv"], "label PV")
            stage_reasons.append(reason)
        rejection = next((f"stage_{i + 1}_{reason}" for i, reason in enumerate(stage_reasons) if reason), None)
        previous, final = stages[-2:]
        if rejection is None:
            if final["requested_nodes"] <= original["teacher_nodes"]:
                rejection = "final_budget_not_greater_than_original"
            elif min(previous["completed_depth"], final["completed_depth"]) < 3:
                rejection = "insufficient_completed_depth"
            elif previous["best_move"] != final["best_move"]:
                rejection = "last_two_best_moves_differ"
            elif abs(previous["cp_stm"] - final["cp_stm"]) > 30:
                rejection = "last_two_cp_unstable"
        eq(record["exclusion"], rejection, "full label selection independently recomputed")
        eq(record["accepted"], rejection is None, "accepted flag independently recomputed")
        key = (original["game_id"], original["ply"])
        if rejection is not None:
            excluded[rejection] += 1
            check(key not in selected, "rejected row absent from training")
        else:
            accepted += 1
            check(key in selected, "accepted row exported")
            output = selected[key]
            for field, value in original.items():
                if not field.startswith("teacher_"):
                    eq(output[field], value, "nonteacher source provenance preserved " + field)
            for field, value in final["label"].items():
                eq(output[field], value, "exported highest-budget label " + field)
            expected_original = {key: value for key, value in original.items() if key.startswith("teacher_")}
            eq(output["teacher_quality"]["original_teacher"], expected_original, "original teacher retained")
            eq(output["teacher_quality"]["source_row_index"], index, "source row provenance")
    eq(accepted, len(selected), "accepted/exported count")
    eq(dict(excluded), report["exclusions"], "all exclusion totals independently recomputed")
    eq(report["accepted_train_positions"], accepted, "report acceptance total")
    for row in output_rows:
        key = neural_key(chess.Board(row["fen"]))
        check(key not in seen_feature_splits or seen_feature_splits[key] == row["split"], "no cross-split exact neural input")
        seen_feature_splits[key] = row["split"]
    eq(dict(collections.Counter(row["split"] for row in output_rows)), target["positions_by_split"], "output split counts")
    return {"requested_train_positions": len(records), "accepted": accepted,
            "excluded": dict(excluded), "queries": len(records) * 3, "legal_teacher_pv_plies": pv_plies,
            "heldout_validation": sum(row["split"] == "validation" for row in output_rows),
            "heldout_test": sum(row["split"] == "test" for row in output_rows),
            "limitation": "Agreement of finite own-engine searches is not ground truth or strength proof."}


def inspect_guard(directory, summary, expected_sha):
    path = directory / "resource-samples.jsonl"
    eq(sha(path), expected_sha, "resource raw samples hash")
    samples = rows(path)
    check(bool(samples), "nonempty native process samples")
    eq(summary["snapshot_count"], len(samples), "raw sample count")
    check(not summary["contaminated"] and summary["valid_for_comparison"], "resource guard clean")
    eq(summary["external_cpu_increase_ticks"], 0, "no reported external native CPU")
    check(not summary["observed_external_engine_cpu_increase"], "no external CPU flag")
    previous = None
    max_gap = max_duration = 0.0
    owned_identities = set()
    for sample in samples:
        check(not sample["contaminated"], "each sample clean")
        eq(sample["external_cpu_delta_ticks"], 0, "sample external CPU zero")
        check(0 <= sample["gap_seconds"] <= 2, "native sampling gap <=2 seconds")
        check(0 <= sample["snapshot_duration_seconds"] <= 2, "native snapshot <=2 seconds")
        max_gap = max(max_gap, sample["gap_seconds"])
        max_duration = max(max_duration, sample["snapshot_duration_seconds"])
        external = {}
        for process in sample["processes"]:
            check("error" not in process, "all recognized process CPU measurable")
            identity = (process["pid"], process["creation_ticks"])
            if process["owned"]:
                owned_identities.add(identity)
            else:
                external[identity] = process["cpu_ticks"]
                check(not process["exited_during_snapshot"], "external identity did not disappear mid-sample")
        if previous is not None:
            eq(set(external), set(previous), "external identity set unchanged")
            for identity, value in external.items():
                eq(value, previous[identity], "external native counters unchanged")
        previous = external
    eq(len(owned_identities), 2, "exactly two recognized owned engines")
    transported = {(row["pid"], row["creation_ticks"]) for row in summary["transport_engines"].values()}
    eq(transported, owned_identities, "UCI transports match observed owned engines")
    close(max_gap, summary["max_snapshot_gap_seconds"], "maximum gap independently recomputed")
    close(max_duration, summary["max_snapshot_duration_seconds"], "maximum snapshot duration independently recomputed")
    return {"samples": len(samples), "max_gap_seconds": max_gap, "max_snapshot_seconds": max_duration}


def parse_model(path):
    header, body = path.read_text(encoding="ascii").split("\n", 1)
    eq(header.split(), ["REGICIDE_NN_V2", "768", "32", "16", "600", "100"], "V2 raw model header")
    values = np.array([float(value) for value in body.split()], dtype=np.float64)
    eq(len(values), 25153, "25153 raw parameters")
    check(bool(np.isfinite(values).all()), "all parameters finite")
    parts, offset = {}, 0
    for name, shape in (("w1", (32, 768)), ("b1", (32,)), ("w2", (16, 32)),
                        ("b2", (16,)), ("w3", (16,)), ("b3", (1,))):
        count = math.prod(shape)
        parts[name] = values[offset:offset + count].reshape(shape)
        offset += count
    return parts


def diagnostic_sample(item, position, limit=None):
    board = replay(position.get("fen", chess.STARTING_FEN), position.get("moves", []), check_claim=True)
    eq(item["search_fen"], board.fen(en_passant="fen"), "diagnostic actual history root")
    check(chess.Move.from_uci(item["move"]) in board.legal_moves, "diagnostic legal best move")
    raw = raw_fields(item["raw_score_event"])
    eq(raw["depth"], item["depth"], "diagnostic same-event depth")
    eq(raw["nodes"], item["nodes"], "diagnostic same-event nodes")
    eq(raw["pv"], item["pv"], "diagnostic same-event PV")
    eq(item["pv"][0], item["move"], "diagnostic PV starts at bestmove")
    check(not raw["lowerbound"] and not raw["upperbound"], "diagnostic exact score")
    score = item["score"]
    eq(score["cp"] if raw["kind"] == "cp" else score["mate"], raw["value"], "diagnostic score perspective")
    check(raw["kind"] != "cp" or abs(raw["value"]) < 29904, "diagnostic finite CP outside mate band")
    check(item["depth"] >= 1 and item["nodes"] >= 1, "completed diagnostic work")
    check(math.isfinite(item["wall_time_seconds"]) and item["wall_time_seconds"] > 0, "finite measured wall time")
    check(math.isfinite(item["engine_time_seconds"]) and item["engine_time_seconds"] >= 0, "finite engine time")
    pv_check(board, item["pv"])
    if limit is not None:
        eq(item["requested_limit"], limit, "prespecified diagnostic search budget")
    return len(item["pv"])


def diagnostic_cost():
    directory = OUT / "diagnostic"
    data = doc(directory / "diagnostic.json")
    check(data["completed"] and data["error"] is None, "diagnostic completed cleanly")
    eq(data["binary"]["declared_source_commit"], SOURCE, "declared frozen source")
    eq(data["binary"]["sha256"], CYCLE["binary_sha256"], "same frozen native executable")
    eq(sha(Path(data["binary"]["path"])), data["binary"]["sha256"], "native binary digest")
    eq(sha(Path(data["model_input"]["path"])), data["model_input"]["sha256"], "original old V2 input unchanged")
    eq(data["model_input"]["sha256"], "1a7ec95c4062e7b448bf3582dc189a9c94c570708c98fce6882eb84ac0d717fe", "predeclared old V2 model")
    for item in data["models"].values():
        eq(sha(directory / item["snapshot"]), item["sha256"], "diagnostic model snapshot hash")
    learned = parse_model(directory / data["models"]["learned_v2"]["snapshot"])
    zero = parse_model(directory / data["models"]["zero_v2"]["snapshot"])
    for name in ("w1", "b1", "w2", "b2"):
        check(np.array_equal(learned[name], zero[name]), "all hidden parameters retained " + name)
    check(not np.any(zero["w3"]) and not np.any(zero["b3"]), "only zero-output head used")
    check(bool(np.any(learned["w3"])), "learned model genuinely nonzero")
    eq(data["configuration"], {"depth": 3, "nodes": 10000, "movetime_ms": 100,
        "repeats": 6, "warmups_per_role": 1, "seed": 20261019, "teacher_nodes": [100000, 200000]}, "prespecified diagnostic configuration")
    for role, options in data["arm_options"].items():
        actual = {key: value for key, value in options.items() if key != "EvalFile"}
        eq(actual, dict(OPTIONS, UseNeuralEvaluation=role != "hce"), "same native options except evaluator")
    for item in data["harness_sources"]:
        eq(sha(ROOT / item["path"]), item["sha256"], "diagnostic harness frozen")
    eq(sha(Path(data["fixture"]["path"])), data["fixture"]["sha256"], "diagnostic fixture frozen")
    for item in data["overlap_check"]["files"]:
        eq(sha(Path(item["path"])), item["sha256"], "protected overlap input frozen")
    positions = data["fixture"]["document"]["positions"]
    eq(len(positions), 12, "12 prespecified development positions")
    for position in positions:
        board = replay(position.get("fen", chess.STARTING_FEN), position.get("moves", []))
        features = np.zeros(768)
        features[list(neural_key(board))] = 1
        h1 = np.maximum(zero["w1"] @ features + zero["b1"], 0)
        h2 = np.maximum(zero["w2"] @ h1 + zero["b2"], 0)
        eq(float(h2 @ zero["w3"] + zero["b3"][0]), 0.0, "raw independent forward zero output")
        eq(100 * math.tanh(float(h2 @ zero["w3"] + zero["b3"][0])), 0.0, "zero CP correction")
    records = data["records"]
    eq(len(records), 216, "216 complete triples")
    eq(sum(role in item for item in records for role in ROLES), 648, "648 timed searches")
    fixed_equal, legal_pv_plies, aggregates = 0, 0, {}
    fields = ("move", "score", "depth", "seldepth", "nodes", "pv", "tt_counters")
    for mode in ("depth", "nodes", "time"):
        group = [row for row in records if row["mode"] == mode]
        eq(len(group), 72, "72 triples per mode")
        for index in range(12):
            selected = [row for row in group if row["position_index"] == index]
            eq(sorted(row["repetition"] for row in selected), [1, 2, 3, 4, 5, 6], "six repetitions per position/mode")
            eq({tuple(row["order"]) for row in selected}, set(itertools.permutations(ROLES)), "all six role orders balanced")
        for row in group:
            position = positions[row["position_index"]]
            eq(row["position"], position["name"], "position name/index")
            limit = {"depth": 3 if mode == "depth" else None,
                     "nodes": 10000 if mode == "nodes" else None,
                     "time": .1 if mode == "time" else None}
            for role in ROLES:
                legal_pv_plies += diagnostic_sample(row[role], position, limit)
                if mode == "depth":
                    eq(row[role]["depth"], 3, "requested fixed depth completed")
                if mode == "nodes":
                    check(row[role]["nodes"] >= 10000, "fixed node budget exhausted")
            if mode != "time":
                for field in fields:
                    eq(row["hce"][field], row["zero_v2"][field], "HCE/zero identical deterministic work " + field)
                eq(row["hce_zero_differences"], [], "no fixed-work differences")
                fixed_equal += 1
        stored = data["summary"]["modes"][mode]
        eq(stored["completed_triples"], len(group), "reported triple count")
        eq(stored["learned_hce_move_changes"], sum(row["hce"]["move"] != row["learned_v2"]["move"] for row in group), "move-change count independently recomputed")
        check(stored["timing_valid"], "clean work permits descriptive timing")
        results = {}
        for role in ("zero_v2", "learned_v2"):
            ratios = [row[role]["wall_time_seconds"] / row["hce"]["wall_time_seconds"] for row in group]
            value = statistics.median(ratios)
            close(value, stored[role]["median_paired_wall_ratio_over_hce"], "paired wall ratio independently recomputed")
            node_ratio = statistics.median(row[role]["nodes"] / row["hce"]["nodes"] for row in group)
            close(node_ratio, stored[role]["median_node_ratio_over_hce"], "node ratio independently recomputed")
            engine_ratios = [row[role]["engine_time_seconds"] / row["hce"]["engine_time_seconds"] for row in group
                             if row[role]["engine_time_seconds"] > 0 and row["hce"]["engine_time_seconds"] > 0]
            if engine_ratios:
                close(statistics.median(engine_ratios), stored[role]["median_paired_engine_ratio_over_hce"], "engine ratio independently recomputed")
            results[role] = {"median_paired_wall_ratio_over_hce": value, "median_node_ratio_over_hce": node_ratio}
        aggregates[mode] = results
    eq(fixed_equal, 144, "144 exact HCE/zero fixed-work pairs")
    guard = inspect_guard(directory, data["resource_guard"], data["resource_samples_sha256"])
    return {"triples": 216, "timed_searches": 648, "fixed_work_equal_pairs": fixed_equal,
            "legal_pv_plies": legal_pv_plies, "independent_aggregates": aggregates, "guard": guard,
            "limitation": "Zero/HCE fixed-work ratios isolate inference path cost; learned ratios include changed search graphs. No strength inference."}


def diagnostic_judgment():
    data = doc(OUT / "diagnostic/diagnostic.json")
    positions = data["fixture"]["document"]["positions"]
    eq(len(data["teacher"]), 12, "one deeper diagnosis per development position")
    stable, unstable, available, unavailable = [], [], 0, 0
    for record in data["teacher"]:
        index = record["position_index"]
        position = positions[index]
        eq(record["position"], position["name"], "teacher position identity")
        references = record["references"]
        eq(len(references), 2, "two deeper unrestricted references")
        for nodes, item in zip((100000, 200000), references):
            diagnostic_sample(item, position, {"depth": None, "nodes": nodes, "time": None})
            check(item["nodes"] >= nodes, "deeper reference consumed node budget")
        same = references[0]["move"] == references[1]["move"]
        eq(record["reference_bestmove_stable"], same, "reference move stability independently recomputed")
        cp_delta = (abs(references[0]["score"]["cp"] - references[1]["score"]["cp"])
                    if all(row["score"]["cp"] is not None for row in references) else None)
        eq(record["reference_cp_change"], cp_delta, "reference CP change independently recomputed")
        (stable if same and cp_delta is not None and cp_delta <= 30 else unstable).append(position["name"])
        common = min(row["depth"] for row in references)
        eq(record["forced_common_depth"], common, "common completed depth")
        actual_choices = {row[role]["move"] for row in data["records"] if row["position_index"] == index for role in ROLES}
        eq(set(record["compared_moves"]), actual_choices | {row["move"] for row in references}, "union of actual choices and teacher best moves")
        eq(set(record["forced"]), set(record["compared_moves"]), "every compared choice searched")
        for move, item in record["forced"].items():
            diagnostic_sample(item, position, {"depth": common, "nodes": 200000, "time": None})
            eq(item["move"], move, "forced root honored")
            eq(item["forced_root_move"], move, "forced root saved")
        ranks = record["ranks"]
        depth_complete = all(row["depth"] == common for row in record["forced"].values())
        eq(ranks["available"], depth_complete, "ranks only at same requested completed depth")
        if ranks["available"]:
            available += 1
            scores = {move: chess.engine.Mate(item["score"]["mate"]) if item["score"]["mate"] is not None
                      else chess.engine.Cp(item["score"]["cp"]) for move, item in record["forced"].items()}
            best = max(scores.values())
            for move, score in scores.items():
                eq(ranks["moves"][move]["rank_among_compared_moves"], 1 + sum(other > score for other in scores.values()), "move rank independently recomputed")
                regret = best.score() - score.score() if best.score() is not None and score.score() is not None else None
                eq(ranks["moves"][move]["regret_cp"], regret, "finite CP regret independently recomputed")
        else:
            unavailable += 1
    return {"available_common_depth_rank_positions": available, "unavailable": unavailable,
            "stable_move_and_cp_within_30": stable, "unstable_or_mate_references": unstable,
            "limitation": "Conditional finite own-engine decision proxy among compared moves. Even stable agreement is not objective correctness or strength proof; unstable references require extra caution."}


def arena_and_adoption():
    directory = RUN / "arena"
    arena = doc(directory / "arena.json")
    eq(arena["status"], "complete", "arena complete")
    eq(arena["errors"], [], "no arena errors")
    check(arena["completed"], "arena finished")
    eq(arena["requested_pairs"], 32, "32 prespecified independent draws")
    eq(arena["movetime_ms"], 100, "equal 100ms budget")
    eq(arena["hash_mb"], 16, "equal 16MiB hash")
    eq(arena["threads"], 1, "single threaded search")
    eq(arena["attempt_index"], 4, "arena alpha uses persistent attempt4")
    eq(arena["candidate_sha256"], CYCLE["candidate_sha256"], "candidate identity frozen before arena")
    eq(sha(directory / arena["candidate_snapshot"]), CYCLE["candidate_sha256"], "arena snapshot preserved")
    training = doc(RUN / "model/report.json")
    eq(sha(RUN / "model" / training["model_path"]), CYCLE["candidate_sha256"], "training candidate matches arena")
    eq(training["model_sha256"], CYCLE["candidate_sha256"], "training report identity")
    eq(training["dataset"]["raw_sha256"]["manifest"], sha(RUN / "quality/manifest.json"), "training actually used quality dataset")
    eq(arena["champion_sha256"], CYCLE["previous_model_sha256"], "previous champion identity")
    eq(arena["champion_sha256"], None, "no historical model silently activated")
    for role, item in arena["engines"].items():
        sent = {key: value for key, value in item["options"].items() if key != "EvalFile"}
        eq(sent, dict(OPTIONS, UseNeuralEvaluation=role == "candidate"), "equal arena options")
    check(training["finished_utc"] <= arena["started_utc"], "candidate training finished before arena")
    start_stage = next(item for item in CYCLE["stages"] if item["name"] == "previous-champion-arena")
    check(training["finished_utc"] <= start_stage["started_utc"] <= arena["started_utc"], "fresh arena seed drawn only after candidate fixed")
    fixture = doc(ROOT / "tests/data/learning_arena_openings_v1.json")
    eq(arena["opening_reserve"]["sha256"], sha(ROOT / "tests/data/learning_arena_openings_v1.json"), "reserved distribution unchanged")
    eq(arena["opening_reserve"]["seed"], CYCLE["arena_seed"], "recorded posttraining arena entropy")
    generator = random.Random(CYCLE["arena_seed"])
    for item in arena["opening_reserve"]["openings"]:
        seed = generator.choice(fixture["seeds"])
        eq(item["seed"], seed, "IID seed draws with replacement")
        rng = random.Random(seed)
        moves = list(rng.choice(fixture["prefixes"]))
        board = replay(chess.STARTING_FEN, moves)
        while len(moves) < 12:
            move = rng.choice(sorted(board.legal_moves, key=lambda value: value.uci()))
            board.push(move)
            moves.append(move.uci())
        eq(item["moves"], moves, "reserved opening independently regenerated")
        eq(item["fen_after_opening"], board.fen(en_passant="fen"), "reserved full-history board")
    games = arena["games"]
    eq(len(games), 64, "64 actual games")
    eq(sha(directory / "games.pgn"), arena["pgn_sha256"], "full PGN digest")
    pgn = io.StringIO((directory / "games.pgn").read_text(encoding="utf-8"))
    points, plies, pv_plies, max_response = [], 0, 0, 0
    for index, game in enumerate(games):
        opening = arena["opening_reserve"]["openings"][index // 2]
        eq(game["pair"], index // 2 + 1, "paired schedule")
        eq(game["game"], index + 1, "game number")
        white = "candidate" if index % 2 == 0 else "baseline"
        eq(game["white"], white, "both colors per opening")
        eq(game["black"], "baseline" if white == "candidate" else "candidate", "opposite engine black")
        eq(game["opening_uci_moves"], opening["moves"], "paired opening moves")
        eq(game["uci_moves"][:len(opening["moves"])], opening["moves"], "opening preserved in full game history")
        eq(game["initial_fen"], chess.STARTING_FEN, "standard initial game")
        check(game["completed"] and game["error"] is None, "fully completed legal game")
        eq(len(game["search_moves"]), len(game["uci_moves"]) - len(opening["moves"]), "per-move evidence complete")
        board = chess.Board()
        for ply, text in enumerate(game["uci_moves"]):
            check(board.outcome(claim_draw=True) is None, "no move after claimable rule ending")
            move = chess.Move.from_uci(text)
            check(move in board.legal_moves, "arena legal full PGN move")
            if ply >= len(opening["moves"]):
                evidence = game["search_moves"][ply - len(opening["moves"])]
                eq(evidence["move"], text, "arena evidence move")
                eq(evidence["fen_before"], board.fen(en_passant="fen"), "arena evidence full-history FEN")
                eq(evidence["role"], game["white"] if board.turn else game["black"], "arena evidence correct engine")
                elapsed = evidence["elapsed_seconds"]
                check(math.isfinite(elapsed) and 0 <= elapsed <= .2, "same fixed transport allowance")
                max_response = max(max_response, elapsed)
                if evidence["pv"]:
                    pv_check(board, evidence["pv"])
                    pv_plies += len(evidence["pv"])
            board.push(move)
            plies += 1
        outcome = board.outcome(claim_draw=True)
        check(outcome is not None, "actual final rules support result")
        eq(game["result"], outcome.result(), "actual outcome")
        eq(game["termination"], outcome.termination.name.lower(), "actual rule ending")
        eq(game["final_fen"], board.fen(en_passant="fen"), "final board")
        eq(game["plies"], len(game["uci_moves"]), "full ply count")
        score = .5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (white == "candidate"))
        eq(game["candidate_points"], score, "candidate score independently recomputed")
        points.append(score)
        parsed = chess.pgn.read_game(pgn)
        check(parsed is not None and not parsed.errors, "full PGN parses legally")
        eq([move.uci() for move in parsed.mainline_moves()], game["uci_moves"], "PGN agrees with all raw plies")
        eq(parsed.headers["Result"], outcome.result(), "PGN outcome")
    check(chess.pgn.read_game(pgn) is None, "no extra unregistered PGN game")
    guard = inspect_guard(directory, arena["resource_guard"], arena["resource_samples_sha256"])
    pair_scores = [(points[index] + points[index + 1]) / 2 for index in range(0, 64, 2)]
    mean = statistics.mean(pair_scores)
    alpha = .05 / (4 * 5)
    lower = max(0.0, mean - math.sqrt(math.log(1 / alpha) / (2 * len(pair_scores))))
    decision = arena["decision"]
    close(decision["candidate_pair_score"], mean, "pair score independently recomputed")
    close(decision["attempt_alpha"], alpha, "summable alpha4 independently recomputed")
    close(decision["one_sided_lower_bound"], lower, "one-sided Hoeffding lower bound independently recomputed")
    eq(decision["minimum_pairs"], 32, "unchanged minimum pairs")
    eq(decision["minimum_movetime_ms"], 100, "unchanged minimum time")
    eq(decision["eligible"], True, "clean complete minimum-sized arena eligible")
    passed = lower > .5
    eq(decision["passed"], passed, "strict independently computed criterion")
    eq(arena["promotion_passed"], passed, "arena pass flag")
    eq(CYCLE["adopted"], passed, "adoption matches unchanged criterion")
    if passed:
        eq(CYCLE["selected_model_sha256"], CYCLE["candidate_sha256"], "only passing candidate selected")
        champion = doc(STATE / "champion.json")
        eq(champion["model_sha256"], CYCLE["candidate_sha256"], "selected champion matches verified candidate")
    else:
        check(not (STATE / "champion.json").exists(), "no rejected candidate activated")
        eq(CYCLE["selected_model_sha256"], None, "classical incumbent retained")
    return {"wins": points.count(1), "draws": points.count(.5), "losses": points.count(0),
            "pair_score": mean, "attempt_alpha": alpha, "one_sided_lower_bound": lower,
            "strict_adopted": passed, "full_legal_game_plies": plies,
            "legal_reported_pv_plies": pv_plies, "max_response_seconds": max_response, "guard": guard}


def disjoint_inputs():
    data = doc(OUT / "diagnostic/diagnostic.json")
    inventory = []
    for reference in data["overlap_check"]["manifests"]:
        path = Path(reference["path"])
        manifest = doc(path)
        actual_rows = rows(path.parent / manifest["positions_path"])
        eq(reference["rows"], len(actual_rows), "protected manifest actual row count")
        inventory.extend(chess.Board(row["fen"]) for row in actual_rows)
    training = doc(ROOT / "tests/data/learning_openings_v1.json")
    for opening in training["openings"]:
        board = chess.Board(opening.get("initial_fen", chess.STARTING_FEN))
        inventory.append(board.copy())
        for move in opening["moves"]:
            board.push_uci(move)
            inventory.append(board.copy())
    reserved = doc(ROOT / "tests/data/learning_arena_openings_v1.json")
    for seed in reserved["seeds"]:
        rng = random.Random(seed)
        moves = list(rng.choice(reserved["prefixes"]))
        board = chess.Board()
        inventory.append(board.copy())
        for move in moves:
            board.push_uci(move)
            inventory.append(board.copy())
        while len(moves) < 12:
            move = rng.choice(sorted(board.legal_moves, key=lambda value: value.uci()))
            board.push(move)
            moves.append(move.uci())
            inventory.append(board.copy())
    protected_canonical = {position_key(board) for board in inventory}
    protected_neural = {neural_key(board) for board in inventory}
    for position in data["fixture"]["document"]["positions"]:
        board = replay(position.get("fen", chess.STARTING_FEN), position.get("moves", []))
        check(position_key(board) not in protected_canonical, "diagnostic disjoint canonical board")
        check(neural_key(board) not in protected_neural, "diagnostic disjoint exact NN input")
    for dataset in (RUN / "corpus", RUN / "quality"):
        manifest = doc(dataset / "manifest.json")
        for game in rows(dataset / manifest["games_path"]):
            if game["split"] != "train":
                continue
            check(game.get("source") != "arena" and not str(game.get("opening_id", "")).startswith("reserved-"), "no explicit arena provenance in learning")
            if game["initial_fen"] == chess.STARTING_FEN:
                check(not any(game["history"][:len(prefix)] == prefix for prefix in reserved["prefixes"]), "no reserved arena-prefix learning game")
    return {"protected_board_keys": len(protected_canonical), "protected_neural_inputs": len(protected_neural),
            "development_positions": 12, "collisions": 0,
            "limitation": "Checks exact supplied learning inputs and all declared arena seed continuations, not arbitrary future continuations."}


def main():
    global RUN, CYCLE
    runs = sorted((STATE / "runs").glob("000004-*"))
    if len(runs) != 1:
        raise ValueError("Expected exactly one persistent attempt4")
    RUN = runs[0]
    CYCLE = doc(RUN / "cycle.json")
    check(not doc(OUT / "power-state.json")["active"], "all measured work ended and sleep request restored before audit")
    check(CYCLE["status"] == "complete", "run audit only after cycle complete")
    check(doc(OUT / "diagnostic/diagnostic.json")["completed"], "run audit only after diagnostic complete")
    section("Frozen implementation, history, attempt and derived artifacts", source_and_history)
    section("Train-only teacher ladder, original provenance and immutable holdouts", quality_data)
    section("Zero-head control, exact work, balanced schedule and independent cost", diagnostic_cost)
    section("Conditional finite teacher ranks and regret", diagnostic_judgment)
    section("Full arena rules, resource evidence, independent promotion criterion", arena_and_adoption)
    section("Learning, development diagnostic and reserved-opening separation", disjoint_inputs)
    result = {"schema": "regicide.neural.cost-quality.independent-audit.v1",
              "finished_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "source_commit": SOURCE, "baseline_commit": BASE,
              "run": RUN.relative_to(STATE).as_posix(), "passed": all(row["passed"] for row in SECTIONS),
              "assertions": ASSERTIONS, "sections": SECTIONS,
              "independence": "No production acceptance, selection, statistics, replay, model-loader or promotion helpers imported. Raw replay uses python-chess; raw matrix parsing and zero control use NumPy. No engine search launched.",
              "scope": "Consistency, rule legality, immutable identities, finite label policy, sampled resource evidence and declared adoption criterion. Does not prove teacher truth, representative strength or all-host load control."}
    (OUT / "independent-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# 계산 비용·판단 분리와 교사 품질 독립 감사", "", f"통과: {result['passed']}. 확인 {ASSERTIONS:,}건.", "",
             "실험 종료 후 원자료를 읽고 별도 산술·체스 재생으로 검사했다. 엔진 검색과 채택 동작은 실행하지 않았다.", ""]
    for entry in SECTIONS:
        lines.append(f"- {entry['name']}: {'통과' if entry['passed'] else '실패'} ({entry['assertions']:,}건)")
        if not entry["passed"]:
            lines.append("  " + entry["error"])
    lines += ["", "자체 교사의 유한 검색 안정성과 비교 수의 순위는 제한적인 진단 근거이며 참정답·기력 향상을 입증하지 않는다.",
              "자원 감시는 알려진 프로세스의 표본 CPU 기록을 확인하며 모든 호스트 부하·온도를 배제하지 않는다.", ""]
    (OUT / "independent-audit.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "assertions": ASSERTIONS, "sections": len(SECTIONS)}), flush=True)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
