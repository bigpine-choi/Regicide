"""Bounded, replay-derived practice concessions followed by full-strength play.

All scores and candidate searches come from the configured Regicide engine.
The loss target, smoothing, phase schedule and naturalness filter are explicit
practice heuristics, not Elo calibration, proven safety, or a promise to win.
The full-budget baseline is supplied by the host and survives every failure.
"""

import math
import re
import threading
import time

import chess
import chess.engine

import boss_play
import evaluation_stability
import opening_variety as opening
import sparring_pacing
import sparring_evaluation

POLICY = "regicide-sparring-play-v6"
SUPPORTED_POLICIES = ("regicide-sparring-play-v1", "regicide-sparring-play-v2",
                      "regicide-sparring-play-v3", "regicide-sparring-play-v4",
                      "regicide-sparring-play-v5", POLICY)
MIN_TOTAL_MS = 600
MIN_DEPTH = 4
MIN_OBSERVATIONS = 3
WINDOW = 12
PRIOR_LOSS_CP = 35.0
PRIOR_WEIGHT = 3.0
RECENCY = .85
MAX_PROFILE_STEP_CP = 12.0
MAX_TARGET_CP = 90
MAX_LOSS_CP = 120
# The preceding two caps are retained only for the historical skill-profile
# scale. Position recovery does not use them as candidate loss limits.
BAND_CP = 100
TARGET_CP = 30
REFERENCE_TOLERANCE_CP = 50
MAX_EXTRA_MS = 2000
QUALITY_BUDGET_MS = 500
# This short pass only orders candidates. Selection still requires an exact
# re-search at the full-budget baseline's completed depth (at least MIN_DEPTH).
DISCOVERY_DEPTH = 3
MAX_DISCOVERY = 16
RANKED_DISCOVERY = 6
MAX_PROBES = 4
MAX_RECOVERY_EXTRA_MS = 12000
RECOVERY_DISCOVERY = 40
RECOVERY_RANKED_DISCOVERY = 8
RECOVERY_PROBES = 10
RECOVERY_POSTMOVE_RESERVE_MS = 2400
POSTMOVE_POLICY = "regicide-sparring-postmove-v1"
POSTMOVE_MIN_MS = 100
POSTMOVE_MAX_MS = 600
POSTMOVE_RESERVE_FRACTION = .3
# Match the existing completed-iteration stability window. This is an
# explicit disagreement screen, not a statistical error bound.
POSTMOVE_MAX_DELTA_CP = 80
RAMP_PLY = 40
FINISH_PLY = 98
PREVIOUS_FINISH_PLY = 138
V3_RAMP_PLY = 60
V2_RAMP_PLY = 80
LEGACY_RAMP_PLY = 100
LEGACY_FINISH_PLY = 140
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def _rows(decisions):
    if not isinstance(decisions, (dict, list, tuple)):
        return []
    values = decisions.values() if isinstance(decisions, dict) else decisions
    return [row for row in values if isinstance(row, dict)]


def _history(value):
    return tuple(value) if isinstance(value, (tuple, list)) and all(type(x) is str for x in value) else None


def _replay(board):
    position = board.root()
    history, fens = [], [position.fen(en_passant="fen")]
    for move in board.move_stack:
        if move not in position.legal_moves:
            return None
        history.append(move.uci())
        position.push(move)
        fens.append(position.fen(en_passant="fen"))
    return (tuple(history), fens) if position.fen(en_passant="fen") == board.fen(en_passant="fen") else None


def _finish_ply(policy):
    if policy == SUPPORTED_POLICIES[0]:
        return LEGACY_FINISH_PLY
    return PREVIOUS_FINISH_PLY if policy in SUPPORTED_POLICIES[1:4] else FINISH_PLY


def phase(board, *, policy=None):
    """v5/v6: close through move 20, ramp 21..49, full on each side's move 50."""
    ply = len(board.move_stack)
    legacy = policy == SUPPORTED_POLICIES[0]
    if ply >= _finish_ply(policy):
        return "finish"
    ramp_ply = (LEGACY_RAMP_PLY if legacy else V2_RAMP_PLY if policy == SUPPORTED_POLICIES[1]
                else V3_RAMP_PLY if policy == SUPPORTED_POLICIES[2] else RAMP_PLY)
    if ply >= ramp_ply:
        return "ramp"
    return "calibration" if ply < 6 else "sparring"


def extra_budget(movetime_ms, *, ply=0, analysis_only=False):
    if movetime_ms < MIN_TOTAL_MS:
        return 0
    return QUALITY_BUDGET_MS if analysis_only or ply >= FINISH_PLY else min(MAX_EXTRA_MS, 2 * movetime_ms)


def _ramp_fraction(board, *, policy=None):
    ramp_ply = (V2_RAMP_PLY if policy == SUPPORTED_POLICIES[1]
                else V3_RAMP_PLY if policy == SUPPORTED_POLICIES[2] else RAMP_PLY)
    if len(board.move_stack) < ramp_ply:
        return 0.0
    close_fullmoves = ramp_ply // 2
    full_strength_move = _finish_ply(policy) // 2 + 1
    return min(1., max(0., (board.fullmove_number - close_fullmoves) / (full_strength_move - close_fullmoves)))


def _phase_factor(board, *, policy=None):
    stage = phase(board, policy=policy)
    if stage in ("calibration", "finish"):
        return 0.0
    if stage != "ramp":
        return 1.0
    if policy == SUPPORTED_POLICIES[0]:
        return max(0., (LEGACY_FINISH_PLY - len(board.move_stack)) / (LEGACY_FINISH_PLY - LEGACY_RAMP_PLY))
    return 1 - _ramp_fraction(board, policy=policy)


