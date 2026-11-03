"""Audit gambit learning provenance and repeat the deterministic fitted weights.

This reads only the explicit experiment directory and its native transcript.
It does not run an engine, alter a model, or inspect private games.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import chess

import gambit_learning as learning
import train_gambit_policy as training


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(directory, *, replay_fit=True):
    directory = Path(directory)
    data, plan, queries, model = [read(directory / name) for name in
                                  ("dataset.json", "predeclared.json", "queries.json", "policy.json")]
    require(data["schema"] == training.SCHEMA, "dataset schema")
    for key in ("seed", "nodes", "max_candidates", "binary_sha256", "splits", "source_sha256"):
        require(data[key] == plan[key], "predeclared field changed: " + key)
    require(data["source_sha256"] == training.source_hashes(), "source changed since fixed-node generation")
    require(data["all_closed"] is True and data["cleanup"]["returncode_observed"] is True,
            "native process closure was not observed")
    require(data["cleanup"]["returncode"] == 0, "native process did not exit cleanly")
    binary = Path(data["binary"])
    require(binary.is_file() and hashlib.sha256(binary.read_bytes()).hexdigest() == data["binary_sha256"],
            "native binary changed")
    semantic = training.digest({"seed": data["seed"], "nodes": data["nodes"], "groups": data["groups"],
                                "binary_sha256": data["binary_sha256"], "options": data["configured_options"]})
    require(semantic == data["semantic_sha256"], "dataset semantic checksum mismatch")
    learning._validate(model)
    require(model["training"]["dataset_sha256"] == semantic, "model fitted a different dataset")
    require(model["engine_identity"] == {"binary_sha256": data["binary_sha256"], "evaluation_id": "classical_activity_v1"},
            "runtime model identity mismatch")
    require(data["configured_options"].get("UsePieceActivity") is True, "runtime piece-activity profile is missing")
    for key in ("UsePawnShield", "UsePawnStructure", "UseLMR"):
        if key in data["runtime_defaults"]:
            require(data["configured_options"].get(key) == data["runtime_defaults"][key], "legacy options leaked: " + key)
    raw = [json.loads(line) for line in (directory / "native-uci.jsonl").read_text(encoding="utf-8").splitlines()]
    require([row["seq"] for row in raw] == list(range(1, len(raw) + 1)), "raw transcript sequence")
    planned = {row["query_id"]: row for row in plan["roots"]}
    require(len(planned) == len(plan["roots"]), "duplicate planned root")
    query_map = {row["query_id"]: row for row in queries}
    require(len(query_map) == len(queries), "duplicate native query")
    for query in queries:
        require(query["completed"] is True and query["requested_nodes"] == data["nodes"], "incomplete node query")
        require(type(query["nodes"]) is int and query["nodes"] >= 0, "native node count missing")
        require(query["nodes"] >= data["nodes"] or query["score"].get("kind") == "mate",
                "non-mate query did not reach its node budget")
        start, end = query["uci_begin_seq"], query["uci_end_seq"]
        require(1 <= start <= end <= len(raw), "raw query bounds")
        excerpt = raw[start - 1:end]
        sent = [row["line"] for row in excerpt if row["direction"] == "send"]
        received = [row["line"] for row in excerpt if row["direction"] == "recv"]
        require("ucinewgame" in sent, "query did not start a fresh UCI game")
        require("go nodes " + str(data["nodes"]) in sent, "raw fixed-node command mismatch")
        require(any(line.startswith("position ") for line in sent), "raw board history missing")
        require(any(line.startswith("bestmove ") for line in received), "raw bestmove missing")
    seen = set()
    keys = {name: set() for name in ("train", "validation", "holdout")}
    checked_candidates = 0
    for group in data["groups"]:
        identifier = group["query_id"]
        require(identifier in planned and identifier not in seen, "unknown or duplicate group")
        seen.add(identifier)
        original = planned[identifier]
        for key in original:
            require(group[key] == original[key], "group changed after declaration: " + key)
        require(group["split"] == data["splits"]["families"][group["family_id"]], "family crosses split")
        board = chess.Board(group["root_fen"])
        for uci in group["history_uci"]:
            move = chess.Move.from_uci(uci)
            require(move in board.legal_moves, "illegal root history")
            board.push(move)
        require(board.fen(en_passant="fen") == group["fen"], "history/FEN mismatch")
        require(learning.position_key(board) == group["position_key"], "transposition key mismatch")
        keys[group["split"]].add(group["position_key"])
        require(len(group["candidates"]) >= 2, "group has fewer than two exact targets")
        uci_seen = set()
        root_query = query_map[identifier + ":root"]
        generated = training.candidate_moves(board, original, root_query, seed=data["seed"],
                                             max_candidates=data["max_candidates"])
        for row in group["candidates"]:
            move = chess.Move.from_uci(row["uci"])
            require(move in board.legal_moves and move not in uci_seen, "invalid candidate move")
            uci_seen.add(move)
            require(row["original_order"] < len(generated) and generated[row["original_order"]] == move,
                    "candidate generation changed")
            require(row["graph_candidate"] == (row["uci"] in group["graph_candidates"]), "graph candidate provenance")
            require(row["features"] == learning.features(board, move), "candidate feature mismatch")
            require(row["capture"] == board.is_capture(move) and row["check"] == board.gives_check(move),
                    "forcing candidate metadata mismatch")
            query = query_map[row["query_id"]]
            require(query["role"] == "candidate_label" and query["candidate_uci"] == row["uci"], "candidate query provenance")
            require(query["score"]["kind"] == "cp" and not query["lowerbound"] and not query["upperbound"],
                    "non-exact or mate label cannot be centipawns")
            require(query["depth"] >= 3 and row["native_depth"] == query["depth"], "insufficient completed depth")
            require(row["owner_cp"] == -query["score"]["stm"], "white/black target sign mismatch")
            require(row["native_nodes"] == query["nodes"], "candidate node metadata mismatch")
            child = board.copy(stack=True)
            child.push(move)
            require(child.fen(en_passant="fen") == query["fen"], "native child position mismatch")
            require(query["bestmove_uci"] is not None and chess.Move.from_uci(query["bestmove_uci"]) in child.legal_moves,
                    "native best reply is illegal")
            for uci in query["pv_uci"]:
                step = chess.Move.from_uci(uci)
                require(step in child.legal_moves, "native reply PV is illegal")
                child.push(step)
            checked_candidates += 1
    for a, b in (("train", "validation"), ("train", "holdout"), ("validation", "holdout")):
        require(not keys[a] & keys[b], "cross-split transposed group")
    same_fit = None
    if replay_fit:
        fit = training.fit([row for row in data["groups"] if row["split"] == "train"],
                           seed=data["seed"], regularization=model["training"]["regularization"],
                           epochs=model["training"]["epochs"])
        same_fit = all(fit[key] == model[key] for key in ("weights", "means", "scales"))
        require(same_fit, "fixed-seed training did not reproduce fitted parameters")
    return {"success": True, "schema": "regicide.gambit-ranking-audit.v1",
            "groups": len(data["groups"]), "candidate_targets": checked_candidates,
            "native_queries": len(queries), "nodes": sum(row["nodes"] for row in queries),
            "dataset_sha256": semantic, "model_id": model["model_id"], "model_status": model["status"],
            "same_fit": same_fit, "split_counts": dict(Counter(row["split"] for row in data["groups"])),
            "source_hashes_match": True, "binary_hash_matches": True, "all_native_processes_closed": True,
            "files_sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in
                             ("dataset.json", "predeclared.json", "queries.json", "native-uci.jsonl", "policy.json")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.directory)
    if args.output:
        training.write(args.output, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
