"""Collect independent, self-authored endgame families with reserved-input exclusion.

This is a narrow wrapper around collect_learning_data: games, exact raw teacher
scores, outcomes, PGNs and the v1 dataset format use that existing implementation.
No tablebase, external game, external engine or synthetic result supplies labels.
Constructed starting positions measure a declared endgame distribution, not the
frequency of those positions in normal opening-to-end self-play.
"""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import random

import chess

import collect_learning_data as DATA

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEEDS = ROOT / "tests/data/endgame_learning_seeds_v1.json"


def load_seeds(path, split="all"):
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if document.get("schema") != "regicide.endgame.seeds.v1":
        raise ValueError("unsupported endgame seed schema")
    families = document.get("families")
    if not isinstance(families, list) or not families:
        raise ValueError("nonempty endgame families required")
    identifiers, state_splits, feature_splits, openings = set(), {}, {}, []
    for family in families:
        identifier = family.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("family IDs must be nonempty and unique")
        identifiers.add(identifier)
        assigned = family.get("split")
        if assigned not in DATA.SPLITS:
            raise ValueError("family requires a predefined split")
        if not isinstance(family.get("theme"), str) or not family["theme"]:
            raise ValueError("family requires a theme")
        starts = family.get("starts")
        if not isinstance(starts, list) or len(starts) < 2:
            raise ValueError("each family requires at least two independently authored starts")
        turns = set()
        for index, fen in enumerate(starts, 1):
            board = DATA.replay_position(fen, [])
            if board.outcome(claim_draw=True) is not None or board.is_check():
                raise ValueError("endgame seeds must be nonterminal and outside check")
            if len(board.piece_map()) > 12:
                raise ValueError("endgame seeds must contain at most twelve pieces")
            turns.add(board.turn)
            for key, mapping in ((DATA.canonical_position_key(board), state_splits),
                                 (DATA.neural_feature_key(board), feature_splits)):
                prior = mapping.setdefault(key, assigned)
                if prior != assigned:
                    raise ValueError("endgame seed canonical or neural input crosses splits")
            if split in ("all", assigned) or (split == "heldout" and assigned != "train"):
                openings.append({"id": f"{identifier}-{index:02d}",
                                 "family": f"endgame-v1-{identifier}", "split": assigned,
                                 "name": f"{family['theme']} / authored start {index}",
                                 "initial_fen": fen, "moves": []})
        if turns != {chess.WHITE, chess.BLACK}:
            raise ValueError("each endgame family must include both actual starting turns")
    if not openings:
        raise ValueError("selected split has no endgame starts")
    return document, {"schema": "regicide.learning.openings.v1",
                       "provenance": document.get("provenance"),
                       "split_policy": "preassigned constructed families; no family or start reassignment from outcomes",
                       "openings": openings}


