"""Independent replay audit of study evidence and final discovery evidence.

This script reads recorded UCI, labels, models and PGNs. It starts no engine,
changes no source/policy, and never interprets bounded search as a chess proof.
Use --study-run and --discovery-run to retain a completed study whose source
snapshot differs only in the separately revalidated discovery/CLI code.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import random

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[3]
POLICY_BASELINE = "48e2b85b5005fca1914625e13dcc42067a46a58553e960a590ee540e823c48aa"
ALLOWED_STUDY_ONLY_CHANGES = {"scripts/gambit_discovery.py", "scripts/gambit_research.py"}
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def key(board):
    return " ".join(board.fen(en_passant="legal").split()[:4])


def board_for(moves):
    board = chess.Board()
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("illegal replay move: " + uci)
        board.push(move)
    return board


def balance(board, owner):
    return sum(value * (len(board.pieces(piece, owner)) - len(board.pieces(piece, not owner)))
               for piece, value in VALUES.items())


class Audit:
    def __init__(self):
        self.failures, self.checked = [], Counter()

    def check(self, condition, category, detail):
        self.checked[category] += 1
        if not condition:
            self.failures.append({"category": category, "detail": detail})


def audit_queries(run, audit, *, allow_study_changes=False):
    run = Path(run).resolve()
    pre = read(run / "predeclared.json")
    am = read(run / "analysis/analysis_manifest.json")
    queries = [json.loads(line) for line in (run / "analysis/queries.jsonl").read_text().splitlines()]
    raw = [json.loads(line) for line in (run / "analysis/native-uci.jsonl").read_text().splitlines()]
    checkpoint_count = len(queries)
    final_snapshot = run / "analysis/queries.json"
    audit.check(final_snapshot.is_file() and read(final_snapshot) == queries,
                "final_json_and_jsonl_agree", str(run))
    audit.check([query["query_id"] for query in queries] == ["q%06d" % i for i in range(1, len(queries) + 1)],
                "final_query_ids_contiguous", str(run))
    incremental_path = run / "analysis/queries.incremental.jsonl"
    incremental_ids = set()
    malformed_incremental = 0
    if incremental_path.is_file():
        for line in incremental_path.read_text().splitlines():
            try:
                incremental_ids.add(json.loads(line)["query_id"])
            except (ValueError, KeyError, TypeError):
                malformed_incremental += 1
    incremental_missing = sorted({query["query_id"] for query in queries} - incremental_ids)
    audit.check([row["seq"] for row in raw] == list(range(1, len(raw) + 1)), "raw_sequence", str(run))
    mapping = {row["query_id"]: row for row in queries}
    audit.check(len(mapping) == len(queries), "unique_query_ids", str(run))
    requested, actual, wall, wall_max, issued = 0, 0, 0.0, 0.0, 0
    for query in queries:
        identity = str(run.name) + ":" + query["query_id"]
        board = board_for(query["history_uci"])
        audit.check(board.fen(en_passant="fen") == query["fen"] and query["root_fen"] == chess.STARTING_FEN,
                    "query_history", identity)
        audit.check(("white" if board.turn else "black") == query["turn"], "query_pov", identity)
        requested += query["requested_nodes"]
        actual += query.get("nodes") or 0
        native = query.get("native")
        if native is None:
            audit.check(query["status"] != "complete", "query_completion", identity)
            continue
        wall += native.get("wall_seconds") or 0
        wall_max = max(wall_max, native.get("wall_seconds") or 0)
        begin, end = native["uci_begin_seq"], native["uci_end_seq"]
        if begin > end:
            audit.check(query["status"] != "complete" and native.get("completed") is not True,
                        "unissued_query_not_complete", identity)
            continue
        audit.check(1 <= begin <= end <= len(raw), "raw_query_range", identity)
        lines = raw[begin - 1:end]
        sent = [row["line"] for row in lines if row["direction"] == "send"]
        issued += sum(line.startswith("go nodes ") for line in sent)
        expected = "position startpos" + (" moves " + " ".join(query["history_uci"]) if query["history_uci"] else "")
        audit.check(sent.count(expected) == 1 and sent.count("ucinewgame") == 1
                    and sent.count("go nodes " + str(query["requested_nodes"])) == 1,
                    "raw_full_history_fresh_game_and_budget", identity)
        received = [row["line"] for row in lines if row["direction"] == "recv"]
        if query["status"] == "complete":
            audit.check(native["completed"] and not native["lowerbound"] and not native["upperbound"],
                        "query_completion", identity)
            best = [line.split()[1] for line in received if line.startswith("bestmove ")]
            infos = [line.split() for line in received if line.startswith("info depth ") and " score " in line]
            audit.check(best == [query["bestmove_uci"]] and bool(infos), "raw_bestmove", identity)
            info = infos[-1]
            kind = info[info.index("score") + 1]
            score = int(info[info.index("score") + 2])
            depth = int(info[info.index("depth") + 1])
            pv = info[info.index("pv") + 1:]
            audit.check(query["depth"] == depth and query["pv_uci"] == pv
                        and query["bestmove_uci"] == pv[0], "raw_completed_info", identity)
            audit.check((query["cp"] == score and query["mate"] is None) if kind == "cp"
                        else (query["mate"] == score and query["cp"] is None), "raw_score_pov", identity)
            audit.check(native["score"] == {"kind": kind, "stm": score}, "native_score_matches_raw", identity)
            for uci in pv:
                move = chess.Move.from_uci(uci)
                audit.check(move in board.legal_moves, "legal_query_pv", identity + ":" + uci)
                if move not in board.legal_moves:
                    break
                board.push(move)
    sources = pre["source_sha256"]
    changed = [name for name, expected in sources.items() if not (ROOT / name).is_file() or sha(ROOT / name) != expected]
    audit.check(not changed or allow_study_changes and set(changed) <= ALLOWED_STUDY_ONLY_CHANGES,
                "source_snapshot_scope", {"run": run.name, "changed": changed})
    for name in ("binary_sha256", "options"):
        audit.check(pre["engine_identity"][name] == am["identity"][name], "engine_manifest", name)
    audit.check(pre["source_sha256"] == am["identity"]["source_sha256"], "predeclared_source_identity", str(run))
    binary = ROOT / pre["settings"]["binary"]
    audit.check(binary.is_file() and sha(binary) == am["identity"]["binary_sha256"], "binary_identity", str(run))
    summary = read(run / "analysis/analysis_summary.json")
    audit.check(summary.get("final_query_ledger_sha256") == sha(run / "analysis/queries.jsonl")
                and summary.get("final_query_ledger_rows") == len(queries), "final_ledger_summary_sha_and_rows", str(run))
    cleanup = summary["cleanup"]
    audit.check(summary["closed"] is True and cleanup["transport_closed"] is True
                and cleanup["returncode_observed"] is True, "engine_cleanup", str(run))
    audit.check(summary["queries"] == len(queries) and summary["requested_nodes"] == requested
                and summary["actual_nodes"] == actual, "resource_totals", str(run))
    audit.check(summary["statuses"] == dict(Counter(row["status"] for row in queries)), "query_status_totals", str(run))
    audit.check(summary["binary_unchanged"] is True, "binary_unchanged", str(run))
    if not allow_study_changes:
        audit.check(summary["sources_unchanged"] is True and not changed, "final_discovery_source_unchanged", str(run))
    return mapping, {"run": str(run.relative_to(ROOT)), "queries": len(queries), "raw_lines": len(raw),
                     "original_query_checkpoints": checkpoint_count,
                     "final_query_snapshot_sha256": sha(final_snapshot),
                     "incremental_checkpoint_missing_ids": incremental_missing,
                     "incremental_checkpoint_malformed_lines": malformed_incremental,
                     "final_ledger_is_authoritative_and_independently_raw_verified": True,
                     "statuses": dict(Counter(row["status"] for row in queries)),
                     "requested_nodes": requested, "actual_nodes": actual, "query_wall_seconds_sum": wall,
                     "maximum_query_wall_seconds": wall_max, "issued_go_commands": issued,
                     "queries_without_available_node_counters": sum(query.get("nodes") is None for query in queries),
                     "resource_interpretation": "Requested nodes include recorded requests; actual nodes sum available counters only; wall sum excludes preparation and is not a speed comparison",
                     "source_changes_since_run_start": changed, "recorded_sources_unchanged": summary["sources_unchanged"],
                     "cleanup": cleanup, "raw_sha256": sha(run / "analysis/native-uci.jsonl"),
                     "queries_sha256": sha(run / "analysis/queries.jsonl")}


def independently_fit(groups, seed, regularization):
    vectors = [row["features"] for group in groups for row in group["candidates"]]
    count = len(vectors[0])
    means = [sum(row[i] for row in vectors) / len(vectors) for i in range(count)]
    scales = [max(.25, math.sqrt(sum((row[i] - means[i]) ** 2 for row in vectors) / len(vectors))) for i in range(count)]
    pairs = []
    for group in groups:
        rows = group["candidates"]
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                gap = a["owner_cp"] - b["owner_cp"]
                if abs(gap) < 15:
                    continue
                better, worse = (a, b) if gap > 0 else (b, a)
                pairs.append(([(x - y) / scale for x, y, scale in zip(better["features"], worse["features"], scales)],
                              min(2., max(.25, abs(gap) / 100.))))
    weights, rng = [0.] * count, random.Random(seed)
    for epoch in range(120):
        rng.shuffle(pairs)
        rate = .025 / math.sqrt(1 + epoch / 12.)
        for vector, importance in pairs:
            margin = max(-40., min(40., sum(a * b for a, b in zip(weights, vector))))
            gradient = importance / (1 + math.exp(margin))
            for i, value in enumerate(vector):
                weights[i] += rate * (gradient * value - regularization * weights[i])
    return {"weights": [round(x, 10) for x in weights], "means": [round(x, 10) for x in means],
            "scales": [round(x, 10) for x in scales], "training_pairs": len(pairs)}


def metrics(groups, model):
    losses = {name: [] for name in ("learned", "original_order", "native_baseline")}
    for group in groups:
        rows = group["candidates"]
        def score(row):
            return sum(w * (x - m) / s for w, x, m, s in zip(model["weights"], row["features"], model["means"], model["scales"]))
        choices = {"learned": max(rows, key=lambda row: (score(row), -row["original_order"])),
                   "original_order": min(rows, key=lambda row: row["original_order"]),
                   "native_baseline": next(row for row in rows if row["native_baseline"])}
        best = max(row["owner_cp"] for row in rows)
        for name, row in choices.items():
            losses[name].append(best - row["owner_cp"])
    return {name: {"mean_regret_cp": round(sum(values) / len(values), 5) if values else None,
                   "large_loss_150cp": sum(value >= 150 for value in values),
                   "worst_regret_cp": max(values, default=None)} for name, values in losses.items()}


def audit_study(run, queries, audit):
    curriculum, data = read(run / "curriculum.json"), read(run / "study/dataset.json")
    model, report = read(run / "study/model.json"), read(run / "study/study_report.json")
    audit.check(data["semantic_sha256"] == digest({k: v for k, v in data.items() if k != "semantic_sha256"}),
                "dataset_checksum", str(run))
    audit.check(data["curriculum_sha256"] == digest(curriculum), "curriculum_checksum", str(run))
    for name, expected in data["source_sha256"].items():
        audit.check(sha(ROOT / name) == expected, "study_dependency_unchanged", name)
    audit.check(data["engine_identity"] == read(run / "analysis/analysis_manifest.json")["identity"],
                "study_engine_identity", str(run))
    audit.check(model["model_id"] == digest({k: v for k, v in model.items() if k != "model_id"})
                and model["dataset_sha256"] == report["dataset_sha256"] == data["semantic_sha256"],
                "model_dataset_identity", str(run))
    case_map = {row["case_id"]: row for row in curriculum["cases"]}
    audit.check(data["case_count"] == len(case_map), "all_predeclared_study_cases_counted", str(run))
    footprints, family_splits = {}, {}
    for case in curriculum["cases"]:
        board = board_for(case["history_uci"])
        audit.check(case["fen"] in (board.fen(en_passant="legal"), board.fen(en_passant="fen"))
                    and case["position_key"] == key(board), "curriculum_replay", case["case_id"])
        values = {key(board)}
        for move in list(board.legal_moves):
            board.push(move)
            values.add(key(board))
            board.pop()
        footprints.setdefault(case["split"], set()).update(values)
        audit.check(case["family_id"] not in family_splits or family_splits[case["family_id"]] == case["split"],
                    "family_separation", case["case_id"])
        family_splits[case["family_id"]] = case["split"]
    for a in footprints:
        for b in footprints:
            if a < b:
                audit.check(not footprints[a] & footprints[b], "root_and_all_child_separation", a + ":" + b)
    label_count = 0
    for group in data["groups"]:
        case = case_map[group["case_id"]]
        audit.check(group["split"] == case["split"] and group["history_uci"] == case["history_uci"],
                    "training_case_identity", group["case_id"])
        baseline = queries[group["root_query_id"]]
        audit.check(group["native_baseline_uci"] == baseline["bestmove_uci"], "native_baseline", group["case_id"])
        audit.check(sum(row["native_baseline"] for row in group["candidates"]) == 1
                    and all(row["native_baseline"] == (row["uci"] == baseline["bestmove_uci"]) for row in group["candidates"]),
                    "native_baseline_candidate_identity", group["case_id"])
        for row in group["candidates"]:
            shallow, deep = [queries[identity] for identity in row["query_ids"]]
            audit.check(row["evidence"]["shallow"] == shallow and row["evidence"]["deep"] == deep,
                        "embedded_label_evidence_matches_final_ledger", row["query_ids"])
            expected = case["history_uci"] + [row["uci"]]
            audit.check(shallow["history_uci"] == deep["history_uci"] == expected, "label_full_history", row["query_ids"])
            budgets = data["budgets"]
            audit.check(shallow["requested_nodes"] == budgets["base_nodes"] and deep["requested_nodes"] == budgets["deep_nodes"],
                        "label_two_budgets", row["query_ids"])
            audit.check(shallow["status"] == deep["status"] == "complete"
                        and type(shallow["cp"]) is int and type(deep["cp"]) is int
                        and shallow["mate"] is deep["mate"] is None
                        and deep["depth"] >= shallow["depth"] >= budgets["minimum_depth"],
                        "stable_exact_label", row["query_ids"])
            delta = abs(shallow["cp"] - deep["cp"])
            audit.check(delta <= budgets["maximum_stability_delta_cp"] and row["stability_delta_cp"] == delta
                        and row["owner_cp"] == row["mover_cp"] == -deep["cp"], "label_pov_and_stability", row["query_ids"])
            label_count += 1
    covered = {row["case_id"] for row in data["groups"]} | {row["case_id"] for row in data["excluded"]}
    audit.check(covered == set(case_map), "all_predeclared_study_cases_reported", str(run))
    audit.check(model["status"] == "experimental" and model["active"] is False and report["active"] is False,
                "no_policy_activation", str(run))
    refit = None
    if model["trained"]:
        parts = {name: [row for row in data["groups"] if row["split"] == name] for name in ("train", "validation", "holdout")}
        choices = []
        for regularization in (.001, .01, .05, .2):
            fitted = independently_fit(parts["train"], data["seed"], regularization)
            metric = metrics(parts["validation"], fitted)["learned"]
            choices.append((metric["mean_regret_cp"], metric["large_loss_150cp"], regularization, fitted))
        chosen = min(choices, key=lambda row: row[:3])
        for name in ("weights", "means", "scales"):
            audit.check(model[name] == chosen[3][name], "independent_train_only_refit", name)
        audit.check(model["training"]["regularization"] == chosen[2], "validation_only_hyperparameter", str(run))
        for name in parts:
            report_key = "training" if name == "train" else name
            audit.check(report[report_key]["methods"] == metrics(parts[name], chosen[3]), "independent_ranking_metrics", name)
        gates = {}
        for split in ("validation", "holdout"):
            metric = metrics(parts[split], chosen[3])
            gates[split + "_coverage"] = len(parts[split]) >= 8 and len({group["family_id"] for group in parts[split]}) >= 2
            for baseline in ("original_order", "native_baseline"):
                gates[split + "_regret_vs_" + baseline] = metric["learned"]["mean_regret_cp"] <= metric[baseline]["mean_regret_cp"]
                gates[split + "_large_loss_vs_" + baseline] = metric["learned"]["large_loss_150cp"] <= metric[baseline]["large_loss_150cp"]
        audit.check(report["gates"] == gates and report["candidate_acceptance"] == all(gates.values()),
                    "independent_acceptance_gates", str(run))
        refit = {"weights_means_scales_match": True, "selection_used_only_validation": True,
                 "training_pairs": chosen[3]["training_pairs"], "regularization": chosen[2]}
    else:
        audit.check(report["candidate_acceptance"] is False and report["trained"] is False,
                    "untrained_model_not_accepted", str(run))
    return {"cases": len(curriculum["cases"]), "family_splits": family_splits,
            "groups": len(data["groups"]), "stable_labels": label_count,
            "split_counts": dict(Counter(row["split"] for row in data["groups"])),
            "excluded_records": len(data["excluded"]), "model_trained": model["trained"],
            "candidate_acceptance": report["candidate_acceptance"], "active": model["active"],
            "independent_refit": refit, "dataset_sha256": sha(run / "study/dataset.json")}


def opening_index():
    positions, edges = set(), set()
    source = read(ROOT / "data/openings/manifest.json")
    for name in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"):
        path = ROOT / "data/openings" / name
        if sha(path) != source["files"][name]["sha256"]:
            raise ValueError("Pinned opening source mismatch: " + name)
        for entry in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")), delimiter="\t"):
            game = chess.pgn.read_game(io.StringIO(entry["pgn"] + " *"))
            board = chess.Board()
            positions.add(key(board))
            for move in game.mainline_moves():
                edges.add((key(board), move.uci()))
                board.push(move)
                positions.add(key(board))
    return positions, edges


def independent_gate(probe, baseline, gates):
    if probe["status"] not in ("complete", "terminal") or baseline["status"] not in ("complete", "terminal"):
        return "unknown"
    own_mate, base_mate = probe.get("owner_mate"), baseline.get("owner_mate")
    if own_mate is not None:
        return "pass" if own_mate > 0 else "fail"
    if base_mate is not None and base_mate > 0:
        return "fail"
    value, reference = probe.get("owner_cp"), baseline.get("owner_cp")
    if type(value) is not int or type(reference) is not int:
        return "unknown"
    return "pass" if value >= gates["absolute_floor_cp"] and value >= reference - gates["baseline_max_loss_cp"] else "fail"


def audit_probe(probe, board, owner, queries, audit):
    if probe.get("query_id") is None:
        audit.check(probe["status"] != "complete", "probe_without_native_evidence", board.fen())
        return
    native = queries[probe["query_id"]]
    sign = 1 if board.turn == owner else -1
    audit.check(native["history_uci"] == [move.uci() for move in board.move_stack]
                and native["fen"] == board.fen(en_passant="fen"), "probe_native_history", probe["query_id"])
    audit.check(probe["cp"] == native["cp"] and probe["pv_uci"] == native["pv_uci"]
                and probe["owner_cp"] == (sign * native["cp"] if native["cp"] is not None else None),
                "probe_native_cp_pv_pov", probe["query_id"])
    if native.get("mate") not in (None, 0):
        audit.check(probe["owner_mate"] == sign * native["mate"], "probe_mate_pov", probe["query_id"])


def audit_discovery(run, queries, audit):
    results, summary = read(run / "discovery.json"), read(run / "summary.json")
    pre = read(run / "predeclared.json")
    curriculum = {case["case_id"]: case for case in read(run / "curriculum.json")["cases"]}
    audit.check([result["case_id"] for result in results] == pre["discovery_case_ids"],
                "predeclared_discovery_cases", str(run))
    positions, edges = opening_index()
    candidates = []
    for result in results:
        audit.check(result["status"] != "invalid", "valid_discovery_case", result["case_id"])
        if result["status"] == "invalid":
            continue
        board = board_for(result["history_uci"])
        owner = board.turn
        original_case = curriculum[result["case_id"]]
        audit.check(result["history_uci"] == original_case["history_uci"]
                    and result["owner"] == original_case["owner"] == original_case["turn"],
                    "discovery_curriculum_case_identity", result["case_id"])
        audit.check(not result["knowledge_manifest"]["reference_pgns"], "audit_reference_scope_supported", result["case_id"])
        audit_probe(result["baseline"], board, owner, queries, audit)
        for row in result["candidates"]:
            candidates.append((result, row))
            after = board.copy(stack=True)
            after.push_uci(row["uci"])
            novelty = row["novelty"]
            known_move, known_position = (key(board), row["uci"]) in edges, key(after) in positions
            expected = "known_transition" if known_move else "known_transposition" if known_position else "unseen_position"
            audit.check(novelty["classification"] == expected, "independent_catalog_novelty", result["case_id"] + ":" + row["uci"])
            audit.check(row["material_balance_before_cp"] == balance(board, owner)
                        and row["material_balance_after_offer_cp"] == balance(after, owner), "material_offer", row["uci"])
            if row.get("native_after_offer"):
                probe = row["native_after_offer"]
                audit_probe(probe, after, owner, queries, audit)
                audit.check(row["candidate_score_gate"] == independent_gate(probe, result["baseline"], result["gates"]),
                            "independent_candidate_gate", row["uci"])
            replies = row.get("replies", [])
            if row.get("all_replies_complete"):
                audit.check({item["uci"] for item in replies} == {move.uci() for move in after.legal_moves},
                            "all_immediate_defenses", row["uci"])
            if row["bounded_screen_passed"]:
                audit.check(row["status"] == "screened" and row["all_replies_complete"]
                            and row["candidate_score_gate"] == "pass"
                            and all(item["score_gate"] == "pass" for item in replies)
                            and row["persistent_material_investment_found"], "bounded_candidate_gate", row["uci"])
            for reply in replies:
                child = after.copy(stack=True)
                child.push_uci(reply["uci"])
                probe, evidence = reply["native"], reply.get("material_evidence")
                audit_probe(probe, child, owner, queries, audit)
                audit.check(reply["score_gate"] == independent_gate(probe, result["baseline"], result["gates"]),
                            "independent_defense_gate", reply["uci"])
                if evidence:
                    recovery = []
                    for move in child.legal_moves:
                        if child.is_capture(move):
                            recaptured = child.copy(stack=True)
                            recaptured.push(move)
                            if balance(recaptured, owner) >= balance(board, owner):
                                recovery.append(move.uci())
                    audit.check(sorted(recovery) == evidence["immediate_material_recovery_uci"],
                                "independent_immediate_recapture", reply["uci"])
                    trace = [balance(child, owner)]
                    for uci in probe["pv_uci"]:
                        child.push_uci(uci)
                        trace.append(balance(child, owner))
                    audit.check(trace == [item["material_balance_cp"] for item in evidence["material_trace"]],
                                "material_continuation_trace", reply["uci"])
                    if evidence["persistent_material_investment"]:
                        audit.check(all(value < balance(board, owner) for value in trace)
                                    and not evidence["immediate_material_recovery_uci"], "persistent_sacrifice", reply["uci"])
    games = []
    stream = io.StringIO((run / "discovery.pgn").read_text())
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        audit.check(not game.errors and game.headers["Result"] == "*", "research_pgn_legality", len(games))
        games.append(game)
    audit.check(len(games) == len(candidates), "research_pgn_count", str(run))
    for game, (result, row) in zip(games, candidates):
        actual = [move.uci() for move in game.mainline_moves()]
        expected = result["history_uci"] + [row["uci"]] + row.get("native_after_offer", {}).get("pv_uci", [])
        audit.check(actual == expected and game.headers.get("ResearchCase") == result["case_id"], "research_pgn_evidence", row["uci"])
    counts = summary["discovery"]
    audit.check(counts["executed_cases"] == counts["requested_cases"] == len(results)
                and counts["candidate_count"] == len(candidates), "discovery_summary_counts", str(run))
    audit.check(counts["bounded_screen_passed"] == sum(bool(row["bounded_screen_passed"]) for _, row in candidates)
                and counts["source_unseen_two_budget_candidates"] == sum(bool(row.get("two_budget_candidate")) for _, row in candidates),
                "discovery_screening_summary_counts", str(run))
    return {"cases": len(results), "invalid_cases": sum(result["status"] == "invalid" for result in results),
            "candidates": len(candidates), "candidate_statuses": dict(Counter(row["status"] for _, row in candidates)),
            "pgn_games": len(games), "bounded_screen_passed": counts["bounded_screen_passed"],
            "source_unseen_two_budget_candidates": counts["source_unseen_two_budget_candidates"],
            "soundness_proven": False, "world_first_claimed": False}


def excluded_previous_runs(directory, primary_runs):
    findings = []
    for run in sorted(Path(directory).glob("native-v*")):
        if run.resolve() in primary_runs:
            continue
        summary_path = run / "analysis/analysis_summary.json"
        journal = run / "analysis/queries.jsonl"
        if not summary_path.is_file() or not journal.is_file():
            findings.append({"run": run.name, "excluded_from_final_evidence": True, "reason": "incomplete_prior_run"})
            continue
        summary = read(summary_path)
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        ids = {row["query_id"] for row in rows}
        missing = ["q%06d" % i for i in range(1, summary["queries"] + 1) if "q%06d" % i not in ids]
        invalid = []
        if (run / "discovery.json").is_file():
            invalid = [{"case_id": result["case_id"], "reason": result.get("reason")}
                       for result in read(run / "discovery.json") if result["status"] == "invalid"]
        findings.append({"run": run.name, "excluded_from_final_evidence": True,
                         "reason": "superseded_by_complete_final_frozen_source_run",
                         "recorded_sources_unchanged": summary["sources_unchanged"],
                         "incremental_query_checkpoint_missing_ids": missing,
                         "invalid_discovery_cases": invalid,
                         "raw_uci_sha256": sha(run / "analysis/native-uci.jsonl"),
                         "original_files_modified_by_audit": False})
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-run", type=Path, required=True)
    parser.add_argument("--discovery-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("independent-audit.json"))
    args = parser.parse_args()
    audit = Audit()
    report = {"schema": "regicide.gambit-research-independent-audit.v1", "engine_started": False,
              "study_run": str(args.study_run), "discovery_run": str(args.discovery_run),
              "limitations": ["Native finite-depth labels are not objective chess truth",
                              "No Elo or equal-time completed-match evidence",
                              "Novelty checked against pinned named openings; additional PGNs require extending audit"]}
    report["excluded_previous_runs"] = excluded_previous_runs(args.study_run.parent,
                                    {args.study_run.resolve(), args.discovery_run.resolve()})
    try:
        required = {}
        for run, names in ((args.study_run, ["study/dataset.json", "study/model.json", "study/study_report.json", "analysis/analysis_summary.json"]),
                           (args.discovery_run, ["discovery.json", "discovery.pgn", "summary.json", "analysis/analysis_summary.json"])):
            required.setdefault(run, set()).update(names)
        missing = [str(run / name) for run, names in required.items() for name in names if not (run / name).is_file()]
        if missing:
            report.update(status="incomplete", passed=False, missing=missing)
        else:
            same_run = args.study_run.resolve() == args.discovery_run.resolve()
            if same_run:
                sq, study_queries = audit_queries(args.study_run, audit)
                dq, discovery_queries = sq, study_queries
            else:
                sq, study_queries = audit_queries(args.study_run, audit, allow_study_changes=True)
                dq, discovery_queries = audit_queries(args.discovery_run, audit)
            report["shared_query_run"] = same_run
            report["study_queries"], report["discovery_queries"] = study_queries, discovery_queries
            report["study"] = audit_study(args.study_run, sq, audit)
            report["discovery"] = audit_discovery(args.discovery_run, dq, audit)
            policy = sha(ROOT / "data/gambits/policy.json")
            audit.check(policy == POLICY_BASELINE, "existing_policy_matches_documented_baseline", policy)
            report["existing_policy_sha256"] = policy
            report.update(status="complete", passed=not audit.failures)
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
        report.update(status="audit_error", passed=False, error=type(error).__name__ + ": " + str(error))
    report["checks"] = dict(audit.checked)
    report["failures"] = audit.failures
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "passed": report["passed"], "failures": len(audit.failures)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
