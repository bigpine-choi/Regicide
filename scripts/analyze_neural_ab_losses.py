"""Replay the frozen A/B match without launching engines or changing old evidence.

Material is only a mechanical 100/320/330/500/900 proxy.  Neither material
deficits nor either player's search score identify objectively bad moves.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

import chess
import chess.pgn
import numpy as np

import neural_precision as precision

VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(value, sort_keys=True, indent=2) + "\n")


def jsonl(path, rows):
    with Path(path).open("x", encoding="utf-8", newline="\n") as out:
        for row in rows:
            out.write(json.dumps(row, sort_keys=True) + "\n")


def material(board, color=chess.WHITE):
    return sum(VALUES[p.piece_type] * (1 if p.color == color else -1)
               for p in board.piece_map().values())


def phase(board):
    n = len(board.piece_map())
    return "<=12" if n <= 12 else "13-24" if n <= 24 else "25-32"


def predictor(model, board):
    # Same exact integer formula as quantized_white, without validating the
    # unchanged arrays for each of the thousands of positions.
    views = precision.base.feature_indices(board)
    activations = [np.clip(model["hidden_bias"] + model["input_weights"][view].sum(axis=0),
                           0, model["clip"]) for view in views]
    numerator = int((activations[0] - activations[1]) @ model["output_weights"])
    value = abs(numerator) // model["output_scale"] * (1 if numerator >= 0 else -1)
    return max(-28000, min(28000, value))


def mating_moves(board):
    mates = []
    for move in list(board.legal_moves):
        board.push(move)
        if board.is_checkmate():
            mates.append(move.uci())
        board.pop()
    return mates


def median(values):
    return statistics.median(values) if values else None


def summarize_moves(rows):
    cp = [r["search_white_cp"] * (1 if r["color"] == "white" else -1)
          for r in rows if r["search_white_cp"] is not None]
    margins = [r["own_minus_other_white_cp"] * (1 if r["color"] == "white" else -1)
               for r in rows]
    return {"moves": len(rows), "median_depth": median([r["depth"] for r in rows]),
            "median_nodes": median([r["nodes"] for r in rows]),
            "search_mover_cp_median": median(cp),
            "own_minus_other_mover_cp_median": median(margins),
            "mating_reply_created": sum(bool(r["opponent_mate_in_one"]) for r in rows),
            "minor_plus_unrecaptured_next_reply": sum(r["minor_plus_unrecaptured_next_reply"] for r in rows),
            "phase_counts": dict(Counter(r["phase"] for r in rows)),
            "search_score_note": "Recorded own search scores are on different reached positions; not an objective accuracy comparison."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--match-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise ValueError("Refusing to overwrite existing analysis artifacts")
    source = json.loads((args.match_dir / "match.json").read_text())
    pgns = []
    with (args.match_dir / "match.pgn").open(encoding="utf-8") as handle:
        while (game := chess.pgn.read_game(handle)) is not None:
            pgns.append(game)
    assert len(source["games"]) == len(pgns) == 96
    receipts = []
    pending = {}
    for line in (args.match_dir / "uci.jsonl").open(encoding="utf-8"):
        entry = json.loads(line)
        role, text = entry["role"], entry["line"]
        if entry["direction"] == "send" and text.startswith("position "):
            pending[role] = {"position_seq": entry["seq"], "position_command": text}
        elif entry["direction"] == "send" and text.startswith("go "):
            assert role in pending
            pending[role].update(go_seq=entry["seq"], go_command=text)
        elif entry["direction"] == "recv" and text.startswith("bestmove "):
            row = pending.pop(role)
            row.update(bestmove_seq=entry["seq"], bestmove=text.split()[1], role=role)
            receipts.append(row)
    assert len(receipts) == sum(len(g["search_moves"]) for g in source["games"])
    receipt_index = 0
    analyses, selections, replay_cache = [], [], []
    for game, pgn in zip(source["games"], pgns, strict=True):
        assert not pgn.errors
        assert [m.uci() for m in pgn.mainline_moves()] == game["uci_moves"]
        assert pgn.headers["Result"] == game["result"]
        board = chess.Board(game["initial_fen"])
        states, sans = [board.copy(stack=True)], []
        for uci in game["uci_moves"]:
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves
            sans.append(board.san(move))
            board.push(move)
            states.append(board.copy(stack=True))
        assert board.fen(en_passant="fen") == game["final_fen"]
        outcome = board.outcome(claim_draw=True)
        assert outcome and outcome.result() == game["result"]
        search_receipts = {}
        for step in game["search_moves"]:
            receipt = receipts[receipt_index]
            receipt_index += 1
            assert receipt["role"] == step["role"] and receipt["bestmove"] == step["move"]
            assert game["uci_seq_before"] < receipt["go_seq"] < receipt["bestmove_seq"] <= game["uci_seq_after"]
            assert receipt["position_command"].split(" moves ")[-1].split() == game["uci_moves"][:step["ply"]-1]
            assert states[step["ply"]-1].fen(en_passant="fen") == step["fen_before"]
            search_receipts[step["ply"]] = receipt
        loser = None if game["result"] == "1/2-1/2" else game["result"] == "0-1"
        loser_role = None if loser is None else game["white"] if loser else game["black"]
        loser_arm = None if loser_role is None else source["role_mapping"][loser_role]
        steps = {r["ply"]: r for r in game["search_moves"]}
        loser_steps = [] if loser is None else [p for p in steps if states[p-1].turn == loser]
        deficits = [] if loser is None else [material(b, loser) for b in states]
        sustained = None
        if loser is not None:
            for index in range(len(game["opening_uci_moves"])+1, len(states)-3):
                if all(v <= -300 for v in deficits[index:index+4]):
                    candidates = [p for p in loser_steps if p <= index]
                    if candidates:
                        sustained = {"first_state_ply": index, "decision_ply": max(candidates)}
                        break
        mate_pivots = []
        if loser is not None:
            for ply in loser_steps:
                mates = mating_moves(states[ply])
                if mates:
                    mate_pivots.append({"ply": ply, "opponent_mates": mates})
        if loser is not None:
            if sustained:
                pivot, reason = sustained["decision_ply"], "first_four_halfmove_material_deficit_le_minus300"
            elif mate_pivots:
                pivot, reason = mate_pivots[0]["ply"], "first_legal_mate_in_one_for_opponent_after_loser_move"
            else:
                # Include the opponent response so a capture following a quiet
                # losing-side move can contribute to the two-halfmove swing.
                pivot = min(loser_steps, key=lambda p: (deficits[min(p+1, len(states)-1)]-deficits[p-1], p))
                reason = "largest_next_two_halfmove_material_drop_earliest_tie"
            step = steps[pivot]
            before, after = states[pivot-1], states[pivot]
            selection = {"query_id": f"game-{game['game']:04d}-loss-pivot", "game": game["game"],
                         "losing_arm": loser_arm, "loser_color": "white" if loser else "black",
                         "seed": game["seed"], "family_id": game["family_id"],
                         "selection_reason": reason, "ply": pivot,
                         "initial_fen": game["initial_fen"], "moves_before": game["uci_moves"][:pivot-1],
                         "fen_before": before.fen(en_passant="fen"), "played_uci": step["move"], "played_san": sans[pivot-1],
                         "fen_after": after.fen(en_passant="fen"), "material_before": material(before, loser),
                         "material_after": material(after, loser), "phase": phase(before),
                         "model_A": game["models"]["baseline"], "model_B": game["models"]["candidate"],
                         "original_search": step, "uci_range": search_receipts[pivot],
                         "continuation_san_next8": sans[pivot-1:pivot+7],
                         "continuation_uci_next8": game["uci_moves"][pivot-1:pivot+7]}
            selections.append(selection)
        analysis = {"game": game["game"], "seed": game["seed"], "family_id": game["family_id"],
                    "result": game["result"], "termination": game["termination"],
                    "plies": game["plies"], "played_plies": len(game["search_moves"]),
                    "losing_arm": loser_arm, "loser_color": None if loser is None else "white" if loser else "black",
                    "first_sustained_deficit": sustained,
                    "mate_in_one_after_loser_moves": mate_pivots,
                    "max_material_deficit_loser": None if loser is None else min(deficits),
                    "selected_query_id": None if loser is None else selections[-1]["query_id"],
                    "material_loser_by_ply": deficits,
                    "san": sans}
        analyses.append(analysis)
        replay_cache.append((game, states, sans, search_receipts))
    assert len(selections) == 76 and Counter(r["losing_arm"] for r in selections) == {"A": 27, "B": 49}
    jsonl(args.output / "selected-positions.jsonl", selections)
    manifest = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                "selection_frozen_before_new_engine_analysis": True,
                "policy": ["Every decisive game, without selecting only representative losses",
                           "First material deficit <=-300 for four consecutive halfmove states; choose the last losing-side decision no later than deficit start",
                           "If absent, first losing-side move leaving opponent a legal mate in one",
                           "Otherwise earliest maximum material drop over that losing move plus opponent reply"],
                "material_proxy": {chess.piece_name(k): v for k, v in VALUES.items()},
                "limitations": "Material and mate-in-one availability are mechanical indicators, not proven blunders. Existing games are exploratory evidence, not a fresh test set.",
                "selected_counts": dict(Counter(r["losing_arm"] for r in selections)),
                "source_files": {name: {"path": str((args.match_dir/name).resolve()), "sha256": digest(args.match_dir/name)}
                                 for name in ("match.json", "match.pgn", "uci.jsonl")},
                "script_sha256": digest(__file__),
                "selection_sha256": digest(args.output / "selected-positions.jsonl"),
                "python_chess_version": chess.__version__, "replayed_games": len(analyses),
                "raw_search_receipts_linked": receipt_index}
    write(args.output / "selection-manifest.json", manifest)
    print(json.dumps({"selection_complete": 76, "selection_sha256": manifest["selection_sha256"]}), flush=True)
    models = {}
    for game in source["games"]:
        for role, descriptor in game["models"].items():
            key = (game["seed"], source["role_mapping"][role])
            if key not in models:
                assert digest(descriptor["path"]) == descriptor["sha256"]
                models[key] = precision.load_model(descriptor["path"])
    moves = []
    for game, states, sans, search_receipts in replay_cache:
        loser_arm = analyses[game["game"]-1]["losing_arm"]
        for step in game["search_moves"]:
            ply = step["ply"]
            before, after = states[ply-1], states[ply]
            arm = source["role_mapping"][step["role"]]
            preds = {a: predictor(models[(game["seed"], a)], before) for a in "AB"}
            after_preds = {a: predictor(models[(game["seed"], a)], after) for a in "AB"}
            score = step["score"]
            cp = score["cp"]
            reply = chess.Move.from_uci(game["uci_moves"][ply]) if ply < game["plies"] else None
            capture_square = reply.to_square if reply is not None else None
            captured = after.piece_at(capture_square) if capture_square is not None else None
            if reply is not None and after.is_en_passant(reply):
                captured = chess.Piece(chess.PAWN, before.turn)
            immediate_recapture = (ply+1 < game["plies"] and game["uci_moves"][ply+1][2:4] == reply.uci()[2:4]
                                   and states[ply+1].is_capture(chess.Move.from_uci(game["uci_moves"][ply+1]))) if reply else False
            moves.append({"game": game["game"], "seed": game["seed"], "family_id": game["family_id"],
                          "ply": ply, "arm": arm, "role": step["role"], "color": step["color"],
                          "eventual_loss": arm == loser_arm, "fen_before": before.fen(en_passant="fen"),
                          "move": step["move"], "san": sans[ply-1], "phase": phase(before),
                          "material_white_cp": material(before), "material_after_white_cp": material(after),
                          "predictions_white_cp": preds, "after_predictions_white_cp": after_preds,
                          "own_minus_other_white_cp": preds[arm]-preds["B" if arm == "A" else "A"],
                          "search_white_cp": None if cp is None else cp * (1 if before.turn else -1),
                          "recorded_search_mate_mover": score["mate"], "depth": step["depth"], "nodes": step["nodes"],
                          "opponent_mate_in_one": mating_moves(after),
                          "minor_plus_unrecaptured_next_reply": bool(captured and VALUES[captured.piece_type] >= 300 and not immediate_recapture),
                          "captured_next_reply_piece": None if captured is None else captured.symbol(),
                          "uci_range": search_receipts[ply]})
    grouped = {}
    for arm in "AB":
        own = [r for r in moves if r["arm"] == arm]
        losses = [r for r in analyses if r["losing_arm"] == arm]
        chosen = [r for r in selections if r["losing_arm"] == arm]
        grouped[arm] = {"all_moves": summarize_moves(own),
                        "loss_moves": summarize_moves([r for r in own if r["eventual_loss"]]),
                        "loss_games": len(losses), "loss_plies_median": median([r["plies"] for r in losses]),
                        "loss_plies_range": [min(r["plies"] for r in losses), max(r["plies"] for r in losses)],
                        "losses_with_sustained_material_deficit": sum(r["first_sustained_deficit"] is not None for r in losses),
                        "losses_ever_leaving_mate_in_one": sum(bool(r["mate_in_one_after_loser_moves"]) for r in losses),
                        "selection_reason_counts": dict(Counter(r["selection_reason"] for r in chosen)),
                        "selection_phase_counts": dict(Counter(r["phase"] for r in chosen)),
                        "selected_ply_median": median([r["ply"] for r in chosen]),
                        "losses_by_seed": dict(Counter(r["seed"] for r in losses)),
                        "losses_by_color": dict(Counter(r["loser_color"] for r in losses))}
    jsonl(args.output / "move-predictions.jsonl", moves)
    jsonl(args.output / "games.jsonl", analyses)
    write(args.output / "summary.json", {"schema_version": 1,
          "games": len(analyses), "decisive_games": len(selections), "search_moves": len(moves),
          "termination_counts": dict(Counter(r["termination"] for r in analyses)),
          "by_arm": grouped, "selection_manifest": manifest,
          "interpretation": "All groups describe reached game positions. Search scores, material drops, and uncaptured replies alone are not objective move-quality or cause estimates. No engine was launched."})
    print(json.dumps({"complete": True, "search_moves": len(moves), "by_arm": grouped}), flush=True)


if __name__ == "__main__":
    main()
