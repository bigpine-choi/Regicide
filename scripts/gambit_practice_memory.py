"""Exact-position memory from native post-sacrifice operational practice.

This is an inspected analysis table, not a neural evaluator or a claim of
generalization.  Its candidates still require fresh native safety verification.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import chess

from game_outcome import automatic_outcome

POLICY = "regicide-gambit-practice-memory-v1"
SCHEMA = "regicide.gambit-operations-memory.v1"
SOURCE = "native_practice_memory"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY = ROOT / "data/gambits/operations_memory.json"
MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10000
MAX_HISTORY_PLIES = 160
GOALS = ("development", "attack_maintenance", "material_recovery", "defensive_transition", "consolidation")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def memory_digest(value):
    return digest({key: item for key, item in value.items() if key != "memory_id"})


def position_key(board):
    return " ".join(board.fen(en_passant="legal").split()[:4])


def _board(history):
    if not isinstance(history, list) or len(history) > MAX_HISTORY_PLIES:
        raise ValueError("invalid history")
    board = chess.Board()
    for uci in history:
        if not isinstance(uci, str):
            raise ValueError("invalid move")
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("illegal history")
        board.push(move)
    return board


def _context(board):
    if (not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN
            or len(board.move_stack) > MAX_HISTORY_PLIES or board.is_repetition(2)
            or automatic_outcome(board) is not None):
        return None
    history = [move.uci() for move in board.move_stack]
    try:
        replay = _board(history)
    except (ValueError, TypeError):
        return None
    if replay.fen(en_passant="fen") != board.fen(en_passant="fen"):
        return None
    return {"fen": board.fen(en_passant="fen"), "history_uci": history,
            "owner": "white" if board.turn else "black", "position_key": position_key(board)}


def _validate(value):
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("policy") != POLICY
            or value.get("memory_id") != memory_digest(value)):
        raise ValueError("invalid memory identity")
    identity = value.get("engine_identity")
    if (not isinstance(identity, dict) or set(identity) != {"binary_sha256", "evaluation_id"}
            or not isinstance(identity["binary_sha256"], str) or len(identity["binary_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in identity["binary_sha256"])
            or not isinstance(identity["evaluation_id"], str) or not identity["evaluation_id"]):
        raise ValueError("invalid engine identity")
    rows = value.get("records")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_RECORDS:
        raise ValueError("invalid record count")
    heldout = value.get("heldout_family_ids")
    if not isinstance(heldout, list) or not heldout or not all(isinstance(item, str) for item in heldout):
        raise ValueError("invalid heldout families")
    seen_ids = set()
    heldout_keys = set(value.get("heldout_position_keys", []))
    for row in rows:
        if (not isinstance(row, dict) or row.get("split") not in ("train", "dev", "validation")
                or row.get("family_id") in heldout or row.get("goal") not in GOALS
                or not isinstance(row.get("record_id"), str) or row["record_id"] in seen_ids):
            raise ValueError("invalid memory record")
        seen_ids.add(row["record_id"])
        board = _board(row.get("history_uci"))
        if (row.get("fen") != board.fen(en_passant="fen") or row.get("position_key") != position_key(board)
                or row.get("owner") != ("white" if board.turn else "black") or position_key(board) in heldout_keys):
            raise ValueError("invalid recorded state")
        candidates = row.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
            raise ValueError("invalid candidate count")
        moves = []
        for item in candidates:
            move = chess.Move.from_uci(item["uci"])
            if (move not in board.legal_moves or move in moves or item.get("goal") not in GOALS
                    or type(item.get("offline_owner_cp")) is not int
                    or type(item.get("native_depth")) is not int or item["native_depth"] < 4
                    or type(item.get("requested_nodes")) is not int or item["requested_nodes"] < 1
                    or not isinstance(item.get("query_id"), str)):
                raise ValueError("invalid candidate evidence")
            moves.append(move)
            child = board.copy(stack=True)
            child.push(move)
            if position_key(child) in heldout_keys:
                raise ValueError("heldout child overlap")
            replay = board.copy(stack=True)
            pv = item.get("pv_uci")
            if not isinstance(pv, list) or not 2 <= len(pv) <= 16 or pv[0] != item["uci"]:
                raise ValueError("missing reply evidence")
            for uci in pv:
                follow = chess.Move.from_uci(uci)
                if follow not in replay.legal_moves:
                    raise ValueError("illegal candidate line")
                replay.push(follow)
    return value


@lru_cache(maxsize=4)
def _read(path, modified_ns, size):
    if not 0 < size <= MAX_BYTES:
        raise ValueError("invalid memory size")
    payload = Path(path).read_bytes()
    if len(payload) != size:
        raise ValueError("memory changed during read")
    value = _validate(json.loads(payload))
    index = {}
    for row in value["records"]:
        index.setdefault(digest([row["fen"], row["history_uci"]]), []).append(row)
    return value, index


def _load(memory_path=None):
    path = Path(memory_path) if memory_path is not None else DEFAULT_MEMORY
    stat = path.stat()
    return _read(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def memory_identity(memory_path=None):
    try:
        data, _index = _load(memory_path)
        return {"policy": POLICY, "memory_id": data["memory_id"], "status": data.get("status")}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return {"policy": POLICY, "memory_id": None, "status": "unavailable"}


def metadata(memory_path=None):
    identity = memory_identity(memory_path)
    try:
        data, _index = _load(memory_path)
        return {**identity, "available": data.get("status") == "approved",
                "scope": "exact practiced full histories only; no unseen-family generalization",
                "records": len(data["records"]), "engine_identity": copy.deepcopy(data["engine_identity"]),
                "counts": copy.deepcopy(data.get("counts", {})),
                "heldout_family_ids": list(data["heldout_family_ids"])}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return {**identity, "available": False, "records": 0}


def _proposal(data, record, item, current, identity):
    return {"source": SOURCE, "policy": POLICY, "uci": item["uci"],
        "record_id": record["record_id"], "memory_id": data["memory_id"],
        "family_id": record["family_id"], "case_id": record["case_id"],
        **copy.deepcopy(current), "engine_identity": copy.deepcopy(identity),
        "goal": item["goal"], "offline_evidence": copy.deepcopy(item),
        "candidate_only": True, "loss_cap_cp": 35}


def proposals(board, seed, *, engine_identity, context=None, limit=4, memory_path=None):
    current = _context(board)
    if current is None or type(limit) is not int or not 1 <= limit <= 8:
        return []
    try:
        data, index = _load(memory_path)
        if (data.get("status") != "approved" or data.get("acceptance", {}).get("passed") is not True
                or engine_identity != data["engine_identity"]):
            return []
        rows = index.get(digest([current["fen"], current["history_uci"]]), [])
        result, seen = [], set()
        for record in sorted(rows, key=lambda row: row["record_id"]):
            if isinstance(context, dict) and context.get("family_id") not in (None, record["family_id"]):
                continue
            for item in sorted(record["candidates"], key=lambda value: (-value["offline_owner_cp"], value["uci"])):
                if item["uci"] in seen:
                    continue
                seen.add(item["uci"])
                result.append(_proposal(data, record, item, current, engine_identity))
        return result[:limit]
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return []


def valid_proposal(board, proposal, *, engine_identity, memory_path=None):
    if not isinstance(proposal, dict):
        return False
    current = _context(board)
    if current is None:
        return False
    try:
        data, index = _load(memory_path)
        if (data.get("status") != "approved" or data.get("acceptance", {}).get("passed") is not True
                or engine_identity != data["engine_identity"]):
            return False
        # Resolve the authoritative row, not a possibly different deduplicated
        # representative of the same move from another practiced case.
        for record in index.get(digest([current["fen"], current["history_uci"]]), []):
            if record["record_id"] != proposal.get("record_id"):
                continue
            return any(proposal == _proposal(data, record, item, current, engine_identity)
                       for item in record["candidates"])
        return False
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return False