class ExclusionInventory:
    """Input membership only: never use reserved moves, scores or results as labels."""

    def __init__(self):
        self.canonical, self.features, self.sources = set(), set(), []

    def add_board(self, board):
        self.canonical.add(DATA.canonical_position_key(board))
        self.features.add(DATA.neural_feature_key(board))

    def add_history(self, initial_fen, history):
        board = DATA.replay_position(initial_fen, [])
        self.add_board(board)
        if not isinstance(history, list):
            raise ValueError("exclusion history must be a list")
        # Arena histories sometimes include the final draw-claim move. Verify
        # actual legality; do not invent a continuation beyond checkmate.
        for text in history:
            move = chess.Move.from_uci(text)
            if move not in board.legal_moves or board.is_game_over(claim_draw=False):
                raise ValueError("illegal exclusion game history")
            board.push(move)
            self.add_board(board)

    def _source(self, path, kind, **metadata):
        path = Path(path).resolve(strict=True)
        self.sources.append({"path": str(path), "sha256": DATA.sha256_file(path),
                             "kind": kind, **metadata})
        return path

    def add_games(self, path, splits=None):
        path = self._source(path, "complete_trajectory_inventory",
                            splits=sorted(splits) if splits else "all")
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            document = json.loads(text)
            rows = document.get("games") if isinstance(document, dict) else document
            if not isinstance(rows, list):
                raise ValueError("JSON exclusion inventory requires a games list")
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        for row in rows:
            if splits is None or row.get("split") in splits:
                history = row.get("history", row.get("uci_moves", row.get("moves")))
                self.add_history(row.get("initial_fen", chess.STARTING_FEN), history)

    def add_dataset(self, path, heldout_only=False):
        path = Path(path).resolve(strict=True)
        if path.is_dir():
            path = path / "manifest.json"
        path = self._source(path, "dataset_manifest", heldout_only=heldout_only)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema") != DATA.SCHEMA:
            raise ValueError("exclusion dataset must use the original self-play schema")
        if manifest.get("status", "complete") not in ("complete", "completed"):
            raise ValueError("exclusion dataset collection has not completed")
        relative = Path(manifest["games_path"])
        target = (path.parent / relative).resolve(strict=True)
        if relative.is_absolute() or not target.is_relative_to(path.parent):
            raise ValueError("exclusion dataset path escapes its manifest directory")
        expected = manifest.get("files_sha256", {}).get(relative.as_posix())
        if expected is not None and DATA.sha256_file(target) != expected:
            raise ValueError("exclusion games SHA256 mismatch")
        self.add_games(target, {"validation", "test"} if heldout_only else None)

    def add_openings(self, path):
        path = self._source(path, "reserved_opening_input_inventory")
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") == "regicide.learning.arena.openings.v1":
            seeds, prefixes = document.get("seeds"), document.get("prefixes")
            if not isinstance(seeds, list) or not seeds or not isinstance(prefixes, list) or not prefixes:
                raise ValueError("reserved arena seed/prefix inventory missing")
            for prefix in prefixes:
                self.add_history(chess.STARTING_FEN, prefix)
            # Reconstruct the entire declared pool, not only a random sample.
            for seed in seeds:
                if isinstance(seed, bool) or not isinstance(seed, int):
                    raise ValueError("reserved arena seeds must be integers")
                generator = random.Random(seed)
                moves = list(generator.choice(prefixes))
                board = DATA.replay_position(chess.STARTING_FEN, moves)
                while len(moves) < 12:
                    if board.outcome(claim_draw=True) is not None:
                        raise ValueError("reserved arena opening becomes terminal")
                    move = generator.choice(sorted(board.legal_moves, key=lambda item: item.uci()))
                    board.push(move)
                    moves.append(move.uci())
                self.add_history(chess.STARTING_FEN, moves)
        elif document.get("schema") == "regicide.learning.openings.v1":
            for opening in document.get("openings", []):
                self.add_history(opening.get("initial_fen", chess.STARTING_FEN), opening["moves"])
        else:
            raise ValueError("unsupported exclusion opening inventory")

    def reason(self, board):
        if DATA.canonical_position_key(board) in self.canonical:
            return "reserved_canonical_input"
        if DATA.neural_feature_key(board) in self.features:
            return "reserved_neural_input"
        return None

    def verify_sources(self):
        for source in self.sources:
            if DATA.sha256_file(source["path"]) != source["sha256"]:
                raise ValueError("reserved exclusion source changed during collection")


def trajectory_leakage(games, positions):
    """Exclude sample collisions with any opposite-split generated trajectory."""
    boards, features = defaultdict(set), defaultdict(set)
    for game in games:
        board = DATA.replay_position(game["initial_fen"], [])
        for ply in range(len(game["history"]) + 1):
            boards[DATA.canonical_position_key(board)].add(game["split"])
            features[DATA.neural_feature_key(board)].add(game["split"])
            if ply < len(game["history"]):
                board.push_uci(game["history"][ply])
    crossed_boards = {key for key, splits in boards.items() if len(splits) > 1}
    crossed_features = {key for key, splits in features.items() if len(splits) > 1}
    accepted, excluded = [], Counter()
    for row in positions:
        board = DATA.replay_position(row["initial_fen"], row["history"])
        if DATA.canonical_position_key(board) in crossed_boards:
            excluded["canonical"] += 1
        elif DATA.neural_feature_key(board) in crossed_features:
            excluded["neural_input"] += 1
        else:
            accepted.append(row)
    return accepted, {"crossed_canonical_trajectory_keys": len(crossed_boards),
                      "crossed_neural_trajectory_keys": len(crossed_features),
                      "excluded_samples": dict(excluded),
                      "policy": "exclude every sample intersecting any opposite-split complete trajectory; never change family splits"}