def derive_profile(board, decisions, *, policy=None):
    """Rebuild verified observations from the surviving game, including undo.

    A rolling, recency-weighted 12-observation mean is shrunk towards a modest
    prior. The smoothed estimate can move at most 12cp per valid human move.
    Missing observations never count as accurate play. Trap replies are unused.
    """
    policy = POLICY if policy is None else policy
    if policy not in SUPPORTED_POLICIES:
        raise ValueError("unsupported sparring profile policy")
    replay = _replay(board)
    history, fens = replay if replay else ((), [])
    found, ignored = {}, 0
    for row in _rows(decisions):
        observation = row.get("opponent_observation")
        if not isinstance(observation, dict):
            continue
        prefix = _history(observation.get("history_before"))
        row_history = _history(row.get("history_uci"))
        provisional = set(row) == {"opponent_observation"}
        loss, depth = observation.get("loss_cp"), observation.get("depth")
        valid = (replay is not None and prefix is not None and len(prefix) < len(history)
                 and history[:len(prefix)] == prefix and observation.get("valid") is True
                 and observation.get("forced") is False and observation.get("ply") == len(prefix) + 1
                 and observation.get("played_uci") == history[len(prefix)]
                 and observation.get("before_fen") == fens[len(prefix)]
                 and type(depth) is int and depth >= MIN_DEPTH
                 and type(loss) in (int, float) and math.isfinite(loss) and loss >= 0)
        if valid and provisional:
            valid = len(prefix) + 1 == len(history)
        elif valid:
            valid = (row_history == history[:len(prefix) + 1]
                     and row.get("before_fen") == fens[len(prefix) + 1]
                     and (len(row_history) == len(history)
                          or row.get("selected_uci") == history[len(row_history)]))
        if not valid:
            ignored += 1
            continue
        key = len(prefix)
        item = {"ply": key + 1, "played_uci": observation["played_uci"],
                "loss_cp": loss, "depth": depth}
        # Repeated receipts do not manufacture evidence. Conflicting duplicates
        # are discarded rather than letting iteration order change the model.
        if key in found and found[key] != item:
            found[key] = None
        elif key not in found:
            found[key] = item
    observations = [item for _key, item in sorted(found.items()) if item is not None]
    estimate, recent = PRIOR_LOSS_CP, []
    for item in observations:
        recent = [*recent, item][-WINDOW:]
        weights = [RECENCY ** age for age in reversed(range(len(recent)))]
        desired = (PRIOR_WEIGHT * PRIOR_LOSS_CP
                   + sum(weight * min(300, value["loss_cp"]) for value, weight in zip(recent, weights)))
        desired /= PRIOR_WEIGHT + sum(weights)
        estimate += max(-MAX_PROFILE_STEP_CP, min(MAX_PROFILE_STEP_CP, desired - estimate))
    count = len(observations)
    skill_target = min(MAX_TARGET_CP, max(0, (estimate - 20) * .9)) if count >= MIN_OBSERVATIONS else 0
    target = skill_target * _phase_factor(board, policy=policy)
    current_phase = phase(board, policy=policy)
    status = "adapting" if count >= MIN_OBSERVATIONS else "calibrating"
    if replay is None or current_phase == "finish":
        status, target = "holding", 0
    return {"policy": policy, "status": status, "level": round(1 - target / MAX_LOSS_CP, 4),
            "confidence": "high" if len(recent) >= 10 else "medium" if len(recent) >= 6 else "low",
            "confidence_scope": "heuristic evidence coverage, not a probability or rating",
            "observations": count, "recent_observations": recent,
            "ignored_observations": ignored, "conflicting_observations": sum(x is None for x in found.values()),
            "observation_weight": round(sum(RECENCY ** i for i in range(len(recent))), 4),
            "smoothed_loss_cp": round(estimate, 3), "skill_target_loss_cp": round(skill_target, 3),
            "target_loss_cp": round(target, 3), "phase_factor": round(_phase_factor(board, policy=policy), 4),
            "error_band": "unknown" if count < MIN_OBSERVATIONS else
                          "accurate" if estimate <= 30 else "mixed" if estimate <= 75 else "frequent_errors",
            "phase": current_phase, "history_valid": replay is not None,
            "scope": "current-game own-engine loss estimate; no Elo or win-probability claim"}


def memo_key(board, budget_ms, binary_sha256, evaluation_id, *, seed="", profile=None):
    return opening._digest([POLICY, opening.history_key(board), budget_ms, binary_sha256,
                            evaluation_id, seed, profile])


def _capture_value(board, move):
    if board.is_en_passant(move):
        return VALUES[chess.PAWN]
    victim = board.piece_at(move.to_square)
    return VALUES[victim.piece_type] if victim else 0


