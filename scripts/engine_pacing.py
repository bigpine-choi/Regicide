"""Reproducible human-style turn timing, independent of move selection.

The first plan allocates a search budget. Validated move evidence refines the
turn target. Early sparring imbalance may spend that target on real recovery
search instead of a cosmetic hold. All targets include elapsed turn time.
This is an authored timing heuristic, not a learned human-response model.
"""

import copy
import hashlib
import math

import chess
import chess.engine

import decision_support
import evaluation_stability
import sparring_evaluation


POLICY = "regicide-engine-pacing-v13"
# Keep each game's established variation while changing its time allowance.
RHYTHM_NAMESPACE = "regicide-engine-pacing-v3"
LOW_CLOCK_MS = 10_000
PRESSURE_START_MS = 300_000
# Compatibility name for existing callers and archived policy descriptions.
SPARRING_PRESSURE_START_MS = PRESSURE_START_MS
WAIT_SLICE_SECONDS = .05
MAX_TURN_MS = 45_000
TURN_REDUCTION_MS = 1000
# Trim presentation time after search, including capped opening turns, without
# reducing the baseline, recovery, or tactical-verification search allowances.
POST_SEARCH_REDUCTION_MS = 1500
# Include search in this floor: shortening only the post-search hold could
# never produce a genuinely near-instant response to an obvious move.
MIN_TURN_TARGET_MS = 100
QUICK_KINDS = ("single_reply", "mate_in_one", "clear_queen_gain", "familiar_opening")
OPENING_MAX_FULLMOVE = 12


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _unit(seed, *parts):
    key = "|".join((RHYTHM_NAMESPACE, str(seed), *(str(part) for part in parts)))
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / (2 ** 64 - 1)


def _rhythm_identity(seed):
    return hashlib.sha256((POLICY + "|" + str(seed)).encode()).hexdigest() if seed is not None else None


def _identity(board):
    return {"root_ply": len(board.move_stack), "before_fen": board.fen(en_passant="fen"),
            "root_fen": board.root().fen(en_passant="fen"),
            "history_uci": [move.uci() for move in board.move_stack]}


def _shorter_target(target, current):
    """Reduce a fresh whole-turn target once, without extending fast turns.

    Clock safety caps are applied afterwards. Under ten seconds there is no
    artificial hold; preserve the remaining urgent search allowance instead
    of subtracting a second from a budget already measured in milliseconds.
    """
    target = min(MAX_TURN_MS, max(0, target))
    if current <= LOW_CLOCK_MS:
        return target
    return max(min(target, MIN_TURN_TARGET_MS), target - TURN_REDUCTION_MS)


def _rhythm(board, support, seed):
    """A shared game tempo plus correlated beats and small position variation."""
    if seed is None:
        return 1.0, "neutral", 1.0, 1.0
    complexity = support.get("complexity", {})
    clarity = complexity.get("clarity", {}).get("kind", "unclassified")
    tempo = .88 + .24 * _unit(seed, "game-tempo")
    turn = len(board.move_stack) // 2
    # Nearby own turns share two of three innovations. No mutable random
    # generator or unverified previous duration can affect undo/replay.
    beat_unit = sum(weight * _unit(seed, "beat", board.turn, turn - lag)
                    for lag, weight in enumerate((.5, .3, .2)))
    beat = .88 + .24 * beat_unit
    local = _unit(seed, "position", board.fen(en_passant="fen"))
    if clarity in QUICK_KINDS:
        factor = max(.30, min(1.0, tempo * beat * (.40 + .50 * local)))
        profile = "quick_" + clarity
    else:
        factor = max(.65, min(1.35, tempo * beat * (.80 + .40 * local)))
        profile = "complex_position" if complexity.get("score", 0) >= 60 else "ordinary_position"
    return factor, profile, tempo, beat


