"""Revisit declared historical positions with the live own-engine controller.

These are counterfactual single decisions, not new games or playing strength.
The original record and its observations stay immutable; all fresh searches
use Regicide, never the opponent's score. Run before freezing a match version.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import zipfile

import chess
import chess.engine

from boss_strength_match import native_engine, sha, utc
from play_game import source_version
import sparring_play

ROOT = Path(__file__).resolve().parents[1]
CASES = (("ratings-1320-1700/game-001.json", 17),
         ("ratings-1320-1700/game-003.json", 37),
         ("ratings-2100-2500/game-005.json", 45),
         ("ratings-2100-2500/game-007.json", 59),
         ("ratings-1320-1700/game-002.json", 18),
         ("ratings-1320-1700/game-001.json", 19))


def run(binary, archive, movetime_ms):
    cases = []
    tracked = ("scripts/check_sparring_band.py", "scripts/sparring_play.py",
               "scripts/boss_play.py", "scripts/evaluation_stability.py",
               "scripts/boss_strength_match.py", "scripts/opening_variety.py", "scripts/sparring_pacing.py")
    before_hashes = {name: sha(ROOT / name) for name in tracked}
    before_binary, before_archive = sha(binary), sha(archive)
    with zipfile.ZipFile(archive) as records:
        for record_name, ply in CASES:
            raw = records.read(record_name)
            record = json.loads(raw)
            previous = record["moves"][ply - 1]
            assert previous["actor"] == "sparring" and previous["ply"] == ply
            board = chess.Board()
            for uci in record["uci_moves"][:ply - 1]:
                board.push_uci(uci)
            assert board.fen(en_passant="fen") == previous["before_fen"]
            old = previous["sparring_decision"]
            past = [row["sparring_decision"] for row in record["moves"][:ply - 1]
                    if row["actor"] == "sparring"]
            with native_engine(binary, boss=True) as engine:
                options = {name: value for name, value in
                           {"UsePieceActivity": True, "Threads": 1, "Hash": 16}.items()
                           if name in engine.options}
                engine.configure(options)

                def search(budget, *, root_move=None, root_moves=None,
                           position=None, depth=None, publish=False, fresh=True):
                    current = position if position is not None else board
                    request = {"game": object(), "info": chess.engine.INFO_ALL}
                    if root_move is not None:
                        request["root_moves"] = [root_move]
                    elif root_moves is not None:
                        request["root_moves"] = root_moves
                    packets, latest = {}, {}
                    with engine.analysis(current.copy(stack=True),
                                         chess.engine.Limit(time=budget / 1000, depth=depth),
                                         **request) as analysis:
                        for info in analysis:
                            if type(info.get("depth")) is int and "score" in info:
                                latest = dict(info)
                                packets[info["depth"]] = latest
                        reply = analysis.wait()
                    return reply, latest, packets

                started = time.monotonic()
                baseline, final, packets = search(movetime_ms)
                baseline_ms = (time.monotonic() - started) * 1000
                decision, discard = sparring_play.choose(
                    board, baseline.move, final, packets, search, engine.close,
                    decisions=past, seed=record["sparring_seed"], movetime_ms=movetime_ms)
                assert chess.Move.from_uci(decision["selected_uci"]) in board.legal_moves
                assert decision["before_fen"] == previous["before_fen"]
                assert decision["history_uci"] == record["uci_moves"][:ply - 1]
                selected = (decision.get("candidate_compared")
                            if decision["selected_uci"] != decision["baseline_uci"]
                            else decision.get("baseline"))
                cp = (selected or {}).get("cp")
                latest = (decision.get("candidate_final")
                          if decision["selected_uci"] != decision["baseline_uci"]
                          else decision.get("baseline"))
                final_cp = (latest or {}).get("cp")
                case = {"record": record_name, "record_sha256": hashlib.sha256(raw).hexdigest(),
                        "ply": ply, "fen": previous["before_fen"], "color": decision["color"],
                        "original_policy": old["policy"], "original_decision": old,
                        "observation_scope": "unaltered earlier measured receipts from this historical prefix; latest move remeasured",
                        "configured_options": options, "baseline_elapsed_ms": round(baseline_ms, 3),
                        "discard_engine": discard, "selected_cp_own": cp,
                        "selected_latest_cp_own": final_cp,
                        "selected_in_band": (abs(cp) <= 100 and abs(final_cp) <= 100
                                             if type(cp) is int and type(final_cp) is int else None),
                        "decision": decision}
                cases.append(case)
                print(json.dumps({"record": record_name, "ply": ply,
                                  "baseline_cp": (decision.get("baseline") or {}).get("cp"),
                                  "selected": decision["selected_uci"], "selected_cp": cp,
                                  "in_band": case["selected_in_band"], "reason": decision["reason"]}), flush=True)
    after_hashes = {name: sha(ROOT / name) for name in tracked}
    assert before_hashes == after_hashes, "Sources changed during native cases"
    assert before_binary == sha(binary) and before_archive == sha(archive), "Native inputs changed"
    return {"schema": "regicide.sparring-band-native.v1", "created_at": utc(),
            "source": source_version(), "policy": sparring_play.POLICY,
            "source_sha256": after_hashes, "source_sha256_before": before_hashes,
            "sources_and_inputs_unchanged": True,
            "binary": str(binary), "binary_sha256": sha(binary),
            "archive": str(archive), "archive_sha256": sha(archive),
            "baseline_ms": movetime_ms, "cases": cases,
            "scope": "Declared historical-prefix counterfactuals, no new game or rating measurement"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--archive", type=Path,
                        default=ROOT / "docs/matches/2026-11-02-sparring/raw-records.zip")
    parser.add_argument("--movetime-ms", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or not 600 <= args.movetime_ms <= 5000:
        parser.error("output must be new and movetime-ms must be 600..5000")
    result = run(args.binary.resolve(strict=True), args.archive.resolve(strict=True), args.movetime_ms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
