"""Crash-safe completed native queries for resumable gambit research.

The persistent lock file is never removed or replaced. Only a complete query
with an exact score, legal PV and durable raw UCI evidence becomes a checkpoint.
An interrupted attempt may leave a raw log or temporary file; neither is a hit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

import chess

from neural_search_data.engine import normalize_root_moves


SESSION_SCHEMA = "regicide.gambit-research-session.v1"
ENGINE_SCHEMA = "regicide.gambit-research-engine.v1"
QUERY_SCHEMA = "regicide.gambit-research-query.v1"
_RAW_NAME = re.compile(r"native-uci-[0-9a-f]{32}\.jsonl\Z")


class ResearchPaused(RuntimeError):
    """A deliberate boundary between complete queries, safe to resume."""

    def __init__(self, reason="query_budget"):
        self.reason = reason
        super().__init__(reason)


def _json_types(value):
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_types(item)
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
        for item in value.values():
            _json_types(item)
        return
    raise ValueError("Research identity and records must contain finite JSON values")


def _canonical(value):
    _json_types(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sync_directory(path):
    # Windows has no portable directory fsync. The file is still flushed before
    # replacement; POSIX additionally persists the renamed directory entry.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(path, value):
    """Write finite JSON through a same-directory fsynced temporary file."""
    _canonical(value)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       indent=2) + "\n").encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix="." + path.name + ".",
                                         suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in research evidence")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON value in research evidence")


def _read_json(path):
    if path.is_symlink():
        raise ValueError("Research evidence must not be a symbolic link")
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_object, parse_constant=_reject_constant)
        _canonical(value)
        return value
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Missing or corrupt research evidence: " + path.name) from error


def _profile(teacher):
    value = {"identity": teacher.identity, "options": teacher.options}
    if hasattr(teacher, "runtime_defaults"):
        value["runtime_defaults"] = teacher.runtime_defaults
    return json.loads(_canonical(value))


class ResearchSession:
    """Exclusive output directory and immutable inputs for one resumable run.

    Enter the session before constructing RuntimeTeacher with ``raw_path``.
    Enter the teacher before ``bind_engine`` or ``query``. Each attempt has its
    own raw log; a cache hit continues to reference its original attempt.
    ``new_queries`` counts calls begun, including calls that are interrupted.
    """

    def __init__(self, output, configuration, *, resume=False, max_new_queries=None):
        if not isinstance(configuration, dict) or type(configuration.get("nodes")) is not int or configuration["nodes"] < 1:
            raise ValueError("configuration.nodes must be a positive integer")
        if type(resume) is not bool:
            raise ValueError("resume must be a boolean")
        if max_new_queries is not None and (type(max_new_queries) is not int or max_new_queries < 0):
            raise ValueError("max_new_queries must be a nonnegative integer or None")
        self.output = Path(output).resolve()
        self.configuration = json.loads(_canonical(configuration))
        self.config_id = _digest(self.configuration)
        self.resume = resume
        self.max_new_queries = max_new_queries
        self.new_queries = 0
        self.reused_queries = 0
        self.raw_path = self.output / ("native-uci-" + uuid.uuid4().hex + ".jsonl")
        self._cache = self.output / "completed-queries"
        self._lock = None
        self._entered = False
        self._active = False
        self._engine_profile = None
        self._engine_id = None
        self._raw_cursor = 0

    def __enter__(self):
        if self._entered:
            raise RuntimeError("ResearchSession may only be entered once")
        self._entered = True
        if self.resume:
            if not self.output.is_dir():
                raise ValueError("Resume requires an existing research session")
        else:
            self.output.mkdir(parents=True, exist_ok=False)
        try:
            self._acquire_lock()
            expected = {"schema": SESSION_SCHEMA, "config_id": self.config_id,
                        "configuration": self.configuration}
            session_path = self.output / "session.json"
            if self.resume:
                if _canonical(_read_json(session_path)) != _canonical(expected):
                    raise ValueError("Research configuration differs from the saved session")
            else:
                atomic_write(session_path, expected)
            if self._cache.is_symlink():
                raise ValueError("Query checkpoint directory must not be a symbolic link")
            self._cache.mkdir(exist_ok=True)
            self._active = True
            return self
        except BaseException:
            self._release_lock()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._active = False
        self._release_lock()

    def _acquire_lock(self):
        lock_path = self.output / ".session.lock"
        if lock_path.is_symlink():
            raise ValueError("Session lock must not be a symbolic link")
        stream = lock_path.open("a+b")
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise RuntimeError("Research session is already locked") from error
        except BaseException:
            stream.close()
            raise
        self._lock = stream

    def _release_lock(self):
        stream, self._lock = self._lock, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def _require_active(self):
        if not self._active:
            raise RuntimeError("ResearchSession must be entered before use")
        if _digest(self.configuration) != self.config_id:
            raise ValueError("Research configuration was mutated during the session")

    def bind_engine(self, teacher):
        self._require_active()
        profile = _profile(teacher)
        engine_id = _digest(profile)
        expected = {"schema": ENGINE_SCHEMA, "config_id": self.config_id,
                    "engine_id": engine_id, "engine": profile}
        path = self.output / "engine.json"
        if path.exists() or path.is_symlink():
            if _canonical(_read_json(path)) != _canonical(expected):
                raise ValueError("Native engine options or identity differ from the saved session")
        else:
            if any(self._cache.glob("*.json")):
                raise ValueError("Completed queries have no saved native engine identity")
            atomic_write(path, expected)
        self._engine_profile, self._engine_id = profile, engine_id
        return copy.deepcopy(expected)

    def _request(self, board, query_id, roots=None):
        root = board.root()
        replay = root.copy(stack=True)
        history = [move.uci() for move in board.move_stack]
        for uci in history:
            move = chess.Move.from_uci(uci)
            if move not in replay.legal_moves:
                raise ValueError("Query history contains an illegal move")
            replay.push(move)
        if replay.fen(en_passant="fen") != board.fen(en_passant="fen"):
            raise ValueError("Query history does not reconstruct its current position")
        request = {"config_id": self.config_id, "engine_id": self._engine_id,
                   "query_id": json.loads(_canonical(query_id)), "nodes": self.configuration["nodes"],
                   "root_fen": root.fen(en_passant="fen"), "history_uci": history,
                   "fen": board.fen(en_passant="fen"), "chess960": board.chess960}
        if roots is not None:
            request["root_moves_uci"] = [move.uci() for move in roots]
        return request

    def _validate_result(self, record, board, query_id, roots=None):
        if not isinstance(record, dict):
            raise ValueError("Native query returned no result record")
        _canonical(record)
        if _canonical(record.get("query_id")) != _canonical(query_id):
            raise ValueError("Native query identifier differs from its request")
        if record.get("completed") is not True:
            raise ValueError("Incomplete native query cannot be checkpointed")
        if any(type(record.get(key, False)) is not bool or record.get(key, False)
               for key in ("lowerbound", "upperbound")):
            raise ValueError("Bounded native score cannot be checkpointed")
        score = record.get("score")
        if not isinstance(score, dict) or score.get("kind") not in ("cp", "mate") or type(score.get("stm")) is not int:
            raise ValueError("Native query requires an exact integer score")
        if type(record.get("depth")) is not int or record["depth"] < 1:
            raise ValueError("Native query has no completed positive depth")
        if type(record.get("requested_nodes")) is not int or record["requested_nodes"] != self.configuration["nodes"]:
            raise ValueError("Native query node request differs from configuration")
        if type(record.get("nodes")) is not int or not 0 <= record["nodes"] <= self.configuration["nodes"]:
            raise ValueError("Native query exceeded its node limit or omitted node evidence")
        pv, best = record.get("pv_uci"), record.get("bestmove_uci")
        if not isinstance(pv, list) or not pv or type(best) is not str or pv[0] != best:
            raise ValueError("Native query returned no matching move and principal variation")
        if roots is None:
            if "root_moves_uci" in record:
                raise ValueError("Unrestricted query returned restricted-root provenance")
        else:
            allowed = [move.uci() for move in roots]
            if record.get("root_moves_uci") != allowed or best not in allowed:
                raise ValueError("Native query root restriction or returned move differs from its request")
        replay = board.copy(stack=True)
        for uci in pv:
            try:
                move = chess.Move.from_uci(uci) if type(uci) is str else None
            except ValueError as error:
                raise ValueError("Native principal variation contains an invalid move") from error
            if move not in replay.legal_moves:
                raise ValueError("Native principal variation contains an illegal move")
            replay.push(move)
        begin, end = record.get("uci_begin_seq"), record.get("uci_end_seq")
        if type(begin) is not int or type(end) is not int or begin < 1 or end < begin:
            raise ValueError("Native query has no complete raw UCI sequence range")

    def _raw_evidence(self, record, *, saved=None):
        begin, end = record["uci_begin_seq"], record["uci_end_seq"]
        name = self.raw_path.name if saved is None else saved.get("path")
        if type(name) is not str or _RAW_NAME.fullmatch(name) is None:
            raise ValueError("Invalid raw UCI evidence filename")
        path = self.output / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Raw UCI evidence is missing or is a symbolic link")
        offset = self._raw_cursor if saved is None else saved.get("byte_start")
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid raw UCI evidence offset")
        digest = hashlib.sha256()
        found = begin
        byte_start = None
        previous = 0
        try:
            with path.open("r+b" if saved is None else "rb") as stream:
                stream.seek(offset)
                while found <= end:
                    start = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        raise ValueError("Raw UCI evidence is truncated")
                    row = json.loads(line, object_pairs_hook=_object, parse_constant=_reject_constant)
                    seq = row.get("seq") if isinstance(row, dict) else None
                    if type(seq) is not int or seq <= previous:
                        raise ValueError("Raw UCI sequence is invalid")
                    previous = seq
                    if seq < begin:
                        if saved is not None:
                            raise ValueError("Raw UCI evidence offset does not match its range")
                        continue
                    if seq != found:
                        raise ValueError("Raw UCI evidence sequence range has a gap")
                    if byte_start is None:
                        byte_start = start
                    digest.update(line)
                    found += 1
                byte_end = stream.tell()
                if saved is None:
                    # RuntimeTeacher flushes each line; fsync makes those bytes
                    # durable before the completed-query rename is committed.
                    os.fsync(stream.fileno())
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Raw UCI evidence cannot be verified") from error
        evidence = {"path": name, "begin_seq": begin, "end_seq": end,
                    "byte_start": byte_start, "byte_end": byte_end, "sha256": digest.hexdigest()}
        if saved is not None and evidence != saved:
            raise ValueError("Raw UCI evidence checksum or range differs from its checkpoint")
        if saved is None:
            _sync_directory(self.output)
            self._raw_cursor = byte_end
        return evidence

    def query(self, teacher, board, query_id, *, root_moves=None):
        self._require_active()
        snapshot = board.copy(stack=True)
        roots = normalize_root_moves(snapshot, root_moves)
        self.bind_engine(teacher)
        if type(teacher.nodes) is not int or teacher.nodes != self.configuration["nodes"]:
            raise ValueError("Teacher node limit differs from the research configuration")
        if Path(teacher.raw_path).resolve() != self.raw_path:
            raise ValueError("Teacher raw log does not belong to this attempt")
        request = self._request(snapshot, query_id, roots)
        path = self._cache / (_digest(query_id) + ".json")
        if path.exists() or path.is_symlink():
            envelope = _read_json(path)
            if not isinstance(envelope, dict):
                raise ValueError("Invalid completed-query checkpoint")
            payload = {key: value for key, value in envelope.items() if key != "checksum"}
            if envelope.get("checksum") != _digest(payload) or envelope.get("schema") != QUERY_SCHEMA:
                raise ValueError("Completed-query checksum or schema is invalid")
            if _canonical(envelope.get("request")) != _canonical(request):
                raise ValueError("Completed-query request differs from its current position or history")
            result = envelope.get("result")
            self._validate_result(result, snapshot, query_id, roots)
            raw = result.get("raw_log")
            if not isinstance(raw, dict):
                raise ValueError("Completed query has no linked raw UCI evidence")
            self._raw_evidence(result, saved=raw)
            self.reused_queries += 1
            return copy.deepcopy(result)
        if self.max_new_queries is not None and self.new_queries >= self.max_new_queries:
            raise ResearchPaused("query_budget")
        self.new_queries += 1
        # A teacher receives a separate full-history board so a faulty adapter
        # cannot mutate the caller's game or the validation reference board.
        root_options = {} if roots is None else {"root_moves": copy.deepcopy(roots)}
        result = teacher.query(snapshot.copy(stack=True), query_id, **root_options)
        self._validate_result(result, snapshot, query_id, roots)
        if _profile(teacher) != self._engine_profile:
            raise ValueError("Native engine options changed during the query")
        result = copy.deepcopy(result)
        result["raw_log"] = self._raw_evidence(result)
        payload = {"schema": QUERY_SCHEMA, "request": request, "result": result}
        atomic_write(path, {**payload, "checksum": _digest(payload)})
        return copy.deepcopy(result)