def _opening_budget(board, features, seed):
    """A modest early-development budget, not a claim of memorized theory.

    Recognize ordinary openings outside the tiny exact-position list. Material
    and tactical guards keep a low move counter alone from granting a discount.
    This changes engine timing only, not the human's displayed recommendation.
    """
    if (seed is None or board.fullmove_number > OPENING_MAX_FULLMOVE
            or features.get("nonpawn_pieces", 0) < 10 or features.get("pawns", 0) < 12):
        return None
    if (any(features.get(key, 0) for key in (
            "in_check", "own_loose_pieces", "enemy_loose_pieces", "overloaded_defenders",
            "promotion_pawns", "own_advanced_passers", "enemy_advanced_passers"))
            or features.get("attacked_king_neighbors", 0) >= 3
            or features.get("capture_choices", 0) >= 4
            or features.get("checking_choices", 0) >= 2
            or features.get("own_pinned_pieces", 0) >= 2):
        return None
    contact = (features.get("own_pinned_pieces", 0) or features.get("checking_choices", 0)
               or features.get("capture_choices", 0) >= 2 or features.get("tactical_contacts", 0) >= 3)
    variation = _unit(seed, "opening-budget", board.fen(en_passant="fen"))
    cap = 900 + 1100 * variation if contact else 350 + 850 * variation
    return {"kind": "contact_development" if contact else "quiet_development",
            "unpressured_cap_ms": int(cap), "max_fullmove": OPENING_MAX_FULLMOVE}


def plan(board, clock, evaluation=None, *, seed=None, history=None,
         pressure_start_ms=PRESSURE_START_MS):
    color = "white" if board.turn else "black"
    base = {"policy": POLICY, "recommendation_method": decision_support.METHOD,
            "enabled": False, "color": color, **_identity(board),
            "recommended_total_ms": 0, "target_total_ms": 0, "reserve_ms": 0,
            "spent_at_request_ms": 0, "initial_remaining_ms": None,
            "pressure_factor": 1.0, "variation_factor": 1.0,
            "game_tempo_factor": 1.0, "local_beat_factor": 1.0,
            "rhythm_profile": "neutral", "pressure_start_ms": pressure_start_ms,
            "turn_reduction_ms": TURN_REDUCTION_MS, "fast_target_floor_ms": MIN_TURN_TARGET_MS,
            "post_search_reduction_ms": POST_SEARCH_REDUCTION_MS,
            "reason": "untimed", "complexity_score": 0, "clarity_kind": "unclassified",
            "position_features": {}, "rhythm_identity": _rhythm_identity(seed),
            "alternatives_basis_ms": 0, "alternatives_variation_factor": 1.0,
            "opening_budget": None}
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
    factor = .5 + .5 * min(1.0, current / pressure_start_ms)
    variation, profile, tempo, beat = _rhythm(board, support, seed)
    complexity = support.get("complexity", {})
    alternatives_basis, alternatives_variation = total, variation
    if complexity.get("clarity", {}).get("kind") in ("familiar_opening", "mate_in_one", "clear_queen_gain"):
        # The shared guidance describes an obvious opportunity in this board.
        # A policy may deliberately select a different move. Preserve the
        # structural allowance too, without spending it on the initial search.
        structural = decision_support._complexity(complexity["features"])
        alternative = decision_support._recommend(board, board.turn, clock, structural, evaluation)
        if alternative.get("status") == "ready" and _number(alternative.get("total_ms")):
            alternatives_basis = alternative["total_ms"]
            alternatives_variation = _rhythm(board, {"complexity": structural}, seed)[0]
    base.update(recommended_total_ms=total, reserve_ms=max(LOW_CLOCK_MS, reserve), pressure_factor=factor,
                variation_factor=variation, rhythm_profile=profile,
                game_tempo_factor=tempo, local_beat_factor=beat,
                complexity_score=complexity.get("score", 0),
                clarity_kind=complexity.get("clarity", {}).get("kind", "unclassified"),
                position_features=copy.deepcopy(complexity.get("features", {})),
                alternatives_basis_ms=alternatives_basis, alternatives_variation_factor=alternatives_variation)
    # Future increments never fund this turn. The stronger wait reserve is
    # applied by remaining_wait_ms; this smaller margin preserves urgent search.
    search_reserve = min(150, max(1, current * .05))
    target = min(_shorter_target(total * variation * factor, current),
                 elapsed + max(0, current - search_reserve))
    if current <= LOW_CLOCK_MS:
        return {**base, "target_total_ms": max(0, int(target)), "reason": "low_clock"}
    opening_budget = _opening_budget(board, base["position_features"], seed)
    if opening_budget is not None:
        opening_budget["cap_ms"] = int(opening_budget["unpressured_cap_ms"] * factor)
        target = min(target, opening_budget["cap_ms"])
    base["opening_budget"] = opening_budget
    return {**base, "enabled": True, "target_total_ms": max(0, int(target)),
            "reason": "shared_human_recommendation"}


