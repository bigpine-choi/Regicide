"""Own-engine tactical opportunities under a shared optional-turn budget.

The thresholds below are conservative native heuristics, not a reconstruction
of Chess.com ratings or its unpublished move-grading implementation. Local
investment evidence plus finite search is not a proof of compensation.
"""
import math
import threading
import time

import chess
import chess.engine

import sound_sacrifice

POLICY = "regicide-gambit-tactics-v3"
MIN_DEPTH = 4
MIN_BUDGET_MS = 100
MAX_BUDGET_MS = 1000
MAX_CANDIDATES = 8
MAX_LOSS_CP = 25
MIN_CANDIDATE_CP = 0
ALREADY_WINNING_CP = 250
CRITICAL_ALTERNATIVE_CP = -200
MIN_SEARCH_MS = 20


def _line(board, info, required=None, *, allow_mate=False):
    if not isinstance(info, dict):
        return None
    value, depth, pv = info.get("score"), info.get("depth"), info.get("pv")
    if (not isinstance(value, chess.engine.PovScore) or value.turn != board.turn
            or type(depth) is not int or depth < 1 or info.get("multipv", 1) != 1
            or info.get("lowerbound") or info.get("upperbound")
            or not isinstance(pv, (list, tuple)) or not pv or len(pv) > 128
            or required is not None and pv[0] != required):
        return None
    score = value.pov(board.turn)
    cp, mate = score.score(), score.mate()
    if mate is not None and (not allow_mate or type(mate) is not int or mate == 0):
        return None
    if mate is None and type(cp) is not int:
        return None
    replay = board.copy(stack=True)
    for move in pv:
        if (not isinstance(move, chess.Move) or replay.is_game_over(claim_draw=True)
                or move not in replay.legal_moves):
            return None
        replay.push(move)
    if len(pv) < 2 and not (allow_mate and mate is not None and replay.is_checkmate()):
        return None
    return {"depth": depth, "cp": cp, "mate": mate, "uci": pv[0].uci(),
            "cp_white": value.white().score(), "pv_uci": [move.uci() for move in pv],
            "pov": "white" if board.turn else "black"}


def _quality(line, baseline):
    return (line is not None and line["mate"] is None and line["cp"] >= MIN_CANDIDATE_CP
            and line["cp"] >= baseline["cp"] - MAX_LOSS_CP)


def _forcing(board, move):
    return board.is_capture(move) or board.gives_check(move)


