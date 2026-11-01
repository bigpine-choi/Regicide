"""Check native analysis geometry with synthetic text states and optional real UCI.

Headless results validate Container geometry, never rendered pixels or OS input.
A saved pre-fix script can be loaded beside the current themed scene to show the
original drift without replacing any production source.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading

import godot_integration_tests as integration


def run(args, label, extra=()):
    command = [str(args.godot), "--headless", "--path", str(integration.ROOT / "godot"),
               "--script", "res://tests/analysis_layout_tests.gd", "--",
               "--report", str(args.output / f"{label}.json"), *map(str, extra)]
    result = subprocess.run(command, cwd=integration.ROOT, text=True, encoding="utf-8",
                            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=150)
    (args.output / f"{label}.txt").write_text(result.stdout, encoding="utf-8")
    print(result.stdout[-8000:], end="")
    assert result.returncode == 0 and "SCRIPT ERROR:" not in result.stdout and "ERROR:" not in result.stdout
    report = json.loads((args.output / f"{label}.json").read_text(encoding="utf-8"))
    assert report["failures"] == 0
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--output", type=Path, default=integration.ROOT / "docs/checks/2026-11-01-analysis-layout")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = {}
    if args.baseline_source:
        original = args.baseline_source.resolve(strict=True).read_bytes()
        (args.output / "original-analysis-mode.gd.txt").write_bytes(original)
        reports["baseline"] = run(args, "baseline-geometry", ["--source-script", args.baseline_source.resolve(strict=True), "--expect-drift"])
    if args.binary:
        args.binary = args.binary.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="regicide-analysis-layout-") as temporary:
            temporary = Path(temporary)
            game = integration.backend.HumanGame(args.binary, temporary / "games")
            server = integration.backend.HumanServer(game)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            config = temporary / "private-backend.json"
            config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
            original = game.state()
            archives = {path.name: path.read_bytes() for path in game.save_dir.iterdir()}
            try:
                reports["fixed"] = run(args, "fixed-geometry", ["--backend-file", config])
                assert game.state() == original
                assert {path.name: path.read_bytes() for path in game.save_dir.iterdir()} == archives
                assert not game.analysis.snapshot()["active"]
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)
                game.close()
    else:
        reports["fixed"] = run(args, "fixed-geometry")
    summary = {label: {"checks": report["checks"], "mode": report["mode"],
                       "windows": [{"size": window["window"], "maximum_geometry_delta":
                                    max(case["max_delta"] for case in window["cases"])} for window in report["windows"]],
                       "live_transitions": len(report["live"])} for label, report in reports.items()}
    (args.output / "layout-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    sources = [integration.ROOT / "godot/scripts/analysis_mode.gd", integration.ROOT / "godot/scripts/analysis_board.gd",
               integration.ROOT / "godot/scripts/evaluation_bar.gd", integration.ROOT / "godot/tests/analysis_layout_tests.gd",
               Path(__file__).resolve()]
    if args.baseline_source:
        sources.append(args.output / "original-analysis-mode.gd.txt")
    manifest = {str(path.relative_to(integration.ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    (args.output / "layout-source-sha256.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
