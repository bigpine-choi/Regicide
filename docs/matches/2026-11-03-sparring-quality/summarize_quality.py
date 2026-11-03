"""Summarize final quality receipts separately from the legacy match audit."""
import argparse
from collections import Counter
import datetime
import hashlib
import json
from pathlib import Path

import chess


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def phase(ply):
    return "through20" if ply <= 40 else "moves21to49" if ply <= 98 else "move50plus"


def completed_searches(path):
    starts, completed = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        key = (event["engine"], event["search"])
        if event["event"] == "search":
            starts[key] = event
        elif event["event"] == "bestmove" and key in starts:
            completed.append({**starts[key], "returned": event["move"]})
    return completed


def summarize_game(directory, independent):
    game_file = directory / "game.json"
    game, metadata = read(game_file), read(directory / "metadata.json")
    searches = completed_searches(directory / "native-searches.jsonl")
    board, phases, rows, problems = chess.Board(game["initial_fen"]), {}, [], []
    native_repairs = 0
    for row in game["moves"]:
        assert row["before_fen"] == board.fen(en_passant="fen")
        move = chess.Move.from_uci(row["uci"])
        assert move in board.legal_moves
        if row["actor"] == "sparring":
            stage = phase(row["ply"])
            bucket = phases.setdefault(stage, {"moves": 0, "status": Counter(), "reasons": Counter(),
                "verified_external_close": 0, "known_external_cp": 0, "native_repair_proven": 0})
            bucket["moves"] += 1
            decision = row.get("sparring_decision") or {}
            receipt = decision.get("quality_guard") or {}
            status, reason = receipt.get("status", "missing"), receipt.get("reason", "missing")
            if not receipt:
                safety = decision.get("tactical_safety") or {}
                reason = ("preceding_tactical_failure_not_audited" if safety.get("status") == "unverified"
                          else "quality_receipt_missing")
            bucket["status"][status] += 1
            bucket["reasons"][reason] += 1
            verdict = receipt.get("final_verdict") or {}
            context_ok = (receipt.get("selected_uci") == row["uci"]
                and receipt.get("before_fen") == row["before_fen"]
                and receipt.get("history_uci") == [item.uci() for item in board.move_stack])
            truth_ok = context_ok and (status not in ("verified", "replaced") or
                verdict.get("verified") is True and verdict.get("acceptable") is True)
            if not receipt:
                truth_ok = None
            cp = verdict.get("child_cp_own")
            if verdict.get("verified") is True and type(cp) in (int, float):
                bucket["known_external_cp"] += 1
                bucket["verified_external_close"] += abs(cp) <= 100
            proof = None
            if receipt.get("applied"):
                attempts = [attempt for attempt in receipt.get("attempts", [])
                    if (attempt.get("native_evidence") or {}).get("root") == row["uci"]
                    and (attempt.get("assessment") or {}).get("selected_uci") == row["uci"]
                    and (attempt.get("verdict") or {}).get("acceptable") is True]
                matching = []
                for attempt in attempts:
                    excluded = set(attempt.get("excluded_uci", []))
                    allowed = {candidate.uci() for candidate in board.legal_moves} - excluded
                    matching.extend(search for search in searches
                        if search.get("ply") == row["ply"] - 1
                        and search.get("fen") == row["before_fen"]
                        and search.get("history_uci") == receipt["history_uci"]
                        and search.get("returned") == row["uci"]
                        and set(search.get("root_moves") or []) == allowed
                        and receipt.get("original_uci") in excluded)
                proof = bool(matching)
                if proof:
                    native_repairs += 1
                    bucket["native_repair_proven"] += 1
                else:
                    problems.append({"ply": row["ply"], "reason": "missing_native_replacement_trace"})
                truth_ok = truth_ok and status == "replaced" and receipt.get("original_uci") != row["uci"]
            elif status == "replaced":
                truth_ok = False
            if truth_ok is False:
                problems.append({"ply": row["ply"], "reason": "receipt_context_or_verdict_mismatch"})
            rows.append({"ply": row["ply"], "fullmove": board.fullmove_number,
                "phase": stage, "uci": row["uci"], "san": row["san"], "status": status,
                "reason": reason, "repair_attempted": receipt.get("repair_attempted", False),
                "applied": receipt.get("applied", False), "native_trace_proven": proof,
                "original_uci": receipt.get("original_uci"), "root_cp_own": verdict.get("root_cp_own"),
                "child_cp_own": cp, "floor_cp": verdict.get("floor_cp"),
                "tolerance_cp": verdict.get("tolerance_cp"), "acceptable": verdict.get("acceptable"),
                "verified": verdict.get("verified"), "truth_check": truth_ok})
        board.push(move)
        assert row["after_fen"] == board.fen(en_passant="fen")
    assert game["final_fen"] == board.fen(en_passant="fen")
    independently = next((item for item in independent if Path(item["game_file"]).resolve() == game_file.resolve()), None)
    large_losses = []
    for event in independently["suspicious_events"] if independently else []:
        if not event.get("deep_large_loss"):
            continue
        decision = game["moves"][event["ply"] - 1].get("sparring_decision") or {}
        final = (decision.get("quality_guard") or {}).get("final_verdict") or {}
        sign = 1 if game["sparring_color"] == "white" else -1
        a, b = (event.get("deep_before") or {}).get("cp_white"), (event.get("deep_after") or {}).get("cp_white")
        a, b = (a * sign if type(a) is int else None), (b * sign if type(b) is int else None)
        concession = (event["fullmove"] <= 20 and decision.get("selected_uci") != decision.get("baseline_uci")
            and final.get("verified") is True and final.get("acceptable") is True
            and type(a) is int and a > 100 and type(b) is int and abs(b) <= 100)
        large_losses.append({"ply": event["ply"], "fullmove": event["fullmove"], "san": event["san"],
            "cp_before_own": a, "cp_after_own": b, "loss": event["deep_loss"],
            "classification": "before20_verified_close_concession" if concession else "requires_repair_review",
            "native_baseline": decision.get("baseline_uci"), "played": decision.get("selected_uci"),
            "quality_status": (decision.get("quality_guard") or {}).get("status")})
    return {"game_file": str(game_file.resolve()), "game_sha256": sha(game_file),
        "color": game["sparring_color"], "result": game["result"], "termination": game["termination"],
        "plies": game["plies"], "completed": game["completed"], "error": game["error"],
        "legal_replay": True, "artifact_validation": game["validation"],
        "sources_unchanged": metadata["sources_unchanged"], "binaries_unchanged": metadata["binaries_unchanged"],
        "phases": phases, "applied_repairs": sum(row["applied"] for row in rows),
        "native_repairs_proven": native_repairs, "receipt_problems": problems, "moves": rows,
        "independent_large_loss_review": large_losses,
        "independent": None if independently is None else {key: independently[key] for key in (
            "early_all_roots", "early_after_sparring", "early_after_opponent", "strict20_pass",
            "reached20", "reached50", "deep_reviewed", "deep_pending", "deep_large_losses",
            "material_loss_suspects", "suspicious_events")}}


