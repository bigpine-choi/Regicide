# Rook: local American English voice

Rook uses Kokoro v1.0's `am_michael` American male voice in the Windows Godot
client when the optional local runtime is installed. The user requested a friendly,
clear American voice. This is a synthetic preset, not a real person's voice clone.
Naturalness is a listening preference, not a measured playing-strength result.

## Install and use

From the repository root, with Windows Python **3.12 or 3.13**:

```powershell
python scripts/setup_rook_voice.py
```

The installer creates a separate `.venv-voice`, downloads the official quantized
model (92,361,271 bytes) and voices (28,214,398 bytes), checks their SHA-256 hashes,
and verifies actual WAV generation before publishing `build/voice-models/config.json`.
It does not stop or launch the game. The next game launch uses the new voice.
`--voice am_fenrir` selects the other supported American male preset.

The launcher passes the absolute config path. Running the Godot source project
directly also discovers the repository config. Explicit `--voice-config` takes
precedence; older LOCALAPPDATA configs remain a fallback. Keeping the default
config in the checkout avoids Windows packaged-app LOCALAPPDATA redirection.
If the checkout moves, rerun the installer to refresh the absolute paths.

There is no TTS account, paid API, online synthesis, or upload of coaching text.
Only installation downloads packages/models. Generated speech stays local and is
cached by text, preset, asset identity and helper revision, with at most 100 WAVs.
Native synthesis failure switches to an installed English Windows voice with a
visible status. If neither works, text remains available. The browser client still
uses its existing local browser speech provider; the neural adapter is for Godot.

## Playback and resource behavior

- Automatic narration follows the current visible card, including the selected
  human side. Navigation, retry, hiding, mute and exit invalidate old requests.
- SAN is expanded for speech only: `Nf3` becomes `knight to f three`, `exd5` becomes
  `e pawn takes d five`, and castling, checks and promotions are read explicitly.
- One short-lived CPU process per uncached utterance; two ONNX intra-op threads,
  one inter-op thread, no GPU dependency. Repeated utterances avoid model loading.
- Godot owns the real Python interpreter PID. The installer records the isolated
  package path and exact Python major/minor; it does not launch the Windows venv
  redirector, which would leave a separate inference child on cancellation.
- Text travels in UTF-8 JSON. The command line contains only fixed options and
  paths. Each request has a private folder, cancellation marker and atomic result.
  Cancelled or stale tickets cannot fall through to another voice and start talking.
- A complete mono 24kHz/16-bit WAV is required before a cache entry is reused.

On this Ryzen AI 7 350 Windows machine, one uncached 7.27-second sample took
3.58 seconds including process startup; a cache hit took 0.14 seconds. These are
single samples, not latency guarantees. Very short text can be less natural; the
official voice card also documents short-utterance limitations. Coach dialogue
stays concise instead of adding filler to suit the speech model.

## Sources, versions and licenses

Reviewed 2026-11-02. Regicide's engine and dialogue rules remain local project code;
Kokoro is an external speech renderer and never selects or evaluates chess moves.

| Component | Selected version | Source / license |
| --- | --- | --- |
| Kokoro weights and voice presets | 82M v1.0 | [Official model card](https://huggingface.co/hexgrad/Kokoro-82M), Apache-2.0 |
| ONNX model conversion | model-files-v1.0 int8 | [Upstream release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) |
| Kokoro ONNX wrapper | 0.6.1 | [Repository and license](https://github.com/thewh1teagle/kokoro-onnx), MIT |
| Voice IDs and limitations | am_michael / am_fenrir | [Official voice card](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) |
| ONNX Runtime | 1.31.1 | [Microsoft repository](https://github.com/microsoft/onnxruntime), MIT |
| Phonemizer | 3.4.0 | [Author repository](https://github.com/bootphon/phonemizer), GPL-3.0 |
| eSpeak NG via espeakng-loader | loader 0.2.4 | [Loader](https://github.com/thewh1teagle/espeakng-loader), [eSpeak license](https://github.com/espeak-ng/espeak-ng/blob/master/COPYING), GPL dependency |

The speech dependency stack is **not all MIT/Apache**. Installed package notices
remain in the isolated environment. This repository does not bundle the external
runtime or model binaries; redistributing them requires preserving the relevant
licenses, notices and applicable source obligations. The model card additionally
records upstream training-data attributions.

The complete pinned Python dependency list is `requirements-voice.txt`.
The local installation manifest is `build/voice-models/installation.json`.

| Asset | SHA-256 |
| --- | --- |
| kokoro-v1.0.int8.onnx | `6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb` |
| voices-v1.0.bin | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |
