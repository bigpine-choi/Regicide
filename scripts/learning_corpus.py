"""Build an auditable accumulated corpus without changing immutable source runs.

The first dataset owns validation/test positions for the lifetime of a learning
store. Later datasets and loss diagnoses contribute training positions only.
New training inputs colliding with that fixed holdout are discarded, never
relabelled or moved. Chess replay and file hashes are checked before use.
"""

import argparse
from collections import Counter
import copy
import hashlib
import io
import json
from pathlib import Path

import chess
import chess.pgn

from neural_model import feature_key, material_cp_stm
from train_neural import (SCHEMA, SPLITS, json_lines, load_dataset, local_data_path,
                          position_key, replay, require_integer, sha256, write_json)


def _manifest_path(path):
    path = Path(path).resolve()
    return path / "manifest.json" if path.is_dir() else path


def _validate_sample(sample, game):
    """Validate a diagnosis row using the same conventions as the v1 trainer."""
    for field in ("game_id", "split", "family", "initial_fen"):
        if sample.get(field) != game.get(field):
            raise ValueError(f"diagnosis sample/game {field} mismatch")
    history = sample.get("history")
    if not isinstance(history, list) or history != game["history"][:len(history)]:
        raise ValueError("diagnosis history is not a prefix of its source game")
    if require_integer(sample.get("ply"), "ply") != len(history):
        raise ValueError("diagnosis ply disagrees with history")
    board = replay(sample["initial_fen"], history)
    if board.fen(en_passant="fen") != sample.get("fen"):
        raise ValueError("diagnosis FEN disagrees with replayed history")
    if (sample.get("turn") != ("w" if board.turn else "b")
            or board.is_game_over(claim_draw=True) or board.is_check()):
        raise ValueError("diagnosis must be a nonterminal position outside check")
    require_integer(sample.get("teacher_cp_stm"), "teacher_cp_stm")
    if require_integer(sample.get("material_cp_stm"), "material_cp_stm") != material_cp_stm(board):
        raise ValueError("diagnosis material label has the wrong convention")
    result_white = {"1-0": 1, "0-1": -1, "1/2-1/2": 0}[game["result"]]
    if require_integer(sample.get("outcome_stm"), "outcome_stm") != (result_white if board.turn else -result_white):
        raise ValueError("diagnosis outcome has the wrong side-to-move perspective")
    if require_integer(sample.get("teacher_depth"), "teacher_depth") < 1:
        raise ValueError("diagnosis teacher must complete a depth")
    if require_integer(sample.get("teacher_nodes"), "teacher_nodes") < 1:
        raise ValueError("diagnosis teacher nodes must be positive")
    pv = sample.get("teacher_pv")
    if not isinstance(pv, list) or not pv:
        raise ValueError("diagnosis teacher PV must be nonempty")
    replay(board.fen(en_passant="fen"), pv)
    if not game.get("completed") or result_white == 0 or sample["outcome_stm"] != -1:
        raise ValueError("loss diagnosis must label the losing player's completed decisive game")
    return board


def _verify_declared_hashes(manifest_path, manifest):
    declared = manifest.get("files_sha256", {})
    if not isinstance(declared, dict):
        raise ValueError("files_sha256 must be an object")
    for name, expected in declared.items():
        path = local_data_path(manifest_path, name)
        if not isinstance(expected, str) or sha256(path) != expected:
            raise ValueError(f"source file SHA256 mismatch: {name}")


def _verify_pgn(path, games):
    text = path.read_text(encoding="utf-8")
    stream = io.StringIO(text)
    for expected in games:
        parsed = chess.pgn.read_game(stream)
        if parsed is None or parsed.errors:
            raise ValueError("source PGN is missing a game or contains illegal moves")
        if parsed.board().fen(en_passant="fen") != expected["initial_fen"]:
            raise ValueError("source PGN initial position disagrees with games JSONL")
        if [move.uci() for move in parsed.mainline_moves()] != expected["history"]:
            raise ValueError("source PGN history disagrees with games JSONL")
        if parsed.headers.get("Result") != expected["result"]:
            raise ValueError("source PGN result disagrees with games JSONL")
    if chess.pgn.read_game(stream) is not None:
        raise ValueError("source PGN contains unregistered games")


