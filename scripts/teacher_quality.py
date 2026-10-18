"""Re-search training labels with a frozen, conservative classical-teacher ladder.

Only actual train positions are searched. Existing validation/test JSONL lines,
game histories and PGN bytes are copied unchanged. Stability of a bounded own
engine is useful evidence, not a proof of the correct chess move or score.
This tool neither trains a model nor changes any arena/promotion criterion.
"""

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys

import chess
import chess.engine

import collect_learning_data as DATA
from classical_evaluation import classical_cp_stm
from learning_corpus import _source
from train_neural import load_dataset, local_data_path, require_integer


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "regicide.learning.teacher-quality.v1"
POLICY = "fresh-classical-last-two-stable-v1"
DEFAULT_BUDGETS = (10000, 50000, 200000)
RESERVED_OPENINGS = ROOT / "tests/data/learning_arena_openings_v1.json"
# Matches the native protocol's non-mate range: 30000 - (64 + 32).
MATE_CP_THRESHOLD = 29904


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _configuration(node_budgets, max_cp_delta, min_depth):
    if not isinstance(node_budgets, (tuple, list)) or len(node_budgets) != 3:
        raise ValueError("teacher quality requires exactly three increasing node budgets")
    budgets = tuple(require_integer(value, "node budget") for value in node_budgets)
    if any(value < 1000 for value in budgets) or not budgets[0] < budgets[1] < budgets[2]:
        raise ValueError("node budgets must be increasing integers >= 1000")
    if require_integer(max_cp_delta, "max_cp_delta") < 0:
        raise ValueError("max_cp_delta must be nonnegative")
    if require_integer(min_depth, "min_depth") < 1:
        raise ValueError("min_depth must be positive")
    return {"id": POLICY, "node_budgets": list(budgets),
            "max_last_two_cp_delta": max_cp_delta, "minimum_completed_depth": min_depth,
            "selection": "both final searches exact finite cp, completed depth minimum, same legal best move, absolute cp difference within threshold; use final label only",
            "all_stages": "mate, bound, missing score/depth/nodes/PV, non-exhausted budget or mate-range cp at any stage excludes the training label",
            "stronger_than_original": "final requested node budget must exceed original teacher reported nodes",
            "context": "new logical UCI game and Clear Hash for every position and budget; full original history; no time limit",
            "teacher": "own Regicide; complete classical HCE; neural off; positional, pawn shield, pawn structure, quiescence and existing search options enabled",
            "holdout": "never search, relabel, filter or reserialize validation/test rows",
            "uncertainty": "finite-search agreement is not ground truth, optimal-play proof or evidence of playing strength"}


def _holdout(rows, raw_lines):
    return {split: {"rows": sum(row["split"] == split for row in rows),
                    "raw_lines_sha256": hashlib.sha256(b"".join(
                        line for row, line in zip(rows, raw_lines) if row["split"] == split)).hexdigest(),
                    "canonical_rows_sha256": _digest([row for row in rows if row["split"] == split])}
            for split in ("validation", "test")}


def _source_state(dataset):
    source = _source(dataset)
    manifest = source["manifest"]
    if manifest.get("schema") != DATA.SCHEMA or manifest.get("status") != "complete":
        raise ValueError("teacher quality requires a complete v1 learning dataset")
    positions_path = local_data_path(source["path"], manifest.get("positions_path", manifest.get("samples_path")))
    raw_lines = positions_path.read_bytes().splitlines(keepends=True)
    if len(raw_lines) != len(source["positions"]):
        raise ValueError("source JSONL byte rows disagree with validated rows")
    games = {game["game_id"]: game for game in source["games"]}
    for row, line in zip(source["positions"], raw_lines):
        # The general trainer may skip incomplete-game samples. This tool must
        # never promote one of those unchecked rows into a new teacher label.
        if not games[row["game_id"]]["completed"]:
            raise ValueError("teacher quality samples require completed source games")
        if json.loads(line.decode("utf-8")) != row:
            raise ValueError("source positions changed while reading exact JSONL bytes")
        raw_info = row.get("teacher_score_raw_info")
        if raw_info is not None:
            if not isinstance(raw_info, str):
                raise ValueError("source teacher raw score must be a string")
            event = DATA.parse_score_event(raw_info)
            if (event is None or event["kind"] != "cp" or event["upperbound"] or
                    event["lowerbound"] or event["value"] != row["teacher_cp_stm"]):
                raise ValueError("source label is not a matching exact finite raw cp score")
    holdout = _holdout(source["positions"], raw_lines)
    declared = manifest.get("holdout_rows_sha256", {})
    if not isinstance(declared, dict):
        raise ValueError("source holdout_rows_sha256 must be an object")
    for split, details in holdout.items():
        if split in declared and declared[split] != details["canonical_rows_sha256"]:
            raise ValueError("source canonical holdout hash disagrees with rows")
    source["raw_lines"], source["holdout"] = raw_lines, holdout
    return source


