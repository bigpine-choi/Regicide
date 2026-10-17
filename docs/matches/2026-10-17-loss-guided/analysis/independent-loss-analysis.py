"""Material, move and reported-search trajectories; not independent engine scores."""
import csv
import hashlib
import json
from pathlib import Path

import chess

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "src/board.cpp").is_file())
SOURCE = ROOT / "docs/matches/2026-10-17-hash-memory/time100-retry.json"
OUT = Path(__file__).resolve().parent
VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
SELECTED = {1: [47, 49, 53, 55, 103, 107], 2: [76, 78, 94, 96, 98, 100],
            6: [76, 78, 94, 96, 98, 100], 7: [37, 39, 41, 43, 45, 47],
            10: [40, 42, 44, 46, 64, 68], 11: [55, 57, 59, 61, 63, 65, 91],
            13: [17, 19, 21, 25, 63, 85]}


def material(board, candidate):
    return sum(value * (len(board.pieces(piece, candidate)) - len(board.pieces(piece, not candidate)))
               for piece, value in VALUES.items())


def passed(board, color):
    result = []
    for square in board.pieces(chess.PAWN, color):
        file, rank = chess.square_file(square), chess.square_rank(square)
        obstructed = any(abs(chess.square_file(other) - file) <= 1 and
                         ((chess.square_rank(other) > rank) if color else (chess.square_rank(other) < rank))
                         for other in board.pieces(chess.PAWN, not color))
        if not obstructed:
            result.append(chess.square_name(square))
    return sorted(result)


data = json.loads(SOURCE.read_text(encoding="utf-8"))
assert data["completed"] and data["engines"]["candidate"]["engine_id"]["name"] == "Regicide 0.4.1"
losses = [game for game in data["games"] if game["candidate_points"] == 0]
assert [game["game"] for game in losses] == [1, 2, 6, 7, 10, 11, 13]
reports, positions = [], []
for game in losses:
    candidate = game["white"] == "candidate"
    board = chess.Board(game["initial_fen"])
    search = {item["ply"]: item for item in game["search_moves"]}
    rows, captures, promotions, own_drops = [], [], [], []
    last_own = None
    for ply, text in enumerate(game["uci_moves"], 1):
        move = chess.Move.from_uci(text)
        assert move in board.legal_moves
        fen = board.fen(en_passant="fen")
        candidate_move = board.turn == candidate
        san = board.san(move)
        victim = chess.PAWN if board.is_en_passant(move) else board.piece_type_at(move.to_square)
        piece = board.piece_type_at(move.from_square)
        before = material(board, candidate)
        item = search.get(ply)
        score = item["score"] if item else None
        cp = score["cp"] * (1 if candidate_move else -1) if score and score["cp"] is not None else None
        mate = score["mate"] * (1 if candidate_move else -1) if score and score["mate"] is not None else None
        row = {"ply": ply, "move_number": (ply + 1) // 2, "role": "candidate" if candidate_move else "baseline",
               "san": san, "uci": text, "fen_before": fen, "candidate_material_before": before,
               "candidate_pov_search_cp": cp, "candidate_pov_search_mate": mate,
               "reported_depth": item["depth"] if item else None,
               "reported_nodes": item["nodes"] if item else None,
               "in_check_before": board.is_check(), "captured_piece": chess.piece_name(victim) if victim else None,
               "gives_check": board.gives_check(move),
               "candidate_passed_pawns_before": passed(board, candidate),
               "opponent_passed_pawns_before": passed(board, not candidate)}
        if candidate_move and cp is not None:
            if last_own is not None and last_own["candidate_pov_search_cp"] - cp >= 120:
                own_drops.append({"previous_ply": last_own["ply"], "previous_cp": last_own["candidate_pov_search_cp"],
                                  "ply": ply, "cp": cp, "delta": cp - last_own["candidate_pov_search_cp"]})
            last_own = row
        if ply in SELECTED[game["game"]]:
            assert candidate_move
            positions.append({"game": game["game"], "ply": ply, "candidate_color": "white" if candidate else "black",
                              "actual_move": text, "actual_san": san, "fen_before": fen,
                              "recorded_search_cp_mover_pov": score["cp"] if score else None,
                              "recorded_search_depth": item["depth"] if item else None})
        board.push(move)
        row["candidate_material_after"] = material(board, candidate)
        row["material_delta"] = row["candidate_material_after"] - before
        row["fen_after"] = board.fen(en_passant="fen")
        if victim and not candidate_move:
            recaptures = [reply.uci() for reply in board.legal_moves
                          if reply.to_square == move.to_square and board.is_capture(reply)]
            captures.append({"ply": ply, "san": san, "captured": chess.piece_name(victim),
                             "material_delta": row["material_delta"], "balance_after": row["candidate_material_after"],
                             "legal_immediate_recaptures": recaptures,
                             "note": "An unrecapturable capture can still be a legitimate exchange or forced defense; it is not a proven blunder."})
        if move.promotion:
            promotions.append({"ply": ply, "san": san, "role": row["role"],
                               "opponent_passed_pawns_before": row["opponent_passed_pawns_before"]})
        rows.append(row)
    assert board.is_checkmate() and board.turn == candidate
    filename = OUT / f'independent-game{game["game"]:02d}-trajectory.csv'
    with filename.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    own = [row for row in rows if row["role"] == "candidate" and row["candidate_pov_search_cp"] is not None]
    first_bad = next((row for row in own if row["candidate_pov_search_cp"] <= -200), None)
    reports.append({"game": game["game"], "candidate_color": "white" if candidate else "black", "opening": game["opening"],
                    "plies": len(rows), "result": game["result"], "final_material": material(board, candidate),
                    "first_own_report_below_minus_200": first_bad,
                    "candidate_own_search_cp_drops_120_or_more": own_drops,
                    "opponent_checks": sum(row["gives_check"] for row in rows if row["role"] == "baseline"),
                    "candidate_moves_in_check": sum(row["in_check_before"] for row in rows if row["role"] == "candidate"),
                    "opponent_captures": captures, "promotions": promotions, "trajectory": rows,
                    "csv": filename.name})

