"""Capture deterministic host timing receipts, without a live game or engine."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import engine_pacing
from faster_pacing_tests import FasterPacingTests

probe = FasterPacingTests()
records = []
for search_seconds in (.4, 3.5):
    old = probe.host_turn(1000, search_seconds)
    new = probe.host_turn(1500, search_seconds)
    assert old["move"] == new["move"]
    assert old["search_seconds"] == new["search_seconds"]
    records.append({"fixed_search_seconds": search_seconds, "before": old, "after": new,
                    "saved_elapsed_ms": old["elapsed_ms"] - new["elapsed_ms"],
                    "saved_wait_ms": old["wait_ms"] - new["wait_ms"]})
assert 499 <= records[0]["saved_elapsed_ms"] <= 501
assert records[1]["saved_wait_ms"] == 0
paths = ["scripts/engine_pacing.py", "scripts/coaching_session.py", "tests/faster_pacing_tests.py",
         "tests/coaching_tests.py", "tests/pacing_v4_tests.py"]
result = {"created_utc": datetime.now(timezone.utc).isoformat(), "policy": engine_pacing.POLICY,
          "post_search_reduction_ms": engine_pacing.POST_SEARCH_REDUCTION_MS,
          "mode": "deterministic host, fake engine, fake monotonic clock; not native latency or playing strength",
          "records": records,
          "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}}
path = Path(__file__).with_name("pacing-receipts.json")
path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"policy": result["policy"], "saved_elapsed_ms": [r["saved_elapsed_ms"] for r in records]}))
