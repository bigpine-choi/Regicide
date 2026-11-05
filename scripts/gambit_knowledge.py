"""Finite, sourced gambit continuations; every proposal still needs native search.

This is an opening knowledge graph, not a learned evaluator, a soundness claim,
or an instruction to sacrifice. Position keys retain side, castling and legal
en-passant rights. The actual full legal history is independently validated.
"""
from collections import defaultdict
import copy
import csv
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata

import chess
import chess.pgn

import boss_openings
import gambit_knowledge_support as native_support
from game_outcome import automatic_outcome

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "data/gambits"
POLICY = "regicide-gambit-knowledge-v1"
SCHEMA = "regicide.gambit-knowledge.v1"
MAX_HISTORY_PLIES = 64
MAX_PROPOSALS = 8


def position_key(board):
    """A transposition key; move counters never establish repetition identity."""
    return " ".join(board.fen(en_passant="legal").split()[:4])


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _family_name(name):
    matches = list(re.finditer(r"\b(?:countergambit|gambit)\b", name, re.I))
    return name[:matches[-1].end()] if matches else None


def _slug(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return re.sub(r"-(?:counter)?gambit$", "", value)


def _catalog_stage(name):
    suffix = name[len(_family_name(name) or ""):]
    if re.search(r"\bdeclined\b", suffix, re.I):
        return "declined"
    if re.search(r"\baccepted\b", suffix, re.I):
        return "accepted"
    if re.search(r"\bdefen[cs]e\b|\bcounterattack\b", suffix, re.I):
        return "defense"
    return "offer" if not suffix else "variation"


def build_catalog(output_directory=DIRECTORY, source_directory=None):
    """Rebuild only this derived graph from the pinned, unmodified CC0 catalog."""
    source_directory = Path(source_directory or ROOT / "data/openings")
    source = json.loads((source_directory / "manifest.json").read_text(encoding="utf-8"))
    if source.get("license") != "CC0-1.0" or not re.fullmatch(r"[0-9a-f]{40}", source.get("commit", "")):
        raise ValueError("Opening catalog source identity is invalid")
    authored = boss_openings.repertoire()
    overrides = {}
    for row in authored:
        family = _family_name(row["name"])
        if family and (family not in overrides or len(row["line_id"]) < len(overrides[family]["line_id"])):
            overrides[family] = row
    rows, groups = [], defaultdict(list)
    for filename in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"):
        raw = (source_directory / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["files"][filename]["sha256"]:
            raise ValueError("Opening source checksum differs: " + filename)
        for entry in csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t"):
            family_name = _family_name(entry["name"])
            if family_name is None:
                continue
            game = chess.pgn.read_game(io.StringIO(entry["pgn"] + " *"))
            if game is None or game.errors:
                raise ValueError("Invalid opening PGN: " + entry["name"])
            board, moves, keys = chess.Board(), [], []
            for move in game.mainline_moves():
                if move not in board.legal_moves:
                    raise ValueError("Illegal catalog move")
                keys.append(position_key(board))
                moves.append(move.uci())
                board.push(move)
            keys.append(position_key(board))
            if not moves or len(moves) > MAX_HISTORY_PLIES:
                raise ValueError("Unsupported gambit line length")
            line_id = "knowledge-" + _hash([filename, entry["eco"], entry["name"], moves])[:16]
            row = {"line_id": line_id, "name": entry["name"], "family_name": family_name,
                   "eco": entry["eco"], "moves": moves, "position_keys": keys,
                   "catalog_file": filename, "catalog_pgn": entry["pgn"],
                   "source_url": source["files"][filename]["source_url"],
                   "source_license": source["license"], "catalog_commit": source["commit"],
                   "stage": _catalog_stage(entry["name"])}
            rows.append(row)
            groups[family_name].append(row)
    families = {}
    for family_name, members in sorted(groups.items()):
        override = overrides.get(family_name)
        shortest = min(members, key=lambda row: (len(row["moves"]), row["name"], row["line_id"]))
        anchor_moves = list(override["moves"] if override else shortest["moves"])
        anchor = chess.Board()
        for uci in anchor_moves:
            anchor.push_uci(uci)
        owner = "white" if not anchor.turn else "black"
        family_id = override["line_id"] if override else _slug(family_name)
        if not family_id:
            raise ValueError("Empty gambit family identifier")
        if family_id in families:
            family_id += "-" + _hash(family_name)[:8]
        families[family_id] = {"family_id": family_id, "name": family_name, "owner": owner,
            "anchor_moves": anchor_moves, "anchor_ply": len(anchor_moves),
            "anchor_key": position_key(anchor), "anchor_source": "authored_signature" if override else "shortest_catalog_line",
            "owner_inference": "authored_owner" if override else "last_mover_of_shortest_catalog_line",
            "authored_line_id": override["line_id"] if override else None}
        for row in members:
            row.update(family_id=family_id, owner=owner, anchor_moves=anchor_moves,
                       anchor_ply=len(anchor_moves), anchor_key=position_key(anchor),
                       anchor_source=families[family_id]["anchor_source"])
    unique = {row["line_id"]: row for row in rows}
    rows = [unique[key] for key in sorted(unique)]
    nodes = {key for row in rows for key in row["position_keys"]}
    edges = {(row["position_keys"][i], uci) for row in rows for i, uci in enumerate(row["moves"])}
    value = {"schema": SCHEMA, "policy": POLICY, "source": {
        "repository_url": source["repository_url"], "commit": source["commit"], "license": source["license"],
        "files": {name: source["files"][name]["sha256"] for name in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv")}},
        "counts": {"lines": len(rows), "families": len(families), "positions": len(nodes),
                   "edges": len(edges), "max_plies": max(len(row["moves"]) for row in rows),
                   "accepted_lines": sum(row["stage"] == "accepted" for row in rows),
                   "declined_lines": sum(row["stage"] == "declined" for row in rows),
                   "defense_lines": sum(row["stage"] == "defense" for row in rows)},
        "families": families, "lines": rows}
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    (output_directory / "catalog.json").write_bytes(payload)
    manifest = {"schema": SCHEMA, "policy": POLICY, "catalog_sha256": hashlib.sha256(payload).hexdigest(),
                "source": value["source"], "counts": value["counts"],
                "purpose": "Sourced candidate graph only; no evaluations, win rates, or sacrifice permission"}
    (output_directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8", newline="\n")
    _load.cache_clear()
    return manifest


@lru_cache(maxsize=1)
def _load():
    try:
        manifest = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
        raw = (DIRECTORY / "catalog.json").read_bytes()
        if manifest.get("schema") != SCHEMA or hashlib.sha256(raw).hexdigest() != manifest.get("catalog_sha256"):
            raise ValueError("Derived gambit catalog checksum differs")
        data = json.loads(raw)
        if data.get("schema") != SCHEMA or data.get("policy") != POLICY or data.get("source") != manifest.get("source"):
            raise ValueError("Derived gambit catalog identity differs")
        index = defaultdict(list)
        for row in data["lines"]:
            if len(row["position_keys"]) != len(row["moves"]) + 1 or row["owner"] not in ("white", "black"):
                raise ValueError("Malformed gambit graph row")
            for offset, uci in enumerate(row["moves"]):
                index[row["position_keys"][offset]].append((row, offset, uci))
        return data, index, manifest, None
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}, {}, "gambit_catalog_unavailable"


def metadata():
    data, _index, manifest, error = _load()
    extension = native_support.metadata()
    return {"policy": POLICY, "available": error is None, "reason": error,
            "counts": copy.deepcopy(data.get("counts", {})), "source": copy.deepcopy(data.get("source", {})),
            "catalog_sha256": manifest.get("catalog_sha256"), "candidate_only": True,
            "loss_cap_cp": 35, "learned_evaluator": False,
            "native_support": extension, "support_id": extension.get("support_id"),
            "extension_manifest_sha256": extension.get("manifest_sha256")}


def repertoire():
    """Independent source rows for native data generation and held-out tests."""
    return copy.deepcopy(_load()[0].get("lines", []))


def _history(board):
    if (not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()
            or len(board.move_stack) > MAX_HISTORY_PLIES or board.is_check()
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN
            or board.is_repetition(2) or automatic_outcome(board) is not None):
        return None
    replay, moves, keys = chess.Board(), [], [position_key(chess.Board())]
    for move in board.move_stack:
        if move not in replay.legal_moves:
            return None
        moves.append(move.uci())
        replay.push(move)
        keys.append(position_key(replay))
    if replay.fen(en_passant="fen") != board.fen(en_passant="fen"):
        return None
    return moves, keys


def _active_families(data, history, keys, decisions):
    # An authored selected line is advisory context only when its actual
    # selected move survives in this board's full history.
    chosen = []
    rows = decisions.values() if isinstance(decisions, dict) else decisions
    if isinstance(rows, (list, tuple)) or hasattr(rows, "__iter__"):
        authored = {row["line_id"]: row for row in boss_openings.repertoire()}
        for row in rows:
            if not isinstance(row, dict):
                continue
            ply = row.get("ply")
            source = authored.get(row.get("line_id"))
            if (source and type(ply) is int and 0 < ply <= len(history)
                    and row.get("selected_uci") == row.get("candidate_uci") == history[ply - 1]
                    and source["moves"][:ply] == history[:ply]):
                family = _family_name(source["name"])
                chosen.extend(value["family_id"] for value in data.get("families", {}).values()
                              if value["name"] == family)
    completed = {family_id for family_id, family in data.get("families", {}).items()
                 if family["anchor_key"] in keys[1:]}
    chosen = set(chosen)
    return (chosen & completed) or completed


def context(board, decisions=()):
    checked = _history(board)
    data, index, _manifest, error = _load()
    if checked is None or error:
        return {"active": False, "reason": error or "invalid_or_terminal_history", "policy": POLICY}
    history, keys = checked
    active = _active_families(data, history, keys, decisions)
    owner = "white" if board.turn else "black"
    choices = [value for key, value in data["families"].items() if key in active and value["owner"] == owner]
    if not choices:
        return {"active": False, "reason": "no_completed_family", "policy": POLICY,
                "matching_families": sorted(active)}
    family = max(choices, key=lambda value: (max(i for i, key in enumerate(keys) if key == value["anchor_key"]),
                                             value["anchor_ply"], value["family_id"]))
    anchor_at = max(i for i, key in enumerate(keys) if key == family["anchor_key"])
    offer_square = chess.Move.from_uci(family["anchor_moves"][-1]).to_square
    replay = chess.Board()
    captured, withdrawn = False, False
    for ply, uci in enumerate(history, 1):
        move = chess.Move.from_uci(uci)
        if ply > anchor_at and not captured and not withdrawn:
            if replay.turn != (family["owner"] == "white"):
                capture_square = (move.to_square - 8 if replay.turn == chess.WHITE else move.to_square + 8
                                  ) if replay.is_en_passant(move) else move.to_square
                if replay.is_capture(move) and capture_square == offer_square:
                    captured = True
            elif move.from_square == offer_square:
                withdrawn = True
        replay.push(move)
    authored = family["anchor_source"] == "authored_signature"
    stage = ("accepted" if authored else "anchor_piece_captured") if captured else (
        "offer_withdrawn" if withdrawn else "offered" if len(history) == anchor_at else "declined_or_pending")
    researched = native_support.proposals(board,
        active_families={key for key in active if data["families"][key]["owner"] == owner}, seed="context")
    catalog_continuation = any(row["family_id"] == family["family_id"] for row, _i, _uci in index.get(keys[-1], []))
    return {"active": True, "policy": POLICY, "family_id": family["family_id"], "line_id": family["family_id"],
            "name": family["name"], "owner": family["owner"], "stage": stage,
            "anchor_ply": anchor_at, "anchor_source": family["anchor_source"],
            "offer_captured": captured if authored else None, "captured_anchor_piece": captured,
            "offer_withdrawn": withdrawn, "owner_inference": family["owner_inference"],
            "matching_families": sorted(active),
            "native_support_families": sorted({row["family_id"] for row in researched}),
            "has_catalog_continuation": catalog_continuation,
            "has_native_continuation": bool(researched),
            "has_continuation": catalog_continuation,
            "has_any_continuation": catalog_continuation or bool(researched)}


def proposals(board, seed, *, limit=4, decisions=()):
    """Sourced, legal, deduplicated candidate moves; never an automatic book move."""
    checked = _history(board)
    data, index, manifest, error = _load()
    if checked is None or error or type(limit) is not int or not 1 <= limit <= MAX_PROPOSALS:
        return []
    history, keys = checked
    owner = "white" if board.turn else "black"
    active = _active_families(data, history, keys, decisions)
    own_active = {key for key in active if data["families"][key]["owner"] == owner}
    legal = {move.uci() for move in board.legal_moves}
    matches = defaultdict(list)
    for row, offset, uci in index.get(keys[-1], []):
        if row["owner"] != owner or uci not in legal:
            continue
        if own_active and row["family_id"] not in own_active:
            continue
        # A transposition is accepted only after an actual defining family
        # position was reached, or on the exact source preparation path.
        exact = row["moves"][:offset] == history
        if not exact and row["family_id"] not in own_active:
            continue
        matches[uci].append((row, offset, exact))
    def rank(value):
        return _hash([POLICY, str(seed), keys[-1], value])
    result = []
    for uci in sorted(matches, key=lambda value: (rank(value), value))[:limit]:
        row, offset, exact = min(matches[uci], key=lambda value: (
            value[0]["family_id"] not in own_active, not value[2], len(value[0]["moves"]),
            rank(value[0]["line_id"])))
        after_anchor = row["family_id"] in own_active
        result.append({"uci": uci, "line_id": row["line_id"], "name": row["name"],
            "family_id": row["family_id"], "family_name": row["family_name"], "owner": owner,
            "role": "knowledge_continuation" if after_anchor else "knowledge_preparation",
            "stage": row["stage"] if after_anchor else "preparation", "transposition": not exact,
            "source_url": row["source_url"], "source_license": row["source_license"],
            "catalog_commit": row["catalog_commit"], "catalog_sha256": manifest["catalog_sha256"],
            "source_prefix_uci": row["moves"][:offset], "source_next_uci": uci,
            "policy": POLICY, "candidate_only": True, "loss_cap_cp": 35})
    return result


def runtime_proposals(board, seed, *, limit=4, decisions=()):
    """Own-search support plus catalog proposals for play, leaving source builds stable."""
    checked = _history(board)
    data, _index, _manifest, error = _load()
    if checked is None or error or type(limit) is not int or not 1 <= limit <= MAX_PROPOSALS:
        return []
    history, keys = checked
    active = _active_families(data, history, keys, decisions)
    owner = "white" if board.turn else "black"
    own_active = {key for key in active if data["families"][key]["owner"] == owner}
    result = proposals(board, seed, limit=limit, decisions=decisions)
    # Exact, deeper own-search continuations get a review opportunity before
    # seeded catalog diversity. Their stored scores never authorize a move.
    researched = native_support.proposals(board, active_families=own_active, seed=seed, limit=limit)
    merged, seen = [], set()
    for proposal in [*researched, *result]:
        if proposal["uci"] not in seen:
            seen.add(proposal["uci"])
            merged.append(proposal)
    return merged[:limit]


def valid_proposal(board, proposal):
    """Validate against every source edge, not a seeded representative list.

    Different nested family commitments may choose different source rows for
    the same legal move. A row's provenance remains valid when another call
    deduplicates that move under a different family; the full graph is the
    authority. Model-added ranking fields do not authorize an absent edge.
    """
    checked = _history(board)
    data, index, manifest, error = _load()
    if checked is None or error or not isinstance(proposal, dict):
        return False
    if proposal.get("source") == native_support.SOURCE:
        history, keys = checked
        active = _active_families(data, history, keys, ())
        return native_support.valid_proposal(board, proposal, active_families=active)
    if (proposal.get("catalog_sha256") != manifest.get("catalog_sha256")
            or proposal.get("policy") != POLICY or proposal.get("candidate_only") is not True
            or proposal.get("loss_cap_cp") != 35):
        return False
    history, keys = checked
    owner = "white" if board.turn else "black"
    active = _active_families(data, history, keys, ())
    legal = {move.uci() for move in board.legal_moves}
    for source, offset, uci in index.get(keys[-1], []):
        if (source["line_id"] != proposal.get("line_id") or uci != proposal.get("uci")
                or uci not in legal or source["owner"] != owner):
            continue
        exact = source["moves"][:offset] == history
        completed = source["family_id"] in active
        if not exact and not completed:
            continue
        if any(proposal.get(field) != source[field] for field in (
                "family_id", "family_name", "name", "owner", "source_url", "source_license", "catalog_commit")):
            continue
        if (proposal.get("source_prefix_uci") != source["moves"][:offset]
                or proposal.get("source_next_uci") != uci or proposal.get("transposition") != (not exact)
                or proposal.get("role") != ("knowledge_continuation" if completed else "knowledge_preparation")
                or proposal.get("stage") != (source["stage"] if completed else "preparation")):
            continue
        return True
    return False


if __name__ == "__main__":
    print(json.dumps(build_catalog(), ensure_ascii=False, indent=2))
