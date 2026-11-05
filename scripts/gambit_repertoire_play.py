"""Verify learned, sourced opening proposals with the project's own engine.

The model only orders candidates. Completed native root searches and a bounded
legal capture screen decide whether a continuation can replace the baseline.
"""
import copy
import math
import threading
import time

import chess

import gambit_initiative as attack
import gambit_knowledge
import dynamic_play
import gambit_operations
import gambit_practice_memory

POLICY = "regicide-gambit-repertoire-play-v7"
MAX_PROBES = 2
MAX_EXTRA_MS = 1000
LOSS_CAP_CP = 35
NATIVE_GAIN_CP = 10
# Research only: the 2026-11-05 fixed 50-position study changed four moves,
# with one external improvement and three regressions (total loss +29cp).
# Retain same-depth independent verification until this experiment earns use.
ENABLE_STUDIED_FINALISTS = False


def _compare_studied(board, rows, baseline_move, baseline, operation, search, deadline):
    """Let the native engine compare the live baseline and studied alternatives.

    Prior study supplies moves, never scores. The comparison must complete one
    full iteration beyond the baseline with that baseline still in the root
    set. Descendant replies are unrestricted. A partial iteration cannot pick
    an alternative, even if its provisional score looks promising.
    """
    attempt = {"admitted": False, "reason": "studied_comparison_incomplete",
               "uci": baseline_move.uci(), "source": None,
               "finalists": [baseline_move.uci()], "screens": [],
               "target_depth": baseline["depth"] + 1, "loss_cap_cp": 0,
               "loss_basis": "baseline_included_in_same_completed_native_search"}
    admitted = {}
    for row in rows[:MAX_PROBES]:
        move = chess.Move.from_uci(row["uci"])
        screen = attack._material_screen(board, move, deadline)
        attempt["screens"].append({"uci": row["uci"], **screen})
        if screen["admitted"]:
            admitted[row["uci"]] = row
            attempt["finalists"].append(row["uci"])
    if not admitted:
        attempt["reason"] = "no_safe_studied_alternative"
        return attempt
    amount = (deadline - time.monotonic()) * 1000 - 10
    if amount < attack.MIN_PROBE_MS:
        attempt["reason"] = "insufficient_probe_budget"
        return attempt
    reply, info, packets = search(amount,
        root_moves=[chess.Move.from_uci(uci) for uci in attempt["finalists"]],
        publish=False, fresh=True, depth=attempt["target_depth"])
    move = reply.move
    if move is None or move.uci() not in attempt["finalists"]:
        attempt["reason"] = "studied_comparison_wrong_root"
        return attempt
    common = attack._line(board, (packets or {}).get(attempt["target_depth"]), move)
    final = attack._line(board, info, move)
    attempt.update(uci=move.uci(), candidate_compared=common, candidate_final=final,
                   budget_ms=round(amount, 3))
    if common is None or final is None or final["depth"] < attempt["target_depth"]:
        return attempt
    if final["depth"] == common["depth"] and final["cp"] != common["cp"]:
        attempt["reason"] = "candidate_score_conflict"
        return attempt
    if move == baseline_move:
        attempt["reason"] = "baseline_wins_studied_comparison"
        return attempt
    style = attack._style(board, final["pv_uci"])
    if style is None or dynamic_play._pv_has_terminal(board, final["pv_uci"]):
        attempt["reason"] = "insufficient_reply_evidence"
        return attempt
    if operation:
        progress = gambit_operations.continuation_evidence(board, final["pv_uci"], operation["goal"])
        attempt["operations_evidence"] = progress
        if progress is None:
            attempt["reason"] = "insufficient_operations_reply_evidence"
            return attempt
    attempt.update(admitted=True, reason="native_verified_studied_finalists",
                   admission_basis="deeper_native_comparison_including_baseline",
                   source=copy.deepcopy(admitted[move.uci()]), loss_cp=None,
                   selected_vs_finalist_best_loss_cp=0, style=style)
    return attempt


