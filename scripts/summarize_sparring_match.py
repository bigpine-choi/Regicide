"""Describe an external-opponent sparring diagnostic, never estimate Elo.

Own-engine CP, selected-root CP, and the opponent's next-root CP are separate
finite-search measurements. They are not independent measures of human feel,
position truth, or proof that adaptive play improves training.
"""

import argparse
from collections import Counter, defaultdict
import copy
import datetime
import hashlib
import json
import math
from pathlib import Path

import chess


MATCH_SCHEMA = "regicide.sparring-strength-match.v1"
SUMMARY_SCHEMA = "regicide.sparring-match-summary.v2"
POLICY = "regicide-sparring-play-v4"
POLICY_PHASE_ROOTS = {"regicide-sparring-play-v1": (100, 140),
                      "regicide-sparring-play-v2": (80, 138),
                      "regicide-sparring-play-v3": (60, 138), POLICY: (40, 138)}
SUPPORTED_POLICIES = frozenset(POLICY_PHASE_ROOTS)
CONDITIONS = ("sparring_ms", "stockfish_ms", "threads", "hash_mb", "max_plies", "cap_policy", "classical")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _stats(values):
    values = sorted(value for value in values if _number(value))
    def percentile(q):
        if not values:
            return None
        index = (len(values) - 1) * q
        low, high = math.floor(index), math.ceil(index)
        return values[low] + (values[high] - values[low]) * (index - low)
    return {"n": len(values), "mean": sum(values) / len(values) if values else None,
            "min": values[0] if values else None, "max": values[-1] if values else None,
            "median": percentile(.5), "p95": percentile(.95)}


def _cp_stats(values, eligible, close_cp, mates=()):
    known = [value for value in values if _number(value)]
    mate_count = sum(type(value) is int and value != 0 for value in mates)
    close = sum(abs(value) <= close_cp for value in known)
    return {"close_cp": close_cp, "eligible_positions": eligible, "known_exact_cp": len(known),
            "known_exact_mate": mate_count, "outside_known_mates": mate_count,
            "unknown_or_inexact": eligible - len(known) - mate_count, "close_positions": close,
            "close_fraction_of_known": close / len(known) if known else None,
            "mean_absolute_cp": sum(abs(value) for value in known) / len(known) if known else None,
            "white_cp": _stats(known)}


def _trend(values):
    pairs = [(a, b) for a, b in zip(values, values[1:]) if _number(a) and _number(b)]
    return {"values": values, "known_values": sum(_number(x) for x in values),
            "adjacent_known_transitions": len(pairs),
            "increases": sum(b > a + 1e-6 for a, b in pairs),
            "decreases": sum(b < a - 1e-6 for a, b in pairs),
            "unchanged": sum(abs(b - a) <= 1e-6 for a, b in pairs)}


def _category(game, board):
    if any(type(game.get(key)) is not bool for key in ("completed", "legal_result", "capped")):
        raise ValueError("completed/legal_result/capped must be booleans")
    ending, error = str(game.get("termination", "")), game.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("error must be text or null")
    diagnosis = (ending + " " + (error or "")).lower()
    if "interrupt" in diagnosis:
        return "interrupted"
    if "timeout" in diagnosis or "timed out" in diagnosis:
        return "timeout"
    if "illegal" in diagnosis:
        return "illegal_move"
    if error or ending == "engine_error":
        return "engine_error"
    if game["capped"] or ending in ("ply_cap", "ply_cap_draw"):
        return "capped"
    if game["completed"] and game["legal_result"]:
        outcome = board.outcome(claim_draw=True)
        if outcome is None or outcome.result() != game.get("result"):
            raise ValueError("completed result differs from replayed legal outcome")
        if ending != outcome.termination.name.lower():
            raise ValueError("termination differs from replayed legal outcome")
        return "legal_completed"
    return "incomplete"


def _line(board, line, required):
    """Validate a raw exact score without converting mates into CP."""
    if (not isinstance(line, dict)
            or type(line.get("depth")) is not int or line["depth"] < 1
            or line.get("uci") != required
            or line.get("bound", "exact") != "exact"
            or line.get("score_bound", "exact") != "exact"
            or line.get("pov", "white" if board.turn else "black") != ("white" if board.turn else "black")
            or line.get("source_fen", board.fen(en_passant="fen")) != board.fen(en_passant="fen")):
        return None
    pv = line.get("pv_uci")
    if (not isinstance(pv, list) or not 1 <= len(pv) <= 128 or pv[0] != required
            or any(type(uci) is not str for uci in pv)):
        return None
    position = board.copy(stack=True)
    try:
        for uci in pv:
            position.push_uci(uci)
    except (ValueError, TypeError):
        return None
    cp, mate = line.get("cp"), line.get("mate")
    if _number(cp) and mate is None:
        if position.is_checkmate():
            return None
        white_cp = cp if board.turn else -cp
        if "cp_white" in line and line["cp_white"] != white_cp:
            return None
        return {"kind": "cp", "cp": cp, "cp_white": white_cp, "mate": None,
                "mate_white": None, "depth": line["depth"]}
    if cp is None and type(mate) is int and mate != 0:
        white_mate = mate if board.turn else -mate
        if "mate_white" in line and line["mate_white"] != white_mate:
            return None
        if line.get("cp_white") is not None:
            return None
        winner = board.turn if mate > 0 else not board.turn
        if (not position.is_checkmate() or position.turn == winner
                or len(pv) != 2 * abs(mate) - (1 if mate > 0 else 0)
                or line.get("winner", "white" if winner else "black") != ("white" if winner else "black")):
            return None
        return {"kind": "mate", "cp": None, "cp_white": None, "mate": mate,
                "mate_white": white_mate, "depth": line["depth"]}
    return None


