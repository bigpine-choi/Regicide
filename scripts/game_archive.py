"""Persistent, read-only human-game library and explicit legacy imports.

Saved JSON is the source of truth. Boards, SAN and PGN are reconstructed by
legally replaying its moves, so a missing PGN or a stale presentation snapshot
cannot corrupt the viewer or change the game currently being played.
"""

from collections import OrderedDict
import copy
import datetime
import hashlib
import json
import math
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
from opening_variety import pgn_headers as opening_variety_headers
from dynamic_play import pgn_headers as dynamic_play_headers
import boss_play
import boss_opponent
import sparring_play
import sparring_rating
from sparring_referee import POLICY as REFEREE_POLICY, SOURCE as REFEREE_SOURCE
from game_clock import CONTROLS, MAX_ADJUSTED_MS, SCHEMA as CLOCK_SCHEMA


MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_PLIES = 10_000
GAME_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
RESULTS = {"*", "1-0", "0-1", "1/2-1/2"}
TIMEOUTS = {"time_forfeit", "time_forfeit_insufficient_material"}


def _recorded_clock(record, board):
    """Validate saved clock balances independently of presentation snapshots."""
    metadata = record.get("clock")
    if metadata is None or isinstance(metadata, dict) and metadata.get("enabled") is False:
        return None
    error = "대국의 시간 기록이 올바르지 않습니다."
    if not isinstance(metadata, dict) or metadata.get("schema") != CLOCK_SCHEMA or metadata.get("enabled") is not True:
        raise ValueError(error)
    control = metadata.get("control_id")
    if not isinstance(control, str) or control not in CONTROLS:
        raise ValueError(error)
    config = CONTROLS[control]
    policy = metadata.get("timeout_policy", "both")
    soft = metadata.get("soft_timeout_color")
    if (policy not in ("both", "engine_only")
            or (policy == "both" and soft is not None)
            or (policy == "engine_only" and (soft not in ("white", "black")
                or soft != record.get("state", {}).get("human_color")))):
        raise ValueError(error)
    if any(type(metadata.get(key)) is not int or metadata[key] != config[key] for key in ("initial_ms", "increment_ms")):
        raise ValueError(error)
    history, snapshot = metadata.get("history"), metadata.get("snapshot")
    if not isinstance(history, list) or len(history) != len(board.move_stack) + 1 or not isinstance(snapshot, dict):
        raise ValueError(error)
    adjustments = metadata.get("adjustments", [])
    edit_revision = metadata.get("edit_revision", 0)
    if (not isinstance(adjustments, list) or type(edit_revision) is not int or edit_revision < 0
            or type(snapshot.get("edit_revision", 0)) is not int
            or snapshot.get("edit_revision", 0) != edit_revision):
        raise ValueError(error)
    ceiling = (MAX_ADJUSTED_MS if adjustments else config["initial_ms"]) + len(board.move_stack) * config["increment_ms"]

    def finite(value):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= ceiling

    def elapsed_valid(value, color):
        return (type(value) in (int, float) and math.isfinite(value) and value >= 0
                and (color == soft or bool(adjustments) or value <= ceiling))

    events_by_ply, clean_adjustments = {}, []
    last_revision, last_ply, last_elapsed = 0, -1, 0
    for event in adjustments:
        if not isinstance(event, dict):
            raise ValueError(error)
        revision, ply, elapsed = event.get("revision"), event.get("ply"), event.get("turn_elapsed_ms")
        delta, color = event.get("delta_ms"), event.get("color")
        applied = event.get("applied_delta_ms")
        if (type(revision) is not int or not last_revision < revision <= edit_revision
                or type(ply) is not int or not 0 <= ply <= len(board.move_stack) or ply < last_ply
                or color not in ("white", "black")
                or type(delta) is not int or delta == 0 or abs(delta) > MAX_ADJUSTED_MS
                or not elapsed_valid(elapsed, soft)
                or (ply == last_ply and elapsed < last_elapsed)
                or not finite(event.get("before_ms")) or not finite(event.get("after_ms"))
                or event["after_ms"] > MAX_ADJUSTED_MS
                or type(applied) not in (int, float) or not math.isfinite(applied)
                or abs(applied) > ceiling):
            raise ValueError(error)
        clean_event = {key: event[key] for key in ("revision", "ply", "color", "delta_ms",
                       "applied_delta_ms", "before_ms", "after_ms", "turn_elapsed_ms")}
        clean_adjustments.append(clean_event)
        events_by_ply.setdefault(ply, []).append(clean_event)
        last_revision, last_ply, last_elapsed = revision, ply, elapsed

    def turn_balances(previous, root_ply, total_elapsed, mover, *, current=False):
        # Replay individual debit/edit segments: adding time after a human
        # reached zero must not charge their earlier overtime a second time.
        balances = {name: previous[name + "_ms"] for name in ("white", "black")}
        spent, overtime = 0.0, 0.0

        def debit(segment, allow_expired=False):
            nonlocal overtime
            if segment < -.001 or not allow_expired and mover != soft and segment > balances[mover] + .001:
                raise ValueError(error)
            segment = max(0, segment)
            if mover == soft:
                overtime += max(0, segment - balances[mover])
            balances[mover] = max(0, balances[mover] - segment)

        for event in events_by_ply.get(root_ply, []):
            if event["turn_elapsed_ms"] > total_elapsed + .001:
                raise ValueError(error)
            debit(event["turn_elapsed_ms"] - spent)
            if mover != soft and balances[mover] <= 0:
                # An edit cannot rescue an already expired running clock.
                raise ValueError(error)
            name = event["color"]
            before = balances[name]
            after = min(MAX_ADJUSTED_MS, max(0, before + event["delta_ms"]))
            if (abs(event["before_ms"] - before) > .01 or abs(event["after_ms"] - after) > .01
                    or abs(event["applied_delta_ms"] - (after - before)) > .01):
                raise ValueError(error)
            balances[name] = after
            if not current and name == mover and mover != soft and after == 0:
                raise ValueError(error)  # A manual flag cannot earn a move/increment.
            if name == soft and after > 0:
                overtime = 0.0
            spent = event["turn_elapsed_ms"]
        # Legacy v1 live snapshots may have been sampled after zero before
        # the next timeout poll. Preserve that representation, but never let
        # a later adjustment or completed move revive the expired clock.
        debit(total_elapsed - spent, allow_expired=current)
        return balances, overtime

    turn = "white" if board.root().turn else "black"
    clean = []
    for ply, entry in enumerate(history):
        if (not isinstance(entry, dict) or type(entry.get("ply")) is not int or entry["ply"] != ply
                or not all(finite(entry.get(key)) for key in ("white_ms", "black_ms"))
                or not elapsed_valid(entry.get("elapsed_ms"), turn)):
            raise ValueError(error)
        if ply == 0:
            if (entry.get("mover") is not None or entry["elapsed_ms"] != 0
                    or any(entry[key] != config["initial_ms"] for key in ("white_ms", "black_ms"))):
                raise ValueError(error)
        else:
            other = "black" if turn == "white" else "white"
            expected, _overtime = turn_balances(clean[-1], ply - 1, entry["elapsed_ms"], turn)
            expected[turn] += config["increment_ms"]
            if (entry.get("mover") != turn
                    or abs(entry[turn + "_ms"] - expected[turn]) > .01
                    or abs(entry[other + "_ms"] - expected[other]) > .01):
                raise ValueError(error)
            turn = other
        clean.append({key: entry[key] for key in ("ply", "white_ms", "black_ms", "mover", "elapsed_ms")})
    if (snapshot.get("enabled") is not True or snapshot.get("control_id") != control
            or type(snapshot.get("ply")) is not int or snapshot["ply"] != len(board.move_stack)
            or snapshot.get("status") not in ("running", "paused", "finished")
            or not all(finite(snapshot.get(key)) for key in ("white_ms", "black_ms"))
            or not elapsed_valid(snapshot.get("turn_elapsed_ms"), turn)
            or snapshot.get("timeout_policy", "both") != policy
            or snapshot.get("soft_timeout_color") != soft):
        raise ValueError(error)
    for key in ("initial_ms", "increment_ms"):
        if type(snapshot.get(key)) is not int or snapshot[key] != config[key]:
            raise ValueError(error)
    other = "black" if turn == "white" else "white"
    expected, current_overtime = turn_balances(clean[-1], len(board.move_stack), snapshot["turn_elapsed_ms"], turn, current=True)
    if (any(abs(snapshot[name + "_ms"] - expected[name]) > .01 for name in ("white", "black"))
            or snapshot.get("running_color") != (turn if snapshot["status"] == "running" else None)):
        raise ValueError(error)
    flagged = snapshot.get("flagged_color")
    if flagged is not None and (flagged != turn or flagged == soft or snapshot["status"] != "finished" or snapshot[flagged + "_ms"] != 0):
        raise ValueError(error)
    if policy == "engine_only":
        overtime = soft if snapshot[soft + "_ms"] == 0 else None
        overtime_ms = current_overtime if overtime and turn == soft else 0
        if (snapshot.get("overtime_color") != overtime
                or not elapsed_valid(snapshot.get("overtime_ms"), soft)
                or abs(snapshot["overtime_ms"] - overtime_ms) > .01):
            raise ValueError(error)
    return {"enabled": True, "control_id": control, **config, "history": clean,
            "edit_revision": edit_revision, "adjustments": clean_adjustments,
            "timeout_policy": policy, "soft_timeout_color": soft,
            "snapshot": {**snapshot, "sampled_monotonic_ms": None}}


