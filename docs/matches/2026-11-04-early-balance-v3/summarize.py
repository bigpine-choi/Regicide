"""Summarize every retained game and its independent audit without re-evaluation."""
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


def completed_searches(path):
    starts, completed = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        key = (event["engine"], event["search"])
        if event["event"] == "search":
            starts[key] = {**event, "packets": []}
        elif event["event"] == "packet" and key in starts:
            starts[key]["packets"].append(event.get("packet") or {})
        elif event["event"] == "bestmove" and key in starts:
            completed.append({**starts[key], "returned": event["move"]})
    return completed


def packet_matches(search, evidence, uci):
    if not isinstance(evidence, dict) or evidence.get("root") != uci:
        return False
    for packet in search["packets"]:
        pv, score = packet.get("pv"), packet.get("score") or {}
        if (packet.get("depth") == evidence.get("depth") and isinstance(pv, list)
                and pv and pv[0] == uci and not packet.get("lowerbound") and not packet.get("upperbound")
                and score.get("cp_white") == evidence.get("cp")
                and score.get("mate_white") == evidence.get("mate")):
            return True
    return False


def balance_proof(receipt, generated, uci):
    balance = receipt.get("balance") or {}
    proof = []
    for attempt in balance.get("attempts", []):
        evidence = attempt.get("native_evidence") or {}
        assessed = attempt.get("assessment") or {}
        verdict = attempt.get("verdict") or {}
        if (attempt.get("uci") != uci or evidence.get("root") != uci or evidence.get("depth", 0) < 4
                or not (attempt.get("naturalness") or {}).get("admitted")
                or assessed.get("selected_uci") != uci or assessed.get("fen") != receipt.get("before_fen")
                or assessed.get("history_uci") != receipt.get("history_uci")
                or verdict.get("verified") is not True or verdict.get("acceptable") is not True):
            continue
        matching = [search for search in generated if search.get("root_moves") == [uci]
                    and packet_matches(search, evidence, uci)]
        promotion = attempt.get("native_promotion")
        if promotion is not None and (promotion.get("allowed_uci") != [uci]
                or promotion.get("evidence") != evidence):
            matching = []
        if matching:
            proof.append({"uci": uci, "native_depth": evidence["depth"],
                          "naturalness": attempt["naturalness"], "promoted": promotion is not None,
                          "searches": [{k: search.get(k) for k in ("engine", "search", "root_moves", "requested_seconds", "requested_depth")}
                                       for search in matching]})
    return proof


