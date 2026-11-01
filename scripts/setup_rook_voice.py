"""Install optional local American English speech, then publish a verified config.

Run with Python 3.12-3.13. Downloads only the pinned packages and official model
assets. Does not launch, stop, or restart the game, and never uploads coach text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import venv


ROOT = Path(__file__).resolve().parents[1]
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
ASSETS = {
    "kokoro-v1.0.int8.onnx": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


def sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def acquire(path: Path, digest: str) -> None:
    if path.is_file() and sha256(path) == digest:
        return
    temporary = path.with_suffix(path.suffix + ".download")
    try:
        print("Downloading " + path.name, flush=True)
        urllib.request.urlretrieve(RELEASE + path.name, temporary)
        if sha256(temporary) != digest:
            raise ValueError("Official asset checksum mismatch: " + path.name)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", choices=("am_michael", "am_fenrir"), default="am_michael")
    parser.add_argument("--config", type=Path, help="Override config location for an isolated test")
    args = parser.parse_args()
    if os.name != "nt" or not (3, 12) <= sys.version_info[:2] < (3, 14):
        raise SystemExit("This Windows installer requires Python 3.12 through 3.13.")
    # Keep the installation in the checkout: packaged Windows apps can redirect
    # LOCALAPPDATA, so an Explorer launch may otherwise see a different config.
    destination = args.config or ROOT / "build/voice-models/config.json"
    destination = destination.resolve()
    environment = ROOT / ".venv-voice"
    python = environment / "Scripts/python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements-voice.txt")], check=True)
    assets = ROOT / "build/voice-models"
    assets.mkdir(parents=True, exist_ok=True)
    for name, digest in ASSETS.items():
        acquire(assets / name, digest)
    runtime = json.loads(subprocess.check_output([str(python), "-c", "import json,sys,sysconfig; print(json.dumps({'python':sys._base_executable,'site_packages':sysconfig.get_path('purelib'),'python_version':list(sys.version_info[:2])}))"], text=True))
    config = {"version": 1, "provider": "kokoro", **runtime,
              "model": str(assets / "kokoro-v1.0.int8.onnx"), "voices": str(assets / "voices-v1.0.bin"),
              "voice_id": args.voice, "cache_dir": str(destination.parent / "cache")}
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A failed installation never replaces a working user's configuration.
    with tempfile.TemporaryDirectory(prefix="rook-voice-setup-") as temporary:
        folder = Path(temporary)
        candidate = folder / "config.json"
        # Always test the current runtime/model, even on reinstall. A previous
        # installation's cached WAV must not mask a broken inference dependency.
        candidate.write_text(json.dumps({**config, "cache_dir": str(folder / "cache")}, indent=2), encoding="utf-8")
        request = folder / "request.json"
        request.write_text(json.dumps({"request_id": "setup", "action": "synthesize", "voice_id": "kokoro:" + args.voice,
                                       "text": "You've developed the knight. Before your next move, check what your opponent is threatening."}), encoding="utf-8")
        subprocess.run([config["python"], str(ROOT / "godot/scripts/rook_voice.py"), "--config", str(candidate), "--request", str(request)],
                       check=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads((folder / "response.json").read_text(encoding="utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Speech verification failed: " + str(result.get("error")))
        temporary_config = destination.with_suffix(".tmp")
        temporary_config.write_text(json.dumps(config, indent=2), encoding="utf-8")
        temporary_config.replace(destination)
    manifest = {"model_release": RELEASE, "assets": ASSETS, "voice_id": args.voice,
                "packages": subprocess.check_output([str(python), "-m", "pip", "freeze"], text=True).splitlines(),
                "verification": result, "licenses": "See docs/ROOK_VOICE.md; model Apache-2.0, wrapper MIT, phonemizer/eSpeak GPL dependencies."}
    (assets / "installation.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"ready": True, "voice": args.voice, "config": str(destination), "sample_ms": result["elapsed_ms"]}))


if __name__ == "__main__":
    main()