def naturalness(board, move, *, pacing=None, resource_guard=None):
    """Reject obvious one-reply donations, not a proof of human-like play.

    For each opponent capture of a minor or larger piece, include the best
    immediate legal recapture and material already won by the candidate. This
    permits ordinary equal exchanges but rejects e.g. a knight for a pawn.
    """
    if move not in board.legal_moves:
        return {"admitted": False, "reason": "illegal", "style_points": 0, "vulnerable_captures": []}
    piece = board.piece_at(move.from_square)
    gained = _capture_value(board, move)
    if move.promotion:
        gained += VALUES[move.promotion] - 100
    after = board.copy(stack=True)
    after.push(move)
    vulnerabilities = []
    immediate_mate = False
    replies = list(after.legal_moves)
    for reply in replies:
        captured = _capture_value(after, reply)
        if captured < 300 and not after.gives_check(reply):
            continue
        branch = after.copy(stack=True)
        branch.push(reply)
        if branch.is_checkmate():
            immediate_mate = True
        if captured < 300:
            continue
        recaptured = max((_capture_value(branch, answer) for answer in branch.legal_moves
                          if answer.to_square == reply.to_square and branch.is_capture(answer)), default=0)
        swing = gained - captured + recaptured
        if swing <= -200:
            vulnerabilities.append({"reply_uci": reply.uci(), "victim_cp": captured,
                                    "recapture_cp": recaptured, "candidate_capture_cp": gained,
                                    "net_material_cp": swing,
                                    "resources_after_reply": sparring_pacing.current_resources(branch, board.turn)})
    home_rank = 0 if board.turn else 7
    development = int(piece.piece_type in (chess.KNIGHT, chess.BISHOP)
                      and chess.square_rank(move.from_square) == home_rank
                      and chess.square_rank(move.to_square) != home_rank)
    center = int(move.to_square in (chess.D4, chess.E4, chess.D5, chess.E5))
    threats = sum(1 for square in after.attacks(move.to_square)
                  if (target := after.piece_at(square)) is not None
                  and target.color != board.turn and VALUES[target.piece_type] >= 300)
    style = 3 * development + 2 * int(board.is_castling(move)) + center + min(2, threats)
    terminal = after.is_game_over(claim_draw=True)
    # A delayed, compensated exchange can return some previously won material.
    # Merely leaving a piece en prise (or capturing a pawn with a queen) cannot
    # use this exception. Final exact engine verification remains mandatory.
    plan = pacing if isinstance(pacing, dict) else {}
    guard = resource_guard if isinstance(resource_guard, dict) else {}
    cap = plan.get("return_cap_cp")
    material_return = bool(
        vulnerabilities and not immediate_mate and not terminal
        and plan.get("active") is True and plan.get("allow_material_return") is True
        and guard.get("material_concessions_blocked") is not True
        and type(plan.get("stage")) is int and plan["stage"] in (3, 4)
        and type(cap) in (int, float) and math.isfinite(cap) and cap > 0
        and board.is_capture(move) and gained >= max(300, VALUES[piece.piece_type] / 2)
        and all(-row["net_material_cp"] <= cap
                and chess.Move.from_uci(row["reply_uci"]).to_square == move.to_square
                and row["victim_cp"] == VALUES[piece.piece_type] for row in vulnerabilities))
    critical_after_return = material_return and any(
        row["resources_after_reply"]["active"] for row in vulnerabilities)
    material_return = material_return and not critical_after_return
    reason = ("immediate_mate_reply" if immediate_mate else "obvious_material_donation" if vulnerabilities
              else "terminal_or_claimable_draw" if terminal else "no_obvious_one_reply_donation")
    if critical_after_return:
        reason = "material_return_consumes_critical_resources"
    elif material_return:
        reason = "delayed_compensated_exchange"
    return {"admitted": material_return or not (immediate_mate or vulnerabilities or terminal), "reason": reason,
            "material_return_exception": material_return,
            "material_return_cap_cp": cap if material_return else None,
            "style_points": style, "development": development, "direct_piece_threats": threats,
            "opponent_legal_moves": len(replies), "opponent_king_moves": sum(
                after.piece_type_at(reply.from_square) == chess.KING for reply in replies),
            "forcing_check": after.is_check(),
            "vulnerable_captures": vulnerabilities,
            "scope": "local legal capture screen; not exhaustive tactical or human-perception proof"}


def _target(board, profile, baseline_cp):
    """Position feedback is independent of the historical skill-loss scale."""
    ply = len(board.move_stack)
    ramp = _ramp_fraction(board)
    active = phase(board) != "finish"
    target = TARGET_CP + ramp * (baseline_cp - TARGET_CP)
    upper = BAND_CP + ramp * max(0, baseline_cp - BAND_CP)
    return {"active": active, "basis": "own_engine_selected_root",
            "lower_cp": -BAND_CP if active else None, "upper_cp": round(upper, 3) if active else None,
            "target_cp": round(target, 3), "ramp_fraction": round(ramp, 4),
            "baseline_cp": baseline_cp, "selected_cp": baseline_cp, "selected_final_cp": baseline_cp,
            "selected_postmove_cp": None,
            "status": "pending" if active else "inactive", "deviation_cp": None,
            "pre20_inside": -BAND_CP <= baseline_cp <= BAND_CP,
            "pre30_inside": -BAND_CP <= baseline_cp <= BAND_CP,
            "pre40_inside": -BAND_CP <= baseline_cp <= BAND_CP,
            "pre50_inside": -BAND_CP <= baseline_cp <= BAND_CP, "recovered": False,
            "failure_reason": None,
            "selection_objective": "inside band first, then nearest target; small-distance ties prefer less concession and natural style",
            "scope": "own-engine root estimates; selected alternatives also require a recorded post-move counter-search; not a guarantee"}


def _paced_control(control, pacing):
    """Retain the ordinary band separately from temporary recovery targets."""
    control["nominal_target_cp"] = control["target_cp"]
    control["nominal_upper_cp"] = control["upper_cp"]
    control["pacing_applied"] = False
    if control["active"] and pacing.get("active") is True:
        target, upper = pacing.get("target_cp"), pacing.get("upper_cp")
        if all(type(value) in (int, float) and math.isfinite(value) for value in (target, upper)):
            control["target_cp"] = max(control["target_cp"], target)
            control["upper_cp"] = max(control["upper_cp"], upper)
            control["pacing_applied"] = True
            control["selection_objective"] = (
                "temporary recovery band first, then nearest paced target; "
                "ordinary band compliance is reported separately")
    return control


def _deviation(control, cp):
    return max(control["lower_cp"] - cp, cp - control["upper_cp"], 0)


def _candidate_pressure(board, baseline_move, natural):
    after = board.copy(stack=True)
    after.push(baseline_move)
    replies = list(after.legal_moves)
    kings = sum(after.piece_type_at(move.from_square) == chess.KING for move in replies)
    points = min(8, max(0, len(replies) - natural["opponent_legal_moves"]))
    points += 2 * max(0, kings - natural["opponent_king_moves"])
    if natural["forcing_check"] or after.is_check():
        points = 0
    weight = _ramp_fraction(board)
    return {"points": points, "weight": round(weight, 4),
            "scope": "geometric tie-break among verified candidates, not a proven playing style"}


