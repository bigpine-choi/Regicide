"""Replay-derived observations for conservative, adaptive trap frequency.

This is an uncalibrated within-game error indicator, not Elo, an exact capture
probability, or a judgment based on refusing a trap. Native search quality and
observed trap replies remain separate channels. No engine is started here.
"""
import hashlib
import json
import math

import chess

POLICY = "regicide-boss-opponent-v1"
PRIOR_GOOD = 8.0
PRIOR_BAD = 1.0
INITIAL_FREQUENCY = .10
MIN_FREQUENCY = .05
MAX_FREQUENCY = .90
GOOD_LOSS_CP = 30
BAD_LOSS_CP = 150
MIN_DEPTH = 4


def _history(value):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _replay(board):
    root = board.root()
    position = root.copy(stack=True)
    fens = [position.fen(en_passant="fen")]
    history = []
    for move in board.move_stack:
        if move not in position.legal_moves:
            return None
        history.append(move.uci())
        position.push(move)
        fens.append(position.fen(en_passant="fen"))
    if position.fen(en_passant="fen") != board.fen(en_passant="fen"):
        return None
    return tuple(history), fens, root.fen(en_passant="fen")


def _frequency(error_rate):
    prior = PRIOR_BAD / (PRIOR_GOOD + PRIOR_BAD)
    # Initial evidence assumes a strong opponent. Evidence of errors gradually
    # raises the rate; accurate later moves gradually lower it again.
    value = INITIAL_FREQUENCY + .85 * (error_rate - prior) / (1 - prior)
    return max(MIN_FREQUENCY, min(MAX_FREQUENCY, value))


def derive_profile(board, decisions):
    """Reconstruct only observations still present in the actual move history.

    Each valid human-move comparison contributes at most one total weight.
    The engine's measured loss maps linearly from good (<=30cp) to bad
    (>=150cp). This explicit heuristic and its prior are not rating calibration.
    """
    replay = _replay(board)
    history, fens, _root_fen = replay if replay is not None else ((), [], "")
    good, bad = PRIOR_GOOD, PRIOR_BAD
    quality, outcomes, ignored = [], [], 0
    seen_quality, seen_traps = set(), set()
    rows = decisions if isinstance(decisions, (tuple, list)) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_history = _history(row.get("history_uci"))
        row_matches = (replay is not None and row_history is not None
                       and len(row_history) <= len(history)
                       and history[:len(row_history)] == row_history
                       and row.get("before_fen") == fens[len(row_history)])
        observation = row.get("opponent_observation")
        if isinstance(observation, dict):
            prefix = _history(observation.get("history_before"))
            loss, depth = observation.get("loss_cp"), observation.get("depth")
            provisional = (replay is not None and set(row) == {"opponent_observation"}
                           and prefix is not None and len(prefix) + 1 == len(history))
            observation_matches = (prefix is not None and (row_matches or provisional)
                                   and len(prefix) + 1 == (len(history) if provisional else len(row_history)))
            valid = (observation_matches and observation.get("valid") is True
                     and observation.get("forced") is False and prefix is not None
                     and history[:len(prefix)] == prefix
                     and observation.get("ply") == len(prefix) + 1
                     and history[len(prefix)] == observation.get("played_uci")
                     and observation.get("before_fen") == fens[len(prefix)]
                     and type(depth) is int and depth >= MIN_DEPTH
                     and type(loss) in (int, float) and math.isfinite(loss) and loss >= 0)
            if valid:
                key = (prefix, observation["played_uci"])
                if key not in seen_quality:
                    seen_quality.add(key)
                    error_weight = max(0., min(1., (loss - GOOD_LOSS_CP) / (BAD_LOSS_CP - GOOD_LOSS_CP)))
                    bad += error_weight
                    good += 1 - error_weight
                    quality.append({"ply": len(prefix) + 1, "played_uci": observation["played_uci"],
                                    "loss_cp": loss, "depth": depth, "bad_weight": error_weight})
            else:
                ignored += 1
        evidence = row.get("trap_evidence")
        if not row_matches or not isinstance(evidence, dict) or evidence.get("verified") is not True:
            continue
        index = len(row_history)
        selected, capture = row.get("selected_uci"), evidence.get("capture_uci")
        if (index + 1 >= len(history) or history[index] != selected
                or evidence.get("uci") != selected or not isinstance(capture, str)):
            continue
        key = (row_history, selected)
        if key in seen_traps:
            continue
        seen_traps.add(key)
        reply = history[index + 1]
        reference = row.get("best_defense_pv_uci")
        reference_reply = (reference[1] if isinstance(reference, (list, tuple)) and len(reference) >= 2
                           and reference[0] == selected else None)
        if reply == capture:
            outcome = "taken"
        elif reply == reference_reply:
            outcome = "reference_defense"
        else:
            outcome = "unknown"
        outcomes.append({"ply": index + 2, "reply_uci": reply, "outcome": outcome})
    total = good + bad
    error_rate = bad / total
    deviation = math.sqrt(bad * good / (total * total * (total + 1)))
    counts = {name: sum(item["outcome"] == name for item in outcomes)
              for name in ("taken", "reference_defense", "unknown")}
    opportunities = len(outcomes)
    return {"policy": POLICY, "quality_observations": len(quality),
            "quality_rows": quality, "ignored_quality_observations": ignored,
            "prior_good": PRIOR_GOOD, "prior_bad": PRIOR_BAD,
            "good_weight": good, "bad_weight": bad,
            "error_indicator": error_rate, "posterior_stddev": deviation,
            "uncertainty_interval": [max(0., error_rate - 2 * deviation), min(1., error_rate + 2 * deviation)],
            "uncertainty_scope": "approximate two-standard-deviation range of the heuristic Beta model",
            "frequency": _frequency(error_rate), "initial_frequency": INITIAL_FREQUENCY,
            "trap_opportunities": opportunities, "trap_outcomes": counts, "trap_rows": outcomes,
            "observed_take_fraction": counts["taken"] / opportunities if opportunities else None,
            "take_fraction_denominator": "all verified chosen-trap replies, including unknown",
            "scope": "uncalibrated current-game error indicator; no Elo or exact capture probability",
            "history_valid": replay is not None}


def gate(board, decisions, seed, override=None, profile=None):
    """Deterministic trap-hunt frequency gate; full-strength baseline is external.

    ``override`` may set a test/explicit policy error_indicator or frequency;
    it is recorded separately and never disguised as observed opponent skill.
    """
    current = derive_profile(board, decisions) if profile is None else dict(profile)
    frequency = current["frequency"]
    if override is not None:
        if not isinstance(override, dict):
            raise ValueError("override must be a dictionary")
        if "error_indicator" in override:
            value = override["error_indicator"]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("invalid error indicator override")
            frequency = _frequency(value)
        if "frequency" in override:
            value = override["frequency"]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("invalid frequency override")
            frequency = float(value)
    replay = _replay(board)
    history, _fens, root_fen = replay if replay is not None else ((), [], board.fen(en_passant="fen"))
    payload = json.dumps([POLICY, str(seed), root_fen, history], separators=(",", ":")).encode("utf-8")
    draw = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") / 2 ** 64
    allow = replay is not None and draw < frequency
    return {"allow": allow, "attempt_trap": allow, "profile": current,
            "frequency": frequency, "probability": frequency, "draw": draw, "override": override,
            "reason": "frequency_admitted" if allow else "conservative_frequency_gate"}
