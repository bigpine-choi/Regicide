# Natural American English Rook: verification

2026-11-02, Windows, Python 3.13, Godot 4.7.2. Base commit: `6b60428`.

The Windows game now prefers a locally installed Kokoro `am_michael` voice.
Rook dialogue v3 uses the selected human side and short observations about the
actual position: attacked pieces, opened lines, development, exchanges and
verified defenders. Existing reports regenerate their explanations on load.
The chess engine binary and move-selection policy are unchanged.

## Completed checks

- Dialogue, character/retry and coaching backend suites: 64 tests, including existing-cache
  regeneration, both perspectives, legal PV scope, recapture versus retreat,
  discovered check, promotion, pinned defenders and concealed exercise answers.
- Browser coaching: 63 checks, including side changes updating automatic narration.
- Godot coaching: 420 checks, including dialogue/hint perspectives and stable board
  geometry at 900x680, 1240x860 and 1500x1000.
- Local provider routing: 23 checks. Neural success, query/synthesis fallback,
  cancellation and stale responses use the existing speech interface.
- Actual Kokoro worker → Godot WAV decoding: 14 checks. Launch PID equals actual
  inference PID, cancelled work stops, and latest requested audio wins. A separate
  process inventory found no remaining children of the owned helper processes.
- Existing Windows Desktop speech: 12 integration checks still pass.
- Python voice helper: 6 tests. Real 24kHz PCM synthesis/cache identity, notation
  pronunciation, malformed/cancelled requests, truncated cache and bounded pruning.
- Own-engine HTTP coaching: 11 check groups using a disposable Fool's Mate game;
  live board, analysis board and saved JSON/PGN bytes remain unchanged.
- Independent hidden Windows Godot playback: selected `kokoro:am_michael`,
  AudioStreamPlayer advanced through and completed a 7.04-second clip. This is a
  playback-system check, not a blind listening-quality comparison.
- The desktop Regicide shortcut points to this checkout's updated launcher.

All game-facing checks used separate copied projects or disposable backend/game
folders. The user's running game was not stopped or restarted. Five existing
coaching rows were inspected read-only; their 15 perspective variants were 17–22
words in the selected sample. Private game files and dialogue samples are excluded
from the committed evidence. No strength or Elo claim follows from these checks.

## Reproduce

```powershell
python scripts/setup_rook_voice.py
.venv-tests/Scripts/python.exe tests/coach_explanations_tests.py
.venv-tests/Scripts/python.exe tests/character_coach_tests.py
.venv-tests/Scripts/python.exe tests/coaching_tests.py
.venv-tests/Scripts/python.exe tests/rook_voice_tests.py
node tests/web_coaching_tests.js
.venv-tests/Scripts/python.exe tests/coaching_http_tests.py --binary build/regicide.exe --output build/natural-voice-http
```

For Godot, copy `godot/` to a disposable project without `.godot/`, import it, then
run `tests/local_speech_tests.gd` and `tests/local_speech_integration_tests.gd` with
`--headless --script`. The real neural test requires user arguments
`--require-neural --voice-config <absolute-repository>/build/voice-models/config.json`.
`tests/windows_speech_tests.gd` checks the original SAPI provider. The existing
coaching fixture runner covers the UI and layout assertions.

The measured first synthesis in the native integration took 3.111 seconds for
5.525 seconds of audio; the repeated cached request took 0.190 seconds. These
individual CPU measurements include startup and vary with text and system load.
Installation verifies an uncached utterance before publishing its config.

See [voice implementation, installation and licenses](../../ROOK_VOICE.md).
