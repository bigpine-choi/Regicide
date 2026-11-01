"""Position statistics for analysis; never involved in engine move selection.

``local`` indexes completed, non-handicap PGN main lines below ``game_dir``.
Unchanged files are not parsed again. A position contributes at most once per
game, at its first occurrence; positions include turn, castling and *legal*
en-passant rights, but not move counters. Terminal positions also contribute.
Separate PGN records are separate games; identical copied files are not
deduplicated across the corpus.

``masters`` uses the authenticated Lichess Opening Explorer. The optional
``fetcher(url, headers, timeout) -> dict`` injection replaces HTTP for tests;
it may raise urllib.error.HTTPError to simulate authentication/rate limits.
No token, player names, local paths, or complete PGNs are returned to clients.
"""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import io
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

import chess
import chess.pgn


_RESULTS = {"1-0": "white", "1/2-1/2": "draws", "0-1": "black"}
_COUNTS = ("white", "draws", "black")
_LABELS = {"local": "내 완료 기보", "masters": "Lichess 마스터"}
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PLIES = 2048


def _position_key(board):
    # A non-capturable ep target does not change the legal position. Retaining
    # castling rights and the turn prevents incorrect transposition matches.
    return " ".join(board.fen(en_passant="legal").split()[:4])


def _counts():
    return {name: 0 for name in _COUNTS}


def _percent(numerator, denominator):
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _decorate(counts):
    total = sum(counts[name] for name in _COUNTS)
    return {**counts, "total": total,
            "white_pct": _percent(counts["white"], total),
            "draw_pct": _percent(counts["draws"], total),
            "black_pct": _percent(counts["black"], total)}


def _signature(path):
    info = path.stat()
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass
class _IndexedFile:
    signature: tuple
    positions: dict
    games: int
    excluded: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a Bearer token to an unexpected redirect destination.
        return None


