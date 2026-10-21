"""Freeze hand-authored, evaluator-blind openings for the 3 s / 5 s match.

Selection reads only move legality, terminal status, and pieces-only input
novelty. Existing scores, search choices, and game results are never consulted.
Prior recorded histories and principal variations are replayed only as a set of
already exposed board inputs. This script never starts an engine.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime
import hashlib
import json
from pathlib import Path
import subprocess

import chess

import generate_neural_strength_openings as previous


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "docs/matches/2026-10-19-neural-long-time"
DEFAULT_OPENINGS = ROOT / "tests/data/neural_long_time_openings_2026-10-19.json"
DEFAULT_AUDIT = EXPERIMENT / "openings-audit.json"
DECLARED_LINES = (
    ("Long time Spanish closed retreat",
     "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7 f1e1 b7b5 a4b3 d7d6 c2c3 e8g8 h2h3 c6b8"),
    ("Long time Sicilian central tension",
     "e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 e7e6 c1e3 a7a6 f2f3 b7b5 d1d2 b8d7 g2g4"),
    ("Long time queen gambit paired bishops",
     "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6 g1f3 f8e7 c1g5 e8g8 e2e3 h7h6 g5h4 b7b6 f1d3 c8b7"),
    ("Long time Nimzo doubled c-pawns",
     "d2d4 g8f6 c2c4 e7e6 b1c3 f8b4 e2e3 e8g8 f1d3 d7d5 g1f3 c7c5 e1g1 b8c6 a2a3 b4c3 b2c3"),
    ("Long time Caro advance central exchange",
     "e2e4 c7c6 d2d4 d7d5 e4e5 c8f5 g1f3 e7e6 f1e2 c6c5 e1g1 b8c6 c1e3 c5d4 f3d4 g8e7"),
    ("Long time French Winawer development",
     "e2e4 e7e6 d2d4 d7d5 b1c3 f8b4 e4e5 c7c5 a2a3 b4c3 b2c3 g8e7 d1g4 e8g8 f1d3 b8c6 g1f3"),
    ("Long time English open b-file",
     "c2c4 e7e5 b1c3 g8f6 g1f3 b8c6 g2g3 f8b4 f1g2 e8g8 e1g1 b4c3 b2c3 d7d6 d2d3 h7h6 a1b1 a8b8"),
    ("Long time Reti bishop pressure",
     "g1f3 d7d5 g2g3 c7c6 f1g2 c8g4 e1g1 b8d7 d2d3 e7e5 b1d2 g8f6 e2e4 f8c5 d1e1 e8g8 h2h3"),
    ("Long time Catalan central expansion",
     "d2d4 g8f6 c2c4 e7e6 g2g3 d7d5 f1g2 f8e7 g1f3 e8g8 e1g1 b8d7 d1c2 c7c6 b1d2 b7b6 e2e4 c8b7"),
    ("Long time Scotch bishop regrouping",
     "e2e4 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 f8c5 c1e3 d8f6 c2c3 g8e7 f1c4 e8g8 e1g1 c6e5 c4e2"),
    ("Long time Scandinavian central development",
     "e2e4 d7d5 e4d5 d8d5 b1c3 d5a5 d2d4 g8f6 g1f3 c7c6 f1c4 c8f5 e1g1 e7e6 f1e1 b8d7"),
    ("Long time Slav queen development",
     "d2d4 d7d5 c2c4 c7c6 g1f3 g8f6 b1c3 d5c4 a2a4 c8f5 e2e3 e7e6 f1c4 f8b4 e1g1 b8d7 d1e2"),
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def collect_prior_exposure(output):
    """Reuse prior opening/data collector and supplement recorded PV exposure."""
    previous.EXPERIMENT = EXPERIMENT
    prior, sources, totals, issues = previous.collect_prior_positions(output)
    supplemental_counts = Counter()
    supplemental_sources = []
    paths = set(path for path in (ROOT / "docs/matches").rglob("*.json")
                if not path.is_relative_to(EXPERIMENT))
    paths.update((ROOT / "docs/analysis").rglob("*.json"))
    # Query records contain a root/history and the final reported PV. UCI wire
    # logs contain duplicate/intermediate info lines and are outside this scope.
    paths.update((ROOT / "docs/analysis").rglob("queries.jsonl"))

    def add(board, counts):
        prior.add(previous.feature_identity(board))
        counts["position_occurrences"] += 1

    for path in sorted(paths):
        counts = Counter()

        def replay(moves, fen, location, kind):
            board = chess.Board(fen)
            add(board, counts)
            counts[kind] += 1
            for index, text in enumerate(moves, 1):
                move = chess.Move.from_uci(text)
                if move not in board.legal_moves:
                    issues.append({"path": relative(path), "location": location,
                                   "error": "illegal supplemental recorded UCI move",
                                   "ply": index, "move": text})
                    counts["partially_replayed_lines"] += 1
                    return
                board.push(move)
                add(board, counts)
                counts["replayed_plies"] += 1

        def valid_moves(value):
            return (isinstance(value, list) and value
                    and all(isinstance(text, str) and previous.UCI_MOVE.fullmatch(text)
                            for text in value))

        def visit(value, location):
            if isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{location}[{index}]")
            elif isinstance(value, dict):
                for key, text in value.items():
                    if "fen" in key.lower() and isinstance(text, str):
                        try:
                            board = chess.Board(text)
                        except ValueError:
                            continue
                        add(board, counts)
                        counts["fen_fields"] += 1
                fen = value.get("fen_before", value.get("fen"))
                if isinstance(fen, str):
                    for key in ("pv", "pv_uci"):
                        if valid_moves(value.get(key)):
                            replay(value[key], fen, f"{location}.{key}",
                                   "pv_lines_replayed")
                if valid_moves(value.get("history")):
                    replay(value["history"], value.get("initial_fen", chess.STARTING_FEN),
                           f"{location}.history", "analysis_histories_replayed")
                for key, child in value.items():
                    visit(child, f"{location}.{key}")

        if path.suffix == ".jsonl":
            for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                visit(json.loads(line), f"line {index}")
        else:
            visit(json.loads(path.read_text(encoding="utf-8-sig")), "$")
        supplemental_counts.update(counts)
        supplemental_sources.append({"path": relative(path), "sha256": sha256(path),
                                     "kind": "recorded FEN fields and final PV/history inputs; scores ignored",
                                     "counts": dict(counts)})
    return prior, sources, totals, supplemental_sources, supplemental_counts, issues


def generate(output, audit_output):
    output, audit_output = Path(output).resolve(), Path(audit_output).resolve()
    if output.exists() or audit_output.exists():
        raise ValueError("Refusing to overwrite frozen openings or audit")
    (prior, sources, totals, extra_sources, extra_totals,
     issues) = collect_prior_exposure(output)
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
        identity = previous.feature_identity(board)
        reason = ("terminal" if board.outcome(claim_draw=True)
                  else "prior board input" if identity in prior
                  else "within-set duplicate input" if identity in seen else None)
        if reason:
            raise ValueError(f"Declared opening needs pre-match amendment: {name}: {reason}")
        if not board.is_valid():
            raise ValueError(f"Invalid opening board: {name}")
        seen.add(identity)
        openings.append({"name": name, "moves": moves, "san_moves": sans,
                         "fen_after_opening": board.fen(en_passant="fen"),
                         "feature_hash": identity, "plies": len(moves)})
    document = {
        "schema_version": 1,
        "seed": None,
        "generator": relative(__file__),
        "generator_sha256": sha256(__file__),
        "provenance": ("Twelve familiar opening structures hand-authored before any evaluation, "
                       "search, or game on these final starts. No external opening book or games "
                       "are used. Names describe structures and do not certify best play. Only "
                       "legality, nonterminal status, and prior pieces-only input exposure are "
                       "used for acceptance; there is no random draw or seed."),
        "selection_policy": ("The twelve declared positions are a fixed candidate pool. Use only "
                             "the fixed first two positions, with both engine colors, for the "
                             "primary 3000 ms control (4 games). Use only the fixed first position, "
                             "with both engine colors, for the secondary 5000 ms control (2 games). "
                             "The user reduced the time budget before any new evaluation, search, "
                             "or game; the existing position pool and order are preserved. Both "
                             "sets are frozen before the first new search or game and never "
                             "selected from results."),
        "scope": ("A developer-selected sample of twelve opening structures, six White-to-move "
                  "and six Black-to-move. The primary first two cover Spanish and Sicilian "
                  "structures with one White-to-move and one Black-to-move position. The "
                  "secondary first position is Spanish and White-to-move. Only these six games "
                  "are planned; the remaining pool is unused. The small fixed samples do not "
                  "define an Elo population."),
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
        "seed": None,
        "accepted_positions": len(openings),
        "primary_opening_indices_one_based": [1, 2],
        "secondary_opening_indices_one_based": [1],
        "planned_game_counts": {"primary_3000_ms": 4, "secondary_5000_ms": 2},
        "side_to_move_counts": dict(Counter("white" if chess.Board(item["fen_after_opening"]).turn
                                           else "black" for item in openings)),
        "primary_side_to_move_counts": dict(Counter("white" if chess.Board(item["fen_after_opening"]).turn
                                                   else "black" for item in openings[:2])),
        "secondary_side_to_move_counts": dict(Counter("white" if chess.Board(item["fen_after_opening"]).turn
                                                     else "black" for item in openings[:1])),
        "prior_sources": sources,
        "prior_counts": dict(totals),
        "supplemental_prior_sources": extra_sources,
        "supplemental_prior_counts": dict(extra_totals),
        "prior_unique_piece_inputs_including_color_rank_mirrors": len(prior),
        "prior_identity_set_sha256": hashlib.sha256("\n".join(sorted(prior)).encode("ascii")).hexdigest(),
        "prior_record_replay_issues": issues,
        "new_vs_prior_feature_overlap": len(seen & prior),
        "within_new_feature_duplicates": len(openings) - len(seen),
        "all_new_legal_and_nonterminal": True,
        "declared_lines_amended_after_evaluation": False,
        "pre_match_amendments": [
            {"opening": "Long time Caro advance central exchange",
             "stage": "authoring, before any generator execution or engine query",
             "change": "Corrected c7c5 to c6c5 at ply 10; the c-pawn had already moved to c6.",
             "reason": "move-origin transcription error, corrected solely for legality"},
            {"stage": "before freezing and before any new model evaluation, search, or game",
             "change": "Reduced primary 12 pairs to the fixed first 2; secondary 4 pairs to the fixed first 1. All twelve positions and prior exposure inventory remain unchanged.",
             "reason": "user requested a shorter run; preparation and matches have a 55-minute cap"}
        ],
        "source_scope": ("Previous collector: all prior docs/matches/**/*.json outside this "
                         "experiment, tests/data/*opening*.json, and the four sampled "
                         "training/validation/QAT/precision JSONL datasets. Supplemental scan: "
                         "all prior match JSON FEN fields and recorded final PVs, all prior "
                         "docs/analysis/**/*.json FEN fields, and docs/analysis/**/queries.jsonl "
                         "recorded query histories and final PVs. Final positions are compared "
                         "by pieces-only identity including color/rank mirror. Intermediate UCI "
                         "info-line PVs, PGN-only trajectories, and unsampled original training "
                         "positions are outside this scan."),
        "selection_fields": ["hand-authored opening structures before evaluation", "legal moves",
                             "ongoing game", "pieces-only input novelty"],
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
        "secondary_side_to_move_counts", "prior_counts", "supplemental_prior_counts",
        "prior_unique_piece_inputs_including_color_rank_mirrors", "new_vs_prior_feature_overlap",
        "within_new_feature_duplicates", "prior_record_replay_issues")}, indent=2))


if __name__ == "__main__":
    main()
