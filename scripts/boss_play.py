"""Speculative opening gambits and independently verified capture traps.

The boss may choose a worse move against correct defense to offer a tempting
capture. Search scores disclose that cost; they do not cap it. Within-game
move-quality evidence changes only offer frequency, never the engine budget
or a claimed Elo rating. Conditional punishment is not a forced win.
"""

import re
import copy
import threading
import time

import chess
import chess.engine

import boss_openings
import boss_opponent
import gambit_initiative
import gambit_knowledge
import gambit_learning
import gambit_repertoire_play
import gambit_operations
import gambit_practice_memory
import opening_variety as opening
import rating_evidence

POLICY = "regicide-boss-play-v7"
SUPPORTED_POLICIES = ("regicide-boss-play-v3", "regicide-boss-play-v4", "regicide-boss-play-v5", "regicide-boss-play-v6", POLICY)
MAX_PLIES = 16
MIN_TOTAL_MS = 500
MIN_DEPTH = 4
MIN_PROBE_MS = 20
PROBE_DIVISOR = 1
MAX_EXTRA_MS = 1000
TRAP_MAX_PLIES = 8
RISK_PROFILE = "speculative"
QUALITY_BUDGET_MS = 225
SETUP_MAX_LOSS_CP = 35


def _rows(decisions):
    if isinstance(decisions, dict):
        values = decisions.values()
    elif isinstance(decisions, (list, tuple)):
        values = decisions
    else:
        return []
    return [row for row in values if isinstance(row, dict)]


def commitment(decisions):
    selected = [row for row in _rows(decisions)
                if row.get("line_id") and row.get("selected_uci") == row.get("candidate_uci")]
    selected.sort(key=lambda row: row["ply"] if type(row.get("ply")) is int else 0)
    return (selected[-1]["line_id"] if selected else None,
            any(row.get("line_complete") is True for row in selected))


def _effective_commitment(board, decisions):
    line_id, complete = commitment(decisions)
    if line_id and not complete:
        history = [move.uci() for move in board.move_stack]
        source = next((row for row in boss_openings.repertoire() if row["line_id"] == line_id), None)
        if not source or source["moves"][:len(history)] != history:
            line_id = None
    return line_id, complete


def _trap_position(board):
    return (not board.chess960 and board.is_valid() and not board.is_game_over(claim_draw=True)
            and not board.is_check() and not opening.has_repetition(board)
            and board.legal_moves.count() > 1 and len(board.piece_map()) >= 10
            and board.root().fen(en_passant="fen") == chess.STARTING_FEN)


def phase(board, decisions=(), forced=False, engine_color=None, budget_ms=None):
    line_id, complete = _effective_commitment(board, decisions)
    if (forced or board.chess960 or not board.is_valid() or board.is_game_over(claim_draw=True)
            or board.is_check() or opening.has_repetition(board)):
        return "full_strength"
    if budget_ms is not None and budget_ms < MIN_TOTAL_MS:
        return "full_strength"
    if board.root().fen(en_passant="fen") != chess.STARTING_FEN:
        return "full_strength"
    history = [move.uci() for move in board.move_stack]
    for row in boss_openings.repertoire():
        if complete:
            break
        if line_id and row["line_id"] != line_id:
            continue
        if engine_color is not None and (row["color"] == "white") != engine_color:
            continue
        if len(history) < len(row["moves"]) and row["moves"][:len(history)] == history:
            return "opening"
    attack = gambit_initiative.context(board, decisions)
    if (attack.get("active") or attack.get("reason") == "opponent_to_move") and (
            engine_color is None or attack.get("owner") == ("white" if engine_color else "black")):
        return "initiative"
    if (engine_color is None or engine_color == board.turn) and gambit_knowledge.proposals(board, "phase", decisions=decisions):
        return "initiative"
    operation = gambit_operations.context(board, decisions)
    if (operation.get("active") or operation.get("reason") == "opponent_to_move") and (
            engine_color is None or operation.get("owner") == ("white" if engine_color else "black")):
        return "initiative"
    return "trap_hunt" if _trap_position(board) else "full_strength"