def _absolute_mate_choice(board, decision, base, candidate, selected_uci):
    """Explicit v2/v3/v4 exception, within the recorded close-play phase."""
    depth = decision.get("verification_depth")
    policy = decision.get("policy")
    if (policy not in ("regicide-sparring-play-v2", "regicide-sparring-play-v3", POLICY)
            or len(board.move_stack) >= POLICY_PHASE_ROOTS[policy][0]
            or decision.get("comparison_mode") != "absolute_target_from_winning_mate"
            or decision.get("comparison_basis") != "exact_winning_mate_reference"
            or decision.get("loss_cp") is not None or not base or base["kind"] != "mate" or base["mate"] <= 0
            or decision.get("reference_mate") != decision.get("baseline")
            or decision.get("actual_reference_depth") != base["depth"]
            or type(depth) is not int or depth != max(4, base["depth"])
            or decision.get("compared_depth") != depth
            or not candidate or candidate["kind"] != "cp" or candidate["depth"] != depth):
        return None
    raw = decision["baseline"]
    if (raw.get("pov") != ("white" if board.turn else "black")
            or raw.get("source_fen") != board.fen(en_passant="fen") or raw.get("score_bound") != "exact"):
        return None
    final = _line(board, decision.get("candidate_final"), selected_uci)
    if (not final or final["kind"] != "cp" or final["depth"] < depth
            or final["depth"] == depth and final["cp"] != candidate["cp"]):
        return None
    return final


def _decision(board, turn, prefix):
    row = turn.get("sparring_decision")
    if (not isinstance(row, dict) or row.get("policy") not in SUPPORTED_POLICIES
            or row.get("ply") != turn["ply"] or row.get("before_fen") != turn["before_fen"]
            or row.get("history_uci") != prefix or row.get("selected_uci") != turn["uci"]):
        return None
    return row


def _observation(board, observation):
    if not isinstance(observation, dict) or not board.move_stack:
        return None
    before = board.copy(stack=True)
    played = before.pop()
    if (observation.get("valid") is not True or observation.get("forced") is not False
            or observation.get("history_before") != [move.uci() for move in before.move_stack]
            or observation.get("played_uci") != played.uci()
            or observation.get("ply") != len(board.move_stack)
            or observation.get("before_fen") != before.fen(en_passant="fen")
            or not _number(observation.get("loss_cp")) or observation["loss_cp"] < 0
            or type(observation.get("depth")) is not int or observation["depth"] < 4):
        return None
    return observation["loss_cp"]


def _phase(root_ply, policy=POLICY):
    ramp, finish = POLICY_PHASE_ROOTS[policy]
    return "finish" if root_ply >= finish else "ramp" if root_ply >= ramp else "calibration" if root_ply < 6 else "sparring"