def _mate_reference(board, info, required_move):
    """Keep mate as mate; require an exact legal PV ending in that side's mate."""
    if (not isinstance(info, dict) or type(info.get("depth")) is not int or info["depth"] < 1
            or info.get("lowerbound") or info.get("upperbound") or info.get("multipv", 1) != 1):
        return None
    score, pv = info.get("score"), info.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or type(score.relative.mate()) is not int or score.relative.mate() == 0
            or not isinstance(pv, (tuple, list)) or not 0 < len(pv) <= 128 or pv[0] != required_move):
        return None
    replay = board.copy(stack=True)
    for move in pv:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return None
        replay.push(move)
    winner = board.turn if score.relative.mate() > 0 else not board.turn
    expected_plies = 2 * abs(score.relative.mate()) - (1 if score.relative.mate() > 0 else 0)
    if not replay.is_checkmate() or replay.turn == winner or len(pv) != expected_plies:
        return None
    return {"uci": required_move.uci(), "pv_uci": [move.uci() for move in pv], "depth": info["depth"],
            "cp": None, "cp_white": None, "mate": score.relative.mate(), "mate_white": score.white().mate(),
            "winner": "white" if winner else "black", "pov": "white" if board.turn else "black",
            "source_fen": board.fen(en_passant="fen"), "score_bound": "exact"}



def _score_line(board, info, required_move=None):
    # A secondary MultiPV line cannot certify an unrestricted position score.
    if not isinstance(info, dict) or info.get("multipv", 1) != 1:
        return None
    return boss_play.score_line(board, info, required_move)


def _cp_pv_ends_in_mate(board, line):
    if line is None:
        return False
    replay = board.copy(stack=True)
    for uci in line["pv_uci"]:
        replay.push_uci(uci)
    return replay.is_checkmate()


def _absolute_probe(board, move, packets, final_info, returned_move, depth, budget, elapsed):
    common = _score_line(board, packets.get(depth), move)
    final = _score_line(board, final_info, move)
    valid = (returned_move == move and common is not None and final is not None and depth >= MIN_DEPTH
             and final["depth"] >= depth and (final["depth"] != depth or common["cp"] == final["cp"])
             and not _cp_pv_ends_in_mate(board, common) and not _cp_pv_ends_in_mate(board, final))
    return {"uci": move.uci(), "admitted": valid, "reason": "exact_absolute_target" if valid else "absolute_candidate_unreliable",
            "loss_cp": None, "reference_delta_cp": None, "compared_depth": depth,
            "candidate_compared": common, "candidate_final": final,
            "candidate_final_raw": opening.packet_summary(board, final_info),
            "budget_ms": round(budget, 3), "elapsed_ms": round(elapsed, 3), "error": None}



def _postmove_verification(board, move, candidate, search, *, depth, budget_ms, floor_cp):
    """Search all opponent replies after a concession, without publishing it.

    The child starts one ply after the original root. A completed child depth
    of root_depth - 1 therefore preserves the compared horizon, with a floor
    of four. Keep the original restricted-root evidence and this independent
    unrestricted search separate; neither is a proof of tactical safety.
    """
    after = board.copy(stack=True)
    after.push(move)
    receipt = {"policy": POSTMOVE_POLICY, "valid": False, "reason": "postmove_unreliable",
               "after_fen": after.fen(en_passant="fen"), "candidate_uci": move.uci(),
               "required_depth": depth, "budget_ms": round(budget_ms, 3), "elapsed_ms": 0,
               "reply_uci": None, "line": None, "cp_own": None, "cp_white": None,
               "disagreement_cp": None, "max_disagreement_cp": POSTMOVE_MAX_DELTA_CP,
               "floor_cp": floor_cp, "search_scope": "unrestricted opponent replies; separate from current-position gauge"}
    if after.is_game_over(claim_draw=True):
        receipt["reason"] = "postmove_terminal"
        return receipt
    started = time.monotonic()
    reply, final, packets = search(budget_ms, position=after, publish=False, fresh=True, depth=depth)
    receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    receipt["reply_uci"] = reply.move.uci() if isinstance(reply.move, chess.Move) else None
    if receipt["elapsed_ms"] > budget_ms + 20:
        receipt["reason"] = "postmove_verification_timeout"
        return receipt
    if not isinstance(final, dict) or final.get("multipv", 1) != 1:
        return receipt
    line = _score_line(after, final, reply.move) if isinstance(reply.move, chess.Move) else None
    packet = packets.get(final.get("depth")) if isinstance(packets, dict) else None
    completed = (_score_line(after, packet, reply.move)
                 if isinstance(packet, dict) and packet.get("multipv", 1) == 1 and line else None)
    receipt["line"] = line
    if (line is None or line["depth"] < depth or completed != line
            or _cp_pv_ends_in_mate(after, line)):
        return receipt
    # score_line is from the opponent-to-move's view. White CP is invariant.
    own_cp = -line["cp"]
    disagreement = max(abs(own_cp - candidate[key]["cp"])
                       for key in ("candidate_compared", "candidate_final"))
    receipt.update(cp_own=own_cp, cp_white=line["cp_white"], disagreement_cp=disagreement)
    if own_cp < floor_cp:
        receipt["reason"] = "postmove_below_band_floor"
    elif disagreement > POSTMOVE_MAX_DELTA_CP:
        receipt["reason"] = "postmove_score_disagreement"
    else:
        receipt.update(valid=True, reason="postmove_verified")
    return receipt