def _selected_line(board, selected_move, info):
    sample = sparring_evaluation.validate_packet(board, info)
    if sample is None or sample["depth"] < 4 or sample["root"] != selected_move.uci():
        return None
    return {"depth": sample["depth"], "pv_uci": sample["pv_uci"],
            "cp_white": sample["cp"], "mate_white": sample["mate"]}


def _read_forecast(board, row):
    value = row.get("forecast")
    if not isinstance(value, dict) or not isinstance(value.get("pv_uci"), list):
        return None
    try:
        moves = [chess.Move.from_uci(uci) for uci in value["pv_uci"]]
        cp, mate = value.get("cp_white"), value.get("mate_white")
        if type(cp) is int and mate is None:
            white = chess.engine.Cp(cp)
        elif type(mate) is int and cp is None:
            white = chess.engine.Mate(mate)
        else:
            return None
        score = chess.engine.PovScore(white, chess.WHITE).pov(board.turn)
        raw = {"depth": value.get("depth"), "score": chess.engine.PovScore(score, board.turn), "pv": moves}
        selected = chess.Move.from_uci(row["selected_uci"])
        return _selected_line(board, selected, raw)
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def _verified_history(board, history, rhythm_identity):
    """At most four prior own turns, authenticated against the current branch."""
    values = history.values() if isinstance(history, dict) else history if isinstance(history, (list, tuple)) else ()
    ply = len(board.move_stack)
    current = _identity(board)
    candidates = [row for row in values if isinstance(row, dict)
                  and row.get("policy") == POLICY and row.get("rhythm_identity") == rhythm_identity
                  and type(row.get("root_ply")) is int
                  and max(0, ply - 8) <= row["root_ply"] <= ply - 2
                  and (ply - row["root_ply"]) % 2 == 0]
    result, used = [], set()
    for row in sorted(candidates, key=lambda item: item["root_ply"], reverse=True):
        root_ply = row["root_ply"]
        if (root_ply in used or row.get("root_fen") != current["root_fen"]
                or row.get("history_uci") != current["history_uci"][:root_ply]
                or row.get("selected_uci") != current["history_uci"][root_ply]):
            continue
        previous = board.copy(stack=True)
        while len(previous.move_stack) > root_ply:
            previous.pop()
        if row.get("before_fen") != previous.fen(en_passant="fen"):
            continue
        used.add(root_ply)
        result.append((row, _read_forecast(previous, row)))
        if len(result) == 4:
            break
    return result


