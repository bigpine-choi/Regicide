"""Run the combined timing, sparring and coaching regressions once per test ID."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
PATTERNS = ["*pacing*tests.py", "*ramp*tests.py", "sparring_safety*tests.py",
            "*referee*tests.py", "sparring_quality*tests.py", "coaching_tests.py",
            "coaching_reference_tests.py", "opening_explorer_tests.py"]


def hashes():
    paths = [p for directory in ("scripts", "tests") for p in (ROOT / directory).rglob("*.py")]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main():
    selected = {}
    for pattern in PATTERNS:
        for test in flatten(unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern)):
            selected[test.id()] = test
    before = hashes()
    started = time.monotonic()
    with (OUT / "python-tests.log").open("w", encoding="utf-8", newline="\n") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.TestSuite(selected[key] for key in sorted(selected)))
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "patterns": PATTERNS,
              "test_ids": sorted(selected), "unique_test_count": len(selected),
              "tests_run": result.testsRun, "elapsed_seconds": round(time.monotonic() - started, 3),
              "failures": [test.id() for test, _ in result.failures],
              "errors": [test.id() for test, _ in result.errors],
              "skipped": [(test.id(), reason) for test, reason in result.skipped],
              "source_sha256": before, "sources_unchanged": before == hashes(),
              "passed": result.wasSuccessful() and before == hashes()}
    (OUT / "python-tests.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({key: value for key, value in report.items() if key not in ("test_ids", "source_sha256")}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