def _choose_verified(board, baseline_move, baseline, probes, control, seed):
    """Rank exact evidence; neither discovery scores nor profile labels enter."""
    pool = [{"uci": baseline_move.uci(), "cp": baseline["cp"], "final_cp": baseline["cp"],
             "loss": 0 if baseline["cp"] is not None else None, "style": 0, "probe": None}]
    for probe in probes:
        if not probe["admitted"]:
            continue
        natural = probe["naturalness"]
        pressure = _candidate_pressure(board, baseline_move, natural)
        probe["pressure"] = pressure
        pool.append({"uci": probe["uci"], "cp": probe["candidate_compared"]["cp"],
                     "final_cp": probe["candidate_final"]["cp"], "loss": probe["loss_cp"],
                     "style": natural["style_points"] + pressure["weight"] * pressure["points"], "probe": probe})
    for item in pool:
        values = (item["cp"], item["final_cp"])
        if any(value is None for value in values):
            item.update(inside=False, deviation=float("inf"), distance=float("inf"))
            continue
        item["inside"] = all(control["lower_cp"] <= cp <= control["upper_cp"] for cp in values)
        item["deviation"] = max(_deviation(control, cp) for cp in values)
        item["distance"] = max(abs(cp - control["target_cp"]) for cp in values)
    inside = [item for item in pool if item["inside"]]
    eligible = inside or pool
    # Within the band use its actual center target. Outside, first make the
    # largest verified progress towards the band, then towards the center.
    metric = "distance" if inside else "deviation"
    best = min(item[metric] for item in eligible)
    near = [item for item in eligible if item[metric] <= best + (5 if inside else 0)]
    return min(near, key=lambda item: (item["loss"] if item["loss"] is not None else -(item["cp"] or 0),
                                      -item["style"], item["distance"],
                                      opening._digest([POLICY, seed, opening.history_key(board), item["uci"]])))


