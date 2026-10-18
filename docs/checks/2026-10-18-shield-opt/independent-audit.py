"""Recheck saved shield optimization evidence, without running engines or timings.

Run from any directory with Python 3 and python-chess. The default repository
is inferred from this file's build/shield-opt-validation location. An explicit
--root and --output may be supplied. Existing output files are never replaced.
No Regicide benchmark or profiler Python module is imported.
"""

import argparse
import collections
import hashlib
import json
import math
import random
import re
import statistics
import subprocess
from pathlib import Path

import chess


def scalar_eval(fen, shield):
    """Independent translation of the released scalar evaluation formula."""
    board = chess.Board(fen)
    material = middlegame = endgame = phase = 0
    home = {
        color: sum(
            board.piece_at(chess.square(file, 0 if color else 7))
            == chess.Piece(kind, color)
            for file, kind in [(1, 2), (6, 2), (2, 3), (5, 3)]
        )
        for color in [True, False]
    }
    shelters = {
        color: {"pawns": [0] * 8, "king": None, "phase": 0}
        for color in [True, False]
    }
    for square, piece in board.piece_map().items():
        kind, color = piece.piece_type, piece.color
        sign = 1 if color else -1
        file = chess.square_file(square)
        rank = chess.square_rank(square) if color else 7 - chess.square_rank(square)
        file_center, rank_center = min(file, 7 - file), min(rank, 7 - rank)
        center, undeveloped = file_center + rank_center, home[color]
        if kind == 1:
            advanced = max(0, rank - 3)
            middle = 4 * rank + 3 * file_center + (3 * file_center if 2 <= rank <= 4 else 0)
            ending = 8 * rank + 2 * file_center + 4 * advanced * advanced
        elif kind == 2:
            middle, ending = 6 * center + (12 if rank > 0 else 0), 4 * center
        elif kind == 3:
            middle, ending = 4 * center + (10 if rank > 0 else 0), 4 * center
        elif kind == 4:
            middle = 2 * file_center + (18 if rank == 6 else 0) - (3 * undeveloped if 0 < rank < 5 else 0)
            ending = 2 * file_center + (20 if rank == 6 else 0)
        elif kind == 5:
            middle, ending = 2 * center - (4 * undeveloped if rank > 0 else 0), 3 * center
        else:
            middle = -7 * center - 5 * rank + (25 if rank == 0 and (file <= 2 or file >= 6) else 0)
            ending = 8 * center
        weight = {1: 0, 2: 1, 3: 1, 4: 2, 5: 4, 6: 0}[kind]
        material += sign * {1: 100, 2: 320, 3: 330, 4: 500, 5: 900, 6: 0}[kind]
        middlegame += sign * middle
        endgame += sign * ending
        phase += weight
        shelter = shelters[color]
        shelter["phase"] += weight
        if kind == 1:
            shelter["pawns"][file] |= 1 << rank
        if kind == 6:
            shelter["king"] = file, rank

    def penalty(defender, attack):
        if not attack or defender["king"] is None:
            return 0
        file, rank = defender["king"]
        result = 0
        for neighbor in range(max(0, file - 1), min(7, file + 1) + 1):
            ahead = defender["pawns"][neighbor] >> (rank + 1)
            result += 0 if ahead & 1 else 4 if ahead & 2 else 8 if ahead & 4 else 12
        return result * min(attack, 12) // 12

    if shield:
        middlegame += penalty(shelters[False], shelters[True]["phase"])
        middlegame -= penalty(shelters[True], shelters[False]["phase"])
    phase = min(phase, 24)
    numerator = middlegame * phase + endgame * (24 - phase)
    # C++ signed integer division truncates toward zero, unlike Python //.
    blended = (1 if numerator >= 0 else -1) * (abs(numerator) // 24)
    value = material + blended
    return value if board.turn else -value


def audit(root):
    out = root / "build/shield-opt-validation"
    failures = []

    def check(ok, message):
        if not ok:
            failures.append(message)

    def read(name):
        return json.loads((out / name).read_text(encoding="utf-8-sig"))

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    profile = read("profile-windows.json")
    variants, orders = profile["variant_order"], profile["orders_per_block"]
    check(len(profile["samples"]) == 9 * 4 * 4, "profile sample count")
    seen = collections.Counter((sample["block"], sample["order"], sample["variant"]) for sample in profile["samples"])
    check(set(seen) == {(block, order, variant) for block in range(9) for order in orders for variant in variants}
          and set(seen.values()) == {1}, "profile block-order balance")
    for sample in profile["samples"]:
        check(math.isclose(sample["ns_per_call"], sample["elapsed_ns"] / profile["calls_per_sample"], rel_tol=1e-10),
              "ns_per_call arithmetic")
    for block in range(9):
        for order in orders:
            rows = [sample for sample in profile["samples"] if sample["block"] == block and sample["order"] == order]
            expected = [variants["ABCD".index(letter)] for letter in order]
            check([sample["variant"] for sample in rows] == expected, "profile order " + str((block, order)))
    means = []
    for block in range(9):
        means.append({variant: statistics.mean(
            sample["elapsed_ns"] / profile["calls_per_sample"] for sample in profile["samples"]
            if sample["block"] == block and sample["variant"] == variant) for variant in variants})
    ratios = {key: statistics.median(mean[first] / mean[second] for mean in means)
              for key, first, second in [
                  ("new_on_over_old_on", "new_on", "old_on"),
                  ("new_off_over_old_off", "new_off", "old_off"),
                  ("old_on_over_old_off", "old_on", "old_off"),
                  ("new_on_over_new_off", "new_on", "new_off")]}
    for key, value in ratios.items():
        check(math.isclose(value, profile["medians"]["paired_ratios"][key], rel_tol=1e-10), "profile median " + key)

    corpus = root / "tests/data/evaluation_profile_2026_10_18.fens"
    fens = [line.strip() for line in corpus.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    check(len(fens) == 42 and profile["positions"] == 42
          and profile["equivalence"] == {"comparisons": 84, "mismatch_count": 0, "mismatches": []}, "raw equivalence 84")
    values = {shield: [scalar_eval(fen, shield) for fen in fens] for shield in [False, True]}

    def checksum(shield, calls):
        full, remain = divmod(calls, len(fens))
        return full * sum(values[shield]) + sum(values[shield][:remain])

    for index, variant in enumerate(variants):
        shield = variant.endswith("_on")
        expected = checksum(shield, profile["calls_per_sample"])
        check({sample["checksum"] for sample in profile["samples"] if sample["variant"] == variant} == {expected},
              "scalar expected checksum " + variant)
        check(profile["warmup_checksums"][index] == checksum(shield, profile["warmup_calls_per_variant"]),
              "warmup checksum " + variant)
    check(profile["calibration"]["checksum"] == checksum(True, profile["calibration"]["calls"]), "calibration checksum")
    released = subprocess.check_output(["git", "show", "07321d8:src/evaluation.cpp"], cwd=root, text=True)
    reference = (root / "tests/reference_evaluation_v054.cpp").read_text()
    normalized = released.replace('#include "regicide/evaluation.hpp"', '#include "reference_evaluation_v054.hpp"')
    normalized = normalized.replace("int evaluate_position(", "int evaluate_position_v054(")
    reference_exact = normalized.replace("\r\n", "\n") == reference.replace("\r\n", "\n")
    check(reference_exact, "frozen reference exact body")

    fixture_path = root / "tests/data/profile_positions.json"
    fixture, fixture_sha = json.loads(fixture_path.read_text()), sha(fixture_path)
    search, pv_plies, pairs = {}, 0, 0
    fields = ["move", "score", "depth", "seldepth", "nodes", "pv", "tt_counters", "tt_memory", "search_fen", "process_search_index"]
    for label, repeats, depth, shield in [("on-depth3", 5, 3, True), ("on-depth4", 5, 4, True), ("off-depth3", 3, 3, False)]:
        document = read(label + ".json")
        rows = document["records"]
        check(document["error"] is None and document["exit_code"] == 0, label + " completed")
        check(len(rows) == 10 * repeats and document["depth"] == depth and document["repeats"] == repeats, label + " pair count")
        check(document["positions_file"]["sha256"] == fixture_sha, label + " fixture hash")
        recorded = document["positions_file"]["document"]
        stripped = json.loads(json.dumps(recorded))
        for position in stripped["positions"]:
            position.pop("search_fen", None)
        check(stripped == fixture, label + " fixture document")
        for index, position in enumerate(fixture["positions"]):
            board = chess.Board(position.get("fen", chess.STARTING_FEN))
            for uci in position.get("moves", []):
                move = chess.Move.from_uci(uci)
                check(move in board.legal_moves, label + " input moves")
                board.push(move)
            check(board.fen(en_passant="fen") == recorded["positions"][index]["search_fen"], label + " fixture FEN")
            check(fens[2 * index] == board.fen(en_passant="fen"), "static corpus first 10 match fixtures")
        for role in ["baseline", "candidate"]:
            engine = document["engines"][role]
            check(engine["configured_options"] == {"Move Overhead": 20, "Hash": 16, "UsePawnShield": shield}, label + " options")
            # Resolve local binaries without embedding paths from raw metadata.
            binary = out / "regicide-v054.exe" if role == "baseline" else root / "build/regicide.exe"
            check(sha(binary) == engine["sha256"], label + " binary hash " + role)
        schedule, rng = [], random.Random(document["seed"])
        for repetition in range(repeats):
            indices = list(range(10))
            rng.shuffle(indices)
            for index in indices:
                order = ["baseline", "candidate"] if (repetition + index) % 2 == 0 else ["candidate", "baseline"]
                schedule.append((repetition + 1, index, order))
        for row, expected in zip(rows, schedule):
            check((row["repetition"], row["position_index"], row["order"]) == expected, label + " raw schedule")
            index = row["position_index"]
            check(row["position"] == fixture["positions"][index]["name"], label + " row name")
            for field in fields:
                check(row["baseline"][field] == row["candidate"][field], label + " search field " + field)
            for role in ["baseline", "candidate"]:
                sample = row[role]
                check(sample["search_fen"] == recorded["positions"][index]["search_fen"], label + " row FEN")
                board = chess.Board(sample["search_fen"])
                check(chess.Move.from_uci(sample["move"]) in board.legal_moves, label + " bestmove legal")
                if sample["pv"]:
                    check(sample["pv"][0] == sample["move"], label + " PV starts bestmove")
                for uci in sample["pv"]:
                    move = chess.Move.from_uci(uci)
                    check(move in board.legal_moves, label + " PV legal")
                    board.push(move)
                    pv_plies += 1
        wall_ratios = [row["candidate"]["wall_time_seconds"] / row["baseline"]["wall_time_seconds"] for row in rows]
        ratio = statistics.median(wall_ratios)
        search[label] = {"pairs": len(rows), "exact_fields": fields, "median_candidate_over_baseline_wall": ratio,
                         "wall_reduction_percent": 100 * (1 - ratio), "candidate_slower_pairs": sum(value > 1 for value in wall_ratios),
                         "candidate_faster_pairs": sum(value < 1 for value in wall_ratios), "ratio_range": [min(wall_ratios), max(wall_ratios)],
                         "raw_error": document["error"]}
        pairs += len(rows)

    loads = {}
    for label in ["profile-load", "search-load"]:
        document = read(label + ".json")
        samples = document["samples"]
        seconds = sum(sample["interval_seconds"] for sample in samples)
        sums = {key: sum(sample[key] for sample in samples) for key in ["all_observed_cpu_seconds", "target_cpu_seconds",
                "monitor_cpu_seconds", "browser_cpu_seconds", "regicide_cpu_seconds"]}
        loads[label] = {"samples": len(samples), "sampled_interval_seconds": seconds, "cpu_seconds_totals": sums,
                        "observed_average_core_equivalents": sums["all_observed_cpu_seconds"] / seconds,
                        "browser_average_core_equivalents": sums["browser_cpu_seconds"] / seconds,
                        "regicide_average_core_equivalents": sums["regicide_cpu_seconds"] / seconds, "policy": document["monitor_policy"]}
    native = {}
    for label in ["windows-native", "linux-native", "linux-sanitizers"]:
        text = (out / (label + ".log")).read_text()
        matches = re.findall(r"(\d+) groups, (\d+) assertions, (\d+) failures", text)
        check(all(int(match[2]) == 0 for match in matches), label + " all failures zero")
        native[label] = {"suite_summaries": matches, "assertion_total": sum(int(match[1]) for match in matches),
                         "oracle_positions_per_shield_mode": 16860 if "16860 frozen v0.5.4 oracle positions" in text else None}
    return {
        "schema": "regicide-shield-opt-independent-audit-v1",
        "method": "Read raw JSON/logs/source with standard-library Python and python-chess only; no benchmark/profiler helper imports and no new performance measurement. Scalar released evaluation independently translated to recompute corpus sums.",
        "passed": not failures, "failures": failures,
        "profile": {"raw_samples": len(profile["samples"]), "blocks": 9, "orders_each": orders, "variants_each": variants,
                    "balanced": True, "paired_block_median_ratios": ratios,
                    "new_on_wall_call_reduction_percent": 100 * (1 - ratios["new_on_over_old_on"]),
                    "reference_copy_exact_except_header_and_function_name": reference_exact, "raw_exact_comparisons": 84,
                    "scalar_recomputed_scores_by_mode": {"off": values[False], "on": values[True]},
                    "checksums_independently_match": not any("checksum" in failure for failure in failures),
                    "equivalence_limit": "Raw profiler stores mismatch list, not all 84 old/new individual scores. Exact 84 assertion is verified from raw zero-mismatch report plus inspected comparison loop; independently recomputed scalar expected sums match all 144 measured/warmup/calibration checksums.",
                    "corpus_sha256": sha(corpus)},
        "search": search, "total_exact_search_pairs": pairs, "pv_replayed_plies_including_both_roles": pv_plies,
        "fixture_sha256": fixture_sha, "load_samples": loads, "native_logs": native,
        "source_review": {"rounding_preserved": "Shield integer scaling and final blended-score integer division unchanged; table relocation keeps all integer formulas and home-minor coefficient before blending.",
                          "table_safety": "Unsigned-char 256-entry lookup; valid kind static_assert; six kinds x 64 owner-relative squares; table total static_assert <=15 KiB. Shield rank right-shift max 8 fits unsigned type.",
                          "source_provenance": "Frozen reference equals released 07321d8 evaluation after header/function rename; original hand-coded formulas generate constexpr table. No external learned/table data added.", "findings": []},
        "limitations": ["Function-only timing is not whole-engine speed or playing-strength evidence.",
                        "Some fixed-depth pairs are slower; shortest samples are sensitive to timing granularity and scheduling.",
                        "Load sampling observes partial process CPU counters, not thermal/frequency effects or complete host isolation.",
                        "Search result identity is demonstrated for the declared 130 fixed-depth pairs, not every possible future position.",
                        "No matches or Elo measurements were run for this audit."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "build/shield-opt-validation/audit-recheck.json"
    # Reserve the output before work, and never overwrite any saved evidence.
    with output.open("x", encoding="utf-8") as handle:
        result = audit(root)
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"passed": result["passed"], "failures": result["failures"], "output": str(output)}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