def inspect_game(game, policy=None):
    """Replay actual moves; malformed records cannot contribute W/D/L or CP."""
    color = game.get("sparring_color")
    if color not in ("white", "black"):
        raise ValueError("invalid sparring color")
    board = chess.Board(game.get("initial_fen", ""))
    if board.fen(en_passant="fen") != chess.STARTING_FEN:
        raise ValueError("diagnostic must start from the standard position")
    moves, uci_moves = game.get("moves"), game.get("uci_moves")
    if not isinstance(moves, list) or not isinstance(uci_moves, list) or len(moves) != len(uci_moves):
        raise ValueError("move arrays must match")
    if policy is None:
        policy = next((turn["sparring_decision"]["policy"] for turn in moves
                       if isinstance(turn, dict) and isinstance(turn.get("sparring_decision"), dict)
                       and turn["sparring_decision"].get("policy") in SUPPORTED_POLICIES), POLICY)
    samples, failures = [], []
    for index, turn in enumerate(moves):
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("move after a claimable legal ending")
        if not isinstance(turn, dict) or turn.get("ply") != index + 1:
            raise ValueError("nonsequential ply")
        actor = "sparring" if board.turn == (color == "white") else "stockfish"
        if (type(turn.get("uci")) is not str or turn.get("actor") != actor or turn.get("before_fen") != board.fen(en_passant="fen")
                or turn.get("uci") != uci_moves[index]):
            raise ValueError("actor, history or before-FEN mismatch")
        sample = {"ply": index + 1, "actor": actor, "baseline_cp_white": None,
                  "selected_cp_white": None, "baseline_minus_loss_cp_white": None,
                  "sf_cp_white": None, "valid_decision": False, "concession": False,
                  "baseline_mate_white": None, "selected_mate_white": None, "sf_mate_white": None,
                  "baseline_score_kind": "unknown", "selected_score_kind": "unknown", "sf_score_kind": "unknown",
                  "winning_mate_avoidance": False, "mate_avoidance_final_cp_white": None,
                  "pacing": None, "resource_guard": None,
                  "different_move": False, "loss_cp": None, "observation_loss_cp": None,
                  "observation_reason": "missing", "reason": "missing_decision", "reliability": "unknown",
                  "phase": _phase(index, policy), "expected_phase": _phase(index, policy), "profile_loss_cp": None,
                  "skill_target_cp": None, "phase_target_cp": None, "control_target_cp": None,
                  "elapsed_ms": turn.get("elapsed_ms"), "search_elapsed_ms": turn.get("search_elapsed_ms"),
                  "relay_elapsed_ms": turn.get("relay_elapsed_ms"), "extra_elapsed_ms": None,
                  "baseline_elapsed_ms": None, "baseline_budget_ms": turn.get("baseline_budget_ms"),
                  "advertised_extra_budget_ms": turn.get("advertised_extra_budget_ms"),
                  "stockfish_selected_matches_pv": None,
                  "after_sparring_move": bool(samples and samples[-1]["actor"] == "sparring")}
        if actor == "sparring":
            decision = _decision(board, turn, uci_moves[:index])
            if decision is not None:
                sample.update(valid_decision=True, reason=decision.get("reason", "unknown"),
                              phase=decision.get("phase"),
                              reliability=(decision.get("reliability") or {}).get("reliability", "unknown"),
                              extra_elapsed_ms=decision.get("extra_elapsed_ms"),
                              baseline_elapsed_ms=decision.get("baseline_elapsed_ms"),
                              control_target_cp=decision.get("target_loss_cp"))
                pacing = decision.get("pacing")
                if (isinstance(pacing, dict) and pacing.get("active") is True
                        and type(pacing.get("event_ply")) is int and 1 <= pacing["event_ply"] <= index
                        and type(pacing.get("stage")) is int and 0 <= pacing["stage"] <= 4):
                    sample["pacing"] = {key: copy.deepcopy(pacing.get(key)) for key in (
                        "active", "event_ply", "exchange_start_ply", "settled_ply", "start_root_ply", "stage",
                        "debt_cp", "anchor_cp", "target_cp", "upper_cp", "allow_material_return", "return_cap_cp",
                        "ramp_fraction", "material_concessions_blocked", "reason")}
                guard = decision.get("resource_guard")
                if isinstance(guard, dict) and type(guard.get("active")) is bool:
                    sample["resource_guard"] = {key: copy.deepcopy(guard.get(key)) for key in (
                        "active", "trigger_ply", "material_concessions_blocked", "reason", "nonpawn_cp_total")}
                profile = decision.get("profile") or {}
                sample.update(profile_loss_cp=profile.get("smoothed_loss_cp"),
                              skill_target_cp=profile.get("skill_target_loss_cp"),
                              phase_target_cp=profile.get("target_loss_cp"))
                observation = decision.get("opponent_observation") or {}
                sample["observation_reason"] = observation.get("reason", "unknown")
                sample["observation_loss_cp"] = _observation(board, observation)
                base = _line(board, decision.get("baseline"), decision.get("baseline_uci"))
                loss = decision.get("loss_cp")
                changed = decision.get("baseline_uci") != turn["uci"]
                sample["different_move"] = changed
                selected = (_line(board, decision.get("candidate_compared"), turn["uci"]) if changed else base)
                if base is not None:
                    sample["baseline_cp_white"] = base["cp_white"]
                    sample["baseline_mate_white"] = base["mate_white"]
                    sample["baseline_score_kind"] = base["kind"]
                    sample["baseline_depth"] = base["depth"]
                mate_choice = _absolute_mate_choice(board, decision, base, selected, turn["uci"]) if changed else None
                if (base and selected and base["kind"] == "cp" and selected["kind"] == "cp"
                        and selected["depth"] == base["depth"] and _number(loss) and loss >= 0
                        and abs(loss - max(0, base["cp"] - selected["cp"])) < 1e-6):
                    sample.update(selected_cp_white=selected["cp_white"], selected_score_kind="cp", loss_cp=loss,
                                  baseline_minus_loss_cp_white=(base["cp"] - loss) * (1 if board.turn else -1),
                                  concession=changed and loss > 0)
                elif not changed and base and base["kind"] == "mate":
                    sample.update(selected_score_kind="mate", selected_mate_white=base["mate_white"])
                elif mate_choice is not None:
                    sample.update(selected_cp_white=selected["cp_white"], selected_score_kind="cp",
                                  winning_mate_avoidance=True, mate_avoidance_final_cp_white=mate_choice["cp_white"])
                elif changed:
                    failures.append({"ply": index + 1, "issue": "selected_root_evidence_missing_or_conflicting"})
                if sample["phase"] != sample["expected_phase"]:
                    failures.append({"ply": index + 1, "issue": "phase_schedule_mismatch"})
                if sample["expected_phase"] == "finish" and changed:
                    failures.append({"ply": index + 1, "issue": "finish_did_not_preserve_baseline"})
            else:
                failures.append({"ply": index + 1, "issue": "missing_or_mismatched_decision"})
        else:
            evaluation = turn.get("stockfish_score_packet") or {}
            if (evaluation.get("bound") == "exact" and evaluation.get("pv_legal") is True
                    and evaluation.get("score_pov_valid") is True and evaluation.get("score_pov") == "white"
                    and evaluation.get("before_fen") == turn["before_fen"]):
                pv = evaluation.get("pv_uci")
                if isinstance(pv, list) and pv:
                    cp, mate = evaluation.get("cp_white"), evaluation.get("mate_white")
                    sign = 1 if board.turn else -1
                    line = {"uci": pv[0], "pv_uci": pv, "depth": evaluation.get("depth"),
                            "cp": cp * sign if _number(cp) else cp, "cp_white": cp,
                            "mate": mate * sign if type(mate) is int else mate, "mate_white": mate}
                    if evaluation.get("winner") is not None:
                        line["winner"] = evaluation["winner"]
                    score = _line(board, line, pv[0])
                    if score:
                        sample.update(sf_cp_white=score["cp_white"], sf_mate_white=score["mate_white"],
                                      sf_score_kind=score["kind"], stockfish_selected_matches_pv=pv[0] == turn["uci"])
        try:
            board.push_uci(turn["uci"])
        except (ValueError, TypeError) as error:
            raise ValueError("illegal recorded move") from error
        if turn.get("after_fen") != board.fen(en_passant="fen"):
            raise ValueError("after-FEN mismatch")
        samples.append(sample)
    if game.get("plies") != len(moves) or game.get("final_fen") != board.fen(en_passant="fen"):
        raise ValueError("final history mismatch")
    category = _category(game, board)
    score = None
    if category == "legal_completed":
        score = .5 if game["result"] == "1/2-1/2" else float(game["result"] == ("1-0" if color == "white" else "0-1"))
    return {"category": category, "score": score, "samples": samples, "issues": failures}


