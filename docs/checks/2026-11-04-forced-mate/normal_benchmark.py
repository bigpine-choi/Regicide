"""Diagnostic paired timing; run SCRIPT BASELINE PROTOTYPE OUTPUT_JSON."""
import json
import runpy
import statistics
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[3]
module = runpy.run_path(str(repo / "tests/mate_normal_parity.py"))
Engine = module["Engine"]
position = next(case[1] for case in module["CASES"] if case[0] == "castling_depth3")
engines = [Engine(sys.argv[1]), Engine(sys.argv[2])]
records = []
try:
    for engine in engines:
        for _ in range(2):
            engine.search(position, "go depth 3")
    for repetition in range(8):
        pair = {}
        for index in ((0, 1) if repetition % 2 == 0 else (1, 0)):
            pair["baseline" if index == 0 else "prototype"] = engines[index].search(position, "go depth 3")
        assert pair["baseline"]["result"] == pair["prototype"]["result"], pair
        records.append(pair)
    medians = {name: statistics.median(row[name]["wall_ms"] for row in records)
               for name in ("baseline", "prototype")}
    report = {"position": position, "command": "go depth 3", "warmups_each": 2,
              "repetitions": 8, "order": "alternating", "pairs": records,
              "median_ms": medians,
              "prototype_over_baseline": medians["prototype"]/medians["baseline"],
              "note": "Local diagnostic timing, uncontrolled OS load; not an Elo result or universal performance guarantee."}
    Path(sys.argv[3]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("median_ms","prototype_over_baseline","repetitions")}), flush=True)
finally:
    for engine in engines:
        engine.close()