def _reserved_guard(source):
    raw = RESERVED_OPENINGS.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    if document.get("schema") != "regicide.learning.arena.openings.v1":
        raise ValueError("reserved arena fixture has an unexpected schema")
    prefixes = document.get("prefixes")
    if not isinstance(prefixes, list) or not prefixes:
        raise ValueError("reserved arena prefixes are missing")
    for prefix in prefixes:
        if not isinstance(prefix, list) or not prefix:
            raise ValueError("reserved arena prefix is malformed")
        DATA.replay_position(chess.STARTING_FEN, prefix)
    for game in source["games"]:
        if game["split"] != "train":
            continue
        if game["initial_fen"] == chess.STARTING_FEN and any(
                game["history"][:len(prefix)] == prefix for prefix in prefixes):
            raise ValueError("training game starts with a reserved arena prefix")
        if str(game.get("opening_id", "")).startswith("reserved-") or game.get("source") == "arena":
            raise ValueError("reserved arena evidence cannot become training data")
    return {"fixture_sha256": hashlib.sha256(raw).hexdigest(),
            "scope": "reject train game histories starting from reserved prefixes and explicit reserved/arena provenance; caller must never disguise arena evidence as learning data"}


def _freeze_paths(source, binary):
    paths = {"binary": binary, "source_manifest": source["path"],
             "reserved_openings": RESERVED_OPENINGS}
    for name in source["files_sha256"]:
        paths["source/" + name] = source["path"].parent / name
    for name in ("teacher_quality.py", "collect_learning_data.py", "learning_corpus.py",
                 "train_neural.py", "neural_model.py", "classical_evaluation.py"):
        paths["scripts/" + name] = ROOT / "scripts" / name
    return paths, {name: DATA.sha256_file(path) for name, path in paths.items()}


def _check_freeze(paths, hashes):
    for name, path in paths.items():
        if DATA.sha256_file(path) != hashes[name]:
            raise ValueError("teacher quality frozen input changed: " + name)


def search_teacher(engine, board, nodes):
    """One fresh full-history search; do not reuse a previous ladder stage's TT."""
    engine.configure({"Clear Hash": None})
    result = engine.play(board, chess.engine.Limit(nodes=nodes), game=object(),
                         info=chess.engine.INFO_ALL)
    event = copy.deepcopy(engine.protocol.last_score_event)
    if result.move is None or result.move not in board.legal_moves:
        raise ValueError("teacher did not return a legal best move")
    pv = result.info.get("pv", [])
    if not isinstance(pv, list):
        raise ValueError("teacher PV must be a list")
    if pv and pv[0] != result.move:
        raise ValueError("teacher bestmove disagrees with its PV")
    probe = board.copy(stack=True)
    for move in pv:
        if probe.outcome(claim_draw=False) is not None or move not in probe.legal_moves:
            raise ValueError("teacher PV contains an illegal or postterminal move")
        probe.push(move)
    label, exclusion = DATA.exact_cp_info(board, result.info, event)
    score = result.info.get("score")
    cp = score.pov(board.turn).score() if score is not None else None
    mate = score.pov(board.turn).mate() if score is not None else None
    if label is not None:
        depth = require_integer(label["teacher_depth"], "completed teacher depth")
        actual_nodes = require_integer(label["teacher_nodes"], "reported teacher nodes")
        if abs(cp) >= MATE_CP_THRESHOLD:
            exclusion = "mate_range_cp"
        elif actual_nodes < nodes:
            exclusion = "budget_not_exhausted"
        # The raw score is captured per event; require the same event's search
        # fields, so python-chess's accumulated info cannot supply stale data.
        tokens = event["raw_info"].split()
        try:
            if (int(tokens[tokens.index("depth") + 1]) != depth or
                    int(tokens[tokens.index("nodes") + 1]) != actual_nodes or
                    tokens[tokens.index("pv") + 1:] != label["teacher_pv"]):
                raise ValueError("raw and parsed teacher depth/nodes/PV disagree")
        except (IndexError, ValueError) as error:
            raise ValueError("raw teacher event lacks matching depth/nodes/PV") from error
    return {"requested_nodes": nodes, "best_move": result.move.uci(), "cp_stm": cp,
            "mate_stm": mate, "completed_depth": result.info.get("depth"),
            "reported_nodes": result.info.get("nodes"), "pv": [move.uci() for move in pv],
            "raw_score_event": event, "label": label, "exclusion": exclusion}


