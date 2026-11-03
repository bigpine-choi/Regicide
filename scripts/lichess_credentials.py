"""Optional Lichess bearer token: environment, memory, then user DPAPI storage.

No plaintext credential file is written. An environment token takes precedence
over the remembered token. On platforms without Windows DPAPI, remembering is
unavailable and the token remains in this process only.
"""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading
import uuid


def user_data_directory():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base and Path(base).is_absolute() else Path.home() / "AppData" / "Local") / "Regicide"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "share") / "regicide"


def validated_token(value):
    if not isinstance(value, str):
        raise ValueError("Lichess 토큰은 문자열로 입력해 주세요.")
    value = value.strip()
    if len(value) > 4096 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("Lichess 토큰의 형식을 확인해 주세요.")
    return value


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise OSError("Windows credential protection is unavailable")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        arguments = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        arguments = (ctypes.byref(source), "Regicide Lichess", None, None, None, 1, ctypes.byref(target))
    function.restype = wintypes.BOOL
    if not function(*arguments):
        raise OSError("Windows credential protection failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


class LichessCredentials:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else user_data_directory() / "credentials" / "lichess.dpapi"
        self._lock = threading.RLock()
        self._memory = ""
        self._loaded = False

    def token(self):
        with self._lock:
            try:
                environment = validated_token(os.environ.get("REGICIDE_LICHESS_TOKEN", ""))
            except ValueError:
                environment = ""
            if environment:
                return environment
            if not self._loaded:
                self._loaded = True
                try:
                    if self.path.stat().st_size <= 65536:
                        self._memory = validated_token(_dpapi(self.path.read_bytes(), decrypt=True).decode("utf-8"))
                except (OSError, ValueError, UnicodeError):
                    self._memory = ""
            return self._memory

    def set(self, token, remember=True):
        token = validated_token(token)
        if type(remember) is not bool:
            raise ValueError("토큰 저장 여부는 true 또는 false로 지정해 주세요.")
        with self._lock:
            self._memory, self._loaded = token, True
            # Forget a previous remembered value even when the replacement is
            # memory-only. Otherwise the old credential would return next run.
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                return self._message("토큰은 이번 실행에 적용했습니다. 이전 저장 정보는 지우지 못했습니다.")
            if not token:
                return self._message("저장된 토큰을 지웠습니다.")
            if not remember:
                return self._message("토큰은 이번 실행에서만 사용합니다.")
            temporary = None
            try:
                protected = _dpapi(token.encode("utf-8"))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
                with temporary.open("xb") as stream:
                    stream.write(protected)
                os.replace(temporary, self.path)
                return self._message("이 Windows 사용자만 복호화할 수 있도록 토큰을 저장했습니다.")
            except OSError:
                return self._message("보호된 토큰 저장을 사용할 수 없어 이번 실행에서만 사용합니다.")
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    @staticmethod
    def _message(message):
        try:
            environment = validated_token(os.environ.get("REGICIDE_LICHESS_TOKEN", ""))
        except ValueError:
            environment = ""
        if environment:
            message += " 현재 요청에는 REGICIDE_LICHESS_TOKEN 환경변수의 토큰을 우선 사용합니다."
        return message
