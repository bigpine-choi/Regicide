"""Reproduce PR #20 capability and normal-search comparisons.

Usage: python tests/mate_performance.py BASELINE PROTOTYPE --output DIRECTORY
Requires the repository's test-only chess==1.11.2. No external chess engine.
"""
import argparse
import hashlib
import json
import platform
import random
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chess
from mate_oracle_tests import Uci, cases as regression_cases, distance

SEED = 0x20261104
BASE_SHA = "059f7f70ba25ff3c616b081c99a310e1adf9e583"
PROTO_SHA = "36d86679c77d02d95eaae66d53690b111ab043ba"


class TimedUci(Uci):
    def __init__(self, binary):
        self.receipts = []
        super().__init__(binary)

    def reader(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            self.receipts.append((time.perf_counter_ns(), line))
            self.queue.put(line)
        self.queue.put(None)


def evidence_fields(value):
    if isinstance(value, list):
        return [evidence_fields(v) for v in value]
    if isinstance(value, dict):
        result = {k: evidence_fields(v) for k, v in value.items()
                  if k not in ("lines", "mate_report_times")}
        if value.get("mate_report_times"):
            result["first_mate_report"] = value["mate_report_times"][0]
        return result
    return value


def median_or_none(values):
    values = list(values)
    return statistics.median(values) if values else None


def save(path, data):
    path.write_text(json.dumps(evidence_fields(data), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def position(board):
    result = "position fen " + board.root().fen(en_passant="fen")
    if board.move_stack:
        result += " moves " + " ".join(m.uci() for m in board.move_stack)
    return result


def query(engine, board, command):
    engine.send("ucinewgame")
    engine.send("isready")
    engine.until("readyok")
    engine.send(position(board))
    receipt_start = len(engine.receipts)
    started = time.perf_counter_ns()
    engine.send(command)
    lines = engine.until("bestmove ", timeout=90)
    elapsed = (time.perf_counter_ns() - started) / 1e6
    info = [line for line in lines if line.startswith("info depth ") and " score " in line]
    result = {"bestmove": lines[-1].split()[1], "score_type": None, "score": None,
              "depth": None, "nodes": None, "pv": []}
    if info:
        tok = info[-1].split()
        for field in ("depth", "nodes"):
            result[field] = int(tok[tok.index(field) + 1])
        at = tok.index("score")
        result["score_type"], result["score"] = tok[at + 1], int(tok[at + 2])
        result["pv"] = tok[tok.index("pv")+1:] if "pv" in tok else []
    summaries = [line for line in lines if line.startswith("info string mate_status ")]
    if summaries:
        tok = summaries[-1].split()
        for field in ("mate_status", "reason", "scope"):
            result[field] = tok[tok.index(field)+1]
        for field in ("completed", "nodes", "time"):
            result[field] = int(tok[tok.index(field)+1])
    reports = []
    for received, line in engine.receipts[receipt_start:]:
        match = re.search(r"\bscore mate (\d+)\b", line)
        if match and " pv " in line:
            reports.append({"score": int(match.group(1)),
                            "wall_ms": round((received-started)/1e6, 3),
                            "pv": line.split(" pv ", 1)[1].split()})
    return {"result": result, "wall_ms": round(elapsed, 3), "lines": lines,
            "command": command, "mate_report_times": reports}


def new_cases():
    rng = random.Random(SEED)
    counts = {1: 0, 2: 0, None: 0}
    target = {1: 4, 2: 4, None: 4}
    result = []
    attempts = 0
    while counts != target:
        attempts += 1
        assert attempts < 50000
        board = chess.Board.empty()
        bk = rng.choice([chess.A1, chess.H1, chess.A8, chess.H8])
        wk = rng.choice([sq for sq in chess.SQUARES if 2 <= chess.square_distance(sq, bk) <= 3])
        major = rng.choice([sq for sq in chess.SQUARES if sq not in (bk, wk)])
        board.set_piece_at(bk, chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(wk, chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(major, chess.Piece(rng.choice([chess.ROOK, chess.QUEEN]), chess.WHITE))
        if not board.is_valid() or board.outcome(claim_draw=False) is not None:
            continue
        expected = distance(board, 2)
        if counts[expected] < target[expected]:
            counts[expected] += 1
            result.append(("new_seed_%s_%d" % (expected, counts[expected]), board, 2))
    return result


def validate_move(board, bound, bestmove, allowed=None):
    if bestmove == "0000":
        return False
    move = chess.Move.from_uci(bestmove)
    assert move in board.legal_moves, (board.fen(), bestmove)
    if allowed is not None:
        assert bestmove in allowed
    return distance(board, bound, [bestmove]) is not None


def replay_pv(board, pv):
    witness = board.copy(stack=True)
    for token in pv:
        if witness.outcome(claim_draw=False) is not None:
            return {"legal": False, "checkmate_end": False}
        move = chess.Move.from_uci(token)
        if move not in witness.legal_moves:
            return {"legal": False, "checkmate_end": False}
        witness.push(move)
    return {"legal": True, "checkmate_end": witness.is_checkmate() and witness.turn != board.turn}


def capabilities(engines, output):
    corpus = regression_cases() + new_cases()
    records = []
    for number, case in enumerate(corpus):
        label, board, bound = case[:3]
        allowed = case[3] if len(case) == 4 else None
        expected = distance(board, bound, allowed)
        record = {"label": label, "position": position(board), "bound": bound,
                  "searchmoves": allowed, "expected_mate": expected,
                  "new_seed_case": label.startswith("new_seed_"), "budgets": {}}
        for budget, commands in (
            ("nodes_50000", ("go nodes 50000", "go mate %d nodes 50000" % bound)),
            ("movetime_100ms", ("go movetime 100", "go mate %d movetime 100 nodes 1000000000" % bound)),
        ):
            pair = {}
            for index in ((0, 1) if number % 2 == 0 else (1, 0)):
                command = commands[index]
                if allowed:
                    command += " searchmoves " + " ".join(allowed)
                row = query(engines[index], board, command)
                result = row["result"]
                row["bestmove_forces_within_bound"] = validate_move(board, bound, result["bestmove"], allowed) if expected is not None else False
                row["pv_replay"] = replay_pv(board, result["pv"])
                report = result["score_type"] == "mate" and 0 < result["score"] <= bound
                row["positive_mate_report_within_bound"] = report
                # A PV alone is NOT a proof. Independently solve its first move.
                row["verified_positive_report"] = bool(report and row["bestmove_forces_within_bound"])
                row["first_verified_mate_report_ms"] = None
                for report_event in row["mate_report_times"]:
                    if expected is not None and 0 < report_event["score"] <= bound:
                        if validate_move(board, bound, report_event["pv"][0], allowed):
                            row["first_verified_mate_report_ms"] = report_event["wall_ms"]
                            break
                if index == 1:
                    state = result["mate_status"]
                    if state == "proven":
                        assert result["score"] == expected
                        assert row["bestmove_forces_within_bound"]
                        assert row["pv_replay"] == {"legal": True, "checkmate_end": True}
                        assert len(result["pv"]) == 2*expected-1
                    elif state == "no_mate_within_bound":
                        assert expected is None
                    else:
                        assert state == "unknown"
                pair["normal_baseline" if index == 0 else "mate_prototype"] = row
            record["budgets"][budget] = pair
        records.append(record)
        save(output / "capabilities.json", {"seed": SEED, "positions": records})
        print("CAPABILITY", number+1, label, "oracle", expected, flush=True)
    summary = {}
    for budget in ("nodes_50000", "movetime_100ms"):
        summary[budget] = {}
        for mode in ("normal_baseline", "mate_prototype"):
            rows = [r["budgets"][budget][mode] for r in records]
            positive = [row for r, row in zip(records, rows) if r["expected_mate"] is not None]
            summary[budget][mode] = {
                "positions": len(rows),
                "oracle_positive": len(positive),
                "correct_mating_first_moves": sum(r["bestmove_forces_within_bound"] for r in positive),
                "verified_positive_mate_reports": sum(r["verified_positive_report"] for r in positive),
                "unverified_positive_mate_reports": sum(r["positive_mate_report_within_bound"] and not r["verified_positive_report"] for r in rows),
                "statuses": {s: sum(r["result"].get("mate_status") == s for r in rows) for s in ("proven", "no_mate_within_bound", "unknown")},
                "first_verified_mate_report_median_ms": median_or_none(
                    r["first_verified_mate_report_ms"] for r in positive
                    if r["first_verified_mate_report_ms"] is not None),
                "new_seed_positive_correct": sum(r["budgets"][budget][mode]["bestmove_forces_within_bound"] for r in records if r["new_seed_case"] and r["expected_mate"] is not None),
                "new_seed_positive_count": sum(r["new_seed_case"] and r["expected_mate"] is not None for r in records),
                "positive_total_wall_ms": round(sum(r["wall_ms"] for r in positive), 3),
                "positive_median_wall_ms": round(statistics.median(r["wall_ms"] for r in positive), 3),
            }
    save(output / "capabilities-summary.json", summary)
    print("CAPABILITY_SUMMARY", json.dumps(summary), flush=True)
    return summary


def normal_positions():
    rng = random.Random(SEED + 1)
    results = []
    for game in range(10):
        board = chess.Board()
        for ply in range(1, 41):
            if board.outcome(claim_draw=True) is not None:
                break
            board.push(rng.choice(sorted(board.legal_moves, key=lambda m: m.uci())))
            if ply in (10, 20, 30, 40):
                results.append(("random_%d_ply_%d" % (game, ply), board.copy(stack=True)))
    return results


def normal_comparison(engines, output):
    records = []
    corpus = normal_positions()
    for engine in engines:
        query(engine, chess.Board(), "go nodes 10000")  # one excluded warmup
    for number, (label, board) in enumerate(corpus):
        repeats = []
        for repeat in range(3):
            pair = {}
            for index in ((0, 1) if (number+repeat) % 2 == 0 else (1, 0)):
                pair["baseline" if index == 0 else "prototype"] = query(engines[index], board, "go nodes 10000")
            pair["equal"] = pair["baseline"]["result"] == pair["prototype"]["result"]
            assert pair["equal"], pair
            repeats.append(pair)
        records.append({"label": label, "position": position(board), "repeats": repeats})
        save(output / "normal-search.json", {"seed": SEED+1, "positions": records})
        print("NORMAL", number+1, label, "all_equal", flush=True)
    return summarize_normal(records, output)


def summarize_normal(records, output):
    # Aggregate three repetitions per position before comparing. Sample positions
    # and cluster by game in the bootstrap below.
    ratios = []
    totals = {name: 0.0 for name in ("baseline", "prototype")}
    for record in records:
        median = {name: statistics.median(pair[name]["wall_ms"] for pair in record["repeats"]) for name in totals}
        ratios.append(median["prototype"]/median["baseline"])
        for name in totals:
            totals[name] += median[name]
    # Four positions from one playout are correlated: resample entire games.
    groups = {}
    for record, ratio in zip(records, ratios):
        groups.setdefault(record["label"].split("_")[1], []).append(ratio)
    rng = random.Random(SEED + 2)
    bootstrap = sorted(statistics.median([
        ratio for group in rng.choices(list(groups.values()), k=len(groups))
        for ratio in group]) for _ in range(5000))
    summary = {"positions": len(records), "paired_runs": 3*len(records), "equal_pairs": sum(pair["equal"] for record in records for pair in record["repeats"]),
        "nodes_per_search": 10000, "sum_per_position_median_ms": totals,
        "median_position_time_ratio": statistics.median(ratios),
        "bootstrap_unit": "game", "independent_playouts": len(groups),
        "bootstrap_95_percentile_median_ratio": [bootstrap[124], bootstrap[4874]],
        "warning": "Local diagnostic timing with uncontrolled OS/thermal load. No Elo inference; interval excludes neither all systematic bias nor other settings."}
    save(output / "normal-summary.json", summary)
    print("NORMAL_SUMMARY", json.dumps(summary), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("prototype")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--part", choices=("all", "capabilities", "normal", "summarize"), default="all")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.part == "summarize":
        summarize_normal(json.loads((args.output / "normal-search.json").read_text(encoding="utf-8"))["positions"], args.output)
        return
    binaries = [str(Path(b).resolve()) for b in (args.baseline, args.prototype)]
    save(args.output / "environment.json", {
        "baseline_commit": BASE_SHA, "prototype_commit": PROTO_SHA,
        "baseline_adjustment": "Both builds use the identical indexed SEE constexpr portability loop.",
        "binary_sha256": {name: hashlib.sha256(Path(b).read_bytes()).hexdigest() for name, b in zip(("baseline", "prototype"), binaries)},
        "platform": platform.platform(), "python": platform.python_version(), "chess": chess.__version__,
        "seed": SEED, "settings": "UCI defaults; Move Overhead 0; ucinewgame before every query; no concurrent searches",
        "corpus_note": "27 existing regression cases plus 12 freshly seeded KQK/KRK cases; narrow constructed sample, no general strength claim.",
        "prototype_goal": "Bounded proof and correct status, not necessarily fastest mating move selection."
    })
    engines = [TimedUci(b) for b in binaries]
    try:
        for engine in engines:
            engine.send("setoption name Move Overhead value 0")
            engine.send("isready")
            engine.until("readyok")
        if args.part in ("all", "capabilities"):
            capabilities(engines, args.output)
        if args.part in ("all", "normal"):
            normal_comparison(engines, args.output)
    finally:
        for engine in engines:
            engine.close()


if __name__ == "__main__":
    main()
