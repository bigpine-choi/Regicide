"""Reproducible human-style turn timing, independent of move selection.

The first plan allocates a search budget. After that search a single cosmetic
refinement uses its validated evidence, never starts another search, and never
rewrites the allocation. All targets include time already spent this turn.
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


POLICY = "regicide-engine-pacing-v3"
LOW_CLOCK_MS = 10_000
PRESSURE_START_MS = 180_000
WAIT_SLICE_SECONDS = .05
MAX_TURN_MS = 45_000
QUICK_KINDS = ("single_reply", "mate_in_one", "clear_queen_gain", "familiar_opening")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _unit(seed, *parts):
    key = "|".join((POLICY, str(seed), *(str(part) for part in parts)))
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / (2 ** 64 - 1)


def _rhythm_identity(seed):
    return hashlib.sha256((POLICY + "|" + str(seed)).encode()).hexdigest() if seed is not None else None


def _identity(board):
    return {"root_ply": len(board.move_stack), "before_fen": board.fen(en_passant="fen"),
            "root_fen": board.root().fen(en_passant="fen"),
            "history_uci": [move.uci() for move in board.move_stack]}


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


def plan(board, clock, evaluation=None, *, seed=None, history=None):
    color = "white" if board.turn else "black"
    base = {"policy": POLICY, "recommendation_method": decision_support.METHOD,
            "enabled": False, "color": color, **_identity(board),
            "recommended_total_ms": 0, "target_total_ms": 0, "reserve_ms": 0,
            "spent_at_request_ms": 0, "initial_remaining_ms": None,
            "pressure_factor": 1.0, "variation_factor": 1.0,
            "game_tempo_factor": 1.0, "local_beat_factor": 1.0,
            "rhythm_profile": "neutral", "pressure_start_ms": PRESSURE_START_MS,
            "reason": "untimed", "complexity_score": 0, "clarity_kind": "unclassified",
            "position_features": {}, "rhythm_identity": _rhythm_identity(seed),
            "alternatives_basis_ms": 0, "alternatives_variation_factor": 1.0}
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
    target = min(MAX_TURN_MS, total * variation * factor, elapsed + max(0, current - search_reserve))
    if current <= LOW_CLOCK_MS:
        return {**base, "target_total_ms": max(0, int(target)), "reason": "low_clock"}
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
    return spent + waited >= max(3000, target - 100, original * 1.4)


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
        eligible_long = complexity >= 45 and (details["surprise"] or evidence["unsettled"] or complexity >= 70)
        if (eligible_long and not details["long_think_cooldown"]
                and _unit(seed, "long-think", board.fen(en_passant="fen")) < .18):
            factor, kind = 1.85 + .40 * innovation, "long_deliberation"
    color = result["color"]
    if not isinstance(clock, dict):
        return result
    current, elapsed = clock.get(color + "_ms"), clock.get("turn_elapsed_ms")
    if (clock.get("enabled") is not True or clock.get("status") != "running"
            or clock.get("running_color") != color or not _number(current) or not _number(elapsed)):
        result.update(enabled=False, reason="inactive_clock")
        return result
    pressure = min(1.0, current / PRESSURE_START_MS)
    applied_factor = 1.0 + (factor - 1.0) * pressure if factor > 1 else factor
    # Re-evaluate pressure after search, not from a stale turn-start balance.
    pressure_factor = .5 + .5 * pressure
    time_basis = result["alternatives_basis_ms"] if use_alternative_basis else result["recommended_total_ms"]
    time_variation = result["alternatives_variation_factor"] if use_alternative_basis else result["variation_factor"]
    target = time_basis * time_variation * pressure_factor * applied_factor
    reserve = result["reserve_ms"]
    target = min(MAX_TURN_MS, target, elapsed + max(0, current - reserve))
    details.update(applied=True, kind=kind, factor=round(applied_factor, 6),
                   unpressured_factor=round(factor, 6), premium_pressure_fraction=round(pressure, 6),
                   time_basis_ms=time_basis, time_variation_factor=time_variation,
                   original_target_total_ms=pacing["target_total_ms"])
    result.update(rhythm_profile=kind, pressure_factor=pressure_factor,
                  target_total_ms=max(0, int(target)))
    if current <= LOW_CLOCK_MS:
        result.update(enabled=False, reason="low_clock")
    return result


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