def extra_budget(budget_ms, *, ply=0):
    return min(budget_ms, MAX_EXTRA_MS) if budget_ms >= MIN_TOTAL_MS else 0


def memo_key(board, budget_ms, binary_sha256, evaluation_id, *, seed="", catalog_id=None):
    return opening._digest([POLICY, boss_openings.POLICY, boss_opponent.POLICY, gambit_initiative.POLICY,
                            gambit_knowledge.metadata().get("catalog_sha256"), gambit_learning.model_identity(),
                            gambit_operations.metadata().get("curriculum_sha256"), gambit_practice_memory.memory_identity(),
                            seed, opening.history_key(board), budget_ms,
                            binary_sha256, evaluation_id, catalog_id])


def plan(board, budget_ms, seed, *, forced=False, decisions=(), engine_identity=None):
    line_id, complete = _effective_commitment(board, decisions)
    # A shared preparation move is a prospective route, not a promise that
    # the opponent will follow that line. Reconsider every matching route
    # until an actual defining gambit move has been selected.
    if not complete:
        line_id = None
    result = {"eligible": False, "reason": "outside_repertoire", "candidate_uci": None, "mode": "full_strength",
              "candidates": [], "candidate_order": [], "proposal_limit": 1,
              "candidate_source": "authored_boss_repertoire", "catalog_commit": line_id,
              "baseline_budget_ms": budget_ms, "candidate_budget_ms": 0,
              "extra_budget_ms": 0, "line_id": None, "opening_name": None,
              "line_complete": False}
    if complete:
        result["reason"] = "signature_complete"
    elif forced:
        result["reason"] = "user_forced_game"
    elif board.chess960 or not board.is_valid() or board.root().fen(en_passant="fen") != chess.STARTING_FEN:
        result["reason"] = "custom_root"
    elif board.is_game_over(claim_draw=True):
        result["reason"] = "terminal"
    elif board.is_check():
        result["reason"] = "in_check"
    elif opening.has_repetition(board):
        result["reason"] = "repetition"
    elif budget_ms < MIN_TOTAL_MS:
        result["reason"] = "short_budget"
    elif len(board.move_stack) >= MAX_PLIES or board.legal_moves.count() <= 1:
        result["reason"] = "forced_or_late"
    else:
        rows = boss_openings.proposals(board, seed)
        if line_id:
            # The repertoire deduplicates equal next moves across named lines;
            # retain the selected line's identity when a shared move is returned.
            history = [move.uci() for move in board.move_stack]
            source = next((row for row in boss_openings.repertoire() if row["line_id"] == line_id), None)
            legal_proposals = {row["uci"] for row in rows}
            rows = ([{"uci": source["moves"][len(history)], "line_id": line_id, "name": source["name"]}]
                    if source and len(history) < len(source["moves"]) and source["moves"][:len(history)] == history
                    and source["moves"][len(history)] in legal_proposals else [])
        if rows:
            candidate = rows[0]
            source = next(row for row in boss_openings.repertoire() if row["line_id"] == candidate["line_id"])
            extra = extra_budget(budget_ms, ply=len(board.move_stack))
            result.update(eligible=True, mode="opening", reason="verify_authored_opening", candidate_uci=candidate["uci"],
                          candidates=[candidate["uci"]], candidate_order=[candidate["uci"]],
                          candidate_budget_ms=extra, extra_budget_ms=extra,
                          line_id=candidate["line_id"], opening_name=candidate["name"],
                          line_complete=len(board.move_stack) + 1 == len(source["moves"]),
                          catalog_commit=candidate["line_id"])
    if (not result["eligible"] and result["reason"] in ("outside_repertoire", "signature_complete", "forced_or_late")
            and not forced and budget_ms >= MIN_TOTAL_MS):
        attack = gambit_initiative.context(board, decisions)
        if attack.get("active"):
            extra = extra_budget(budget_ms)
            result.update(eligible=True, mode="initiative", reason="continue_gambit_attack",
                          candidate_source="native_gambit_initiative",
                          catalog_commit=[attack["line_id"], attack.get("stage"), attack.get("entry_ply")],
                          initiative_context=attack, candidate_budget_ms=extra, extra_budget_ms=extra)
    if ((not result["eligible"] and result["reason"] in ("outside_repertoire", "signature_complete", "forced_or_late")
         or result.get("mode") == "initiative") and not forced and budget_ms >= MIN_TOTAL_MS):
        rows = gambit_knowledge.proposals(board, seed, decisions=decisions)
        if rows:
            rows, ranking = gambit_learning.rank_with_metadata(board, rows, engine_identity=engine_identity)
            extra = extra_budget(budget_ms)
            result.update(eligible=True, mode="initiative", candidate_source="gambit_knowledge",
                          knowledge_candidates=rows, knowledge_ranking=ranking,
                          knowledge_context=gambit_knowledge.context(board, decisions),
                          candidate_budget_ms=extra, extra_budget_ms=extra)
            if result["reason"] in ("outside_repertoire", "signature_complete", "forced_or_late"):
                result.update(reason="verify_gambit_knowledge", catalog_commit=rows[0]["catalog_sha256"])
    if ((not result["eligible"] and result["reason"] in ("outside_repertoire", "signature_complete", "forced_or_late")
         or result.get("mode") == "initiative") and not forced and budget_ms >= MIN_TOTAL_MS):
        operation = gambit_operations.context(board, decisions)
        if operation.get("active"):
            memory = gambit_practice_memory.proposals(board, seed, engine_identity=engine_identity,
                                                       context=operation, limit=4)
            native = gambit_operations.proposals(board, seed, context_value=operation, limit=4)
            rows, seen = [], set()
            for row in [*memory, *result.get("knowledge_candidates", []), *native]:
                if row["uci"] not in seen:
                    rows.append(row)
                    seen.add(row["uci"])
            if rows:
                extra = extra_budget(budget_ms)
                result.update(eligible=True, mode="initiative", reason="practice_post_sacrifice_operations",
                    candidate_source="post_sacrifice_operations", knowledge_candidates=rows[:4],
                    operations_context=operation, operations_engine_identity=copy.deepcopy(engine_identity),
                    practice_memory=gambit_practice_memory.memory_identity(),
                    candidate_budget_ms=extra, extra_budget_ms=extra,
                    catalog_commit=[operation["curriculum_sha256"], operation["context_id"]])
    if (not result["eligible"] and result["reason"] in ("outside_repertoire", "signature_complete", "forced_or_late")
            and not forced and budget_ms >= MIN_TOTAL_MS and _trap_position(board)):
        extra = extra_budget(budget_ms)
        result.update(eligible=True, mode="trap_hunt", reason="verify_poisoned_piece", candidate_source="poisoned_piece",
                      catalog_commit="poisoned-piece-v1", candidate_budget_ms=extra, extra_budget_ms=extra)
    return result