def confirmed_shallow_proof(receipt, generated, uci):
    proofs = []
    original = receipt.get("initial_verdict") or {}
    for attempt in receipt.get("attempts", []):
        if attempt.get("evidence_kind") != "reference_confirmed_native_candidate":
            continue
        discovery, promoted = attempt.get("discovery_evidence") or {}, attempt.get("native_evidence") or {}
        if promoted.get("root") != uci:
            continue
        confirmation = attempt.get("reference_confirmation") or {}
        promotion = attempt.get("native_promotion") or {}
        first, second = attempt.get("assessment") or {}, confirmation.get("assessment") or {}
        verdicts = attempt.get("verdict") or {}, confirmation.get("verdict") or {}
        valid = (len(receipt.get("history_uci", [])) < 40
                 and original.get("verified") is True and original.get("acceptable") is False
                 and type(original.get("child_cp_own")) in (int, float) and original["child_cp_own"] < -100
                 and confirmation.get("verified") is True
                 and (confirmation.get("naturalness") or {}).get("admitted") is True
                 and promotion.get("status") == "reference_confirmed"
                 and promotion.get("allowed_uci") == [uci] and promotion.get("evidence") == promoted
                 and len(attempt.get("allowed_uci", [])) >= 2)
        for evidence in (discovery, promoted):
            valid = valid and evidence.get("root") == uci and 2 <= evidence.get("depth", 0) < 4 and type(evidence.get("cp")) is int and evidence.get("mate") is None
        if valid:
            valid = (abs(discovery["cp"] - promoted["cp"]) <= 40
                     and confirmation.get("native_score_delta_cp") == abs(discovery["cp"] - promoted["cp"])
                     and confirmation.get("native_completed_depths") == [discovery["depth"], promoted["depth"]])
        child_scores, root_scores = [], []
        initial_root = (receipt.get("initial") or {}).get("root") or {}
        root_scores.append(initial_root.get("cp_own"))
        for assessment, verdict in zip((first, second), verdicts):
            valid = valid and (assessment.get("status") == "ready" and assessment.get("selected_uci") == uci
                and assessment.get("fen") == receipt.get("before_fen") and assessment.get("history_uci") == receipt.get("history_uci")
                and verdict.get("verified") is True and verdict.get("acceptable") is True)
            child_scores.append(verdict.get("child_cp_own"))
            root_scores.append((assessment.get("root") or {}).get("cp_own"))
            for evidence in (assessment.get("root") or {}, assessment.get("child") or {}):
                valid = valid and evidence.get("bound") == "exact" and evidence.get("depth", 0) >= 10 and evidence.get("winner") is None
        if valid:
            valid = (all(type(cp) in (int, float) for cp in root_scores + child_scores)
                     and all(abs(cp) <= 80 for cp in child_scores)
                     and child_scores == [confirmation.get("first_child_cp_own"), confirmation.get("second_child_cp_own")]
                     and abs(child_scores[0] - child_scores[1]) <= 60
                     and max(root_scores) - min(root_scores) <= 100
                     and abs(root_scores[1] - root_scores[2]) <= 60)
        discovery_searches = [s for s in generated if set(s.get("root_moves") or []) == set(attempt.get("allowed_uci", []))
                              and packet_matches(s, discovery, uci)]
        promotion_searches = [s for s in generated if s.get("root_moves") == [uci] and packet_matches(s, promoted, uci)]
        valid = bool(valid and discovery_searches and promotion_searches)
        proofs.append({"uci": uci, "verified": valid,
                       "kind": "reference_confirmed_native_candidate", "native_depths": [discovery.get("depth"), promoted.get("depth")],
                       "child_scores_own": child_scores, "root_scores_own": root_scores,
                       "native_discovery_searches": [s["search"] for s in discovery_searches],
                       "native_promotion_searches": [s["search"] for s in promotion_searches],
                       "scope": "separately labelled shallow native proposal with two external confirmations"})
    return proofs


