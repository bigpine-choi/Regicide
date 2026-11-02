"""Describe a boss-mode ladder on the Stockfish UCI_Elo settings axis.

Only legal completed games enter W/D/L and scores. Caps, failures and unfinished
games remain visible. The optional logistic fit is a working-model equivalent,
not a calibrated human/Chess.com/FIDE rating or an engine-strength certificate.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import datetime
import hashlib
import json
import math
from pathlib import Path

MATCH_SCHEMA = "regicide.boss-strength-match.v1"
SUMMARY_SCHEMA = "regicide.boss-strength-summary.v1"
Z95 = 1.959963984540054
LR95 = 3.841458820694124
ELO_SLOPE = math.log(10) / 400
RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}
CONDITIONS = ("boss_ms", "stockfish_ms", "hash_mb", "threads", "max_plies", "cap_policy")


def _integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def score_statistics(scores):
    """Sample variance of actual 0/.5/1 scores, without binomial draw fiction.

    Normal intervals are descriptive and can collapse for small samples. The
    separate bounded interval uses Hoeffding and requires independent units;
    callers also supply color-pair averages to expose paired dependence.
    """
    values = list(scores)
    if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1 for x in values):
        raise ValueError("scores must be finite values in [0, 1]")
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "sample_variance": None, "standard_error": None,
                "normal95_descriptive": None, "normal_interval_degenerate": None,
                "hoeffding95_independent_units": None}
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1) if n > 1 else None
    se = math.sqrt(variance / n) if variance is not None else None
    normal = [max(0., mean - Z95 * se), min(1., mean + Z95 * se)] if se is not None else None
    radius = math.sqrt(math.log(40) / (2 * n))
    return {"n": n, "mean": mean, "sample_variance": variance, "standard_error": se,
            "normal95_descriptive": normal, "normal_interval_degenerate": variance == 0 if variance is not None else None,
            "hoeffding95_independent_units": [max(0., mean - radius), min(1., mean + radius)]}


def _coordinate(value, status=None):
    return {"value": value, "status": status or "finite"}


def _sigmoid(x):
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    exp = math.exp(x)
    return exp / (1 + exp)


def _softplus(x):
    return max(x, 0) + math.log1p(math.exp(-abs(x)))


def _bisect(function, lower, upper, target=0., increasing=True):
    for _ in range(100):
        middle = (lower + upper) / 2
        if (function(middle) < target) == increasing:
            lower = middle
        else:
            upper = middle
    return (lower + upper) / 2


def logistic_equivalent(observations):
    """Fractional-score logistic quasi-MLE, fixed 400-point logistic scale.

    Draws contribute half a success only to this explicit working likelihood.
    Its likelihood range is NOT a draw/pair-adjusted confidence interval.
    Infinite MLEs and open interval ends are represented without JSON Infinity.
    """
    rows = list(observations)
    for rating, score in rows:
        _integer(rating, "stockfish_elo")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("invalid game score")
    scope = ("Descriptive fractional-score logistic working model on the recorded Stockfish UCI_Elo settings axis; "
             "not human Elo. Likelihood range assumes independent pseudo-Bernoulli scores and ignores color-pair "
             "correlation, draw modeling, adaptive tier selection, and calibration/time-control mismatch.")
    if not rows:
        return {"games": 0, "point": _coordinate(None, "not_identifiable"),
                "working_likelihood95_range": None, "scope": scope}
    total = sum(score for _, score in rows)
    lower = min(rating for rating, _ in rows) - 10000
    upper = max(rating for rating, _ in rows) + 10000

    def predicted_sum(theta):
        return sum(_sigmoid(ELO_SLOPE * (theta - rating)) for rating, _ in rows)

    def likelihood(theta):
        return sum(-score * _softplus(-ELO_SLOPE * (theta - rating))
                   - (1 - score) * _softplus(ELO_SLOPE * (theta - rating)) for rating, score in rows)

    if total == 0:
        point, maximum = _coordinate(None, "negative_infinity"), 0.
        edge = _bisect(likelihood, lower, upper, -LR95 / 2, increasing=False)
        interval = [_coordinate(None, "negative_infinity"), _coordinate(edge)]
    elif total == len(rows):
        point, maximum = _coordinate(None, "positive_infinity"), 0.
        edge = _bisect(likelihood, lower, upper, -LR95 / 2)
        interval = [_coordinate(edge), _coordinate(None, "positive_infinity")]
    else:
        theta = _bisect(predicted_sum, lower, upper, total)
        point, maximum = _coordinate(theta), likelihood(theta)
        interval = [_coordinate(_bisect(likelihood, lower, theta, maximum - LR95 / 2)),
                    _coordinate(_bisect(likelihood, theta, upper, maximum - LR95 / 2, increasing=False))]
    return {"games": len(rows), "point": point, "working_likelihood95_range": interval,
            "log_likelihood_at_supremum": maximum, "scope": scope}


def _category(game):
    for key in ("completed", "legal_result", "capped"):
        if type(game.get(key)) is not bool:
            raise ValueError(f"game.{key} must be boolean")
    result, termination = game.get("result"), game.get("termination")
    if result not in RESULTS or not isinstance(termination, str):
        raise ValueError("invalid game result or termination")
    if game.get("error") is not None and not isinstance(game["error"], str):
        raise ValueError("game.error must be text or null")
    if termination == "interrupted":
        return "interrupted"
    if game.get("error") or termination in ("engine_error", "timeout", "illegal_move"):
        return "failed"
    if game["capped"] or termination in ("ply_cap", "ply_cap_draw"):
        return "capped"
    if game["completed"] and game["legal_result"] and result != "*":
        return "legal_completed"
    return "other_incomplete"


def _score(game):
    if game["result"] == "1/2-1/2":
        return .5
    white_won = game["result"] == "1-0"
    return float(white_won == (game["boss_color"] == "white"))


def _observed_bracket(levels):
    measured = [row for row in levels if row["score_statistics"]["mean"] is not None]
    if not measured:
        return {"status": "no_legal_completed_games", "nearest_settings": [], "crossings": [], "exact_half_score_settings": []}
    distance = min(abs(row["score_statistics"]["mean"] - .5) for row in measured)
    nearest = [row["stockfish_elo"] for row in measured if abs(row["score_statistics"]["mean"] - .5) == distance]
    exact = [row["stockfish_elo"] for row in measured if row["score_statistics"]["mean"] == .5]
    crossings = [[left["stockfish_elo"], right["stockfish_elo"]] for left, right in zip(measured, measured[1:])
                 if left["score_statistics"]["mean"] > .5 > right["score_statistics"]["mean"]]
    status = ("observed_crossing" if crossings else "observed_half_score" if exact else
              "all_observed_scores_below_half" if all(row["score_statistics"]["mean"] < .5 for row in measured) else
              "all_observed_scores_above_half" if all(row["score_statistics"]["mean"] > .5 for row in measured) else
              "nonmonotonic_no_crossing")
    return {"status": status, "nearest_settings": nearest, "crossings": crossings,
            "exact_half_score_settings": exact,
            "sample_nonmonotonic": any(left["score_statistics"]["mean"] < right["score_statistics"]["mean"]
                                       for left, right in zip(measured, measured[1:])),
            "scope": "Observed matchup support only, not a confidence bracket or calibrated rating. Sparse or adaptive tiers may misorder."}


def summarize_matches(matches):
    groups, conditions, input_configs = defaultdict(list), None, []
    scheduled = defaultdict(int)
    for run, match in enumerate(matches):
        if not isinstance(match, dict) or match.get("schema") != MATCH_SCHEMA:
            raise ValueError("unsupported match schema")
        config, games = match.get("config"), match.get("games")
        if not isinstance(config, dict) or not isinstance(games, list):
            raise ValueError("match requires config and games")
        level = _integer(config.get("stockfish_elo"), "config.stockfish_elo")
        count = _integer(config.get("games"), "config.games")
        current = {key: config.get(key) for key in CONDITIONS}
        if any(value is None for value in current.values()):
            raise ValueError("missing match conditions")
        if conditions is not None and current != conditions:
            raise ValueError("do not pool different time/resource/cap conditions")
        conditions = current
        if len(games) > count:
            raise ValueError("more game records than scheduled games")
        ids, pair_colors = set(), set()
        for game in games:
            if not isinstance(game, dict):
                raise ValueError("game must be an object")
            number = _integer(game.get("game"), "game.game")
            pair = _integer(game.get("pair"), "game.pair")
            if number > count or number in ids:
                raise ValueError("duplicate or unscheduled game")
            ids.add(number)
            if game.get("stockfish_elo") != level or type(game.get("stockfish_elo")) is not int:
                raise ValueError("game level does not match config")
            color = game.get("boss_color")
            if color not in ("white", "black") or (pair, color) in pair_colors:
                raise ValueError("invalid or duplicated color in pair")
            pair_colors.add((pair, color))
            category = _category(game)
            groups[level].append({"run": run, "pair": pair, "color": color, "category": category,
                                  "score": _score(game) if category == "legal_completed" else None})
        scheduled[level] += count
        input_configs.append(dict(config))
    levels, pooled = [], []
    for level in sorted(scheduled):
        records = groups[level]
        complete = [row for row in records if row["category"] == "legal_completed"]
        scores = [row["score"] for row in complete]
        pair_rows = defaultdict(list)
        for row in records:
            pair_rows[(row["run"], row["pair"])].append(row)
        pair_scores = [sum(row["score"] for row in pair) / 2 for pair in pair_rows.values()
                       if len(pair) == 2 and {row["color"] for row in pair} == {"white", "black"}
                       and all(row["category"] == "legal_completed" for row in pair)]
        observations = [(level, score) for score in scores]
        pooled.extend(observations)
        levels.append({"stockfish_elo": level, "scheduled_games": scheduled[level],
                       "attempted_games": len(records), "unplayed_games": scheduled[level] - len(records),
                       "legal_completed_games": len(scores),
                       **{name + "_games": sum(row["category"] == name for row in records)
                          for name in ("capped", "failed", "interrupted", "other_incomplete")},
                       "wdl": {"wins": scores.count(1.), "draws": scores.count(.5), "losses": scores.count(0.)},
                       "color_completed_games": {color: sum(row["color"] == color for row in complete) for color in ("white", "black")},
                       "score_statistics": score_statistics(scores),
                       "paired_score_statistics": score_statistics(pair_scores),
                       "paired_scores": pair_scores, "logistic_equivalent": logistic_equivalent(observations)})
    total_keys = ("scheduled_games", "attempted_games", "unplayed_games", "legal_completed_games",
                  "capped_games", "failed_games", "interrupted_games", "other_incomplete_games")
    return {"schema": SUMMARY_SCHEMA, "conditions": conditions, "input_configs": input_configs,
            "levels": levels, "totals": {key: sum(row[key] for row in levels) for key in total_keys},
            "observed_matchup_support": _observed_bracket(levels),
            "pooled_working_logistic": logistic_equivalent(pooled),
            "caveats": [
                "Stockfish UCI_Elo settings at these time controls are not Chess.com, Lichess, FIDE, or calibrated human ratings.",
                "Only legal completed games enter scores; caps/failures/interruption may bias the completed subset and are reported separately.",
                "Normal score intervals use sample variance and a normal approximation; tiny samples and zero variance do not establish certainty.",
                "Color-pair averages are the preferred descriptive units; their bounded intervals still assume independent pairs.",
                "The pooled logistic value and likelihood range use a working fractional-score model, not calibrated rating inference.",
                "A data-dependent follow-up ladder is exploratory; nominal ranges do not correct for repeated looks or selected tiers.",
                "Fixed boss seeds do not make Stockfish's randomized strength-limited choices or shared-PC timing deterministic.",
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matches", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="new JSON path; existing files are not overwritten")
    args = parser.parse_args()
    paths = [path.resolve() for path in args.matches]
    if len(set(paths)) != len(paths):
        parser.error("duplicate input paths")
    matches, sources, hashes = [], [], set()
    for path in paths:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in hashes:
            parser.error("duplicate match content")
        hashes.add(digest)
        matches.append(json.loads(raw.decode("utf-8-sig")))
        sources.append({"path": str(path), "sha256": digest})
    summary = summarize_matches(matches)
    summary.update(generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), inputs=sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(args.output.resolve()), "totals": summary["totals"],
                      "observed_matchup_support": summary["observed_matchup_support"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