def proposals(search_plan, baseline_move):
    return [uci for uci in search_plan["candidate_order"] if uci != baseline_move.uci()]


def apply_frequency(search_plan, frequency, baseline_move, *, probe_failed=False):
    """Keep authored gambit entry separate from adaptive poisoned-piece offers.

    Every legal repertoire proposal still needs the ordinary native evidence.
    Preparation has its own 35cp cost limit; defining gambit moves disclose
    speculative cost without inheriting the midgame trap-frequency lottery.
    """
    result, gate = dict(search_plan), dict(frequency)
    opening = result.get("eligible") is True and result.get("mode") == "opening"
    gated = result.get("eligible") is True and result.get("mode") == "trap_hunt"
    gate.update(baseline_matches_repertoire=baseline_move.uci() == result.get("candidate_uci"),
                scope="poisoned_piece_frequency_only", opening_exempt=opening)
    result.update(conservative_setup=opening and not result.get("line_complete"),
                  opening_frequency_exempt=opening, frequency_gate_applied=gated)
    if probe_failed or gated and not gate.get("attempt_trap"):
        result.update(eligible=False, extra_budget_ms=0, candidate_budget_ms=0,
                      reason="quality_probe_failed" if probe_failed else "opponent_frequency_hold")
    return result, gate


def score_line(board, info, required_move=None):
    if isinstance(info, dict) and isinstance(info.get("score"), chess.engine.PovScore):
        if info["score"].turn != board.turn:
            return None
    return opening.score_line(board, info, required_move)


