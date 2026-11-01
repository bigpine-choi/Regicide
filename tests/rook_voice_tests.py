"""Local neural speech protocol, chess pronunciation and real optional-runtime tests."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "godot/scripts/rook_voice.py"
spec = importlib.util.spec_from_file_location("rook_voice", HELPER)
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)
CONFIG = ROOT / "build/voice-models/config.json"


class VoiceProtocolTests(unittest.TestCase):
    def test_pronounces_chess_notation_without_altering_prose(self):
        self.assertEqual(voice.spoken_chess("Try Nf3, then exd5. Your rook is loose."), "Try knight to f three, then e pawn takes d five. Your rook is loose.")
        self.assertEqual(voice.spoken_chess("Nbd2 R1e1 Qxe8+ e8=Q# O-O O-O-O"), "knight from b to d two rook from one to e one queen takes e eight, check e eight, promoting to a queen, checkmate castle kingside castle queenside")
        self.assertEqual(voice.spoken_chess("It's a good idea to develop."), "It's a good idea to develop.")

    def test_cancelled_and_invalid_requests_have_no_playable_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            request = folder / "request.json"
            request.write_text(json.dumps({"request_id": "fixture", "action": "synthesize"}), encoding="utf-8")
            (folder / "cancel").touch()
            result = voice.process(folder / "missing-config.json", request)
            self.assertEqual(result["error"], "cancelled")
            self.assertFalse((folder / "speech.wav").exists())
            self.assertEqual(json.loads((folder / "response.json").read_text(encoding="utf-8")), result)
            self.assertFalse((folder / "response.tmp").exists())

    def test_voice_and_text_limits_are_checked_before_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            request = folder / "request.json"
            with patch.object(voice, "load_config", return_value={"voice_id": "am_michael"}), patch.object(voice, "synthesize") as inference:
                for text, chosen in (("hello", "untrusted"), ("x" * 1501, "kokoro:am_michael"), (" ", "kokoro:am_michael")):
                    request.write_text(json.dumps({"request_id": "fixture", "action": "synthesize", "voice_id": chosen, "text": text}), encoding="utf-8")
                    self.assertFalse(voice.process(folder / "config.json", request)["ok"])
                inference.assert_not_called()

    def test_cache_prunes_only_owned_waves_and_keeps_100(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for number in range(105):
                (folder / (f"{number:064x}" + ".wav")).touch()
            (folder / "important.wav").write_bytes(b"untouched")
            (folder / "subdirectory").mkdir()
            voice.trim_cache(folder)
            self.assertEqual(len(list(folder.glob("*.wav"))), 101)
            self.assertEqual((folder / "important.wav").read_bytes(), b"untouched")
            self.assertTrue((folder / "subdirectory").is_dir())

    def test_truncated_wave_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "damaged.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                stream.writeframes(b"\x00\x00" * 24000)
            self.assertTrue(voice.valid_wave(path))
            path.write_bytes(path.read_bytes()[:44])
            self.assertFalse(voice.valid_wave(path))


@unittest.skipUnless(os.name == "nt" and CONFIG.is_file(), "Optional local neural voice not installed")
class InstalledVoiceTests(unittest.TestCase):
    def run_request(self, config_path, folder, **values):
        folder.mkdir()
        request = folder / "request.json"
        request.write_text(json.dumps({"request_id": "fixture", **values}), encoding="utf-8")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        started = time.monotonic()
        with subprocess.Popen([config["python"], str(HELPER), "--config", str(config_path), "--request", str(request)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW) as process:
            _, error = process.communicate(timeout=25)
            self.assertEqual(process.returncode, 0, error.decode("utf-8", errors="replace"))
            result = json.loads((folder / "response.json").read_text(encoding="utf-8"))
            if result.get("ok"):
                self.assertEqual(result["runtime_pid"], process.pid, "Godot must own the actual inference process, not a venv redirector")
        return result, round(1000 * (time.monotonic() - started))

    def test_actual_voice_and_identical_cached_audio(self):
        with tempfile.TemporaryDirectory(prefix="regicide-natural-voice-") as temporary:
            folder = Path(temporary)
            config = json.loads(CONFIG.read_text(encoding="utf-8"))
            config["cache_dir"] = str(folder / "cache")
            config_path = folder / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            query, _ = self.run_request(config_path, folder / "query", action="voices")
            self.assertTrue(query["ok"], query)
            chosen = query["voices"][0]["id"]
            self.assertEqual(query["voices"][0]["language"], "en-US")
            text = "Try Nf3 first. It develops your knight, and you can decide where the bishop belongs after their reply."
            cold, cold_ms = self.run_request(config_path, folder / "cold", action="synthesize", voice_id=chosen, text=text)
            warm, warm_ms = self.run_request(config_path, folder / "warm", action="synthesize", voice_id=chosen, text=text)
            self.assertTrue(cold["ok"], cold)
            self.assertTrue(warm["ok"], warm)
            self.assertFalse(cold["cache_hit"])
            self.assertTrue(warm["cache_hit"])
            self.assertEqual((folder / "cold/speech.wav").read_bytes(), (folder / "warm/speech.wav").read_bytes())
            with wave.open(str(folder / "cold/speech.wav"), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
                self.assertGreater(duration, 2)
                peak = max(abs(value[0]) for value in struct.iter_unpack("<h", wav.readframes(wav.getnframes())))
                self.assertGreater(peak, 500)
            print(json.dumps({"voice": chosen, "cold_process_ms": cold_ms, "cached_process_ms": warm_ms, "seconds": duration, "pcm_peak": peak, "audio_played": False}))


if __name__ == "__main__":
    unittest.main()
