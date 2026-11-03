"""Bounded own-engine self-play and search labels; never changes a model."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import time

import chess

from .engine import EngineTeacher, SearchInterrupted
from .schema import (board_from, feature_hash, make_sample, sha256,
                     split_families, state_hash, validate_query, write_json)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEDS = Path(__file__).with_name("seeds.json")


@dataclass(frozen=True)
class Config:
    nodes: int = 10000
    max_plies: int = 96
    sample_every: int = 3
    seed: int = 20261019
    validation_fraction: float = 0.25
    max_seconds: float = 180.0
    max_searches: int = 768
    search_timeout: float = 10.0
    quiet: bool = True
    static_control: bool = True

    def validate(self):
        for name in ("nodes", "max_plies", "sample_every", "max_searches"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(name + " must be a positive integer")
        if type(self.seed) is not int or type(self.quiet) is not bool or type(self.static_control) is not bool:
            raise ValueError("Invalid seed or policy type")
        if (not isinstance(self.validation_fraction, (float, int)) or
                isinstance(self.validation_fraction, bool) or
                not 0 < self.validation_fraction < 1):
            raise ValueError("validation_fraction must be strictly between zero and one")
        for name in ("max_seconds", "search_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + " must be finite and positive")
        if self.max_seconds > 3600:
            raise ValueError("A generation job is capped at one hour")


def now():
    return datetime.now(timezone.utc).isoformat()


def _git(*args):
    reply = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                           text=True, timeout=5, check=False)
    return reply.stdout.strip() if reply.returncode == 0 else None


def source_provenance():
    # Source checkout identity is evidence about inputs, not a claim that an
    # arbitrary supplied executable was compiled from this checkout.
    files = sorted(Path(__file__).parent.glob("*.py"))
    files += sorted((ROOT / "src").glob("*.cpp"))
    files += sorted((ROOT / "include/regicide").glob("*.hpp"))
    return {"checkout_head": _git("rev-parse", "HEAD"),
            "checkout_status": _git("status", "--porcelain=v1"),
            "source_sha256": {p.relative_to(ROOT).as_posix(): sha256(p) for p in files},
            "binary_build_commit": None,
            "binary_build_note": "Executable SHA is recorded separately. Checkout HEAD alone does not attest its build revision."}


def load_seeds(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("sources"), list) or not value["sources"]:
        raise ValueError("Expected seed bank schema 1 with nonempty sources")
    seen = set()
    for row in value["sources"]:
        for key in ("seed_id", "family_id", "kind", "initial_fen", "description", "source"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError("Missing seed metadata: " + key)
        if row["seed_id"] in seen:
            raise ValueError("Duplicate seed_id")
        seen.add(row["seed_id"])
        if row["kind"] not in ("selfplay_start", "synthetic_endgame"):
            raise ValueError("Unknown seed kind")
        board = board_from(row["initial_fen"], row["history"])
        if not board.is_valid() or board.outcome(claim_draw=True) is not None:
            raise ValueError("Seed must be valid and nonterminal: " + row["seed_id"])
        if row["kind"] == "selfplay_start" and row["initial_fen"] != chess.STARTING_FEN:
            raise ValueError("selfplay_start must preserve a standard-start legal history")
        if row["kind"] == "synthetic_endgame" and len(board.piece_map()) > 12:
            raise ValueError("Synthetic endgame seed must have at most 12 pieces")
    return value["sources"]


def _line(stream, row):
    stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    stream.flush()


def _publish_manifest(output, manifest):
    # This one status file is updated within a newly created run directory.
    # Atomic replacement keeps an interrupted write from corrupting it.
    temporary = output / "manifest.next.json"
    write_json(temporary, manifest)
    temporary.replace(output / "manifest.json")


def static_scores(engine_path, rows, deadline):
    """Honest control labels, separate from root search labels."""
    if not rows:
        return []
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("No wall budget for static control")
    reply = subprocess.run([str(engine_path), "eval-batch"],
                           input="".join(row["fen"] + "\n" for row in rows),
                           text=True, capture_output=True, timeout=remaining, check=True)
    lines = reply.stdout.splitlines()
    if len(lines) != len(rows):
        raise ValueError("Static control response count mismatch")
    scores = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 3:
            raise ValueError("Malformed eval-batch response")
        classical, full, incremental = map(int, fields)
        if full != incremental:
            raise ValueError("Engine full/incremental evaluation mismatch")
        scores.append(classical)
    if time.monotonic() >= deadline:
        raise TimeoutError("Wall budget exhausted during static control")
    return scores


def statistics(partitions, queries, games):
    result = {"queries": len(queries), "games": len(games),
              "completed_games": sum(g["result"] is not None for g in games),
              "excluded_reasons": dict(sorted(Counter(
                  reason for q in queries for reason in q["excluded_reasons"]).items())),
              "partitions": {}}
    for name, rows in partitions.items():
        pieces = [len(chess.Board(row["fen"]).piece_map()) for row in rows]
        result["partitions"][name] = {
            "count": len(rows), "kinds": dict(Counter(r["kind"] for r in rows)),
            "side_to_move": dict(Counter(chess.Board(r["fen"]).turn and "white" or "black" for r in rows)),
            "at_most_12_pieces": sum(n <= 12 for n in pieces),
            "min_pieces": min(pieces, default=None), "max_pieces": max(pieces, default=None),
            "depth_min": min((r["depth"] for r in rows), default=None),
            "depth_max": max((r["depth"] for r in rows), default=None),
        }
        controls = [r for r in rows if "classical_static_white_cp" in r]
        result["partitions"][name]["search_static_mae_cp"] = (
            sum(abs(r["target_white_cp"] - r["classical_static_white_cp"]) for r in controls) / len(controls)
            if controls else None)
    return result


def semantic_sha256(partitions, games):
    # No timestamps, measured latency, host paths or UCI line numbers here.
    payload = {"partitions": partitions, "games": games}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def generate(engine_path, output, *, config=Config(), seeds_path=DEFAULT_SEEDS,
             teacher_factory=EngineTeacher, control_labeler=static_scores):
    config.validate()
    started = time.monotonic()
    deadline = started + config.max_seconds
    engine_path, output, seeds_path = Path(engine_path).resolve(), Path(output).resolve(), Path(seeds_path).resolve()
    if not engine_path.is_file():
        raise ValueError("Engine executable does not exist")
    seeds = load_seeds(seeds_path)
    families = split_families(seeds, config.validation_fraction, config.seed)
    output.mkdir(parents=True, exist_ok=False)
    # Exclusive output: an interrupted attempt is never overwritten or resumed.
    plan = {"schema_version": 2, "created_utc": now(), "config": asdict(config),
            "seeds": seeds, "seed_bank_sha256": sha256(seeds_path), "families": families,
            "engine": {"path": str(engine_path), "sha256": sha256(engine_path)},
            "provenance": source_provenance(),
            "environment": {"python": platform.python_version(), "chess": chess.__version__, "platform": platform.platform()},
            "policy": {"teacher": "Own Regicide classical evaluation with own fixed-node search",
                       "node_budget": "One go nodes N per position, no movetime; last completed iteration score",
                       "tt": "New game token for every query; empty TT; full initial FEN and history retained",
                       "sampling": "Root before each move, generated ply modulo sample_every equals zero; quiet exact cp only",
                       "identity": "Global mirror-normalized piece-only identity for current v2 input, plus exact initial-FEN/history hash",
                       "split": "Seed families stratified by source kind before play; related variants must share a family",
                       "results": "Only python-chess outcome(claim_draw=True) yields WDL; truncated games have null result",
                       "scope": "Data generation only; no model training, deployment or strength estimate"},
            "planned_games": len(seeds),
            "search_upper_bound": min(config.max_searches, len(seeds) * config.max_plies),
            "node_upper_bound": config.nodes * min(config.max_searches, len(seeds) * config.max_plies)}
    write_json(output / "plan.json", plan)
    partitions = {"train": [], "validation": []}
    queries, games, seen_features = [], [], set()
    manifest = {"schema_version": 2, "status": "running", "started_utc": now(),
                "plan_sha256": sha256(output / "plan.json"), "error": None,
                "teacher_options": None, "engine_identity": None, "cleanup": None,
                "static_control_status": "pending" if config.static_control else "disabled"}
    _publish_manifest(output, manifest)
    teacher = None
    status, stop_reason = "complete", None
    # Creation order is deliberately the seed-bank order, frozen in plan.json.
    with (output / "queries.jsonl").open("x", encoding="utf-8", newline="\n") as query_stream, \
            (output / "games.jsonl").open("x", encoding="utf-8", newline="\n") as game_stream:
        try:
            teacher = teacher_factory(engine_path, output / "uci.jsonl", config.nodes, deadline, config.search_timeout)
            with teacher:
                manifest["teacher_options"] = teacher.options
                manifest["engine_identity"] = teacher.identity
                for index, source in enumerate(seeds):
                    if stop_reason:
                        break
                    board = board_from(source["initial_fen"], source["history"])
                    game_id = f"game-{index:04}"
                    partition = families[source["family_id"]]
                    game = {"game_id": game_id, "seed_id": source["seed_id"],
                            "family_id": source["family_id"], "kind": source["kind"], "partition": partition,
                            "initial_fen": source["initial_fen"], "initial_history": source["history"],
                            "query_ids": [], "generated_plies": 0,
                            "termination": "max_plies", "result": None, "outcome_termination": None}
                    try:
                        for ply in range(config.max_plies):
                            outcome = board.outcome(claim_draw=True)
                            if outcome is not None:
                                break
                            if time.monotonic() >= deadline:
                                stop_reason = "wall_budget"
                                break
                            if len(queries) >= config.max_searches:
                                stop_reason = "search_budget"
                                break
                            query_id = f"query-{len(queries):06}"
                            metadata = {"query_id": query_id, "family_id": source["family_id"],
                                        "game_id": game_id, "partition": partition, "kind": source["kind"],
                                        "ply": ply, "initial_fen": source["initial_fen"],
                                        "history": [m.uci() for m in board.move_stack],
                                        "fen": board.fen(en_passant="fen"), "feature_hash": feature_hash(board)}
                            metadata["state_hash"] = state_hash(metadata["initial_fen"], metadata["history"])
                            try:
                                query = teacher.query(board, query_id)
                            except SearchInterrupted as exc:
                                query = {**exc.record, "completed": False}
                                query.update(metadata)
                                query["excluded_reasons"] = ["incomplete"]
                                query["sampled"] = False
                                queries.append(query)
                                game["query_ids"].append(query_id)
                                _line(query_stream, query)
                                stop_reason = exc.reason
                                break
                            query.update(metadata)
                            reasons = validate_query(board, query, config.nodes, quiet=config.quiet)
                            if ply % config.sample_every:
                                reasons.append("sampling_interval")
                            if not reasons and query["feature_hash"] in seen_features:
                                reasons.append("duplicate_features")
                            query["excluded_reasons"], query["sampled"] = reasons, not reasons
                            queries.append(query)
                            game["query_ids"].append(query_id)
                            if not reasons:
                                sample = make_sample(board, query, family_id=source["family_id"],
                                                     game_id=game_id, ply=ply, initial_fen=source["initial_fen"],
                                                     history=metadata["history"], partition=partition)
                                sample["kind"] = source["kind"]
                                partitions[partition].append(sample)
                                seen_features.add(query["feature_hash"])
                            _line(query_stream, query)
                            # Check even excluded labels: malformed UCI must not
                            # silently drive a trajectory or become a fake draw.
                            empty_fallback = query.get("depth") == 0 and query.get("pv_uci") == []
                            if not empty_fallback and (
                                    "illegal_pv" in reasons or "illegal_bestmove" in reasons):
                                raise ValueError("Malformed engine move or PV: " + query_id)
                            try:
                                move = chess.Move.from_uci(query["bestmove_uci"])
                            except (ValueError, TypeError, KeyError) as exc:
                                raise ValueError("Missing or malformed engine bestmove") from exc
                            if move not in board.legal_moves:
                                raise ValueError("Illegal engine bestmove")
                            board.push(move)
                            game["generated_plies"] += 1
                        outcome = board.outcome(claim_draw=True)
                        if outcome is not None:
                            game.update(termination="outcome", result=outcome.result(),
                                        outcome_termination=outcome.termination.name.lower())
                        elif stop_reason:
                            game["termination"] = stop_reason
                    except Exception:
                        game["termination"] = "error"
                        raise
                    finally:
                        game.update(moves_uci=[m.uci() for m in board.move_stack], final_fen=board.fen(en_passant="fen"))
                        games.append(game)
                        _line(game_stream, game)
            if stop_reason:
                status = "budget_exhausted" if stop_reason in ("wall_budget", "search_budget") else "error"
                if status == "error":
                    manifest["error"] = stop_reason
            if config.static_control:
                rows = partitions["train"] + partitions["validation"]
                try:
                    controls = control_labeler(engine_path, rows, deadline)
                    for row, score in zip(rows, controls, strict=True):
                        row["classical_static_stm_cp"] = score
                        row["classical_static_white_cp"] = score * (1 if chess.Board(row["fen"]).turn else -1)
                    manifest["static_control_status"] = "complete"
                except (TimeoutError, subprocess.TimeoutExpired):
                    manifest["static_control_status"] = "wall_budget"
                    status = "budget_exhausted"
        except SearchInterrupted as exc:
            stop_reason = exc.reason
            status = "budget_exhausted" if exc.reason == "wall_budget" else "error"
            if status == "error":
                manifest["error"] = exc.reason
        except Exception as exc:
            status = "error"
            manifest["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if teacher is not None:
                try:
                    teacher.close()
                except Exception as exc:
                    status = "error"
                    manifest["error"] = f"Cleanup failed: {type(exc).__name__}: {exc}"
                manifest["cleanup"] = getattr(teacher, "cleanup_status", None)
            # Keep an auditable empty raw log even for failed engine startup.
            if not (output / "uci.jsonl").exists():
                (output / "uci.jsonl").touch()
    partition_meta = {}
    for name, rows in partitions.items():
        file = output / f"{name}.jsonl"
        with file.open("x", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                _line(stream, row)
        partition_meta[name] = {"file": file.name, "count": len(rows), "sha256": sha256(file)}
    try:
        if sha256(engine_path) != plan["engine"]["sha256"]:
            status, manifest["error"] = "error", "Engine executable changed during generation"
    except OSError as exc:
        status, manifest["error"] = "error", f"Engine executable no longer readable: {exc}"
    if status == "complete" and any(not rows for rows in partitions.values()):
        status, manifest["error"] = "error", "No usable samples in one partition; preserve attempt and use a new output"
    manifest.update(status=status, finished_utc=now(), wall_seconds=time.monotonic() - started,
                    stop_reason=stop_reason, partitions=partition_meta,
                    stats=statistics(partitions, queries, games), semantic_sha256=semantic_sha256(partitions, games),
                    files={name: sha256(output / name) for name in (
                        "plan.json", "queries.jsonl", "games.jsonl", "uci.jsonl", "train.jsonl", "validation.jsonl")})
    _publish_manifest(output, manifest)
    return manifest
