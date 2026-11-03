"""Independent full-strength audit of actual production sparring games.

Never use weakened opponent play-info as the score of its selected move.
Each board receives its own fresh search. CP, mate and unknown stay separate.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

import chess
import chess.engine


VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def material(board, color):
    return sum(VALUES[p.piece_type] * (1 if p.color == color else -1) for p in board.piece_map().values())


def capture_value(board, move):
    if board.is_en_passant(move):
        return 100
    piece = board.piece_at(move.to_square)
    return VALUES[piece.piece_type] if piece else 0


def exposure(board, move):
    """Independent short legal-capture diagnostic, not a sacrifice verdict."""
    gain = capture_value(board, move)
    if move.promotion:
        gain += VALUES[move.promotion] - 100
    child = board.copy(stack=True)
    child.push(move)
    rows = []
    for response in child.legal_moves:
        victim = capture_value(child, response)
        if victim < 300:
            continue
        following = child.copy(stack=True)
        following.push(response)
        recovery = max((capture_value(following, answer) for answer in following.legal_moves
                        if answer.to_square == response.to_square and following.is_capture(answer)), default=0)
        swing = gain - victim + recovery
        if swing <= -200:
            rows.append({"reply": response.uci(), "victim_cp": victim, "recapture_cp": recovery,
                         "candidate_gain_cp": gain, "net_cp": swing})
    return rows


def exact_packet(board, info, returned):
    if (type(info.get("depth")) is not int or info["depth"] < 1 or info.get("lowerbound")
            or info.get("upperbound") or info.get("multipv", 1) != 1):
        return None
    score, pv = info.get("score"), info.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or not isinstance(pv, (list, tuple)) or not pv or pv[0] != returned):
        return None
    replay = board.copy(stack=True)
    for move in pv:
        if move not in replay.legal_moves:
            return None
        replay.push(move)
    white = score.white()
    cp, mate = white.score(), white.mate()
    return {"kind": "cp" if cp is not None else "mate", "cp_white": cp, "mate_white": mate,
            "depth": info["depth"], "nodes": info.get("nodes"), "bestmove": returned.uci(),
            "pv_uci": [m.uci() for m in pv], "exact_completed_packet": True}


class Auditor:
    def __init__(self, binary, nodes):
        self.binary, self.nodes = binary, nodes
        self.engine = chess.engine.SimpleEngine.popen_uci(str(binary), timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.options = {"Threads": 1, "Hash": 32, "Skill Level": 20, "UCI_LimitStrength": False}
        self.engine.configure(self.options)
        self.identity = dict(self.engine.id)

    def close(self):
        self.engine.quit()

    def evaluate(self, board, *, deep_ms=None):
        outcome = board.outcome(claim_draw=True)
        if outcome:
            return {"kind": "terminal", "result": outcome.result(), "winner":
                    None if outcome.winner is None else "white" if outcome.winner else "black",
                    "cp_white": 0 if outcome.winner is None else None, "mate_white": None,
                    "depth": None, "exact_completed_packet": False}
        started = time.monotonic()
        packets = []
        limit = chess.engine.Limit(time=deep_ms / 1000) if deep_ms else chess.engine.Limit(nodes=self.nodes)
        # New game token resets the transposition table between positions.
        with self.engine.analysis(board.copy(stack=True), limit, game=object(), info=chess.engine.INFO_ALL) as analysis:
            for info in analysis:
                if "score" in info and "depth" in info:
                    packets.append(dict(info))
            returned = analysis.wait().move
        valid = [value for info in packets if (value := exact_packet(board, info, returned)) is not None]
        result = max(valid, key=lambda item: item["depth"]) if valid else {
            "kind": "unknown", "cp_white": None, "mate_white": None, "reason": "no_exact_matching_completed_packet"}
        return {**result, "budget": {"nodes": None if deep_ms else self.nodes, "time_ms": deep_ms},
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}


def own_cp(score, white):
    cp = score.get("cp_white")
    return cp * (1 if white else -1) if type(cp) is int else None


def winning_side(score):
    if score.get("kind") == "terminal":
        return score.get("winner")
    mate = score.get("mate_white")
    return ("white" if mate > 0 else "black") if type(mate) is int and mate else None


def score_loss(before, after, white):
    a, b = own_cp(before, white), own_cp(after, white)
    own = "white" if white else "black"
    known = all(own_cp(score, white) is not None or winning_side(score) is not None for score in (before, after))
    changed_to_losing_mate = (winning_side(after) not in (None, own) and winning_side(before) in (None, own)) if known else None
    lost_winning_mate = (winning_side(before) == own and winning_side(after) != own) if known else None
    return {"cp_loss": a - b if a is not None and b is not None else None,
            "new_losing_mate": changed_to_losing_mate, "lost_winning_mate": lost_winning_mate,
            "comparison_known": known}


def ramp_receipts(decision, fullmove, *, path="final"):
    """Check the actual adjudicating score source, plus pre-safety decisions."""
    if not isinstance(decision, dict):
        return []
    results = []
    prior = decision.get("pre_safety_selection")
    if isinstance(prior, dict):
        results.extend(ramp_receipts(prior, fullmove, path=path + ".pre_safety_selection"))
    selected, baseline = decision.get("selected_uci"), decision.get("baseline_uci")
    changed = bool(selected and baseline and selected != baseline)
    if not 21 <= fullmove <= 49 or not changed:
        return results
    ref = decision.get("referee") or {}
    if ref.get("applied") and ref.get("accepted") and ref.get("selected_uci") == selected:
        baseline_cp = (ref.get("band") or {}).get("baseline_cp_own")
        matching = next((r for r in ref.get("candidates", []) if r.get("uci") == selected), {})
        values = {"root": matching.get("cp_own"), "child": (ref.get("postmove") or {}).get("cp_own")}
        recorded_guard = ref.get("ramp_guard") or (ref.get("band") or {}).get("ramp_guard")
        source = "referee"
    else:
        baseline_cp = (decision.get("baseline") or {}).get("cp")
        control = decision.get("band_control") or {}
        values = {"compared": (decision.get("candidate_compared") or {}).get("cp", control.get("selected_cp")),
                  "final": (decision.get("candidate_final") or {}).get("cp", control.get("selected_final_cp")),
                  "child": (decision.get("postmove_verification") or {}).get("cp_own", control.get("selected_postmove_cp"))}
        recorded_guard = decision.get("ramp_guard") or control.get("ramp_guard")
        source = "native"
    cap = round(120 * (50 - fullmove) / 30, 6)
    floor = min(baseline_cp, max(0, baseline_cp - cap)) if type(baseline_cp) in (int, float) else None
    guard_policy = recorded_guard.get("policy") if isinstance(recorded_guard, dict) else None
    if guard_policy == "regicide-sparring-ramp-guard-v2" and type(baseline_cp) in (int, float):
        # Independently recompute the recorded policy; do not import the
        # implementation that this auditor is intended to check.
        cap = round(min(120, max(0, baseline_cp) * .35) * ((50 - fullmove) / 30) ** 2, 6)
        floor = round(baseline_cp - cap, 6)
    violations = [key for key, value in values.items() if type(value) in (int, float) and floor is not None and value < floor - 1e-6]
    unknown = floor is None or any(type(value) not in (int, float) for value in values.values())
    guard_match = (isinstance(recorded_guard, dict) and floor is not None
                   and guard_policy in (None, "regicide-sparring-ramp-guard-v1", "regicide-sparring-ramp-guard-v2")
                   and type(recorded_guard.get("floor_cp")) in (int, float)
                   and type(recorded_guard.get("max_loss_cp")) in (int, float)
                   and abs(recorded_guard.get("floor_cp", -1e9) - floor) < 1e-5
                   and abs(recorded_guard.get("max_loss_cp", -1e9) - cap) < 1e-5)
    return results + [{"path": path, "source": source, "selected": selected, "baseline": baseline,
                       "played_final": path == "final", "execution": "played_final" if path == "final" else "superseded_pre_safety",
                       "baseline_cp": baseline_cp, "scores": values, "fullmove": fullmove,
                       "expected_cap_cp": cap, "expected_floor_cp": floor, "recorded_guard_matches": guard_match,
                       "violations": violations, "unknown": unknown,
                       "compliant": not violations and not unknown and guard_match}]


def replay_game(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    board = chess.Board(data.get("initial_fen", chess.STARTING_FEN))
    boards = [board.copy(stack=True)]
    moves = data.get("moves", [])
    ucis = data.get("uci_moves") or [row["uci"] for row in moves]
    if len(moves) != len(ucis):
        raise ValueError(f"Move detail count mismatch: {path}")
    own_white = data["sparring_color"] == "white"
    for i, (uci, row) in enumerate(zip(ucis, moves), 1):
        if row["ply"] != i or row["uci"] != uci or row["before_fen"] != board.fen(en_passant="fen"):
            raise ValueError(f"Identity mismatch: {path}, ply {i}")
        move = chess.Move.from_uci(uci)
        actor = "sparring" if board.turn == own_white else "stockfish"
        if move not in board.legal_moves or row["actor"] != actor:
            raise ValueError(f"Illegal move/actor: {path}, ply {i}")
        board.push(move)
        if row["after_fen"] != board.fen(en_passant="fen"):
            raise ValueError(f"After-position mismatch: {path}, ply {i}")
        boards.append(board.copy(stack=True))
    if data.get("final_fen") != board.fen(en_passant="fen"):
        raise ValueError(f"Final position mismatch: {path}")
    return data, boards


def close_stats(scores, indexes):
    known = [i for i in indexes if own_cp(scores[i], True) is not None]
    inside = [i for i in known if abs(scores[i]["cp_white"]) <= 100]
    return {"positions": len(indexes), "known_cp": len(known), "close": len(inside),
            "unknown_or_mate": len(indexes) - len(known), "fraction_known": len(inside) / len(known) if known else None,
            "fraction_all": len(inside) / len(indexes) if indexes else None,
            "outside_plies": [i for i in indexes if i not in inside]}


def audit_game(path, auditor, output, deep_ms, max_deep):
    game, boards = replay_game(path)
    white = game["sparring_color"] == "white"
    scores = []
    for ply, board in enumerate(boards):
        score = auditor.evaluate(board)
        scores.append({"ply": ply, "fen": board.fen(en_passant="fen"), **score})
        if ply % 20 == 0:
            print(json.dumps({"event": "audit_root", "game": str(path), "ply": ply, "cp_white": score.get("cp_white")}), flush=True)
    early = list(range(min(40, len(boards) - 1) + 1))
    own_early = [i for i in early if i > 0 and game["moves"][i - 1]["actor"] == "sparring"]
    human_early = [i for i in early if i > 0 and game["moves"][i - 1]["actor"] == "stockfish"]
    events, ramps, safety, own_moves = [], [], [], []
    for ply, turn in enumerate(game["moves"], 1):
        if turn["actor"] != "sparring":
            continue
        # A controlled continuation retains historical positions for rules and
        # repetition. Its historical moves are not fresh policy decisions.
        if ply <= game.get("continuation_start_ply", 0) and turn.get("recorded_prefix"):
            continue
        before, after = boards[ply - 1], boards[ply]
        decision = turn.get("sparring_decision") or {}
        estimates = score_loss(scores[ply - 1], scores[ply], white)
        exposed = exposure(before, chess.Move.from_uci(turn["uci"]))
        future = min(len(boards) - 1, ply + 4)
        actual_swing = min(material(boards[k], white) - material(before, white) for k in range(ply, future + 1))
        kind = ("safety_replacement" if decision.get("reason") == "tactical_safety_replacement" else
                "native_baseline" if decision.get("selected_uci") == decision.get("baseline_uci") else
                "referee_alternative" if (decision.get("referee") or {}).get("applied") else "native_alternative")
        row = {"ply": ply, "fullmove": before.fullmove_number, "uci": turn["uci"], "san": turn["san"],
               "selection_kind": kind, "decision_reason": decision.get("reason"), **estimates,
               "cp_before_own": own_cp(scores[ply - 1], white), "cp_after_own": own_cp(scores[ply], white),
               "immediate_exposure": exposed, "minimum_actual_material_swing_next4plies": actual_swing}
        own_moves.append(row)
        receipts = ramp_receipts(decision, before.fullmove_number)
        ramps.extend({"ply": ply, **item} for item in receipts)
        if decision.get("tactical_safety"):
            safety.append({"ply": ply, **decision["tactical_safety"]})
        ramp_cap = round(120 * (50 - before.fullmove_number) / 30, 6) if 21 <= before.fullmove_number <= 49 else None
        current_receipt = next((item for item in receipts if item["played_final"]), None)
        if current_receipt is not None:
            ramp_cap = current_receipt["expected_cap_cp"]
        empirical_excess = (ramp_cap is not None and kind in ("native_alternative", "referee_alternative")
                            and estimates["cp_loss"] is not None and estimates["cp_loss"] > ramp_cap + 50)
        row["independent_ramp_excess_signal"] = empirical_excess
        row["ramp_cap_cp"] = ramp_cap
        suspicious = empirical_excess or estimates["new_losing_mate"] or estimates["lost_winning_mate"] or (
            estimates["cp_loss"] is not None and estimates["cp_loss"] > 150)
        if suspicious or exposed or actual_swing <= -200:
            events.append({**row, "deep_reviewed": False, "trigger": "score_loss_or_mate" if suspicious else
                           "material_exposure" if exposed else "delayed_material_swing"})
    # Prioritize largest losses; a bounded deep-review budget stays explicit.
    ordered = sorted(events, key=lambda r: (not r["new_losing_mate"], r["fullmove"] > 20,
                                           not bool(r["immediate_exposure"]), -(r["cp_loss"] or 0)))
    for index, event in enumerate(ordered):
        if max_deep and index >= max_deep:
            break
        ply = event["ply"]
        a = auditor.evaluate(boards[ply - 1], deep_ms=deep_ms)
        b = auditor.evaluate(boards[ply], deep_ms=deep_ms)
        loss = score_loss(a, b, white)
        serious = loss["new_losing_mate"] or loss["lost_winning_mate"] or loss["cp_loss"] is not None and loss["cp_loss"] > 150
        material_signal = bool(event["immediate_exposure"]) or event["minimum_actual_material_swing_next4plies"] <= -200
        alternate_recommended = a.get("bestmove") is not None and a["bestmove"] != event["uci"]
        avoidability = ("independent_search_recommends_alternative" if alternate_recommended else
                        "same_bestmove_or_missing_recommendation; avoidability_unresolved")
        ramp_excess = (event["ramp_cap_cp"] is not None and event["selection_kind"] in ("native_alternative", "referee_alternative")
                       and loss["cp_loss"] is not None and loss["cp_loss"] > event["ramp_cap_cp"] + 50)
        event.update(deep_reviewed=True, deep_before=a, deep_after=b, deep_loss=loss,
                     deep_large_loss=bool(serious), uncompensated_material_loss_suspect=bool(serious and material_signal),
                     alternative_recommended=alternate_recommended, avoidability=avoidability,
                     deep_empirical_ramp_excess=bool(ramp_excess),
                     classification=("material_loss_with_deep_tactical_loss" if serious and material_signal else
                                     "deep_large_positional_or_tactical_loss" if serious else
                                     "independent_ramp_loss_exceeds_cap_plus50" if ramp_excess else "quick_signal_not_confirmed"))
        print(json.dumps({"event": "audit_deep", "game": str(path), "ply": ply, "loss_cp": loss["cp_loss"],
                          "classification": event["classification"]}), flush=True)
    early_stats = close_stats(scores, early)
    result = {"game_file": str(path.resolve()), "game_sha256": sha(path), "sparring_color": game["sparring_color"],
              "stockfish_elo_setting": game.get("stockfish_elo"), "plies": len(boards) - 1,
              "result": game.get("result"), "termination": game.get("termination"), "error": game.get("error"),
              "legal_replay": True, "reached20": len(boards) > 40, "reached50": len(boards) > 98,
              "early_all_roots": early_stats, "early_after_sparring": close_stats(scores, own_early),
              "early_after_opponent": close_stats(scores, human_early),
              "strict20_pass": len(boards) > 40 and early_stats["close"] == 41,
              "ramp_concessions": ramps, "ramp_violation_count": sum(r["played_final"] and bool(r["violations"]) for r in ramps),
              "ramp_unknown_count": sum(r["played_final"] and (r["unknown"] or not r["recorded_guard_matches"]) for r in ramps),
              "pre_safety_attempt_violation_count": sum(not r["played_final"] and bool(r["violations"]) for r in ramps),
              "pre_safety_attempt_unknown_count": sum(not r["played_final"] and (r["unknown"] or not r["recorded_guard_matches"]) for r in ramps),
              "played_ramp_concessions": sum(r["played_final"] for r in ramps),
              "ramp_own_moves": sum(21 <= r["fullmove"] <= 49 for r in own_moves),
              "finish_own_moves": sum(r["fullmove"] >= 50 for r in own_moves),
              "ramp_baseline_moves": sum(21 <= r["fullmove"] <= 49 and r["selection_kind"] == "native_baseline" for r in own_moves),
              "missing_sparring_receipts": sum(t["actor"] == "sparring" and not t.get("sparring_decision") for t in game["moves"]),
              "safety_checks": safety, "own_moves": own_moves, "suspicious_events": events,
              "deep_reviewed": sum(e["deep_reviewed"] for e in events),
              "deep_pending": sum(not e["deep_reviewed"] for e in events),
              "deep_large_losses": sum(e.get("deep_large_loss", False) for e in events),
              "deep_empirical_ramp_excesses": sum(e.get("deep_empirical_ramp_excess", False) for e in events),
              "material_loss_suspects": sum(e.get("uncompensated_material_loss_suspect", False) for e in events),
              "position_scores": scores}
    write(output, result)
    return result


def report(document):
    games = document["games"]
    lines = ["# 수정 스파링 실제 대국 독립 검증", "", "약화 상대의 탐색값 대신 별도 전력 Stockfish로 모든 실제 위치를 다시 평가했다. 작은 기능 검증이며 Elo나 인간 체감의 증거는 아니다.", "",
             "| 대국 | 상대 설정 | Regicide | 수 | 20수 도달 | 20수까지 접전 위치 | 연속 접전 | 램프 위반 | 깊은 재검토 큰 손실 |", "|---|---:|---|---:|---|---|---|---:|---:|"]
    for i, g in enumerate(games, 1):
        c = g["early_all_roots"]
        lines.append(f"| {i} | {g['stockfish_elo_setting']} | {g['sparring_color']} | {g['plies']/2:g} | {'예' if g['reached20'] else '아니오'} | {c['close']}/{c['positions']} | {'충족' if g['strict20_pass'] else '미충족'} | {g['ramp_violation_count']} | {g['deep_large_losses']} |")
    lines += ["", "접전 기준은 초기 위치부터 20수까지 41개 위치 모두 |평가| ≤ 1.00이며, 메이트·미확인 평가도 충족으로 세지 않는다. 상대가 둔 직후와 스파링이 둔 직후의 비율은 JSON에 따로 기록했다. 표의 램프 위반은 실제 최종 수만 센다. 안전 검사에 의해 대체된 이전 시도는 별도 집계한다.", "",
              "## 주요 의심 장면", ""]
    for i, g in enumerate(games, 1):
        for e in g["suspicious_events"]:
            if e.get("deep_large_loss") or e.get("deep_empirical_ramp_excess"):
                loss = e["deep_loss"]
                description = (f"깊은 재평가 손실 {loss['cp_loss']}cp" if loss["cp_loss"] is not None else
                               "새로운 패배 메이트 추정" if loss["new_losing_mate"] else "기존 승리 메이트 추정 소실")
                avoidability = "별도 탐색이 다른 수 권고" if e["alternative_recommended"] else "같은 수 권고 또는 근거 부족으로 회피 가능성 미확정"
                lines.append(f"- 대국 {i}, {e['fullmove']}수 {e['san']} (ply {e['ply']}): {e['selection_kind']}, {description}, {e['classification']}, {avoidability}.")
        if g["pre_safety_attempt_violation_count"]:
            lines.append(f"- 대국 {i}: 최종 적용 전 안전 검사로 교체된 선택 중 램프 위반 시도 {g['pre_safety_attempt_violation_count']}건(실제 둔 수 위반에 포함하지 않음).")
        if g["ramp_unknown_count"] or g["deep_pending"]:
            lines.append(f"- 대국 {i}: 램프 근거 미확인 {g['ramp_unknown_count']}건, 깊은 재검토 미완료 {g['deep_pending']}건.")
    lines += ["", "## 해석 한계", "", "기물 노출과 큰 평가 손실이 함께 나온 장면은 보상 없는 희생의 의심 사례로 분류한다. 제한된 전력 엔진 탐색이며 장기 전술의 수학적 증명은 아니다. 별도 탐색이 실제 수와 같은 최선수를 권고했다면 해당 손실의 회피 가능성도 미확정이다. 실제 material 변화에는 이후 상대의 선택과 스파링의 후속 실수도 섞일 수 있어 최초 수의 단독 원인으로 단정하지 않는다. 램프 규칙 준수는 기록된 자체/심판 점수에 대한 검사이며 독립 평가 오차와 구분한다. 독립적인 과도 양보 신호는 실제 대안 선택에서 손실이 해당 수의 램프 상한+50cp를 초과한 경우이며, 기록 규칙 위반과 별도 분류한다. 수 제한·오류·시간패는 정상 체크메이트 승패와 구분해 원본 JSON에 남긴다.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", nargs="+", type=Path, required=True)
    parser.add_argument("--stockfish", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nodes", type=int, default=20000)
    parser.add_argument("--deep-ms", type=int, default=3000)
    parser.add_argument("--max-deep-per-game", type=int, default=0, help="0 reviews every suspicious event")
    args = parser.parse_args()
    if args.nodes < 1 or args.deep_ms < 1 or args.max_deep_per_game < 0:
        parser.error("positive budgets and nonnegative deep event cap required")
    paths = []
    for path in args.games:
        paths.extend(sorted(path.glob("**/game.json")) if path.is_dir() else [path])
    paths = list(dict.fromkeys(p.resolve(strict=True) for p in paths))
    if not paths:
        parser.error("no games found")
    args.output.mkdir(parents=True, exist_ok=False)
    source_hash = sha(__file__)
    auditor = Auditor(args.stockfish.resolve(strict=True), args.nodes)
    document = {"schema": "regicide.sparring-validation-audit.v1", "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "auditor_binary": str(args.stockfish.resolve()), "auditor_sha256": sha(args.stockfish),
                "auditor_identity": auditor.identity, "auditor_options": auditor.options,
                "analyzer_sha256": source_hash, "quick_nodes": args.nodes, "deep_ms": args.deep_ms,
                "max_deep_per_game": args.max_deep_per_game, "fresh_game_each_position": True,
                "platform": platform.platform(), "python": platform.python_version(), "logical_cpus": os.cpu_count(),
                "games": [], "status": "running"}
    try:
        for i, path in enumerate(paths, 1):
            result = audit_game(path, auditor, args.output / f"audit-game-{i:03}.json", args.deep_ms, args.max_deep_per_game)
            document["games"].append({k: v for k, v in result.items() if k != "position_scores"})
            write(args.output / "summary.json", document)
            (args.output / "report.md").write_text(report(document), encoding="utf-8")
        document.update(status="completed", analyzer_unchanged=sha(__file__) == source_hash,
                        strict20_pass_games=sum(g["strict20_pass"] for g in document["games"]),
                        ramp_violations=sum(g["ramp_violation_count"] for g in document["games"]),
                        pre_safety_attempt_violations=sum(g["pre_safety_attempt_violation_count"] for g in document["games"]),
                        independent_ramp_excesses=sum(g["deep_empirical_ramp_excesses"] for g in document["games"]),
                        deep_pending=sum(g["deep_pending"] for g in document["games"]))
        write(args.output / "summary.json", document)
        (args.output / "report.md").write_text(report(document), encoding="utf-8")
    finally:
        auditor.close()
    print(json.dumps({"event": "audit_complete", "games": len(paths), "strict20_pass_games": document["strict20_pass_games"],
                      "ramp_violations": document["ramp_violations"], "deep_pending": document["deep_pending"]}), flush=True)


if __name__ == "__main__":
    main()
