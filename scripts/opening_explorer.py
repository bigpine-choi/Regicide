"""Read-only public game statistics, isolated from Regicide's move selection.

Official API: https://lichess.org/api#tag/Opening-Explorer
Supports masters and standard rated Lichess games. Lichess rating groups are
0, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500; each starts a bucket ending
at the next group's lower bound. Speeds: ultraBullet, bullet, blitz, rapid,
classical, correspondence. Masters ignores rating and speed filters.

Queries return immediately. One daemon worker serializes external requests and
keeps only the newest pending position. Tokens never enter snapshots or caches.
"""

from collections import OrderedDict
import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import chess

from lichess_credentials import LichessCredentials, user_data_directory


RATINGS = (0, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500)
SPEEDS = ("ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence")
DEFAULT_RATINGS = (1600, 1800, 2000, 2200, 2500)
DEFAULT_SPEEDS = ("rapid", "classical")
API_ORIGIN = "https://explorer.lichess.org"
MAX_BODY_BYTES = 1024 * 1024
MEMORY_ENTRIES = 128
DISK_ENTRIES = 128
DISK_BYTES = 8 * 1024 * 1024
CACHE_SECONDS = 24 * 60 * 60
CACHE_VERSION = 1


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization must never follow a redirect to another host.
        return None


def _context(fen, source, ratings, speeds):
    if source not in ("masters", "lichess"):
        raise ValueError("자료는 masters 또는 lichess를 선택해 주세요.")
    if not isinstance(fen, str) or not 0 < len(fen) <= 200 or len(fen.split()) != 6:
        raise ValueError("표준 체스 FEN의 6개 항목을 입력해 주세요.")
    try:
        board = chess.Board(fen)
        if not board.is_valid() or board.halfmove_clock > 1_000_000_000 or board.fullmove_number > 1_000_000_000:
            raise ValueError()
    except (ValueError, IndexError):
        raise ValueError("통계를 조회할 수 있는 합법적인 체스 배치를 입력해 주세요.") from None
    if source == "masters":
        ratings, speeds = [], []
    else:
        ratings = DEFAULT_RATINGS if ratings is None else ratings
        speeds = DEFAULT_SPEEDS if speeds is None else speeds
        if (not isinstance(ratings, (list, tuple)) or not 1 <= len(ratings) <= len(RATINGS)
                or any(type(value) is not int or value not in RATINGS for value in ratings)):
            raise ValueError("지원하는 레이팅 구간을 하나 이상 선택해 주세요.")
        if (not isinstance(speeds, (list, tuple)) or not 1 <= len(speeds) <= len(SPEEDS)
                or any(not isinstance(value, str) or value not in SPEEDS for value in speeds)):
            raise ValueError("지원하는 시간제를 하나 이상 선택해 주세요.")
        ratings, speeds = sorted(set(ratings)), sorted(set(speeds))
    position = " ".join(board.fen(en_passant="legal").split()[:4])
    identity = [position, source, ratings, speeds]
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"fen": board.fen(en_passant="fen"), "position": position,
            "source": source, "ratings": list(ratings), "speeds": list(speeds), "request_key": key}


def _count(value):
    if type(value) is not int or not 0 <= value <= 10 ** 14:
        raise ValueError("invalid statistic")
    return value


def _rating(value):
    return value if type(value) is int and 0 <= value <= 5000 else None


def _name(value):
    if not isinstance(value, str) or not 0 < len(value) <= 200 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid name")
    return value


