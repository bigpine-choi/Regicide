"""Persistent, read-only human-game library and explicit legacy imports.

Saved JSON is the source of truth. Boards, SAN and PGN are reconstructed by
legally replaying its moves, so a missing PGN or a stale presentation snapshot
cannot corrupt the viewer or change the game currently being played.
"""

from collections import OrderedDict
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading

import chess
import chess.pgn

from opening_presentation import OpeningGuideCache


MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_PLIES = 10_000
GAME_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
RESULTS = {"*", "1-0", "0-1", "1/2-1/2"}


def default_game_directory():
    """Use a user-owned data folder independent of a repository/build folder."""
    if os.name == "nt":
        # CSIDL_PERSONAL resolves redirected/OneDrive Documents as well.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        try:
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value) / "Regicide" / "games"
        except (AttributeError, OSError):
            pass
        return Path.home() / "Documents" / "Regicide" / "games"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Regicide" / "games"
    configured = os.environ.get("XDG_DATA_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local" / "share"
    return base / "regicide" / "games"


def _text(value, default="", limit=1000):
    if not isinstance(value, str):
        return default
    return "".join(character for character in value[:limit]
                   if ord(character) >= 32 and not 0xD800 <= ord(character) <= 0xDFFF)


def _stamp(value):
    value = _text(value, limit=100)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc).isoformat(), parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return value, 0.0


