"""Define reliable labels ONCE for a newly collected endgame holdout.

Ordinary existing validation/test datasets, training data and previously refined
holdouts are refused. This is initial benchmark construction before its first
freeze, never feedback from a model or arena. Games and PGN bytes stay unchanged.
The existing own-engine three-stage teacher policy is reused without weakening.
"""

import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import shutil
import sys

import collect_endgame_data as ENDATA
import collect_learning_data as DATA
from learning_corpus import _source
import teacher_quality as QUALITY
from train_neural import local_data_path

SCHEMA = "regicide.endgame.initial-holdout-quality.v1"


def _fresh_source(dataset):
    source = _source(dataset, allow_empty_splits=True)
    manifest = source["manifest"]
    endgame = manifest.get("endgame", {})
    if (manifest.get("schema") != DATA.SCHEMA or manifest.get("status") != "complete" or
            endgame.get("schema") != "regicide.endgame.collection.v1" or
            endgame.get("selected_split") != "heldout" or
            manifest.get("endgame_holdout_refinement") is not None or
            manifest.get("teacher_quality_schema") is not None):
        raise ValueError("requires a NEW unrefined endgame collection with selected_split='heldout'")
    seeds, openings = ENDATA.load_seeds(ENDATA.DEFAULT_SEEDS, "heldout")
    if (endgame.get("seed_file_sha256") != DATA.sha256_file(ENDATA.DEFAULT_SEEDS) or
            endgame.get("seed_definition") != seeds):
        raise ValueError("new holdout seed definition disagrees with frozen authored seeds")
    allowed = {(item["family"], item["split"], item["initial_fen"]) for item in openings["openings"]}
    if not source["games"]:
        raise ValueError("new endgame holdout must contain games")
    games = {game["game_id"]: game for game in source["games"]}
    for game in games.values():
        if game["split"] not in ("validation", "test") or (
                game["family"], game["split"], game["initial_fen"]) not in allowed:
            raise ValueError("new holdout contains training, unknown or reassigned seed families")
        if str(game.get("opening_id", "")).startswith("reserved-") or game.get("source") == "arena":
            raise ValueError("arena evidence cannot become an endgame holdout")
    for row in source["positions"]:
        if row["split"] not in ("validation", "test") or not games[row["game_id"]]["completed"]:
            raise ValueError("new holdout samples require completed held-out source games")
        if any(key in row for key in ("teacher_quality", "initial_holdout_quality")):
            raise ValueError("previously refined holdout labels cannot be redefined")
        event = DATA.parse_score_event(row.get("teacher_score_raw_info", ""))
        if (event is None or event["kind"] != "cp" or event["upperbound"] or event["lowerbound"] or
                event["value"] != row["teacher_cp_stm"]):
            raise ValueError("original held-out label must have matching exact finite raw score")
    return source


