"""Private Godot validation; no player preferences, archives or backend."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import chess
from coaching_session import _annotation
from coaching_godot_tests import fixture

OUT = ROOT / "docs/checks/2026-11-04-coaching-symbols-pacing"
OUT.mkdir(parents=True, exist_ok=True)
GODOT = Path("C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64.exe")
fixture_path = OUT / "native-fixture.json"
fixture_path.write_text(json.dumps(fixture(), ensure_ascii=False, indent=2), encoding="utf-8")
sources = []
for relative, name in [("http/fools-mate-report.json", "fools-mate-report.json"),
                       ("backend/real-engine-report.json", "great-report.json")]:
    source = ROOT / "docs/checks/2026-11-01-move-badges" / relative
    report = json.loads(source.read_text(encoding="utf-8"))
    for row in report["moves"]:
        row["annotation"] = _annotation(chess.Board(row["fen_before"]), row)
    path = OUT / name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sources.append({"source": str(source.relative_to(ROOT)), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "new_display": name, "scores_changed": False})

with tempfile.TemporaryDirectory(prefix="regicide-symbols-") as temporary:
    project = Path(temporary) / "godot"
    shutil.copytree(ROOT / "godot", project, ignore=shutil.ignore_patterns(".godot"))
    config = project / "project.godot"
    config.write_text(config.read_text(encoding="utf-8").replace('config/name="Regicide"', 'config/name="Regicide Private Symbols QA"'), encoding="utf-8")
    for label, flags in [
        ("godot-symbol-regressions", ["--headless", "--script", "res://tests/coaching_tests.gd", "--", "--fixture", str(fixture_path), "--evidence-dir", str(OUT)]),
        ("godot-symbol-render", ["--position", "-16000,-16000", "--script", "res://tests/coaching_badge_render.gd", "--", str(fixture_path), str(OUT / "fools-mate-report.json"), str(OUT / "great-report.json"), str(OUT / "render")])]:
        command = [str(GODOT), "--path", str(project), *flags]
        result = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=110)
        output = result.stdout + result.stderr
        (OUT / (label + ".log")).write_text(output, encoding="utf-8")
        print(label, "exit", result.returncode, output[-1800:], flush=True)
        if result.returncode or "SCRIPT ERROR:" in output or "ERROR:" in output:
            raise RuntimeError(label)
    (OUT / "render-provenance.json").write_text(json.dumps({"mode": "saved own-engine evidence replayed with new annotation mapping; no new evaluation", "sources": sources}, indent=2), encoding="utf-8")