class OpeningExplorer:
    def __init__(self, game_dir: Path, token=None, fetcher=None, *, cache_size=128,
                 cache_ttl=86400.0, timeout=8.0):
        self.game_dir = Path(game_dir).expanduser().resolve()
        self._token = token
        self._fetcher = fetcher or self._fetch
        self._cache_size = max(1, min(int(cache_size), 1024))
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._timeout = max(0.1, min(float(timeout), 30.0))
        self._local_lock = threading.Lock()
        self._files = {}
        self._positions = {}
        self._games = 0
        self._excluded = 0
        self._masters_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._cache = OrderedDict()
        self._pending = {}
        self._retry_at = 0.0

    @staticmethod
    def _response(fen, source, status, message, *, counts=None, moves=None, opening=None, **extra):
        # None percentages distinguish an absent sample from an observed 0%.
        return {"fen": fen, "status": status, "source": source,
                "source_label": _LABELS.get(source, "알 수 없는 출처"),
                **_decorate(counts or _counts()), "moves": moves or [],
                "opening": opening, "message": message, **extra}

    def query(self, fen: str, source: str = "local") -> dict:
        """Return a detached, JSON-safe statistics snapshot for one position."""
        try:
            if not isinstance(fen, str) or len(fen) > 200:
                raise ValueError("invalid FEN")
            board = chess.Board(fen)
            if not board.is_valid():
                raise ValueError("invalid position")
            normalized = board.fen(en_passant="fen")
        except (ValueError, TypeError, IndexError):
            return self._response(fen if isinstance(fen, str) else "", source, "unavailable",
                                  "올바른 체스 포지션을 입력해 주세요.")
        if source == "local":
            return self._local(board, normalized)
        if source == "masters":
            return self._masters(board, normalized)
        return self._response(normalized, source, "unavailable", "지원하지 않는 통계 출처입니다.")

    @staticmethod
    def _is_handicap(game):
        handicap = game.headers.get("HandicapMoves", "0").strip()
        if handicap not in ("", "0"):
            return True
        if game.headers.get("Handicap", "").strip().lower() not in ("", "0", "false", "none"):
            return True
        return any("user-forced regicide move" in node.comment.lower()
                   for node in game.mainline())

    @staticmethod
    def _parse_file(path, signature):
        positions = {}
        games = excluded = 0
        if signature[1] > _MAX_FILE_BYTES:
            return _IndexedFile(signature, {}, 0, 1)
        # Decode only PGN text, never sibling JSON metadata or credentials.
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        if _signature(path) != signature:
            raise OSError("PGN changed while reading")
        stream = io.StringIO(text)
        while True:
            try:
                game = chess.pgn.read_game(stream)
            except (ValueError, IndexError, TypeError):
                excluded += 1
                break
            if game is None:
                break
            result = _RESULTS.get(game.headers.get("Result"))
            if (result is None or game.errors or OpeningExplorer._is_handicap(game)
                    or game.headers.get("Variant", "Standard").lower() not in
                    ("standard", "chess", "normal")):
                excluded += 1
                continue
            try:
                board = game.board()
                if not board.is_valid() or board.chess960:
                    raise ValueError("invalid initial position")
                seen = set()
                entries = []
                for ply, move in enumerate(game.mainline_moves()):
                    if ply >= _MAX_PLIES or move not in board.legal_moves:
                        raise ValueError("invalid or excessive main line")
                    key = _position_key(board)
                    if key not in seen:
                        entries.append((key, move.uci(), board.san(move)))
                        seen.add(key)
                    board.push(move)
                key = _position_key(board)
                if key not in seen:
                    entries.append((key, None, None))
            except (ValueError, IndexError, AssertionError):
                excluded += 1
                continue
            for key, uci, san in entries:
                entry = positions.setdefault(key, {**_counts(), "moves": {}})
                entry[result] += 1
                if uci is not None:
                    row = entry["moves"].setdefault(uci, {**_counts(), "san": san})
                    row[result] += 1
            games += 1
        return _IndexedFile(signature, positions, games, excluded)

    def _merge_file(self, record, sign):
        self._games += sign * record.games
        self._excluded += sign * record.excluded
        for key, contribution in record.positions.items():
            entry = self._positions.setdefault(key, {**_counts(), "moves": {}})
            for name in _COUNTS:
                entry[name] += sign * contribution[name]
            for uci, values in contribution["moves"].items():
                row = entry["moves"].setdefault(uci, {**_counts(), "san": values["san"]})
                for name in _COUNTS:
                    row[name] += sign * values[name]
                if not sum(row[name] for name in _COUNTS):
                    del entry["moves"][uci]
            if not sum(entry[name] for name in _COUNTS):
                del self._positions[key]

    def _refresh_local(self):
        # Metadata-only traversal; replay just added/modified files. Cached
        # contributions are subtracted on modifications or deletion.
        present = set()
        for path in self.game_dir.rglob("*.pgn"):
            try:
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.game_dir):
                    continue
                signature = _signature(path)
                present.add(path)
                previous = self._files.get(path)
                if previous is not None and previous.signature == signature:
                    continue
                record = self._parse_file(path, signature)
                if previous is not None:
                    self._merge_file(previous, -1)
                self._files[path] = record
                self._merge_file(record, 1)
            except OSError:
                # Do not retain stale contributions for a file being rewritten.
                previous = self._files.pop(path, None)
                if previous is not None:
                    self._merge_file(previous, -1)
        for path in self._files.keys() - present:
            self._merge_file(self._files.pop(path), -1)

    def _local(self, board, fen):
        with self._local_lock:
            try:
                self._refresh_local()
            except OSError:
                return self._response(fen, "local", "unavailable", "기보 통계를 읽을 수 없습니다.")
            entry = self._positions.get(_position_key(board))
            counts = {name: entry[name] for name in _COUNTS} if entry else _counts()
            total = sum(counts.values())
            moves = []
            if entry:
                for uci, row in entry["moves"].items():
                    values = _decorate({name: row[name] for name in _COUNTS})
                    moves.append({"uci": uci, "san": row["san"], **values,
                                  "frequency_pct": _percent(values["total"], total)})
                moves.sort(key=lambda row: (-row["total"], row["san"]))
            return self._response(fen, "local", "ready" if total else "empty",
                                  "완료 기보 기준입니다. 미완료·핸디캡 대국은 제외합니다."
                                  if total else "이 포지션에 해당하는 완료 기보가 없습니다.",
                                  counts=counts, moves=moves, indexed_games=self._games,
                                  indexed_files=len(self._files), excluded_games=self._excluded)

    @staticmethod
    def _fetch(url, headers, timeout):
        request = urllib.request.Request(url, headers=headers)
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("excessive response")
        return json.loads(body)

    @staticmethod
    def _validated_payload(payload, board):
        def counts(item):
            result = {}
            for name in _COUNTS:
                value = item[name]
                if type(value) is not int or not 0 <= value <= 10 ** 18:
                    raise ValueError("invalid count")
                result[name] = value
            return result

        if not isinstance(payload, dict):
            raise ValueError("invalid response")
        values = counts(payload)
        total = sum(values.values())
        candidates = payload.get("moves")
        if not isinstance(candidates, list) or len(candidates) > 256:
            raise ValueError("invalid candidates")
        moves, seen = [], set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("invalid candidate")
            move = chess.Move.from_uci(candidate["uci"])
            if move not in board.legal_moves or move.uci() in seen:
                raise ValueError("illegal or duplicated candidate")
            seen.add(move.uci())
            row = _decorate(counts(candidate))
            if row["total"] > total or any(row[name] > values[name] for name in _COUNTS):
                raise ValueError("inconsistent sample")
            if not row["total"]:
                continue
            moves.append({"uci": move.uci(), "san": board.san(move), **row,
                          "frequency_pct": _percent(row["total"], total)})
        if (sum(row["total"] for row in moves) > total
                or any(sum(row[name] for row in moves) > values[name] for name in _COUNTS)):
            raise ValueError("inconsistent candidates")
        moves.sort(key=lambda row: (-row["total"], row["san"]))
        opening = payload.get("opening")
        if (not isinstance(opening, dict) or not isinstance(opening.get("name"), str)
                or not 0 < len(opening["name"]) <= 200
                or not isinstance(opening.get("eco"), str)
                or re.fullmatch(r"[A-E][0-9]{2}", opening["eco"]) is None):
            opening = None
        else:
            opening = {"name": opening["name"], "eco": opening["eco"]}
        return values, moves, opening

    def _rate_limited(self, fen):
        return self._response(fen, "masters", "rate_limited",
                              "통계 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.",
                              retry_after_seconds=max(1, math.ceil(self._retry_at - time.monotonic())))

    def _masters(self, board, fen):
        token = self._token if self._token is not None else os.environ.get("REGICIDE_LICHESS_TOKEN", "")
        if not isinstance(token, str) or not token.strip():
            return self._response(fen, "masters", "auth_required",
                                  "마스터 통계는 Lichess 토큰 설정이 필요합니다.")
        token = token.strip()
        # Tokens never enter result snapshots, cache keys, URLs or error text.
        key = _position_key(board)
        while True:
            with self._masters_lock:
                cached = self._cache.get(key)
                if cached and cached[0] > time.monotonic():
                    self._cache.move_to_end(key)
                    result = deepcopy(cached[1])
                    result["fen"] = fen
                    result["cached"] = True
                    return result
                if cached:
                    del self._cache[key]
                if self._retry_at > time.monotonic():
                    return self._rate_limited(fen)
                waiting = self._pending.get(key)
                if waiting is None:
                    event = self._pending[key] = threading.Event()
                    break
            if not waiting.wait(self._timeout + 2.0):
                return self._response(fen, "masters", "unavailable", "통계 조회가 지연되고 있습니다.")

        try:
            with self._request_lock:
                with self._masters_lock:
                    limited = self._retry_at > time.monotonic()
                if limited:
                    result = self._rate_limited(fen)
                else:
                    query = urllib.parse.urlencode({"fen": fen, "moves": 12, "topGames": 0})
                    payload = self._fetcher("https://explorer.lichess.org/masters?" + query,
                                            {"Authorization": "Bearer " + token,
                                             "Accept": "application/json", "User-Agent": "Regicide/OpeningExplorer"},
                                            self._timeout)
                    counts, moves, opening = self._validated_payload(payload, board)
                    result = self._response(fen, "masters", "ready" if sum(counts.values()) else "empty",
                                            "Lichess 마스터 기보의 실제 결과입니다. 엔진 평가와 별개입니다."
                                            if sum(counts.values()) else "마스터 데이터베이스에 해당 기보가 없습니다.",
                                            counts=counts, moves=moves, opening=opening, cached=False,
                                            source_url="https://lichess.org/analysis#explorer")
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                result = self._response(fen, "masters", "auth_required", "Lichess 토큰을 확인해 주세요.")
            elif error.code == 429:
                delay = 60.0
                try:
                    delay = max(delay, min(float(error.headers.get("Retry-After", "60")), 86400.0))
                except (ValueError, TypeError, AttributeError):
                    pass
                with self._masters_lock:
                    self._retry_at = time.monotonic() + delay
                result = self._rate_limited(fen)
            else:
                result = self._response(fen, "masters", "unavailable", "마스터 통계 서버에 연결할 수 없습니다.")
        except Exception:
            # Remote exception messages can contain credentials or URLs. Do not
            # print or propagate them into the UI, logs, or saved game files.
            result = self._response(fen, "masters", "unavailable", "마스터 통계를 가져오지 못했습니다.")
        finally:
            with self._masters_lock:
                if "result" in locals() and result["status"] != "rate_limited":
                    ttl = self._cache_ttl if result["status"] in ("ready", "empty") else 3.0
                    self._cache[key] = (time.monotonic() + ttl, deepcopy(result))
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                self._pending.pop(key, None)
                event.set()
        return result