def choose(board, baseline_move, baseline_info, baseline_packets, search, close_engine, *, budget_ms):
    started = time.monotonic()
    base_uci = baseline_move.uci() if isinstance(baseline_move, chess.Move) else None
    result = {"policy": POLICY, "selected_uci": base_uci, "applied": False, "qualified": False,
              "kind": None, "kinds": [], "reason": "baseline_preserved", "loss_cp": 0,
              "evidence": None, "baseline": None, "attempts": [], "budget_ms": 0, "extra_elapsed_ms": 0,
              "budget_basis": "shared monotonic wall deadline; per-call requested ceilings may overlap when an earlier depth completes early",
              "threshold_basis": "native conservative heuristics; not exact Chess.com classification",
              "thresholds": {"max_loss_cp": MAX_LOSS_CP, "candidate_cp_min": MIN_CANDIDATE_CP,
                  "already_winning_alternative_cp": ALREADY_WINNING_CP,
                  "critical_alternative_cp_max": CRITICAL_ALTERNATIVE_CP}}

    def finish(reason, discard=False):
        result["reason"] = reason
        result["extra_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        result["search_requested_ms"] = round(sum(row.get("requested_ms", 0) for row in result["attempts"]), 3)
        result["search_elapsed_ms"] = round(sum(row.get("elapsed_ms", 0) for row in result["attempts"]), 3)
        result["search_returned_unused_ms"] = round(sum(row.get("returned_unused_ms", 0) for row in result["attempts"]), 3)
        return result, discard

    if (not isinstance(board, chess.Board) or not board.is_valid() or board.chess960
            or baseline_move not in board.legal_moves):
        return finish("invalid_baseline_or_position")
    if board.is_check() or board.is_game_over(claim_draw=True) or board.is_repetition(2) or board.legal_moves.count() <= 1:
        return finish("forced_check_or_terminal")
    if (type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms < MIN_BUDGET_MS):
        return finish("short_or_invalid_budget")
    raw_score = (baseline_info or {}).get("score") if isinstance(baseline_info, dict) else None
    if isinstance(raw_score, chess.engine.PovScore) and raw_score.pov(board.turn).mate() is not None:
        return finish("protected_mate")
    baseline = _line(board, baseline_info, baseline_move)
    if baseline is None or baseline["depth"] < MIN_DEPTH or baseline["depth"] >= 64:
        return finish("baseline_unreliable")
    saved = _line(board, (baseline_packets or {}).get(baseline["depth"]), baseline_move)
    if saved != baseline:
        return finish("baseline_packet_conflict")
    result["baseline"] = baseline
    result["budget_ms"] = min(MAX_BUDGET_MS, budget_ms)
    deadline = started + result["budget_ms"] / 1000
    geometry_deadline = min(deadline, started + result["budget_ms"] * .22 / 1000)
    done, expired = threading.Event(), threading.Event()
    def expire():
        if not done.is_set():
            expired.set()
            try:
                close_engine()
            except Exception:
                pass
    watchdog = threading.Timer(max(0, deadline - time.monotonic()), expire)
    watchdog.daemon = True
    watchdog.start()
    discard = False
    final_reason = "no_tactical_candidates"
    chosen = None

    def timed_search(requested, stage, **options):
        # A request is a ceiling, not consumed time: a completed depth may
        # return much earlier. The one outer deadline charges geometry,
        # validation and actual search time, and lets later probes use the rest.
        before = time.monotonic()
        remaining = max(0, (deadline - before) * 1000)
        amount = min(requested, remaining)
        if expired.is_set() or amount < MIN_SEARCH_MS:
            raise TimeoutError("No remaining tactical search time")
        attempt = {"stage": stage, "budget_ms": amount, "requested_ms": amount,
                   "deadline_remaining_before_ms": remaining}
        result["attempts"].append(attempt)
        try:
            answer = search(amount, **options)
        finally:
            after = time.monotonic()
            elapsed = max(0, (after - before) * 1000)
            attempt.update(elapsed_ms=round(elapsed, 3),
                           returned_unused_ms=round(max(0, amount - elapsed), 3),
                           deadline_remaining_after_ms=round(max(0, (deadline - after) * 1000), 3))
        return (*answer, attempt)

    try:
        legal = list(board.legal_moves)
        order = sorted(legal, key=lambda move: (move != baseline_move, not board.gives_check(move),
                                                not board.is_capture(move), move.uci()))
        sacrifices, geometries = [], {}
        for move in order:
            if time.monotonic() >= geometry_deadline or len(sacrifices) >= MAX_CANDIDATES:
                break
            geometry = sound_sacrifice.evidence(board, move, deadline=geometry_deadline)
            geometries[move.uci()] = geometry
            if geometry.get("eligible") is True and geometry.get("complete") is True:
                sacrifices.append(move)
        # A free-search quiet baseline would usually win another mixed root
        # search and conceal a near-best sacrifice. Search the intended group.
        # The baseline belongs to that group only if it has its own investment.
        if sacrifices:
            candidates = sacrifices
            result["candidate_group"] = "sacrifice"
        else:
            candidates = [move for move in order if _forcing(board, move)][:MAX_CANDIDATES]
            result["candidate_group"] = "forcing_critical" if candidates else "none"
        result["geometry_probes"] = len(geometries)
        result["candidates_uci"] = [move.uci() for move in candidates]
        if not candidates:
            final_reason = "no_tactical_candidates"
        else:
            remaining = (deadline - time.monotonic()) * 1000
            amount = remaining * .32
            if amount < MIN_SEARCH_MS:
                final_reason = "insufficient_search_budget"
            else:
                reply, final_info, packets, attempt = timed_search(amount, "candidate_set", root_moves=candidates,
                    publish=False, fresh=True, depth=baseline["depth"])
                selected = reply.move
                attempt["selected_uci"] = selected.uci() if selected else None
                common = _line(board, (packets or {}).get(baseline["depth"]), selected)
                final = _line(board, final_info, selected)
                attempt.update(common=common, final=final)
                if (selected not in candidates or final is None or common is None
                        or final["depth"] != baseline["depth"] or not _quality(common, baseline) or not _quality(final, baseline)
                        or final["depth"] == common["depth"] and final != common):
                    final_reason = "candidate_unreliable_or_outside_quality_floor"
                else:
                    remaining = (deadline - time.monotonic()) * 1000
                    amount = remaining * .52
                    if amount < MIN_SEARCH_MS:
                        final_reason = "insufficient_confirmation_budget"
                    else:
                        reply2, info2, packets2, confirm_attempt = timed_search(amount, "deeper_confirmation",
                            root_move=selected, publish=False, fresh=True, depth=baseline["depth"] + 1)
                        confirm_common = _line(board, (packets2 or {}).get(baseline["depth"]), selected)
                        deeper = _line(board, info2, selected)
                        depth_packet = _line(board, (packets2 or {}).get(baseline["depth"] + 1), selected)
                        confirm_attempt.update(common=confirm_common, final=deeper)
                        if (reply2.move != selected or deeper is None or depth_packet != deeper
                                or deeper["depth"] != baseline["depth"] + 1
                                or not _quality(confirm_common, baseline) or deeper["cp"] < MIN_CANDIDATE_CP):
                            final_reason = "deeper_confirmation_incomplete_or_outside_floor"
                        else:
                            alternatives = [move for move in legal if move != selected]
                            remaining = (deadline - time.monotonic()) * 1000 - 2
                            if remaining < MIN_SEARCH_MS:
                                final_reason = "insufficient_alternative_budget"
                            else:
                                reply3, info3, packets3, alternative_attempt = timed_search(remaining,
                                    "all_other_legal_roots", root_moves=alternatives,
                                    publish=False, fresh=True, depth=baseline["depth"])
                                alternative = _line(board, info3, reply3.move, allow_mate=True)
                                alternative_common = _line(board, (packets3 or {}).get(baseline["depth"]),
                                                           reply3.move, allow_mate=True)
                                alternative_attempt.update(root_count=len(alternatives), final=alternative,
                                                           common=alternative_common)
                                if (reply3.move not in alternatives or alternative is None
                                        or alternative["depth"] != baseline["depth"] or alternative_common != alternative):
                                    final_reason = "alternative_probe_incomplete"
                                else:
                                    geometry = geometries.get(selected.uci(), {})
                                    winning_elsewhere = (alternative["mate"] is not None and alternative["mate"] > 0
                                        or alternative["cp"] is not None and alternative["cp"] >= ALREADY_WINNING_CP)
                                    critical = (alternative["mate"] is not None and alternative["mate"] < 0
                                        or alternative["cp"] is not None and alternative["cp"] <= CRITICAL_ALTERNATIVE_CP)
                                    sacrifice = (geometry.get("eligible") is True and geometry.get("complete") is True
                                                 and not winning_elsewhere)
                                    if winning_elsewhere:
                                        final_reason = "already_winning_without_sacrifice"
                                    elif not sacrifice and not critical:
                                        final_reason = "no_qualified_tactical_opportunity"
                                    else:
                                        kinds = (["sound_sacrifice_candidate"] if sacrifice else []) + (["critical_opportunity"] if critical else [])
                                        compared = min((common, confirm_common), key=lambda line: line["cp"])
                                        chosen = {"move": selected, "kinds": kinds,
                                            "loss_cp": max(0, baseline["cp"] - compared["cp"]),
                                            "evidence": {"geometry": geometry, "candidate_common": common, "candidate_set_final": final,
                                                "confirmation_common": confirm_common, "deeper_confirmation": deeper,
                                                "alternative": alternative, "alternative_root_count": len(alternatives),
                                                "candidate_compared": compared, "candidate_final": deeper,
                                                "same_depth_quality": {"depth": baseline["depth"],
                                                    "baseline_cp": baseline["cp"], "candidate_cp": common["cp"],
                                                    "confirmation_cp": confirm_common["cp"], "max_loss_cp": MAX_LOSS_CP},
                                                "deeper_nonlosing_confirmation": {"depth": deeper["depth"],
                                                    "cp": deeper["cp"], "minimum_cp": MIN_CANDIDATE_CP,
                                                    "near_best_at_this_depth": False},
                                                "deeper_delta_from_common": deeper["cp"] - confirm_common["cp"],
                                                "sacrifice": geometry,
                                                "scope": "near-best floor compares only depth D; depth D+1 confirms nonlosing only, not relative best play; own finite search and local investment, not forced compensation or a site grade"}}
                                        final_reason = "native_verified_tactical_opportunity"
    except Exception as error:
        result["error"] = type(error).__name__
        discard = True
        chosen = None
        final_reason = "tactical_probe_failed"
    finally:
        watchdog.cancel()
        watchdog.join()
        done.set()
    if expired.is_set() or time.monotonic() >= deadline:
        return finish("tactical_probe_timeout", True)
    if chosen:
        result.update(selected_uci=chosen["move"].uci(), applied=chosen["move"] != baseline_move,
            qualified=True, kind=chosen["kinds"][0], kinds=chosen["kinds"],
            loss_cp=chosen["loss_cp"], evidence=chosen["evidence"])
    return finish(final_reason, discard)
