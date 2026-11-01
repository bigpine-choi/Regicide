"""Verify actual Godot mixer PCM from move landing in an isolated project.

No editor import is run: new WAV files must work with the ordinary launcher.
An effect silences this test's output after PCM capture, without touching the
user's game, backend, audio device settings, or project cache.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r'''extends SceneTree

const Board = preload("res://scripts/chess_board.gd")
var capture := AudioEffectCapture.new()
var results: Array = []
var failures := 0

func _initialize() -> void:
	_run.call_deferred()

func sample(label: String, audible: bool) -> void:
	var frames := capture.get_buffer(capture.get_frames_available())
	var peak := 0.0
	var energy := 0.0
	for frame in frames:
		peak = maxf(peak, maxf(absf(frame.x), absf(frame.y)))
		energy += frame.x * frame.x + frame.y * frame.y
	var rms := sqrt(energy / maxf(1.0, frames.size() * 2.0))
	var valid := peak > 0.001 if audible else peak < 0.00001
	if not valid:
		failures += 1
		printerr("FAIL: " + label)
	results.append({"case": label, "frames": frames.size(), "peak": peak, "rms": rms, "pass": valid})
	capture.clear_buffer()

func _run() -> void:
	AudioServer.add_bus()
	var bus := AudioServer.bus_count - 1
	AudioServer.set_bus_name(bus, "MoveSoundQA")
	capture.buffer_length = 2.0
	AudioServer.add_bus_effect(bus, capture)
	var silence := AudioEffectAmplify.new()
	silence.volume_linear = 0.0
	AudioServer.add_bus_effect(bus, silence)
	var board := Board.new()
	root.add_child(board)
	board.size = Vector2(650, 650)
	board._move_sound.bus = "MoveSoundQA"
	await process_frame
	var first := {"board": {"e1": "K", "e8": "k", "e2": "P", "d5": "p"}, "turn": "white", "ply": 0, "game_id": "audio-qa", "last_move": ""}
	var moved := {"board": {"e1": "K", "e8": "k", "e4": "P", "d5": "p"}, "turn": "black", "ply": 1, "game_id": "audio-qa", "last_move": "e2e4"}
	var taken := {"board": {"e1": "K", "e8": "k", "e4": "p"}, "turn": "white", "ply": 2, "game_id": "audio-qa", "last_move": "d5e4"}
	board.state = first
	await create_timer(0.5).timeout
	sample("initial board is silent", false)
	board.state = moved
	await create_timer(0.6).timeout
	sample("human move produces actual mixer PCM", true)
	var move_length := board._move_sound.stream.get_length()
	board.state = moved.duplicate(true)
	await create_timer(0.5).timeout
	sample("repeated state is silent", false)
	board.state = taken
	await create_timer(0.6).timeout
	sample("opponent capture produces actual mixer PCM", true)
	var capture_length := board._move_sound.stream.get_length()
	if not (is_equal_approx(move_length, 0.12) and is_equal_approx(capture_length, 0.18)):
		failures += 1
		printerr("FAIL: distinct move and capture samples")
	board.state = moved
	await create_timer(0.6).timeout
	sample("undo is silent", false)
	board.state = taken
	board.hide()
	await create_timer(0.6).timeout
	sample("hidden transition is silent", false)
	var args := OS.get_cmdline_user_args()
	var report := {"driver": AudioServer.get_driver_name(), "mix_rate": AudioServer.get_mix_rate(), "results": results, "move_seconds": move_length, "capture_seconds": capture_length, "failures": failures}
	if not args.is_empty():
		var output := FileAccess.open(args[0], FileAccess.WRITE)
		output.store_string(JSON.stringify(report, "  "))
	print(JSON.stringify(report))
	board.free()
	quit(1 if failures else 0)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "build/move-sounds/review")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="regicide-audio-qa-") as temporary:
        project = Path(temporary) / "godot"
        shutil.copytree(ROOT / "godot", project, ignore=shutil.ignore_patterns(".godot"))
        settings = project / "project.godot"
        settings.write_text(settings.read_text(encoding="utf-8").replace(
            'config/name="Regicide"', f'config/name="Regicide Audio QA {os.getpid()}"'), encoding="utf-8")
        (project / "tests/move_sound_mixer.gd").write_text(SCRIPT, encoding="utf-8")
        result = subprocess.run([
            str(args.godot.resolve(strict=True)), "--path", str(project),
            "--position", "20000,20000", "--script", "res://tests/move_sound_mixer.gd", "--", str(output / "mixer.json")
        ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    text = result.stdout + result.stderr
    (output / "mixer.txt").write_text(text, encoding="utf-8")
    print(text)
    if result.returncode or "ERROR:" in text or "FAIL:" in text:
        raise SystemExit(1)
    report = json.loads((output / "mixer.json").read_text(encoding="utf-8"))
    if report["driver"] == "Dummy" or report["failures"]:
        raise SystemExit("Real audio driver verification did not pass")


if __name__ == "__main__":
    main()
