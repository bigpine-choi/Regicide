"""Run an existing benchmark/match with a separate per-play LMR sidecar.

Usage: python scripts/lmr_experiment.py benchmark --telemetry NEW.json [benchmark args]
       python scripts/lmr_experiment.py match --telemetry NEW.json [match args]

The existing runner, JSON/PGN formats and runner exit code are retained. Outputs
must be new: --overwrite is deliberately unavailable through this experiment
wrapper. UCI lines are observed without changing python-chess's normal parser.
No telemetry file writes occur during play; snapshots are accumulated in memory
and serialized once on exit. A hard process kill can leave the reserved sidecar
empty. Python bookkeeping still contributes a small amount to runner wall time.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

import chess
import chess.engine


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("baseline", "candidate")
LMR_LINE = re.compile(r"info string lmr reductions (\d+) researches (\d+)")
BASE_SIMPLE_ENGINE = chess.engine.SimpleEngine


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class LmrProtocol(chess.engine.UciProtocol):
    """Observe every LMR string before the ordinary command parser sees it."""

    def reset_lmr(self):
        self.lmr_latest = None
        self.lmr_lines = 0
        self.lmr_errors = []
        self.lmr_bestmove_seen = False
        self.lmr_active = False

    async def play(self, board, limit, **kwargs):
        # Reset on the protocol loop as well as before SimpleEngine.play(), so
        # readiness/previous-command state cannot be mistaken for this result.
        self.reset_lmr()
        self.lmr_active = True
        try:
            return await super().play(board, limit, **kwargs)
        finally:
            self.lmr_active = False

    def line_received(self, line):
        if getattr(self, "lmr_active", False):
            match = LMR_LINE.fullmatch(line.strip())
            if match:
                values = tuple(map(int, match.groups()))
                if values[1] > values[0]:
                    self.lmr_errors.append("researches exceed reductions")
                if self.lmr_latest is not None and any(
                        new < old for new, old in zip(values, self.lmr_latest)):
                    self.lmr_errors.append("cumulative LMR counters decreased")
                self.lmr_latest = values
                self.lmr_lines += 1
            elif line.startswith("info string lmr "):
                self.lmr_errors.append("malformed LMR counter line")
            elif line.startswith("bestmove "):
                self.lmr_bestmove_seen = True
                self.lmr_active = False
        super().line_received(line)


class Recorder:
    def __init__(self, mode):
        self.mode = mode
        self.started_utc = utc_now()
        self.expected = []
        self.startups = []
        self.plays = []
        self.errors = []
        self.warmup = 0
        self.outputs = []

    def bind(self, args):
        self.warmup = args.warmup if self.mode == "benchmark" else 0
        self.outputs = [str(args.output)] if self.mode == "benchmark" else [str(args.pgn), str(args.json)]
        for role in ROLES:
            binary = getattr(args, role)
            external = getattr(args, "external_baseline_source", None) if role == "baseline" else None
            self.expected.append({
                "role": role, "binary": str(binary),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "command": [str(binary), *(external["args"] if external else ["uci"])],
                "requested_options": dict(getattr(args, role + "_options")),
            })

    def reserve_engine(self, command):
        index = len(self.startups)
        if index >= len(ROLES):
            raise RuntimeError("runner created more than the two declared engine roles")
        expected = self.expected[index]
        received = [str(item) for item in command] if isinstance(command, (list, tuple)) else [str(command)]
        if received != expected["command"]:
            raise RuntimeError(f"runner engine creation order/command differs for {expected['role']}")
        record = {key: value for key, value in expected.items() if key != "command"}
        record.update(creation_index=index + 1, startup_completed=False, startup_error=None,
                      advertises_lmr=None, applied_options={})
        self.startups.append(record)
        return record

    def append_play(self, engine, board, limit, reply, error):
        protocol = engine.protocol
        advertised = engine.lmr_identity["advertises_lmr"]
        counters = protocol.lmr_latest if advertised else None
        index = engine.lmr_play_count + 1
        engine.lmr_play_count = index
        # Only memory snapshots here. FEN/UCI conversion and JSON serialization
        # are deferred until the runner has stopped all searches.
        self.plays.append({
            "sequence": len(self.plays) + 1, "role": engine.lmr_identity["role"],
            "binary": engine.lmr_identity["binary"],
            "binary_sha256": engine.lmr_identity["binary_sha256"],
            "process_search_index": index,
            "sample_kind": ("warmup" if self.mode == "benchmark" and index <= self.warmup
                            else "measured" if self.mode == "benchmark" else "match"),
            "board_snapshot": board.copy(stack=False), "root_snapshot": board.root(),
            "history_snapshot": tuple(board.move_stack),
            "selected_move_snapshot": reply.move if reply is not None else None,
            "nodes": reply.info.get("nodes") if reply is not None else None,
            "depth": reply.info.get("depth") if reply is not None else None,
            "seldepth": reply.info.get("seldepth") if reply is not None else None,
            "engine_time_seconds": reply.info.get("time") if reply is not None else None,
            "requested_limit": {name: value for name, value in vars(limit).items() if name != "clock_id"},
            "lmr": {"reductions": counters[0], "researches": counters[1]} if counters is not None else None,
            "lmr_info_lines": protocol.lmr_lines if advertised else None,
            "lmr_status": ("not_advertised" if not advertised else "missing"
                           if counters is None else "reported"),
            "bestmove_received": protocol.lmr_bestmove_seen,
            "returned_from_play": reply is not None,
            "counter_errors": list(protocol.lmr_errors),
            "error": f"{type(error).__name__}: {error}" if error is not None else None,
        })

    def document(self, code, error):
        records = []
        for pending in self.plays:
            item = {key: value for key, value in pending.items() if not key.endswith("_snapshot")}
            item.update(fen=pending["board_snapshot"].fen(en_passant="fen"),
                        root_fen=pending["root_snapshot"].fen(en_passant="fen"),
                        full_history=[move.uci() for move in pending["history_snapshot"]],
                        selected_move=(pending["selected_move_snapshot"].uci()
                                       if pending["selected_move_snapshot"] is not None else None))
            records.append(item)
        factory_complete = len(self.startups) == 2 and all(item["startup_completed"] for item in self.startups)
        telemetry_complete = factory_complete and not self.errors and all(
            item["lmr_status"] != "missing" and not item["counter_errors"]
            for item in records if item["returned_from_play"])
        return {
            "schema": "regicide-lmr-play-telemetry-v1", "mode": self.mode,
            "started_utc": self.started_utc, "finished_utc": utc_now(),
            "runner_exit_code": code,
            "uncaught_error": f"{type(error).__name__}: {error}" if error is not None else None,
            "runner_outputs": self.outputs, "python_chess": chess.__version__,
            "engine_creation_policy": "Exactly baseline then candidate; command checked by ordinal even for identical binary paths.",
            "factory_complete": factory_complete,
            "telemetry_complete_for_returned_plays": telemetry_complete,
            "engines": self.startups, "plays": records, "capture_errors": self.errors,
            "limits": "Final info strings are cumulative for each search. Missing or unadvertised counters are null, never inferred as zero. Returned play is not proof a capped match completed. No per-play disk I/O; final serialization only. Python snapshots add bookkeeping to runner wall time. A hard kill can leave the exclusively reserved file empty.",
        }


def recording_engine(recorder):
    class RecordingEngine(BASE_SIMPLE_ENGINE):
        @classmethod
        def popen_uci(cls, command, *, timeout=10.0, debug=None, setpgrp=False, **popen_args):
            identity = recorder.reserve_engine(command)
            try:
                engine = cls.popen(LmrProtocol, command, timeout=timeout, debug=debug,
                                   setpgrp=setpgrp, **popen_args)
            except BaseException as exc:
                identity["startup_error"] = f"{type(exc).__name__}: {exc}"
                raise
            engine.lmr_identity = identity
            engine.lmr_play_count = 0
            identity["startup_completed"] = True
            identity["advertises_lmr"] = "UseLMR" in engine.options
            identity["engine_id"] = dict(engine.id)
            identity["default_lmr"] = engine.options["UseLMR"].default if identity["advertises_lmr"] else None
            return engine

        def configure(self, options):
            result = super().configure(options)
            self.lmr_identity["applied_options"].update(dict(options))
            return result

        def play(self, board, limit, **kwargs):
            self.protocol.reset_lmr()
            reply, error = None, None
            try:
                reply = super().play(board, limit, **kwargs)
                return reply
            except BaseException as exc:
                error = exc
                raise
            finally:
                try:
                    recorder.append_play(self, board, limit, reply, error)
                except Exception as exc:
                    # Recording failure must not replace the runner's original
                    # engine exception/exit code; make the missing capture clear.
                    recorder.errors.append(f"capture: {type(exc).__name__}: {exc}")

    return RecordingEngine


def load_runner(mode):
    name = "benchmark_search" if mode == "benchmark" else "compare_engines"
    spec = importlib.util.spec_from_file_location("regicide_lmr_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("mode", choices=("benchmark", "match"))
    parser.add_argument("--telemetry", required=True, type=Path)
    args, forwarded = parser.parse_known_args(argv)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    telemetry = args.telemetry.expanduser().resolve()
    recorder = Recorder(args.mode)
    original_engine = chess.engine.SimpleEngine
    output = None
    code, error = None, None
    try:
        # Patch before import: benchmark.run's default factory is bound while
        # its module is defined. Both original main() functions run unchanged.
        chess.engine.SimpleEngine = recording_engine(recorder)
        runner = load_runner(args.mode)
        declared = runner.arguments(forwarded)
        if declared.overwrite:
            parser.error("LMR experiments require new outputs; --overwrite is not permitted")
        related = {getattr(declared, name, None) for name in
                   ("baseline", "candidate", "positions", "openings", "baseline_source",
                    "output", "pgn", "json")}
        if telemetry in related:
            parser.error("telemetry must differ from every runner input/output path")
        recorder.bind(declared)
        telemetry.parent.mkdir(parents=True, exist_ok=True)
        output = telemetry.open("x", encoding="utf-8")
        code = runner.main(forwarded)
        return code
    except BaseException as exc:
        error = exc
        code = (130 if isinstance(exc, KeyboardInterrupt) else
                exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 2)
        raise
    finally:
        chess.engine.SimpleEngine = original_engine
        if output is not None:
            try:
                json.dump(recorder.document(code, error), output, ensure_ascii=False, indent=2)
                output.write("\n")
            finally:
                output.close()


if __name__ == "__main__":
    sys.exit(main())