def _search_evidence(board, selected_move, packets):
    values = packets if isinstance(packets, dict) else {}
    depths = sorted(depth for depth, info in values.items()
                    if type(depth) is int and depth >= 4 and isinstance(info, dict)
                    and info.get("depth") == depth and "score" in info)[-3:]
    samples = [sparring_evaluation.validate_packet(board, values[depth]) for depth in depths]
    complete = len(depths) >= 2 and all(sample is not None for sample in samples)
    consecutive = complete and depths == list(range(depths[-1] - len(depths) + 1, depths[-1] + 1))
    roots = [sample["root"] for sample in samples] if complete else []
    cp_values = [sample["cp"] for sample in samples] if complete else []
    cp_span = max(cp_values) - min(cp_values) if cp_values and all(type(cp) is int for cp in cp_values) else None
    root_changes = sum(a != b for a, b in zip(roots, roots[1:]))
    # A changed root can reflect multiple similar choices; this only models
    # time spent reconsidering, never a probability of an error.
    invalid_latest = bool(samples and samples[-1] is None)
    unsettled = bool(invalid_latest or consecutive and (root_changes or cp_span is not None and cp_span > 80))
    stable = evaluation_stability.assess(board, values).get("reliability") == "stable"
    return {"completed_depths": depths, "cp_span": cp_span, "root_changes": root_changes,
            "invalid_latest": invalid_latest,
            "unsettled": unsettled, "stable_selected_root": bool(stable and roots and roots[-1] == selected_move.uci())}


def _simple_recapture(board, selected_move, features, evidence):
    if (not board.move_stack or not evidence["stable_selected_root"]
            or board.is_check() or board.gives_check(selected_move)
            or features.get("own_pinned_pieces", 0) or features.get("own_loose_pieces", 0)
            or features.get("overloaded_defenders", 0) or features.get("attacked_king_neighbors", 0) >= 3
            or features.get("checking_choices", 0) > 1 or features.get("capture_choices", 0) > 3):
        return False
    before = board.copy(stack=True)
    last = before.pop()
    return (before.is_capture(last) and board.is_capture(selected_move)
            and selected_move.to_square == last.to_square)


def _completed_long_think(row):
    """A proposed pause is not an experienced long turn until it completed."""
    refinement = row.get("refinement")
    if (not isinstance(refinement, dict) or refinement.get("kind") != "long_deliberation"
            or row.get("completion_reason") not in ("target_reached", "wall_deadline")):
        return False
    spent, waited = row.get("spent_before_wait_ms"), row.get("waited_ms")
    target, original = row.get("target_total_ms"), refinement.get("original_target_total_ms")
    if not all(_number(value) for value in (spent, waited, target, original)):
        return False
    # Three seconds is a floor for this cadence heuristic, not a human-data
    # threshold. A rushed/aborted/shortened pause cannot suppress the next one.
    # A completed pause at the global cap also counts. Without this limit a
    # 32s initial target stretched to 44s could never activate the cooldown.
    reduction = row.get("post_search_reduction_ms", 0)
    if not _number(reduction):
        return False
    return spent + waited >= max(3000, target - reduction - 100,
                                 min(original * 1.4, MAX_TURN_MS - TURN_REDUCTION_MS - 100) - reduction)


