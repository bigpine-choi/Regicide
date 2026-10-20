"""Additional descriptive checks; no engines, no edits to prior evidence."""
import collections
import hashlib
import json
from pathlib import Path
import statistics
import sys

import chess

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_neural_ab_losses import mating_moves

OUT = Path(__file__).resolve().parent
MATCH = ROOT / "docs/checks/2026-10-20-neural-target-ab/attempt-002/matches-100ms/match.json"
data = json.loads(MATCH.read_text())
selected = [json.loads(line) for line in (OUT / "selected-positions.jsonl").read_text().splitlines()]
moves = [json.loads(line) for line in (OUT / "move-predictions.jsonl").read_text().splitlines()]
lookup = {(r["game"], r["ply"]): r for r in moves}
report = {"schema_version": 1, "mechanical_mate_escape_audit": [], "by_arm": {},
          "color_scores": {}, "limitations": [
              "A forced mate on the next move at the final losing decision does not locate the earlier mistake.",
              "Recorded search scores are each model's own estimate, not chess truth.",
              "Selected pivots may be after the original mistake. The rule locates material deterioration, not the first bad decision.",
              "Same-position A/B static delta comparisons do not compare full search move rankings."]}
for color in ("white", "black"):
    for arm, role in (("A", "baseline"), ("B", "candidate")):
        games = [g for g in data["games"] if g[color] == role]
        points = sum(g["candidate_points"] if arm == "B" else 1-g["candidate_points"] for g in games)
        report["color_scores"][arm + "_" + color] = {"games": len(games), "points": points, "score": points/len(games)}
for g in data["games"]:
    if g["result"] == "1/2-1/2":
        continue
    loser_color = g["result"] == "0-1"
    loser_role = g["white"] if loser_color else g["black"]
    loser_arm = data["role_mapping"][loser_role]
    board = chess.Board(g["initial_fen"])
    for uci in g["uci_moves"][:-2]:
        board.push_uci(uci)
    assert board.turn == loser_color
    alternatives = []
    for move in list(board.legal_moves):
        board.push(move)
        replies = mating_moves(board)
        board.pop()
        alternatives.append({"move": move.uci(), "opponent_mates_in_one": replies})
    report["mechanical_mate_escape_audit"].append({
        "game": g["game"], "losing_arm": loser_arm, "ply": g["plies"]-1,
        "initial_fen": g["initial_fen"], "history": g["uci_moves"][:-2],
        "fen": board.fen(en_passant="fen"), "played_uci": g["uci_moves"][-2],
        "legal_alternatives": alternatives,
        "legal_moves_avoiding_mate_on_next_reply": sum(not row["opponent_mates_in_one"] for row in alternatives)})
for arm in "AB":
    rows = [r for r in selected if r["losing_arm"] == arm]
    cp = [r["original_search"]["score"]["cp"] for r in rows if r["original_search"]["score"]["cp"] is not None]
    delta = lambda r, a: (lookup[(r["game"], r["ply"])]["after_predictions_white_cp"][a]
                          - lookup[(r["game"], r["ply"])]["predictions_white_cp"][a]) * (1 if r["loser_color"] == "white" else -1)
    report["by_arm"][arm] = {
        "selected_positions": len(rows), "own_search_cp_count": len(cp),
        "own_search_cp_min_median_max": [min(cp), statistics.median(cp), max(cp)],
        "own_search_cp_nonnegative": sum(v >= 0 for v in cp),
        "own_search_mate_scores": [r["original_search"]["score"]["mate"] for r in rows if r["original_search"]["score"]["mate"] is not None],
        "played_move_static_loser_delta_median_A": statistics.median(delta(r, "A") for r in rows),
        "played_move_static_loser_delta_median_B": statistics.median(delta(r, "B") for r in rows),
        "B_delta_nonnegative_and_A_delta_le_minus100": sum(delta(r, "B") >= 0 and delta(r, "A") <= -100 for r in rows),
        "final_losing_decisions_with_legal_move_avoiding_next_reply_mate": sum(
            r["legal_moves_avoiding_mate_on_next_reply"] > 0 for r in report["mechanical_mate_escape_audit"] if r["losing_arm"] == arm)}
report["source_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in (MATCH, OUT / "selected-positions.jsonl", OUT / "move-predictions.jsonl", Path(__file__))}
with (OUT / "mechanical-followup.json").open("x", encoding="utf-8", newline="\n") as handle:
    json.dump(report, handle, sort_keys=True, indent=2)
    handle.write("\n")
print(json.dumps({"by_arm": report["by_arm"], "color_scores": report["color_scores"]}))