def collect(args):
    seed_hash = DATA.sha256_file(args.seeds)
    seeds, openings = load_seeds(args.seeds, args.split)
    if DATA.sha256_file(args.seeds) != seed_hash:
        raise ValueError("endgame seed definition changed during validation")
    inventory = ExclusionInventory()
    inventory.add_openings(ROOT / "tests/data/learning_arena_openings_v1.json")
    for path in args.exclude_dataset:
        inventory.add_dataset(path)
    for path in args.exclude_heldout_dataset:
        inventory.add_dataset(path, heldout_only=True)
    for path in args.exclude_games:
        inventory.add_games(path)
    for path in args.exclude_openings:
        inventory.add_openings(path)
    # Reserve this directory through the base collector, preserving its refusal
    # to overwrite an existing run. Generated opening file lives beside it.
    output = args.output.resolve()
    opening_path = output.with_name(output.name + ".endgame-openings.json")
    if output.exists() or opening_path.exists():
        raise FileExistsError(output if output.exists() else opening_path)
    opening_path.parent.mkdir(parents=True, exist_ok=True)
    with opening_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(openings, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    args.openings = opening_path
    manifest = DATA.collect(args, position_exclusion=inventory.reason)
    try:
        inventory.verify_sources()
        if DATA.sha256_file(args.seeds) != seed_hash:
            raise ValueError("endgame seed definition changed during collection")
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["errors"].append({"type": type(exc).__name__, "message": str(exc),
                                   "stage": "endgame_reserved_inventory_verification"})
        DATA.checkpoint_json(output / "manifest.json", manifest)
        raise
    positions_path, games_path = output / "positions.jsonl", output / "games.jsonl"
    positions = [json.loads(line) for line in positions_path.read_text(encoding="utf-8").splitlines() if line]
    games = [json.loads(line) for line in games_path.read_text(encoding="utf-8").splitlines() if line]
    positions, leakage = trajectory_leakage(games, positions)
    DATA.write_jsonl(positions_path, positions)
    manifest.update({"positions": len(positions),
                     "positions_by_split": dict(Counter(row["split"] for row in positions)),
                     "positions_by_turn": dict(Counter(row["turn"] for row in positions)),
                     "counts_by_turn": {split: {turn: sum(row["split"] == split and row["turn"] == turn
                                                              for row in positions) for turn in ("w", "b")}
                                        for split in DATA.SPLITS},
                     "endgame": {"schema": "regicide.endgame.collection.v1",
                                 "seed_definition": seeds, "seed_file_sha256": seed_hash,
                                 "wrapper_sha256": DATA.sha256_file(__file__),
                                 "selected_split": args.split,
                                 "reserved_input_inventory": {"sources": inventory.sources,
                                     "canonical_keys": len(inventory.canonical), "neural_keys": len(inventory.features),
                                     "policy": "membership exclusion before teacher sampling; no reserved score, result or move is a training target"},
                                 "full_trajectory_split_exclusion": leakage,
                                 "scope": "independent constructed starting families; actual legal self-play with actual rule outcomes"}})
    manifest["files_sha256"]["positions.jsonl"] = DATA.sha256_file(positions_path)
    DATA.checkpoint_json(output / "manifest.json", manifest)
    return manifest


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("binary", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    for name in ("games-per-opening", "nodes", "max-plies", "sample-every", "teacher-nodes"):
        parser.add_argument(f"--{name}", type=DATA.positive_int, required=True)
    parser.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--split", choices=("all", "heldout", *DATA.SPLITS), default="train")
    parser.add_argument("--source-commit")
    parser.add_argument("--model", type=Path)
    for name in ("exclude-dataset", "exclude-heldout-dataset", "exclude-games", "exclude-openings"):
        parser.add_argument(f"--{name}", type=Path, action="append", default=[])
    return parser.parse_args(argv)


def main(argv=None):
    result = collect(parse_args(argv))
    print(f"complete: {result['completed_games']} actual completed games, {result['positions']} eligible positions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