def _source(path, *, allow_empty_splits=False):
    path = _manifest_path(path)
    manifest_sha = sha256(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("source manifest must be an object")
    _verify_declared_hashes(path, manifest)
    games_path = local_data_path(path, manifest.get("games_path"))
    positions_path = local_data_path(path, manifest.get("positions_path", manifest.get("samples_path")))
    files = [games_path, positions_path]
    if manifest.get("pgn_path") is not None:
        files.append(local_data_path(path, manifest["pgn_path"]))
    hashes = {file.relative_to(path.parent).as_posix(): sha256(file) for file in files}
    # This validates every original sample and game before filtering holdouts.
    _, metadata = load_dataset(path, allow_empty_splits=allow_empty_splits)
    games, positions = list(json_lines(games_path)), list(json_lines(positions_path))
    for game in games:
        board = replay(game["initial_fen"], game["history"])
        if not game["completed"] and game.get("result") != "*":
            raise ValueError("unfinished game must have result '*'")
        if game["completed"] and board.outcome(claim_draw=True).result() != game["result"]:
            raise ValueError("source game outcome disagrees with replay")
    if manifest.get("pgn_path") is not None:
        pgn_path = local_data_path(path, manifest["pgn_path"])
        _verify_pgn(pgn_path, games)
    _verify_declared_hashes(path, manifest)
    if sha256(path) != manifest_sha:
        raise ValueError("source manifest changed during validation")
    # Detect replacement even when the legacy source has no declared hashes.
    for filename, expected in hashes.items():
        if sha256(path.parent / filename) != expected:
            raise ValueError("source dataset changed during validation")
    return {"path": path, "sha256": manifest_sha, "manifest": manifest,
            "metadata": metadata, "games": games, "positions": positions,
            "files_sha256": hashes}


def _game_key(game):
    return (game["initial_fen"], tuple(game["history"]), game["result"],
            game["family"], game["split"], game["completed"])


def _write_jsonl(path, rows):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")


def build_corpus(dataset_paths, output, diagnosis_paths=(), fixed_holdout=True, *, position_exclusion=None):
    """Return a v1 manifest dict; output/manifest.json is the trainer entrypoint.

    Dataset order is chronological. Repeated identical manifests are idempotent.
    Later labels replace the same game/ply, and the newest exact training input
    is retained once. Diagnosis inputs must name a registered dataset hash.
    """
    if not fixed_holdout:
        raise ValueError("continual-learning corpora require fixed_holdout=True")
    paths = list(dataset_paths)
    if not paths:
        raise ValueError("at least one bootstrap dataset is required")
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    sources, source_by_hash, family_splits, stats = [], {}, {}, Counter()
    for path in paths:
        source = _source(path, allow_empty_splits=bool(sources))
        if source["sha256"] in source_by_hash:
            if source["files_sha256"] != source_by_hash[source["sha256"]]["files_sha256"]:
                raise ValueError("same manifest hash references different source files; immutable run identity is ambiguous")
            stats["duplicate_dataset_registrations"] += 1
            continue
        for family, split in source["metadata"]["family_splits"].items():
            if family in family_splits and family_splits[family] != split:
                raise ValueError("opening family split changed across source datasets")
            family_splits[family] = split
        sources.append(source)
        source_by_hash[source["sha256"]] = source
    games, game_keys, source_game_ids, samples = {}, {}, {}, {}
    for index, source in enumerate(sources):
        mapping = {}
        for game in source["games"]:
            if not game["completed"]:
                stats["incomplete_games_excluded"] += 1
                continue
            if index and game["split"] != "train":
                stats["later_holdout_games_ignored"] += 1
                continue
            key = _game_key(game)
            if key in game_keys:
                identifier = game_keys[key]
                stats["duplicate_games_coalesced"] += 1
            else:
                identifier = source["sha256"] + ":" + game["game_id"]
                game_keys[key] = identifier
                games[identifier] = {**copy.deepcopy(game), "game_id": identifier,
                                     "source_dataset_manifest_sha256": source["sha256"],
                                     "source_game_id": game["game_id"]}
            mapping[game["game_id"]] = identifier
        source_game_ids[source["sha256"]] = mapping
        for sample in source["positions"]:
            if sample["game_id"] not in mapping:
                stats["unselected_source_samples"] += 1
                continue
            identifier = mapping[sample["game_id"]]
            key = (identifier, sample["ply"])
            # The first source owns every held-out label permanently.
            if key in samples and sample["split"] != "train":
                continue
            if key in samples:
                stats["same_game_ply_labels_replaced"] += 1
                samples.pop(key)
            samples[key] = {**copy.deepcopy(sample), "game_id": identifier,
                            "source_dataset_manifest_sha256": source["sha256"],
                            "source_game_id": sample["game_id"]}
    diagnoses, seen_diagnoses = [], set()
    for diagnosis_path in diagnosis_paths:
        directory = Path(diagnosis_path).resolve()
        report_path = directory / "loss-report.json" if directory.is_dir() else directory
        report_sha = sha256(report_path)
        if report_sha in seen_diagnoses:
            stats["duplicate_diagnosis_registrations"] += 1
            continue
        seen_diagnoses.add(report_sha)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") not in ("complete", "completed"):
            raise ValueError("loss diagnosis has not completed")
        source_sha = report.get("source_dataset_manifest_sha256")
        if source_sha not in source_by_hash:
            raise ValueError("loss diagnosis references an unregistered source dataset hash")
        source = source_by_hash[source_sha]
        declared = report.get("source_files_sha256")
        required_files = (source["manifest"]["games_path"],
                          source["manifest"].get("positions_path", source["manifest"].get("samples_path")))
        if not isinstance(declared, dict) or any(
                declared.get(filename) != source["files_sha256"][Path(filename).as_posix()]
                for filename in required_files):
            raise ValueError("loss diagnosis source file hashes do not match registered source")
        positions_path = local_data_path(report_path, report.get("positions_path", "positions.jsonl"))
        positions_sha = sha256(positions_path)
        if report.get("positions_sha256") != positions_sha:
            raise ValueError("loss diagnosis positions SHA256 mismatch")
        original_games = {game["game_id"]: game for game in source["games"]}
        seen_positions = set()
        for sample in json_lines(positions_path):
            game = original_games.get(sample.get("game_id"))
            if game is None:
                raise ValueError("loss diagnosis refers to an unknown source game")
            if sample.get("split") != "train":
                raise ValueError("loss diagnosis may never change validation/test labels")
            _validate_sample(sample, game)
            analysis = sample.get("loss_analysis")
            if not isinstance(analysis, dict) or analysis.get("reliable") is not True:
                raise ValueError("unreliable loss diagnosis cannot become a training label")
            identifier = source_game_ids[source_sha][sample["game_id"]]
            key = (identifier, sample["ply"])
            if key in seen_positions:
                raise ValueError("duplicate game/ply in one loss diagnosis")
            seen_positions.add(key)
            if key in samples:
                stats["diagnosed_labels_replaced"] += 1
                samples.pop(key)
            else:
                stats["diagnosed_positions_added"] += 1
            samples[key] = {**copy.deepcopy(sample), "game_id": identifier,
                            "source_dataset_manifest_sha256": source_sha,
                            "source_game_id": sample["game_id"],
                            "source_diagnosis_report_sha256": report_sha}
        if sha256(report_path) != report_sha or sha256(positions_path) != positions_sha:
            raise ValueError("loss diagnosis changed during validation")
        diagnoses.append({"report_sha256": report_sha, "positions_sha256": positions_sha,
                          "source_dataset_manifest_sha256": source_sha,
                          "positions": len(seen_positions)})

    holdout_positions, holdout_features = set(), set()
    for sample in samples.values():
        if sample["split"] != "train":
            board = chess.Board(sample["fen"])
            holdout_positions.add(position_key(board))
            holdout_features.add(feature_key(board))
    selected_train, heldout = {}, []
    for sample in samples.values():
        if sample["split"] != "train":
            heldout.append(sample)
            continue
        board = chess.Board(sample["fen"])
        neural_key = feature_key(board)
        if position_key(board) in holdout_positions or neural_key in holdout_features:
            stats["train_holdout_collisions_excluded"] += 1
            continue
        if position_exclusion is not None:
            # Only training rows reach this optional guard. Restore the actual
            # legal history, so a caller never receives a synthetic bare FEN.
            reason = position_exclusion(replay(sample["initial_fen"], sample["history"]))
            if reason is not None:
                if not isinstance(reason, str) or not reason:
                    raise ValueError("training position exclusion must return a nonempty reason or None")
                stats["position_exclusion_" + reason] += 1
                continue
        if neural_key in selected_train:
            stats["duplicate_train_neural_inputs_replaced"] += 1
        selected_train[neural_key] = sample
    rows = list(selected_train.values()) + heldout
    rows.sort(key=lambda row: (SPLITS.index(row["split"]), row["game_id"], row["ply"]))
    ordered_games = sorted(games.values(), key=lambda game: game["game_id"])
    output.mkdir(parents=True)
    manifest = {"schema": SCHEMA, "status": "complete", "games_path": "games.jsonl",
                "positions_path": "positions.jsonl", "samples_path": "positions.jsonl",
                "pgn_path": "games.pgn", "corpus_schema": "regicide.learning.corpus.v1",
                "bootstrap_manifest_sha256": sources[0]["sha256"],
                "source_datasets": [{"manifest_sha256": source["sha256"],
                                     "files_sha256": source["files_sha256"],
                                     "counts": source["metadata"]["counts"]} for source in sources],
                "source_diagnoses": diagnoses, "family_splits": family_splits,
                "holdout_policy": "first dataset validation/test rows and labels fixed; later holdout games ignored; conflicting training inputs excluded",
                "training_policy": "chronological datasets then diagnoses; latest same game/ply label; latest exact neural training input once",
                "test_policy": "reporting only; never a model promotion or hyperparameter tuning signal",
                "exclusions": dict(stats), "positions": len(rows),
                "positions_by_split": dict(Counter(row["split"] for row in rows)),
                "holdout_rows_sha256": {split: hashlib.sha256(json.dumps(
                    [row for row in rows if row["split"] == split], sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
                    for split in ("validation", "test")},
                "completed_games": len(ordered_games), "errors": []}
    try:
        _write_jsonl(output / "games.jsonl", ordered_games)
        _write_jsonl(output / "positions.jsonl", rows)
        pgn_games = []
        for game in ordered_games:
            parsed = chess.pgn.Game.from_board(replay(game["initial_fen"], game["history"]))
            parsed.headers["Result"] = game["result"]
            parsed.headers["GameId"] = game["game_id"]
            parsed.headers["Family"] = game["family"]
            parsed.headers["Split"] = game["split"]
            pgn_games.append(str(parsed))
        (output / "games.pgn").write_text("\n\n".join(pgn_games) + "\n", encoding="utf-8", newline="\n")
        manifest["files_sha256"] = {name: sha256(output / name)
                                     for name in ("games.jsonl", "positions.jsonl", "games.pgn")}
        write_json(output / "manifest.json", manifest)
        _, metadata = load_dataset(output / "manifest.json")
        manifest["validated_counts"] = metadata["counts"]
        manifest["split_checks"] = {"family_split_conflicts": 0,
                                     "cross_split_canonical_positions": 0,
                                     "cross_split_exact_neural_inputs": 0,
                                     "validation_test_labels_from_bootstrap_only": True}
        write_json(output / "manifest.json", manifest)
        return manifest
    except Exception as error:
        manifest.update({"status": "failed", "error_type": type(error).__name__, "error": str(error)})
        write_json(output / "manifest.json", manifest)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--diagnosis", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = build_corpus(args.dataset, args.output, args.diagnosis)
    print(json.dumps({"status": manifest["status"], "positions_by_split": manifest["positions_by_split"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