def refine(board, pacing, clock, *, selected_move, baseline_packets=None,
           selected_info=None, history=None, seed=None):
    """Refine visible cadence once, using the actual selected move's evidence.

    The returned plan is detached. Search allocation and chosen move are never
    changed. History and forecasts are used only on the exact current branch.
    """
    result = copy.deepcopy(pacing)
    if (not isinstance(result, dict) or result.get("policy") != POLICY
            or result.get("before_fen") != board.fen(en_passant="fen")
            or result.get("root_ply") != len(board.move_stack)):
        return result
    result.update(**_identity(board), selected_uci=None, forecast=None)
    details = {"applied": False, "kind": "no_selected_evidence", "factor": 1.0,
               "premium_pressure_fraction": 0.0, "expected_reply": False,
               "expected_continuation": False, "surprise": False, "long_think_cooldown": False}
    result["refinement"] = details
    if not isinstance(selected_move, chess.Move) or selected_move not in board.legal_moves:
        details["kind"] = "invalid_selected_move"
        return result
    result["selected_uci"] = selected_move.uci()
    clarity = result.get("clarity_kind", "unclassified")
    after = board.copy(stack=False)
    after.push(selected_move)
    known_continuation = (clarity == "familiar_opening" and
                          decision_support._position_key(after) in decision_support._familiar_positions())
    obvious_selected = (clarity == "single_reply"
                        or clarity == "mate_in_one" and after.is_checkmate()
                        or clarity == "clear_queen_gain" and board.piece_type_at(selected_move.to_square) == chess.QUEEN
                           and board.color_at(selected_move.to_square) != board.turn)
    line = _selected_line(board, selected_move, selected_info)
    result["forecast"] = line
    if result.get("balance_recovery", {}).get("active"):
        details["kind"] = "balance_recovery_search"
        return result
    if line is None or seed is None:
        details["kind"] = "neutral" if seed is None else "no_selected_evidence"
        if clarity in QUICK_KINDS and not obvious_selected and not known_continuation:
            result["rhythm_profile"] = "unverified_alternative"
        return result
    if result.get("rhythm_identity") != _rhythm_identity(seed):
        details["kind"] = "seed_mismatch"
        return result
    evidence = _search_evidence(board, selected_move, baseline_packets)
    details["search_evidence"] = evidence
    verified = _verified_history(board, history, result["rhythm_identity"])
    previous = next(((row, forecast) for row, forecast in verified
                     if row["root_ply"] == len(board.move_stack) - 2), None)
    if previous and previous[1] and len(previous[1]["pv_uci"]) >= 2:
        forecast = previous[1]["pv_uci"]
        details["expected_reply"] = forecast[1] == board.peek().uci()
        details["surprise"] = not details["expected_reply"]
        details["expected_continuation"] = (details["expected_reply"] and len(forecast) >= 3
                                                and forecast[2] == selected_move.uci())
    details["long_think_cooldown"] = any(_completed_long_think(row) for row, _forecast in verified[:2])
    clear_selected = obvious_selected or known_continuation and not evidence["unsettled"]
    use_alternative_basis = clarity in ("familiar_opening", "mate_in_one", "clear_queen_gain") and not clear_selected
    complexity = result.get("complexity_score", 0)
    innovation = _unit(seed, "post-search", board.fen(en_passant="fen"))
    factor, kind = 1.0, "ordinary_decision"
    if clear_selected:
        kind = "quick_" + clarity
    elif clarity in ("mate_in_one", "clear_queen_gain"):
        # The position offered an obvious move, but the actual policy selected
        # another. Do not falsely describe its timing as an immediate mate/grab.
        factor, kind = 1.25 + .25 * innovation, "alternative_to_obvious_move"
    elif details["expected_continuation"] and evidence["stable_selected_root"] and not evidence["unsettled"]:
        factor, kind = .45 + .20 * innovation, "expected_continuation"
    elif complexity <= 40 and _simple_recapture(board, selected_move, result.get("position_features", {}), evidence):
        factor, kind = .45 + .25 * innovation, "simple_recapture"
    else:
        if use_alternative_basis:
            kind = "unfamiliar_continuation"
        if details["surprise"]:
            factor, kind = 1.18 + .30 * innovation, "unexpected_reply"
        if evidence["unsettled"]:
            factor, kind = max(factor, 1.30 + .40 * innovation), "reconsidering_choices"
        eligible_long = (not result.get("opening_budget") and complexity >= 45
                         and (details["surprise"] or evidence["unsettled"] or complexity >= 70))
        if (eligible_long and not details["long_think_cooldown"]
                and _unit(seed, "long-think", board.fen(en_passant="fen")) < .10):
            # Less frequent, more distinct pauses: a long tail rather than
            # making every ordinary decision slower. Clock pressure below
            # the configured pressure boundary fades this premium and caps
            # the whole turn (five minutes in sparring).
            factor, kind = 2.80 + .60 * innovation, "long_deliberation"
    color = result["color"]
    if not isinstance(clock, dict):
        return result
    current, elapsed = clock.get(color + "_ms"), clock.get("turn_elapsed_ms")
    if (clock.get("enabled") is not True or clock.get("status") != "running"
            or clock.get("running_color") != color or not _number(current) or not _number(elapsed)):
        result.update(enabled=False, reason="inactive_clock")
        return result
    pressure = min(1.0, current / result.get("pressure_start_ms", PRESSURE_START_MS))
    applied_factor = 1.0 + (factor - 1.0) * pressure if factor > 1 else factor
    # Re-evaluate pressure after search, not from a stale turn-start balance.
    pressure_factor = .5 + .5 * pressure
    time_basis = result["alternatives_basis_ms"] if use_alternative_basis else result["recommended_total_ms"]
    time_variation = result["alternatives_variation_factor"] if use_alternative_basis else result["variation_factor"]
    # Recompute from the original recommendation, not the already shortened
    # plan target: post-search refinement must not subtract the second twice.
    target = _shorter_target(time_basis * time_variation * pressure_factor * applied_factor, current)
    opening_budget = result.get("opening_budget")
    if opening_budget is not None and current > LOW_CLOCK_MS:
        # Off-book choices and ordinary opening root changes must not restore
        # a multi-second middle-game hold after the short initial search.
        opening_budget["cap_ms"] = int(opening_budget["unpressured_cap_ms"] * pressure_factor)
        details["target_before_opening_cap_ms"] = int(target)
        target = min(target, opening_budget["cap_ms"])
    reserve = result["reserve_ms"]
    target = min(target, elapsed + max(0, current - reserve))
    details.update(applied=True, kind=kind, factor=round(applied_factor, 6),
                   unpressured_factor=round(factor, 6), premium_pressure_fraction=round(pressure, 6),
                   time_basis_ms=time_basis, time_variation_factor=time_variation,
                   original_target_total_ms=pacing["target_total_ms"])
    result.update(rhythm_profile=kind, pressure_factor=pressure_factor,
                  target_total_ms=max(0, int(target)))
    if current <= LOW_CLOCK_MS:
        result.update(enabled=False, reason="low_clock")
    return result