def report(document):
    lines = ["# 마지막 형세 품질 검사 실제 대국 결과", "",
        "실제 최종 수의 quality_guard 기록과 Regicide 탐색 원본을 대조했다. 기존 감사 스크립트의 선택 종류·램프 집계는 새 검사 전체의 판정에 사용하지 않았다.", "",
        "| Regicide | 결과 | 종료 | ply | 자체 착수 | 대체 | 자체 탐색 출처 확인 | 미확인 | 생략 | 검사 기록 없음 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for game in document["games"]:
        statuses = Counter(row["status"] for row in game["moves"])
        lines.append(f"| {game['color']} | {game['result']} | {game['termination']} | {game['plies']} | {len(game['moves'])} | {game['applied_repairs']} | {game['native_repairs_proven']} | {statuses['unverified']} | {statuses['skipped']} | {statuses['missing']} |")
    lines += ["", "## 20수까지의 독립 평가", ""]
    for game in document["games"]:
        audit = game["independent"]
        if audit is None:
            lines.append(f"- {game['color']}: 별도 200000노드 평가와 의심 장면 3초 재검토가 아직 포함되지 않았다.")
            continue
        own, all_roots = audit["early_after_sparring"], audit["early_all_roots"]
        lines.append(f"- {game['color']}: 초기 위치와 양측 착수 후 {all_roots['close']}/{all_roots['positions']} 위치가 ±100cp, 스파링 직후 {own['close']}/{own['positions']} 위치가 ±100cp. 20수 연속 접전 {'충족' if audit['strict20_pass'] else '미충족'}. 깊은 재검토 큰 손실 {audit['deep_large_losses']}건, 기물 손실 의심 {audit['material_loss_suspects']}건, 추가 재검토 미완료 {audit['deep_pending']}건.")
    lines += ["", "## 단계별 품질 검사", "",
        "| Regicide | 단계 | 수 | 검증 유지 | 대체 | 미확인 | 생략 | 검사 기록 없음 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for game in document["games"]:
        for stage in ("through20", "moves21to49", "move50plus"):
            item = game["phases"].get(stage, {"moves": 0, "status": {}})
            status = item["status"]
            lines.append(f"| {game['color']} | {stage} | {item['moves']} | {status.get('verified', 0)} | {status.get('replaced', 0)} | {status.get('unverified', 0)} | {status.get('skipped', 0)} | {status.get('missing', 0)} |")
    lines += ["", "미확인·생략 사유와 각 수의 외부 평가·하한·출처 증거는 quality-summary.json에 남겼다. 앞선 전술 검사 실패 후 검사 기록이 없는 수도 미실행으로 분리했다. 원본 자체 평가의 접전 표시는 외부 검증 통과로 세지 않았다.", "",
        "## 큰 평가 손실의 해석", ""]
    for game in document["games"]:
        for event in game["independent_large_loss_review"]:
            meaning = ("20수 이전 우세를 접전으로 되돌린 검증된 자체 후보 양보" if event["classification"] == "before20_verified_close_concession"
                       else "추가 수정 검토가 필요한 손실")
            lines.append(f"- {game['color']} {event['fullmove']}수 {event['san']} (ply {event['ply']}): {event['cp_before_own']}→{event['cp_after_own']}cp, {meaning}.")
    lines += ["",
        "기능 진단 2판이며 Elo·전반적인 강도·인간 체감·전술적 무오류의 증거가 아니다. 제한된 심판 평가의 검증과 독립 재평가를 구분한다.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    audit_file = args.root / "audit" / "summary.json"
    independent = read(audit_file)["games"] if audit_file.exists() else []
    document = {"schema": "regicide.sparring-quality-summary.v1",
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "independent_audit_included": bool(independent),
        "legacy_audit_limit": "Reuse independent position/deep scores only; legacy selection and ramp counts do not understand quality_guard.",
        "games": [summarize_game(args.root / f"1700-{color}", independent) for color in ("white", "black")]}
    (args.root / "quality-summary.json").write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (args.root / "README.md").write_text(report(document), encoding="utf-8")
    print(json.dumps({"games": len(document["games"]), "native_repairs_proven": sum(game["native_repairs_proven"] for game in document["games"]),
        "receipt_problems": sum(len(game["receipt_problems"]) for game in document["games"]),
        "independent_audit_included": document["independent_audit_included"]}))


if __name__ == "__main__":
    main()