def baseline_guard(board, baseline_move, baseline_info):
    line = score_line(board, baseline_info, baseline_move)
    if line is None or line["depth"] < MIN_DEPTH:
        raw = opening.packet_summary(board, baseline_info)
        if raw and raw["mate"] is not None:
            return "mate_reference_not_comparable"
        return "baseline_unreliable"
    return None


def observe_human_move(board, search, close_engine, *, budget_ms=QUALITY_BUDGET_MS, forced=False):
    """Compare the human's actual move against the same pre-move position.

    Both searches use the configured own engine and exact completed depth.
    No score difference across positions is treated as move quality.
    """
    history = [move.uci() for move in board.move_stack]
    result = {"valid": False, "ply": len(history), "history_before": history[:-1],
              "played_uci": history[-1] if history else None, "loss_cp": None,
              "depth": None, "forced": bool(forced), "reason": "no_previous_human_move",
              "budget_ms": round(budget_ms, 3), "elapsed_ms": 0,
              "rating_evidence": rating_evidence.empty("no_previous_human_move")}
    if not history or forced or budget_ms < 100:
        result["reason"] = "forced_game" if forced else "short_budget" if history else result["reason"]
        result["rating_evidence"] = rating_evidence.empty(result["reason"])
        return result, False
    before = board.copy(stack=True)
    played = before.pop()
    result["before_fen"] = before.fen(en_passant="fen")
    if (not before.is_valid() or before.root().fen(en_passant="fen") != chess.STARTING_FEN
            or before.legal_moves.count() <= 1 or played not in before.legal_moves):
        result["reason"] = "forced_or_invalid_position"
        result["rating_evidence"] = rating_evidence.empty(result["reason"])
        return result, False
    started = time.monotonic()
    done, expired = threading.Event(), threading.Event()
    def expire():
        if not done.is_set():
            expired.set()
            try:
                close_engine()
            except Exception:
                pass
    timer = threading.Timer(budget_ms / 1000 + .1, expire)
    timer.daemon = True
    timer.start()
    discard = False
    try:
        best, info, best_packets = search(budget_ms * .45, position=before, publish=False, fresh=True)
        line = score_line(before, info, best.move) if best.move else None
        result["baseline"] = line
        comparable = line is not None and line["depth"] >= MIN_DEPTH
        rating_reference = rating_evidence.reference(before, best.move, info, best_packets)
        result["rating_evidence"] = rating_evidence.empty("reference_unreliable")
        if not comparable:
            result["reason"] = "reference_unreliable"
        if best.move == played:
            if comparable:
                result.update(valid=True, loss_cp=0, depth=line["depth"], candidate_compared=line,
                              reason="actual_move_is_reference")
            result["rating_evidence"] = rating_evidence.assess(before, best.move, info, best_packets, played)
        elif comparable or rating_reference is not None:
            remaining = budget_ms - (time.monotonic() - started) * 1000
            if remaining < MIN_PROBE_MS:
                if comparable:
                    result["reason"] = "quality_budget_exhausted"
                result["rating_evidence"] = rating_evidence.empty("quality_budget_exhausted")
            else:
                # A strict mate reference can inform the descriptive rating,
                # while the legacy CP-based controller remains unchanged.
                depth = line["depth"] if comparable else rating_reference["depth"]
                actual, final, packets = search(remaining, root_move=played, position=before,
                                                publish=False, fresh=True, depth=depth)
                if comparable:
                    checked = assess(before, best.move, info, played.uci(), packets, final, actual.move)
                    result.update(candidate_compared=checked["candidate_compared"],
                                  depth=checked["compared_depth"], reason=checked["reason"])
                    if checked["admitted"] and checked["reference_delta_cp"] < -25:
                        result["reason"] = "quality_reference_conflict"
                    elif checked["admitted"]:
                        result.update(valid=True, loss_cp=checked["loss_cp"], reason="same_position_completed_depth")
                result["rating_evidence"] = rating_evidence.assess(
                    before, best.move, info, best_packets, played, played_info=final,
                    played_packets=packets, returned_move=actual.move)
    except Exception as error:
        result.update(reason="quality_probe_failed", error=type(error).__name__)
        result["rating_evidence"] = rating_evidence.empty("quality_probe_failed")
        discard = True
    finally:
        done.set()
        timer.cancel()
        timer.join(timeout=.2)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    if expired.is_set() or result["elapsed_ms"] > budget_ms + 20:
        result.update(valid=False, loss_cp=None, reason="quality_probe_timeout")
        result["rating_evidence"] = rating_evidence.empty("quality_probe_timeout")
        discard = expired.is_set() or discard
    return result, discard