def _metrics(samples, close_cp):
    own = [row for row in samples if row["actor"] == "sparring"]
    opponent = [row for row in samples if row["actor"] == "stockfish" and row["after_sparring_move"]]
    stable = [row for row in own if row["reliability"] == "stable"]
    valid = [row for row in own if row["valid_decision"]]
    concessions = [row for row in valid if row["concession"]]
    return {"sparring_moves": len(own), "valid_decisions": len(valid),
            "baseline_before_selection": _cp_stats([row["baseline_cp_white"] for row in own], len(own), close_cp, [row["baseline_mate_white"] for row in own]),
            "baseline_stable_subset": _cp_stats([row["baseline_cp_white"] for row in stable], len(stable), close_cp, [row["baseline_mate_white"] for row in stable]),
            "selected_root_same_depth": _cp_stats([row["selected_cp_white"] for row in own], len(own), close_cp, [row["selected_mate_white"] for row in own]),
            "baseline_minus_clamped_loss": _cp_stats([row["baseline_minus_loss_cp_white"] for row in own], len(own), close_cp),
            "selected_vs_clamped_loss_disagreements": sum(_number(row["selected_cp_white"])
                and _number(row["baseline_minus_loss_cp_white"])
                and abs(row["selected_cp_white"] - row["baseline_minus_loss_cp_white"]) > 1e-6 for row in own),
            "stockfish_next_root_auxiliary": _cp_stats([row["sf_cp_white"] for row in opponent], len(opponent), close_cp, [row["sf_mate_white"] for row in opponent]),
            "stockfish_exact_root_differs_from_weakened_selected_move": sum(row["stockfish_selected_matches_pv"] is False for row in opponent),
            "reliability_counts": dict(Counter(row["reliability"] for row in own)),
            "decision_reason_counts": dict(Counter(row["reason"] for row in own)),
            "different_from_baseline": sum(row["different_move"] for row in valid),
            "verified_winning_mate_avoidance": sum(row["winning_mate_avoidance"] for row in valid),
            "winning_mate_avoidance_inside_report_band": sum(row["winning_mate_avoidance"]
                and abs(row["selected_cp_white"]) <= close_cp and abs(row["mate_avoidance_final_cp_white"]) <= close_cp for row in valid),
            "winning_mate_avoidance_final_cp_white": _stats([row["mate_avoidance_final_cp_white"] for row in valid]),
            "verified_positive_loss_concessions": len(concessions),
            "concession_fraction_of_all_sparring_moves": len(concessions) / len(own) if own else None,
            "concession_fraction_of_valid_decisions": len(concessions) / len(valid) if valid else None,
            "concession_loss_cp": _stats([row["loss_cp"] for row in concessions]),
            "valid_quality_observations": sum(_number(row["observation_loss_cp"]) for row in own),
            "quality_loss_cp": _stats([row["observation_loss_cp"] for row in own]),
            "observation_reason_counts": dict(Counter(row["observation_reason"] for row in own)),
            "phase_counts": dict(Counter(row["phase"] for row in own)),
            "pacing_active_moves": sum(row["pacing"] is not None for row in own),
            "pacing_stage_counts": dict(Counter(str(row["pacing"]["stage"]) for row in own if row["pacing"])),
            "strict_band_while_pacing": dict(Counter(_classify(row["selected_cp_white"], close_cp,
                row["selected_score_kind"], row["selected_mate_white"]) for row in own if row["pacing"])),
            "resource_guard_active_moves": sum(bool(row["resource_guard"] and row["resource_guard"]["active"]) for row in own),
            "material_return_blocked_moves": sum(bool(row["resource_guard"] and row["resource_guard"]["material_concessions_blocked"]) for row in own),
            "latency_ms": {actor: {name: _stats([row[name] for row in samples if row["actor"] == actor])
                for name in ("elapsed_ms", "search_elapsed_ms", "relay_elapsed_ms", "baseline_elapsed_ms", "extra_elapsed_ms")}
                for actor in ("sparring", "stockfish")}}


def _classify(cp, close_cp, kind="unknown", mate=None):
    known_mate = kind == "mate" and type(mate) is int and mate != 0
    return "outside" if known_mate else "unknown" if not _number(cp) else "inside" if abs(cp) <= close_cp else "outside"