def choose(board, search_plan, baseline_move, baseline_info, baseline_packets, seed,
           search, close_engine):
    started = time.monotonic()
    baseline = attack._line(board, baseline_info, baseline_move)
    result = {"policy": POLICY, "selected_uci": baseline_move.uci(), "applied": False,
              "reason": "knowledge_unverified", "loss_cp": 0, "baseline": baseline,
              "context": copy.deepcopy(search_plan.get("knowledge_context")),
              "ranking": copy.deepcopy(search_plan.get("knowledge_ranking")),
              "attempts": [], "evidence": None, "extra_elapsed_ms": 0,
              "budget_ms": 0, "loss_cap_cp": LOSS_CAP_CP}
    rows = search_plan.get("knowledge_candidates")
    if not isinstance(rows, list) or not rows or baseline is None or baseline["depth"] < 4:
        result["reason"] = "knowledge_or_baseline_unavailable"
        return result, False
    # Re-resolve the actual position, including full history. A stale plan or
    # a caller-supplied move does not establish a catalog relationship.
    operation = search_plan.get("operations_context")
    current_operation = gambit_operations.context(board) if operation else None
    if operation and (not current_operation.get("active")
                      or current_operation.get("context_id") != operation.get("context_id")):
        result["reason"] = "operations_context_changed"
        return result, False
    def valid(row):
        if gambit_knowledge.valid_proposal(board, row):
            return True
        if not operation:
            return False
        return (gambit_operations.valid_proposal(board, row) or
                gambit_practice_memory.valid_proposal(board, row,
                    engine_identity=search_plan.get("operations_engine_identity")))
    rows = [row for row in rows if valid(row)]
    if operation:
        result.update(operations_context=copy.deepcopy(current_operation),
                      loss_cap_cp=min(LOSS_CAP_CP, current_operation["loss_cap_cp"]))
    if not rows or board.is_check() or board.is_repetition(2):
        result["reason"] = "knowledge_context_changed"
        return result, False
    if dynamic_play._pv_has_terminal(board, baseline["pv_uci"]):
        result["reason"] = "forcing_baseline"
        return result, False
    if attack._style(board, baseline["pv_uci"]) is None:
        result["reason"] = "insufficient_reply_evidence"
        return result, False
    base_packet = (baseline_packets or {}).get(baseline["depth"])
    if base_packet is not None and attack._line(board, base_packet, baseline_move) != baseline:
        result["reason"] = "baseline_packet_conflict"
        return result, False
    baseline_operation = (gambit_operations.continuation_evidence(board, baseline["pv_uci"], operation["goal"])
                          if operation else None)
    if operation:
        result["baseline_operations_evidence"] = baseline_operation
        if baseline_operation is None:
            result["reason"] = "insufficient_operations_reply_evidence"
            return result, False
    studied = ([row for row in rows if row.get("source") == "native_gambit_continuation"
                and row["uci"] != baseline_move.uci()] if ENABLE_STUDIED_FINALISTS else [])
    if (not studied and any(row["uci"] == baseline_move.uci() for row in rows)
            and (not operation or rows[0]["uci"] == baseline_move.uci())):
        result.update(reason="knowledge_move_is_baseline", evidence={"source": next(
            copy.deepcopy(row) for row in rows if row["uci"] == baseline_move.uci())})
        return result, False
    budget = search_plan.get("extra_budget_ms")
    if type(budget) not in (int, float) or not math.isfinite(budget) or budget < attack.MIN_PROBE_MS:
        result["reason"] = "short_budget"
        return result, False
    result["budget_ms"] = min(budget, MAX_EXTRA_MS)
    deadline = started + result["budget_ms"] / 1000
    done, expired = threading.Event(), threading.Event()
    def expire():
        if not done.is_set():
            expired.set()
            try:
                close_engine()
            except Exception:
                pass
    watchdog = threading.Timer(max(0, deadline - time.monotonic()) + .1, expire)
    watchdog.daemon = True
    watchdog.start()
    chosen, discard = None, False
    try:
        alternatives = [row for row in rows if row["uci"] != baseline_move.uci()][:MAX_PROBES]
        if studied:
            attempt = _compare_studied(board, studied, baseline_move, baseline, operation, search, deadline)
            result["attempts"].append(attempt)
            if attempt["admitted"]:
                chosen = attempt
                result["loss_basis"] = attempt["loss_basis"]
                result["selected_vs_finalist_best_loss_cp"] = 0
            else:
                result["reason"] = attempt["reason"]
            alternatives = []
        for index, row in enumerate(alternatives):
            move = chess.Move.from_uci(row["uci"])
            attempt = {"uci": row["uci"], "admitted": False, "source": copy.deepcopy(row)}
            result["attempts"].append(attempt)
            screen = attack._material_screen(board, move, deadline)
            attempt["material_screen"] = screen
            if not screen["admitted"]:
                attempt["reason"] = screen["reason"]
                continue
            remaining = (deadline - time.monotonic()) * 1000 - 10
            amount = remaining / max(1, len(alternatives) - index)
            if expired.is_set() or amount < attack.MIN_PROBE_MS:
                attempt["reason"] = "insufficient_probe_budget"
                break
            reply, info, packets = search(amount, root_move=move, publish=False,
                                           fresh=True, depth=baseline["depth"])
            common = attack._line(board, (packets or {}).get(baseline["depth"]), move)
            final = attack._line(board, info, move)
            attempt.update(candidate_compared=common, candidate_final=final,
                           budget_ms=round(amount, 3))
            if reply.move != move or common is None or final is None or final["depth"] < baseline["depth"]:
                attempt["reason"] = "candidate_unreliable_or_incomplete"
                continue
            if final["depth"] == baseline["depth"] and final["cp"] != common["cp"]:
                attempt["reason"] = "candidate_score_conflict"
                continue
            # Geometry alone supplies no evidence for accepting a weaker
            # move. A known book/practiced analysis may use the bounded
            # compensation allowance; new unsourced plans must be lossless.
            cap = (0 if row.get("source") in ("native_operations_geometry", "native_gambit_continuation")
                   else result["loss_cap_cp"])
            attempt["loss_cap_cp"] = cap
            if min(common["cp"], final["cp"]) < baseline["cp"] - cap:
                attempt["reason"] = "outside_quality_floor"
                continue
            style = attack._style(board, final["pv_uci"])
            if style is None or dynamic_play._pv_has_terminal(board, final["pv_uci"]):
                attempt["reason"] = "insufficient_reply_evidence"
                continue
            if operation:
                progress = gambit_operations.continuation_evidence(board, final["pv_uci"], operation["goal"])
                attempt["operations_evidence"] = progress
                native_gain = min(common["cp"], final["cp"]) - baseline["cp"]
                attempt["native_gain_cp"] = native_gain
                # Geometry describes a plan; it is not a second evaluator.
                # A settled, completed native improvement must not be vetoed
                # just because it trades an active piece or changes the plan.
                operations_gain = (progress is not None and
                    (progress["points"] > baseline_operation["points"] or
                     progress["points"] == baseline_operation["points"] and native_gain > 0))
                if progress is None or not (operations_gain or native_gain >= NATIVE_GAIN_CP):
                    attempt["reason"] = "no_reply_supported_operations_gain"
                    continue
                attempt["admission_basis"] = ("native_quality_improvement" if native_gain >= NATIVE_GAIN_CP
                                               else "reply_supported_operations_gain")
            attempt.update(admitted=True, reason="native_verified_knowledge_continuation",
                           loss_cp=max(0, baseline["cp"] - common["cp"]), style=style)
            if chosen is None or (min(common["cp"], final["cp"]), style["points"]) > (
                    min(chosen["candidate_compared"]["cp"], chosen["candidate_final"]["cp"]),
                    chosen["style"]["points"]):
                chosen = attempt
    except Exception as error:
        result.update(reason="knowledge_probe_failed", error=type(error).__name__)
        chosen, discard = None, True
    finally:
        done.set()
        watchdog.cancel()
        watchdog.join(timeout=.2)
    result["extra_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    if expired.is_set() or time.monotonic() > deadline:
        result["reason"] = "knowledge_probe_timeout"
        return result, True
    if chosen is not None:
        result.update(selected_uci=chosen["uci"], applied=True, reason=chosen["reason"],
                      loss_cp=chosen["loss_cp"], evidence=copy.deepcopy(chosen))
    elif result["reason"] == "knowledge_unverified":
        result["reason"] = "no_verified_knowledge_alternative"
    return result, discard