def pending_exchange_proof(board, move, natural):
    proof = natural.get("pending_exchange") if isinstance(natural, dict) else None
    if not isinstance(proof, dict):
        return None
    valid = (2 <= len(board.move_stack) < 40 and board.root().fen(en_passant="fen") == chess.STARTING_FEN
             and natural.get("admitted") is True and natural.get("reason") == "pending_equal_exchange"
             and natural.get("material_return_exception") is False and not board.is_capture(move)
             and not board.gives_check(move) and not move.promotion and not board.is_check())
    previous = board.copy(stack=True)
    declined, captured = previous.pop(), previous.pop()
    piece, victim = previous.piece_at(captured.from_square), previous.piece_at(captured.to_square)
    values = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
              chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
    valid = valid and (proof.get("capture_uci") == captured.uci() and proof.get("declined_reply_uci") == declined.uci()
        and proof.get("capture_ply") == len(board.move_stack) - 1 and previous.is_capture(captured)
        and not captured.promotion and piece is not None and victim is not None
        and piece.color == board.turn and piece.piece_type not in (chess.PAWN, chess.KING)
        and proof.get("maximum_exchange_credit_cp") == values[piece.piece_type]
        and proof.get("captured_value_cp") == values[victim.piece_type]
        and values[victim.piece_type] >= values[piece.piece_type]
        and proof.get("piece_square") == chess.square_name(captured.to_square) and proof.get("piece") == piece.symbol()
        and move.from_square != captured.to_square and board.piece_at(captured.to_square) == piece)
    previous.push(captured)
    replies = {m.uci() for m in previous.legal_moves if m.to_square == captured.to_square and previous.is_capture(m)}
    after = board.copy(stack=True)
    after.push(move)
    after_replies = {m.uci() for m in after.legal_moves if m.to_square == captured.to_square and after.is_capture(m)}
    existing = board.copy(stack=False)
    existing.turn = not board.turn
    existing.ep_square = None
    existing_replies = {m.uci() for m in existing.legal_moves if m.to_square == captured.to_square and existing.is_capture(m)}
    permitted = set(proof.get("permitted_replies", []))
    strict = natural.get("strict_naturalness") or {}
    vulnerabilities = strict.get("vulnerable_captures") or []
    if "existing_legal_recaptures" in proof:
        # v4 permits an attack changed by the opponent's already-played quiet
        # reply, while the current candidate still creates no new capture.
        recapture_identity = (set(proof["existing_legal_recaptures"]) == existing_replies
            and permitted.issubset(after_replies & existing_replies)
            and sorted(proof.get("opponent_changed_recapture", [])) == sorted(permitted - replies))
        proof_version = "opponent_changed_recapture"
    else:
        recapture_identity = permitted.issubset(replies & after_replies & existing_replies)
        proof_version = "same_original_recapture"
    valid = bool(valid and not previous.is_capture(declined) and not declined.promotion
                 and replies and replies == set(proof.get("original_legal_recaptures", [])) and permitted
                 and recapture_identity
                 and after.piece_at(captured.to_square) == piece
                 and strict.get("admitted") is False and strict.get("reason") == "obvious_material_donation"
                 and {r.get("reply_uci") for r in vulnerabilities} == permitted
                 and all(r.get("victim_cp") == proof.get("maximum_exchange_credit_cp")
                         and r.get("net_material_cp", -10000) >= -proof.get("maximum_exchange_credit_cp", 0)
                         and (r.get("recapture_exchange") or {}).get("complete") is True for r in vulnerabilities))
    return {"verified": valid, "proof_version": proof_version, "proof": proof,
            "scope": "replayed declined exchange and opponent's existing attack; no candidate-created material concession"}


def stage(ply):
    return "through20" if ply <= 40 else "moves21to49" if ply <= 98 else "move50plus"


def independent_map(root):
    # Full audit-game files keep position_scores; collection summaries omit them.
    rows = {}
    for path in sorted(root.glob("audit*/audit-game-*.json")):
        value = read(path)
        key = str(Path(value["game_file"]).resolve())
        if key in rows and rows[key]["game_sha256"] != value["game_sha256"]:
            raise ValueError("Conflicting independent game identity")
        rows[key] = {**value, "audit_file": str(path.resolve()), "audit_sha256": sha(path)}
    return rows


