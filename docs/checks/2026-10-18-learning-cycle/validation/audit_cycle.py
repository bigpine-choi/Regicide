"""Independently audit a finished learning cycle without running any engine.

Uses python-chess rules, direct JSON/PGN replay, hashlib and elementary math.
Does not import the collector, trainer, corpus builder, arena or registry.
Run ONLY after the guarded arena has finished.
"""
import argparse
from collections import Counter
import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import random
import re
import sys

import chess
import chess.pgn

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "scripts/learning_cycle.py").is_file())
BOOTSTRAP_SHA = {
    "manifest.json": "9cdf5ad763321e91b390eb23e51147e1e4b98f28f4d9436a5467aa44fd63c8af",
    "games.jsonl": "9ec6cc4d75aa2a1322974543d9df66a9c7299895134cbd64152e4d2d4ffa1cc5",
    "positions.jsonl": "260005178e8e6189f21f76000d649984d9decc3c34c724aa5c0f751291179e55",
    "games.pgn": "10e432cc7551f93432ed9805c67fb6b039b719f8ac0c087bcfc9c9b31e74d49a",
}
EXPECTED_BINARY = "573804ed05e6b2d99addb7dceca61566f546da8f401586e310efcc4e6e19176c"
VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
EXPECTED_OPTIONS = {"Hash": 16, "Move Overhead": 0, "UsePositionalEvaluation": True,
                    "UseQuiescence": True, "UseTranspositionTable": True, "UseFastMoves": True,
                    "UseQuietMoveOrdering": True, "UseQuiescenceChecks": True, "PVS": True,
                    "UseSEE": True, "UsePawnShield": True, "UsePawnStructure": True, "UseLMR": True}
