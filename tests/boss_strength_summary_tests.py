"""Independent scoring examples for the exploratory boss/Stockfish ladder."""
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import boss_strength_summary as summary


def match(level=1700, scores=(1., .5, 0., .5)):
    games = []
    for index, score in enumerate(scores, 1):
        white = index % 2 == 1
        result = "1/2-1/2" if score == .5 else "1-0" if (score == 1) == white else "0-1"
        games.append({"game": index, "pair": (index + 1) // 2,
                      "boss_color": "white" if white else "black", "stockfish_elo": level,
                      "result": result, "termination": "threefold_repetition" if score == .5 else "checkmate",
                      "completed": True, "legal_result": True, "capped": False, "error": None})
    return {"schema": summary.MATCH_SCHEMA,
            "config": {"stockfish_elo": level, "boss_ms": 1000, "stockfish_ms": 2000,
                       "hash_mb": 16, "threads": 1, "games": 4, "max_plies": 300,
                       "cap_policy": "incomplete", "seed": 20261102}, "games": games}


class BossStrengthSummaryTests(unittest.TestCase):
    def test_color_orientation_draws_variance_and_pairs(self):
        result = summary.summarize_matches([match()])
        row = result["levels"][0]
        self.assertEqual(row["wdl"], {"wins": 1, "draws": 2, "losses": 1})
        self.assertEqual(row["score_statistics"]["mean"], .5)
        self.assertAlmostEqual(row["score_statistics"]["sample_variance"], 1 / 6)
        self.assertAlmostEqual(row["score_statistics"]["standard_error"], math.sqrt(1 / 24))
        self.assertEqual(row["paired_scores"], [.75, .25])
        self.assertEqual(row["paired_score_statistics"]["n"], 2)
        self.assertEqual(row["color_completed_games"], {"white": 2, "black": 2})

    def test_capdraw_error_interruption_and_unplayed_never_become_wdl(self):
        data = match(scores=(1., .5, 0.))
        data["games"][0].update(result="1/2-1/2", termination="ply_cap", capped=True,
                                completed=True, legal_result=False)
        data["games"][1].update(result="1-0", termination="engine_error", error="controlled failure")
        data["games"][2].update(result="*", termination="interrupted", completed=False, legal_result=False)
        result = summary.summarize_matches([data])
        row = result["levels"][0]
        self.assertEqual(row["legal_completed_games"], 0)
        self.assertEqual(row["wdl"], {"wins": 0, "draws": 0, "losses": 0})
        for key in ("capped_games", "failed_games", "interrupted_games", "unplayed_games"):
            self.assertEqual(row[key], 1)
        self.assertEqual(result["pooled_working_logistic"]["point"]["status"], "not_identifiable")

    def test_unverified_result_and_unfinished_pair_are_excluded(self):
        data = match()
        data["games"][0]["legal_result"] = False
        row = summary.summarize_matches([data])["levels"][0]
        self.assertEqual(row["other_incomplete_games"], 1)
        self.assertEqual(row["legal_completed_games"], 3)
        self.assertEqual(row["paired_scores"], [.25])

    def test_zero_variance_is_flagged_and_bounded_interval_does_not_collapse(self):
        for values in ([0.] * 4, [.5] * 4, [1.] * 4):
            stats = summary.score_statistics(values)
            self.assertTrue(stats["normal_interval_degenerate"])
            self.assertEqual(stats["normal95_descriptive"][0], stats["normal95_descriptive"][1])
            self.assertLess(*stats["hoeffding95_independent_units"])
        self.assertIsNone(summary.score_statistics([.5])["standard_error"])
        self.assertIsNone(summary.score_statistics([])["mean"])

    def test_logistic_half_score_and_color_symmetric_tiers(self):
        fitted = summary.logistic_equivalent([(1700, .5)] * 4)
        self.assertAlmostEqual(fitted["point"]["value"], 1700)
        lo, hi = (item["value"] for item in fitted["working_likelihood95_range"])
        self.assertLess(lo, 1700)
        self.assertGreater(hi, 1700)
        self.assertAlmostEqual(1700 - lo, hi - 1700)
        pooled = summary.logistic_equivalent([(1400, 1)] * 4 + [(1800, 0)] * 4)
        self.assertAlmostEqual(pooled["point"]["value"], 1600)
        shifted = summary.logistic_equivalent([(1600, 1)] * 4 + [(2000, 0)] * 4)
        self.assertAlmostEqual(shifted["point"]["value"], pooled["point"]["value"] + 200)

    def test_all_wins_losses_have_open_likelihood_ends_not_fake_finite_ratings(self):
        won = summary.logistic_equivalent([(1320, 1)] * 4)
        lost = summary.logistic_equivalent([(2100, 0)] * 4)
        self.assertEqual(won["point"], {"value": None, "status": "positive_infinity"})
        self.assertEqual(lost["point"], {"value": None, "status": "negative_infinity"})
        self.assertEqual(won["working_likelihood95_range"][0]["status"], "finite")
        self.assertEqual(won["working_likelihood95_range"][1]["status"], "positive_infinity")
        self.assertEqual(lost["working_likelihood95_range"][0]["status"], "negative_infinity")
        self.assertEqual(lost["working_likelihood95_range"][1]["status"], "finite")
        json.dumps([won, lost], allow_nan=False)

    def test_observed_crossing_is_separate_from_a_confidence_bracket(self):
        result = summary.summarize_matches([match(1320, (1, 1, 1, 0)), match(1700, (1, 0, 0, 0)),
                                            match(2100, (0, 0, 0, 0))])
        support = result["observed_matchup_support"]
        self.assertEqual(support["crossings"], [[1320, 1700]])
        self.assertEqual(support["nearest_settings"], [1320, 1700])
        self.assertIn("not a confidence bracket", support["scope"])
        self.assertFalse(support["sample_nonmonotonic"])

    def test_sparse_all_below_and_nonmonotonic_levels_are_explicit(self):
        result = summary.summarize_matches([match(1320, (0, 0, 0, 0)), match(1700, (0, 0, .5, 0))])
        support = result["observed_matchup_support"]
        self.assertEqual(support["status"], "all_observed_scores_below_half")
        self.assertEqual(support["crossings"], [])
        self.assertTrue(support["sample_nonmonotonic"])

    def test_followup_runs_do_not_merge_distinct_color_pairs(self):
        a, b = match(scores=(1, 0)), match(scores=(1, 1))
        a["config"]["games"] = b["config"]["games"] = 2
        b["config"]["seed"] += 1
        row = summary.summarize_matches([a, b])["levels"][0]
        self.assertEqual(row["paired_scores"], [.5, 1.])
        self.assertEqual(row["scheduled_games"], 4)

    def test_rejects_mixed_conditions_duplicate_games_and_wrong_level(self):
        changed = match(2100)
        changed["config"]["stockfish_ms"] = 1000
        with self.assertRaises(ValueError):
            summary.summarize_matches([match(), changed])
        for mutation in (lambda d: d["games"][1].update(game=1),
                         lambda d: d["games"][0].update(stockfish_elo=1800),
                         lambda d: d["games"][0].update(legal_result=1),
                         lambda d: d["games"][1].update(boss_color="white")):
            data = match()
            mutation(data)
            with self.assertRaises(ValueError):
                summary.summarize_matches([data])

    def test_pure_summary_does_not_mutate_evidence(self):
        data = match()
        saved = copy.deepcopy(data)
        summary.summarize_matches([data])
        self.assertEqual(data, saved)

    def test_cli_records_source_hash_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory(prefix="boss-summary-") as folder:
            folder = Path(folder)
            source, output = folder / "match.json", folder / "summary.json"
            source.write_text(json.dumps(match()), encoding="utf-8")
            command = [sys.executable, str(ROOT / "scripts/boss_strength_summary.py"), str(source), "--output", str(output)]
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(result["inputs"][0]["sha256"]), 64)
            original = output.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
