"""Use the human time recommendation as a total opponent turn duration.

This policy does not select a move or increase search strength. Its fixed
target includes time already spent waiting for the client and searching.
"""

import hashlib
import math

import decision_support


POLICY = "regicide-engine-pacing-v2"
LOW_CLOCK_MS = 10_000
PRESSURE_START_MS = 180_000
WAIT_SLICE_SECONDS = .05


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _rhythm(board, support, seed):
    """Reproducible heuristic variation, not a learned human timing model."""
    if seed is None:
        return 1.0, "neutral"
    complexity = support.get("complexity", {})
    clarity = complexity.get("clarity", {}).get("kind", "unclassified")
    if clarity in ("single_reply", "mate_in_one", "clear_queen_gain", "familiar_opening"):
        low, high, profile = .4, 1.0, "quick_" + clarity
    elif complexity.get("score", 0) >= 60:
        low, high, profile = .8, 1.3, "complex_position"
    else:
        low, high, profile = .65, 1.35, "ordinary_position"
    digest = hashlib.sha256((str(seed) + "|" + board.fen(en_passant="fen") + "|" + POLICY).encode()).digest()
    # Two uniform draws give a central peak instead of frequent extremes.
    unit = (int.from_bytes(digest[:8], "big") + int.from_bytes(digest[8:16], "big")) / (2 * (2 ** 64 - 1))
    return low + (high - low) * unit, profile


def plan(board, clock, evaluation=None, *, seed=None):
    color = "white" if board.turn else "black"
    base = {"policy": POLICY, "recommendation_method": decision_support.METHOD,
            "enabled": False, "color": color, "root_ply": len(board.move_stack),
            "before_fen": board.fen(en_passant="fen"), "recommended_total_ms": 0,
            "target_total_ms": 0, "reserve_ms": 0, "spent_at_request_ms": 0,
            "initial_remaining_ms": None, "pressure_factor": 1.0,
            "variation_factor": 1.0, "rhythm_profile": "neutral",
            "pressure_start_ms": PRESSURE_START_MS, "reason": "untimed"}
    if not isinstance(clock, dict) or clock.get("enabled") is not True:
        return base
    current, elapsed = clock.get(color + "_ms"), clock.get("turn_elapsed_ms")
    if (clock.get("status") != "running" or clock.get("running_color") != color
            or not _number(current) or not _number(elapsed)):
        return {**base, "reason": "inactive_clock"}
    support = decision_support.assess(board, board.turn, clock, evaluation=evaluation)
    recommendation = support.get("recommendation", {})
    total, reserve = recommendation.get("total_ms"), recommendation.get("reserve_ms")
    base.update(initial_remaining_ms=current, spent_at_request_ms=elapsed)
    if recommendation.get("status") != "ready" or not _number(total) or not _number(reserve):
        return {**base, "reason": "no_recommendation"}
    factor = .5 + .5 * min(1.0, current / PRESSURE_START_MS)
    variation, profile = _rhythm(board, support, seed)
    base.update(recommended_total_ms=total, reserve_ms=max(LOW_CLOCK_MS, reserve), pressure_factor=factor,
                variation_factor=variation, rhythm_profile=profile)
    # Expected future increments influence the shared recommendation, but
    # only time already on the clock can fund the actual waiting period.
    search_reserve = min(150, max(1, current * .05))
    target = min(45000, total * variation * factor, elapsed + max(0, current - search_reserve))
    if current <= LOW_CLOCK_MS:
        return {**base, "target_total_ms": max(0, int(target)), "reason": "low_clock"}
    return {**base, "enabled": True, "target_total_ms": max(0, int(target)),
            "reason": "shared_human_recommendation"}


def search_budget(pacing, clock, configured_ms, *, automatic=False):
    """Allocate baseline+optional analysis inside the same total-turn target."""
    if not isinstance(clock, dict) or clock.get("enabled") is not True:
        return {"baseline_ms": configured_ms, "total_search_ms": None, "completion_margin_ms": 0,
                "automatic": False}
    current = clock[pacing["color"] + "_ms"]
    elapsed = clock["turn_elapsed_ms"]
    remainder = max(0, pacing["target_total_ms"] - elapsed)
    if pacing["reason"] in ("inactive_clock", "no_recommendation"):
        remainder = current
    available = max(0, min(remainder, current))
    margin = min(150, available * .10)
    total_search = max(1, int(available - margin))
    baseline = min(3000, max(100, total_search // 3)) if automatic else configured_ms
    baseline = max(1, min(baseline, total_search))
    return {"baseline_ms": baseline, "total_search_ms": total_search,
            "completion_margin_ms": round(margin, 3), "automatic": automatic}


def remaining_wait_ms(pacing, clock):
    if not pacing.get("enabled") or not isinstance(clock, dict):
        return 0
    color = pacing["color"]
    current, elapsed = clock.get(color + "_ms"), clock.get("turn_elapsed_ms")
    if (clock.get("enabled") is not True or clock.get("status") != "running"
            or clock.get("running_color") != color
            or not _number(current) or not _number(elapsed) or current <= LOW_CLOCK_MS):
        return 0
    return max(0, min(pacing["target_total_ms"] - elapsed,
                      current - pacing["reserve_ms"]))