def _fingerprint(record):
    # A repeated import of a conflict must find its previous imported copy.
    payload = {key: value for key, value in record.items()
               if key not in ("game_id", "original_game_id", "imported_at")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class GameArchive:
    def __init__(self, save_dir):
        self.save_dir = Path(save_dir).expanduser().resolve()
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self._opening_guide = OpeningGuideCache()

    @staticmethod
    def _check_id(game_id):
        if not isinstance(game_id, str) or GAME_ID_PATTERN.fullmatch(game_id) is None:
            raise ValueError("올바른 대국 번호를 선택해 주세요.")
        return game_id

    @staticmethod
    def _safe_stat(path, directory):
        try:
            if path.is_symlink() or path.resolve().parent != directory:
                raise ValueError("대국 보관함 밖의 파일은 읽을 수 없습니다.")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
                raise ValueError("대국 기록의 파일 형식이나 크기가 올바르지 않습니다.")
            return info
        except (OSError, RuntimeError) as exc:
            raise ValueError("대국 기록을 읽을 수 없습니다.") from exc

    @staticmethod
    def _stat_key(info):
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino

    @classmethod
    def _read_json(cls, path, directory):
        cls._safe_stat(path, directory)
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
                    raise ValueError("대국 기록의 크기가 너무 큽니다.")
                raw = stream.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError("대국 기록의 크기가 너무 큽니다.")
            record = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(record, dict):
                raise ValueError("대국 기록 형식이 올바르지 않습니다.")
            return record, cls._stat_key(info)
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("대국 기록을 읽을 수 없습니다.") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    def _prepare(cls, record, game_id):
        cls._check_id(game_id)
        if record.get("game_id") != game_id:
            raise ValueError("대국 번호와 기록 파일이 일치하지 않습니다.")
        initial_fen = record.get("initial_fen", chess.STARTING_FEN)
        uci_moves = record.get("uci_moves")
        state = record.get("state")
        if not isinstance(initial_fen, str) or len(initial_fen) > 256 or not isinstance(state, dict):
            raise ValueError("대국 기록 형식이 올바르지 않습니다.")
        if not isinstance(uci_moves, list) or len(uci_moves) > MAX_PLIES:
            raise ValueError("대국의 수 목록이 올바르지 않습니다.")
        human_color = state.get("human_color")
        if human_color not in ("white", "black"):
            raise ValueError("대국의 사용자 색상이 올바르지 않습니다.")
        human_side = human_color == "white"
        try:
            board = chess.Board(initial_fen)
            if not board.is_valid():
                raise ValueError("유효하지 않은 초기 체스판입니다.")
            metadata = state.get("moves", [])
            if not isinstance(metadata, list):
                metadata = []
            moves = []
            for ply, uci in enumerate(uci_moves, 1):
                if not isinstance(uci, str) or len(uci) not in (4, 5):
                    raise ValueError("올바르지 않은 수가 포함된 대국입니다.")
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    raise ValueError("불법 수가 포함된 대국입니다.")
                actor = "human" if board.turn == human_side else "engine"
                entry = {"ply": ply, "uci": uci, "san": board.san(move), "actor": actor}
                previous = metadata[ply - 1] if ply <= len(metadata) else None
                if (isinstance(previous, dict) and previous.get("uci") == uci
                        and previous.get("forced") is True and actor == "engine"):
                    entry["forced"] = True
                moves.append(entry)
                board.push(move)
        except (ValueError, TypeError, IndexError) as exc:
            raise ValueError("합법적으로 재생할 수 없는 대국 기록입니다.") from exc
        result = state.get("result", "*")
        if not isinstance(result, str) or result not in RESULTS:
            raise ValueError("대국 결과가 올바르지 않습니다.")
        outcome = board.outcome(claim_draw=True)
        termination = _text(state.get("termination"), limit=100) or None
        if outcome is not None:
            if result not in ("*", outcome.result()):
                raise ValueError("체스판과 대국 결과가 일치하지 않습니다.")
            result = outcome.result()
            termination = outcome.termination.name.lower()
        elif result != "*" and termination != "resignation":
            raise ValueError("종료 근거가 없는 대국 결과입니다.")
        engine = state.get("engine", {})
        if not isinstance(engine, dict):
            engine = {}
        engine_name = _text(engine.get("name"), "Regicide", 200)
        movetime_ms = engine.get("movetime_ms")
        if type(movetime_ms) is not int or not 0 <= movetime_ms <= 86_400_000:
            movetime_ms = None
        started_at, started_sort = _stamp(record.get("started_at"))
        updated_at, _ = _stamp(record.get("updated_at", started_at))
        ended_at, _ = _stamp(record.get("ended_at"))
        return {"record": record, "board": board, "moves": moves,
                "game_id": game_id, "human_color": human_color,
                "result": result, "termination": termination,
                "engine_name": engine_name, "movetime_ms": movetime_ms,
                "started_at": started_at, "started_sort": started_sort,
                "updated_at": updated_at, "ended_at": ended_at or None,
                "error": bool(state.get("error"))}

    def _load(self, game_id):
        game_id = self._check_id(game_id)
        path = self.save_dir / (game_id + ".json")
        info = self._safe_stat(path, self.save_dir)
        key = self._stat_key(info)
        cached = self._cache.get(game_id)
        if cached is not None and cached[0] == key:
            self._cache.move_to_end(game_id)
            if isinstance(cached[1], str):
                raise ValueError(cached[1])
            return cached[1]
        try:
            record, key = self._read_json(path, self.save_dir)
            prepared = self._prepare(record, game_id)
        except ValueError as exc:
            self._cache[game_id] = (key, str(exc))
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
            raise
        self._cache[game_id] = (key, prepared)
        self._cache.move_to_end(game_id)
        while len(self._cache) > 128:
            self._cache.popitem(last=False)
        return prepared

    @staticmethod
    def _summary(prepared, current_game_id=None):
        record = prepared["record"]
        current = prepared["game_id"] == current_game_id
        if prepared["result"] != "*":
            record_status = "completed"
        elif prepared["error"] or record.get("record_status") == "error":
            record_status = "error"
        elif current and record.get("record_status") != "unfinished":
            record_status = "in_progress"
        else:
            record_status = "unfinished"
        if record_status == "completed":
            if prepared["result"] == "1/2-1/2":
                status = "무승부"
            else:
                won = (prepared["result"] == "1-0") == (prepared["human_color"] == "white")
                status = "사용자 승리" if won else "Regicide 승리"
        else:
            status = {"in_progress": "진행 중", "unfinished": "중단됨", "error": "엔진 오류"}[record_status]
        return {"game_id": prepared["game_id"], "started_at": prepared["started_at"],
                "updated_at": prepared["updated_at"], "ended_at": prepared["ended_at"],
                "human_color": prepared["human_color"], "result": prepared["result"],
                "termination": prepared["termination"], "status": status,
                "record_status": record_status, "engine_name": prepared["engine_name"],
                "movetime_ms": prepared["movetime_ms"], "ply_count": len(prepared["moves"]),
                "handicap_moves": sum(bool(move.get("forced")) for move in prepared["moves"]),
                "current": current}

    def list_games(self, current_game_id=None):
        with self._lock:
            games = []
            invalid_count = 0
            for path in self.save_dir.glob("*.json"):
                try:
                    prepared = self._load(path.stem)
                    summary = self._summary(prepared, current_game_id)
                    if not summary["ply_count"] and summary["record_status"] not in ("completed", "error"):
                        continue
                    games.append((prepared["started_sort"], summary))
                except ValueError:
                    invalid_count += 1
            games.sort(key=lambda item: (item[0], item[1]["game_id"]), reverse=True)
            return {"games": [summary for _stamp_value, summary in games],
                    "total": len(games), "invalid_count": invalid_count}

    @staticmethod
    def _pgn(prepared):
        record = prepared["record"]
        game = chess.pgn.Game.from_board(prepared["board"])
        engine = record["state"].get("engine", {})
        source = record.get("source", {})
        if not isinstance(engine, dict):
            engine = {}
        if not isinstance(source, dict):
            source = {}
        started = prepared["started_at"]
        try:
            started_date = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            if started_date.tzinfo is None:
                started_date = started_date.replace(tzinfo=datetime.timezone.utc)
            date = started_date.astimezone().strftime("%Y.%m.%d")
        except (ValueError, OverflowError, OSError):
            date = "????.??.??"
        human_white = prepared["human_color"] == "white"
        game.headers.update({
            "Event": "Regicide local human practice", "Site": "local", "Date": date,
            "White": "Human" if human_white else prepared["engine_name"],
            "Black": prepared["engine_name"] if human_white else "Human",
            "Result": prepared["result"], "GameId": prepared["game_id"],
            "EngineMoveTimeMS": str(prepared["movetime_ms"] or "unknown"),
            "EngineSHA256": _text(engine.get("binary_sha256"), "unknown", 128),
            "InterfaceSourceCommit": _text(source.get("commit"), "unknown", 128),
            "DrawPolicy": _text(record.get("draw_policy"), "always claim available threefold/50-move draws"),
            "Termination": "unterminated" if prepared["result"] == "*" else "normal",
            "TerminationReason": prepared["termination"] or ("engine_error" if prepared["error"] else "in_progress"),
            "HandicapMoves": str(sum(bool(move.get("forced")) for move in prepared["moves"]))})
        movetimes = record.get("engine_movetimes_ms", {})
        if not isinstance(movetimes, dict):
            movetimes = {}
        if movetimes or record.get("time_settings_events"):
            game.headers["EngineMoveTimePolicy"] = "Header is latest setting; move comments record each search budget"
        for entry, node in zip(prepared["moves"], game.mainline()):
            if entry.get("forced"):
                node.comment = "User-forced Regicide move (handicap)"
            else:
                movetime = movetimes.get(str(entry["ply"]))
                if entry["actor"] == "engine" and type(movetime) is int and 100 <= movetime <= 5000:
                    node.comment = f"Regicide think time: {movetime} ms"
        return str(game) + "\n\n"

    def get_game(self, game_id, current_game_id=None):
        with self._lock:
            prepared = self._load(game_id)
            summary = self._summary(prepared, current_game_id)
            board = prepared["board"].root()
            frames = []
            for ply in range(len(prepared["moves"]) + 1):
                final = ply == len(prepared["moves"])
                frames.append({
                    "ply": ply,
                    "board": {chess.square_name(square): piece.symbol()
                              for square, piece in board.piece_map().items()},
                    "turn": "white" if board.turn else "black",
                    "human_color": prepared["human_color"],
                    "in_check": board.is_check(),
                    "last_move": board.peek().uci() if board.move_stack else None,
                    "result": summary["result"] if final else "*",
                    "termination": summary["termination"] if final else None,
                    "status": summary["status"] if final else "과거 기보 · " + ("백" if board.turn else "흑") + " 차례",
                    "human_to_move": False, "engine_to_move": False, "legal_moves": [],
                    "can_undo": False, "can_handicap": False, "handicap_mode": False,
                    "opening_guide": self._opening_guide.get(board),
                    "error": None})
                if not final:
                    board.push_uci(prepared["moves"][ply]["uci"])
            return {"summary": summary, "moves": [dict(move) for move in prepared["moves"]],
                    "frames": frames, "pgn": self._pgn(prepared)}

    def get_pgn(self, game_id):
        with self._lock:
            return self._pgn(self._load(game_id))

    def _atomic_add(self, target, contents):
        """Publish one complete file without replacing an existing user file."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=self.save_dir, prefix=".import-", suffix=".tmp",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
            os.link(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _add_missing_pgn(self, game_id, prepared):
        try:
            self._atomic_add(self.save_dir / (game_id + ".pgn"), self._pgn(prepared))
        except FileExistsError:
            pass  # Existing PGNs, including links, are never overwritten.

    def _write_import(self, game_id, record):
        """Add validated JSON and generated PGN, each published atomically."""
        prepared = self._prepare(record, game_id)
        contents = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        if len(contents.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("대국 기록의 크기가 너무 큽니다.")
        self._atomic_add(self.save_dir / (game_id + ".json"), contents)
        self._add_missing_pgn(game_id, prepared)

    def import_legacy(self, source_dirs):
        """Copy played legacy games once, leaving every source file untouched."""
        if isinstance(source_dirs, (str, os.PathLike)):
            source_dirs = [source_dirs]
        report = {"imported": 0, "skipped": 0, "invalid": 0, "conflicts": 0}
        with self._lock:
            for source_dir in source_dirs:
                directory = Path(source_dir).expanduser().resolve()
                if directory == self.save_dir or not directory.is_dir():
                    continue
                for path in sorted(directory.glob("*.json")):
                    try:
                        self._check_id(path.stem)
                        record, _key = self._read_json(path, directory)
                        prepared = self._prepare(record, path.stem)
                        if not prepared["moves"]:
                            report["skipped"] += 1
                            continue
                        game_id = path.stem
                        digest = _fingerprint(record)
                        target = self.save_dir / (game_id + ".json")
                        conflict = False
                        pgn_target = self.save_dir / (game_id + ".pgn")
                        if target.exists() or target.is_symlink() or pgn_target.exists() or pgn_target.is_symlink():
                            try:
                                existing = self._load(game_id)
                                if _fingerprint(existing["record"]) == digest:
                                    self._add_missing_pgn(game_id, existing)
                                    report["skipped"] += 1
                                    continue
                            except ValueError:
                                pass
                            conflict = True
                            game_id = game_id[:91] + "-" + digest[:8]
                            target = self.save_dir / (game_id + ".json")
                            pgn_target = self.save_dir / (game_id + ".pgn")
                            if target.exists() or target.is_symlink() or pgn_target.exists() or pgn_target.is_symlink():
                                try:
                                    existing = self._load(game_id)
                                    if _fingerprint(existing["record"]) == digest:
                                        self._add_missing_pgn(game_id, existing)
                                        report["skipped"] += 1
                                        continue
                                except ValueError:
                                    pass
                                # An exceptionally rare digest-prefix collision
                                # still must not overwrite an existing record.
                                game_id = path.stem[:35] + "-" + digest
                        imported = dict(record)
                        imported["game_id"] = game_id
                        if conflict:
                            imported["original_game_id"] = path.stem
                        self._write_import(game_id, imported)
                        report["imported"] += 1
                        if conflict:
                            report["conflicts"] += 1
                    except (ValueError, OSError, RecursionError):
                        report["invalid"] += 1
        return report
