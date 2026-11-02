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

POLICY = "regicide-sparring-play-v1"
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
MAX_EXTRA_MS = 2000
QUALITY_BUDGET_MS = 500
# This short pass only orders candidates. Selection still requires an exact
# re-search at the full-budget baseline's completed depth (at least MIN_DEPTH).
DISCOVERY_DEPTH = 3
MAX_DISCOVERY = 6
MAX_PROBES = 2
RAMP_PLY = 100
FINISH_PLY = 140
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


def phase(board):
    """50 full moves of practice, then 20 of ramp; never a guaranteed win."""
    ply = len(board.move_stack)
    if ply >= FINISH_PLY:
        return "finish"
    if ply >= RAMP_PLY:
        return "ramp"
    return "calibration" if ply < 6 else "sparring"


def extra_budget(movetime_ms, *, ply=0):
    return min(MAX_EXTRA_MS, 2 * movetime_ms) if movetime_ms >= MIN_TOTAL_MS and ply < FINISH_PLY else 0


def _phase_factor(board):
    stage = phase(board)
    if stage in ("calibration", "finish"):
        return 0.0
    return max(0., (FINISH_PLY - len(board.move_stack)) / (FINISH_PLY - RAMP_PLY)) if stage == "ramp" else 1.0


def derive_profile(board, decisions):
    """Rebuild verified observations from the surviving game, including undo.

    A rolling, recency-weighted 12-observation mean is shrunk towards a modest
    prior. The smoothed estimate can move at most 12cp per valid human move.
    Missing observations never count as accurate play. Trap replies are unused.
    """
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
    target = skill_target * _phase_factor(board)
    current_phase = phase(board)
    status = "adapting" if count >= MIN_OBSERVATIONS else "calibrating"
    if replay is None or current_phase == "finish":
        status, target = "holding", 0
    return {"policy": POLICY, "status": status, "level": round(1 - target / MAX_LOSS_CP, 4),
            "confidence": "high" if len(recent) >= 10 else "medium" if len(recent) >= 6 else "low",
            "confidence_scope": "heuristic evidence coverage, not a probability or rating",
            "observations": count, "recent_observations": recent,
            "ignored_observations": ignored, "conflicting_observations": sum(x is None for x in found.values()),
            "observation_weight": round(sum(RECENCY ** i for i in range(len(recent))), 4),
            "smoothed_loss_cp": round(estimate, 3), "skill_target_loss_cp": round(skill_target, 3),
            "target_loss_cp": round(target, 3), "phase_factor": round(_phase_factor(board), 4),
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


def naturalness(board, move):
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
                                    "net_material_cp": swing})
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
    reason = ("immediate_mate_reply" if immediate_mate else "obvious_material_donation" if vulnerabilities
              else "terminal_or_claimable_draw" if terminal else "no_obvious_one_reply_donation")
    return {"admitted": not (immediate_mate or vulnerabilities or terminal), "reason": reason,
            "style_points": style, "development": development, "direct_piece_threats": threats,
            "opponent_legal_moves": len(replies), "opponent_king_moves": sum(
                after.piece_type_at(reply.from_square) == chess.KING for reply in replies),
            "forcing_check": after.is_check(),
            "vulnerable_captures": vulnerabilities,
            "scope": "local legal capture screen; not exhaustive tactical or human-perception proof"}