def refine_holdout(dataset, binary, output, *, node_budgets=QUALITY.DEFAULT_BUDGETS,
                   max_cp_delta=30, min_depth=3):
    policy = QUALITY._configuration(node_budgets, max_cp_delta, min_depth)
    policy.update({"id": SCHEMA,
                   "holdout": "one-time initial definition of a NEW independently authored endgame holdout before first freeze; never modify bootstrap or a prior refined benchmark",
                   "all_stages": "existing teacher-quality exact finite/mate/bound/budget checks unchanged",
                   "training": "no train rows; labels and exclusions never used for model training or arena selection"})
    source = _fresh_source(dataset)
    binary = Path(binary).expanduser().resolve(strict=True)
    if not binary.is_file():
        raise ValueError("teacher binary must be a file")
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(source["path"].parent):
        raise ValueError("initial holdout refinement output must be outside its source directory")
    paths, frozen = QUALITY._freeze_paths(source, binary)
    for key, path in (("initial_holdout_wrapper", Path(__file__)),
                      ("endgame_collector", Path(ENDATA.__file__)),
                      ("endgame_seed_definition", ENDATA.DEFAULT_SEEDS)):
        paths[key] = path
        frozen[key] = DATA.sha256_file(path)
    if frozen["source_manifest"] != source["sha256"] or any(
            frozen["source/" + name] != digest for name, digest in source["files_sha256"].items()):
        raise ValueError("new holdout source changed between validation and freeze")
    output = DATA.reserve_output(output)
    records, accepted, exclusions = [], [], Counter()
    report = {"schema": SCHEMA, "status": "running", "started_utc": DATA.utc_now(),
              "policy": policy, "policy_sha256": QUALITY._digest(policy),
              "source_manifest_sha256": source["sha256"], "source_files_sha256": source["files_sha256"],
              "frozen_inputs_sha256": frozen, "queries_completed": 0,
              "requested_positions": len(source["positions"]), "records": records, "errors": [],
              "engine": {"sha256": frozen["binary"], "options": {**DATA.OPTIONS, "UseNeuralEvaluation": False}}}
    manifest = copy.deepcopy(source["manifest"])
    manifest.update({"status": "running", "files_sha256": {}, "errors": [],
                     "games_path": "games.jsonl", "positions_path": "positions.jsonl", "samples_path": "positions.jsonl",
                     "endgame_holdout_refinement": {"schema": SCHEMA, "source_manifest_sha256": source["sha256"],
                         "policy_sha256": report["policy_sha256"], "report_path": "holdout-quality-report.json",
                         "scope": "initial benchmark definition only; freeze derived labels before first model evaluation"}})

    def checkpoint():
        report.update({"checkpoint_utc": DATA.utc_now(), "accepted_positions": len(accepted),
                       "exclusions": dict(exclusions)})
        payload = dict(report)
        if report["status"] == "running":
            payload.pop("records")
            payload["positions_started"] = len(records)
            if records:
                payload["active_record"] = records[-1]
        DATA.checkpoint_json(output / "holdout-quality-report.json", payload)
        DATA.checkpoint_json(output / "manifest.json", manifest)

    checkpoint()
    try:
        with DATA.LearningEngine.popen_uci([str(binary), "uci"], timeout=600) as teacher:
            required = set(DATA.OPTIONS) | {"UseNeuralEvaluation", "Clear Hash"}
            if not teacher.id.get("name", "").startswith("Regicide ") or not required.issubset(teacher.options):
                raise ValueError("initial holdout quality requires own Regicide and all classical/fresh-hash options")
            teacher.configure(DATA.OPTIONS)
            teacher.configure({"UseNeuralEvaluation": False})
            report["engine"]["uci_id"] = dict(teacher.id)
            QUALITY._check_freeze(paths, frozen)
            for index, row in enumerate(source["positions"]):
                board = DATA.replay_position(row["initial_fen"], row["history"])
                if board.fen(en_passant="fen") != row["fen"] or DATA.sample_exclusion(board) is not None:
                    raise ValueError("new held-out sample differs from eligible replayed position")
                record = {"source_row_index": index, "game_id": row["game_id"], "split": row["split"],
                          "initial_fen": row["initial_fen"], "history": row["history"], "fen": row["fen"],
                          "original_teacher": {key: copy.deepcopy(value) for key, value in row.items() if key.startswith("teacher_")},
                          "stages": []}
                records.append(record)
                for budget in policy["node_budgets"]:
                    report["active_query"] = {"source_row_index": index, "requested_nodes": budget}
                    checkpoint()
                    record["stages"].append(QUALITY.search_teacher(teacher, board, budget))
                    report["queries_completed"] += 1
                    checkpoint()
                label, reason = QUALITY.select_label(record["stages"], row["teacher_nodes"], policy)
                record.update({"accepted": reason is None, "exclusion": reason})
                if reason is None:
                    updated = copy.deepcopy(row)
                    updated.update(label)
                    updated["initial_holdout_quality"] = {"schema": SCHEMA,
                        "source_manifest_sha256": source["sha256"], "source_row_index": index,
                        "policy_sha256": report["policy_sha256"], "original_teacher": record["original_teacher"],
                        "reliable_under_finite_policy": True, "not_ground_truth": True}
                    accepted.append(updated)
                else:
                    exclusions[reason] += 1
                report.pop("active_query", None)
                checkpoint()
                print(f"initial endgame holdout {len(records)}/{len(source['positions'])}: {reason or 'accepted'}", flush=True)
        QUALITY._check_freeze(paths, frozen)
        shutil.copyfile(local_data_path(source["path"], source["manifest"]["games_path"]), output / "games.jsonl")
        if source["manifest"].get("pgn_path") is not None:
            shutil.copyfile(local_data_path(source["path"], source["manifest"]["pgn_path"]), output / "games.pgn")
            manifest["pgn_path"] = "games.pgn"
        DATA.write_jsonl(output / "positions.jsonl", accepted)
        names = ["games.jsonl", "positions.jsonl"] + (["games.pgn"] if "pgn_path" in manifest else [])
        hashes = {name: DATA.sha256_file(output / name) for name in names}
        if hashes["games.jsonl"] != source["files_sha256"][source["manifest"]["games_path"]]:
            raise ValueError("derived games bytes changed")
        if "pgn_path" in manifest and hashes["games.pgn"] != source["files_sha256"][source["manifest"]["pgn_path"]]:
            raise ValueError("derived PGN bytes changed")
        manifest.update({"status": "complete", "positions": len(accepted), "positions_by_split": dict(Counter(row["split"] for row in accepted)),
                         "positions_by_turn": dict(Counter(row["turn"] for row in accepted)), "files_sha256": hashes,
                         "quality_counts": {"requested": len(source["positions"]), "accepted": len(accepted), "excluded": sum(exclusions.values())},
                         "counts_by_turn": {split: {turn: sum(row["split"] == split and row["turn"] == turn for row in accepted)
                                                     for turn in ("w", "b")} for split in DATA.SPLITS},
                         "holdout_rows_sha256": {split: QUALITY._digest([row for row in accepted if row["split"] == split])
                                                  for split in ("validation", "test")}})
        report.update({"status": "complete", "finished_utc": DATA.utc_now(), "output_files_sha256": hashes,
                       "games_and_pgn_bytes_preserved": True, "freeze_rechecked": True})
        checkpoint()
        _source(output / "manifest.json", allow_empty_splits=True)
        QUALITY._check_freeze(paths, frozen)
        return manifest
    except BaseException as exc:
        report.update({"status": "failed", "finished_utc": DATA.utc_now()})
        report["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        manifest.update({"status": "failed", "errors": report["errors"]})
        checkpoint()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset", "binary", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--node-budgets", type=int, nargs=3, default=QUALITY.DEFAULT_BUDGETS)
    parser.add_argument("--max-cp-delta", type=int, default=30)
    parser.add_argument("--min-depth", type=int, default=3)
    try:
        result = refine_holdout(**vars(parser.parse_args(argv)))
        print(json.dumps(result["quality_counts"]))
        return 0
    except Exception as exc:
        print(f"initial endgame holdout quality failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
