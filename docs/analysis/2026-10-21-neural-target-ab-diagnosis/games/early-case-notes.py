"""Describe bounded root/child counterfactuals without executing an engine."""
import hashlib
import json
from pathlib import Path
import statistics

import chess

OUT = Path(__file__).resolve().parent
DIAG = OUT.parent
ROOT = OUT.parents[3]
MATCH = ROOT / "docs/checks/2026-10-20-neural-target-ab/attempt-002/matches-100ms/match.json"
games = {r["game"]: r for r in json.loads(MATCH.read_text())["games"]}
comparisons_path = DIAG / "early-counterfactuals/game-comparisons.json"
queries_path = DIAG / "early-counterfactuals/queries.jsonl"
selection_path = DIAG / "early-games/selected-positions.jsonl"
comparisons = json.loads(comparisons_path.read_text())
queries = {r["query_id"]: r for r in map(json.loads, queries_path.read_text().splitlines())}
selections = {r["game"]: r for r in map(json.loads, selection_path.read_text().splitlines())}


def san_pv(fen, moves):
    board = chess.Board(fen)
    out = []
    for uci in moves:
        move = chess.Move.from_uci(uci)
        out.append(board.san(move))
        board.push(move)
    return out


report = {"schema_version": 1, "by_losing_arm": {}, "cases": [], "interpretation": [
    "All 76 decisive games contribute one sixth searched decision, fixed before the new engine probes.",
    "These counterfactuals use the own classical 100k engine, a finite reference sharing the training teacher; they do not prove best moves.",
    "A10k and B10k have equal node budgets, unlike the original 100ms game. Played moves are retained separately.",
    "Root and fresh child searches have different horizons. Candidate quality comparisons use equal fresh child budgets, not root minus child.",
    "The highlighted examples are explanatory case selection after exploratory analysis, not an independent success-rate test."]}
for arm in "AB":
    rows = [r for r in comparisons if r["losing_arm"] == arm]
    gaps = []
    alternate_gaps = []
    for r in rows:
        value = lambda label: r["child_reference"][r["candidate_moves"][label]]["mover_cp"]
        a, b, played = value("A10k"), value("B10k"), value("played")
        if a is not None and b is not None:
            gaps.append(a-b)
        alternate = a if arm == "B" else b
        if alternate is not None and played is not None:
            alternate_gaps.append(alternate-played)
    report["by_losing_arm"][arm] = {
        "positions": len(rows), "same_A10k_B10k_move": sum(r["candidate_moves"]["A10k"] == r["candidate_moves"]["B10k"] for r in rows),
        "actual_100ms_move_matches_own10k": sum(r["candidate_moves"][arm+"10k"] == r["candidate_moves"]["played"] for r in rows),
        "exact_child_cp_comparisons": len(gaps),
        "A_minus_B_child_reference_cp_median": statistics.median(gaps),
        "A_minus_B_child_reference_cp_mean": statistics.mean(gaps),
        "A_child_at_least100cp_better": sum(v >= 100 for v in gaps),
        "B_child_at_least100cp_better": sum(v <= -100 for v in gaps),
        "opposite_arm10k_at_least100cp_better_than_actual": sum(v >= 100 for v in alternate_gaps),
        "opposite_arm10k_vs_actual_exact_cp_denominator": len(alternate_gaps)}
for gid in (78, 23, 4):
    row = next(r for r in comparisons if r["game"] == gid)
    selection, game = selections[gid], games[gid]
    board = chess.Board(row["fen_before"])
    mover_sign = 1 if board.turn else -1
    actual_san = san_pv(game["initial_fen"], game["uci_moves"])
    case = {"game": gid, "seed": row["seed"], "family_id": row["family_id"],
            "losing_arm": row["losing_arm"], "ply": selection["ply"],
            "query_id": row["query_id"], "fen_before": row["fen_before"],
            "initial_fen": selection["initial_fen"], "history": selection["moves_before"],
            "actual_search": selection["original_search"],
            "actual_san_next12": actual_san[selection["ply"]-1:selection["ply"]+11],
            "candidate_moves_san": {k: board.san(chess.Move.from_uci(v)) for k, v in row["candidate_moves"].items()},
            "classical_root_mover_cp": row["classical_root_white_cp"]*mover_sign,
            "candidate_child_mover_cp": {k: row["child_reference"][v]["mover_cp"] for k, v in row["candidate_moves"].items()},
            "queries": []}
    for q in queries.values():
        if q["query_id"].startswith(row["query_id"]+"/"):
            case["queries"].append({"query_id": q["query_id"], "fen": q["fen"],
                "exact_white_cp": q["exact_white_cp"], "depth": q["result"]["depth"],
                "nodes": q["result"]["nodes"], "pv_san": san_pv(q["fen"], q["result"]["pv_uci"])})
    report["cases"].append(case)
report["case_interpretations_ko"] = {
    "78": "12...Ne4+는 원래 B 100ms 실제 수이면서 B10k 재검색 수도 같다. A10k는 Nbd7. 흑관점 공통 자식참조는 각각 -246과 -55cp로 191cp 차이다. 루트참조 -41cp여서 후기 패색 위치만 본 사례보다 진단 가치가 크다. 실전은 Nxe4 dxe4 d5 Ra6 dxc6 Qxd1 c7로 이어졌다. B의 10k 주변화는 d5 대신 Nxc6를 예상했다. 유한 탐색의 수순 예측 차이가 확인되지만 이 한 수가 49패 전체의 원인이라는 뜻은 아니다.",
    "23": "원래 B 실제 12...Nb4의 자식참조 -334cp에 비해 A10k Ne5는 -85cp다. B10k는 원래 수와 다른 Qh4+(-314cp)를 택한다. 루트 -93cp에서 불리한 선택을 하는 양상은 보이지만 10k B 대안을 실전 실제 수와 혼동하면 안 된다.",
    "4": "A패배 위치에서는 B10k Qe5+의 자식참조 -506cp가 A10k Qb4+(-648cp)보다142cp 낫다. 그러나 원래 A 100ms 실제 수도 Qe5+이므로 고정노드 차이를 실제 A의 실수로 부르면 잘못이다. B가 언제나 나쁜 수를 고르는 것은 아니며 예산과 선택시점을 맞춘 비교가 필요하다."}
report["source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (MATCH, comparisons_path, queries_path, selection_path, Path(__file__))}
with (OUT / "early-case-notes.json").open("x", encoding="utf-8", newline="\n") as handle:
    json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
    handle.write("\n")
print(json.dumps(report["by_losing_arm"]))
