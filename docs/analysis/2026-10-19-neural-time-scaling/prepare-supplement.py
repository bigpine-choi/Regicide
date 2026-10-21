"""Scope correction: also inspect the long-time loss in the adverse G15 control.

Uses historical long-game scores, not new diagnostic scores, to mirror the two
existing deterioration rules. The primary 15 positions and their results stand.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_neural_time_scaling as diagnostic

base = Path(__file__).resolve().parent
output = base / "supplement"
output.mkdir()
primary = diagnostic.read(base / "protocol.json")
short = diagnostic.read(ROOT / diagnostic.OLD / "attempt-001-time100/time100.json")
long = diagnostic.read(ROOT / diagnostic.OLD / "attempt-001-time1000/time1000.json")
a = next(g for g in short["games"] if g["game"] == 15)
b = next(g for g in long["games"] if g["game"] == 15)
assert a["candidate_points"] == 0.5 and b["candidate_points"] == 0

# Swap only for selection, so the existing pure selector applies its two
# deterioration rules to the historical long trajectory. Drop first divergence.
swapped = diagnostic.select_positions({"games": [b]}, {"games": [a]})
positions = []
for position in swapped["positions"]:
    position["sources"] = [s for s in position["sources"] if s["selection_rule"] != "first_divergence"]
    if not position["sources"]:
        continue
    position["position_id"] = f"S{len(positions) + 1:02}"
    for source in position["sources"]:
        source["source_movetime_ms"] = 1000
    positions.append(position)
changed = next(g for g in diagnostic.read(base / "positions.json")["changed_games"] if g["game"] == 15)
diagnostic.write(output / "positions.json", {"schema_version": 1, "changed_games": [changed], "positions": positions})
protocol = dict(primary)
protocol.update(created_utc=diagnostic.now(), variant="long_loss_control",
                positions=(output / "positions.json").relative_to(ROOT).as_posix(),
                selection="Scope supplement after primary queries completed: mirror the same largest nonmate drop (pre>=-200;drop<=-100) and first three own scores<=-100 pre-turn rules on the historical 1000ms trajectory of the sole worsened game G15. No first-divergence position. Pure selector sees old logs only, not new engine results.",
                scope_amendment="Primary selection searched deterioration only in 100ms games; this omitted the losing 1000ms path of the adverse control. Preserve the primary plan/results unchanged, add these two old-log-selected positions to inspect both outcome directions. This is an exploratory scope correction, not a fresh holdout or strength experiment.")
protocol["files"] = dict(primary["files"])
for path in [base / "protocol.json", Path(__file__), output / "positions.json"]:
    protocol["files"][path.relative_to(ROOT).as_posix()] = diagnostic.digest(path)
diagnostic.write(output / "protocol.json", protocol)
print(json.dumps({"prepared": str(output), "positions": len(positions),
                  "source_plies": [s["ply"] for p in positions for s in p["sources"]]}))
