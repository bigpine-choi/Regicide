"""Run reproducible theory study and bounded novel-sacrifice research.

Examples (use a NEW output directory for every experiment):
  python scripts/gambit_research.py curriculum --output research/curriculum-01
  python scripts/gambit_research.py all --binary build/regicide --output research/run-01
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import platform
from pathlib import Path
import sys

from gambit_research_core import NativeAnalyzer, digest, source_hashes, write_json
from gambit_theory_curriculum import build_curriculum


def discovery_cases(curriculum, limit):
    """Owner-to-move roots, interleaved by family without consulting scores."""
    grouped = defaultdict(list)
    for case in curriculum["cases"]:
        if case["owner"] == case["turn"]:
            grouped[case["family_id"]].append(case)
    for rows in grouped.values():
        rows.sort(key=lambda row: (row["stage"] != "offer", len(row["history_uci"]), row["case_id"]))
    selected = []
    while len(selected) < limit and any(grouped.values()):
        for family in sorted(grouped):
            if grouped[family] and len(selected) < limit:
                selected.append(grouped[family].pop(0))
    return selected


def confirm_discovery(case, result, analyzer, *, candidates, nodes, deep_multiplier,
                      reply_limit, max_loss_cp, minimum_depth, stability_cp):
    """Recheck source-unseen survivors at a larger independent node budget."""
    from gambit_discovery import discover_case
    for name, value, lower in (("candidates", candidates, 1), ("nodes", nodes, 1),
                               ("deep_multiplier", deep_multiplier, 2),
                               ("reply_limit", reply_limit, 0), ("max_loss_cp", max_loss_cp, 0),
                               ("minimum_depth", minimum_depth, 1), ("stability_cp", stability_cp, 0)):
        if type(value) is not int or value < lower:
            raise ValueError(name + " must be an integer >= " + str(lower))
    survivors = [row for row in result.get("candidates", [])
                 if row.get("status") == "screened" and row.get("bounded_screen_passed")
                 and row.get("novelty", {}).get("unseen_position")]
    for row in result.get("candidates", []):
        row["two_budget_candidate"] = False
    if not survivors:
        result["confirmation_reason"] = "no_source_unseen_screened_candidate"
        return result
    deeper = discover_case({**case, "discovery_nodes": nodes * deep_multiplier}, analyzer,
                           candidate_limit=candidates, reply_limit=reply_limit,
                           max_loss_cp=max_loss_cp, minimum_depth=minimum_depth)
    result["confirmation"] = deeper
    matched = {row["uci"]: row for row in deeper.get("candidates", [])}
    for row in survivors:
        other = matched.get(row["uci"])
        if other is None:
            row["confirmation_reason"] = "candidate_not_revisited_with_deeper_budget"
            continue
        old = row.get("native_after_offer", {})
        new = other.get("native_after_offer", {})
        stable = (old.get("status") == "complete" and new.get("status") == "complete"
                  and type(old.get("owner_cp")) is int and type(new.get("owner_cp")) is int
                  and abs(old["owner_cp"] - new["owner_cp"]) <= stability_cp
                  and type(old.get("depth")) is int and type(new.get("depth")) is int
                  and new["depth"] >= old["depth"])
        row["two_budget_candidate"] = bool(other.get("status") == "screened"
                                              and other.get("bounded_screen_passed") and stable
                                              and other.get("novelty", {}).get("unseen_position"))
        row["confirmation_reason"] = ("two_budget_bounded_screen_passed" if row["two_budget_candidate"]
                                       else "deeper_screen_failed_incomplete_or_unstable")
    return result


def export_discovery_pgn(path, results):
    """Research evidence can be inspected on a board, including failed offers."""
    import chess
    import chess.pgn
    from gambit_research_core import replay
    games = []
    for result in results:
        for row in result.get("candidates", []):
            game = chess.pgn.Game()
            game.headers.update(Event="Regicide gambit research", Result="*",
                                ResearchCase=str(result.get("case_id", "")),
                                ResearchStatus=str(row.get("status", "unknown")),
                                Novelty=str(row.get("novelty", {}).get("classification", "unknown")))
            node = game
            board = replay(result["history_uci"])
            for move in board.move_stack:
                node = node.add_variation(move)
            offer = chess.Move.from_uci(row["uci"])
            if offer not in board.legal_moves:
                raise ValueError("Illegal recorded discovery offer")
            node = node.add_variation(offer)
            board.push(offer)
            node.comment = ("Finite own-engine research. Screened=" + str(row.get("bounded_screen_passed", False))
                            + "; two-budget candidate=" + str(row.get("two_budget_candidate", False))
                            + ". Not proof of soundness or historical novelty.")
            for uci in row.get("native_after_offer", {}).get("pv_uci", []):
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    break
                node = node.add_variation(move)
                board.push(move)
            games.append(str(game))
    Path(path).write_text("\n\n".join(games) + ("\n" if games else ""), encoding="utf-8")


def run(args):
    output = Path(args.output)
    # Research logs and predeclarations are immutable between runs.
    output.mkdir(parents=True, exist_ok=False)
    curriculum = build_curriculum(seed=args.seed, roots_per_family=args.roots_per_family,
                                  families=args.families)
    write_json(output / "curriculum.json", curriculum)
    cases = discovery_cases(curriculum, args.discovery_cases)
    manifest = {"schema": "regicide.gambit-research-run.v1", "command": args.command,
                "created_utc": datetime.now(timezone.utc).isoformat(), "seed": args.seed,
                "python": sys.version, "platform": platform.platform(),
                "settings": {key: value for key, value in vars(args).items() if key != "output"},
                "source_sha256": source_hashes(),
                "curriculum_sha256": digest(curriculum),
                "discovery_case_ids": [case["case_id"] for case in cases],
                "meaning": "offline own-engine research; no active policy replacement or strength claim"}
    write_json(output / "predeclared.json", manifest)
    summary = {"schema": "regicide.gambit-research-summary.v1", "command": args.command,
               "curriculum_cases": len(curriculum["cases"]),
               "families": dict(Counter(case["family_id"] for case in curriculum["cases"])),
               "active_policy_changed": False, "new_world_first_gambit_claimed": False}
    if args.command == "curriculum":
        write_json(output / "summary.json", summary)
        return summary
    if not args.binary:
        raise ValueError("--binary is required for native study or discovery")
    from gambit_discovery import discover_case
    from study_gambit_theory import study, train_study, write_artifacts
    with NativeAnalyzer(args.binary, output / "analysis", nodes=args.nodes,
                        wall_seconds=args.wall_seconds, minimum_depth=args.minimum_depth,
                        search_timeout=args.search_timeout) as analyzer:
        manifest["engine_identity"] = analyzer.identity
        write_json(output / "predeclared.json", manifest)
        if args.command in ("study", "all"):
            dataset = study(curriculum, analyzer, seed=args.seed,
                            candidate_limit=args.candidates,
                            deep_multiplier=args.deep_multiplier, stability_cp=args.stability_cp)
            training = train_study(dataset)
            write_artifacts(output / "study", dataset, training)
            summary["study"] = training["report"]
        if args.command in ("discover", "all"):
            results = []
            for case in cases:
                research_case = {**case, "reference_pgn_paths": args.reference_pgn,
                                 "absolute_floor_cp": args.absolute_floor_cp}
                result = discover_case(research_case, analyzer, candidate_limit=args.candidates,
                                       reply_limit=args.reply_limit, max_loss_cp=args.max_loss_cp,
                                       minimum_depth=args.minimum_depth)
                result = confirm_discovery(research_case, result, analyzer, candidates=args.candidates,
                                           nodes=args.nodes, deep_multiplier=args.deep_multiplier,
                                           reply_limit=args.reply_limit, max_loss_cp=args.max_loss_cp,
                                           minimum_depth=args.minimum_depth, stability_cp=args.stability_cp)
                results.append(result)
                write_json(output / "discovery.json", results)
                print("discovery " + case["case_id"], flush=True)
                if analyzer.teacher.closed:
                    break
            export_discovery_pgn(output / "discovery.pgn", results)
            summary["discovery"] = {"requested_cases": len(cases), "executed_cases": len(results),
                                     "candidate_count": sum(len(row.get("candidates", [])) for row in results),
                                     "bounded_screen_passed": sum(
                                         bool(candidate.get("bounded_screen_passed"))
                                         for row in results for candidate in row.get("candidates", [])),
                                     "source_unseen_two_budget_candidates": sum(
                                         bool(candidate.get("two_budget_candidate"))
                                         for row in results for candidate in row.get("candidates", [])),
                                     "proof_of_soundness": False,
                                     "novelty_scope": "full pinned named-opening catalog plus explicitly supplied PGN files"}
        summary["queries"] = len(analyzer.records)
        summary["query_statuses"] = dict(Counter(row["status"] for row in analyzer.records))
    summary["engine_cleanup"] = analyzer.teacher.cleanup_status
    write_json(output / "summary.json", summary)
    return summary


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("curriculum", "study", "discover", "all"))
    result.add_argument("--output", required=True, help="New experiment directory (must not exist)")
    result.add_argument("--binary", help="Own Regicide UCI executable")
    result.add_argument("--seed", type=int, default=20261105)
    result.add_argument("--roots-per-family", type=int, default=8)
    result.add_argument("--families", nargs="+")
    result.add_argument("--nodes", type=int, default=25000)
    result.add_argument("--deep-multiplier", type=int, default=4)
    result.add_argument("--stability-cp", type=int, default=80)
    result.add_argument("--minimum-depth", type=int, default=4)
    result.add_argument("--wall-seconds", type=float, default=600)
    result.add_argument("--search-timeout", type=float, default=10)
    result.add_argument("--candidates", type=int, default=4)
    result.add_argument("--discovery-cases", type=int, default=4)
    result.add_argument("--reply-limit", type=int, default=0,
                        help="0 checks every legal first defense; truncation can never pass the screen")
    result.add_argument("--max-loss-cp", type=int, default=60)
    result.add_argument("--absolute-floor-cp", type=int, default=-100)
    result.add_argument("--reference-pgn", action="append", default=[],
                        help="Additional local PGN scope for novelty comparison (repeatable)")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.candidates < 1 or args.discovery_cases < 1 or args.roots_per_family < 1:
        raise SystemExit("candidate, case and root limits must be positive")
    if args.reply_limit < 0 or args.max_loss_cp < 0:
        raise SystemExit("reply limit and relative CP loss must be nonnegative")
    if args.deep_multiplier < 2 or args.stability_cp < 0:
        raise SystemExit("deep multiplier must be >=2 and stability CP nonnegative")
    if args.command in ("study", "all") and args.candidates < 2:
        raise SystemExit("study requires at least two candidates")
    try:
        summary = run(args)
    except (ValueError, FileExistsError, OSError) as error:
        raise SystemExit(str(error)) from error
    import json
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