def _target(board, profile, baseline_cp):
    factor = _phase_factor(board)
    if profile["observations"] < MIN_OBSERVATIONS:
        factor = 0
    # A clear advantage retains at least half of its measured value (>=40cp).
    # Slight advantages retain nonnegative evaluation; balanced positions have
    # only a small cushion. These are finite-search safeguards, not guarantees.
    floor = max(40, baseline_cp // 2) if baseline_cp >= 100 else 0 if baseline_cp >= 30 else -60
    behind_factor = min(1, max(0, (baseline_cp + 60) / 60))
    skill_target = profile.get("skill_target_loss_cp", profile["target_loss_cp"])
    target = min(MAX_TARGET_CP, skill_target * factor * behind_factor,
                 max(0, baseline_cp - floor))
    cap = min(MAX_LOSS_CP, max(0, baseline_cp - floor), target + 20) if target > 0 else 0
    return {"target_loss_cp": round(target, 3), "max_loss_cp": round(cap, 3),
            "position_floor_cp": floor, "phase_factor": round(factor, 4),
            "behind_factor": round(behind_factor, 4)}


def choose(board, baseline_move, baseline_info, baseline_packets, search, close_engine, *,
           decisions, seed, movetime_ms, forced=False):
    """Return (auditable decision, discard_engine) using one shared engine.

    ``search`` is the host callback with root_move/root_moves, depth, position,
    publish and fresh arguments. It returns BestMove, final raw packet, packets
    by completed depth. Discovery scores only order final verification probes.
    """
    started = time.monotonic()
    rows = _rows(decisions)
    baseline = boss_play.score_line(board, baseline_info, baseline_move)
    reliability = evaluation_stability.assess(board, baseline_packets)
    profile = derive_profile(board, rows)
    extra = extra_budget(movetime_ms, ply=len(board.move_stack))
    observation = {"valid": False, "forced": bool(forced), "reason": "not_observed",
                   "loss_cp": None, "depth": None, "elapsed_ms": 0}
    result = {"policy": POLICY, "ply": len(board.move_stack) + 1, "before_fen": board.fen(en_passant="fen"),
              "history_uci": [move.uci() for move in board.move_stack],
              "color": "white" if board.turn else "black", "selected_uci": baseline_move.uci(),
              "baseline_uci": baseline_move.uci(), "baseline": baseline, "loss_cp": 0,
              "compared_depth": baseline["depth"] if baseline else None,
              "reused": False, "opponent_observation": observation, "profile": profile,
              "phase": phase(board), "target_loss_cp": 0, "max_loss_cp": 0,
              "reliability": reliability, "discovery": [], "probes": [], "naturalness": None,
              "baseline_budget_ms": movetime_ms, "extra_budget_ms": extra, "extra_elapsed_ms": 0,
              "quality_budget_ms": 0, "discovery_budget_ms": 0, "probe_budget_sum_ms": 0,
              "reason": "baseline_preserved", "scope": "heuristic sparring; finish seeks victory without guaranteeing it"}

    def finish(reason, discarded=False):
        result["reason"] = reason
        result["extra_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        if reason in ("user_forced_game", "custom_or_invalid_root", "terminal", "finish_full_strength",
                      "short_budget", "forced_reply", "observation_failed", "baseline_unreliable_or_mate",
                      "baseline_packet_conflict", "evaluation_not_stable", "forcing_or_repeated_position",
                      "position_or_phase_full_strength", "extra_budget_timeout", "candidate_search_failed"):
            result["profile"] = {**result["profile"], "status": "holding", "target_loss_cp": 0,
                                 "level": 1.0, "constraint": reason}
            result["target_loss_cp"] = result["max_loss_cp"] = 0
        return result, discarded

    if forced:
        return finish("user_forced_game")
    if board.chess960 or not board.is_valid() or board.root().fen(en_passant="fen") != chess.STARTING_FEN:
        return finish("custom_or_invalid_root")
    if board.is_game_over(claim_draw=True):
        return finish("terminal")
    if phase(board) == "finish":
        return finish("finish_full_strength")
    if extra == 0:
        return finish("short_budget")
    if board.legal_moves.count() <= 1:
        return finish("forced_reply")

    quality_budget = min(QUALITY_BUDGET_MS, extra * .3)
    result["quality_budget_ms"] = quality_budget
    observation, discarded = boss_play.observe_human_move(
        board, search, close_engine, budget_ms=quality_budget, forced=forced)
    result["opponent_observation"] = observation
    profile = derive_profile(board, [*rows, {"opponent_observation": observation}])
    result["profile"] = profile
    if discarded:
        return finish("observation_failed", True)
    if baseline is None or baseline["depth"] < MIN_DEPTH:
        return finish("baseline_unreliable_or_mate")
    root_packet = boss_play.score_line(board, (baseline_packets or {}).get(baseline["depth"]), baseline_move)
    if root_packet != baseline:
        return finish("baseline_packet_conflict")
    if reliability["reliability"] != "stable":
        return finish("evaluation_not_stable")
    if board.is_check() or opening.has_repetition(board):
        return finish("forcing_or_repeated_position")
    settings = _target(board, profile, baseline["cp"])
    result.update(settings)
    result["profile"] = {**profile, "target_loss_cp": settings["target_loss_cp"],
                         "level": round(1 - settings["target_loss_cp"] / MAX_LOSS_CP, 4)}
    if settings["target_loss_cp"] <= 0:
        return finish("calibrating" if profile["observations"] < MIN_OBSERVATIONS else "position_or_phase_full_strength")

    deadline = started + extra / 1000
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
    try:
        discovery_started = time.monotonic()
        discovery_budget = min(350, max(0, (deadline - discovery_started) * 1000) * .25)
        result["discovery_budget_ms"] = round(discovery_budget, 3)
        discovery_deadline = discovery_started + discovery_budget / 1000
        excluded = {baseline_move}
        legal = sorted(board.legal_moves, key=lambda move: move.uci())
        shallow = boss_play.score_line(board, (baseline_packets or {}).get(DISCOVERY_DEPTH))
        reference_cp = shallow["cp"] if shallow else baseline["cp"]
        for index in range(MAX_DISCOVERY):
            allowed = [move for move in legal if move not in excluded]
            remaining = (discovery_deadline - time.monotonic()) * 1000
            if not allowed or remaining < 20 or expired.is_set():
                break
            budget = remaining / max(1, MAX_DISCOVERY - index)
            candidate, final, packets = search(budget, root_moves=allowed, publish=False,
                                               fresh=True, depth=DISCOVERY_DEPTH)
            move = candidate.move
            if move is None or move not in allowed:
                failure = "discovery_invalid_move"
                break
            excluded.add(move)
            line = boss_play.score_line(board, packets.get(DISCOVERY_DEPTH), move)
            natural = naturalness(board, move)
            row = {"uci": move.uci(), "line": line, "naturalness": natural,
                   "rough_loss_cp": max(0, reference_cp - line["cp"]) if line else None,
                   "scope": "discovery only; not admissible selection evidence"}
            result["discovery"].append(row)
        candidates = [row for row in result["discovery"] if row["line"] is not None and row["naturalness"]["admitted"]]
        candidates.sort(key=lambda row: (abs(row["rough_loss_cp"] - result["target_loss_cp"]),
                                         -row["naturalness"]["style_points"],
                                         opening._digest([POLICY, seed, opening.history_key(board), row["uci"]])))
        for index, candidate in enumerate(candidates[:MAX_PROBES]):
            remaining = (deadline - time.monotonic()) * 1000
            if failure or remaining < 25 or expired.is_set():
                break
            budget = remaining / max(1, min(MAX_PROBES, len(candidates)) - index)
            move = chess.Move.from_uci(candidate["uci"])
            probe_started = time.monotonic()
            reply, final, packets = search(budget, root_move=move, publish=False,
                                           fresh=True, depth=baseline["depth"])
            probe = boss_play.assess(board, baseline_move, baseline_info, move.uci(), packets, final,
                                     reply.move, budget_ms=round(budget, 3),
                                     elapsed_ms=(time.monotonic() - probe_started) * 1000)
            probe["naturalness"] = candidate["naturalness"]
            if probe["admitted"]:
                compared, latest = probe["candidate_compared"], probe["candidate_final"]
                if probe["reference_delta_cp"] < -25:
                    probe.update(admitted=False, reason="candidate_reference_conflict")
                elif probe["loss_cp"] > result["max_loss_cp"]:
                    probe.update(admitted=False, reason="loss_cap")
                elif min(compared["cp"], latest["cp"]) < result["position_floor_cp"]:
                    probe.update(admitted=False, reason="position_floor")
                elif baseline["cp"] - latest["cp"] > result["max_loss_cp"]:
                    probe.update(admitted=False, reason="deeper_candidate_warning")
                else:
                    probe["reason"] = "verified_sparring_candidate"
            result["probes"].append(probe)
            result["probe_budget_sum_ms"] += round(budget, 3)
    except Exception as error:
        failure = "candidate_search_failed"
        result["error"] = type(error).__name__
        discarded = True
    finally:
        done.set()
        timer.cancel()
        timer.join(timeout=.2)
    if expired.is_set() or time.monotonic() > deadline + .02:
        return finish("extra_budget_timeout", True)
    if failure:
        return finish(failure, discarded)
    verified = [row for row in result["probes"] if row["admitted"]]
    if not verified:
        return finish("no_verified_natural_candidate")
    # A candidate must improve distance to the target; the baseline is always
    # an option. Similar loss distances prefer development and useful threats.
    pool = [(abs(result["target_loss_cp"]), 0, baseline_move.uci(), None)]
    baseline_after = board.copy(stack=True)
    baseline_after.push(baseline_move)
    baseline_replies = list(baseline_after.legal_moves)
    baseline_king_moves = sum(baseline_after.piece_type_at(move.from_square) == chess.KING for move in baseline_replies)
    pressure_weight = 1 - _phase_factor(board) if phase(board) == "ramp" else 0
    result["pressure_weight"] = round(pressure_weight, 4)
    for row in verified:
        distance = abs(row["loss_cp"] - result["target_loss_cp"])
        natural = row["naturalness"]
        # Checks mechanically reduce legal replies, so they receive no squeeze
        # bonus. Mobility/king-space are a narrow geometric tie-break only.
        pressure = (min(8, max(0, len(baseline_replies) - natural["opponent_legal_moves"]))
                    + 2 * max(0, baseline_king_moves - natural["opponent_king_moves"]))
        if natural["forcing_check"] or baseline_after.is_check():
            pressure = 0
        row["pressure"] = {"points": pressure, "weight": round(pressure_weight, 4),
                           "baseline_opponent_legal_moves": len(baseline_replies),
                           "baseline_opponent_king_moves": baseline_king_moves,
                           "scope": "geometric tie-break among verified candidates, not a proven playing style"}
        style = natural["style_points"] + pressure_weight * pressure
        pool.append((distance, -style, row["uci"], row))
    best_distance = min(row[0] for row in pool)
    near = [row for row in pool if row[0] <= best_distance + 5 and row[0] < result["target_loss_cp"]]
    if not near:
        return finish("baseline_closest_to_target")
    selected = min(near, key=lambda row: (row[1], row[0], opening._digest([POLICY, seed, opening.history_key(board), row[2]])))
    chosen = selected[3]
    result.update(selected_uci=chosen["uci"], loss_cp=chosen["loss_cp"],
                  candidate_compared=chosen["candidate_compared"], candidate_final=chosen["candidate_final"],
                  naturalness=chosen["naturalness"], pressure=chosen["pressure"])
    return finish("accepted_sparring_alternative")


def pgn_headers(metadata):
    if not isinstance(metadata, dict) or metadata.get("enabled") is not True or metadata.get("policy") != POLICY:
        return {}
    headers = {"SparringMode": "1", "SparringPolicy": POLICY,
               "SparringScope": "Adaptive practice concessions; full-strength finish; no guaranteed win"}
    seed = metadata.get("seed")
    if isinstance(seed, str) and re.fullmatch(r"[0-9a-f]{32}", seed):
        headers["SparringSeed"] = seed
    return headers