def _replay_clock(clock, ply, final):
    if clock is None:
        return {"enabled": False, "status": "disabled", "running_color": None}
    values = clock["snapshot"] if final else clock["history"][ply]
    return {"enabled": True, **{key: clock[key] for key in ("control_id", "label", "initial_ms", "increment_ms")},
            "timeout_policy": clock["timeout_policy"], "soft_timeout_color": clock["soft_timeout_color"],
            "overtime_color": (clock["soft_timeout_color"] if clock["soft_timeout_color"]
                               and values[clock["soft_timeout_color"] + "_ms"] == 0 else None),
            "white_ms": values["white_ms"], "black_ms": values["black_ms"],
            "running_color": None, "status": "replay", "ply": ply, "turn_elapsed_ms": 0,
            "flagged_color": values.get("flagged_color") if final else None,
            "sampled_monotonic_ms": None, "edit_revision": clock["edit_revision"] if final else 0,
            "editable": False}


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


def _recorded_referee(record, sparring_mode):
    """Keep optional referee identity distinct from the actual playing engine.

    Old games have no referee metadata. New assisted labels require agreement
    between the authoritative record, its identity, and the saved UI state;
    malformed optional metadata never prevents replaying a legal old game.
    """
    metadata = record.get("sparring_referee")
    if not sparring_mode or not isinstance(metadata, dict) or metadata.get("policy") != REFEREE_POLICY:
        return None
    engine = record.get("state", {}).get("engine", {})
    saved = engine.get("sparring_referee") if isinstance(engine, dict) else None
    if not isinstance(saved, dict) or saved.get("policy") != REFEREE_POLICY:
        return None
    flags = ("enabled", "available", "assisted")
    if any(type(metadata.get(key)) is not bool or metadata[key] != saved.get(key) for key in flags):
        return None
    if metadata["assisted"] and not metadata["enabled"]:
        return None
    configured = metadata.get("configured", metadata["assisted"])
    if (type(configured) is not bool or configured != saved.get("configured", saved.get("assisted"))
            or metadata["assisted"] and not configured):
        return None
    name, sha = metadata.get("name"), metadata.get("binary_sha256")
    if any(metadata.get(key) != saved.get(key) for key in ("name", "binary_sha256")):
        return None
    identity = metadata.get("identity")
    if metadata["assisted"]:
        if (not isinstance(name, str) or not name or len(name) > 200 or _text(name, limit=200) != name
                or "stockfish" not in name.casefold() or not isinstance(sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", sha) is None or not isinstance(identity, dict)
                or identity.get("name") != name or identity.get("binary_sha256") != sha):
            return None
    result = {key: metadata[key] for key in flags}
    result.update({"name": _text(name, limit=200) or None, "configured": configured,
                   "binary_sha256": sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else None,
                   "policy": REFEREE_POLICY, "historical": True,
                   "status": _text(metadata.get("status"), "unavailable", 40),
                   "message": _text(metadata.get("message"), limit=400)})
    if isinstance(identity, dict):
        result["identity"] = {"name": result["name"], "binary_sha256": result["binary_sha256"],
                              "source_url": _text(identity.get("source_url"), limit=300) or None,
                              "license": _text(identity.get("license"), limit=100) or None}
    return result


def _recorded_referee_decisions(record, actual_decisions, positions, metadata):
    """Expose only referee evidence attached to an actual recorded engine move.

    Configuration attribution survives failed screens. Individual score
    receipts have stricter position/source checks and remain a separate audit
    channel; they never become the replay board's own evaluation.
    """
    if not metadata or not metadata["assisted"]:
        return []
    recorded = record.get("sparring_referee", {}).get("decisions", [])
    if not isinstance(recorded, list):
        return []
    valid = []
    for row in actual_decisions:
        receipt = row.get("referee")
        ply = row["ply"]
        if (not isinstance(receipt, dict) or receipt not in recorded
                or receipt.get("source") != REFEREE_SOURCE or receipt.get("policy") != REFEREE_POLICY
                or receipt.get("game_id") != record.get("game_id")
                or receipt.get("fen") != positions.get(ply - 1)
                or receipt.get("selected_uci") != row.get("selected_uci")
                or receipt.get("engine_name") != metadata["name"]
                or receipt.get("binary_sha256") != metadata["binary_sha256"]
                or receipt.get("identity_verified") is not True):
            continue
        valid.append({"ply": ply, **copy.deepcopy(receipt)})
    return valid


def _stamp(value):
    value = _text(value, limit=100)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc).isoformat(), parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return value, 0.0