def _pacing_game(samples, close_cp):
    """Describe receipts and independently scored recovery within each episode."""
    trace, episodes, guards = [], defaultdict(list), set()
    for row in samples:
        if row["actor"] != "sparring":
            continue
        pacing, guard = row["pacing"], row["resource_guard"]
        if guard and guard["active"] and type(guard["trigger_ply"]) is int:
            guards.add(guard["trigger_ply"])
        if not pacing and not guard:
            continue
        item = {"ply": row["ply"], "pacing": pacing, "resource_guard": guard,
                "selected_cp_white": row["selected_cp_white"],
                "strict_classification": _classify(row["selected_cp_white"], close_cp,
                    row["selected_score_kind"], row["selected_mate_white"])}
        trace.append(item)
        if pacing:
            episodes[pacing["event_ply"]].append(item)
    results = []
    for event, rows in sorted(episodes.items()):
        previous, previous_ply, restoration = None, None, None
        for row in rows:
            if previous_ply != row["ply"] - 2:
                previous = None
            if previous == "outside" and row["strict_classification"] == "inside" and restoration is None:
                restoration = {"ply": row["ply"], "stage": row["pacing"]["stage"]}
            previous = row["strict_classification"]
            previous_ply = row["ply"]
        counts = Counter(row["strict_classification"] for row in rows)
        last = rows[-1]
        outcome = ("outside_at_stage4" if last["pacing"]["stage"] == 4 and last["strict_classification"] == "outside" else
                   "unknown_at_stage4" if last["pacing"]["stage"] == 4 and last["strict_classification"] == "unknown" else
                   "restored_after_known_outside" if restoration else
                   "no_observed_outside" if not counts["outside"] and not counts["unknown"] else
                   "incomplete_or_unverified_recovery")
        results.append({"event_ply": event, "first_ply": rows[0]["ply"], "last_ply": last["ply"],
                        "stages": [row["pacing"]["stage"] for row in rows],
                        "strict_counts": dict(counts), "first_verified_restoration": restoration,
                        "restoration_within_stages_2_to_4": bool(restoration and 2 <= restoration["stage"] <= 4),
                        "outcome": outcome})
    return {"episode_count": len(results), "episodes": results, "resource_guard_trigger_plies": sorted(guards),
            "episode_outcomes": dict(Counter(row["outcome"] for row in results)), "trace": trace}


def _band_sequence(sequence, reached_target_plies, close_cp, target_fullmoves=20):
    """Measure consecutive samples within one game; missing scores break runs."""
    trace, recoveries = [], []
    outside_run, longest, outside_start, first_exit = 0, 0, None, None
    for item in sequence:
        cp = item["cp_white"]
        status = _classify(cp, close_cp, item.get("score_kind"), item.get("mate_white"))
        trace.append({**item, "classification": status})
        if status == "outside":
            if outside_run == 0:
                outside_start = item["ply"]
            if first_exit is None:
                first_exit = item["ply"]
            outside_run += 1
            longest = max(longest, outside_run)
        else:
            if status == "inside" and outside_run:
                recoveries.append({"outside_ply": outside_start, "recovery_ply": item["ply"],
                                   "own_turns": outside_run})
            # unknown is a break, never evidence of a recovery.
            outside_run, outside_start = 0, None
    counts = Counter(row["classification"] for row in trace)
    all_inside = bool(trace) and counts["inside"] == len(trace)
    sustained = reached_target_plies and len(trace) == target_fullmoves and all_inside
    return {"close_cp": close_cp, "target_fullmoves": target_fullmoves, "expected_samples": target_fullmoves,
            "sample_count": len(trace), "inside_count": counts["inside"],
            "outside_count": counts["outside"], "unknown_count": counts["unknown"],
            "outside_mate_count": sum(row.get("score_kind") == "mate" and row["classification"] == "outside" for row in trace),
            "observed_prefix_all_inside": all_inside,
            "partial_prefix_all_inside": all_inside and not sustained,
            "sustained_through_target_full_moves": sustained,
            "first_exit_ply": first_exit, "longest_consecutive_outside": longest,
            "recovery_count": len(recoveries),
            "recovery_after_outside_own_turns": _stats([row["own_turns"] for row in recoveries]),
            "recoveries": recoveries, "sequence": trace}


def _bands(samples, close_cp, target_fullmoves):
    """Align each own move in the requested window with both root samples."""
    own = [row for row in samples if row["actor"] == "sparring" and row["ply"] <= 2 * target_fullmoves]
    by_ply = {row["ply"]: row for row in samples}
    sequences = {key: [] for key in ("baseline_before_selection", "selected_root_same_depth",
                                     "stockfish_next_root_auxiliary")}
    for row in own:
        for metric, prefix in (("baseline_before_selection", "baseline"),
                               ("selected_root_same_depth", "selected")):
            sequences[metric].append({"ply": row["ply"], "evaluation_record_ply": row["ply"],
                                      "cp_white": row[prefix + "_cp_white"],
                                      "score_kind": row[prefix + "_score_kind"],
                                      "mate_white": row[prefix + "_mate_white"]})
        following = by_ply.get(row["ply"] + 1)
        following = following if following and following["actor"] == "stockfish" else {}
        sequences["stockfish_next_root_auxiliary"].append({"ply": row["ply"],
            "evaluation_record_ply": row["ply"] + 1, "cp_white": following.get("sf_cp_white"),
            "score_kind": following.get("sf_score_kind", "unknown"), "mate_white": following.get("sf_mate_white")})
    return {key: _band_sequence(sequence, len(samples) >= 2 * target_fullmoves, close_cp, target_fullmoves)
            for key, sequence in sequences.items()}