def reserve_analysis(pacing, clock, *, minimum_baseline_ms, optional_ms):
    """Reserve verified move-selection work inside the common turn target.

    The caller supplies requirements, never a mode name. This only raises a
    short timing target enough to investigate an eligible alternative. Actual
    balance, the ten-second reserve, and the ordinary turn ceiling take
    priority. Obvious moves keep their fast path.
    """
    result = copy.deepcopy(pacing) if isinstance(pacing, dict) else {}
    reserve = {"active": False, "reason": "inactive_plan",
               "requested_minimum_baseline_ms": minimum_baseline_ms if _number(minimum_baseline_ms) else None,
               "requested_optional_ms": optional_ms if _number(optional_ms) else None,
               "minimum_baseline_ms": 0, "optional_ms": 0,
               "target_before_ms": result.get("target_total_ms", 0),
               "target_total_ms": result.get("target_total_ms", 0),
               "completion_margin_ms": 150, "pressure_fraction": 0,
               "clock_reserve_ms": LOW_CLOCK_MS}
    result["analysis_reserve"] = reserve
    if not _number(minimum_baseline_ms) or not _number(optional_ms):
        reserve["reason"] = "invalid_analysis_requirements"
        return result
    if minimum_baseline_ms <= 0:
        reserve["reason"] = "no_analysis_requested"
        return result
    if (result.get("policy") != POLICY
            or not isinstance(clock, dict) or clock.get("enabled") is not True):
        return result
    color = result.get("color")
    current, elapsed = clock.get(str(color) + "_ms"), clock.get("turn_elapsed_ms")
    if (color not in ("white", "black") or clock.get("status") != "running"
            or clock.get("running_color") != color or not _number(current) or not _number(elapsed)):
        reserve["reason"] = "inactive_clock"
        return result
    if current <= LOW_CLOCK_MS:
        reserve["reason"] = "low_clock"
        return result
    if result.get("enabled") is not True:
        return result
    if result.get("clarity_kind") in ("single_reply", "mate_in_one", "clear_queen_gain"):
        reserve["reason"] = "obvious_move"
        return result
    pressure_start = result.get("pressure_start_ms", PRESSURE_START_MS)
    if not _number(pressure_start) or pressure_start <= 0:
        pressure_start = PRESSURE_START_MS
    pressure = min(1., current / pressure_start)
    optional = int(optional_ms * pressure ** 2)
    minimum = int(math.ceil(minimum_baseline_ms))
    available = max(0, min(current - LOW_CLOCK_MS, MAX_TURN_MS - elapsed))
    reserve.update(pressure_fraction=round(pressure, 6),
                   pressure_scaled_optional_ms=optional,
                   available_analysis_ms=int(available))
    if available < minimum + reserve["completion_margin_ms"]:
        reserve["reason"] = "insufficient_baseline_reserve"
        return result
    actual_optional = min(optional, max(0, int(available) - minimum - reserve["completion_margin_ms"]))
    required = minimum + actual_optional + reserve["completion_margin_ms"]
    original_target = result.get("target_total_ms", 0)
    original_target = original_target if _number(original_target) else 0
    target = min(max(original_target, elapsed + required), elapsed + available)
    reserve.update(active=True, minimum_baseline_ms=minimum, optional_ms=actual_optional,
                   target_total_ms=int(target),
                   reason="clock_capped_analysis" if actual_optional < optional else
                          "reserved_policy_analysis")
    result["target_total_ms"] = int(target)
    return result


