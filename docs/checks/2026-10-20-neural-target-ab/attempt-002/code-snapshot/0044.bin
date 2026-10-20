"""Validate and freeze evaluator-blind openings for the first precision-model match.

Only legal moves, terminal status, and prior board-input identities are read to
accept a position. Scores, model predictions, search, and game results never
participate in selection. The exact hand-authored lines are declared below.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess

import chess


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "docs/matches/2026-10-19-neural-strength"
DEFAULT_OPENINGS = ROOT / "tests/data/neural_strength_openings_2026-10-19.json"
DEFAULT_AUDIT = EXPERIMENT / "openings-audit.json"
DATASETS = (
    "docs/checks/2026-10-19-neural-training/first-run/data/train.jsonl",
    "docs/checks/2026-10-19-neural-training/first-run/data/validation.jsonl",
    "docs/checks/2026-10-19-neural-qat/holdout/holdout.jsonl",
    "docs/checks/2026-10-19-neural-precision/holdout/holdout.jsonl",
)
DECLARED_LINES = (
    ("Neural strength Italian quiet center",
     "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3 f8c5 c2c3 d7d6 e1g1 e8g8 a2a4 a7a6"),
    ("Neural strength Alapin bishop retreat",
     "e2e4 c7c5 c2c3 g8f6 e4e5 f6d5 d2d4 c5d4 g1f3 b8c6 c3d4 d7d6 f1c4 d5b6 c4b5"),
    ("Neural strength queen pawn bishop retreat",
     "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6 c1g5 f8e7 e2e3 e8g8 g1f3 h7h6 g5h4 b7b6"),
    ("Neural strength king fianchetto closed center",
     "d2d4 g8f6 c2c4 g7g6 b1c3 f8g7 e2e4 d7d6 g1f3 e8g8 f1e2 e7e5 e1g1 b8c6 d4d5 c6e7 f3e1"),
    ("Neural strength Caro bishop and h-pawns",
     "e2e4 c7c6 d2d4 d7d5 b1c3 d5e4 c3e4 c8f5 e4g3 f5g6 h2h4 h7h6 g1f3 b8d7"),
    ("Neural strength French knight retreat",
     "e2e4 e7e6 d2d4 d7d5 b1d2 g8f6 e4e5 f6d7 f1d3 c7c5 c2c3 b8c6 g1e2"),
    ("Neural strength English castled fianchetto",
     "c2c4 e7e5 b1c3 g8f6 g2g3 d7d5 c4d5 f6d5 f1g2 d5b6 g1f3 b8c6 e1g1 f8e7"),
    ("Neural strength Reti delayed center",
     "g1f3 d7d5 g2g3 g8f6 f1g2 e7e6 e1g1 f8e7 d2d3 e8g8 b1d2 c7c5 e2e4"),
)
UCI_MOVE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?\Z")
FEN_FIELDS = frozenset(("fen", "opening_fen", "fen_after_opening", "initial_fen",
                        "start_fen", "final_fen", "board_fen"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def feature_identity(board):
    """Same pieces-only input identity as neural_training.feature_identity.

    Ignore turn, castling, en passant, and clocks, which the evaluator cannot read.
    Color reversal and rank reflection exchange the model's two input views.
    """
    canonical = min(board.board_fen(), board.mirror().board_fen())
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def collect_prior_positions(output):
    prior, sources, issues = set(), [], []
    totals = Counter()
    paths = set((ROOT / "tests/data").glob("*opening*.json"))
    paths.update(path for path in (ROOT / "docs/matches").rglob("*.json")
                 if not path.is_relative_to(EXPERIMENT))
    paths.discard(Path(output).resolve())

    def add(board, counts):
        prior.add(feature_identity(board))
        counts["position_occurrences"] += 1

    for path in sorted(paths):
        counts = Counter()
        data = json.loads(path.read_text(encoding="utf-8-sig"))

        def replay(moves, start, location, kind):
            board = chess.Board(start)
            counts[kind] += 1
            add(board, counts)
            for ply, text in enumerate(moves, 1):
                move = chess.Move.from_uci(text)
                if move not in board.legal_moves:
                    issues.append({"path": relative(path), "location": location,
                                   "error": "illegal recorded UCI move", "ply": ply,
                                   "move": text})
                    counts["partially_replayed_lines"] += 1
                    return
                board.push(move)
                add(board, counts)

        def visit(value, location="$", opening_context=False):
            if isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{location}[{index}]", opening_context)
            elif isinstance(value, dict):
                for key in FEN_FIELDS:
                    text = value.get(key)
                    if isinstance(text, str):
                        try:
                            board = chess.Board(text)
                        except ValueError:
                            counts["non_fen_string_fields"] += 1
                        else:
                            counts["fen_fields"] += 1
                            add(board, counts)
                start = value.get("initial_fen", chess.STARTING_FEN)
                try:
                    chess.Board(start)
                except (TypeError, ValueError):
                    start = chess.STARTING_FEN
                for key in ("uci_moves", "opening_uci_moves", "moves"):
                    moves = value.get(key)
                    if (isinstance(moves, list)
                            and all(isinstance(text, str) and UCI_MOVE.fullmatch(text)
                                    for text in moves)
                            and (key != "moves" or opening_context)):
                        kind = ("game_lines_replayed" if key == "uci_moves"
                                else "opening_lines_replayed")
                        replay(moves, start, f"{location}.{key}", kind)
                for key, child in value.items():
                    visit(child, f"{location}.{key}", opening_context or key == "openings")

        visit(data)
        totals.update(counts)
        sources.append({"path": relative(path), "sha256": sha256(path),
                        "kind": "prior opening definitions or match JSON",
                        "counts": dict(counts)})

    for name in DATASETS:
        path, counts = ROOT / name, Counter()
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            board = chess.Board(row["fen"])
            identity = feature_identity(board)
            if identity != row["feature_hash"]:
                raise ValueError(f"Prior dataset feature identity mismatch: {name}")
            prior.add(identity)
            counts["rows"] += 1
        sources.append({"path": name, "sha256": sha256(path),
                        "kind": "prior training or heldout inputs; labels ignored",
                        "counts": dict(counts)})
        totals["prior_dataset_rows"] += counts["rows"]
    return prior, sources, totals, issues


def generate(output, audit_output):
    output, audit_output = Path(output).resolve(), Path(audit_output).resolve()
    if output.exists() or audit_output.exists():
        raise ValueError("Refusing to overwrite frozen openings or audit")
    prior, sources, totals, issues = collect_prior_positions(output)
    openings, seen = [], set()
    for name, line in DECLARED_LINES:
        board, moves, sans = chess.Board(), [], []
        for text in line.split():
            move = chess.Move.from_uci(text)
            if board.outcome(claim_draw=True) is not None or move not in board.legal_moves:
                raise ValueError(f"Declared opening has illegal/post-terminal move: {name}: {text}")
            sans.append(board.san(move))
            moves.append(move.uci())
            board.push(move)
        identity = feature_identity(board)
        reason = ("terminal" if board.outcome(claim_draw=True)
                  else "prior board input" if identity in prior
                  else "within-set duplicate input" if identity in seen else None)
        if reason:
            raise ValueError(f"Declared opening needs prior-to-match amendment: {name}: {reason}")
        if not board.is_valid():
            raise ValueError("Generated board failed independent rule-library validation")
        seen.add(identity)
        openings.append({"name": name,
                         "moves": moves, "san_moves": sans,
                         "fen_after_opening": board.fen(en_passant="fen"),
                         "feature_hash": identity, "plies": len(moves)})
    document = {
        "schema_version": 1,
        "seed": None,
        "generator": relative(__file__),
        "generator_sha256": sha256(__file__),
        "provenance": ("Eight short opening lines hand-authored for this experiment before any "
                       "model or engine evaluation, search, or game result on them. No external "
                       "games or opening book was used. Names describe structures, not optimal play. "
                       "The listed moves are fixed, so no random seed applies."),
        "selection_policy": ("Use exactly all eight declared legal ongoing final positions, which "
                             "must not share the pieces-only input or its color/rank mirror with "
                             "the prior sources listed in openings-audit.json or another new final "
                             "position. Freeze all eight before any match and use each with both "
                             "engine colors at 100 ms "
                             "and 1000 ms per move. Never replace or retune from match results."),
        "scope": ("A small developer-selected sample of eight familiar opening structures, "
                  "with four White-to-move and four Black-to-move positions. It is not a random "
                  "sample of all chess openings or a general Elo rating population."),
        "openings": openings,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    audit_output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    audit = {
        "schema_version": 1,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                         capture_output=True, text=True, check=True).stdout.strip(),
        "generator": {"path": relative(__file__), "sha256": sha256(__file__)},
        "opening_file": {"path": relative(output), "sha256": sha256(output)},
        "seed": None, "accepted_positions": len(openings),
        "declared_lines_amended_after_evaluation": False,
        "design_note": ("Before any engine/model evaluation, search, or game on a new position, "
                        "a seed-20261027 random legal 8-12 ply preview was inspected only as SAN "
                        "moves and FENs. Early king moves and a queen capture made its distribution "
                        "unsuitable for the intended first normal-opening comparison. The preview "
                        "was discarded before freezing and before match evidence existed. The "
                        "researcher then declared the eight hand-authored lines in the generator; "
                        "no preview position is used. This change was structural, with no scores "
                        "or game outcomes observed. The initially declared king-fianchetto line "
                        "ending at d4d5 overlapped a recorded SEE-replication game position, so "
                        "c6e7 f3e1 was appended before freezing, solely for input novelty. The "
                        "other seven lines were preserved."),
        "side_to_move_counts": dict(Counter("white" if chess.Board(item["fen_after_opening"]).turn
                                           else "black" for item in openings)),
        "prior_sources": sources, "prior_counts": dict(totals),
        "prior_unique_piece_inputs_including_color_rank_mirrors": len(prior),
        "prior_identity_set_sha256": hashlib.sha256("\n".join(sorted(prior)).encode("ascii")).hexdigest(),
        "prior_record_replay_issues": issues,
        "new_vs_prior_feature_overlap": len(seen & prior),
        "within_new_feature_duplicates": len(openings) - len(seen),
        "all_new_legal_and_nonterminal": True,
        "source_scope": ("All prior docs/matches/**/*.json outside this new experiment subtree, "
                         "plus tests/data/*opening*.json. Every recognized FEN field and each "
                         "replayable uci_moves game record, opening_uci_moves record, and moves "
                         "list under openings is included, so recorded prior game trajectories "
                         "are checked as well as starts. Training1498 + validation390 + QAT1905 "
                         "+ precision1893 piece-input rows are checked. Unrecorded positions, "
                         "PGN-only files, and raw unsampled training-game positions are not scanned."),
        "selection_fields": ["hand-authored opening structure before evaluation",
                             "legal moves", "ongoing game", "piece-input novelty"],
        "scores_models_search_and_game_results_used_for_selection": False,
    }
    with audit_output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(audit, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OPENINGS)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args()
    audit = generate(args.output, args.audit_output)
    print(json.dumps({key: audit[key] for key in (
        "opening_file", "seed", "accepted_positions", "side_to_move_counts",
        "prior_counts", "prior_unique_piece_inputs_including_color_rank_mirrors",
        "new_vs_prior_feature_overlap", "within_new_feature_duplicates",
        "prior_record_replay_issues")}, indent=2))


if __name__ == "__main__":
    main()