def _normalise(payload, context):
    if not isinstance(payload, dict):
        raise ValueError("invalid statistics")
    white, draws, black = (_count(payload.get(key)) for key in ("white", "draws", "black"))
    total = white + draws + black
    raw_moves = payload.get("moves")
    if not isinstance(raw_moves, list) or len(raw_moves) > 256:
        raise ValueError("invalid moves")
    board = chess.Board(context["fen"])
    moves, seen, used = [], set(), [0, 0, 0]
    for row in raw_moves:
        try:
            if not isinstance(row, dict) or not isinstance(row.get("uci"), str):
                continue
            uci = row["uci"]
            if not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", uci) or uci in seen:
                continue
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                continue
            counts = [_count(row.get(key)) for key in ("white", "draws", "black")]
            count = sum(counts)
            if not count or any(counts[index] + used[index] > limit for index, limit in enumerate((white, draws, black))):
                continue
            after = board.copy(stack=False)
            after.push(move)
            moves.append({"uci": uci, "san": board.san(move), "count": count,
                          "play_pct": 100.0 * count / total if total else 0.0,
                          "white": counts[0], "draws": counts[1], "black": counts[2],
                          "white_pct": 100.0 * counts[0] / count,
                          "draw_pct": 100.0 * counts[1] / count,
                          "black_pct": 100.0 * counts[2] / count,
                          "average_rating": _rating(row.get("averageRating")),
                          "fen_after": after.fen(en_passant="fen")})
            seen.add(uci)
            used = [used[index] + counts[index] for index in range(3)]
        except (ValueError, TypeError, KeyError):
            continue
    moves.sort(key=lambda row: (-row["count"], row["san"]))
    games, ids = [], set()
    for field in ("topGames", "recentGames"):
        raw_games = payload.get(field, [])
        if not isinstance(raw_games, list):
            continue
        for game in raw_games[:30]:
            try:
                if not isinstance(game, dict):
                    continue
                game_id = game.get("id")
                if not isinstance(game_id, str) or not re.fullmatch(r"[a-zA-Z0-9]{8}", game_id) or game_id in ids:
                    continue
                if game.get("winner") not in (None, "white", "black"):
                    continue
                white_player, black_player = game["white"], game["black"]
                if not isinstance(white_player, dict) or not isinstance(black_player, dict):
                    continue
                year = game.get("year")
                year = year if type(year) is int and 1000 <= year <= 3000 else None
                games.append({"id": game_id, "white": _name(white_player.get("name")),
                              "black": _name(black_player.get("name")),
                              "white_rating": _rating(white_player.get("rating")),
                              "black_rating": _rating(black_player.get("rating")),
                              "winner": game.get("winner"), "year": year,
                              "url": ("https://lichess.org/import/master/" + game_id + "/white"
                                      if context["source"] == "masters" else "https://lichess.org/" + game_id)})
                ids.add(game_id)
            except (ValueError, KeyError, TypeError):
                continue
    return {"total": total, "white": white, "draws": draws, "black": black,
            "moves": moves, "games": games[:15]}


def _base(context=None):
    context = context or {"request_key": None, "fen": None, "source": "masters", "ratings": [], "speeds": []}
    return {**{key: context[key] for key in ("request_key", "fen", "source", "ratings", "speeds")},
            "status": "idle", "configured": False, "message": "자료를 선택하고 통계 조회를 눌러 주세요.",
            "total": 0, "white": 0, "draws": 0, "black": 0, "moves": [], "games": [],
            "fetched_at": None, "cached": False, "retry_after": 0}