def search_budget(pacing, clock, configured_ms, *, automatic=False):
    """Allocate baseline+optional analysis inside the same total-turn target."""
    if not isinstance(clock, dict) or clock.get("enabled") is not True:
        return {"baseline_ms": configured_ms, "total_search_ms": None, "completion_margin_ms": 0,
                "automatic": False, "optional_ms": None, "analysis_reserve_active": False}
    current = clock[pacing["color"] + "_ms"]
    elapsed = clock["turn_elapsed_ms"]
    remainder = max(0, pacing["target_total_ms"] - elapsed)
    if pacing["reason"] in ("inactive_clock", "no_recommendation"):
        remainder = current
    available = max(0, min(remainder, current))
    reserve = pacing.get("analysis_reserve") or {}
    reserved = automatic and reserve.get("active") is True
    margin = min(150, available if reserved else available * .10)
    total_search = max(1, int(available - margin))
    baseline = min(3000, max(100, total_search // 3)) if automatic else configured_ms
    if reserved:
        baseline = max(baseline, reserve["minimum_baseline_ms"])
    baseline = max(1, min(baseline, total_search))
    optional = (min(reserve["optional_ms"], max(0, total_search - baseline)) if reserved else
                0 if automatic else max(0, total_search - baseline))
    return {"baseline_ms": baseline, "total_search_ms": total_search,
            "completion_margin_ms": round(margin, 3), "automatic": automatic,
            "optional_ms": optional,
            "analysis_reserve_active": bool(reserved and baseline >= reserve["minimum_baseline_ms"])}


def recovery_clock_limit(clock, color, direction, *, pressure_start_ms=PRESSURE_START_MS):
    """Extra computation cap with continuous pressure from this game's boundary."""
    current = clock.get(color + "_ms") if isinstance(clock, dict) else None
    if not _number(current) or current <= LOW_CLOCK_MS:
        return 0
    pressure = min(1.0, current / pressure_start_ms)
    cap = 12000 if direction == "ahead" else 30000
    return max(0, min(cap * pressure ** 2, current - LOW_CLOCK_MS - 150))


def recovery_plan(board, pacing, clock, *, baseline_info, baseline_move):
    """Allocate actual recovery work only for an exact current-root imbalance.

    A shallow completed score can request confirmation, never certify a
    concession. The move policy retains its own depth and safety checks.
    """
    result = copy.deepcopy(pacing)
    recovery = {"active": False, "reason": "outside_close_phase", "budget_ms": 0,
                "direction": None, "initial_cp_own": None}
    result["balance_recovery"] = recovery
    if len(board.move_stack) >= 40 or board.root().fen() != chess.STARTING_FEN:
        return result
    sample = sparring_evaluation.validate_packet(board, baseline_info)
    if (sample is None or not isinstance(baseline_move, chess.Move)
            or sample["root"] != baseline_move.uci()):
        recovery["reason"] = "unverified_baseline"
        return result
    own_cp = sample["cp"] * (1 if board.turn else -1) if sample["cp"] is not None else None
    recovery.update(initial_cp_own=own_cp, baseline_depth=sample["depth"])
    if own_cp is not None and abs(own_cp) <= 100:
        recovery["reason"] = "within_close_band"
        return result
    color = "white" if board.turn else "black"
    direction = ("ahead" if own_cp > 100 else "behind") if own_cp is not None else (
        "ahead" if sample["winner"] == color else "behind")
    recovery["direction"] = direction
    if (not isinstance(clock, dict) or clock.get("enabled") is not True
            or clock.get("status") != "running" or clock.get("running_color") != color):
        recovery["reason"] = "inactive_clock"
        return result
    current, elapsed = clock.get(color + "_ms"), clock.get("turn_elapsed_ms")
    if not _number(current) or not _number(elapsed):
        recovery["reason"] = "invalid_clock"
        return result
    pressure_start_ms = result.get("pressure_start_ms", PRESSURE_START_MS)
    pressure = min(1.0, current / pressure_start_ms)
    target = max(result.get("target_total_ms", 0), 6000 * pressure)
    reserve = max(LOW_CLOCK_MS, result.get("reserve_ms", 0))
    budget = int(max(0, min(target - elapsed - 150, current - reserve - 150,
                            recovery_clock_limit(clock, color, direction,
                                                 pressure_start_ms=pressure_start_ms))))
    if budget < 100:
        recovery["reason"] = "insufficient_clock_budget"
        return result
    recovery.update(active=True, reason="early_balance_recovery", budget_ms=budget,
                    target_total_ms=int(elapsed + budget + 150),
                    original_target_total_ms=pacing.get("target_total_ms", 0),
                    opening_cap_bypassed=bool(result.get("opening_budget")))
    result.update(target_total_ms=recovery["target_total_ms"], balance_recovery=recovery,
                  rhythm_profile="balance_recovery_search")
    return result


def wait_target_ms(pacing):
    """Presentation deadline only; never feed this back into search budgets.

    Always derive it from the original target so polling and clock edits do
    not repeatedly subtract time. Legacy receipts retain their original pace.
    """
    return max(0, pacing["target_total_ms"] - pacing.get("post_search_reduction_ms", 0))


def remaining_wait_ms(pacing, clock):
    if (pacing.get("balance_recovery", {}).get("active") or pacing.get("safety_check", {}).get("active")
            or pacing.get("quality_check", {}).get("active")):
        return 0
    if not pacing.get("enabled") or not isinstance(clock, dict):
        return 0
    color = pacing["color"]
    current, elapsed = clock.get(color + "_ms"), clock.get("turn_elapsed_ms")
    if (clock.get("enabled") is not True or clock.get("status") != "running"
            or clock.get("running_color") != color
            or not _number(current) or not _number(elapsed) or current <= LOW_CLOCK_MS):
        return 0
    return max(0, min(wait_target_ms(pacing) - elapsed,
                      current - pacing["reserve_ms"]))