def assess(board, baseline_move, baseline_info, proposed_uci, candidate_packets=None,
           candidate_final=None, candidate_move=None, candidate_error=None, *, budget_ms=0, elapsed_ms=0):
    baseline = score_line(board, baseline_info, baseline_move)
    final = score_line(board, candidate_final, candidate_move) if candidate_move else None
    raw = opening.packet_summary(board, candidate_final)
    common, loss = None, None
    reason = baseline_guard(board, baseline_move, baseline_info) or "candidate_unreliable"
    if candidate_error:
        reason = "candidate_failed"
    elif raw and raw["mate"] is not None:
        reason = "mate_candidate_not_comparable"
    elif baseline_guard(board, baseline_move, baseline_info) is None and final is not None and candidate_move.uci() == proposed_uci:
        common = score_line(board, (candidate_packets or {}).get(baseline["depth"]), candidate_move)
        if common is None or final["depth"] < baseline["depth"]:
            reason = "no_common_depth"
        else:
            loss = max(0, baseline["cp"] - common["cp"])
            if final["depth"] == baseline["depth"] and final["cp"] != common["cp"]:
                reason = "candidate_score_conflict"
            else:
                reason = "evaluated_speculative_risk"
    return {"uci": proposed_uci, "admitted": reason == "evaluated_speculative_risk", "reason": reason,
            "loss_cp": loss, "compared_depth": baseline["depth"] if baseline else None,
            "reference_delta_cp": baseline["cp"] - common["cp"] if baseline and common else None,
            "candidate_compared": common, "candidate_final": final, "candidate_final_raw": raw,
            "budget_ms": budget_ms, "elapsed_ms": round(elapsed_ms, 3), "error": candidate_error}


