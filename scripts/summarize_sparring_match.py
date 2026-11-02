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
SUMMARY_SCHEMA = "regicide.sparring-match-summary.v1"
POLICY = "regicide-sparring-play-v1"
CONDITIONS = ("sparring_ms", "stockfish_ms", "threads", "hash_mb", "max_plies", "cap_policy", "classical")
CLOSE_CP = 150


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


def _cp_stats(values, eligible):
    known = [value for value in values if _number(value)]
    close = sum(abs(value) <= CLOSE_CP for value in known)
    return {"eligible_positions": eligible, "known_exact_cp": len(known),
            "unknown_or_inexact": eligible - len(known), "close_positions": close,
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
    if (not isinstance(line, dict) or not _number(line.get("cp"))
            or type(line.get("depth")) is not int or line["depth"] < 1
            or line.get("uci") != required or line.get("mate") is not None
            or line.get("bound", "exact") != "exact"):
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
    if position.is_checkmate():
        return None
    white_cp = line["cp"] if board.turn else -line["cp"]
    if "cp_white" in line and line["cp_white"] != white_cp:
        return None
    return {"cp": line["cp"], "cp_white": white_cp, "depth": line["depth"]}


def _decision(board, turn, prefix):
    row = turn.get("sparring_decision")
    if (not isinstance(row, dict) or row.get("policy") != POLICY
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


def _phase(root_ply):
    return "finish" if root_ply >= 140 else "ramp" if root_ply >= 100 else "calibration" if root_ply < 6 else "sparring"


def inspect_game(game):
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
                  "different_move": False, "loss_cp": None, "observation_loss_cp": None,
                  "observation_reason": "missing", "reason": "missing_decision", "reliability": "unknown",
                  "phase": _phase(index), "expected_phase": _phase(index), "profile_loss_cp": None,
                  "skill_target_cp": None, "phase_target_cp": None, "control_target_cp": None,
                  "elapsed_ms": turn.get("elapsed_ms"), "search_elapsed_ms": turn.get("search_elapsed_ms"),
                  "relay_elapsed_ms": turn.get("relay_elapsed_ms"), "extra_elapsed_ms": None,
                  "baseline_elapsed_ms": None, "baseline_budget_ms": turn.get("baseline_budget_ms"),
                  "advertised_extra_budget_ms": turn.get("advertised_extra_budget_ms"),
                  "stockfish_selected_matches_pv": None}
        if actor == "sparring":
            decision = _decision(board, turn, uci_moves[:index])
            if decision is not None:
                sample.update(valid_decision=True, reason=decision.get("reason", "unknown"),
                              phase=decision.get("phase"),
                              reliability=(decision.get("reliability") or {}).get("reliability", "unknown"),
                              extra_elapsed_ms=decision.get("extra_elapsed_ms"),
                              baseline_elapsed_ms=decision.get("baseline_elapsed_ms"),
                              control_target_cp=decision.get("target_loss_cp"))
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
                    sample["baseline_depth"] = base["depth"]
                if (base and selected and selected["depth"] == base["depth"] and _number(loss) and loss >= 0
                        and abs(loss - max(0, base["cp"] - selected["cp"])) < 1e-6):
                    sample.update(selected_cp_white=selected["cp_white"], loss_cp=loss,
                                  baseline_minus_loss_cp_white=(base["cp"] - loss) * (1 if board.turn else -1),
                                  concession=changed and loss > 0)
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
            if (evaluation.get("bound") == "exact" and _number(evaluation.get("cp_white"))
                    and evaluation.get("mate_white") is None and evaluation.get("pv_legal") is True
                    and evaluation.get("score_pov_valid") is True and evaluation.get("score_pov") == "white"
                    and evaluation.get("before_fen") == turn["before_fen"]):
                pv = evaluation.get("pv_uci")
                position = board.copy(stack=True)
                try:
                    if not isinstance(pv, list) or not 1 <= len(pv) <= 128 or any(type(uci) is not str for uci in pv):
                        raise ValueError("missing PV")
                    for uci in pv:
                        position.push_uci(uci)
                    if not position.is_checkmate():
                        sample["sf_cp_white"] = evaluation["cp_white"]
                        sample["stockfish_selected_matches_pv"] = pv[0] == turn["uci"]
                except (ValueError, TypeError):
                    pass
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


def _metrics(samples):
    own = [row for row in samples if row["actor"] == "sparring"]
    opponent = [row for row in samples if row["actor"] == "stockfish"]
    stable = [row for row in own if row["reliability"] == "stable"]
    valid = [row for row in own if row["valid_decision"]]
    concessions = [row for row in valid if row["concession"]]
    return {"sparring_moves": len(own), "valid_decisions": len(valid),
            "baseline_before_selection": _cp_stats([row["baseline_cp_white"] for row in own], len(own)),
            "baseline_stable_subset": _cp_stats([row["baseline_cp_white"] for row in stable], len(stable)),
            "selected_root_same_depth": _cp_stats([row["selected_cp_white"] for row in own], len(own)),
            "baseline_minus_clamped_loss": _cp_stats([row["baseline_minus_loss_cp_white"] for row in own], len(own)),
            "selected_vs_clamped_loss_disagreements": sum(_number(row["selected_cp_white"])
                and _number(row["baseline_minus_loss_cp_white"])
                and abs(row["selected_cp_white"] - row["baseline_minus_loss_cp_white"]) > 1e-6 for row in own),
            "stockfish_next_root_auxiliary": _cp_stats([row["sf_cp_white"] for row in opponent], len(opponent)),
            "stockfish_exact_root_differs_from_weakened_selected_move": sum(row["stockfish_selected_matches_pv"] is False for row in opponent),
            "reliability_counts": dict(Counter(row["reliability"] for row in own)),
            "decision_reason_counts": dict(Counter(row["reason"] for row in own)),
            "different_from_baseline": sum(row["different_move"] for row in valid),
            "verified_positive_loss_concessions": len(concessions),
            "concession_fraction_of_all_sparring_moves": len(concessions) / len(own) if own else None,
            "concession_fraction_of_valid_decisions": len(concessions) / len(valid) if valid else None,
            "concession_loss_cp": _stats([row["loss_cp"] for row in concessions]),
            "valid_quality_observations": sum(_number(row["observation_loss_cp"]) for row in own),
            "quality_loss_cp": _stats([row["observation_loss_cp"] for row in own]),
            "observation_reason_counts": dict(Counter(row["observation_reason"] for row in own)),
            "phase_counts": dict(Counter(row["phase"] for row in own)),
            "latency_ms": {actor: {name: _stats([row[name] for row in samples if row["actor"] == actor])
                for name in ("elapsed_ms", "search_elapsed_ms", "relay_elapsed_ms", "baseline_elapsed_ms", "extra_elapsed_ms")}
                for actor in ("sparring", "stockfish")}}


def summarize_matches(matches):
    """Pure aggregation; input games must contain detailed per-move records."""
    results, all_samples, conditions, configurations = [], [], None, []
    groups, group_samples = defaultdict(list), defaultdict(list)
    planned = 0
    warnings = []
    for run, match in enumerate(matches):
        if not isinstance(match, dict) or match.get("schema") != MATCH_SCHEMA:
            raise ValueError("unsupported match schema")
        config, games = match.get("config"), match.get("games")
        if not isinstance(config, dict) or not isinstance(games, list):
            raise ValueError("match requires config and detailed games")
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
                checked = inspect_game(game)
            except (ValueError, TypeError, KeyError) as error:
                checked = {"category": "invalid_record", "score": None, "samples": [],
                           "issues": [{"issue": str(error)}]}
            own = [row for row in checked["samples"] if row["actor"] == "sparring"]
            result = {"run": run, "game": number, "pair": game.get("pair"), "stockfish_elo": level,
                      "sparring_color": game.get("sparring_color"), "sparring_seed": game.get("sparring_seed"),
                      "plies": game.get("plies"), "result": game.get("result"), "termination": game.get("termination"),
                      "category": checked["category"], "score": checked["score"], "issues": checked["issues"],
                      "reached_50_full_moves": len(checked["samples"]) >= 100,
                      "reached_finish_phase": len(checked["samples"]) >= 140,
                      "first_50_full_moves": _metrics([row for row in checked["samples"] if row["ply"] <= 100]),
                      "all_moves": _metrics(checked["samples"]),
                      "control_trace": [{key: row[key] for key in ("ply", "phase", "profile_loss_cp", "skill_target_cp", "phase_target_cp", "control_target_cp", "loss_cp", "reason")} for row in own],
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
                "reached_50_full_moves": sum(row["reached_50_full_moves"] for row in rows),
                "reached_finish_phase": sum(row["reached_finish_phase"] for row in rows)}
    pairs = defaultdict(list)
    for row in results:
        pairs[(row["run"], row["stockfish_elo"], row["pair"])].append(row)
    complete_pairs = sum(len(rows) == 2 and {row["sparring_color"] for row in rows} == {"white", "black"}
                         and len({row["sparring_seed"] for row in rows}) == 1
                         and all(row["category"] == "legal_completed" for row in rows) for rows in pairs.values())
    return {"schema": SUMMARY_SCHEMA, "purpose": "Eight-game functional/trend diagnostic; no Elo or causal training-effect estimate",
            "conditions": conditions, "input_configs": configurations, "planned_games": planned,
            "unplayed_games": planned - len(results), "outcomes": outcome(results),
            "by_level": [{"stockfish_elo": level, **outcome(rows),
                          "first_50_full_moves": _metrics([row for row in group_samples[level] if row["ply"] <= 100]),
                          "all_moves": _metrics(group_samples[level]),
                          "control_trend_counts": {key: {name: sum(row["trends"][key][name] for row in rows)
                              for name in ("adjacent_known_transitions", "increases", "decreases", "unchanged")}
                              for key in ("profile_loss_cp", "skill_target_cp", "phase_target_cp", "control_target_cp")}}
                         for level, rows in sorted(groups.items())],
            "by_color": [{"sparring_color": color, **outcome([row for row in results if row["sparring_color"] == color])}
                         for color in ("white", "black")],
            "complete_legal_color_pairs": complete_pairs, "warnings": warnings,
            "first_50_full_moves": _metrics([row for row in all_samples if row["ply"] <= 100]),
            "all_moves": _metrics(all_samples), "games": results,
            "definitions": {
                "50_full_moves": "Actual move ply <=100 (pre-move root ply<100); reaching 100 plies counts even for capped games, with outcomes separated",
                "close": "abs(White-perspective finite exact CP)<=150; denominators contain only available validated scores, no mate conversion",
                "baseline": "Own-engine unrestricted root score before policy selection; this is the gauge reference, not the played move score",
                "selected": "Own-engine same-depth candidate_compared CP for an alternative, baseline CP otherwise; same pre-move root, not a later-board evaluation",
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
    args = parser.parse_args(argv)
    paths = [path.resolve(strict=True) for path in args.matches]
    if len(set(paths)) != len(paths):
        parser.error("duplicate input match")
    result = summarize_matches([load_match(path) for path in paths])
    result.update(generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  inputs=[{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths])
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result["outcomes"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
