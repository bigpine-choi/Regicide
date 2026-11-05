"""Own-search continuations beyond pinned named openings, never book authority."""
from collections import defaultdict
import copy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "data/gambits"
POLICY = "regicide-native-gambit-support-v1"
SCHEMA = "regicide.native-gambit-support.v1"
SOURCE = "native_gambit_continuation"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def board_for(history):
    if not isinstance(history, list) or len(history) > 64:
        raise ValueError("Invalid support history")
    board = chess.Board()
    for uci in history:
        board.push_uci(uci)
    return board


def position_key(board):
    return " ".join(board.fen(en_passant="legal").split()[:4])


@lru_cache(maxsize=2)
def _load_cached(directory, fingerprint):
    directory = Path(directory)
    try:
        manifest_raw = (directory / "native_continuations_manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        raw = (directory / "native_continuations.json").read_bytes()
        data = json.loads(raw)
        base_raw = (directory / "catalog.json").read_bytes()
        base = json.loads(base_raw)
        if (manifest.get("schema") != SCHEMA or data.get("schema") != SCHEMA
                or data.get("policy") != POLICY or manifest.get("data_sha256") != hashlib.sha256(raw).hexdigest()
                or manifest.get("support_id") != data.get("support_id") or manifest.get("engine_returncode") != 0
                or data.get("origin") != "Regicide own search only"
                or manifest.get("base_catalog_sha256") != hashlib.sha256(base_raw).hexdigest()
                or data.get("base_catalog_sha256") != manifest["base_catalog_sha256"]
                or data.get("support_id") != digest({k: v for k, v in data.items() if k != "support_id"})):
            raise ValueError("Support identity differs")
        sources = {row["line_id"]: row for row in base["lines"]}
        queries, index, seen = data["queries"], defaultdict(list), set()
        if (not isinstance(queries, dict) or not isinstance(data["entries"], list)
                or not 0 < len(data["entries"]) <= 10000):
            raise ValueError("Invalid support entries")
        for entry in data["entries"]:
            seed = sources[entry["source_line_id"]]
            provenance = data["source_prefixes"][entry["source_line_id"]]
            history, uci = entry["history_uci"], entry["uci"]
            board = board_for(history)
            query = queries[entry["query_id"]]
            if (entry["record_id"] in seen or entry["family_id"] != seed["family_id"]
                    or entry.get("name") != seed["name"] + " · native continuation"
                    or provenance != {key:seed[key] for key in
                        ("source_url", "source_license", "catalog_commit", "name", "moves", "family_id", "owner")}
                    or history[:len(seed["moves"])] != seed["moves"]
                    or entry["owner"] != seed["owner"]
                    or entry["owner"] != ("white" if board.turn else "black")
                    or entry["fen"] != board.fen(en_passant="fen")
                    or entry["position_key"] != position_key(board)
                    or chess.Move.from_uci(uci) not in board.legal_moves
                    or query.get("history_uci") != history or query.get("fen") != entry["fen"]
                    or query.get("root_moves") is not None or query.get("bestmove_uci") != uci
                    or query.get("completed") is not True or query.get("depth", 0) < 4
                    or type(query.get("nodes")) is not int or not 0 < query["nodes"] <= query.get("requested_nodes", 0)
                    or query["depth"] > query.get("requested_depth", 0)
                    or query.get("lowerbound") or query.get("upperbound")
                    or query.get("score", {}).get("kind") != "cp"
                    or type(query["score"].get("stm")) is not int
                    or not query.get("pv_uci") or query["pv_uci"][0] != uci
                    or data["engine_identity"] != entry["engine_identity"]):
                raise ValueError("Support provenance or history differs")
            for move in query["pv_uci"]:
                board.push_uci(move)
            seen.add(entry["record_id"])
            index[entry["position_key"]].append(entry)
        return data, index, {**manifest, "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest()}, None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return {}, {}, {}, "native_support_unavailable"


def load(directory=None):
    directory = Path(directory or DIRECTORY)
    try:
        fingerprint = tuple((path.stat().st_mtime_ns, path.stat().st_size) for path in
            (directory / "native_continuations_manifest.json", directory / "native_continuations.json", directory / "catalog.json"))
        return _load_cached(str(directory), fingerprint)
    except OSError:
        return {}, {}, {}, "native_support_unavailable"


load.cache_clear = _load_cached.cache_clear


def metadata():
    data, _, manifest, error = load()
    return {"available": error is None, "reason": error, "policy": POLICY,
            "support_id": data.get("support_id"), "manifest_sha256": manifest.get("manifest_sha256"),
            "counts": copy.deepcopy(data.get("counts", {})), "candidate_only": True,
            "engine_identity": copy.deepcopy(data.get("engine_identity", {})), "loss_cap_cp": 0}


def _proposal(data, manifest, row):
    provenance = data["source_prefixes"][row["source_line_id"]]
    return {"source": SOURCE, "policy": POLICY, "uci": row["uci"],
            "record_id": row["record_id"], "query_id": row["query_id"],
            "line_id": row["source_line_id"], "family_id": row["family_id"],
            "name": row["name"], "owner": row["owner"], "role": "knowledge_continuation",
            "source_prefix_uci": list(row["history_uci"]), "source_next_uci": row["uci"],
            "fen": row["fen"], "support_id": data["support_id"],
            "catalog_sha256": data["base_catalog_sha256"],
            "catalog_commit": provenance["catalog_commit"],
            "source_url": "data/gambits/native_continuations.json#" + row["record_id"],
            "prefix_source_url": provenance["source_url"],
            "prefix_source_license": provenance["source_license"],
            "extension_manifest_sha256": manifest["manifest_sha256"],
            "engine_identity": copy.deepcopy(row["engine_identity"]),
            "source_license": "project-generated analysis; source prefix " + provenance["source_license"],
            "candidate_only": True, "loss_cap_cp": 0,
            "native_depth": data["queries"][row["query_id"]]["depth"],
            "basis": "exact-history own-search candidate; current native verification required"}


def proposals(board, *, active_families, seed, limit=4):
    data, index, manifest, error = load()
    if error:
        return []
    history = [move.uci() for move in board.move_stack]
    rows = [row for row in index.get(position_key(board), [])
            if row["history_uci"] == history and row["family_id"] in active_families]
    result, seen = [], set()
    for row in sorted(rows, key=lambda r: (-data["queries"][r["query_id"]]["depth"],
                                          digest([str(seed), r["record_id"]]))):
        if row["uci"] in seen:
            continue
        seen.add(row["uci"])
        result.append(_proposal(data, manifest, row))
        if len(result) >= limit:
            break
    return result


def valid_proposal(board, proposal, *, active_families):
    data, index, manifest, error = load()
    if error or not isinstance(proposal, dict):
        return False
    history = [move.uci() for move in board.move_stack]
    return any(row["history_uci"] == history and row["family_id"] in active_families and
               proposal == _proposal(data, manifest, row)
               for row in index.get(position_key(board), []))