def summarize(directory, independent, cohort):
    game_path = directory / "game.json"
    game, metadata = read(game_path), read(directory / "metadata.json")
    audit = independent.get(str(game_path.resolve()))
    if audit is not None and audit["game_sha256"] != sha(game_path):
        raise ValueError("Audit/game hash mismatch")
    scores = {row["ply"]: row for row in audit["position_scores"]} if audit else {}
    searches = completed_searches(directory / "native-searches.jsonl")
    board = chess.Board(game["initial_fen"])
    rows, phases = [], {}
    own_white = game["sparring_color"] == "white"
    sign = 1 if own_white else -1
    for turn in game["moves"]:
        assert turn["ply"] == len(board.move_stack) + 1
        assert turn["before_fen"] == board.fen(en_passant="fen")
        move = chess.Move.from_uci(turn["uci"])
        assert move in board.legal_moves
        if turn["actor"] == "sparring":
            decision = turn.get("sparring_decision") or {}
            receipt = decision.get("quality_guard") or {}
            verdict = receipt.get("final_verdict") or {}
            context = (receipt.get("selected_uci") == turn["uci"]
                and receipt.get("before_fen") == turn["before_fen"]
                and receipt.get("history_uci") == [m.uci() for m in board.move_stack]) if receipt else None
            generated = [s for s in searches if s.get("ply") == len(board.move_stack)
                and s.get("fen") == turn["before_fen"] and s.get("returned") == turn["uci"]
                and s.get("history_uci") == [m.uci() for m in board.move_stack]]
            balanced_proof = balance_proof(receipt, generated, turn["uci"]) if receipt.get("status") in ("balanced", "balance_improved") else None
            shallow_proof = confirmed_shallow_proof(receipt, generated, turn["uci"])
            natural = verdict.get("naturalness") or {}
            if not natural.get("pending_exchange"):
                natural = next((attempt.get("naturalness") for attempt in (receipt.get("balance") or {}).get("attempts", [])
                                if attempt.get("uci") == turn["uci"] and (attempt.get("naturalness") or {}).get("pending_exchange")), {})
            exchange_proof = pending_exchange_proof(board, move, natural)
            selected_natural = verdict.get("naturalness") or next((attempt.get("naturalness")
                for attempt in (receipt.get("balance") or {}).get("attempts", []) if attempt.get("uci") == turn["uci"]), None) or decision.get("naturalness")
            before, after = scores.get(turn["ply"] - 1, {}), scores.get(turn["ply"], {})
            a, b = before.get("cp_white"), after.get("cp_white")
            a, b = (a * sign if type(a) is int else None), (b * sign if type(b) is int else None)
            if type(b) is not int:
                category = "mate_terminal_or_unknown"
            elif abs(b) <= 100:
                category = "close"
            elif b > 100:
                category = "advantage_remains"
            else:
                category = "disadvantage_remains"
            row = {"ply": turn["ply"], "fullmove": board.fullmove_number,
                "uci": turn["uci"], "san": turn["san"], "phase": stage(turn["ply"]),
                "cp_before_own": a, "cp_after_own": b, "category": category,
                "prior_position_outside_band": abs(a) > 100 if type(a) is int else None,
                "quality_status": receipt.get("status", "missing"),
                "quality_reason": receipt.get("reason"), "receipt_context_matches": context,
                "selected_naturalness": selected_natural, "decision_naturalness": decision.get("naturalness"),
                "quality_applied": receipt.get("applied", False),
                "quality_final_verdict": verdict,
                "quality_balance": receipt.get("balance"),
                "quality_repair_attempts": receipt.get("attempts", []),
                "balanced_native_promotion_proof": balanced_proof,
                "reference_confirmed_shallow_proof": shallow_proof,
                "pending_exchange_proof": exchange_proof,
                "native_returned_move_proven": bool(generated),
                "native_matching_searches": [{k: s.get(k) for k in ("engine", "search", "root_moves", "requested_seconds")} for s in generated]}
            rows.append(row)
            bucket = phases.setdefault(row["phase"], {"moves": 0, "statuses": Counter(), "categories": Counter()})
            bucket["moves"] += 1
            bucket["statuses"][row["quality_status"]] += 1
            bucket["categories"][category] += 1
        board.push(move)
        assert turn["after_fen"] == board.fen(en_passant="fen")
    assert game["final_fen"] == board.fen(en_passant="fen")
    deep_reviews = []
    for event in audit["suspicious_events"] if audit else []:
        if not event.get("deep_large_loss"):
            continue
        row = next(r for r in rows if r["ply"] == event["ply"])
        before, after = event.get("deep_before") or {}, event.get("deep_after") or {}
        a, b = before.get("cp_white"), after.get("cp_white")
        a, b = (a * sign if type(a) is int else None), (b * sign if type(b) is int else None)
        natural = row.get("selected_naturalness") or {}
        verdict = row["quality_final_verdict"]
        safe_concession = (row["ply"] <= 40 and type(a) is int and a > 100 and type(b) is int and abs(b) <= 100
            and verdict.get("verified") is True and verdict.get("acceptable") is True
            and natural.get("admitted") is True and natural.get("material_return_exception") is False
            and not natural.get("vulnerable_captures") and not event.get("immediate_exposure")
            and event.get("minimum_actual_material_swing_next4plies", 0) > -200)
        mate = before.get("mate_white")
        lost_mate_with_advantage = type(mate) is int and mate * sign > 0 and type(b) is int and b >= 300
        deep_reviews.append({"ply": row["ply"], "san": row["san"], "cp_before_own": a, "cp_after_own": b,
            "raw_classification": event["classification"],
            "classification": "verified_non_material_balance_concession" if safe_concession else
                              "winning_mate_lost_with_large_advantage" if lost_mate_with_advantage else "requires_review",
            "quality_status": row["quality_status"], "naturalness": natural,
            "immediate_exposure": event.get("immediate_exposure"),
            "minimum_actual_material_swing_next4plies": event.get("minimum_actual_material_swing_next4plies")})
    return {"cohort": cohort, "label": directory.name, "game_file": str(game_path.resolve()),
        "game_sha256": sha(game_path), "metadata_sha256": sha(directory / "metadata.json"),
        "source_fingerprint": hashlib.sha256(json.dumps(metadata["source_sha256"], sort_keys=True).encode()).hexdigest()[:12],
        "quality_policies": sorted({((r.get("sparring_decision") or {}).get("quality_guard") or {}).get("policy", "missing")
                                    for r in game["moves"] if r["actor"] == "sparring"}),
        "color": game["sparring_color"], "seed_input": metadata["config"]["seed_input"],
        "started_at": game.get("started_at"),
        "plies": game["plies"], "result": game["result"], "termination": game["termination"],
        "completed": game["completed"], "error": game["error"], "legal_replay": True,
        "artifact_validation": game["validation"], "sources_unchanged": metadata["sources_unchanged"],
        "binaries_unchanged": metadata["binaries_unchanged"], "phases": phases, "moves": rows,
        "receipt_identity_problems": [r["ply"] for r in rows if r["receipt_context_matches"] is False],
        "applied_replacement_native_trace_missing": [r["ply"] for r in rows if r["quality_applied"] and not r["native_returned_move_proven"]],
        "balanced_native_promotion_trace_missing": [r["ply"] for r in rows if r["balanced_native_promotion_proof"] == []],
        "reference_confirmed_shallow_problems": [r["ply"] for r in rows if any(not proof["verified"] for proof in r["reference_confirmed_shallow_proof"])],
        "pending_exchange_proof_problems": [r["ply"] for r in rows if r["pending_exchange_proof"] and not r["pending_exchange_proof"]["verified"]],
        "deep_large_loss_review": deep_reviews,
        "early_outside_rows": [r for r in rows if r["ply"] <= 40 and r["category"] != "close"],
        "independent": None if audit is None else {key: audit[key] for key in (
            "audit_file", "audit_sha256", "early_all_roots", "early_after_sparring", "early_after_opponent",
            "strict20_pass", "reached20", "reached50", "deep_reviewed", "deep_pending",
            "deep_large_losses", "material_loss_suspects", "suspicious_events")}}


