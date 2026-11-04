"""Separate attacking proposals from native, paired tactical verification.

The thresholds below are conservative native heuristics, not a reconstruction
of Chess.com ratings or its unpublished move-grading implementation. Local
investment evidence plus finite search is not a proof of compensation. The
finalist search keeps the baseline and the proposed attack at the same deeper
depth. Only its native winner can replace the baseline; all descendant replies
remain available. No external evaluator participates in move selection.
"""
import math
import threading
import time

import chess
import chess.engine

import sound_sacrifice

POLICY = "regicide-gambit-tactics-v4"
MIN_DEPTH = 4
MIN_BUDGET_MS = 100
MAX_BUDGET_MS = 1000
MAX_CANDIDATES = 8
MAX_LOSS_CP = 25
MIN_CANDIDATE_CP = 0
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
              "quality_qualified": False, "classification_scope": "local_native_verification_not_chesscom_grade",
              "kind": None, "kinds": [], "reason": "baseline_preserved", "loss_cp": 0,
              "evidence": None, "baseline": None, "attempts": [], "budget_ms": 0, "extra_elapsed_ms": 0,
              "budget_basis": "shared monotonic wall deadline; per-call requested ceilings may overlap when an earlier depth completes early",
              "threshold_basis": "native conservative heuristics; not exact Chess.com classification",
              "thresholds": {"max_loss_cp": MAX_LOSS_CP, "candidate_cp_min": MIN_CANDIDATE_CP,
                  "deeper_requirement": "candidate_wins_native_finalist_search"}}

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
            result["candidate_group"] = "forcing" if candidates else "none"
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
                    amount = remaining - 2
                    if amount < MIN_SEARCH_MS:
                        final_reason = "insufficient_confirmation_budget"
                    else:
                        # A positive deeper score alone is insufficient: the
                        # baseline may be substantially better at this depth.
                        # Compete both roots in one native search, using the
                        # former all-alternative grading time for verification.
                        # The root restriction never restricts opponent replies.
                        finalists = list(dict.fromkeys((selected, baseline_move)))
                        reply2, info2, packets2, confirm_attempt = timed_search(amount, "paired_verification",
                            root_moves=finalists, publish=False, fresh=True, depth=baseline["depth"] + 1)
                        deeper = _line(board, info2, reply2.move, allow_mate=True)
                        depth_packet = _line(board, (packets2 or {}).get(baseline["depth"] + 1),
                                             reply2.move, allow_mate=True)
                        confirm_attempt.update(roots_uci=[move.uci() for move in finalists], final=deeper)
                        if (reply2.move not in finalists or deeper is None or depth_packet != deeper
                                or deeper["depth"] != baseline["depth"] + 1):
                            final_reason = "paired_verification_incomplete_or_unreliable"
                        elif reply2.move != selected:
                            final_reason = "paired_verification_prefers_baseline"
                        elif deeper["mate"] is not None or deeper["cp"] < MIN_CANDIDATE_CP:
                            # Keep mate handling on the ordinary engine path;
                            # never coerce a mate score into a centipawn grade.
                            final_reason = "paired_verification_outside_floor"
                        else:
                            geometry = geometries.get(selected.uci(), {})
                            sacrifice = geometry.get("eligible") is True and geometry.get("complete") is True
                            kinds = ["sound_sacrifice_candidate" if sacrifice else "verified_forcing_candidate"]
                            chosen = {"move": selected, "kinds": kinds,
                                "loss_cp": max(0, baseline["cp"] - common["cp"]),
                                "evidence": {"geometry": geometry, "candidate_common": common,
                                    "candidate_set_final": final, "candidate_compared": common,
                                    "candidate_final": deeper, "deeper_confirmation": deeper,
                                    "same_depth_quality": {"depth": baseline["depth"],
                                        "baseline_cp": baseline["cp"], "candidate_cp": common["cp"],
                                        "max_loss_cp": MAX_LOSS_CP},
                                    "paired_verification": {"depth": deeper["depth"],
                                        "roots_uci": [move.uci() for move in finalists],
                                        "winner_uci": selected.uci(), "baseline_included": True,
                                        "comparison": "native_best_of_finalists", "global_best_proven": False},
                                    "deeper_delta_from_common": deeper["cp"] - common["cp"],
                                    "sacrifice": geometry,
                                    "scope": "depth D proposal floor, then native best of candidate and baseline at D+1; all descendant replies remain; finite local verification, not global best play, forced compensation or a site grade"}}
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
            qualified=True, quality_qualified=True, kind=chosen["kinds"][0], kinds=chosen["kinds"],
            loss_cp=chosen["loss_cp"], evidence=chosen["evidence"])
    return finish(final_reason, discard)
