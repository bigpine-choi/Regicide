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

POLICY = "regicide-gambit-repertoire-play-v3"
MAX_PROBES = 2
MAX_EXTRA_MS = 1000
LOSS_CAP_CP = 35


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
    if (any(row["uci"] == baseline_move.uci() for row in rows)
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
            cap = 0 if row.get("source") == "native_operations_geometry" else result["loss_cap_cp"]
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
                if (progress is None or progress["points"] < baseline_operation["points"]
                        or progress["points"] == baseline_operation["points"]
                        and min(common["cp"], final["cp"]) <= baseline["cp"]):
                    attempt["reason"] = "no_reply_supported_operations_gain"
                    continue
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