def report(document):
    lines = ["# 초반 접전 실제 대국 재검증", "", "이전 세 대국을 보존하고 새 대국을 별도 표본으로 표시한다. 상대의 확률적 선택 때문에 동일 수순이나 승률의 직접 전후 비교로 해석하지 않는다.", "",
        "| 표본 | 대국 | 색 | ply | 종료 | 20수 도달 | 스파링 직후 접전 | 양측 전체 접전 | 20수 연속 접전 |", "|---|---|---|---:|---|---|---|---|---|"]
    for game in document["games"]:
        audit = game["independent"]
        if audit:
            own, every = audit["early_after_sparring"], audit["early_all_roots"]
            values = ("예" if audit["reached20"] else "아니오", f"{own['close']}/{own['positions']}",
                f"{every['close']}/{every['positions']}", "충족" if audit["strict20_pass"] else "미충족")
        else:
            values = ("감사 대기",) * 4
        lines.append(f"| {game['cohort']} | {game['label']} | {game['color']} | {game['plies']} | {game['termination']} | {' | '.join(values)} |")
    groups = {}
    for game in document["games"]:
        groups.setdefault(game["source_fingerprint"], []).append(game)
    lines += ["", "## 실행 소스 구분", ""]
    for fingerprint, games in groups.items():
        lines.append(f"- `{fingerprint}`: {', '.join(g['label'] for g in games)}. 기록 정책 {', '.join(sorted({p for g in games for p in g['quality_policies']}))}. 전체 파일별 해시는 각 metadata.json에 보존했다.")
    latest = document.get("latest_implementation")
    if latest:
        lines += ["", "## 가장 최근 구현의 대국", "",
            f"소스 `{latest['source_fingerprint']}`의 {latest['games']}판·{latest['plies']}ply를 별도로 본다. 이전 중간 구현의 실패 대국도 위 표에 남겼다. 독립 감사는 {latest['audited_games']}/{latest['games']}판 완료했다. 완료한 감사에서 스파링 직후 접전은 {latest['early_after_sparring_close']}/{latest['early_after_sparring_positions']}이며, 양측 모든 위치의 20수 연속 접전은 {latest['strict20_pass_games']}/{latest['audited_games']}판이다. 독립 기물 손실 원시 의심 신호 {latest['material_loss_suspects']}건, 심층 미완료 {latest['deep_pending']}건이다. 원본 큰 손실 신호에는 안전한 우세 반환과 메이트 거리 소실도 포함되므로 아래 해석과 함께 읽는다.", "",
            f"실제 대체 수 {latest['applied_replacements']}건의 native 반환검색을 대조했고, 영수증 위치 불일치 {latest['receipt_identity_problems']}건·출처 누락 {latest['native_trace_problems']}건이었다. 모든 실행은 해당 시점의 소스·실행 파일 해시가 불변인 조건이다."]
    lines += ["", "## 새 표본의 접전 이탈", ""]
    for game in document["games"]:
        if game["cohort"] != "new":
            continue
        lines.append(f"- {game['label']}: 초반 이탈·메이트·종국·미확인 {len(game['early_outside_rows'])}회; 영수증 위치 불일치 {len(game['receipt_identity_problems'])}회; 적용 대체 수 native 반환 근거 누락 {len(game['applied_replacement_native_trace_missing'])}회.")
        for row in game["early_outside_rows"]:
            lines.append(f"  - {row['fullmove']}수 {row['san']} (ply {row['ply']}): {row['cp_before_own']}→{row['cp_after_own']}cp, {row['category']}, 실행 중 {row['quality_status']}.")
        for review in game["deep_large_loss_review"]:
            lines.append(f"  - 심층 손실 재검토 ply {review['ply']} {review['san']}: {review['cp_before_own']}→{review['cp_after_own']}cp, {review['classification']} (원래 신호 {review['raw_classification']}).")
    lines += ["", "## 안전·해석", "",
        "±100cp 접전, 승리/패배 메이트, 종국과 미확인은 별도다. 20수 이전 종료는 연속 접전 통과가 아니다. 상대의 실수로 이미 생긴 격차를 실제 스파링 착수 오류로 단정하지 않는다. 기물 노출·다음 4ply 기물 감소와 평가 손실의 결합은 추가 수순 검토가 필요한 신호이며 보상 없는 헌납의 확정 판정이 아니다. 각 대국의 원본 감사와 실행 기록을 함께 보관한다.", "",
        "각 대국은 실제 HumanGame의 수 선택·시계를 실행한 자동 대국이다. 화면을 수동 조작한 검증, Elo, 보편적 접전 보장 또는 기물 헌납 완전 차단의 증거가 아니다.", ""]
    if document.get("manual_reviews"):
        lines += ["## 별도 수순 재검토", "",
            "pair3-1700-white의 15.Bxb5(ply 29)는 실행 중 엄격 검사에서 기존 Na5의 노출 때문에 거부됐고 원래 unverified 상태를 보존한다. 별도 합법 수순 열거에서 `Bxb5 Qxa5 Bxd7+` 뒤 모든 합법 왕 응수에 `Bxc8`로 룩을 회수하며 상대 재포획 뒤에도 시작 대비 기물 점수 +100cp임을 확인했다. 따라서 해당 국소 신호를 보상 없는 헌납으로 확정하지 않는다.", ""]
        if any(Path(item["path"]).name == "ra7-material-review.json" for item in document["manual_reviews"]):
            lines += ["pair4-1700-black의 12...Ra7(ply 24)에서 원시 기물 감소 신호 1건을 보존했다. 실제 `13.e5 Nxe5 14.Nxe5 Bxe6`의 나이트·두 폰 교환 사이 최저값이 −200cp였고, 흑의 다음 착수까지 순감소는 −100cp였다. 중간에 합법 ...dxe5 회수 기회가 있었으며 독립 형세는 교환 중 흑 +66→+45→+31cp로 유지됐다. 이 수순을 보상 없는 기물 헌납으로 확정하지 않는다. 실제 재료 변화와 평가를 함께 기록한 아래 별도 검토를 참조한다.", ""]
        for item in document["manual_reviews"]:
            lines.append(f"- [{Path(item['path']).name}]({item['relative_link']}), SHA256 `{item['sha256']}`")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--previous", type=Path, default=Path(__file__).resolve().parent.parent / "2026-11-04-sparring-quality-v2")
    args = parser.parse_args()
    games = []
    for cohort, root in (("previous", args.previous), ("new", args.root)):
        independent = independent_map(root)
        for directory in sorted(p.parent for p in root.glob("*/game.json")):
            games.append(summarize(directory, independent, cohort))
    document = {"schema": "regicide.early-balance-summary.v1", "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "games": games, "new_games": sum(g["cohort"] == "new" for g in games),
        "previous_games_retained": sum(g["cohort"] == "previous" for g in games)}
    new_games = [game for game in games if game["cohort"] == "new"]
    if new_games:
        latest_fingerprint = max(new_games, key=lambda game: game["started_at"])["source_fingerprint"]
        latest_games = [game for game in new_games if game["source_fingerprint"] == latest_fingerprint]
        audited = [game for game in latest_games if game["independent"] is not None]
        document["latest_implementation"] = {"source_fingerprint": latest_fingerprint, "games": len(latest_games),
            "labels": [game["label"] for game in latest_games], "plies": sum(game["plies"] for game in latest_games),
            "audited_games": len(audited), "early_after_sparring_close": sum(game["independent"]["early_after_sparring"]["close"] for game in audited),
            "early_after_sparring_positions": sum(game["independent"]["early_after_sparring"]["positions"] for game in audited),
            "strict20_pass_games": sum(game["independent"]["strict20_pass"] for game in audited),
            "material_loss_suspects": sum(game["independent"]["material_loss_suspects"] for game in audited),
            "deep_pending": sum(game["independent"]["deep_pending"] for game in audited),
            "applied_replacements": sum(sum(row["quality_applied"] for row in game["moves"]) for game in latest_games),
            "receipt_identity_problems": sum(len(game["receipt_identity_problems"]) for game in latest_games),
            "native_trace_problems": sum(len(game["applied_replacement_native_trace_missing"]) for game in latest_games)}
    reviews = []
    for name in ("bxb5-material-review.md", "bxb5-material-review.json", "final-native-review.md", "final-native-cross-check.json",
                 "final-v4-native-review.md", "final-v4-native-cross-check.json"):
        path = args.root.parent.parent / "checks/2026-11-04-early-balance/diagnostics" / name
        if path.exists():
            reviews.append({"path": str(path.resolve()), "sha256": sha(path),
                            "relative_link": "../../checks/2026-11-04-early-balance/diagnostics/" + name})
    for name in ("ra7-material-review.md", "ra7-material-review.json"):
        path = args.root / name
        if path.exists():
            reviews.append({"path": str(path.resolve()), "sha256": sha(path), "relative_link": name})
    document["manual_reviews"] = reviews
    (args.root / "summary.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.root / "README.md").write_text(report(document), encoding="utf-8")
    print(json.dumps({k: v for k, v in document.items() if k != "games"}))


if __name__ == "__main__":
    main()
