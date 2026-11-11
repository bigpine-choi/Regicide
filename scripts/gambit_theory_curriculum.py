"""Finite, reproducible opening-theory roots for native Regicide research.

The pinned CC0 opening names/scores are candidate sources, not verified best
moves, accepted-sacrifice proofs or learned chess knowledge.  Structural stage
names deliberately make no claim that an offered piece was actually captured.
"""
from __future__ import annotations

from collections import defaultdict
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile

import chess

import gambit_knowledge

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = ROOT / "data/openings"
SCHEMA = "regicide.gambit-theory-curriculum.v1"
POLICY = "regicide-gambit-theory-curriculum-v1"
PINNED_COMMIT = "a6189a30dc273ccb21fc2536a9a2fefd5592a67a"
MAX_ROOTS_PER_FAMILY = 128

# These are project-selected family identities and defining-move boundaries.
# In particular, the Benko boundary is ...b5, before any possible acceptance.
FAMILIES = {
    "evans": {"name": "Evans Gambit", "owner": "white", "split": "train",
              "pattern": r"\bEvans Gambit\b",
              "anchor": "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4"},
    "smith-morra": {"name": "Smith-Morra Gambit", "owner": "white", "split": "train",
                    "pattern": r"\bSmith-Morra Gambit\b",
                    "anchor": "e2e4 c7c5 d2d4 c5d4 c2c3"},
    "albin": {"name": "Albin Countergambit", "owner": "black", "split": "validation",
              "pattern": r"\bAlbin Countergambit\b",
              "anchor": "d2d4 d7d5 c2c4 e7e5"},
    "benko": {"name": "Benko Gambit", "owner": "black", "split": "holdout",
              "pattern": r"\bBenko Gambit\b",
              "anchor": "d2d4 g8f6 c2c4 c7c5 d4d5 b7b5"},
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _replay(moves):
    board = chess.Board()
    for uci in moves:
        if not isinstance(uci, str):
            raise ValueError("Theory source moves must be UCI strings")
        try:
            move = chess.Move.from_uci(uci)
        except (TypeError, ValueError) as error:
            raise ValueError("Malformed theory source move: " + str(uci)) from error
        if move not in board.legal_moves:
            raise ValueError("Illegal theory source move: " + uci)
        board.push(move)
    return board


def _load_catalog():
    """Verify and rebuild in scratch without altering the runtime graph."""
    try:
        upstream = json.loads((SOURCE_DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
        if (not isinstance(upstream, dict) or upstream.get("commit") != PINNED_COMMIT or
                upstream.get("license") != "CC0-1.0" or
                upstream.get("repository_url") != "https://github.com/lichess-org/chess-openings"):
            raise ValueError("Theory source is not the pinned CC0 catalog")
        license_bytes = (SOURCE_DIRECTORY / "LICENSE").read_bytes()
        if hashlib.sha256(license_bytes).hexdigest() != upstream["files"]["LICENSE"]["sha256"]:
            raise ValueError("Theory source license checksum differs")
        with tempfile.TemporaryDirectory(prefix="regicide-theory-catalog-") as directory:
            manifest = gambit_knowledge.build_catalog(directory, source_directory=SOURCE_DIRECTORY)
            catalog = json.loads((Path(directory) / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Theory source catalog is unavailable or malformed") from error
    if (catalog.get("schema") != gambit_knowledge.SCHEMA or
            catalog.get("source") != manifest.get("source") or
            not isinstance(catalog.get("lines"), list)):
        raise ValueError("Theory source graph identity is malformed")
    return catalog, manifest


def _checked_line(row, source):
    if (not isinstance(row, dict) or not isinstance(row.get("line_id"), str) or
            not row["line_id"] or not isinstance(row.get("name"), str) or
            not isinstance(row.get("moves"), list) or not row["moves"] or
            len(row["moves"]) > gambit_knowledge.MAX_HISTORY_PLIES or
            row.get("source_license") != source["license"] or
            row.get("catalog_commit") != source["commit"] or
            not isinstance(row.get("source_url"), str) or
            row.get("catalog_file") not in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv") or
            row.get("catalog_file") not in source["files"]):
        raise ValueError("Malformed theory source line")
    filename = row["catalog_file"]
    expected_url = ("https://raw.githubusercontent.com/lichess-org/chess-openings/"
                    + source["commit"] + "/" + filename)
    if row["source_url"] != expected_url:
        raise ValueError("Theory source URL does not match its pinned file")
    board, boards = chess.Board(), [chess.Board()]
    for uci in row["moves"]:
        try:
            move = chess.Move.from_uci(uci)
        except (TypeError, ValueError) as error:
            raise ValueError("Malformed theory source move") from error
        if move not in board.legal_moves:
            raise ValueError("Illegal theory source line")
        board.push(move)
        boards.append(board.copy(stack=True))
    keys = [gambit_knowledge.position_key(position) for position in boards]
    if row.get("position_keys") != keys:
        raise ValueError("Theory source position keys differ from its legal history")
    return boards


def _guard_keys(board):
    """Protect roots AND every legal immediate successor across splits."""
    keys = {gambit_knowledge.position_key(board)}
    for move in list(board.legal_moves):
        board.push(move)
        keys.add(gambit_knowledge.position_key(board))
        board.pop()
    return keys


def _ordered(cases, seed, family):
    # Round-robin structural stages/roles before imposing the family limit.
    # This keeps offer, defender response and later play represented when
    # those stages exist, without consulting an engine score.
    buckets = defaultdict(list)
    stage_order = {"offer": 0, "response": 1, "continuation": 2, "leaf": 3}
    for case in cases:
        buckets[(stage_order[case["stage"]], case["training_role"])].append(case)
    for bucket in buckets.values():
        bucket.sort(key=lambda case: (_digest([POLICY, seed, family,
                                               case["position_key"]]), case["case_id"]))
    result = []
    while any(buckets.values()):
        for bucket_id in sorted(buckets):
            if buckets[bucket_id]:
                result.append(buckets[bucket_id].pop(0))
    return result


def build_curriculum(*, seed=20261105, roots_per_family=8, families=None):
    """Build sourced roots with fixed family splits and one-ply leak guards.

    ``families`` is an optional sequence of the four supported family IDs.
    Corrupt/missing source data raises ValueError; partial output is not used.
    No file is written and no engine is queried by this function.
    """
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if type(roots_per_family) is not int or not 1 <= roots_per_family <= MAX_ROOTS_PER_FAMILY:
        raise ValueError("roots_per_family must be between 1 and 128")
    if families is None:
        selected = list(FAMILIES)
    else:
        if (not isinstance(families, (list, tuple)) or not families or
                any(not isinstance(family, str) or family not in FAMILIES for family in families) or
                len(set(families)) != len(families)):
            raise ValueError("families must be a nonempty sequence of distinct supported IDs")
        selected = [family for family in FAMILIES if family in families]
    catalog, manifest = _load_catalog()
    source = catalog.get("source")
    if (not isinstance(source, dict) or source.get("license") != "CC0-1.0" or
            source.get("commit") != PINNED_COMMIT or not isinstance(source.get("files"), dict)):
        raise ValueError("Theory source graph is not pinned")
    groups, excluded, line_counts = {}, [], dict.fromkeys(selected, 0)
    for row in catalog["lines"]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("Malformed theory source row")
        matched = [family for family in selected if re.search(FAMILIES[family]["pattern"], row["name"])]
        if not matched:
            continue
        boards = _checked_line(row, source)
        for family in matched:
            config = FAMILIES[family]
            anchor_key = gambit_knowledge.position_key(_replay(config["anchor"].split()))
            anchors = [index for index, board in enumerate(boards)
                       if gambit_knowledge.position_key(board) == anchor_key]
            if not anchors:
                excluded.append({"family_id": family, "source_line_id": row["line_id"],
                                 "reason": "family_anchor_not_reached"})
                continue
            anchor_at = anchors[0]
            if anchor_at < 1:
                raise ValueError("Theory family anchor cannot be the initial board")
            line_counts[family] += 1
            for offset in range(anchor_at - 1, len(boards)):
                board = boards[offset]
                if board.is_game_over(claim_draw=True):
                    excluded.append({"family_id": family, "source_line_id": row["line_id"],
                                     "history_uci": row["moves"][:offset], "reason": "terminal_root"})
                    continue
                key = gambit_knowledge.position_key(board)
                group = groups.setdefault((family, key), {"histories": [], "candidates": set(),
                                                          "sources": {}, "offsets": []})
                group["histories"].append(tuple(row["moves"][:offset]))
                group["offsets"].append((offset, anchor_at))
                if offset < len(row["moves"]):
                    group["candidates"].add(row["moves"][offset])
                group["sources"][row["line_id"]] = {
                    "line_id": row["line_id"], "name": row["name"],
                    "catalog_file": row["catalog_file"], "catalog_pgn": row.get("catalog_pgn"),
                    "source_url": row["source_url"], "license": row["source_license"],
                    "catalog_commit": row["catalog_commit"],
                    "catalog_label": row.get("stage"),
                }
    missing = [family for family in selected if not line_counts[family]]
    if missing:
        raise ValueError("Pinned source contains no anchored lines for: " + ", ".join(missing))

    by_family = defaultdict(list)
    for (family, key), group in sorted(groups.items()):
        config = FAMILIES[family]
        history = list(min(group["histories"], key=lambda moves: (len(moves), moves)))
        board = _replay(history)
        candidates = sorted(group["candidates"])
        if any(chess.Move.from_uci(uci) not in board.legal_moves for uci in candidates):
            raise ValueError("Merged theory edge is illegal at its root")
        stages = {"offer" if offset == anchor - 1 else "response" if offset == anchor else
                  "continuation" for offset, anchor in group["offsets"]}
        stage = ("offer" if "offer" in stages else "response" if "response" in stages else
                 "continuation" if candidates else "leaf")
        turn = "white" if board.turn else "black"
        successor_keys = {}
        for uci in candidates:
            board.push_uci(uci)
            successor_keys[uci] = gambit_knowledge.position_key(board)
            board.pop()
        details = [group["sources"][line_id] for line_id in sorted(group["sources"])]
        case = {"case_id": "theory-" + _digest([SCHEMA, family, key, history])[:20],
                "family_id": family, "family_name": config["name"], "split": config["split"],
                "owner": config["owner"], "turn": turn, "stage": stage,
                "training_role": "gambit_theory" if turn == config["owner"] else "defense_theory",
                "history_uci": history, "fen": board.fen(en_passant="legal"),
                "position_key": key, "book_candidates": candidates,
                "candidate_position_keys": successor_keys,
                "source_lines": details, "source_urls": sorted({item["source_url"] for item in details}),
                "split_guard_keys": sorted(_guard_keys(board)), "candidate_only": True}
        by_family[family].append(case)

    # Earlier training roots win a conflict. Every dropped cross-split root is
    # explicit; there is no relabeling or silent movement of test positions.
    occupied = defaultdict(set)
    cases = []
    for family in selected:
        count = 0
        for case in _ordered(by_family[family], seed, family):
            footprint = set(case["split_guard_keys"])
            collisions = {split: sorted(footprint & keys) for split, keys in occupied.items()
                          if split != case["split"] and footprint & keys}
            reason = "cross_split_position_overlap" if collisions else (
                "family_root_limit" if count >= roots_per_family else None)
            if reason:
                exclusion = {"case_id": case["case_id"], "family_id": family,
                             "split": case["split"], "position_key": case["position_key"], "reason": reason}
                if collisions:
                    exclusion["overlap_keys_by_split"] = collisions
                excluded.append(exclusion)
                continue
            cases.append(case)
            occupied[case["split"]].update(footprint)
            count += 1
    result = {"schema": SCHEMA, "policy": POLICY, "seed": seed,
              "roots_per_family": roots_per_family, "families": selected,
              "source": {**copy.deepcopy(source), "catalog_sha256": manifest["catalog_sha256"],
                         "license_url": "https://raw.githubusercontent.com/lichess-org/chess-openings/"
                                        + PINNED_COMMIT + "/COPYING.txt"},
              "candidate_only": True, "learned_model": False,
              "purpose": "Named opening-theory candidates for native analysis, not best-move or sacrifice-soundness labels",
              "stage_semantics": {
                  "offer": "root immediately before the project defining move",
                  "response": "root immediately after that move; no capture or acceptance is implied",
                  "continuation": "later sourced root with at least one catalog successor",
                  "leaf": "later sourced root without any catalog successor"},
              "split": {"assignment": {family: FAMILIES[family]["split"] for family in selected},
                        "unit": "whole_family", "conflict_policy": "earlier_family_wins_drop_conflicts",
                        "guard": "root_and_all_legal_immediate_successor_position_keys",
                        "guard_plies": 1, "longer_horizon_disjointness_claimed": False},
              "counts": {"cases": len(cases), "excluded": len(excluded),
                         "source_lines_by_family": line_counts,
                         "cases_by_family": {family: sum(case["family_id"] == family for case in cases)
                                             for family in selected}},
              "cases": cases, "excluded": excluded}
    result["curriculum_id"] = _digest(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261105)
    parser.add_argument("--roots-per-family", type=int, default=8)
    parser.add_argument("--families", nargs="+", choices=tuple(FAMILIES))
    args = parser.parse_args()
    result = build_curriculum(seed=args.seed, roots_per_family=args.roots_per_family, families=args.families)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    print(json.dumps({"curriculum_id": result["curriculum_id"], "counts": result["counts"]},
                     sort_keys=True))


if __name__ == "__main__":
    main()