def _local_stamp(value):
    """Format the saved instant using its historical local UTC offset.

    Do not apply today's offset to an old game: zones with daylight saving may
    have used a different offset when that particular game was played.
    """
    try:
        parsed = datetime.datetime.fromisoformat(_text(value, limit=100).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        local = parsed.astimezone()
        offset = local.strftime("%z")
        if len(offset) == 5:
            offset = offset[:3] + ":" + offset[3:]
        return local.strftime("%Y-%m-%d %H:%M:%S"), offset or None
    except (ValueError, OverflowError, OSError):
        return "시간 정보 없음", None


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
            positions = {0: board.fen(en_passant="fen")}
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
                positions[ply] = board.fen(en_passant="fen")
        except (ValueError, TypeError, IndexError) as exc:
            raise ValueError("합법적으로 재생할 수 없는 대국 기록입니다.") from exc
        result = state.get("result", "*")
        if not isinstance(result, str) or result not in RESULTS:
            raise ValueError("대국 결과가 올바르지 않습니다.")
        outcome = board.outcome(claim_draw=True)
        termination = _text(state.get("termination"), limit=100) or None
        clock = _recorded_clock(record, board)
        if outcome is not None:
            if result not in ("*", outcome.result()):
                raise ValueError("체스판과 대국 결과가 일치하지 않습니다.")
            result = outcome.result()
            termination = outcome.termination.name.lower()
        elif result != "*" and termination in TIMEOUTS:
            flagged = clock["snapshot"].get("flagged_color") if clock else None
            if flagged not in ("white", "black") or (flagged == "white") != board.turn:
                raise ValueError("시간 초과 결과를 확인할 시간 기록이 없습니다.")
            insufficient = clock["timeout_policy"] != "engine_only" and board.has_insufficient_material(not board.turn)
            expected = "1/2-1/2" if insufficient else ("0-1" if board.turn else "1-0")
            expected_reason = "time_forfeit_insufficient_material" if insufficient else "time_forfeit"
            if result != expected or termination != expected_reason:
                raise ValueError("시간 초과 기록과 대국 결과가 일치하지 않습니다.")
        elif result != "*" and termination != "resignation":
            raise ValueError("종료 근거가 없는 대국 결과입니다.")
        if clock and clock["snapshot"].get("flagged_color") is not None and termination not in TIMEOUTS:
            raise ValueError("시간 초과 기록과 종료 사유가 일치하지 않습니다.")
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
        boss_metadata = record.get("boss_play")
        boss_mode = bool(boss_play.pgn_headers(boss_metadata))
        boss_decisions = (boss_metadata.get("decisions", []) if boss_mode else [])
        if not isinstance(boss_decisions, list):
            boss_decisions = []
        # Replay accepts only decisions attached to an actual engine move in
        # this history. A final saved phase must not leak into earlier frames.
        boss_decisions = [row for row in boss_decisions
                          if isinstance(row, dict) and type(row.get("ply")) is int
                          and 1 <= row["ply"] <= len(moves)
                          and moves[row["ply"] - 1]["actor"] == "engine"
                          and moves[row["ply"] - 1]["uci"] == row.get("selected_uci")
                          and row.get("history_uci") == uci_moves[:row["ply"] - 1]]
        sparring_metadata = record.get("sparring_play")
        sparring_mode = not boss_mode and bool(sparring_play.pgn_headers(sparring_metadata))
        sparring_decisions = sparring_metadata.get("decisions", []) if sparring_mode else []
        if not isinstance(sparring_decisions, list):
            sparring_decisions = []
        sparring_decisions = [row for row in sparring_decisions
                             if isinstance(row, dict) and type(row.get("ply")) is int
                             and row.get("policy") == sparring_metadata.get("policy")
                             and 1 <= row["ply"] <= len(moves)
                             and moves[row["ply"] - 1]["actor"] == "engine"
                             and moves[row["ply"] - 1]["uci"] == row.get("selected_uci")
                             and row.get("history_uci") == uci_moves[:row["ply"] - 1]]
        referee = _recorded_referee(record, sparring_mode)
        referee_decisions = _recorded_referee_decisions(record, sparring_decisions, positions, referee)
        return {"record": record, "board": board, "moves": moves,
                "game_id": game_id, "human_color": human_color,
                "result": result, "termination": termination, "clock": clock,
                "engine_name": engine_name, "movetime_ms": movetime_ms,
                "started_at": started_at, "started_sort": started_sort,
                "updated_at": updated_at, "ended_at": ended_at or None,
                "boss_mode": boss_mode, "boss_decisions": boss_decisions,
                "sparring_mode": sparring_mode, "sparring_decisions": sparring_decisions,
                "sparring_referee": referee,
                "referee_decisions": referee_decisions,
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
        started_local, started_offset = _local_stamp(prepared["started_at"])
        ended_local, ended_offset = _local_stamp(prepared["ended_at"])
        return {"game_id": prepared["game_id"], "started_at": prepared["started_at"],
                "updated_at": prepared["updated_at"], "ended_at": prepared["ended_at"],
                "started_at_local": started_local, "ended_at_local": ended_local,
                "started_at_utc_offset": started_offset, "ended_at_utc_offset": ended_offset,
                "human_color": prepared["human_color"], "result": prepared["result"],
                "termination": prepared["termination"], "status": status,
                "record_status": record_status, "engine_name": prepared["engine_name"],
                "movetime_ms": prepared["movetime_ms"], "ply_count": len(prepared["moves"]),
                "boss_mode": prepared["boss_mode"],
                "sparring_mode": prepared["sparring_mode"],
                "assisted_sparring": bool(prepared["sparring_referee"] and prepared["sparring_referee"]["assisted"]),
                "sparring_referee": copy.deepcopy(prepared["sparring_referee"]),
                "time_control": prepared["clock"]["control_id"] if prepared["clock"] else None,
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
        game.headers.update(opening_variety_headers(record.get("opening_variety")))
        game.headers.update(dynamic_play_headers(record.get("dynamic_play")))
        game.headers.update(boss_play.pgn_headers(record.get("boss_play")))
        if prepared["sparring_mode"]:
            game.headers.update(sparring_play.pgn_headers(record.get("sparring_play")))
        referee = prepared["sparring_referee"]
        if referee and referee["assisted"]:
            game.headers.update({"SparringReferee": referee["name"],
                                 "RefereeBinarySHA256": referee["binary_sha256"],
                                 "RefereePolicy": referee["policy"], "AssistedSparring": "true"})
        clock = prepared["clock"]
        if clock:
            game.headers["TimeControl"] = f"{clock['initial_ms'] // 1000}+{clock['increment_ms'] // 1000}"
            if clock["adjustments"]:
                game.headers["ClockAdjustments"] = str(len(clock["adjustments"]))
                game.headers["ClockAdjustmentPolicy"] = "User-edited practice clocks; completed-move clocks preserved"
            pacing = record.get("engine_pacing")
            if (isinstance(pacing, dict) and pacing.get("enabled") is True
                    and pacing.get("policy") in ("regicide-engine-pacing-v1", "regicide-engine-pacing-v2",
                                                  "regicide-engine-pacing-v3", "regicide-engine-pacing-v4",
                                                  "regicide-engine-pacing-v5", "regicide-engine-pacing-v6",
                                                  "regicide-engine-pacing-v7", "regicide-engine-pacing-v8",
                                                  "regicide-engine-pacing-v9")):
                game.headers["EnginePacingPolicy"] = pacing["policy"]
            if clock["timeout_policy"] == "engine_only":
                game.headers["TimeForfeitPolicy"] = "engine_only"
            if prepared["termination"] in TIMEOUTS:
                game.headers["Termination"] = "time forfeit"
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
                if entry["actor"] == "engine" and type(movetime) is int and 1 <= movetime <= 5000:
                    node.comment = f"Regicide think time: {movetime} ms"
            if clock:
                clock_entry = clock["history"][entry["ply"]]
                node.set_clock(clock_entry[clock_entry["mover"] + "_ms"] / 1000)
        return str(game) + "\n\n"

    def get_game(self, game_id, current_game_id=None):
        with self._lock:
            prepared = self._load(game_id)
            summary = self._summary(prepared, current_game_id)
            board = prepared["board"].root()
            frames = []
            for ply in range(len(prepared["moves"]) + 1):
                final = ply == len(prepared["moves"])
                past_decisions = [row for row in prepared["boss_decisions"] if row["ply"] <= ply]
                forced = any(row.get("forced") for row in prepared["moves"][:ply])
                replay_budget = self._budget_at_ply(prepared, ply)
                replay_engine = {"name": prepared["engine_name"],
                                 "movetime_ms": replay_budget,
                                 "boss_mode": prepared["boss_mode"],
                                 "sparring_mode": prepared["sparring_mode"]}
                if prepared["sparring_referee"] is not None:
                    replay_engine["sparring_referee"] = copy.deepcopy(prepared["sparring_referee"])
                if prepared["sparring_mode"]:
                    replay_engine["sparring"] = sparring_play.derive_profile(
                        board, [row for row in prepared["sparring_decisions"] if row["ply"] <= ply],
                        policy=prepared["record"]["sparring_play"]["policy"])
                    # Historical observation reconstruction must not relabel an
                    # old game as a newer move-selection policy.
                    replay_engine["sparring"]["policy"] = prepared["record"]["sparring_play"]["policy"]
                    replay_engine["sparring_rating"] = sparring_rating.archive_snapshot(
                        board, prepared["record"].get("sparring_rating"))
                if prepared["boss_mode"]:
                    replay_engine["boss_phase"] = boss_play.phase(
                        board, past_decisions, forced=forced,
                        engine_color=prepared["human_color"] != "white",
                        budget_ms=replay_budget)
                    replay_engine["boss_opponent"] = boss_opponent.derive_profile(board, past_decisions)
                frames.append({
                    "ply": ply, "game_id": prepared["game_id"], "fen": board.fen(en_passant="fen"),
                    "board": {chess.square_name(square): piece.symbol()
                              for square, piece in board.piece_map().items()},
                    "turn": "white" if board.turn else "black",
                    "human_color": prepared["human_color"],
                    "engine": replay_engine,
                    "clock": _replay_clock(prepared["clock"], ply, final),
                    "decision_support": {"status": "inactive", "experimental": True,
                                         "recommendation": {"status": "inactive"},
                                         "note": "기보 재생 중에는 생각시간을 추천하지 않습니다."},
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
                    "frames": frames, "pgn": self._pgn(prepared),
                    "referee_decisions": copy.deepcopy(prepared["referee_decisions"])}

    @staticmethod
    def _budget_at_ply(prepared, ply):
        """Recover settings events, preferring the next recorded actual search.

        A setting changed during a search belongs to the following search, so
        its requested value must not relabel an already-running 100ms move.
        """
        record = prepared["record"]
        budget = prepared["movetime_ms"]
        events = record.get("time_settings_events", [])
        if not isinstance(events, list):
            events = []
        events = [event for event in events if isinstance(event, dict)
                  and type(event.get("after_ply")) is int and event["after_ply"] >= 0
                  and all(type(event.get(key)) is int and 100 <= event[key] <= 5000
                          for key in ("from_ms", "to_ms"))]
        if events:
            budget = events[0]["from_ms"]
            for event in events:
                if event["after_ply"] <= ply:
                    budget = event["to_ms"]
        if ply < len(prepared["moves"]) and prepared["moves"][ply]["actor"] == "engine":
            movetimes = record.get("engine_movetimes_ms", {})
            actual = movetimes.get(str(ply + 1)) if isinstance(movetimes, dict) else None
            if type(actual) is int and 1 <= actual <= 5000:
                budget = actual
        return budget

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
