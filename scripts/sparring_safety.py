"""Bounded own-engine checks for tactically exposed sparring selections.

Geometry requests extra search; it never proves a sacrifice is unsound.
Only complete native root and child evidence can replace a legal selection.
"""

import threading
import time

import chess

import sparring_evaluation
import sparring_play


POLICY = "regicide-sparring-safety-v1"
MIN_DEPTH = 6
MIN_ROOT_DEPTH = 5
MIN_BUDGET_MS = 600
MAX_BUDGET_MS = 12000
FLOOR_CP = -100
MIN_IMPROVEMENT_CP = 100
MAX_ROOT_CHILD_DELTA_CP = 120


def inspect(board, move):
    """Identify obvious donations or an exposed higher-value capture."""
    result = {"risky": False, "reason": "no_tactical_trigger", "naturalness": None,
              "capture_replies": []}
    if (not isinstance(board, chess.Board) or not board.is_valid()
            or not isinstance(move, chess.Move) or move not in board.legal_moves):
        result["reason"] = "invalid_move_or_position"
        return result
    natural = sparring_play.naturalness(board, move)
    result["naturalness"] = natural
    if natural.get("reason") in ("immediate_mate_reply", "obvious_material_donation"):
        result.update(risky=True, reason=natural["reason"])
        return result
    child = board.copy(stack=True)
    child.push(move)
    if child.is_game_over(claim_draw=True):
        result["reason"] = "terminal_or_claimable_draw"
        return result
    piece = board.piece_at(move.from_square)
    value = sparring_play.VALUES[piece.piece_type]
    gained = sparring_play._capture_value(board, move)
    if value >= 300 and 0 < gained < value:
        result["capture_replies"] = [reply.uci() for reply in child.legal_moves
                                     if reply.to_square == move.to_square and child.is_capture(reply)]
        if result["capture_replies"]:
            result.update(risky=True, reason="exposed_higher_value_capture")
    return result