def receipt(board, search_plan, baseline_move, baseline_info, probes=(), seed="", *, baseline_elapsed_ms=0, extra_elapsed_ms=0):
    base = baseline_move.uci()
    selected, reason = base, search_plan["reason"]
    chosen = None
    authored = False
    if search_plan["eligible"]:
        guard = baseline_guard(board, baseline_move, baseline_info)
        if guard:
            reason = guard
        elif base == search_plan["candidate_uci"]:
            authored, reason = True, "authored_move_is_baseline"
        else:
            chosen = next((p for p in probes if p["uci"] == search_plan["candidate_uci"] and p["admitted"]), None)
            if chosen:
                selected, authored = chosen["uci"], True
                reason = "accepted_safe_gambit_setup" if search_plan.get("conservative_setup") else "accepted_authored_gambit"
            else:
                reason = probes[-1]["reason"] if probes else "candidate_unverified"
    return {"policy": POLICY, "repertoire_policy": boss_openings.POLICY, "mode": search_plan.get("mode", "opening"),
            "ply": len(board.move_stack) + 1, "before_fen": board.fen(en_passant="fen"),
            "history_uci": opening.history_key(board)[1], "seed": seed,
            "baseline_uci": base, "selected_uci": selected, "candidate_uci": search_plan["candidate_uci"],
            "line_id": search_plan["line_id"] if authored else None,
            "opening_name": search_plan["opening_name"] if authored else None,
            "proposed_line_id": search_plan["line_id"], "proposed_opening_name": search_plan["opening_name"],
            "line_complete": authored and search_plan["line_complete"], "reason": reason,
            "loss_cp": chosen["loss_cp"] if chosen else 0,
            "compared_depth": chosen["compared_depth"] if chosen else (baseline_info or {}).get("depth"),
            "baseline": score_line(board, baseline_info, baseline_move), "probes": list(probes),
            "best_defense_pv_uci": ((chosen["candidate_compared"] if chosen else
                                      score_line(board, baseline_info, baseline_move)) or {}).get("pv_uci", []),
            "risk_profile": RISK_PROFILE,
            "conservative_setup": bool(search_plan.get("conservative_setup")),
            "opening_frequency_exempt": bool(search_plan.get("opening_frequency_exempt")),
            "frequency_gate_applied": bool(search_plan.get("frequency_gate_applied")),
            "setup_max_loss_cp": SETUP_MAX_LOSS_CP if search_plan.get("conservative_setup") else None,
            "reference_delta_cp": chosen["reference_delta_cp"] if chosen else 0,
            "best_defense_cp": (chosen["candidate_compared"]["cp"] if chosen else
                                (score_line(board, baseline_info, baseline_move) or {}).get("cp")),
            "final_best_defense_cp": (chosen["candidate_final"]["cp"] if chosen else
                                      (score_line(board, baseline_info, baseline_move) or {}).get("cp")),
            "baseline_budget_ms": search_plan["baseline_budget_ms"], "extra_budget_ms": search_plan["extra_budget_ms"],
            "baseline_elapsed_ms": round(baseline_elapsed_ms, 3), "extra_elapsed_ms": round(extra_elapsed_ms, 3),
            "reused": False}


def explore_initiative(board, search_plan, baseline_move, baseline_info, baseline_packets, seed,
                       search, close_engine, *, baseline_elapsed_ms=0):
    """Keep gambit compensation active inside the existing shared allowance."""
    knowledge = bool(search_plan.get("knowledge_candidates"))
    if knowledge:
        detail, discard = gambit_repertoire_play.choose(
            board, search_plan, baseline_move, baseline_info, baseline_packets, seed, search, close_engine)
    else:
        detail, discard = gambit_initiative.choose(
            board, baseline_move, baseline_info, baseline_packets, search, close_engine,
            seed=seed, budget_ms=search_plan["extra_budget_ms"],
            context=search_plan.get("initiative_context"))
    result = receipt(board, {**search_plan, "eligible": False, "reason": detail["reason"]},
                     baseline_move, baseline_info, seed=seed,
                     baseline_elapsed_ms=baseline_elapsed_ms,
                     extra_elapsed_ms=detail["extra_elapsed_ms"])
    evidence = detail.get("evidence") or {}
    compared, final = evidence.get("candidate_compared"), evidence.get("candidate_final")
    initiative_detail = copy.deepcopy(detail) if knowledge else detail
    if knowledge and isinstance(search_plan.get("initiative_context"), dict):
        initiative_detail["context"] = copy.deepcopy(search_plan["initiative_context"])
    result.update(mode="initiative", selected_uci=detail["selected_uci"],
                  candidate_uci=detail["selected_uci"] if detail["applied"] else None,
                  initiative=initiative_detail, reason=detail["reason"], loss_cp=detail["loss_cp"],
                  trap_evidence=None, trap_attempts=[],
                  verification_policy="native_same_depth_reply_supported_initiative")
    if knowledge:
        result.update(knowledge=detail, verification_policy=gambit_repertoire_play.POLICY)
    if search_plan.get("operations_context"):
        result.update(operations=copy.deepcopy(detail),
                      operations_context=copy.deepcopy(search_plan["operations_context"]),
                      practice_memory=copy.deepcopy(search_plan.get("practice_memory")))
    if compared is not None and final is not None:
        result.update(compared_depth=compared["depth"], best_defense_cp=compared["cp"],
                      final_best_defense_cp=final["cp"], best_defense_pv_uci=compared["pv_uci"],
                      reference_delta_cp=result["baseline"]["cp"] - compared["cp"])
    return result, discard


