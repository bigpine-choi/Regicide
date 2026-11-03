"""Reproducible accepted-sacrifice exercises, independent of engine answers.

Catalog continuations retain CC0 provenance. Explicit authored continuations
are legal exercises, never claimed best defense or historical master games.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import chess
import gambit_knowledge

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data/gambits/operations_curriculum.json"
SCHEMA = "regicide.gambit-operations-curriculum.v1"
POLICY = "regicide-gambit-operations-curriculum-v1"
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}

# Explicit offering identities avoid treating a named catalog endpoint as proof
# of a sacrifice.  Scotch's Bc4 also distinguishes the gambit from main-line
# Scotch positions that share d4/exd4.
FAMILIES = (
    ("evans", "white", "train", 7, 8, "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4", 8),
    ("kings", "white", "train", 3, 4, "e2e4 e7e5 f2f4 e5f4", 4),
    ("danish", "white", "train", 5, 6, "e2e4 e7e5 d2d4 e5d4 c2c3 d4c3", 6),
    ("scotch", "white", "train", 5, 6, "e2e4 e7e5 g1f3 b8c6 d2d4 e5d4 f1c4", 7),
    ("smith-morra", "white", "train", 5, 6, "e2e4 c7c5 d2d4 c5d4 c2c3 d4c3", 6),
    ("blackmar-diemer", "white", "train", 7, 8, "d2d4 d7d5 e2e4 d5e4 b1c3 g8f6 f2f3 e4f3", 8),
    ("stafford", "black", "train", 6, 7, "e2e4 e7e5 g1f3 g8f6 f3e5 b8c6 e5c6", 7),
    ("albin", "black", "train", 4, 5, "d2d4 d7d5 c2c4 e7e5 d4e5", 5),
    ("budapest", "black", "validation", 4, 5, "d2d4 g8f6 c2c4 e7e5 d4e5", 5),
    ("vienna", "white", "validation", 5, 6, "e2e4 e7e5 b1c3 g8f6 f2f4 e5f4", 6),
    ("benko", "black", "holdout", 6, 7, "d2d4 g8f6 c2c4 c7c5 d4d5 b7b5 c4b5", 7),
    ("from", "black", "holdout", 2, 3, "f2f4 e7e5 f4e5", 3),
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def position_key(board):
    return " ".join(board.fen(en_passant="legal").split()[:4])


def replay(moves):
    board = chess.Board()
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("Illegal curriculum move: " + uci)
        board.push(move)
    return board


def balance(board, owner):
    color = owner == "white"
    return sum(value * (len(board.pieces(kind, color)) - len(board.pieces(kind, not color)))
               for kind, value in VALUES.items())


def proof(moves, owner, offer_ply, capture_ply):
    if not 0 < offer_ply < capture_ply <= len(moves):
        raise ValueError("Offer/capture order is invalid")
    before = replay(moves[:offer_ply - 1])
    move = chess.Move.from_uci(moves[offer_ply - 1])
    piece = before.piece_at(move.from_square)
    if piece is None or piece.color != (owner == "white"):
        raise ValueError("The offer was not made by its owner")
    after = before.copy(stack=True)
    after.push(move)
    square = move.to_square
    for ply in range(offer_ply + 1, capture_ply):
        next_move = chess.Move.from_uci(moves[ply - 1])
        if next_move.from_square == square or (next_move.to_square == square and after.is_capture(next_move)):
            raise ValueError("Original offering piece moved or was captured before recorded acceptance")
        after.push(next_move)
    capture = chess.Move.from_uci(moves[capture_ply - 1])
    victim = after.piece_at(square)
    if (after.turn == piece.color or capture.to_square != square or not after.is_capture(capture)
            or victim != piece):
        raise ValueError("The original offered piece was not actually captured")
    accepted = after.copy(stack=True)
    accepted.push(capture)
    return ({"ply": offer_ply, "uci": move.uci(), "before_fen": before.fen(en_passant="fen"),
             "after_fen": replay(moves[:offer_ply]).fen(en_passant="fen"),
             "offered_piece": piece.symbol(), "offered_square": chess.square_name(square),
             "offered_value_cp": VALUES[piece.piece_type]},
            {"capture_ply": capture_ply, "capture_uci": capture.uci(),
             "before_fen": after.fen(en_passant="fen"), "after_fen": accepted.fen(en_passant="fen"),
             "victim_piece": victim.symbol(), "victim_square": chess.square_name(square),
             "victim_value_cp": VALUES[victim.piece_type], "actual_capture": True})


def _eligible(board, owner):
    return (board.is_valid() and board.turn == (owner == "white") and balance(board, owner) < 0
            and not board.is_game_over(claim_draw=True) and not board.is_repetition(2)
            and not board.is_check() and board.legal_moves.count() > 1)


def _source(row, length, manifest):
    return {"catalog_line_id": row["line_id"], "catalog_name": row["name"],
            "url": row["source_url"], "license": "CC0-1.0", "catalog_commit": row["catalog_commit"],
            "catalog_sha256": manifest["catalog_sha256"], "source_prefix_plies": length,
            "source_moves": row["moves"], "source_owner_inference": row.get("anchor_source")}


def _topics(board, owner, source_name, kind):
    color = owner == "white"
    result = ["development", "attack_maintenance"]
    if balance(board, owner) <= -300 or any(board.is_capture(move) for move in board.legal_moves):
        result.append("material_recovery")
    king = board.king(color)
    ring = chess.SquareSet(chess.BB_KING_ATTACKS[king] | chess.BB_SQUARES[king])
    pressure = sum(len(board.attackers(not color, square)) for square in ring)
    if pressure >= 2 or "Defense" in source_name or kind == "project_authored":
        result.append("defensive_transition")
    return result, pressure


def _case(config, moves, row, manifest, graph_edges, *, kind="catalog_prefix", source_length=None, note=""):
    family, owner, split, offer_ply, capture_ply, core, confirmation = config
    board = replay(moves)
    if not _eligible(board, owner):
        return None
    signature, acceptance = proof(moves, owner, offer_ply, capture_ply)
    key = position_key(board)
    topics, pressure = _topics(board, owner, row["name"], kind)
    source_edge = bool(graph_edges.get((key, owner)))
    known = bool(gambit_knowledge.proposals(board, "operations-curriculum", limit=8))
    return {"case_id": family + "-" + digest([family, moves])[:12], "family_id": family,
            "split": split, "owner": owner, "root_fen": chess.STARTING_FEN,
            "moves": list(moves), "history_uci": list(moves), "fen": board.fen(en_passant="fen"),
            "position_key": key, "root_ply": len(moves), "material_balance_cp": balance(board, owner),
            "material_debt_cp": -balance(board, owner), "material_units": "pawn=100; material only, not evaluation",
            "signature": signature, "acceptance": acceptance, "family_confirmation_ply": confirmation,
            "recognition_prefix_uci": moves[:max(capture_ply, confirmation)], "goal_tags": topics,
            "goal_status": "practice topics, not prescribed correct moves", "opponent_ring_pressure": pressure,
            "source": _source(row, source_length if source_length is not None else len(moves), manifest),
            "variation": {"kind": kind, "notes": note,
                          "response_status": "unverified exercise response; not claimed best defense"},
            "book_status": "known_continuation" if known else "catalog_leaf_or_authored_off_book",
            "has_catalog_continuation": known, "source_graph_edge_exists": source_edge}


def _authored_after_leaf(config, row, manifest, graph_edges):
    """Add a plainly labeled off-book exercise without consulting any engine."""
    moves = list(row["moves"])
    board = replay(moves)
    owner = config[1]
    # A catalog line ending on the owner's move needs only a hypothetical reply;
    # otherwise make an explicit quiet owner/reply pair to reach a new root.
    needed = 1 if board.turn != (owner == "white") else 2
    def ordered(position):
        return sorted((move for move in position.legal_moves if not position.is_capture(move)
                       and not position.gives_check(move)),
                      key=lambda move: digest([config[0], moves, move.uci()]))
    def visit(position, path, left):
        if left == 0:
            if _eligible(position, owner) and not graph_edges.get((position_key(position), owner)):
                return path
            return None
        for move in ordered(position):
            position.push(move)
            result = visit(position, path + [move.uci()], left - 1)
            position.pop()
            if result is not None:
                return result
        return None
    extra = visit(board, [], needed)
    if extra is None:
        return None
    return _case(config, moves + extra, row, manifest, graph_edges, kind="project_authored",
                 source_length=len(moves), note="Catalog endpoint plus deterministic legal quiet exercise moves; no best-defense claim")


def construct():
    catalog_raw = (ROOT / "data/gambits/catalog.json").read_bytes()
    manifest = json.loads((ROOT / "data/gambits/manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(catalog_raw).hexdigest() != manifest["catalog_sha256"]:
        raise ValueError("Source graph checksum differs")
    catalog = json.loads(catalog_raw)
    if catalog["source"]["license"] != "CC0-1.0":
        raise ValueError("Unexpected catalog license")
    graph_edges = {}
    for row in catalog["lines"]:
        for index, move in enumerate(row["moves"]):
            graph_edges.setdefault((row["position_keys"][index], row["owner"]), set()).add(move)
    cases, families = [], {}
    for config in FAMILIES:
        family, owner, split, offer_ply, capture_ply, prefix, confirmation = config
        core = prefix.split()
        rows = [row for row in catalog["lines"] if row["moves"][:len(core)] == core]
        selected = []
        available = {}
        for row in sorted(rows, key=lambda value: (len(value["moves"]), value["line_id"])):
            for offset in range(len(core), len(row["moves"]) + 1):
                case = _case(config, row["moves"][:offset], row, manifest, graph_edges)
                if case:
                    available.setdefault(case["position_key"], case)
        if available:
            # Keep first recovery, a developed position, and an actual endpoint.
            choices = sorted(available.values(), key=lambda value: (value["root_ply"], value["case_id"]))
            selected.append(choices[0])
            latest = max(choices, key=lambda value: (value["root_ply"], value["case_id"]))
            if latest["position_key"] != selected[0]["position_key"]:
                selected.append(latest)
            for candidate in sorted(choices, key=lambda value: (value["source_graph_edge_exists"], digest(value["case_id"]))):
                if candidate["position_key"] not in {row["position_key"] for row in selected}:
                    selected.append(candidate)
                if len(selected) >= 3:
                    break
            for row in sorted(rows, key=lambda value: (-len(value["moves"]), value["line_id"])):
                authored = _authored_after_leaf(config, row, manifest, graph_edges)
                if authored and authored["position_key"] not in {value["position_key"] for value in selected}:
                    selected.append(authored)
                    break
        if family == "vienna":
            # The Nf6/exf4 acceptance is a project-authored response to the
            # sourced signature; the catalog does not print this continuation.
            source_moves = core[:-1]
            source_rows = [row for row in catalog["lines"] if row["moves"][:len(source_moves)] == source_moves]
            if not source_rows:
                raise ValueError("Vienna signature source is missing")
            source = min(source_rows, key=lambda value: (len(value["moves"]), value["line_id"]))
            tails = [[], ["g1f3", "d7d5"], ["f1c4", "d7d5"], ["f1c4", "d7d5", "c4b5", "c7c6"]]
            selected = [_case(config, core + tail, source, manifest, graph_edges,
                              kind="project_authored", source_length=len(source_moves),
                              note="Authored accepted Vienna response and central-pressure exercise; legality only, not best defense")
                        for tail in tails]
            selected = [case for case in selected if case is not None]
        if len(selected) < 3:
            raise ValueError("Not enough accepted-sacrifice cases: " + family)
        cases.extend(selected[:4])
        families[family] = {"family_id": family, "owner": owner, "split": split,
                            "offer_ply": offer_ply, "capture_ply": capture_ply,
                            "recognition_prefix_uci": core[:max(capture_ply, confirmation)],
                            "family_confirmation_ply": confirmation, "actual_acceptance_required": True}
    # Whole families are fixed before any engine answer. Also check every
    # possible immediate child: neighboring transpositions must not leak.
    claimed = {}
    for case in cases:
        board = replay(case["moves"])
        keys = {position_key(board)}
        for move in list(board.legal_moves):
            board.push(move)
            keys.add(position_key(board))
            board.pop()
        for key in keys:
            if key in claimed and claimed[key] != case["split"]:
                raise ValueError("Cross-split root/child transposition")
            claimed[key] = case["split"]
    source = copy.deepcopy(catalog["source"])
    source.update(catalog_sha256=manifest["catalog_sha256"],
                  builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    value = {"schema": SCHEMA, "policy": POLICY, "source": source,
             "split_policy": {"unit": "entire family including all root/immediate-child positions",
                              "holdout_families": ["benko", "from"], "validation_families": ["budapest", "vienna"],
                              "cross_split_root_or_child_intersections": 0,
                              "previous_experiment_note": "Benko/From appeared in the previous separate opening-ranking experiment; they are withheld throughout this new operations experiment."},
             "families": families, "cases": sorted(cases, key=lambda case: (case["split"], case["family_id"], case["case_id"])),
             "counts": {"families": len(families), "cases": len(cases),
                        "by_split": {split: sum(case["split"] == split for case in cases) for split in ("train", "validation", "holdout")},
                        "by_owner": {owner: sum(case["owner"] == owner for case in cases) for owner in ("white", "black")},
                        "off_book_roots": sum(not case["has_catalog_continuation"] for case in cases),
                        "authored_cases": sum(case["variation"]["kind"] == "project_authored" for case in cases)},
             "claims": {"accepted_piece_capture_verified": True, "positive_starting_material_debt": True,
                        "all_owner_to_move": True, "best_defense_verified": False,
                        "goal_tags_are_correct_move_labels": False}}
    value["curriculum_sha256"] = digest(value)
    return value


def build(output=DEFAULT_OUTPUT):
    value = construct()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return value


if __name__ == "__main__":
    result = build()
    print(json.dumps({"curriculum_sha256": result["curriculum_sha256"], "counts": result["counts"]}, ensure_ascii=False))
