"""Pure validation and provenance helpers for own-search training records.

Search scores describe the root position.  They are deliberately never named
``classical_stm_cp``, which belongs to the earlier static-imitation dataset.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import random
import re

import chess


KINDS = ("selfplay_start", "synthetic_endgame")
PARTITIONS = ("train", "validation")


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path: Path | str, value: object) -> None:
    """Publish a new JSON artifact; never overwrite an earlier experiment."""
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def feature_hash(board: chess.Board) -> str:
    """Identity of current pieces-only inputs, including color/rank symmetry."""
    identity = min(board.board_fen(), board.mirror().board_fen())
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def state_hash(initial_fen: str, history: list[str]) -> str:
    if not isinstance(initial_fen, str) or not isinstance(history, list) or any(
            not isinstance(move, str) for move in history):
        raise ValueError("State identity requires an initial FEN and a list of UCI strings")
    identity = json.dumps({"initial_fen": initial_fen, "history": history},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def board_from(initial_fen: str, history: list[str]) -> chess.Board:
    """Replay legal orthodox history; only its final position may be terminal."""
    state_hash(initial_fen, history)  # Enforce the public representation.
    if len(initial_fen.split()) != 6:
        raise ValueError("Initial FEN must contain all six orthodox FEN fields")
    try:
        board = chess.Board(initial_fen, chess960=False)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid initial FEN") from error
    if not board.is_valid():
        raise ValueError("Invalid initial orthodox position")
    for text in history:
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("History continues after an outcome or available draw claim")
        try:
            move = chess.Move.from_uci(text)
        except ValueError as error:
            raise ValueError("Malformed history move") from error
        if move not in board.legal_moves:
            raise ValueError("Illegal history move")
        board.push(move)
    return board


def _integer(value: object) -> bool:
    return type(value) is int


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def split_families(seeds: list[dict], validation_fraction: float,
                   seed: int) -> dict[str, str]:
    """Assign whole source families before searching, stratified by source kind."""
    if (isinstance(validation_fraction, bool)
            or not isinstance(validation_fraction, (int, float))
            or not math.isfinite(validation_fraction)
            or not 0 < validation_fraction < 1 or not _integer(seed)):
        raise ValueError("Require a finite validation fraction in (0,1) and an integer seed")
    if not isinstance(seeds, list) or not seeds:
        raise ValueError("Seeds must be a nonempty list")
    family_kinds: dict[str, str] = {}
    inputs: dict[str, str] = {}
    for record in seeds:
        if not isinstance(record, dict):
            raise ValueError("Each seed must be an object")
        family, kind = record.get("family_id"), record.get("kind")
        if not _nonempty_string(family) or kind not in KINDS:
            raise ValueError("Each seed needs a family and supported source kind")
        if family in family_kinds and family_kinds[family] != kind:
            raise ValueError("A family cannot cross source kinds")
        family_kinds[family] = kind
        board = board_from(record.get("initial_fen"), record.get("history", []))
        identity = feature_hash(board)
        if identity in inputs and inputs[identity] != family:
            raise ValueError("Feature-equivalent source positions must share a family")
        inputs[identity] = family
    result: dict[str, str] = {}
    rng = random.Random(seed)
    for kind in KINDS:
        families = sorted(family for family, source in family_kinds.items() if source == kind)
        if len(families) < 2:
            raise ValueError("Each source kind needs at least two unique families")
        rng.shuffle(families)
        count = max(1, min(len(families) - 1, round(len(families) * validation_fraction)))
        validation = set(families[:count])
        result.update({family: "validation" if family in validation else "train"
                       for family in families})
    return dict(sorted(result.items()))


def validate_query(board: chess.Board, row: dict, nodes: int,
                   quiet: bool = True) -> list[str]:
    """Return exclusion reasons without turning ordinary tactical samples into errors."""
    if not _integer(nodes) or nodes < 1 or type(quiet) is not bool:
        raise ValueError("Require a positive node budget and boolean quiet policy")
    if not isinstance(row, dict):
        return ["malformed_query"]
    if row.get("completed") is not True:
        return ["incomplete"]
    reasons: list[str] = []
    score = row.get("score")
    if not isinstance(score, dict) or score.get("kind") != "cp":
        reasons.append("score_kind")
    if not isinstance(score, dict) or not _integer(score.get("stm")):
        reasons.append("score_value")
    if (type(row.get("lowerbound", False)) is not bool
            or type(row.get("upperbound", False)) is not bool
            or row.get("lowerbound", False) or row.get("upperbound", False)):
        reasons.append("bound")
    if not _integer(row.get("depth")) or row["depth"] < 1:
        reasons.append("depth")
    if not _integer(row.get("nodes")) or not 1 <= row["nodes"] <= nodes:
        reasons.append("nodes")
    if row.get("requested_nodes") != nodes or not _integer(row.get("requested_nodes")):
        reasons.append("requested_nodes")
    pv, best = row.get("pv_uci"), row.get("bestmove_uci")
    first = None
    if not isinstance(pv, list) or not pv or any(not isinstance(text, str) for text in pv):
        reasons.append("illegal_pv")
    else:
        replay = board.copy(stack=True)
        for text in pv:
            try:
                move = chess.Move.from_uci(text)
            except ValueError:
                reasons.append("illegal_pv")
                break
            if move not in replay.legal_moves:
                reasons.append("illegal_pv")
                break
            if first is None:
                first = move
            replay.push(move)
    if (not isinstance(best, str) or not isinstance(pv, list) or not pv
            or best != pv[0] or first is None):
        reasons.append("illegal_bestmove")
    if board.outcome(claim_draw=True) is not None:
        reasons.append("terminal")
    if quiet:
        if board.is_check():
            reasons.append("in_check")
        if first is not None:
            if board.is_capture(first):
                reasons.append("capture")
            if first.promotion is not None:
                reasons.append("promotion")
    return reasons


def make_sample(board: chess.Board, query: dict, *, family_id: str, game_id: str,
                ply: int, initial_fen: str, history: list[str], partition: str) -> dict:
    """Copy an exact root CP target, never the score of a PV leaf."""
    requested = query.get("requested_nodes")
    reasons = validate_query(board, query, requested, quiet=False)
    if reasons:
        raise ValueError("Cannot publish an invalid search target: " + ", ".join(reasons))
    replay = board_from(initial_fen, history)
    if replay.fen(en_passant="fen") != board.fen(en_passant="fen"):
        raise ValueError("Sample board does not match its source history")
    if (not _nonempty_string(family_id) or not _nonempty_string(game_id)
            or not _integer(ply) or ply < 0 or partition not in PARTITIONS
            or not _nonempty_string(query.get("query_id"))):
        raise ValueError("Invalid sample linkage")
    value = query["score"]["stm"]
    return {"query_id": query["query_id"], "fen": board.fen(en_passant="fen"),
            "initial_fen": initial_fen, "history": list(history),
            "feature_hash": feature_hash(board), "state_hash": state_hash(initial_fen, history),
            "family_id": family_id, "game_id": game_id, "ply": ply, "partition": partition,
            "search_stm_cp": value, "target_white_cp": value if board.turn else -value,
            "depth": query["depth"], "nodes": query["nodes"],
            "requested_nodes": requested, "pv_uci": list(query["pv_uci"]),
            "bestmove_uci": query["bestmove_uci"]}


def _artifact(directory: Path, name: object) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("Dataset files must use single safe basenames")
    path = directory / name
    if path.resolve().parent != directory.resolve():
        raise ValueError("Dataset file escapes its directory")
    return path


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot read dataset JSON: " + path.name) from error
    if not isinstance(value, dict):
        raise ValueError("Dataset metadata must be an object")
    return value


def _read_rows(path: Path) -> list[dict]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot read dataset records: " + path.name) from error
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Each dataset row must be an object")
    return rows


def load_dataset(directory: Path | str, allow_partial: bool = False) -> dict:
    """Load schema 2 only after replaying source, search and partition links."""
    directory = Path(directory)
    manifest = _read_json(directory / "manifest.json")
    if manifest.get("schema_version") != 2 or type(manifest.get("schema_version")) is not int:
        raise ValueError("Unsupported own-search dataset schema")
    if type(allow_partial) is not bool:
        raise ValueError("allow_partial must be boolean")
    if manifest.get("status") not in ("complete", "budget_exhausted", "error"):
        raise ValueError("Invalid dataset completion status")
    if not allow_partial and manifest["status"] != "complete":
        raise ValueError("Partial datasets require explicit allow_partial=True")
    if (manifest["status"] == "complete" and manifest.get("error") is not None
            or manifest["status"] == "error" and not _nonempty_string(manifest.get("error"))):
        raise ValueError("Dataset error details conflict with its completion status")
    files = manifest.get("files")
    if not isinstance(files, dict) or not {"plan.json", "queries.jsonl", "games.jsonl", "uci.jsonl"} <= files.keys():
        raise ValueError("Dataset manifest is missing required artifacts")
    for name, expected in files.items():
        path = _artifact(directory, name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid artifact checksum")
        try:
            actual = sha256(path)
        except OSError as error:
            raise ValueError("Missing artifact: " + name) from error
        if actual != expected:
            raise ValueError("Artifact checksum mismatch: " + name)
    if manifest.get("plan_sha256") != files["plan.json"]:
        raise ValueError("Plan checksum mismatch")
    plan = _read_json(directory / "plan.json")
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 2:
        raise ValueError("Unsupported own-search plan schema")
    config = plan.get("config")
    if not isinstance(config, dict):
        raise ValueError("Missing dataset generation configuration")
    requested, interval, quiet = config.get("nodes"), config.get("sample_every"), config.get("quiet")
    if not _integer(requested) or requested < 1 or not _integer(interval) or interval < 1 or type(quiet) is not bool:
        raise ValueError("Invalid search/sampling configuration")
    for key in ("max_plies", "max_searches"):
        if not _integer(config.get(key)) or config[key] < 1:
            raise ValueError("Invalid generation count limit")
    for key in ("max_seconds", "search_timeout"):
        value = config.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError("Invalid generation wall-clock limit")
    if config["max_seconds"] > 3600 or type(config.get("static_control")) is not bool:
        raise ValueError("Invalid generation budget or control policy")
    control_status = manifest.get("static_control_status")
    if control_status not in ("disabled", "complete", "wall_budget", "pending"):
        raise ValueError("Unknown static-control completion status")
    if (not config["static_control"] and control_status != "disabled"
            or config["static_control"] and control_status == "disabled"
            or manifest["status"] == "complete" and config["static_control"] and control_status != "complete"):
        raise ValueError("Static-control status conflicts with its declared policy")
    families = split_families(plan.get("seeds"), config.get("validation_fraction"), config.get("seed"))
    if plan.get("families") != families:
        raise ValueError("Family assignments differ from the predeclared split")
    seeds: dict[str, dict] = {}
    for source in plan["seeds"]:
        seed_id = source.get("seed_id")
        if not _nonempty_string(seed_id) or seed_id in seeds:
            raise ValueError("Invalid or duplicate source seed identifier")
        board = board_from(source["initial_fen"], source.get("history", []))
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("A source seed cannot already have an outcome")
        if source["kind"] == "selfplay_start" and source["initial_fen"] != chess.STARTING_FEN:
            raise ValueError("Self-play sources must retain standard-start history")
        if source["kind"] == "synthetic_endgame" and len(board.piece_map()) > 12:
            raise ValueError("Synthetic endgame sources must have at most 12 pieces")
        seeds[seed_id] = source
    planned = len(seeds)
    search_upper = min(config["max_searches"], planned * config["max_plies"])
    for key, expected in (("planned_games", planned), ("search_upper_bound", search_upper),
                          ("node_upper_bound", requested * search_upper)):
        if key in plan and (not _integer(plan[key]) or plan[key] != expected):
            raise ValueError("Plan count bound differs from its configuration")

    games: dict[str, dict] = {}
    game_boards: dict[str, list[str]] = {}
    source_order = list(seeds)
    for game in _read_rows(directory / "games.jsonl"):
        game_id = game.get("game_id")
        if not _nonempty_string(game_id) or game_id in games:
            raise ValueError("Invalid or duplicate game identifier")
        if not _nonempty_string(game.get("seed_id")):
            raise ValueError("Invalid game seed identifier")
        source = seeds.get(game["seed_id"])
        if source is None or any(game.get(key) != source.get(key) for key in ("family_id", "kind", "initial_fen")):
            raise ValueError("Game does not match its source seed")
        if len(games) >= planned or game["seed_id"] != source_order[len(games)]:
            raise ValueError("Games differ from their predeclared source order")
        if game.get("partition") != families[game["family_id"]]:
            raise ValueError("Game crosses its assigned family partition")
        initial_history = source.get("history", [])
        history = game.get("moves_uci")
        if game.get("initial_history") != initial_history or not isinstance(history, list) or history[:len(initial_history)] != initial_history:
            raise ValueError("Game history does not preserve its source history")
        board = board_from(game["initial_fen"], history)
        generated = game.get("generated_plies")
        if (not _integer(generated) or generated != len(history) - len(initial_history)
                or not 0 <= generated <= config["max_plies"]):
            raise ValueError("Game ply count differs from its history")
        if game.get("final_fen") != board.fen(en_passant="fen"):
            raise ValueError("Game final FEN differs from replay")
        outcome = board.outcome(claim_draw=True)
        termination = game.get("termination")
        if termination == "outcome":
            if (outcome is None or game.get("result") != outcome.result()
                    or game.get("outcome_termination") != outcome.termination.name.lower()):
                raise ValueError("Game result differs from its actual outcome")
        elif termination in ("max_plies", "wall_budget", "search_budget", "search_timeout", "error"):
            if game.get("result") is not None or game.get("outcome_termination") is not None:
                raise ValueError("An unfinished game cannot carry a result")
            if outcome is not None or termination == "max_plies" and generated != config["max_plies"]:
                raise ValueError("Game truncation differs from its state or ply limit")
        else:
            raise ValueError("Unknown game termination")
        links = game.get("query_ids")
        if not isinstance(links, list) or any(not _nonempty_string(link) for link in links) or len(set(links)) != len(links):
            raise ValueError("Invalid game query links")
        games[game_id], game_boards[game_id] = game, history
    if manifest["status"] == "complete" and (len(games) != planned or any(
            game["termination"] not in ("max_plies", "outcome") for game in games.values())):
        raise ValueError("A complete dataset must process every planned seed")

    queries: dict[str, dict] = {}
    query_reasons: dict[str, list[str]] = {}
    selected_features: set[str] = set()
    for query in _read_rows(directory / "queries.jsonl"):
        query_id = query.get("query_id")
        if not _nonempty_string(query_id) or query_id in queries:
            raise ValueError("Invalid or duplicate query identifier")
        if not _nonempty_string(query.get("game_id")):
            raise ValueError("Invalid query game identifier")
        game = games.get(query["game_id"])
        if game is None or any(query.get(key) != game.get(key) for key in ("family_id", "kind", "partition", "initial_fen")):
            raise ValueError("Query does not match its source game")
        ply = query.get("ply")
        initial_length = len(game["initial_history"])
        if not _integer(ply) or not 0 <= ply <= game["generated_plies"]:
            raise ValueError("Query ply lies outside its game")
        history = query.get("history")
        if history != game_boards[game["game_id"]][:initial_length + ply]:
            raise ValueError("Query history differs from its game prefix")
        board = board_from(query["initial_fen"], history)
        if (query.get("fen") != board.fen(en_passant="fen")
                or query.get("feature_hash") != feature_hash(board)
                or query.get("state_hash") != state_hash(query["initial_fen"], history)):
            raise ValueError("Query identity differs from its source history")
        reasons = validate_query(board, query, requested, quiet)
        if (type(query.get("completed")) is not bool or type(query.get("sampled")) is not bool
                or not isinstance(query.get("excluded_reasons"), list)):
            raise ValueError("Query sample/exclusion metadata is malformed")
        declared = query["excluded_reasons"]
        if any(not isinstance(reason, str) for reason in declared) or len(declared) != len(set(declared)):
            raise ValueError("Malformed query exclusion reasons")
        expected_reasons = list(reasons)
        if query.get("completed") is True:
            if ply % interval:
                expected_reasons.append("sampling_interval")
            if not expected_reasons and query["feature_hash"] in selected_features:
                expected_reasons.append("duplicate_features")
        if declared != expected_reasons or query["sampled"] != (not expected_reasons):
            raise ValueError("Query sampling differs from its declared filter policy")
        if query["sampled"]:
            selected_features.add(query["feature_hash"])
        queries[query_id], query_reasons[query_id] = query, reasons
    if len(queries) > search_upper:
        raise ValueError("Query count exceeds its declared search budget")
    for game_id, game in games.items():
        actual = [query_id for query_id, query in queries.items() if query["game_id"] == game_id]
        if game["query_ids"] != actual:
            raise ValueError("Game query links differ from recorded searches")
        game_queries = [queries[query_id] for query_id in actual]
        if [query["ply"] for query in game_queries] != list(range(len(game_queries))):
            raise ValueError("Each game must record exactly one query per consecutive generated ply")
        generated = game["generated_plies"]
        complete_game = game["termination"] in ("max_plies", "outcome")
        if len(game_queries) not in ({generated} if complete_game else {generated, generated + 1}):
            raise ValueError("Game moves and search query counts differ")
        offset = len(game["initial_history"])
        for index, query in enumerate(game_queries[:generated]):
            empty_fallback = query.get("depth") == 0 and query.get("pv_uci") == []
            if (query.get("completed") is not True
                    or query.get("bestmove_uci") != game["moves_uci"][offset + index]
                    or not empty_fallback and (
                        "illegal_pv" in query_reasons[query["query_id"]]
                        or "illegal_bestmove" in query_reasons[query["query_id"]])):
                raise ValueError("Game trajectory differs from its completed search decisions")

    result: dict = {"manifest": manifest}
    identities: set[str] = set()
    sample_queries: set[str] = set()
    metadata = manifest.get("partitions")
    if not isinstance(metadata, dict) or set(metadata) != set(PARTITIONS):
        raise ValueError("Dataset must declare train and validation partitions")
    for partition in PARTITIONS:
        entry = metadata[partition]
        if not isinstance(entry, dict) or entry.get("file") != partition + ".jsonl":
            raise ValueError("Unexpected dataset partition filename")
        file = _artifact(directory, entry["file"])
        if entry.get("sha256") != sha256(file):
            raise ValueError("Dataset partition checksum mismatch")
        rows = _read_rows(file)
        if (not _integer(entry.get("count")) or len(rows) != entry["count"]
                or not rows and not (allow_partial and manifest["status"] != "complete")):
            raise ValueError("Empty or inconsistent dataset partition")
        for row in rows:
            if not _nonempty_string(row.get("query_id")):
                raise ValueError("Invalid sample query identifier")
            query = queries.get(row["query_id"])
            if query is None or query["sampled"] is not True or query_reasons[query["query_id"]]:
                raise ValueError("Sample lacks an eligible completed search query")
            board = board_from(query["initial_fen"], query["history"])
            expected = make_sample(board, query, family_id=query["family_id"],
                                   game_id=query["game_id"], ply=query["ply"],
                                   initial_fen=query["initial_fen"], history=query["history"],
                                   partition=query["partition"])
            if (any(row.get(key) != value for key, value in expected.items())
                    or any(not _integer(row.get(key)) for key in
                           ("ply", "search_stm_cp", "target_white_cp", "depth", "nodes", "requested_nodes"))
                    or row.get("partition") != partition):
                raise ValueError("Sample target or provenance differs from its root query")
            if row.get("kind") != query["kind"]:
                raise ValueError("Sample source kind differs from its query")
            if "classical_stm_cp" in row:
                raise ValueError("Search targets cannot masquerade as static labels")
            control_keys = ("classical_static_stm_cp", "classical_static_white_cp")
            present = [key in row for key in control_keys]
            if any(present) and (not all(present) or any(not _integer(row[key]) for key in control_keys)
                                 or row[control_keys[1]] != row[control_keys[0]] * (1 if board.turn else -1)):
                raise ValueError("Static control labels have invalid types or score perspective")
            if (not config["static_control"] and any(present)
                    or config["static_control"] and control_status == "complete" and not all(present)):
                raise ValueError("Static control labels differ from their completion policy")
            if row["feature_hash"] in identities or row["query_id"] in sample_queries:
                raise ValueError("Duplicate feature input or search query across dataset partitions")
            identities.add(row["feature_hash"])
            sample_queries.add(row["query_id"])
        result[partition] = rows
    if sample_queries != {query_id for query_id, query in queries.items() if query["sampled"]}:
        raise ValueError("Sampled queries differ from published dataset rows")
    return result
