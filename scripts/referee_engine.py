"""Resolve a separate local Stockfish referee without starting any process.

Only the application's production entry point should call this resolver. Tests
that inject an engine must opt into a referee explicitly. Resolution never
downloads files, modifies an installation, scans PATH, or selects Regicide as a
fallback. A caller must still check the UCI identity when opening the process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_VARIABLE = "REGICIDE_REFEREE_ENGINE"
STOCKFISH_VERSION = "18"
STOCKFISH_REVISION = "cb3d4ee9b47d0c5aae855b12379378ea1439675c"
STOCKFISH_SHA256 = "9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6"
STOCKFISH_LICENSE_SHA256 = "230184f60bae2feaf244f10a8bac053c8ff33a183bcc365b4d8b876d2b7f4809"
STOCKFISH_SOURCE_URL = "https://github.com/official-stockfish/Stockfish"
STOCKFISH_RELEASE_URL = f"{STOCKFISH_SOURCE_URL}/releases/tag/sf_18"
STOCKFISH_DOWNLOAD_URL = f"{STOCKFISH_SOURCE_URL}/releases/download/sf_18/stockfish-windows-x86-64.zip"
STOCKFISH_LICENSE_URL = f"{STOCKFISH_SOURCE_URL}/blob/{STOCKFISH_REVISION}/Copying.txt"
DEFAULT_RELATIVE_PATHS = (
    "build-m2-opponents/stockfish18/package/stockfish/stockfish-windows-x86-64.exe",
    "build/referee/stockfish18/package/stockfish/stockfish-windows-x86-64.exe",
)


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _unavailable(reason, selection, binary=None):
    return {"available": False, "binary": str(binary) if binary is not None else None,
            "name": None, "sha256": None, "source_url": None, "license": None,
            "verified_official": False, "selection": selection, "reason": reason}


def _resolve_path(value, root):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("심판 엔진의 실행 파일 경로를 지정하세요.")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip() or any(char in raw for char in "\0\r\n"):
        raise ValueError("심판 엔진 경로는 비어 있지 않은 한 줄이어야 합니다.")
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _inspect(path, selection, *, require_official=False):
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return _unavailable("심판 엔진 실행 파일이 없거나 비어 있습니다.", selection, path)
        if sys.platform == "win32" and path.suffix.lower() != ".exe":
            return _unavailable("Windows 심판 엔진은 .exe 실행 파일이어야 합니다.", selection, path)
        if sys.platform != "win32" and (path.suffix.lower() == ".exe" or not os.access(path, os.X_OK)):
            return _unavailable("현재 운영체제에서 실행할 수 있는 심판 엔진 파일을 지정하세요.", selection, path)
        sha256 = _digest(path)
        official = sha256 == STOCKFISH_SHA256
        if require_official and not official:
            return _unavailable("기존 Stockfish 실행 파일의 해시가 공식 고정 기록과 다릅니다. 심판을 사용하지 않습니다.", selection, path)
        license_path = path.parent / "Copying.txt"
        license_sha256 = _digest(license_path) if license_path.is_file() else None
        if require_official and license_sha256 != STOCKFISH_LICENSE_SHA256:
            return _unavailable("기존 Stockfish의 원본 라이선스를 확인하지 못했습니다. 심판을 사용하지 않습니다.", selection, path)
    except (OSError, RuntimeError) as error:
        return _unavailable(f"심판 엔진 파일을 읽지 못했습니다: {type(error).__name__}", selection, path)
    result = {
        "available": True, "binary": str(path), "name": "Stockfish 18" if official else None,
        "sha256": sha256, "verified_official": official, "selection": selection,
        "source_url": STOCKFISH_SOURCE_URL if official else None,
        "release_url": STOCKFISH_RELEASE_URL if official else None,
        "download_url": STOCKFISH_DOWNLOAD_URL if official else None,
        "source_revision": STOCKFISH_REVISION if official else None,
        "license": "GPL-3.0-or-later" if official else None,
        "license_url": STOCKFISH_LICENSE_URL if official else None,
        "license_path": str(license_path) if license_path.is_file() else None,
        "license_sha256": license_sha256,
        "cpu_variant": "Windows x86-64 generic" if official else None,
        "reason": "" if official else "사용자가 지정한 별도 실행 파일입니다. 실행 시 Stockfish 신원을 확인해야 합니다.",
    }
    return result


def resolve_referee_engine(repo_root=ROOT, explicit_path=None):
    """Return availability and provenance; an invalid explicit choice never falls back.

    Selection order is explicit path, REGICIDE_REFEREE_ENGINE, then the two
    known local package locations. The automatic package is pinned to the
    previously verified official generic Windows Stockfish 18 binary and GPL
    text. Explicit paths may point elsewhere and are reported as unverified
    unless their bytes match that pinned release. No process is started here.
    """
    try:
        root = Path(repo_root).expanduser().resolve()
        if not root.is_dir():
            return _unavailable("프로젝트 폴더를 확인하지 못했습니다.", "none")
        if explicit_path is not None:
            try:
                path = _resolve_path(explicit_path, root)
            except (ValueError, OSError, RuntimeError):
                return _unavailable("명시한 심판 엔진 경로를 확인해 주세요.", "explicit")
            return _inspect(path, "explicit")
        environment_path = os.environ.get(ENVIRONMENT_VARIABLE)
        if environment_path is not None and environment_path.strip():
            try:
                path = _resolve_path(environment_path, root)
            except (ValueError, OSError, RuntimeError):
                return _unavailable("환경변수의 심판 엔진 경로를 확인해 주세요.", "environment")
            return _inspect(path, "environment")
        if sys.platform != "win32":
            return _unavailable("자동 준비 경로는 Windows용입니다. 이 운영체제의 Stockfish 경로를 별도로 지정하세요.", "none")
        for relative in DEFAULT_RELATIVE_PATHS:
            path = root / relative
            if path.exists():
                # A changed or broken preferred installation must be visible,
                # rather than silently replacing it with another candidate.
                return _inspect(path.resolve(), "local_package", require_official=True)
        return _unavailable("로컬 Stockfish가 없습니다. 별도 실행 파일을 지정하면 심판을 사용할 수 있습니다.", "none")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _unavailable("심판 엔진 경로를 확인하지 못했습니다.", "none")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--engine", type=Path, help="명시적으로 선택할 별도 Stockfish 실행 파일")
    args = parser.parse_args(argv)
    result = resolve_referee_engine(args.repo_root, args.engine)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