def verify(board, move, search, close_engine, *, budget_ms, cancelled=None):
    """Return (native root replacement or None, receipt, engine_discarded).

The host must additionally cap searches to its current clock and reject late
results. A close callback must interrupt an in-flight native search. Failed or
incomplete verification preserves the caller's legal move, never a safety claim.
"""
    if type(budget_ms) is not int or budget_ms < 0:
        raise ValueError("Safety budget must be nonnegative integer milliseconds")
    if not callable(search) or not callable(close_engine) or cancelled is not None and not callable(cancelled):
        raise ValueError("Safety verification requires search, close and cancellation callbacks")
    started = time.monotonic()
    budget_ms = min(budget_ms, MAX_BUDGET_MS)
    deadline = started + budget_ms / 1000
    risk = inspect(board, move)
    receipt = {"policy": POLICY, "status": "skipped", "reason": risk["reason"],
               "risk": risk, "budget_ms": budget_ms, "elapsed_ms": 0, "searches": 0,
               "original_move": move.uci() if isinstance(move, chess.Move) else None,
               "selected_move": move.uci() if isinstance(move, chess.Move) else None,
               "before_fen": board.fen(en_passant="fen") if isinstance(board, chess.Board) else None,
               "history_uci": [m.uci() for m in board.move_stack] if isinstance(board, chess.Board) else [],
               "original_child": None, "alternative_root": None, "alternative_child": None,
               "scope": "bounded native tactical check; no guaranteed safety or close game"}
    if not risk["risky"] or budget_ms < MIN_BUDGET_MS:
        if risk["risky"]:
            receipt["reason"] = "insufficient_budget"
        return None, receipt, False
    position = board.copy(stack=True)
    own = "white" if position.turn else "black"
    done, lock = threading.Event(), threading.Lock()
    failure, closed, active, stage_deadline = [None], [False], [False], [None]

    def cancelled_now():
        try:
            return bool(cancelled()) if cancelled else False
        except Exception:
            return True

    def stop(reason):
        close = False
        with lock:
            if done.is_set():
                return
            if failure[0] is None:
                failure[0] = reason
            if active[0] and not closed[0]:
                closed[0], close = True, True
        if close:
            try:
                close_engine()
            except Exception:
                pass

    def check():
        if cancelled_now():
            stop("cancelled")
        if time.monotonic() >= deadline:
            stop("deadline_exceeded")
        if failure[0]:
            raise InterruptedError(failure[0])

    def monitor():
        while not done.wait(.01):
            if cancelled_now():
                stop("cancelled")
            elif time.monotonic() >= deadline:
                stop("deadline_exceeded")
            elif active[0] and stage_deadline[0] is not None and time.monotonic() >= stage_deadline[0]:
                stop("stage_budget_exceeded")

    watcher = threading.Thread(target=monitor, daemon=True)

    def stage(target, allowance, *, root_moves=None):
        check()
        amount = min(allowance, (deadline - time.monotonic()) * 1000)
        if amount < 50:
            raise TimeoutError("insufficient_stage_budget")
        options = {"publish": False, "fresh": True}
        if root_moves is None:
            options["position"] = target.copy(stack=True)
        else:
            options["root_moves"] = root_moves
        receipt["searches"] += 1
        with lock:
            active[0] = True
            stage_deadline[0] = time.monotonic() + amount / 1000 + .1
        begin = time.monotonic()
        try:
            returned = search(amount, **options)
        except Exception:
            stop("native_search_failed")
            raise
        finally:
            with lock:
                active[0] = False
                stage_deadline[0] = None
        check()
        elapsed = (time.monotonic() - begin) * 1000
        if elapsed > amount + 100:
            raise TimeoutError("stage_budget_exceeded")
        reply, final, packets = returned
        if not isinstance(final, dict) or not isinstance(packets, dict):
            raise ValueError("invalid_search_evidence")
        sample = sparring_evaluation.validate_packet(target, final)
        packet = packets.get(final.get("depth"))
        completed = sparring_evaluation.validate_packet(target, packet)
        required_depth = MIN_ROOT_DEPTH if root_moves is not None else MIN_DEPTH
        if (sample is None or completed != sample or sample["depth"] < required_depth
                or not isinstance(reply.move, chess.Move) or reply.move not in target.legal_moves
                or sample["root"] != reply.move.uci()
                or root_moves is not None and reply.move not in root_moves):
            raise ValueError("unverified_search_evidence")
        evidence = {**sample, "fen": target.fen(en_passant="fen"),
                    "cp_own": sample["cp"] * (1 if position.turn else -1) if sample["cp"] is not None else None,
                    "budget_ms": round(amount, 3), "elapsed_ms": round(elapsed, 3)}
        return returned, evidence

    replacement = None
    receipt["status"] = "unverified"
    watcher.start()
    try:
        check()
        child = position.copy(stack=True)
        child.push(move)
        _, original = stage(child, budget_ms * .30)
        receipt["original_child"] = original
        direct_donation = risk["reason"] in ("obvious_material_donation", "immediate_mate_reply")
        if (original["winner"] == own or not direct_donation
                and original["cp_own"] is not None and original["cp_own"] > FLOOR_CP):
            receipt.update(status="preserved", reason="compensation_verified")
        else:
            allowed = []
            for alternative in position.legal_moves:
                check()
                if alternative == move:
                    continue
                natural = sparring_play.naturalness(position, alternative)
                if natural.get("reason") not in ("immediate_mate_reply", "obvious_material_donation"):
                    allowed.append(alternative)
            if not allowed:
                receipt["reason"] = "no_safe_native_alternative"
            else:
                candidate, root_evidence = stage(position, budget_ms * .40, root_moves=allowed)
                receipt["alternative_root"] = root_evidence
                after = position.copy(stack=True)
                after.push(candidate[0].move)
                if after.is_game_over(claim_draw=True):
                    receipt["reason"] = "terminal_alternative_not_verified"
                else:
                    _, checked = stage(after, budget_ms * .30)
                    receipt["alternative_child"] = checked
                    cp, previous = checked["cp_own"], original["cp_own"]
                    improvement = cp - previous if cp is not None and previous is not None else None
                    receipt["improvement_cp"] = improvement
                    winning = checked["winner"] == own
                    escaped_mate = original["winner"] is not None and original["winner"] != own and cp is not None
                    root_cp = root_evidence["cp_own"]
                    improved = (improvement is not None and improvement >= MIN_IMPROVEMENT_CP
                                and root_cp is not None and root_cp - previous >= MIN_IMPROVEMENT_CP)
                    delta = abs(root_cp - cp) if root_cp is not None and cp is not None else None
                    consistent = (delta is not None and delta <= MAX_ROOT_CHILD_DELTA_CP
                                  or winning and root_evidence["winner"] == own)
                    receipt["root_child_delta_cp"] = delta
                    if root_cp is None:
                        receipt["reason"] = "mate_alternative_requires_separate_selection"
                    elif not consistent:
                        receipt["reason"] = "alternative_root_child_disagreement"
                    elif winning or escaped_mate or improved:
                        replacement = candidate
                        receipt.update(status="replaced", selected_move=candidate[0].move.uci(),
                            reason="verified_alternative_in_band" if cp is not None and abs(cp) <= 100
                            else "verified_alternative_improves_outside_band")
                    else:
                        receipt["reason"] = "no_verified_improvement"
        check()
    except Exception as exc:
        replacement = None
        receipt.update(status="unverified", reason=failure[0] or str(exc), error=type(exc).__name__)
    finally:
        done.set()
        watcher.join(timeout=.2)
        receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    if replacement is None:
        receipt["selected_move"] = receipt["original_move"]
    return replacement, receipt, closed[0]