def select_label(stages, original_nodes, policy):
    for index, stage in enumerate(stages):
        if stage["exclusion"] is not None or stage["label"] is None:
            return None, "stage_" + str(index + 1) + "_" + (stage["exclusion"] or "missing_label")
    previous, final = stages[-2:]
    if final["requested_nodes"] <= original_nodes:
        return None, "final_budget_not_greater_than_original"
    if min(previous["completed_depth"], final["completed_depth"]) < policy["minimum_completed_depth"]:
        return None, "insufficient_completed_depth"
    if previous["best_move"] != final["best_move"]:
        return None, "last_two_best_moves_differ"
    if abs(previous["cp_stm"] - final["cp_stm"]) > policy["max_last_two_cp_delta"]:
        return None, "last_two_cp_unstable"
    return copy.deepcopy(final["label"]), None


def refine_dataset(dataset, binary, output, *, node_budgets=DEFAULT_BUDGETS,
                   max_cp_delta=30, min_depth=3):
    """Return a separate v1 dataset manifest; raw diagnosis is quality-report.json.

    A complete report with trainable=false is valid diagnostic evidence only.
    The cycle/trainer must refuse to train or adopt with an empty train split.
    """
    policy = _configuration(node_budgets, max_cp_delta, min_depth)
    source = _source_state(dataset)
    reserved = _reserved_guard(source)
    binary = Path(binary).expanduser().resolve(strict=True)
    if not binary.is_file():
        raise ValueError("teacher binary must be a file")
    frozen_paths, frozen = _freeze_paths(source, binary)
    if (frozen["source_manifest"] != source["sha256"] or
            frozen["reserved_openings"] != reserved["fixture_sha256"] or any(
                frozen["source/" + name] != digest for name, digest in source["files_sha256"].items())):
        raise ValueError("teacher quality source changed between validation and freeze")
    output = DATA.reserve_output(Path(output).expanduser().resolve())
    records, exclusions, accepted = [], Counter(), {}
    report = {"schema": SCHEMA, "status": "running", "started_utc": DATA.utc_now(),
              "policy": policy, "policy_sha256": _digest(policy),
              "source_manifest_sha256": source["sha256"],
              "source_files_sha256": source["files_sha256"], "source_holdout": source["holdout"],
              "frozen_inputs_sha256": frozen, "reserved_arena_guard": reserved,
              "engine": {"binary_name": binary.name, "sha256": frozen["binary"],
                         "configured_options": {**DATA.OPTIONS, "UseNeuralEvaluation": False}},
              "runtime": {"python": sys.version, "python_chess": chess.__version__,
                          "platform": platform.platform(), "checkout": DATA.checkout_provenance()},
              "requested_train_positions": sum(row["split"] == "train" for row in source["positions"]),
              "queries_completed": 0, "records": records, "errors": []}
    manifest = {"schema": DATA.SCHEMA, "status": "running", "games_path": "games.jsonl",
                "positions_path": "positions.jsonl", "samples_path": "positions.jsonl",
                "teacher_quality_schema": SCHEMA, "source_manifest_sha256": source["sha256"],
                "source_files_sha256": source["files_sha256"],
                "quality_report_path": "quality-report.json", "policy_sha256": _digest(policy),
                "holdout_policy": "input validation/test JSONL lines and every game/PGN byte preserved unchanged; no held-out teacher queries",
                "test_policy": "reporting only; never a training, selection or promotion signal",
                "family_splits": source["metadata"]["family_splits"], "errors": []}

    def checkpoint():
        report.update({"checkpoint_utc": DATA.utc_now(), "accepted_train_positions": len(accepted),
                       "exclusions": dict(exclusions)})
        payload = dict(report)
        if report["status"] == "running":
            # Avoid quadratic disk traffic while collecting a large ladder.
            # Full raw records are written once on completion or failure.
            payload.pop("records")
            payload["positions_started"] = len(records)
            if records:
                payload["active_record"] = records[-1]
        DATA.checkpoint_json(output / "quality-report.json", payload)

    checkpoint()
    DATA.checkpoint_json(output / "manifest.json", manifest)
    try:
        with DATA.LearningEngine.popen_uci([str(binary), "uci"], timeout=600) as teacher:
            if not teacher.id.get("name", "").startswith("Regicide "):
                raise ValueError("teacher quality requires the own Regicide engine")
            required = set(DATA.OPTIONS) | {"UseNeuralEvaluation", "Clear Hash"}
            if not required.issubset(teacher.options):
                raise ValueError("teacher omitted required classical/fresh-hash options")
            teacher.configure(DATA.OPTIONS)
            teacher.configure({"UseNeuralEvaluation": False})
            report["engine"]["uci_id"] = dict(teacher.id)
            _check_freeze(frozen_paths, frozen)
            for index, row in enumerate(source["positions"]):
                if row["split"] != "train":
                    continue
                board = DATA.replay_position(row["initial_fen"], row["history"])
                if board.fen(en_passant="fen") != row["fen"] or DATA.sample_exclusion(board) is not None:
                    raise ValueError("teacher root differs from an eligible actual source position")
                static_cp = classical_cp_stm(board)
                record = {"source_row_index": index, "game_id": row["game_id"], "ply": row["ply"],
                          "turn": row["turn"], "fen": row["fen"], "initial_fen": row["initial_fen"],
                          "history": row["history"], "original_teacher_cp_stm": row["teacher_cp_stm"],
                          "original_teacher_nodes": row["teacher_nodes"], "hce_cp_stm": static_cp,
                          "hce_is_search_label": False, "stages": []}
                records.append(record)
                for budget in policy["node_budgets"]:
                    report["active_query"] = {"source_row_index": index, "requested_nodes": budget}
                    checkpoint()
                    stage = search_teacher(teacher, board, budget)
                    stage["teacher_minus_hce_cp"] = (stage["cp_stm"] - static_cp
                                                       if stage["cp_stm"] is not None else None)
                    record["stages"].append(stage)
                    report["queries_completed"] += 1
                    checkpoint()
                label, reason = select_label(record["stages"], row["teacher_nodes"], policy)
                record.update({"accepted": reason is None, "exclusion": reason,
                               "last_two_same_best_move": record["stages"][-2]["best_move"] == record["stages"][-1]["best_move"],
                               "last_two_cp_delta": (abs(record["stages"][-2]["cp_stm"] - record["stages"][-1]["cp_stm"])
                                   if all(stage["cp_stm"] is not None for stage in record["stages"][-2:]) else None)})
                if reason is not None:
                    exclusions[reason] += 1
                else:
                    updated = copy.deepcopy(row)
                    original = {key: copy.deepcopy(value) for key, value in row.items() if key.startswith("teacher_")}
                    updated.update(label)
                    updated["teacher_quality"] = {"schema": SCHEMA, "policy_sha256": _digest(policy),
                        "source_manifest_sha256": source["sha256"], "source_row_index": index,
                        "report_record_index": len(records) - 1, "original_teacher": original,
                        "reliable_under_finite_policy": True, "not_ground_truth": True}
                    accepted[index] = updated
                report.pop("active_query", None)
                checkpoint()
                print(f"teacher quality {len(records)}/{report['requested_train_positions']}: {reason or 'accepted'}", flush=True)

        _check_freeze(frozen_paths, frozen)
        # Copy games verbatim, including holdout histories, rather than regenerating PGN.
        shutil.copyfile(local_data_path(source["path"], source["manifest"]["games_path"]), output / "games.jsonl")
        if source["manifest"].get("pgn_path") is not None:
            shutil.copyfile(local_data_path(source["path"], source["manifest"]["pgn_path"]), output / "games.pgn")
            manifest["pgn_path"] = "games.pgn"
        selected_rows, selected_lines = [], []
        with (output / "positions.jsonl").open("xb") as handle:
            for index, (row, original_line) in enumerate(zip(source["positions"], source["raw_lines"])):
                if row["split"] != "train":
                    selected, line = row, original_line
                elif index in accepted:
                    selected = accepted[index]
                    line = (json.dumps(selected, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                else:
                    continue
                # A preserved final holdout line may lack its newline. It can
                # only remain last; otherwise preserving it and valid JSONL
                # simultaneously is impossible, so refuse rather than alter it.
                if selected_lines and not selected_lines[-1].endswith(b"\n"):
                    raise ValueError("non-final source JSONL line lacks a newline")
                handle.write(line)
                selected_rows.append(selected)
                selected_lines.append(line)
        holdout = _holdout(selected_rows, selected_lines)
        if holdout != source["holdout"]:
            raise ValueError("derived holdout labels or exact JSONL bytes changed")
        names = ["games.jsonl", "positions.jsonl"] + (["games.pgn"] if "pgn_path" in manifest else [])
        report.update({"status": "complete", "finished_utc": DATA.utc_now(),
                       "trainable": bool(accepted), "output_holdout": holdout,
                       "freeze_rechecked": True, "output_files_sha256": {
                           name: DATA.sha256_file(output / name) for name in names}})
        checkpoint()
        manifest.update({"status": "complete", "trainable": bool(accepted),
                         "positions": len(selected_rows), "completed_games": sum(game["completed"] for game in source["games"]),
                         "positions_by_split": dict(Counter(row["split"] for row in selected_rows)),
                         "holdout_rows_sha256": {split: details["canonical_rows_sha256"] for split, details in holdout.items()},
                         "holdout_raw_lines_sha256": {split: details["raw_lines_sha256"] for split, details in holdout.items()},
                         "files_sha256": report["output_files_sha256"],
                         "quality_report_sha256": DATA.sha256_file(output / "quality-report.json"),
                         "quality_counts": {"requested": report["requested_train_positions"],
                                            "accepted": len(accepted), "excluded": sum(exclusions.values())}})
        DATA.checkpoint_json(output / "manifest.json", manifest)
        _, metadata = load_dataset(output / "manifest.json", allow_empty_splits=True)
        manifest["validated_counts"] = metadata["counts"]
        DATA.checkpoint_json(output / "manifest.json", manifest)
        _source(output / "manifest.json", allow_empty_splits=True)
        _check_freeze(frozen_paths, frozen)
        return manifest
    except BaseException as error:
        report.update(status="failed", finished_utc=DATA.utc_now())
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
        checkpoint()
        manifest.update(status="failed", errors=report["errors"])
        DATA.checkpoint_json(output / "manifest.json", manifest)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory only")
    parser.add_argument("--node-budgets", type=int, nargs=3, default=DEFAULT_BUDGETS)
    parser.add_argument("--max-cp-delta", type=int, default=30)
    parser.add_argument("--min-depth", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        manifest = refine_dataset(args.dataset, args.binary, args.output,
                                  node_budgets=args.node_budgets,
                                  max_cp_delta=args.max_cp_delta, min_depth=args.min_depth)
        print(json.dumps({"status": manifest["status"], "trainable": manifest["trainable"],
                          "quality_counts": manifest["quality_counts"]}))
        return 0 if manifest["trainable"] else 2
    except Exception as error:
        print(f"teacher quality failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
