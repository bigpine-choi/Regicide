"""Replay archived SEE games; never launch an engine or evaluate an alternative move.

Run from the repository root with .venv-tests/Scripts/python.exe. The archived
scores are the players' own search reports, not an independent blunder oracle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

import chess

VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
SELECTED = {1: [123, 153], 2: [22, 70], 4: [88, 90, 92],
            10: [68, 76], 11: [39, 41, 45], 12: [36, 40], 16: [88, 96]}
HYPOTHESES = {
    1: "Long minor-piece/pawn ending; king activity and passed-pawn race, not an isolated exchange error.",
    2: "Early ...c5 and a later exposed-king attack; quiet follow-up Qe6/+f7 is outside a same-square exchange model.",
    4: "Rook sacrifice/capture greed followed by a mating net; own PV reaches the dangerous king walk but misses its final consequence.",
    10: "Queen/rook/knight coordination; after ...Ra2 the actual reply Nxe5 captures a bishop, whereas its PV predicted Qb8.",
    11: "Very shallow completed main search despite large extension depth; quiet ...f3 attack after Qg3+ causes a large score reversal.",
    12: "...Nb6 permits quiet Bg5, attacking the queen; the predicted reply was Rf4 instead.",
    16: "Rook ending and remote passed c-pawn; candidate sees the promotion threat too late despite depth 8-9.",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(value * (len(board.pieces(piece, color)) - len(board.pieces(piece, not color)))
               for piece, value in VALUES.items())


def score_cp(record):
    """Do not turn mate scores into arbitrary centipawns."""
    return record["score"].get("cp")


def stats(records):
    if not records:
        return {"count": 0}
    elapsed = sum(s["elapsed_seconds"] for s in records)
    nodes = sum(s["nodes"] for s in records)
    return {
        "count": len(records),
        "mean_nodes": statistics.mean(s["nodes"] for s in records),
        "nodes_per_wall_second": nodes / elapsed,
        "mean_depth": statistics.mean(s["depth"] for s in records),
        "minimum_depth": min(s["depth"] for s in records),
        "depth_le_4": sum(s["depth"] <= 4 for s in records),
        "mean_seldepth": statistics.mean(s["seldepth"] for s in records),
        "mean_extension_gap": statistics.mean(s["seldepth"] - s["depth"] for s in records),
        "mean_elapsed_ms": elapsed * 1000 / len(records),
        "max_elapsed_ms": max(s["elapsed_seconds"] * 1000 for s in records),
        "actual_positions_differ": True,
    }


def pv_san(board, pv):
    copy = board.copy(stack=False)
    result = []
    for uci in pv:
        move = chess.Move.from_uci(uci)
        assert move in copy.legal_moves, (copy.fen(), uci)
        result.append(copy.san(move))
        copy.push(move)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("docs/matches/2026-10-18-see-replication/attempt-002/time1000.json"))
    parser.add_argument("--output", type=Path, default=Path("build/see-cost-improvement/analysis"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    data = json.loads(args.input.read_text(encoding="utf-8"))
    all_records = []
    analyses = []
    positions = []
    replayed = 0
    pv_replayed = 0
    for game in data["games"]:
        board = chess.Board(game["initial_fen"])
        records = {s["ply"]: s for s in game["search_moves"]}
        candidate_color = chess.WHITE if game["white"] == "candidate" else chess.BLACK
        before = {}
        after = {}
        for ply, uci in enumerate(game["uci_moves"], 1):
            before[ply] = board.copy(stack=False)
            if ply in records:
                assert records[ply]["fen_before"] == board.fen(en_passant="fen")
                assert records[ply]["move"] == uci
                pv_replayed += len(pv_san(board, records[ply]["pv"]))
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves
            board.push(move)
            after[ply] = board.copy(stack=False)
            replayed += 1
        assert board.fen(en_passant="fen") == game["final_fen"]
        assert game["plies"] == len(game["uci_moves"])
        for s in game["search_moves"]:
            all_records.append(dict(s, game=game["game"], candidate_points=game["candidate_points"]))
        if game["candidate_points"] != 0:
            continue
        own = [s for s in game["search_moves"] if s["role"] == "candidate"]
        events = []
        for index, record in enumerate(own):
            ply = record["ply"]
            position = before[ply]
            move = chess.Move.from_uci(record["move"])
            following = own[index + 1] if index + 1 < len(own) else None
            opponent = records.get(ply + 1)
            current_cp = score_cp(record)
            next_cp = score_cp(following) if following else None
            event = {
                "game": game["game"], "pair": game["pair"], "ply": ply,
                "turn_label": f"{position.fullmove_number}{'.' if position.turn else '...'}",
                "candidate_color": "white" if candidate_color else "black",
                "fen": position.fen(en_passant="fen"), "move_uci": record["move"],
                "move_san": position.san(move), "in_check": position.is_check(),
                "capture": position.is_capture(move), "gives_check": position.gives_check(move),
                "legal_move_count": position.legal_moves.count(),
                "legal_tactical_count": sum(position.is_capture(m) or bool(m.promotion) for m in position.legal_moves),
                "score": record["score"], "next_own_score": following["score"] if following else None,
                "own_cp_change_by_next_turn": next_cp - current_cp if current_cp is not None and next_cp is not None else None,
                "depth": record["depth"], "seldepth": record["seldepth"], "nodes": record["nodes"],
                "elapsed_ms": record["elapsed_seconds"] * 1000,
                "material_cp_before": material(position, candidate_color),
                "material_cp_after_move": material(after[ply], candidate_color),
                "material_cp_after_2_plies": material(after.get(ply + 1, after[ply]), candidate_color),
                "material_cp_after_8_plies": material(after.get(min(ply + 7, game["plies"]), after[ply]), candidate_color),
                "pv_uci": record["pv"], "pv_san": pv_san(position, record["pv"]),
                "actual_reply_uci": opponent["move"] if opponent else None,
                "predicted_reply_uci": record["pv"][1] if len(record["pv"]) > 1 else None,
                "opponent_score": opponent["score"] if opponent else None,
            }
            if opponent:
                reply_pos = before[ply + 1]
                reply_move = chess.Move.from_uci(opponent["move"])
                event["actual_reply_san"] = reply_pos.san(reply_move)
                event["actual_reply_quiet"] = not reply_pos.is_capture(reply_move) and not reply_move.promotion
                event["actual_reply_check"] = reply_pos.gives_check(reply_move)
                event["predicted_reply_matches"] = event["predicted_reply_uci"] == opponent["move"]
            events.append(event)
            if ply in SELECTED[game["game"]]:
                positions.append(dict(event,
                    id=f"loss-g{game['game']:02d}-p{ply:03d}",
                    opening_uci_moves=game["uci_moves"][:ply - 1],
                    source_game=game["game"], source_ply=ply,
                    purpose="Development/cost diagnosis; never held out for this change",
                    best_move_known=False))
        finite_events = [e for e in events if e["own_cp_change_by_next_turn"] is not None]
        analyses.append({
            "game": game["game"], "pair": game["pair"], "opening": game["opening"],
            "candidate_color": "white" if candidate_color else "black", "plies": game["plies"],
            "termination": game["termination"], "hypothesis": HYPOTHESES[game["game"]],
            "candidate_resources": stats(own),
            "largest_own_cp_drops": sorted(finite_events, key=lambda e: e["own_cp_change_by_next_turn"])[:5],
            "selected_events": [e for e in events if e["ply"] in SELECTED[game["game"]]],
            "first_own_cp_below_minus100": next((e for e in events if e["score"].get("cp") is not None and e["score"]["cp"] <= -100), None),
            "all_candidate_events": events,
        })
    own_records = [s for s in all_records if s["role"] == "candidate"]
    result = {
        "schema_version": 1, "input": args.input.as_posix(), "input_sha256": sha(args.input),
        "script_sha256": sha(Path(__file__)), "python_chess_version": chess.__version__,
        "method": "Exact legal replay and archived mover scores; no engines launched; no alternative move searched.",
        "correction_note": "Initial manual prose incorrectly called game 10 ply 77 Nxe5 a quiet Ne5 and the predicted Qb8 a check. These descriptions are corrected; generated SAN/quiet/check fields were always derived from python-chess and remain authoritative.",
        "limitations": [
            "Own score changes are suspicions, not independent centipawn loss or proof of a blunder.",
            "Material changes within eight actual plies are descriptive and may be temporary tactical investments.",
            "Mean nodes/depth/NPS compare different played positions, not a controlled SEE cost benchmark.",
            "Archived game JSON has no see_calls/qnodes/pvs_researches data; cannot attribute wall time to SEE from it.",
            "A high seldepth is maximum selective path length and does not quantify where total work went.",
            "Selected positions are development data; no future match positions may be selected based on these results.",
        ],
        "replayed_game_count": len(data["games"]), "replayed_plies": replayed,
        "replayed_pv_plies": pv_replayed,
        "candidate_loss_games": [g["game"] for g in analyses],
        "candidate_all_games": stats(own_records),
        "baseline_all_games": stats([s for s in all_records if s["role"] == "baseline"]),
        "candidate_loss_moves": stats([s for s in own_records if s["candidate_points"] == 0]),
        "candidate_nonloss_moves": stats([s for s in own_records if s["candidate_points"] != 0]),
        "losses": analyses,
    }
    (args.output / "archived-loss-analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "development-positions.json").write_text(json.dumps({
        "schema_version": 1, "source": args.input.as_posix(), "source_sha256": sha(args.input),
        "role": "Development positions only; this is not an unbiased test set or a best-move suite.",
        "positions": positions,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    low_depth = sorted(positions, key=lambda r: (r["depth"], -(r["seldepth"] - r["depth"]), r["game"], r["ply"]))[:3]
    drops = sorted([r for r in positions if r["score"].get("cp") is not None and r["score"]["cp"] >= -200
                    and r["own_cp_change_by_next_turn"] is not None],
                   key=lambda r: (r["own_cp_change_by_next_turn"], r["game"], r["ply"]))[:3]
    cost_subset = list({r["id"]: r for r in low_depth + drops}.values())
    (args.output / "development-cost-subset.json").write_text(json.dumps({
        "schema_version": 1,
        "purpose": "Deterministic development cost sample only; not strength validation or a best-move test",
        "source": (args.output / "development-positions.json").as_posix(),
        "selection_rule": {
            "low_depth": "First 3 sorted by (depth ascending, seldepth-depth descending, game ascending, ply ascending)",
            "score_drops": "First 3 with finite own cp >= -200 and finite own_cp_change_by_next_turn, sorted by (own_cp_change_by_next_turn ascending, game ascending, ply ascending)",
            "combine": "low_depth then score_drops; first occurrence per id",
        },
        "positions": cost_subset,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Archived 1-second SEE losses: descriptive replay", "",
             "No engine was launched. Scores are the players' own reports; the word suspicious does not mean a verified blunder.", "",
             result["correction_note"], "",
             f"Replayed {result['replayed_game_count']} games, {replayed} game plies, and {pv_replayed} PV plies legally.", "",
             "| Game | Color | Main-search minimum | Mean depth | Mean selective depth | Hypothesis |",
             "|---|---|---:|---:|---:|---|"]
    for g in analyses:
        s = g["candidate_resources"]
        lines.append(f"| {g['game']} | {g['candidate_color']} | {s['minimum_depth']} | {s['mean_depth']:.2f} | {s['mean_seldepth']:.2f} | {g['hypothesis']} |")
    lines.extend(["", "## Selected development positions", "", "All positions include full actual move history in development-positions.json. No best move is asserted.", ""])
    for g in analyses:
        lines.extend([f"### Game {g['game']}: {g['opening']}", "", g["hypothesis"], ""])
        for e in g["selected_events"]:
            lines.extend([f"- Ply {e['ply']}, {e['turn_label']}{e['move_san']}: cp {e['score'].get('cp')}, depth {e['depth']}, seldepth {e['seldepth']}, nodes {e['nodes']}; next own cp {e['next_own_score'].get('cp') if e['next_own_score'] else None}; material before/after 8 actual plies {e['material_cp_before']}/{e['material_cp_after_8_plies']}.",
                          f"  - FEN: `{e['fen']}`",
                          f"  - Published PV: {' '.join(e['pv_san'])}",
                          f"  - Actual reply: {e.get('actual_reply_san')} (quiet={e.get('actual_reply_quiet')}, check={e.get('actual_reply_check')}); predicted reply matches={e.get('predicted_reply_matches')}."])
        lines.append("")
    lines.extend(["## Cost attribution limits", ""] + [f"- {s}" for s in result["limitations"]])
    (args.output / "archived-loss-analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("replayed_game_count", "replayed_plies", "replayed_pv_plies", "candidate_loss_games", "candidate_all_games", "baseline_all_games", "candidate_loss_moves", "candidate_nonloss_moves")}, indent=2))
    print(f"Wrote {len(positions)} development positions to {args.output}")


if __name__ == "__main__":
    main()