def choose(board, baseline_move, baseline_info, baseline_packets, search, close_engine, *,
           decisions, seed, movetime_ms, forced=False, recovery_budget_ms=None):
    """Preserve the full search; spend bounded extra time on position feedback.

    Finite alternatives reach the original finite baseline's completed depth.
    An exact winning-mate reference instead uses an absolute CP target with
    candidate depth at least four; mate is never converted to CP. An unstable
    finite baseline can be checked again without replacing its original label.
    An explicit recovery allowance is extra work, not a minimum waiting time.
    The host must cap each search to its fresh clock/turn deadline as well.
    """
    started = time.monotonic()
    rows = _rows(decisions)
    finite_baseline = _score_line(board, baseline_info, baseline_move)
    reference_mate = _mate_reference(board, baseline_info, baseline_move) if finite_baseline is None else None
    baseline = finite_baseline or reference_mate
    absolute = reference_mate is not None and reference_mate["mate"] > 0 and len(board.move_stack) < RAMP_PLY
    verification_depth = max(MIN_DEPTH, baseline["depth"]) if baseline else None
    reliability = evaluation_stability.assess(board, baseline_packets)
    profile = derive_profile(board, rows)
    pacing = (sparring_pacing.derive(board, rows, baseline["cp"], ramp_fraction=_ramp_fraction(board))
              if finite_baseline else {"active": False, "reason": "no_finite_reference"})
    resource_guard = sparring_pacing.resource_guard(board)
    analysis_only = phase(board) == "finish" or resource_guard.get("active") is True
    extra = extra_budget(movetime_ms, ply=len(board.move_stack), analysis_only=analysis_only)
    if recovery_budget_ms is not None and (type(recovery_budget_ms) is not int or recovery_budget_ms < 0):
        raise ValueError("Recovery allowance must be nonnegative integer milliseconds.")
    standard_history = (not board.chess960 and board.is_valid()
                        and board.root().fen(en_passant="fen") == chess.STARTING_FEN
                        and _replay(board) is not None)
    recovery_eligible = (standard_history and not analysis_only and len(board.move_stack) < RAMP_PLY
                         and (absolute or finite_baseline is not None
                              and finite_baseline["depth"] >= MIN_DEPTH and finite_baseline["cp"] > BAND_CP))
    recovery = bool(recovery_eligible and recovery_budget_ms)
    if recovery:
        extra = min(MAX_RECOVERY_EXTRA_MS, recovery_budget_ms)
    discovery_limit = RECOVERY_DISCOVERY if recovery else MAX_DISCOVERY
    ranked_limit = RECOVERY_RANKED_DISCOVERY if recovery else RANKED_DISCOVERY
    probe_limit = RECOVERY_PROBES if recovery else MAX_PROBES
    control = _target(board, profile, baseline["cp"]) if finite_baseline else {
        "active": phase(board) != "finish", "basis": "own_engine_selected_root",
        "lower_cp": -BAND_CP, "upper_cp": BAND_CP, "target_cp": TARGET_CP,
        "baseline_cp": None, "selected_cp": None, "selected_final_cp": None, "selected_postmove_cp": None,
        "status": "unmet" if reference_mate else "unavailable", "deviation_cp": None,
        "pre20_inside": False if reference_mate else None,
        "pre30_inside": False if reference_mate else None,
        "pre40_inside": False if reference_mate else None,
        "pre50_inside": False if reference_mate else None,
        "baseline_mate": reference_mate["mate"] if reference_mate else None,
        "recovered": False, "failure_reason": "known_mate_outside_cp_band" if reference_mate else "no_exact_finite_reference",
        "selection_objective": "finite in-band candidate nearest target; no mate-to-CP conversion"}
    _paced_control(control, pacing)
    result = {"policy": POLICY, "ply": len(board.move_stack) + 1, "before_fen": board.fen(en_passant="fen"),
              "history_uci": [move.uci() for move in board.move_stack],
              "color": "white" if board.turn else "black", "selected_uci": baseline_move.uci(),
              "baseline_uci": baseline_move.uci(), "baseline": baseline,
              "reference_mate": reference_mate, "loss_cp": None if reference_mate else 0,
              "actual_reference_depth": baseline["depth"] if baseline else None,
              "verification_depth": verification_depth, "compared_depth": verification_depth,
              "comparison_mode": "absolute_target_from_winning_mate" if absolute else "relative_cp",
              "reused": False, "opponent_observation": {"valid": False, "forced": bool(forced),
                  "reason": "not_observed", "loss_cp": None, "depth": None, "elapsed_ms": 0},
              "profile": profile, "phase": phase(board), "band_control": control,
              "pacing": pacing, "resource_guard": resource_guard,
              "target_loss_cp": max(0, baseline["cp"] - control["target_cp"]) if finite_baseline else None,
              "max_loss_cp": max(0, baseline["cp"] + BAND_CP) if finite_baseline else None,
              "position_floor_cp": -BAND_CP, "reliability": reliability,
              "comparison_basis": None, "reference_revalidation": None,
              "verification_policy": POSTMOVE_POLICY, "postmove_verification": None,
              "postmove_attempts": [],
              "postmove_reserved_ms": 0,
              "discovery": [], "probes": [], "naturalness": None,
              "baseline_budget_ms": movetime_ms, "extra_budget_ms": extra, "extra_elapsed_ms": 0,
              "analysis_only": analysis_only, "candidate_budget_ms": 0,
              "recovery": {"enabled": recovery, "eligible": recovery_eligible,
                           "requested_budget_ms": recovery_budget_ms, "extra_budget_ms": extra if recovery else 0,
                           "discovery_limit": discovery_limit, "probe_limit": probe_limit,
                           "forced_provenance": bool(forced), "forced_recovery": bool(forced and recovery),
                           "blocked_by_forced_game": bool(forced and not recovery), "postmove_retries": 0},
              "quality_budget_ms": 0, "discovery_budget_ms": 0, "probe_budget_sum_ms": 0,
              "reason": "baseline_preserved",
              "scope": "position feedback from own-engine scores; no continuous-band or victory guarantee"}

    def finish(reason, discarded=False, unavailable=False):
        result["reason"] = reason
        result["extra_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        result["recovery"]["elapsed_ms"] = result["extra_elapsed_ms"] if recovery else 0
        result["recovery"]["postmove_retries"] = max(0, len(result["postmove_attempts"]) - 1)
        cp, final_cp = control.get("selected_cp"), control.get("selected_final_cp")
        known = type(cp) in (int, float) and type(final_cp) in (int, float)
        values = [cp, final_cp]
        postmove_cp = control.get("selected_postmove_cp")
        if type(postmove_cp) in (int, float):
            values.append(postmove_cp)
        inside = all(-BAND_CP <= value <= BAND_CP for value in values) if known else False if reference_mate else None
        control["pre20_inside"] = control["pre30_inside"] = control["pre40_inside"] = control["pre50_inside"] = inside
        control["strict_band_status"] = "met" if inside is True else "unmet" if inside is False else "unavailable"
        control["strict_deviation_cp"] = max(max(-BAND_CP - value, value - BAND_CP, 0)
                                              for value in values) if known else None
        control["pacing_status"] = ("met" if known and max(_deviation(control, value) for value in values) == 0
                                    else "unmet") if control["pacing_applied"] else "inactive"
        result["selected_cp_own"] = cp
        result["selected_cp_white"] = cp * (1 if board.turn else -1) if known else None
        if not control["active"]:
            control.update(status="inactive", failure_reason="finish_full_strength", deviation_cp=None)
        elif reference_mate and not known:
            control.update(status="unmet", failure_reason=reason, deviation_cp=None)
        elif not known or unavailable:
            control.update(status="unavailable", failure_reason=reason,
                           deviation_cp=max(max(control["lower_cp"] - value, value - control["nominal_upper_cp"], 0)
                                            for value in values) if known else None)
        else:
            deviation = max(max(control["lower_cp"] - value, value - control["nominal_upper_cp"], 0)
                            for value in values)
            control.update(status="met" if deviation == 0 else "unmet", deviation_cp=round(deviation, 3),
                           failure_reason=None if deviation == 0 else reason)
        control["recovered"] = bool(control["status"] == "met" and baseline
                                    and (absolute or finite_baseline and baseline["cp"] > control["nominal_upper_cp"]))
        if unavailable or reason in ("finish_full_strength", "position_below_band_strongest", "winning_resources_guard"):
            # Keep the historical skill estimate separate from a controller
            # exception; the UI can distinguish the reason without fake Elo.
            result["profile"] = {**result["profile"], "status": "holding", "constraint": reason}
        return result, discarded

    if forced and not recovery:
        return finish("user_forced_game", unavailable=True)
    if board.chess960 or not board.is_valid() or board.root().fen(en_passant="fen") != chess.STARTING_FEN:
        return finish("custom_or_invalid_root", unavailable=True)
    if board.is_game_over(claim_draw=True):
        return finish("terminal", unavailable=True)
    if extra == 0:
        return finish("short_budget", unavailable=True)

    # An urgent board correction gets priority over the descriptive skill
    # sample. Full-strength and protected endings still collect rating evidence
    # once, with a separate 500ms ceiling and no candidate search at all.
    correction = not analysis_only and (absolute or finite_baseline is not None and baseline["cp"] > control["upper_cp"])
    quality_budget = min(200 if correction else QUALITY_BUDGET_MS, extra if analysis_only else extra * .3)
    result["quality_budget_ms"] = quality_budget
    observation, discarded = boss_play.observe_human_move(
        board, search, close_engine, budget_ms=quality_budget, forced=forced)
    result["opponent_observation"] = observation
    profile = derive_profile(board, [*rows, {"opponent_observation": observation}])
    result["profile"] = profile
    if analysis_only:
        return finish("finish_full_strength" if phase(board) == "finish" else "winning_resources_guard", discarded)
    if discarded:
        return finish("observation_failed", True, unavailable=True)
    if board.legal_moves.count() <= 1:
        return finish("forced_reply", unavailable=True)
    if baseline is None or (not reference_mate and baseline["depth"] < MIN_DEPTH):
        return finish("baseline_unreliable_or_mate", unavailable=True)
    if finite_baseline and _cp_pv_ends_in_mate(board, finite_baseline):
        return finish("baseline_score_pv_conflict", unavailable=True)
    reference_reader = _mate_reference if reference_mate else boss_play.score_line
    root_packet = reference_reader(board, (baseline_packets or {}).get(baseline["depth"]), baseline_move)
    if root_packet != baseline:
        return finish("baseline_packet_conflict", unavailable=True)
    if reference_mate and not absolute:
        return finish("mate_reference_full_strength")
    if finite_baseline and baseline["cp"] < -BAND_CP:
        return finish("position_below_band_strongest")
    if board.is_check() or opening.has_repetition(board):
        return finish("forcing_or_repeated_position", unavailable=True)
    headroom = (finite_baseline is not None and baseline["cp"] > control["target_cp"] + 15
                and (pacing.get("active") is True or profile.get("skill_target_loss_cp", 0) > 0))
    if not correction and not headroom:
        return finish("paced_baseline_preserved" if control["pacing_applied"]
                      and baseline["cp"] > control["nominal_upper_cp"] else "baseline_inside_band")

    deadline = started + extra / 1000
    result["candidate_budget_ms"] = round(max(0, (deadline - time.monotonic()) * 1000), 3)
    if result["candidate_budget_ms"] < POSTMOVE_MIN_MS:
        return finish("postmove_verification_budget", unavailable=True)
    reserve_cap = RECOVERY_POSTMOVE_RESERVE_MS if recovery else POSTMOVE_MAX_MS
    reserved = min(reserve_cap, max(POSTMOVE_MIN_MS,
                   result["candidate_budget_ms"] * (0.35 if recovery else POSTMOVE_RESERVE_FRACTION)))
    result["postmove_reserved_ms"] = round(reserved, 3)
    done, expired = threading.Event(), threading.Event()
    def expire():
        if not done.is_set():
            expired.set()
            try:
                close_engine()
            except Exception:
                pass
    timer = threading.Timer(max(0, deadline - time.monotonic()) + .1, expire)
    timer.daemon = True
    timer.start()
    failure = None
    chosen = None
    try:
        if absolute:
            result["comparison_basis"] = "exact_winning_mate_reference"
        elif reliability["reliability"] == "stable":
            result["comparison_basis"] = "stable_completed_depth"
        else:
            remaining = (deadline - time.monotonic()) * 1000 - reserved
            budget = min(1000 if recovery else 350, remaining * .25)
            if budget < 25:
                failure = "reference_revalidation_budget"
            else:
                rechecked, final, packets = search(budget, root_move=baseline_move, publish=False,
                                                    fresh=True, depth=baseline["depth"])
                line = _score_line(board, packets.get(baseline["depth"]), baseline_move)
                latest = _score_line(board, final, baseline_move)
                valid = (rechecked.move == baseline_move and line is not None and latest is not None
                         and latest["depth"] >= baseline["depth"]
                         and abs(line["cp"] - baseline["cp"]) <= REFERENCE_TOLERANCE_CP
                         and abs(latest["cp"] - baseline["cp"]) <= REFERENCE_TOLERANCE_CP
                         and (latest["depth"] != line["depth"] or latest["cp"] == line["cp"]))
                result["reference_revalidation"] = {"valid": valid, "budget_ms": round(budget, 3),
                    "original": baseline, "rechecked": line, "final": latest,
                    "delta_cp": line["cp"] - baseline["cp"] if line else None,
                    "scope": "same-position same-depth reference check; original stability label preserved"}
                if valid:
                    result["comparison_basis"] = "rechecked_exact_depth"
                else:
                    failure = "reference_revalidation_failed"

        if failure is None:
            discovery_started = time.monotonic()
            discovery_budget = min(2000 if recovery else 650,
                                   max(0, (deadline - discovery_started) * 1000 - reserved) * (.30 if recovery else .42))
            result["discovery_budget_ms"] = round(discovery_budget, 3)
            discovery_deadline = discovery_started + discovery_budget / 1000
            excluded = {baseline_move}
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            direct_order = None
            for index in range(discovery_limit):
                remaining = (discovery_deadline - time.monotonic()) * 1000
                if remaining < 12 or expired.is_set():
                    break
                allowed = [move for move in legal if move not in excluded]
                if not allowed:
                    break
                budget = max(12, remaining / max(1, discovery_limit - index))
                if index < ranked_limit:
                    candidate, final, packets = search(budget, root_moves=allowed, publish=False,
                                                       fresh=True, depth=DISCOVERY_DEPTH)
                    move = candidate.move
                    source = "rank_exclusion"
                else:
                    if direct_order is None:
                        # Spread direct probes across quiet legal moves rather
                        # than repeatedly asking only for the engine's top six.
                        # Once the concealment delay has passed, include the
                        # compensated captures that may repay a major loss.
                        prefer_returns = recovery and pacing.get("allow_material_return") is True
                        direct_order = sorted(allowed, key=lambda move: (
                            not board.is_capture(move) if prefer_returns else board.is_capture(move),
                            board.gives_check(move),
                            opening._digest([POLICY, seed, opening.history_key(board), "direct", move.uci()])))
                    move = next((move for move in direct_order if move not in excluded), None)
                    if move is None:
                        break
                    candidate, final, packets = search(budget, root_move=move, publish=False,
                                                       fresh=True, depth=DISCOVERY_DEPTH)
                    source = "spread_legal"
                if candidate.move is None or move not in allowed or candidate.move != move:
                    failure = "discovery_invalid_move"
                    break
                excluded.add(move)
                line = _score_line(board, packets.get(DISCOVERY_DEPTH), move)
                natural = naturalness(board, move, pacing=pacing, resource_guard=resource_guard)
                result["discovery"].append({"uci": move.uci(), "line": line, "naturalness": natural,
                    "source": source, "rough_cp": line["cp"] if line else None,
                    "rough_loss_cp": max(0, baseline["cp"] - line["cp"]) if line and finite_baseline else None,
                    "scope": "discovery only; exact final same-depth verification is mandatory"})
            candidates = [row for row in result["discovery"] if row["naturalness"]["admitted"]]
            candidates.sort(key=lambda row: (
                2 if row["line"] is None else 0 if control["lower_cp"] <= row["line"]["cp"] <= control["upper_cp"] else 1,
                abs(row["line"]["cp"] - control["target_cp"]) if row["line"] else 100000,
                -row["naturalness"]["style_points"], row["uci"]))
            for index, candidate in enumerate(candidates[:probe_limit]):
                remaining = (deadline - time.monotonic()) * 1000 - reserved
                if failure or remaining < 25 or expired.is_set():
                    break
                remaining_candidates = min(probe_limit, len(candidates)) - index
                # Broad discovery must not divide the exact-depth budget into
                # ten tiny searches. Front-load the strongest recovery leads;
                # later candidates use savings when earlier searches finish.
                divisor = min(MAX_PROBES, remaining_candidates) if recovery else remaining_candidates
                budget = remaining / max(1, divisor)
                move = chess.Move.from_uci(candidate["uci"])
                probe_started = time.monotonic()
                reply, final, packets = search(budget, root_move=move, publish=False,
                                               fresh=True, depth=verification_depth)
                elapsed = (time.monotonic() - probe_started) * 1000
                probe = (_absolute_probe(board, move, packets, final, reply.move, verification_depth, budget, elapsed)
                         if absolute else boss_play.assess(board, baseline_move, baseline_info, move.uci(), packets, final,
                                                          reply.move, budget_ms=round(budget, 3), elapsed_ms=elapsed))
                probe["naturalness"] = candidate["naturalness"]
                if probe["admitted"]:
                    compared, latest = probe["candidate_compared"], probe["candidate_final"]
                    if (_score_line(board, packets.get(verification_depth), move) is None
                            or _score_line(board, final, move) is None):
                        probe.update(admitted=False, reason="candidate_nonprimary_or_invalid_score")
                    elif _cp_pv_ends_in_mate(board, compared) or _cp_pv_ends_in_mate(board, latest):
                        probe.update(admitted=False, reason="candidate_score_pv_conflict")
                    elif not absolute and probe["reference_delta_cp"] < -25:
                        probe.update(admitted=False, reason="candidate_reference_conflict")
                    elif min(compared["cp"], latest["cp"]) < control["lower_cp"]:
                        probe.update(admitted=False, reason="below_band_safety_floor")
                    else:
                        probe["reason"] = "verified_position_candidate"
                        probe["inside_band"] = all(control["lower_cp"] <= cp <= control["upper_cp"]
                                                   for cp in (compared["cp"], latest["cp"]))
                result["probes"].append(probe)
                result["probe_budget_sum_ms"] += round(budget, 3)
        if failure is None:
            remaining_probes = list(result["probes"])
            while True:
                chosen = _choose_verified(board, baseline_move, baseline, remaining_probes, control, seed)
                if chosen["probe"] is None:
                    break
                budget = min(POSTMOVE_MAX_MS, (deadline - time.monotonic()) * 1000)
                if budget < POSTMOVE_MIN_MS or expired.is_set():
                    failure = "postmove_verification_budget"
                    break
                else:
                    verified = _postmove_verification(board, chess.Move.from_uci(chosen["uci"]),
                        chosen["probe"], search, depth=max(MIN_DEPTH, verification_depth - 1),
                        budget_ms=budget, floor_cp=control["lower_cp"])
                    result["postmove_verification"] = verified
                    result["postmove_attempts"].append(verified)
                    if not verified["valid"]:
                        failure = verified["reason"]
                        if recovery:
                            remaining_probes.remove(chosen["probe"])
                            continue
                    else:
                        failure = None
                        chosen["inside"] = (chosen["inside"] and
                            control["lower_cp"] <= verified["cp_own"] <= control["upper_cp"])
                    break
    except Exception as error:
        failure = "candidate_search_failed"
        result["error"] = type(error).__name__
        discarded = True
    finally:
        done.set()
        timer.cancel()
        timer.join(timeout=.2)
    if expired.is_set() or time.monotonic() > deadline + .02:
        return finish("extra_budget_timeout", True, unavailable=True)
    if failure:
        return finish(failure, discarded, unavailable=True)
    probe = chosen["probe"]
    if probe is not None:
        result.update(selected_uci=probe["uci"], loss_cp=probe["loss_cp"],
                      candidate_compared=probe["candidate_compared"], candidate_final=probe["candidate_final"],
                      naturalness=probe["naturalness"], pressure=probe["pressure"])
        control.update(selected_cp=chosen["cp"], selected_final_cp=chosen["final_cp"],
                       selected_postmove_cp=result["postmove_verification"]["cp_own"],
                       basis="own_engine_selected_root_with_postmove_check")
        if control["pacing_applied"] and chosen["inside"] and not all(
                control["lower_cp"] <= value <= control["nominal_upper_cp"] for value in (chosen["cp"], chosen["final_cp"])):
            return finish("accepted_paced_recovery")
        return finish("accepted_sparring_alternative" if chosen["inside"] else "closest_verified_outside_band")
    if chosen["inside"]:
        return finish("paced_baseline_preserved" if control["pacing_applied"]
                      and baseline["cp"] > control["nominal_upper_cp"] else "baseline_inside_band")
    if reference_mate:
        return finish("known_winning_mate_no_finite_candidate")
    verified = [probe for probe in result["probes"] if probe["admitted"]]
    return finish("bounded_search_no_in_band_candidate" if verified else "bounded_search_no_verified_candidate")


def pgn_headers(metadata):
    if not isinstance(metadata, dict) or metadata.get("enabled") is not True or metadata.get("policy") not in SUPPORTED_POLICIES:
        return {}
    headers = {"SparringMode": "1", "SparringPolicy": metadata["policy"],
               "SparringScope": "Adaptive practice concessions; full-strength finish; no guaranteed win"}
    seed = metadata.get("seed")
    if metadata.get("evaluation_policy") == sparring_evaluation.POLICY:
        headers["SparringEvaluationPolicy"] = sparring_evaluation.POLICY
    if metadata.get("verification_policy") == POSTMOVE_POLICY:
        headers["SparringVerificationPolicy"] = POSTMOVE_POLICY
    if isinstance(seed, str) and re.fullmatch(r"[0-9a-f]{32}", seed):
        headers["SparringSeed"] = seed
    return headers