def _band_totals(games, close_cp, target_fullmoves):
    """Sum completed per-game measurements; never concatenate different games."""
    result = {}
    for key in ("baseline_before_selection", "selected_root_same_depth", "stockfish_next_root_auxiliary"):
        bands = [game[f"continuous_first{target_fullmoves}_band"][key] for game in games]
        result[key] = {"close_cp": close_cp, "target_fullmoves": target_fullmoves, "games": len(bands),
            **{name: sum(row[name] for row in bands) for name in (
                "sample_count", "inside_count", "outside_count", "outside_mate_count", "unknown_count", "recovery_count")},
            **{name + "_games": sum(row[name] for row in bands) for name in (
                "sustained_through_target_full_moves", "partial_prefix_all_inside", "observed_prefix_all_inside")},
            "games_with_outside": sum(row["outside_count"] > 0 for row in bands),
            "games_with_unknown": sum(row["unknown_count"] > 0 for row in bands),
            "longest_consecutive_outside_in_one_game": max(
                (row["longest_consecutive_outside"] for row in bands), default=0),
            "first_exit_ply": _stats([row["first_exit_ply"] for row in bands]),
            "recovery_after_outside_own_turns": _stats([
                event["own_turns"] for row in bands for event in row["recoveries"]])}
    return result


def _match_policy(match):
    """Keep old receipts readable while preventing mixed-policy pooled scores."""
    policies = []
    if match.get("policy") is not None:
        policies.append(match["policy"])
    for game in match["games"]:
        host = game.get("host") or {}
        metadata = host.get("sparring_play") or {}
        if metadata.get("policy") is not None:
            policies.append(metadata["policy"])
        for move in game.get("moves", []):
            decision = move.get("sparring_decision") if isinstance(move, dict) else None
            if isinstance(decision, dict) and decision.get("policy") is not None:
                policies.append(decision["policy"])
    if not policies or any(type(value) is not str or value not in SUPPORTED_POLICIES for value in policies):
        raise ValueError("missing or unsupported sparring policy")
    if len(set(policies)) != 1:
        raise ValueError("do not pool different sparring policies")
    return policies[0]