second, sixth = [game for game in losses if game["game"] in [2, 6]]
suffix = 0
for first_move, other_move in zip(reversed(second["uci_moves"]), reversed(sixth["uci_moves"])):
    if first_move != other_move:
        break
    suffix += 1
same_after = len(second["uci_moves"]) - suffix
b2, b6 = chess.Board(), chess.Board()
for text in second["uci_moves"][:same_after]:
    b2.push_uci(text)
for text in sixth["uci_moves"][:same_after]:
    b6.push_uci(text)
assert b2.fen() == b6.fen()
output = {"dataset": str(SOURCE), "dataset_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
          "scope": "All seven losses from completed 100ms retry only; original interrupted run excluded. Material and search reports are observations, not an independent strength evaluation.",
          "piece_values": {chess.piece_name(piece): value for piece, value in VALUES.items()},
          "loss_games": [game["game"] for game in losses],
          "games_2_6_transposition": {"same_fen_after_ply": same_after, "shared_final_move_count": suffix,
                                     "same_fen": b2.fen(en_passant="fen"),
                                     "entire_move_vectors_equal": second["uci_moves"] == sixth["uci_moves"],
                                     "caution": "Separate games and full histories, but one repeated critical continuation; do not count as independent failure patterns."},
          "reports": reports}
(OUT / "independent-loss-analysis.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(OUT / "independent-critical-positions.json").write_text(json.dumps(positions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"loss_games": output["loss_games"], "rows": sum(len(r["trajectory"]) for r in reports),
                  "selected_positions": len(positions), "transposition": output["games_2_6_transposition"],
                  "first_negative_reports": [{"game": r["game"], "ply": r["first_own_report_below_minus_200"]["ply"],
                                                "score_cp": r["first_own_report_below_minus_200"]["candidate_pov_search_cp"]} for r in reports]}, indent=2))
