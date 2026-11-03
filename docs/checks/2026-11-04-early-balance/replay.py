"""Replay the recorded early-balance failures; incumbents remain fixed evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import chess
from runtime_engine import resolve_runtime_engine
from sparring_quality_native_integration import run_case, sha
import sparring_play
import pending_exchange


def hashes():
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted((ROOT / "scripts").glob("*.py"))}


def cases():
    old = ROOT / "docs/matches/2026-11-04-sparring-quality-v2"
    new = ROOT / "docs/matches/2026-11-04-early-balance-v3"
    for base, game, plies in [(old, "1700-white", (29, 31, 33)), (old, "1700-black", (8, 10, 12)),
                              (old, "1700-white-extra", (25,)),
                              (new, "pair2-1700-white", (31, 33)), (new, "pair2-1700-black", (28, 36)),
                              (new, "pair3-1700-white", (29,))]:
        path = base / game / "game.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        for ply in plies:
            board = chess.Board(saved["initial_fen"])
            for uci in saved["uci_moves"][:ply - 1]:
                board.push_uci(uci)
            row = saved["moves"][ply - 1]
            assert row["actor"] == "sparring" and board.fen(en_passant="fen") == row["before_fen"]
            yield {"id": game + "-" + str(ply), "board": board, "original_uci": row["uci"],
                   "recorded": {"san": row["san"], "quality": row["sparring_decision"].get("quality_guard")},
                   "evidence_sha256": {path.relative_to(ROOT).as_posix(): sha(path)}}
            if game == "pair2-1700-white" and ply == 31:
                incumbent = row["sparring_decision"]["quality_guard"]["original_uci"]
                yield {"id": game + "-31-prequality", "board": board.copy(stack=True),
                       "original_uci": incumbent,
                       "recorded": {"actual_selected": row["uci"], "quality_input": incumbent},
                       "evidence_sha256": {path.relative_to(ROOT).as_posix(): sha(path)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--audit-ms", type=int, default=3000)
    args = parser.parse_args()
    binary = resolve_runtime_engine(ROOT)
    sf = ROOT / "build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe"
    before = hashes()
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "source_sha256": before,
              "native_sha256": sha(binary), "stockfish_sha256": sha(sf), "cases": [],
              "scope": "fixed recorded incumbent; actual native alternatives; separate fresh Stockfish audit"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases():
        if args.case and case["id"] not in args.case:
            continue
        row = run_case(case, binary, sf, args.audit_ms)
        root = chess.Board(row["root_fen"])
        for uci in row["history_uci"]:
            root.push_uci(uci)
        natural = sparring_play.naturalness(root, chess.Move.from_uci(row["selected_uci"]))
        contextual = pending_exchange.naturalness(root, chess.Move.from_uci(row["selected_uci"]))
        audits = row["independent_audit"]
        selected = (audits["selected_child"]["completed"] or {})
        cp = selected.get("cp_own")
        row["early_balance_verification"] = {
            "inside_close_band": type(cp) is int and abs(cp) <= 100,
            "cp_own": cp, "mate_own": selected.get("mate_own"),
            "strict_naturalness": natural,
            "contextual_naturalness": contextual,
            "native_only": row["native_only_replacement"],
            "runtime_status": row["receipt"]["status"],
            "runtime_balance": row["receipt"].get("balance"),
        }
        report["cases"].append(row)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"id": row["id"], "original": row["original_uci"], "selected": row["selected_uci"],
                          "cp": cp, "mate": selected.get("mate_own"),
                          "natural": contextual["admitted"], "pending_exchange": bool(contextual.get("pending_exchange")),
                          "status": row["receipt"]["status"]}), flush=True)
    report.update(source_sha256_after=hashes(), sources_unchanged=before == hashes(),
                  binaries_unchanged=sha(binary) == report["native_sha256"] and sha(sf) == report["stockfish_sha256"],
                  finished_utc=datetime.now(timezone.utc).isoformat())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    assert report["sources_unchanged"] and report["binaries_unchanged"]


if __name__ == "__main__":
    main()