def summarize_matches(matches, close_cp=100, target_fullmoves=None):
    """Pure aggregation; input games must contain detailed per-move records."""
    if type(close_cp) is not int or close_cp not in (100, 150):
        raise ValueError("close_cp must be 100 or 150")
    if target_fullmoves is not None and (type(target_fullmoves) is not int or target_fullmoves not in (20, 30, 40, 50)):
        raise ValueError("target_fullmoves must be 20, 30, 40 or 50")
    automatic_window = target_fullmoves is None
    target_fullmoves = 20 if automatic_window else target_fullmoves
    target_plies = 2 * target_fullmoves
    band_key = f"continuous_first{target_fullmoves}_band"
    window_key = f"first_{target_fullmoves}_full_moves"
    results, all_samples, conditions, configurations = [], [], None, []
    policy, input_runs = None, []
    groups, group_samples = defaultdict(list), defaultdict(list)
    planned = 0
    warnings = []
    for run, match in enumerate(matches):
        if not isinstance(match, dict) or match.get("schema") != MATCH_SCHEMA:
            raise ValueError("unsupported match schema")
        config, games = match.get("config"), match.get("games")
        if not isinstance(config, dict) or not isinstance(games, list):
            raise ValueError("match requires config and detailed games")
        current_policy = _match_policy(match)
        if policy is not None and policy != current_policy:
            raise ValueError("do not pool different sparring policies")
        if policy is None and automatic_window:
            # Historical v1/v2 invocations defaulted to a 40-move reporting
            # window (v1's full 50-move window remains an explicit option).
            target_fullmoves = (20 if current_policy == POLICY else
                                30 if current_policy == "regicide-sparring-play-v3" else 40)
            target_plies = 2 * target_fullmoves
            band_key = f"continuous_first{target_fullmoves}_band"
            window_key = f"first_{target_fullmoves}_full_moves"
        policy = current_policy
        input_runs.append({"run": run, "policy": policy, **{key: copy.deepcopy(match.get(key))
                          for key in ("source", "source_sha256", "binary_sha256", "catalog_sha256")}})
        current = {key: config.get(key) for key in CONDITIONS}
        if any(value is None for value in current.values()) or conditions is not None and current != conditions:
            raise ValueError("do not pool missing or different time/resource/cap conditions")
        conditions = current
        configurations.append(copy.deepcopy(config))
        schedule = match.get("schedule")
        if not isinstance(schedule, list):
            raise ValueError("schedule is required")
        if len(games) > len(schedule):
            raise ValueError("more records than scheduled games")
        planned += len(schedule)
        if match.get("status") == "completed" and any(match.get(k) is not True for k in ("sources_unchanged", "catalog_unchanged", "binaries_unchanged")):
            warnings.append({"run": run, "issue": "source_or_binary_unchanged_not_confirmed"})
        seen = set()
        for game in games:
            number, level = game.get("game"), game.get("stockfish_elo")
            if type(number) is not int or number in seen or type(level) is not int or level not in config.get("ratings", []):
                raise ValueError("invalid/duplicate game id or opponent setting")
            seen.add(number)
            try:
                checked = inspect_game(game, policy)
            except (ValueError, TypeError, KeyError) as error:
                checked = {"category": "invalid_record", "score": None, "samples": [],
                           "issues": [{"issue": str(error)}]}
            own = [row for row in checked["samples"] if row["actor"] == "sparring"]
            phases = {phase: [row["ply"] for row in own if row["valid_decision"]
                             and row["phase"] == phase and row["expected_phase"] == phase]
                      for phase in ("ramp", "finish")}
            result = {"run": run, "game": number, "pair": game.get("pair"), "stockfish_elo": level,
                      "policy": policy,
                      "sparring_color": game.get("sparring_color"), "sparring_seed": game.get("sparring_seed"),
                      "plies": game.get("plies"), "result": game.get("result"), "termination": game.get("termination"),
                      "category": checked["category"], "score": checked["score"], "issues": checked["issues"],
                      "reached_target_full_moves": len(checked["samples"]) >= target_plies,
                      "board_reached_ramp_threshold": len(checked["samples"]) >= POLICY_PHASE_ROOTS[policy][0],
                      "board_reached_finish_threshold": len(checked["samples"]) >= POLICY_PHASE_ROOTS[policy][1],
                      "reached_ramp_phase": bool(phases["ramp"]),
                      "reached_finish_phase": bool(phases["finish"]),
                      "phase_activation": {phase: {"moves": len(plies), "first_ply": plies[0] if plies else None}
                                           for phase, plies in phases.items()},
                      "pacing": _pacing_game(checked["samples"], close_cp),
                      band_key: _bands(checked["samples"], close_cp, target_fullmoves),
                      window_key: _metrics([row for row in checked["samples"] if row["ply"] <= target_plies], close_cp),
                      "all_moves": _metrics(checked["samples"], close_cp),
                      "control_trace": [{key: row[key] for key in ("ply", "phase", "profile_loss_cp", "skill_target_cp", "phase_target_cp", "control_target_cp", "loss_cp", "winning_mate_avoidance", "baseline_mate_white", "mate_avoidance_final_cp_white", "reason")} for row in own],
                      "trends": {key: _trend([row[key] for row in own]) for key in
                                 ("profile_loss_cp", "skill_target_cp", "phase_target_cp", "control_target_cp")}}
            results.append(result)
            groups[level].append(result)
            group_samples[level].extend(checked["samples"])
            all_samples.extend(checked["samples"])
    def outcome(rows):
        scores = [row["score"] for row in rows if row["score"] is not None]
        return {"games_recorded": len(rows), "categories": dict(Counter(row["category"] for row in rows)),
                "legal_completed": len(scores), "wins": scores.count(1.), "draws": scores.count(.5), "losses": scores.count(0.),
                "score_fraction_completed_only": sum(scores) / len(scores) if scores else None,
                "reached_target_full_moves": sum(row["reached_target_full_moves"] for row in rows),
                "board_reached_ramp_threshold": sum(row["board_reached_ramp_threshold"] for row in rows),
                "board_reached_finish_threshold": sum(row["board_reached_finish_threshold"] for row in rows),
                "reached_ramp_phase": sum(row["reached_ramp_phase"] for row in rows),
                "reached_finish_phase": sum(row["reached_finish_phase"] for row in rows),
                "pacing_episodes": sum(row["pacing"]["episode_count"] for row in rows)}
    pairs = defaultdict(list)
    for row in results:
        pairs[(row["run"], row["stockfish_elo"], row["pair"])].append(row)
    complete_pairs = sum(len(rows) == 2 and {row["sparring_color"] for row in rows} == {"white", "black"}
                         and len({row["sparring_seed"] for row in rows}) == 1
                         and all(row["category"] == "legal_completed" for row in rows) for rows in pairs.values())
    return {"schema": SUMMARY_SCHEMA, "purpose": "Eight-game functional/trend diagnostic; no Elo or causal training-effect estimate",
            "policy": policy, "close_cp": close_cp, "close_points": close_cp / 100,
            "target_fullmoves": target_fullmoves, "target_plies": target_plies,
            "policy_phase_root_plies": dict(zip(("ramp", "finish"), POLICY_PHASE_ROOTS.get(policy, POLICY_PHASE_ROOTS[POLICY]))),
            "threshold_inclusive": True,
            "conditions": conditions, "input_configs": configurations, "input_runs": input_runs, "planned_games": planned,
            "unplayed_games": planned - len(results), "outcomes": outcome(results),
            "by_level": [{"stockfish_elo": level, **outcome(rows),
                          band_key: _band_totals(rows, close_cp, target_fullmoves),
                          window_key: _metrics([row for row in group_samples[level] if row["ply"] <= target_plies], close_cp),
                          "all_moves": _metrics(group_samples[level], close_cp),
                          "control_trend_counts": {key: {name: sum(row["trends"][key][name] for row in rows)
                              for name in ("adjacent_known_transitions", "increases", "decreases", "unchanged")}
                              for key in ("profile_loss_cp", "skill_target_cp", "phase_target_cp", "control_target_cp")}}
                         for level, rows in sorted(groups.items())],
            "by_color": [{"sparring_color": color, **outcome([row for row in results if row["sparring_color"] == color]),
                          band_key: _band_totals([row for row in results if row["sparring_color"] == color], close_cp, target_fullmoves)}
                         for color in ("white", "black")],
            "complete_legal_color_pairs": complete_pairs, "warnings": warnings,
            band_key: _band_totals(results, close_cp, target_fullmoves),
            window_key: _metrics([row for row in all_samples if row["ply"] <= target_plies], close_cp),
            "all_moves": _metrics(all_samples, close_cp), "games": results,
            "definitions": {
                "target_window": f"Actual move ply <={target_plies} (pre-move root ply<{target_plies}); reaching {target_plies} plies counts even for capped games, with outcomes separated. This reporting window does not reinterpret the recorded policy's phase schedule",
                "close": f"abs(White-perspective finite exact CP)<={close_cp} (inclusive); CP ratios use available validated finite scores only. Validated mates are separately counted outside; missing, incoherent or inexact scores are unknown. Neither counts as inside",
                "continuous_band": f"Each game's first {target_fullmoves} sparring moves have separate baseline, selected-root and Stockfish-next-root sequences. Sustained success requires reaching {target_plies} plies and all {target_fullmoves} samples known inside. Partial all-inside prefixes are separate; no concatenation across games or evaluators, and no every-halfmove continuity claim. Pacing exceptions never convert outside samples into inside",
                "band_ply_axis": f"ply and first_exit_ply identify the associated sparring move. evaluation_record_ply identifies the move record containing the search; its root board has one fewer ply. Stockfish's initial white root is excluded; its root after Black's {target_fullmoves}th move is in record {target_plies + 1} (board ply {target_plies}) and is unknown if absent",
                "recovery": "Consecutive outside samples followed immediately by a known inside sample; own_turns counts sparring turns from the first outside sample. Unknown breaks both outside runs and recovery evidence",
                "pacing": "Pacing stages and resource guards are policy receipts, grouped by event_ply separately within each game; not independent proof of a human blunder. Strict selected-root classification and consecutive outside-to-inside restoration use validated scores, ignoring relaxed pacing status. Unknown breaks recovery evidence; temporary outside scores still fail strict continuity",
                "phase_activation": "Board reach at the policy's ramp/finish thresholds is separate from an actual valid matching decision. v4 roots40/138 activate White41/139 and Black42/140; v3 roots60/138 activate White61/139 and Black62/140; v2 roots80/138 activate White81/139 and Black82/140; v1 roots100/140 activate White101/141 and Black102/142. No early material phase guard",
                "baseline": "Own-engine unrestricted root score before policy selection; this is the gauge reference, not the played move score",
                "selected": "Own-engine candidate_compared CP for an alternative, baseline score otherwise; finite-baseline cases require the same depth. The explicit v2/v3/v4 winning-mate exception instead requires independently verified finite compared/final CP at depth>=4. Known outside values remain outside. Same pre-move root, not a later-board evaluation. Recomputed from score evidence, never trusted from band_control status",
                "mate_avoidance": "A validated positive own-perspective exact mate reference with a complete legal matching mate PV is declined for an independently verified finite candidate before root ply40 in v4, ply60 in v3 or ply80 in v2. Separate from CP-loss concessions; in-band mate avoidance has a separate count. Baseline CP, loss CP and CP subtraction remain null; raw mate scores are never converted to CP",
                "stability": "Finite exact CP includes provisional iterations; the stable-only baseline subset is separately reported",
                "stockfish": "Auxiliary next-root score after the sparring move and before Stockfish moves; not its weakened selected-move score. play merged-info depth/score packet coherence is not established",
                "latency": "Observed host wall time, not CPU time; measured overhead is not automatically a time-control failure",
                "move_denominator": "All legally replayed recorded prefixes, including capped/failed games; game outcomes and incomplete categories are separate",
                "trends": "Profile and control targets reflect the algorithm's receipts; not independently calibrated human skill. No transitions are joined across games",
                "limitations": "Two games per setting, adaptive strength, asymmetric extra budgets and no full-power control group; no Elo, objective CP calibration, human-feel, or guaranteed-win conclusion"}}


