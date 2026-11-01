"""Windows Desktop voice smoke tests: synthesize private WAVs without audio playback."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "godot/scripts/windows_speech.ps1"


@unittest.skipUnless(os.name == "nt", "Windows Desktop SAPI is platform-specific")
class WindowsSpeechTests(unittest.TestCase):
    def request(self, folder: Path, **values):
        folder.mkdir()
        request = {"request_id": "fixture", **values}
        source = folder / "request.json"
        source.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        if values.get("cancel"):
            (folder / "cancel").write_text("cancel", encoding="ascii")
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(HELPER), "-RequestPath", str(source)],
                                capture_output=True, text=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((folder / "response.json").read_text(encoding="utf-8"))

    def test_installed_english_voice_produces_non_silent_pcm_without_playback(self):
        with tempfile.TemporaryDirectory(prefix="regicide-voice-test-") as temp:
            base = Path(temp)
            voices = self.request(base / "voices", action="voices")
            self.assertTrue(voices["ok"])
            self.assertTrue(all(item["language"].lower().split("-")[0] == "en" for item in voices["voices"]))
            if not voices["voices"]:
                self.skipTest("No enabled English Desktop SAPI voice is installed")
            chosen = voices["voices"][0]["id"]
            result = self.request(base / "audio", action="synthesize", voice_id=chosen,
                                  text="White develops the knight. Check the opponent's strongest reply.")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["voice_id"], chosen)
            with wave.open(str(base / "audio/speech.wav"), "rb") as wav:
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertGreater(wav.getnframes() / wav.getframerate(), 1)
                samples = list(struct.iter_unpack("<h", wav.readframes(wav.getnframes())))
                peak = max(abs(item[0]) for item in samples)
                self.assertGreater(peak, 500)
            print(json.dumps({"desktop_voice": chosen, "pcm_peak": peak, "played_audio": False}))

    def test_cancellation_and_invalid_voice_never_produce_a_playable_result(self):
        with tempfile.TemporaryDirectory(prefix="regicide-voice-test-") as temp:
            base = Path(temp)
            cancelled = self.request(base / "cancelled", action="synthesize", voice_id="ignored", text="Do not speak.", cancel=True)
            self.assertFalse(cancelled["ok"])
            self.assertFalse((base / "cancelled/speech.wav").exists())
            invalid = self.request(base / "invalid", action="synthesize", voice_id="not-installed", text="Do not speak.")
            self.assertFalse(invalid["ok"])
            self.assertFalse((base / "invalid/speech.wav").exists())

    def test_request_text_stays_data_and_helper_has_no_speaker_output(self):
        source = HELPER.read_text(encoding="utf-8")
        self.assertIn("SetOutputToWaveFile", source)
        self.assertNotIn("SetOutputToDefaultAudioDevice", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("Start-Process", source)


if __name__ == "__main__":
    unittest.main()
