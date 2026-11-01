"""Local Kokoro -> PCM WAV helper. Text is JSON data, never a shell command.

Optional runtime installed by scripts/setup_rook_voice.py. This process has no
network client, audio playback, or access to chess engine state.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import wave


VOICE_IDS = ("am_michael", "am_fenrir")
CACHE_LIMIT = 100
REVISION = 1
PIECES = {"K": "king", "Q": "queen", "R": "rook", "B": "bishop", "N": "knight"}
NUMBERS = dict(zip("12345678", ("one", "two", "three", "four", "five", "six", "seven", "eight")))
SAN = re.compile(r"(?<![\w])(?:O-O-O|O-O|0-0-0|0-0|[KQRBN](?:[a-h][1-8]|[a-h1-8])?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=[QRBN])?|[a-h][1-8](?:=[QRBN])?)[+#]?(?![\w])")


def square_name(square: str) -> str:
    return square[0] + " " + NUMBERS[square[1]]


def spoken_chess(text: str) -> str:
    """Expand SAN only for speech; the visible coach text keeps normal notation."""
    def expand(match: re.Match) -> str:
        token = match.group()
        suffix = ", checkmate" if token.endswith("#") else ", check" if token.endswith("+") else ""
        token = token.rstrip("+#")
        if token in ("O-O-O", "0-0-0", "O-O", "0-0"):
            return ("castle queenside" if token.count("-") == 2 else "castle kingside") + suffix
        promotion = ""
        if "=" in token:
            token, piece = token.split("=")
            promotion = ", promoting to a " + PIECES[piece]
        target, prefix = token[-2:], token[:-2]
        if prefix and prefix[0] in PIECES:
            piece = PIECES[prefix[0]]
            origin = prefix[1:].replace("x", "")
            if origin:
                piece += " from " + (square_name(origin) if len(origin) == 2 else NUMBERS.get(origin, origin))
            wording = piece + (" takes " if "x" in prefix else " to ") + square_name(target)
        elif "x" in prefix:
            wording = prefix[0] + " pawn takes " + square_name(target)
        else:
            wording = square_name(target)
        return wording + promotion + suffix
    return SAN.sub(expand, text.replace("’", "'"))


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("version") != 1 or config.get("provider") != "kokoro" or config.get("voice_id") not in VOICE_IDS:
        raise ValueError("Unsupported local voice configuration")
    # Launch the real interpreter, not Windows' venv redirector: then Godot owns
    # the actual inference PID and cancellation cannot leave a child running.
    packages = config.get("site_packages", "")
    if config.get("python_version") != list(sys.version_info[:2]) or not Path(packages).is_absolute() or not Path(packages).is_dir():
        raise ValueError("Local voice interpreter or package environment does not match")
    if packages not in sys.path:
        sys.path.insert(0, packages)
    for key in ("model", "voices", "cache_dir"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError("Voice paths must be absolute")
    for key in ("model", "voices"):
        if not Path(config[key]).is_file():
            raise FileNotFoundError("Local voice assets are missing")
    if importlib.util.find_spec("kokoro_onnx") is None:
        raise RuntimeError("Local voice runtime is missing")
    return config


def cancelled(folder: Path) -> None:
    if (folder / "cancel").exists():
        raise InterruptedError("cancelled")


def valid_wave(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as stream:
            frames = stream.getnframes()
            return (stream.getnchannels() == 1 and stream.getsampwidth() == 2 and stream.getframerate() == 24000
                    and 0 < frames <= 24000 * 120 and len(stream.readframes(frames)) == frames * 2)
    except (OSError, EOFError, wave.Error):
        return False


def trim_cache(folder: Path) -> None:
    # Only this helper's content-addressed WAVs; never recurse or delete other files.
    files = []
    for item in folder.glob("*.wav"):
        if re.fullmatch(r"[a-f0-9]{64}\.wav", item.name):
            try:
                files.append((item.stat().st_mtime, item))
            except OSError:
                pass  # Another game window may be pruning the same local cache.
    for _, item in sorted(files, reverse=True)[CACHE_LIMIT:]:
        try:
            item.unlink(missing_ok=True)
        except OSError:
            pass


def synthesize(config: dict, text: str, folder: Path) -> dict:
    started = time.monotonic()
    spoken = spoken_chess(text)
    identity = {"revision": REVISION, "text": spoken, "voice": config["voice_id"], "speed": 1.0,
                "assets": [(config[key], Path(config[key]).stat().st_size, Path(config[key]).stat().st_mtime_ns) for key in ("model", "voices")]}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    cache = Path(config["cache_dir"])
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / (digest + ".wav")
    output = folder / "speech.wav"
    cancelled(folder)
    hit = valid_wave(cached)
    if hit:
        try:
            shutil.copyfile(cached, output)
            cached.touch()
        except OSError:
            hit = False
    if not hit:
        # Avoid competing with Regicide's search for all CPU cores.
        os.environ["OMP_NUM_THREADS"] = "2"
        import numpy as np
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        session = ort.InferenceSession(config["model"], sess_options=options, providers=["CPUExecutionProvider"])
        engine = Kokoro.from_session(session, config["voices"])
        cancelled(folder)
        samples, rate = engine.create(spoken, voice=config["voice_id"], lang="en-us", speed=1.0)
        cancelled(folder)
        if rate != 24000 or len(samples) == 0 or len(samples) > rate * 120 or not np.isfinite(samples).all() or np.max(np.abs(samples)) < .001:
            raise ValueError("Invalid generated speech")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(str(output), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm.tobytes())
        temporary = cache / (digest + "." + str(os.getpid()) + ".tmp")
        try:
            shutil.copyfile(output, temporary)
            temporary.replace(cached)
        finally:
            temporary.unlink(missing_ok=True)
    cancelled(folder)
    trim_cache(cache)
    return {"output": "speech.wav", "voice_id": "kokoro:" + config["voice_id"], "provider": "kokoro",
            "cache_hit": hit, "elapsed_ms": round((time.monotonic() - started) * 1000)}


def process(config_path: Path, request_path: Path) -> dict:
    folder = request_path.parent
    request_id = ""
    try:
        request = json.loads(request_path.read_text(encoding="utf-8-sig"))
        request_id = request.get("request_id", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise ValueError("Invalid request identifier")
        cancelled(folder)
        config = load_config(config_path)
        voice = "kokoro:" + config["voice_id"]
        if request.get("action") == "voices":
            detail = {"voices": [{"id": voice, "name": "Rook · American English", "language": "en-US", "provider": "kokoro"}]}
        elif request.get("action") == "synthesize":
            text = request.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 1500:
                raise ValueError("Speech text must contain 1 to 1500 characters")
            if request.get("voice_id") != voice:
                raise ValueError("Voice does not match the configured American voice")
            detail = synthesize(config, text, folder)
        else:
            raise ValueError("Unknown voice action")
        result = {"ok": True, "request_id": request_id, "runtime_pid": os.getpid(), **detail}
    except Exception as exc:
        (folder / "speech.wav").unlink(missing_ok=True)
        result = {"ok": False, "request_id": request_id, "error": str(exc)}
    temporary = folder / "response.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(folder / "response.json")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    process(args.config, args.request)