def load_match(path):
    path = Path(path).resolve(strict=True)
    match = json.loads(path.read_text(encoding="utf-8"))
    detailed = []
    for row in match.get("games", []):
        name = row.get("record_file")
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("record_file must be a local filename")
        record = json.loads((path.parent / name).read_text(encoding="utf-8"))
        for key, value in row.items():
            if record.get(key) != value:
                raise ValueError(f"summary/detail mismatch: {name}:{key}")
        detailed.append(record)
    match["games"] = detailed
    return match


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matches", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--close-cp", type=int, choices=(100, 150), default=100,
                        help="inclusive absolute CP threshold; 150 reproduces the historical wider band")
    parser.add_argument("--target-fullmoves", type=int, choices=(20, 30, 40, 50), default=None,
                        help="reporting window only; default 20 for v4, 30 for v3, historical default 40 for v1/v2; 50 preserves the older full window")
    args = parser.parse_args(argv)
    paths = [path.resolve(strict=True) for path in args.matches]
    if len(set(paths)) != len(paths):
        parser.error("duplicate input match")
    result = summarize_matches([load_match(path) for path in paths], close_cp=args.close_cp,
                               target_fullmoves=args.target_fullmoves)
    result.update(generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  inputs=[{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths])
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result["outcomes"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