NAMESPACE_FIELDS = {"source_dataset_manifest_sha256", "source_game_id", "source_diagnosis_report_sha256"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def document(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def rows(path):
    result = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            raise ValueError("blank JSONL row")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("nonobject JSONL row")
        result.append(row)
    return result


class Audit:
    def __init__(self):
        self.assertions = 0
        self.sections = []
        self.metrics = {}

    def require(self, condition, message):
        self.assertions += 1
        if not condition:
            raise AssertionError(message)

    def section(self, name, action):
        before = self.assertions
        try:
            action()
            self.sections.append({"name": name, "passed": True, "assertions": self.assertions - before})
        except Exception as error:
            self.sections.append({"name": name, "passed": False, "assertions": self.assertions - before,
                                  "error": f"{type(error).__name__}: {error}"})

    def replay(self, initial_fen, history, *, claim=True):
        self.require(isinstance(history, list), "history is not a list")
        board = chess.Board(initial_fen)
        self.require(board.is_valid(), "invalid initial board")
        for ply, text in enumerate(history):
            self.require(board.outcome(claim_draw=claim) is None,
                         f"history continues after a rule ending at ply {ply}")
            move = chess.Move.from_uci(text)
            self.require(move in board.legal_moves, f"illegal historical move {text}")
            board.push(move)
        return board

    def pv(self, board, texts):
        self.require(isinstance(texts, list) and bool(texts), "missing PV")
        probe = board.copy(stack=True)
        for text in texts:
            self.require(probe.outcome(claim_draw=False) is None, "PV continues after automatic ending")
            move = chess.Move.from_uci(text)
            self.require(move in probe.legal_moves, f"illegal PV move {text}")
            probe.push(move)
        return len(texts)

    def finite_label(self, sample, board):
        for name in ("teacher_cp_stm", "teacher_depth", "teacher_nodes"):
            self.require(type(sample.get(name)) is int, f"invalid {name}")
        self.require(sample["teacher_depth"] >= 1 and sample["teacher_nodes"] >= 1,
                     "teacher depth/node count incomplete")
        raw = sample["teacher_score_raw_info"].split()
        self.require(raw[:1] == ["info"] and "score" in raw and "string" not in raw,
                     "teacher raw info is not a score event")
        index = raw.index("score")
        self.require(raw[index + 1:index + 3] == ["cp", str(sample["teacher_cp_stm"])],
                     "teacher label/raw cp mismatch")
        self.require("upperbound" not in raw and "lowerbound" not in raw, "bounded teacher label")
        self.require("depth" in raw and int(raw[raw.index("depth") + 1]) == sample["teacher_depth"],
                     "teacher label/raw depth mismatch")
        self.require("nodes" in raw and int(raw[raw.index("nodes") + 1]) == sample["teacher_nodes"],
                     "teacher label/raw nodes mismatch")
        return self.pv(board, sample["teacher_pv"])

    def sample(self, sample, game):
        history = sample["history"]
        self.require(history == game["history"][:len(history)], "sample is not a prefix of its game")
        self.require(sample["initial_fen"] == game["initial_fen"] and sample["family"] == game["family"]
                     and sample["split"] == game["split"], "sample/game identity mismatch")
        self.require(type(sample["ply"]) is int and sample["ply"] == len(history), "sample ply mismatch")
        board = self.replay(sample["initial_fen"], history)
        self.require(board.fen(en_passant="fen") == sample["fen"], "sample FEN/history mismatch")
        self.require(sample["turn"] == ("w" if board.turn else "b"), "sample turn mismatch")
        self.require(not board.is_check() and board.outcome(claim_draw=True) is None,
                     "training/holdout sample is in check or terminal/claimable")
        white = sum(VALUES[piece.piece_type] * (1 if piece.color else -1)
                    for piece in board.piece_map().values())
        self.require(sample["material_cp_stm"] == (white if board.turn else -white), "material mismatch")
        outcome = {"1-0": 1, "0-1": -1, "1/2-1/2": 0}[game["result"]]
        self.require(sample["outcome_stm"] == (outcome if board.turn else -outcome), "outcome perspective mismatch")
        self.metrics["validated_sample_pv_plies"] = self.metrics.get("validated_sample_pv_plies", 0) + self.finite_label(sample, board)
        return board

    def pgn_games(self, path, game_rows):
        stream = io.StringIO(Path(path).read_text(encoding="utf-8"))
        parsed = []
        while (game := chess.pgn.read_game(stream)) is not None:
            self.require(not game.errors, "PGN parser reported errors")
            parsed.append(game)
        self.require(len(parsed) == len(game_rows), f"PGN/game count differs for {Path(path).name}")
        for pgn, row in zip(parsed, game_rows):
            self.require(pgn.board().fen(en_passant="fen") == chess.Board(row["initial_fen"]).fen(en_passant="fen"),
                         "PGN initial board differs")
            self.require([move.uci() for move in pgn.mainline_moves()] == row["history"], "PGN/history mismatch")
            self.require(pgn.headers["Result"] == row["result"], "PGN result mismatch")
            if "GameId" in pgn.headers:
                self.require(pgn.headers["GameId"] == row["game_id"], "PGN game id mismatch")
            if "white" in row:
                self.require(pgn.headers["White"] == row["white"] and pgn.headers["Black"] == row["black"],
                             "PGN color/role headers mismatch")


def feature_key(board):
    return tuple(sorted((piece.piece_type - 1 + (0 if piece.color == board.turn else 6)) * 64 +
                        (square if board.turn else square ^ 56)
                        for square, piece in board.piece_map().items()))


def normalize_row(row):
    result = {key: value for key, value in row.items() if key not in NAMESPACE_FIELDS}
    result["game_id"] = row.get("source_game_id", row["game_id"])
    return result


def audit_finished_cycle(state, run, output):
    state, run, output = Path(state).resolve(), Path(run).resolve(), Path(output).resolve()
    cycle = document(run / "cycle.json")
    if cycle.get("status") != "complete":
        raise ValueError("audit is permitted only after a complete guarded cycle; refusing to run")
    audit = Audit()
    catalog = document(state / "catalog.json")
    original = ROOT / "docs/checks/2026-10-18-neural-v1/data"
    dataset_directories = [state / "bootstrap", *[(state / item["manifest"]).parent for item in catalog["datasets"]]]
    source_directories = [*dataset_directories, run / "corpus"]
    loaded, sources = {}, {}
    training = document(run / "model/report.json")
    arena = document(run / "arena/arena.json")
    loss = document(run / "loss-analysis/loss-report.json")
    expected_model = cycle["candidate_sha256"]
    previous_model = cycle["previous_model_sha256"]

    def identity():
        audit.require(sha(run / "regicide.exe") == EXPECTED_BINARY == cycle["binary_sha256"], "frozen binary differs")
        audit.require(sha(run / "model/small-v1.nn") == expected_model == training["model_sha256"],
                      "trained model identity differs")
        audit.require(cycle["attempt_index"] == catalog["attempts_started"] == len(catalog["runs"]), "unexpected attempt accounting")
        audit.require(catalog["runs"][-1] == run.relative_to(state).as_posix(), "selected run is not the latest attempt")
        for index, relative in enumerate(catalog["runs"], 1):
            audit.require(relative.startswith(f"runs/{index:06d}-") and (state / relative).is_dir(), "run catalog order differs")
        for name, digest in BOOTSTRAP_SHA.items():
            audit.require(sha(original / name) == digest == sha(state / "bootstrap" / name),
                          f"bootstrap changed: {name}")
            audit.require(catalog["bootstrap"]["files_sha256"][name] == digest, "bootstrap catalog hash differs")
        for relative, digest in cycle["implementation_sha256"].items():
            audit.require(sha(ROOT / relative) == digest, f"frozen implementation changed: {relative}")
        for reference in catalog["datasets"]:
            directory = (state / reference["manifest"]).parent
            for name, digest in reference["files_sha256"].items():
                audit.require(sha(directory / name) == digest, f"dataset catalog hash mismatch: {name}")
            diagnosis = reference.get("diagnosis")
            if diagnosis:
                for name, digest in diagnosis["files_sha256"].items():
                    audit.require(sha(state / diagnosis["path"] / name) == digest, "diagnosis catalog hash mismatch")
        for key, filename in (("manifest", "manifest.json"), ("games", "games.jsonl"), ("positions", "positions.jsonl")):
            audit.require(training["dataset"]["raw_sha256"][key] == sha(run / "corpus" / filename),
                          "training inputs differ from accumulated corpus")
        audit.require(training["initial_model_sha256"] == previous_model, "training did not initialize from the current champion identity")
        if previous_model is None:
            audit.require(training["initialization"] == "seeded_random", "no-champion training initialization differs")
        history = training["history"]
        selected = min(history, key=lambda row: (row["validation_mse"], row["epoch"]))
        audit.require(training["selected_epoch"] == selected["epoch"], "validation-selected epoch differs")
        audit.require(training["metrics"]["validation"]["selected_mse"] == selected["validation_mse"],
                      "selected validation metric differs")
        audit.metrics["training_selected_epoch"] = training["selected_epoch"]
        audit.metrics["model_sha256"] = expected_model
        audit.metrics["attempt_index"] = cycle["attempt_index"]
        checks = document(run / "rules/checks.json")
        audit.require(checks == cycle["rules"] and checks["status"] == "passed", "rule-check summary differs")
        for key, name in (("independent_rules", "differential.log"), ("native_neural_integration", "neural-integration.log")):
            audit.require(sha(run / "rules" / name) == checks["checks"][key], "rule-check log hash differs")

    def datasets():
        for directory in source_directories:
            manifest = document(directory / "manifest.json")
            game_rows, samples = rows(directory / "games.jsonl"), rows(directory / "positions.jsonl")
            bundle = (manifest, game_rows, samples)
            if directory in dataset_directories:
                sources[sha(directory / "manifest.json")] = bundle
            if directory == state / "bootstrap":
                loaded["bootstrap"] = bundle
            elif directory == run / "data":
                loaded["data"] = bundle
            elif directory == run / "corpus":
                loaded["corpus"] = bundle
            audit.require(manifest["status"] == "complete", "dataset/corpus not complete")
            audit.require(manifest["positions"] == len(samples) and manifest["completed_games"] == len(game_rows),
                          "dataset/corpus manifest row counts differ")
            audit.require(manifest["positions_by_split"] == dict(Counter(row["split"] for row in samples)),
                          "dataset/corpus split counts differ")
            for name, digest in manifest["files_sha256"].items():
                audit.require(sha(directory / name) == digest, "dataset/corpus file hash mismatch")
            games = {}
            family_splits = {}
            for game in game_rows:
                audit.require(game["game_id"] not in games, "duplicate game id")
                games[game["game_id"]] = game
                audit.require(game["completed"] is True, "unfinished source/corpus game")
                board = audit.replay(game["initial_fen"], game["history"])
                outcome = board.outcome(claim_draw=True)
                audit.require(outcome is not None and outcome.result() == game["result"], "game result not justified by chess rules")
                if "termination" in game:
                    audit.require(outcome.termination.name == game["termination"], "game termination disagrees")
                if "fen" in game:
                    audit.require(board.fen(en_passant="fen") == game["fen"], "game final FEN differs")
                audit.require(family_splits.setdefault(game["family"], game["split"]) == game["split"],
                              "family spans splits")
                searches = game.get("selfplay_searches", [])
                start = len(game.get("opening_moves", [])) + len(game.get("random_opening_moves", []))
                audit.require([record["ply"] for record in searches] == list(range(start, len(game["history"]))),
                              "self-play search coverage differs")
                for record in searches:
                    audit.require(record["move"] == game["history"][record["ply"]], "archived search move/history differs")
            seen, position_splits, feature_splits = set(), {}, {}
            for sample in samples:
                key = (sample["game_id"], sample["ply"])
                audit.require(key not in seen, "duplicate sample game/ply")
                seen.add(key)
                board = audit.sample(sample, games[sample["game_id"]])
                position = " ".join(board.fen(en_passant="legal").split()[:4])
                neural = feature_key(board)
                audit.require(position_splits.setdefault(position, sample["split"]) == sample["split"], "cross-split canonical position")
                audit.require(feature_splits.setdefault(neural, sample["split"]) == sample["split"], "cross-split neural input")
            audit.pgn_games(directory / "games.pgn", game_rows)
            label = directory.relative_to(state).as_posix()
            audit.metrics.setdefault("datasets", {})[label] = {"games": len(game_rows),
                "samples": dict(Counter(row["split"] for row in samples))}
        audit.require(len(loaded["data"][1]) == cycle["collection"]["completed_games"] and
                      len(loaded["data"][2]) == cycle["collection"]["positions"], "current-cycle collection count differs")

    def corrections():
        source_manifest = run / "data/manifest.json"
        games = {game["game_id"]: game for game in loaded["data"][1]}
        correction_rows = rows(run / "loss-analysis/positions.jsonl")
        audit.require(loss["status"] == "complete" and not loss["errors"], "loss analysis incomplete")
        audit.require(loss["source_dataset_manifest_sha256"] == sha(source_manifest), "loss analysis source identity differs")
        for name, digest in loss["source_files_sha256"].items():
            audit.require(sha(run / "data" / name) == digest, "loss analysis source bytes differ")
        audit.require(loss["positions_sha256"] == sha(run / "loss-analysis/positions.jsonl"), "correction hash mismatch")
        audit.require(loss["queries_completed"] == len(loss["records"]) * 2, "diagnostic query count differs")
        audit.require(loss["loss_games"] == sum(game["result"] in ("1-0", "0-1") for game in games.values()),
                      "decisive loss count differs")
        record_map = {}
        per_game = Counter()
        for record in loss["records"]:
            key = (record["game_id"], record["ply"])
            audit.require(key not in record_map, "duplicate diagnosis game/ply")
            record_map[key] = record
            per_game[record["game_id"]] += 1
            game = games[record["game_id"]]
            board = audit.replay(record["initial_fen"], record["history"])
            audit.require(record["history"] == game["history"][:record["ply"]], "diagnostic history not game prefix")
            audit.require(record["fen"] == board.fen(en_passant="fen"), "diagnostic FEN differs")
            audit.require(record["played_move"] == game["history"][record["ply"]], "diagnosis actual move differs")
            loser = chess.BLACK if game["result"] == "1-0" else chess.WHITE
            audit.require(game["result"] in ("1-0", "0-1") and board.turn == loser, "diagnosis not a loser decision")
            for name in ("best", "played"):
                query = record[name]
                audit.require(query["requested_nodes"] == loss["limits"]["analysis_nodes_per_query"], "diagnostic query budgets differ")
                audit.require(query["move"] == query["pv"][0], "diagnostic move/PV differs")
                audit.metrics["diagnostic_pv_plies"] = audit.metrics.get("diagnostic_pv_plies", 0) + audit.pv(board, query["pv"])
                if query["label"] is not None:
                    audit.finite_label(query["label"], board)
            audit.require(record["played"]["move"] == record["played_move"], "forced root search ignored actual move")
            best, played = record["best"], record["played"]
            gap = (best["cp_stm"] - played["cp_stm"] if best["exact"] and played["exact"] and
                   best["cp_stm"] is not None and played["cp_stm"] is not None else None)
            audit.require(record["cp_loss"] == gap, "diagnostic cp loss arithmetic differs")
            audit.require(record["classification_is_inference"] is True, "diagnosis falsely labels a causal fact")
        audit.require(all(count <= loss["limits"]["max_positions_per_game"] for count in per_game.values()), "unbounded per-game diagnosis")
        accepted = [record for record in loss["records"] if record["training_exclusion"] is None]
        audit.require(len(accepted) == len(correction_rows) == cycle["loss_analysis"]["training_corrections"], "accepted correction count differs")
        for sample in correction_rows:
            record = record_map[(sample["game_id"], sample["ply"])]
            game = games[sample["game_id"]]
            board = audit.sample(sample, game)
            audit.require(sample["split"] == "train" and sample["outcome_stm"] == -1 and not board.is_check(),
                          "correction is not a noncheck train-family loser position")
            audit.require(record["training_exclusion"] is None and record["reliable_for_training"] is True,
                          "excluded/unreliable correction entered training")
            audit.require(record["best"]["move"] != record["played_move"], "no different better move found")
            audit.require(record["cp_loss"] >= loss["limits"]["min_loss_cp"] >= 100, "correction under declared threshold")
            audit.require(record["best"]["depth"] >= 2 and record["played"]["depth"] >= 2,
                          "insufficient completed diagnostic depth")
            audit.require(loss["limits"]["analysis_nodes_per_query"] > loaded["data"][0]["limits"]["selfplay_nodes"],
                          "diagnosis budget not greater than original search")
            for name, value in record["best"]["label"].items():
                audit.require(sample[name] == value, "correction teacher label differs from actual diagnostic best query")
            audit.require(sample["loss_analysis"]["cp_loss"] == record["cp_loss"] and sample["loss_analysis"]["reliable"] is True,
                          "correction loss metadata differs")
        audit.metrics["diagnosed_positions"] = len(record_map)
        audit.metrics["accepted_loss_corrections"] = len(correction_rows)

    def corpus_holdouts():
        bootstrap_samples = loaded["bootstrap"][2]
        corpus_manifest, corpus_games, corpus_samples = loaded["corpus"]
        for split in ("validation", "test"):
            expected = sorted([row for row in bootstrap_samples if row["split"] == split],
                              key=lambda row: (row["game_id"], row["ply"]))
            observed = sorted([normalize_row(row) for row in corpus_samples if row["split"] == split],
                              key=lambda row: (row["game_id"], row["ply"]))
            audit.require(expected == observed, f"fixed {split} histories or labels changed")
            audit.require(all(row["source_dataset_manifest_sha256"] == BOOTSTRAP_SHA["manifest.json"]
                              and "source_diagnosis_report_sha256" not in row
                              for row in corpus_samples if row["split"] == split), "held-out source provenance changed")
        source_rows = {}
        source_games = {}
        for directory in dataset_directories:
            identity = sha(directory / "manifest.json")
            for row in sources[identity][2]:
                source_rows[(identity, row["game_id"], row["ply"])] = row
            for game in sources[identity][1]:
                source_games[(identity, game["game_id"])] = game
        for reference in catalog["datasets"]:
            if reference.get("diagnosis"):
                directory = state / reference["diagnosis"]["path"]
                report = document(directory / "loss-report.json")
                identity = report["source_dataset_manifest_sha256"]
                audit.require(identity in sources, "accumulated diagnosis source is unregistered")
                for row in rows(directory / "positions.jsonl"):
                    source_rows[(identity, row["game_id"], row["ply"])] = row
        for row in corpus_samples:
            source = source_rows[(row["source_dataset_manifest_sha256"], row["source_game_id"], row["ply"])]
            audit.require(normalize_row(row) == source, "corpus row cannot be traced to an original or diagnosed source label")
        for game in corpus_games:
            source = source_games[(game["source_dataset_manifest_sha256"], game["source_game_id"])]
            audit.require(normalize_row(game) == source, "corpus game differs from its original history")
        counts = dict(Counter(row["split"] for row in corpus_samples))
        audit.require(counts["validation"] == 96 and counts["test"] == 125 and counts["train"] > 0,
                      "fixed holdout counts changed or train split empty")
        audit.require(counts == corpus_manifest["positions_by_split"] == cycle["corpus"], "corpus/cycle counts disagree")
        audit.require(corpus_manifest["bootstrap_manifest_sha256"] == BOOTSTRAP_SHA["manifest.json"], "corpus bootstrap identity differs")
        audit.metrics["fixed_holdout_rows"] = {"validation": 96, "test": 125}

    def arena_replay():
        audit.require(arena["status"] == "complete" and arena["completed"] is True and not arena["errors"], "arena did not complete cleanly")
        audit.require(arena["binary_sha256"] == cycle["binary_sha256"] == EXPECTED_BINARY, "arena binary differs")
        audit.require(arena["candidate_sha256"] == expected_model, "arena candidate differs")
        audit.require(arena["champion_sha256"] == previous_model, "arena champion differs")
        audit.require(sha(run / "arena/candidate.nn") == expected_model, "arena model snapshot changed")
        if previous_model:
            audit.require(sha(run / "arena/champion.nn") == previous_model, "arena champion snapshot changed")
        audit.require(arena["movetime_ms"] >= 100 and arena["hash_mb"] == 16 and arena["threads"] == 1,
                      "arena time/memory/thread conditions differ")
        allowance = arena["movetime_ms"] / 1000 + max(.1, arena["movetime_ms"] / 1000)
        audit.require(arena["move_response_allowance_seconds"] == allowance, "time allowance differs")
        for role in ("baseline", "candidate"):
            identity = arena["engines"][role]
            audit.require(identity["binary_sha256"] == EXPECTED_BINARY, "role uses a different binary")
            audit.require(all(identity["options"].get(name) == value for name, value in EXPECTED_OPTIONS.items()), "role search options differ")
            role_model = expected_model if role == "candidate" else previous_model
            audit.require(identity["model_sha256"] == role_model and
                          identity["options"]["UseNeuralEvaluation"] is (role_model is not None), "role evaluation activation differs")
            if role_model:
                snapshot_name = "candidate.nn" if role == "candidate" else "champion.nn"
                audit.require(Path(identity["options"]["EvalFile"]).resolve() == (run / "arena" / snapshot_name).resolve(),
                              "EvalFile not frozen snapshot")
        reserve = arena["opening_reserve"]
        fixture_path = ROOT / "tests/data/learning_arena_openings_v1.json"
        fixture = document(fixture_path)
        audit.require(reserve["sha256"] == sha(fixture_path) and reserve["document"] == fixture, "opening reserve changed")
        audit.require(reserve["seed"] == cycle["arena_seed"], "fresh arena seed differs")
        audit.require("after" in cycle["arena_seed_policy"], "no recorded post-candidate seed policy")
        generator = random.Random(reserve["seed"])
        expected_openings = []
        for index in range(arena["requested_pairs"]):
            selected_seed = generator.choice(fixture["seeds"])
            rng = random.Random(selected_seed)
            opening_moves = list(rng.choice(fixture["prefixes"]))
            board = chess.Board()
            for move in opening_moves:
                board.push_uci(move)
            while len(opening_moves) < 12:
                audit.require(board.outcome(claim_draw=True) is None, "generated opening already terminal")
                move = rng.choice(sorted(board.legal_moves, key=lambda move: move.uci()))
                opening_moves.append(move.uci())
                board.push(move)
            audit.require(board.outcome(claim_draw=True) is None, "reserved opening ended at the final generated ply")
            expected_openings.append({"name": f"reserved-{index + 1:04d}-seed-{selected_seed}",
                                      "seed": selected_seed, "moves": opening_moves,
                                      "fen_after_opening": board.fen(en_passant="fen")})
        audit.require(reserve["openings"] == expected_openings, "opening schedule not reproducible from recorded fresh seed")
        games = arena["games"]
        audit.require(arena["requested_pairs"] >= 32 and len(games) == arena["requested_pairs"] * 2,
                      "not a complete >=32-pair arena")
        pair_scores, outcomes, game_rows, total_plies, response_max = [], Counter(), [], 0, 0.0
        for index, opening in enumerate(expected_openings):
            points = []
            for color_index in range(2):
                record = games[index * 2 + color_index]
                white = "candidate" if color_index == 0 else "baseline"
                audit.require(record["pair"] == index + 1 and record["game"] == index * 2 + color_index + 1,
                              "pair/game order differs")
                audit.require(record["white"] == white and record["black"] == ("baseline" if white == "candidate" else "candidate"),
                              "paired colors not swapped")
                audit.require(record["opening_uci_moves"] == opening["moves"] and record["opening"] == opening["name"]
                              and record["uci_moves"][:12] == opening["moves"] and record["initial_fen"] == chess.STARTING_FEN,
                              "paired opening differs")
                audit.require(record["completed"] is True and record.get("error") is None, "incomplete/failed arena game")
                board = chess.Board()
                searches = record["search_moves"]
                audit.require(len(searches) == len(record["uci_moves"]) - 12, "arena search coverage differs")
                for ply, text in enumerate(record["uci_moves"]):
                    audit.require(board.outcome(claim_draw=True) is None, "arena moves continue beyond rule ending")
                    move = chess.Move.from_uci(text)
                    audit.require(move in board.legal_moves, "illegal actual arena move")
                    if ply >= 12:
                        evidence = searches[ply - 12]
                        role = record["white"] if board.turn else record["black"]
                        elapsed = evidence["elapsed_seconds"]
                        audit.require(evidence["move"] == text and evidence["role"] == role and evidence["ply"] == ply + 1
                                      and evidence["fen_before"] == board.fen(en_passant="fen"), "arena move role/history evidence differs")
                        audit.require(isinstance(elapsed, (float, int)) and math.isfinite(elapsed) and 0 <= elapsed <= allowance,
                                      "arena response exceeded fixed allowance")
                        response_max = max(response_max, elapsed)
                        pv = evidence.get("pv", [])
                        if pv:
                            audit.require(pv[0] == text, "arena chosen move/PV differs")
                            audit.metrics["arena_pv_plies"] = audit.metrics.get("arena_pv_plies", 0) + audit.pv(board, pv)
                    board.push(move)
                outcome = board.outcome(claim_draw=True)
                audit.require(outcome is not None and outcome.result() == record["result"] and
                              outcome.termination.name.lower() == record["termination"], "arena result not justified by rules")
                audit.require(board.fen(en_passant="fen") == record["final_fen"] and len(record["uci_moves"]) == record["plies"],
                              "arena final board/ply count differs")
                score = .5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (white == "candidate"))
                audit.require(record["candidate_points"] == score, "candidate points do not follow actual winner")
                points.append(score)
                outcomes["wins" if score == 1 else "draws" if score == .5 else "losses"] += 1
                total_plies += len(record["uci_moves"])
                game_rows.append({"initial_fen": chess.STARTING_FEN, "history": record["uci_moves"],
                                  "result": record["result"], "white": record["white"], "black": record["black"]})
            pair_scores.append(sum(points) / 2)
        audit.require(sha(run / "arena/games.pgn") == arena["pgn_sha256"], "arena PGN hash differs")
        audit.pgn_games(run / "arena/games.pgn", game_rows)
        count, attempt = len(pair_scores), arena["attempt_index"]
        alpha = .05 / (attempt * (attempt + 1))
        mean = sum(pair_scores) / count
        lower = max(0, mean - math.sqrt(math.log(1 / alpha) / (2 * count)))
        clean = arena["resource_guard"]["valid_for_comparison"] is True
        eligible = count >= 32 and arena["movetime_ms"] >= 100 and clean
        passed = bool(eligible and lower > .5)
        decision = arena["decision"]
        for key, value in (("attempt_alpha", alpha), ("candidate_pair_score", mean),
                           ("one_sided_lower_bound", lower), ("completed_pairs", count),
                           ("eligible", eligible), ("passed", passed), ("attempt_index", attempt)):
            audit.require(decision[key] == value, f"independent statistical computation differs: {key}")
        audit.require(arena["promotion_passed"] is passed and cycle["arena"]["decision"] == decision,
                      "arena/cycle promotion decision mismatch")
        audit.metrics.update(arena_games=len(games), arena_pairs=count, arena_plies=total_plies,
                             arena_candidate_outcomes=dict(outcomes), candidate_pair_score=mean,
                             independent_alpha=alpha, independent_lower_bound=lower,
                             independent_promotion_passed=passed, maximum_response_seconds=response_max)

    def resources_and_registry():
        directory = run / "arena"
        audit.require(sha(directory / "resource-samples.jsonl") == arena["resource_samples_sha256"], "resource evidence hash differs")
        samples = rows(directory / "resource-samples.jsonl")
        guard = arena["resource_guard"]
        audit.require(guard["valid_for_comparison"] is True and guard["contaminated"] is False and
                      guard["failure"] is None and guard["external_cpu_increase_ticks"] == 0 and
                      guard["observed_external_engine_cpu_increase"] is False, "resource guard did not remain clean")
        audit.require(len(samples) == guard["snapshot_count"] and len(samples) >= 2, "resource sampling coverage differs")
        transports = guard["transport_engines"]
        identities = {(row["pid"], row["creation_ticks"]) for row in guard["owned_engines"]}
        transport_ids = {(row["pid"], row["creation_ticks"]) for row in transports.values()}
        audit.require(set(transports) == {"baseline", "candidate"} and len(identities) == 2 and identities == transport_ids,
                      "owned process/transport identities differ")
        external_previous, owned_previous, elapsed_previous = None, {}, -1.0
        maximum_gap = maximum_duration = 0
        for sample in samples:
            audit.require(sample["contaminated"] is False and sample["external_cpu_delta_ticks"] == 0,
                          "contaminated resource sample")
            audit.require(0 <= sample["gap_seconds"] <= 2 and 0 <= sample["snapshot_duration_seconds"] <= 2,
                          "resource timing gap exceeds fail-closed limit")
            audit.require(sample["elapsed_seconds"] >= elapsed_previous, "resource sampling time moved backwards")
            elapsed_previous = sample["elapsed_seconds"]
            maximum_gap = max(maximum_gap, sample["gap_seconds"])
            maximum_duration = max(maximum_duration, sample["snapshot_duration_seconds"])
            external = {}
            for process in sample["processes"]:
                audit.require("error" not in process, "inaccessible resource process")
                identity = (process["pid"], process["creation_ticks"])
                if process["owned"]:
                    audit.require(identity in identities, "an unregistered owned process was exempted")
                    audit.require(process["cpu_ticks"] >= owned_previous.get(identity, 0), "owned CPU counter decreased")
                    owned_previous[identity] = process["cpu_ticks"]
                else:
                    audit.require(not process["exited_during_snapshot"], "external process exited during resource sample")
                    external[identity] = process["cpu_ticks"]
            if external_previous is not None:
                audit.require(external == external_previous, "external identity/CPU ticks changed despite zero-delta summary")
            external_previous = external
        audit.require(guard["max_snapshot_gap_seconds"] == maximum_gap and
                      guard["max_snapshot_duration_seconds"] == maximum_duration, "resource timing summary mismatch")
        passed = audit.metrics["independent_promotion_passed"]
        audit.require(cycle["adopted"] is passed, "adoption does not follow independent match criterion")
        champion_path = state / "champion.json"
        if not passed:
            audit.require(cycle["selected_model_sha256"] == previous_model,
                          "failed candidate replaced the previous champion")
            if previous_model is None:
                audit.require(not champion_path.exists(), "a failed no-champion candidate created a champion registry")
            else:
                audit.require(document(champion_path)["model_sha256"] == previous_model, "previous champion was changed despite a failed candidate")
        else:
            champion = document(champion_path)
            audit.require(champion["model_sha256"] == expected_model and champion["engine_sha256"] == EXPECTED_BINARY,
                          "registry adopted different model/engine identities")
            audit.require(sha(state / champion["relative_model_path"]) == expected_model, "registry model snapshot differs")
            audit.require(champion["arena_sha256"] == sha(directory / "arena.json"), "registry arena evidence identity differs")
        audit.metrics.update(resource_samples=len(samples), maximum_resource_gap_seconds=maximum_gap,
                             adopted=cycle["adopted"], retained_model_sha256=cycle.get("selected_model_sha256"))

    def previous_failed_attempts():
        failures = []
        for relative in catalog["runs"][:-1]:
            directory = state / relative
            previous = document(directory / "cycle.json")
            prior_training = document(directory / "model/report.json")
            audit.require(sha(directory / "model/small-v1.nn") == prior_training["model_sha256"] == previous["candidate_sha256"],
                          "previous candidate model was not preserved")
            arena_path = directory / "arena/arena.json"
            if previous["status"] in ("failed", "interrupted") and arena_path.exists():
                prior_arena = document(arena_path)
                audit.require(previous["adopted"] is False and prior_arena["promotion_passed"] is False,
                              "invalid previous arena was used to adopt a model")
                audit.require(prior_arena["status"] != "complete" and prior_arena["decision"]["passed"] is False,
                              "invalid previous arena has a passing criterion")
                for key, filename in (("resource_samples_sha256", "resource-samples.jsonl"), ("pgn_sha256", "games.pgn")):
                    audit.require(sha(directory / "arena" / filename) == prior_arena[key], "failed arena evidence was not preserved")
                failures.append({"attempt_index": previous["attempt_index"], "played_games": len(prior_arena["games"]),
                                 "status": prior_arena["status"], "adopted": False,
                                 "resource_failure": prior_arena["resource_guard"].get("failure"),
                                 "retained_dataset_manifest_sha256": sha(directory / "data/manifest.json"),
                                 "retained_candidate_sha256": prior_training["model_sha256"]})
        audit.metrics["previous_invalid_attempts_preserved_and_excluded"] = failures

    for name, action in (("frozen identities and epoch selection", identity),
                         ("source/corpus games, labels and PGNs", datasets),
                         ("same-root loss diagnoses and train corrections", corrections),
                         ("fixed holdouts and source provenance", corpus_holdouts),
                         ("independent paired arena replay and statistics", arena_replay),
                         ("native resource evidence and registry decision", resources_and_registry),
                         ("prior invalid arenas excluded; data and candidate models preserved", previous_failed_attempts)):
        audit.section(name, action)
    report = {"schema": "regicide.learning.independent-cycle-audit.v1",
              "finished_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "status": "passed" if all(section["passed"] for section in audit.sections) else "failed",
              "assertions": audit.assertions, "sections": audit.sections, "metrics": audit.metrics,
              "audited_run": run.relative_to(state).as_posix(), "catalog_sha256": sha(state / "catalog.json"),
              "bootstrap_manifest_sha256": BOOTSTRAP_SHA["manifest.json"],
              "method": "Independent Python chess-rule/history/PV/PGN replay and direct arithmetic; no native engine calls or implementation helper imports",
              "scope": "Verifies saved evidence and consistency, not universal strength; external workload observation is limited to named processes sampled by the original guard",
              "input_sha256": {name: sha(run / name) for name in ("cycle.json", "data/manifest.json", "corpus/manifest.json",
                                      "loss-analysis/loss-report.json", "model/report.json", "arena/arena.json")},
              "audit_script_sha256": sha(__file__)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Independent learning-cycle audit", "", f"Status: **{report['status']}**; {audit.assertions:,} assertions.", "",
             "This audit ran after the guarded arena ended. It launched no engine and imported no learning implementation helpers.", ""]
    for section in audit.sections:
        lines.append(f"- {'PASS' if section['passed'] else 'FAIL'} {section['name']} ({section['assertions']:,} assertions)" +
                     (f": {section['error']}" if "error" in section else ""))
    lines += ["", "```json", json.dumps(audit.metrics, ensure_ascii=False, indent=2), "```", "",
              report["scope"], ""]
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": report["status"], "assertions": report["assertions"], "metrics": report["metrics"]}, indent=2))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=ROOT / "build/learning-cycle")
    parser.add_argument("--run", type=Path,
                        help="completed run directory; default reads the latest run from the state catalog")
    parser.add_argument("--output", type=Path, default=ROOT / "build/continual-cycle-validation/independent-cycle-audit.json")
    args = parser.parse_args()
    run = args.run or args.state / document(args.state / "catalog.json")["runs"][-1]
    return audit_finished_cycle(args.state, run, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