class OpeningExplorer:
    def __init__(self, cache_dir=None, credentials=None, opener=None, *, timeout=8.0,
                 min_interval=1.0, clock=None, monotonic=None):
        self._condition = threading.Condition()
        self._credentials = credentials if credentials is not None else LichessCredentials()
        self._cache_dir = Path(cache_dir) if cache_dir is not None else user_data_directory() / "opening-explorer"
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self._timeout = max(0.1, min(float(timeout), 30.0))
        self._min_interval = max(0.0, float(min_interval))
        self._clock, self._monotonic = clock or time.time, monotonic or time.monotonic
        self._cache = OrderedDict()
        self._context, self._pending, self._worker = None, None, None
        self._closed, self._generation = False, 0
        self._blocked_until, self._next_request = 0.0, 0.0
        self._snapshot = _base()

    def _copy_locked(self):
        result = copy.deepcopy(self._snapshot)
        result["configured"] = bool(self._credentials.token())
        if result["status"] == "rate_limited":
            result["retry_after"] = max(0, math.ceil(self._blocked_until - self._monotonic()))
        return result

    def snapshot(self):
        with self._condition:
            return self._copy_locked()

    def set_token(self, token, remember=True):
        with self._condition:
            if self._closed:
                raise ValueError("오프닝 통계 화면이 종료됐습니다.")
            message = self._credentials.set(token, remember)
            self._generation += 1
            self._pending = None
            self._snapshot = _base(self._context)
            self._snapshot["message"] = message
            self._condition.notify_all()
            return self._copy_locked()

    def query(self, fen, source="masters", ratings=None, speeds=None):
        context = _context(fen, source, ratings, speeds)
        with self._condition:
            if self._closed:
                raise ValueError("오프닝 통계 화면이 종료됐습니다.")
            if (self._context and self._context["request_key"] == context["request_key"]
                    and self._snapshot["status"] == "loading"):
                self._context = context
                self._snapshot["fen"] = context["fen"]
                return self._copy_locked()
            self._generation += 1
            self._context, self._pending = context, None
            self._snapshot = _base(context)
            cached = self._cached_locked(context)
            if cached is not None:
                self._snapshot = self._ready(context, cached, cached=True)
            elif self._monotonic() < self._blocked_until:
                self._snapshot.update(status="rate_limited", message="Lichess 요청 한도에 도달했습니다. 표시된 시간 뒤 다시 조회해 주세요.")
            elif not self._credentials.token():
                self._snapshot.update(status="auth_required", message="Lichess 개인 API 토큰을 연결해 주세요. 별도 권한은 필요하지 않습니다.")
            else:
                self._snapshot.update(status="loading", message="Lichess의 실제 대국 통계를 불러오고 있습니다.")
                self._pending = (self._generation, context)
                if self._worker is None:
                    self._worker = threading.Thread(target=self._run, name="regicide-opening-explorer", daemon=True)
                    self._worker.start()
            self._condition.notify_all()
            return self._copy_locked()

    def close(self):
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending = None
            self._snapshot = _base(self._context)
            self._snapshot["message"] = "오프닝 통계 조회를 종료했습니다."
            self._condition.notify_all()

    def _ready(self, context, entry, *, cached):
        result = _base(context)
        result.update(copy.deepcopy(entry["data"]))
        # A cache key ignores FEN counters; derive every child from this caller's
        # actual FEN so clocks/move numbers do not come from a previous viewer.
        board = chess.Board(context["fen"])
        for move in result["moves"]:
            after = board.copy(stack=False)
            after.push_uci(move["uci"])
            move["fen_after"] = after.fen(en_passant="fen")
        result.update(status="ready", cached=cached, fetched_at=entry["fetched_at"],
                      message=("저장된 대국 통계입니다." if cached else "실제 대국 통계입니다. 엔진의 최선수나 예상 승률을 뜻하지 않습니다."))
        return result

    def _cached_locked(self, context):
        key, now = context["request_key"], self._clock()
        entry = self._cache.get(key)
        if entry is not None and 0 <= now - entry["timestamp"] < CACHE_SECONDS:
            self._cache.move_to_end(key)
            return entry
        self._cache.pop(key, None)
        try:
            path = self._cache_dir / (key + ".json")
            if path.stat().st_size > MAX_BODY_BYTES:
                return None
            with path.open("rb") as stream:
                raw = stream.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                return None
            saved = json.loads(raw.decode("utf-8"))
            if not isinstance(saved, dict):
                return None
            timestamp = saved.get("timestamp")
            if (saved.get("version") != CACHE_VERSION or saved.get("request_key") != key
                    or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                    or not 0 <= now - timestamp < CACHE_SECONDS):
                return None
            # Revalidate disk data just like network input. Only raw public
            # statistics and a timestamp are persisted, never a token/header.
            data = _normalise(saved["payload"], context)
            entry = {"timestamp": timestamp,
                     "fetched_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                     "data": data}
            self._remember_locked(key, entry)
            return entry
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
            return None

    def _remember_locked(self, key, entry):
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > MEMORY_ENTRIES:
            self._cache.popitem(last=False)

    def _save_cache(self, context, entry):
        data = entry["data"]
        payload = {key: data[key] for key in ("white", "draws", "black")}
        payload["moves"] = [{"uci": row["uci"], "white": row["white"], "draws": row["draws"],
                             "black": row["black"], "averageRating": row["average_rating"]} for row in data["moves"]]
        payload["topGames"] = [{"id": row["id"], "white": {"name": row["white"], "rating": row["white_rating"]},
                                "black": {"name": row["black"], "rating": row["black_rating"]},
                                "winner": row["winner"], "year": row["year"]} for row in data["games"]]
        document = {"version": CACHE_VERSION, "request_key": context["request_key"],
                    "timestamp": entry["timestamp"], "payload": payload}
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_BODY_BYTES:
            return
        temporary = None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._cache_dir / (context["request_key"] + ".json")
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            with temporary.open("xb") as stream:
                stream.write(encoded)
            os.replace(temporary, path)
            files = []
            for candidate in self._cache_dir.glob("*.json"):
                if re.fullmatch(r"[0-9a-f]{64}\.json", candidate.name):
                    stat = candidate.stat()
                    files.append((stat.st_mtime, candidate, stat.st_size))
            files.sort(key=lambda row: row[0], reverse=True)
            total = 0
            for index, (_, candidate, size) in enumerate(files):
                total += size
                if index >= DISK_ENTRIES or total > DISK_BYTES:
                    candidate.unlink(missing_ok=True)
        except OSError:
            pass  # Optional cache failures must not prevent looking at games.
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _fetch(self, context, token):
        parameters = {"fen": context["position"] + " 0 1", "moves": "30", "topGames": "4"}
        if context["source"] == "lichess":
            parameters.update(variant="standard", ratings=",".join(map(str, context["ratings"])),
                              speeds=",".join(context["speeds"]), recentGames="4")
        request = urllib.request.Request(API_ORIGIN + "/" + context["source"] + "?" + urllib.parse.urlencode(parameters),
                                         headers={"Accept": "application/json", "Authorization": "Bearer " + token,
                                                  "User-Agent": "Regicide-OpeningExplorer/1.0"})
        with self._open(request, timeout=self._timeout) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                raise ValueError("invalid response length")
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise ValueError("response too large")
            return _normalise(json.loads(raw.decode("utf-8")), context)

    def _retry_delay(self, headers):
        value = headers.get("Retry-After", "") if headers is not None else ""
        try:
            delay = int(value)
        except (ValueError, TypeError):
            try:
                delay = math.ceil(parsedate_to_datetime(value).timestamp() - self._clock())
            except (ValueError, TypeError, OverflowError):
                delay = 60
        return max(60, min(delay, 24 * 60 * 60))

    def _run(self):
        while True:
            with self._condition:
                while not self._closed and self._pending is None:
                    self._condition.wait()
                if self._closed:
                    return
                remaining = max(self._next_request, self._blocked_until) - self._monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=min(remaining, 1.0))
                    continue
                generation, context = self._pending
                self._pending = None
                token = self._credentials.token()
                if not token:
                    if generation == self._generation:
                        self._snapshot.update(status="auth_required", message="Lichess API 토큰을 연결해 주세요.")
                    continue
                self._next_request = self._monotonic() + self._min_interval
            data, error, retry = None, None, 0
            try:
                data = self._fetch(context, token)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    error = ("auth_required", "Lichess가 토큰을 승인하지 않았습니다. 새 토큰과 환경변수 설정을 확인해 주세요.")
                elif exc.code == 429:
                    retry = self._retry_delay(exc.headers)
                    error = ("rate_limited", "Lichess 요청 한도에 도달했습니다. 표시된 시간 뒤 다시 조회해 주세요.")
                else:
                    error = ("error", "Lichess 통계 서버에서 자료를 받지 못했습니다. 잠시 뒤 다시 조회해 주세요.")
                exc.close()
            except (OSError, ValueError, UnicodeError, urllib.error.URLError,
                    http.client.HTTPException, RecursionError):
                error = ("error", "통계를 불러오지 못했습니다. 인터넷 연결을 확인한 뒤 다시 조회해 주세요.")
            # Never interpolate exceptions: libraries may include request URLs,
            # headers, or credentials in their diagnostic strings.
            with self._condition:
                if retry:
                    self._blocked_until = max(self._blocked_until, self._monotonic() + retry)
                    if self._pending is not None:
                        self._pending = None
                        self._snapshot.update(status="rate_limited", message=error[1])
                if self._closed:
                    return
                if generation != self._generation:
                    continue
                if error is not None:
                    self._snapshot.update(status=error[0], message=error[1])
                    continue
                timestamp = self._clock()
                entry = {"data": data, "timestamp": timestamp,
                         "fetched_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}
                self._remember_locked(context["request_key"], entry)
                self._snapshot = self._ready(self._context, entry, cached=False)
            self._save_cache(context, entry)
