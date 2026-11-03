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
        self._session_only = False
        self._signature = None
        self._source = "missing"

    def token(self):
        with self._lock:
            try:
                environment = validated_token(os.environ.get("REGICIDE_LICHESS_TOKEN", ""))
            except ValueError:
                environment = ""
            if environment:
                return environment
            if self._session_only:
                return self._memory
            try:
                stat = self.path.stat()
                signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            except FileNotFoundError:
                signature = None
            except OSError:
                self._memory, self._source = "", "unavailable"
                self._loaded = False
                return ""
            # Another app window can save the shared connection after this
            # process first checked it. Reload replacements, not just startup.
            if not self._loaded or signature != self._signature:
                self._loaded = True
                self._signature = signature
                self._memory, self._source = "", "missing"
                try:
                    if signature is not None:
                        if stat.st_size > 65536:
                            raise ValueError("credential file too large")
                        self._memory = validated_token(_dpapi(self.path.read_bytes(), decrypt=True).decode("utf-8"))
                        self._source = "stored" if self._memory else "missing"
                except OSError:
                    self._memory, self._source = "", "unavailable"
                    # A temporary file lock or protection-service failure is
                    # not proof that an unchanged saved key cannot be read.
                    self._loaded = False
                except (ValueError, UnicodeError):
                    self._memory, self._source = "", "unavailable"
            return self._memory

    def status(self):
        """Connection metadata only: never return the key to a UI or log."""
        with self._lock:
            configured = bool(self.token())
            try:
                environment = bool(validated_token(os.environ.get("REGICIDE_LICHESS_TOKEN", "")))
            except ValueError:
                environment = False
            source = "environment" if environment else self._source
            messages = {
                "stored": "저장된 리체스 연결키를 자동으로 불러왔습니다. 다시 입력할 필요가 없습니다.",
                "session": "연결키는 이번 실행에서만 사용합니다. 다음 실행에도 쓰려면 연결 기억을 선택해 저장해 주세요.",
                "environment": "이 컴퓨터에 설정된 리체스 연결키를 사용합니다. 다시 입력할 필요가 없습니다.",
                "missing": "처음 한 번만 연결하면 다음 실행부터 저장된 연결을 자동으로 사용합니다.",
                "unavailable": "저장된 리체스 연결키를 불러오지 못했습니다. 연결 설정을 확인해 주세요.",
            }
            return {"configured": configured, "source": source,
                    "remembered": source == "stored", "message": messages[source]}

    def set(self, token, remember=True):
        token = validated_token(token)
        if type(remember) is not bool:
            raise ValueError("토큰 저장 여부는 true 또는 false로 지정해 주세요.")
        with self._lock:
            self._memory, self._loaded = token, True
            self._session_only = True
            self._source = "session" if token else "missing"
            if not token or not remember:
                # An explicit memory-only replacement must not resurrect the
                # old saved value on the next launch.
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    return self._message("토큰은 이번 실행에 적용했습니다. 이전 저장 정보는 지우지 못했습니다.")
                if not token:
                    self._session_only, self._loaded = False, False
                    return self._message("저장된 토큰을 지웠습니다.")
                return self._message("토큰은 이번 실행에서만 사용합니다.")
            temporary = None
            try:
                protected = _dpapi(token.encode("utf-8"))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
                with temporary.open("xb") as stream:
                    stream.write(protected)
                os.replace(temporary, self.path)
                self._session_only, self._loaded = False, False
                self._source = "stored"
                return self._message("리체스 연결을 저장했습니다. 다음 실행부터 자동으로 불러옵니다.")
            except OSError:
                # Keep an existing saved key intact if its replacement cannot
                # be encrypted or committed; the new key stays session-only.
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
