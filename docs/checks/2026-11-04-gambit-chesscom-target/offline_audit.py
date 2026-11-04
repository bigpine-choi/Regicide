"""Audit two already declared examples; never feed external answers into play."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

import chess
import chess.engine

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from gambit_candidate_probe import safe, validate_packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    binary = args.binary.resolve(strict=True)
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    source = HERE / "native-002/report.json"
    if source.exists():
        native_bytes = source.read_bytes()
    else:
        with zipfile.ZipFile(source.parent / "evidence.zip") as archive:
            native_bytes = archive.read("report.json")
    native = json.loads(native_bytes)
    rows = [r for r in native["cases"] if r["case"]["id"] in ("refutable-legal-nc6", "legal-d6-g6")]
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "binary_sha256": sha(binary),
              "native_report_sha256": hashlib.sha256(native_bytes).hexdigest(), "script_sha256": sha(Path(__file__)),
              "scope": "Post-selection external diagnostic only. Not Chess.com Game Review, full games, rating calibration or a strength claim.",
              "milliseconds_per_search": 1500, "options": {"Threads": 1, "Hash": 16, "UCI_LimitStrength": False},
              "cases": [], "engine_closed": False}
    flags = {"creationflags": __import__("subprocess").CREATE_NO_WINDOW} if os.name == "nt" else {}
    engine = chess.engine.SimpleEngine.popen_uci([str(binary)], timeout=10, **flags)
    try:
        engine.configure(report["options"])
        report["engine_id"] = dict(engine.id)
        for row in rows:
            fixture = row["case"]
            board = chess.Board(fixture["initial_fen"])
            for uci in fixture["history_uci"]:
                board.push_uci(uci)
            queries = []
            roots = [None, row["baseline_uci"]]
            if fixture["reference_candidate_uci"] not in roots:
                roots.append(fixture["reference_candidate_uci"])
            for uci in roots:
                move = chess.Move.from_uci(uci) if uci else None
                packets, exact = [], {}
                with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=1.5),
                                     root_moves=[move] if move else None, game=object(),
                                     info=chess.engine.INFO_ALL) as stream:
                    for packet in stream:
                        packets.append(safe(packet))
                        line = validate_packet(board, packet, move)
                        if line is not None:
                            exact[line["depth"]] = line
                    answer = stream.wait()
                assert answer.move in board.legal_moves and (move is None or answer.move == move)
                queries.append({"root_uci": uci, "returned_uci": answer.move.uci(),
                                "packets": packets, "exact_by_depth": exact})
            common = set.intersection(*(set(q["exact_by_depth"]) for q in queries))
            depth = max(common) if common else None
            comparison = [{"root_uci": q["root_uci"], "line": q["exact_by_depth"][depth]}
                          for q in queries] if depth else []
            report["cases"].append({"case": fixture, "native_selected_uci": row["selected_uci"],
                                    "queries": queries, "common_completed_depth": depth,
                                    "comparison": comparison})
    finally:
        try:
            engine.quit()
        except Exception:
            engine.close()
        report["process_exit_code"] = engine.returncode.result(timeout=5)
        report["engine_closed"] = True
        report["binary_unchanged"] = sha(binary) == report["binary_sha256"]
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in report["cases"]:
        print(json.dumps({"id": row["case"]["id"], "depth": row["common_completed_depth"],
            "scores": [{"root": q["root_uci"], "cp": q["line"]["cp_root"], "mate": q["line"]["mate_root"]}
                       for q in row["comparison"]]}))


if __name__ == "__main__":
    main()