def explore_traps(board, search_plan, baseline_move, baseline_info, baseline_packets, seed,
                  search, close_engine, *, baseline_elapsed_ms=0):
    """Measure an offer's cost and prove punishment in one shared time budget.

    ``search`` is the host's own configured engine; only the capture branch
    changes the position. Its scores never replace the live root evaluation.
    """
    from poisoned_piece import enumerate_offers, validate_capture_branch

    fallback_plan = {**search_plan, "eligible": False, "reason": "no_verified_lure"}
    result = receipt(board, fallback_plan, baseline_move, baseline_info, seed=seed,
                     baseline_elapsed_ms=baseline_elapsed_ms)
    result.update(risk_profile=RISK_PROFILE, trap_evidence=None, trap_attempts=[])
    guard = baseline_guard(board, baseline_move, baseline_info)
    if guard:
        result["reason"] = guard
        return result, False
    baseline = score_line(board, baseline_info, baseline_move)
    started = time.monotonic()
    deadline = started + search_plan["extra_budget_ms"] / 1000
    done, expired = threading.Event(), threading.Event()

    def expire():
        if not done.is_set():
            expired.set()
            try:
                close_engine()
            except Exception:
                pass

    watchdog = threading.Timer(search_plan["extra_budget_ms"] / 1000 + .1, expire)
    watchdog.daemon = True
    watchdog.start()
    discard = False
    try:
        offers = enumerate_offers(board, baseline_move=baseline_move, max_candidates=8, max_captures=1)
        # A newly exposed queen behind a moved knight is a more interesting
        # hypothesis than an ordinary exchange already available beforehand.
        offers.sort(key=lambda row: (-row.get("attraction_score", row.get("offered_cp", 0)),
                                     not row.get("newly_exposed", False), -row.get("offered_cp", 0),
                                     row.get("moved_piece_is_bait", False), not row.get("offer_is_capture", False),
                                     row["uci"] != baseline_move.uci(), row["uci"], row["capture_uci"]))
        offers = offers[:2]
        best_appeal = None
        for index, offer in enumerate(offers):
            if expired.is_set() or deadline - time.monotonic() < .05:
                break
            uci = offer["uci"]
            attempt = {"offer": dict(offer), "evaluated": False, "reason": "unverified"}
            result["trap_attempts"].append(attempt)
            move = chess.Move.from_uci(uci)
            if move == baseline_move:
                loss = 0
                common, last = baseline, baseline
                attempt.update(evaluated=True, reason="baseline_offer", loss_cp=0, compared_depth=baseline["depth"])
            else:
                remaining_ms = (deadline - time.monotonic()) * 1000
                candidate_budget = min(remaining_ms * .45, search_plan["extra_budget_ms"] / 2)
                candidate_started = time.monotonic()
                candidate, final, packets = search(candidate_budget, root_move=move, publish=False,
                                                   fresh=True, depth=baseline["depth"])
                checked = assess(board, baseline_move, baseline_info, uci, packets, final, candidate.move,
                                 budget_ms=candidate_budget, elapsed_ms=(time.monotonic() - candidate_started) * 1000)
                common, last = checked["candidate_compared"], checked["candidate_final"]
                loss = checked["loss_cp"]
                evaluated = checked["admitted"] and common is not None and last is not None
                attempt.update(evaluated=evaluated, reason="evaluated_speculative_risk" if evaluated else "offer_cost_unreliable",
                               loss_cp=loss, comparison=checked, compared_depth=baseline["depth"])
                if not evaluated:
                    continue
            after_capture = board.copy(stack=True)
            after_capture.push(move)
            capture = chess.Move.from_uci(offer["capture_uci"])
            if capture not in after_capture.legal_moves:
                attempt["reason"] = "capture_no_longer_legal"
                continue
            after_capture.push(capture)
            remaining_ms = (deadline - time.monotonic()) * 1000
            if remaining_ms < 75:
                attempt["reason"] = "verification_budget_exhausted"
                break
            # Reserve time for independent legal-defense enumeration, not only
            # the engine's preferred continuation after the greedy capture.
            branch_share = remaining_ms / max(1, len(offers) - index)
            branch_budget = max(MIN_PROBE_MS, branch_share - 100)
            branch_started = time.monotonic()
            _reply, info, _packets = search(branch_budget, publish=False, fresh=True,
                                           depth=TRAP_MAX_PLIES, position=after_capture)
            attempt.update(branch_budget_ms=round(branch_budget, 3),
                           branch_elapsed_ms=round((time.monotonic() - branch_started) * 1000, 3))
            proof_started = time.monotonic()
            evidence = validate_capture_branch(board, uci, offer["capture_uci"], info,
                                                engine_color=board.turn, max_plies=TRAP_MAX_PLIES,
                                                deadline_monotonic=deadline)
            attempt["proof_elapsed_ms"] = round((time.monotonic() - proof_started) * 1000, 3)
            attempt["evidence"] = evidence
            if evidence.get("verified") is True and not expired.is_set() and time.monotonic() <= deadline:
                delay = len(evidence.get("pv_uci", []))
                proven_delay = evidence.get("minimum_payoff_moves") or 0
                payoff = 3000 if evidence.get("kind") == "forced_mate" else min(4000, evidence.get("pv_countergain_cp") or 0)
                apparent_gain = offer.get("apparent_capture_gain_cp", offer.get("offered_cp", 0))
                motif = 500 * bool(offer.get("opened_slider_rays"))
                appeal = 2 * apparent_gain + payoff + 150 * min(4, proven_delay) + motif - .15 * max(0, loss)
                attraction = {"score": round(appeal, 3), "bait_cp": offer.get("offered_cp"),
                              "payoff": evidence.get("kind"), "punishment_plies": delay,
                              "apparent_capture_gain_cp": apparent_gain, "minimum_payoff_moves": proven_delay,
                              "opened_slider_bonus_points": motif,
                              "countergain_cp": evidence.get("pv_countergain_cp"), "cost_penalty_points": round(.15 * max(0, loss), 3),
                              "basis": "apparent capture gain, proved payoff/delay and opened ray; heuristic, not human capture probability"}
                attempt["attraction"] = attraction
                if best_appeal is None or appeal > best_appeal:
                    best_appeal = appeal
                    result.update(selected_uci=uci, candidate_uci=uci, reason="verified_poisoned_piece",
                                  loss_cp=loss, reference_delta_cp=baseline["cp"] - common["cp"],
                                  best_defense_cp=common["cp"], final_best_defense_cp=last["cp"],
                                  best_defense_pv_uci=common["pv_uci"],
                                  trap_evidence={"offer": dict(offer), **evidence}, attraction=attraction,
                                  selection_rule="highest_verified_appeal_within_budget")
                continue
            attempt["reason"] = evidence.get("reason", "capture_punishment_unverified")
    except Exception as error:
        if result["trap_evidence"] is None:
            result["reason"] = "trap_probe_failed"
        result["trap_error"] = type(error).__name__
        discard = True
    finally:
        done.set()
        watchdog.cancel()
        watchdog.join(timeout=.2)
    if expired.is_set():
        result.update(selected_uci=baseline_move.uci(), candidate_uci=None, loss_cp=0,
                      reference_delta_cp=0, best_defense_cp=baseline["cp"], final_best_defense_cp=baseline["cp"],
                      best_defense_pv_uci=baseline["pv_uci"],
                      reason="trap_probe_timeout", trap_evidence=None, attraction=None)
        discard = True
    result["extra_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    return result, discard


def pgn_headers(metadata):
    if (not isinstance(metadata, dict) or metadata.get("enabled") is not True
            or metadata.get("policy") not in SUPPORTED_POLICIES):
        return {}
    seed = metadata.get("seed")
    if not isinstance(seed, str) or not re.fullmatch(r"[0-9a-f]{32}", seed):
        return {}
    return {"BossMode": "1", "BossPolicy": metadata["policy"], "BossSeed": seed}
