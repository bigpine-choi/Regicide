"""Run the existing paired match under a fail-closed Windows engine CPU guard.

Move selection, time limits, adjudication and PGN generation remain those of
compare_engines.py. Native process enumeration avoids spawning a second workload
every sample. Zero observed external engine CPU ticks are required; newly seen,
disappearing or inaccessible external engines also invalidate the entire block.
The guard cannot observe an unknown engine that starts and exits between samples,
nor does it prove the absence of non-engine host load.
"""

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

import compare_engines as comparison


DEFAULT_ENGINE_NAMES = frozenset({"regicide.exe", "stockfish.exe", "lc0.exe", "lc0-cpu.exe"})
CPU_TICKS_PER_SECOND = 10_000_000


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    name: str
    creation_ticks: int | None = None
    cpu_ticks: int | None = None
    error: str | None = None
    exited_during_snapshot: bool = False

    @property
    def identity(self):
        return self.pid, self.creation_ticks


def descendant_chain(process, processes, root_identity):
    """Return a checked ancestry, or None; account for the venv launcher layer.

    Every ancestor must exist in the current snapshot and predate its child.
    Remembered exact engine identities handle a launcher that exits afterwards.
    """
    chain = [process.pid]
    current = process
    seen = {process.pid}
    while current.pid != root_identity[0]:
        parent = processes.get(current.parent_pid)
        if parent is None or parent.pid in seen or parent.creation_ticks is None:
            return None
        if current.creation_ticks is None or parent.creation_ticks > current.creation_ticks:
            return None
        chain.append(parent.pid)
        seen.add(parent.pid)
        current = parent
    return chain if current.identity == root_identity else None


class WindowsProcesses:
    """Toolhelp process tree plus GetProcessTimes, no WMI helper processes."""

    def __init__(self, engine_names=DEFAULT_ENGINE_NAMES, extra_engine_pids=()):
        if sys.platform != "win32":
            raise RuntimeError("The clean CPU guard requires Windows native process APIs.")
        self.engine_names = frozenset(name.casefold() for name in engine_names)
        self.extra_engine_pids = frozenset(extra_engine_pids)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.engine_handles = {}

        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

        self.entry_type = ProcessEntry
        signatures = {
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL),
            "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4,
                                wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def is_engine(self, process):
        name = process.name.casefold()
        return (name in self.engine_names or process.pid in self.extra_engine_pids
                or (bool(self.engine_names) and name.endswith(".exe")
                    and (name.startswith("stockfish") or name.startswith("regicide"))))

    def times(self, process):
        handle = self.kernel.OpenProcess(0x101000, False, process.pid)
        if not handle:
            error = ctypes.get_last_error()
            cached = self.engine_handles.get(process.pid)
            # A process may quit between enumeration and the timer query.
            # Reuse an already proven identity only if no process can now be
            # opened (INVALID_PARAMETER) and that exact held object has exited.
            # Access denial and an existing/reused PID are never ignored.
            if error == 87 and cached is not None and self.kernel.WaitForSingleObject(cached[0], 0) == 0:
                return self._handle_times(cached[0], cached[1])
            return ProcessInfo(process.pid, process.parent_pid, process.name,
                               error=f"OpenProcess error {error}")
        retained = False
        try:
            measured = self._handle_times(handle, process)
            if self.is_engine(process) and measured.error is None:
                cached = self.engine_handles.get(process.pid)
                if cached is None or cached[1].identity != measured.identity:
                    if cached is not None:
                        self.kernel.CloseHandle(cached[0])
                    self.engine_handles[process.pid] = (handle, measured)
                    retained = True
            return measured
        finally:
            if not retained:
                self.kernel.CloseHandle(handle)

    def _handle_times(self, handle, process):
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                          ctypes.byref(kernel), ctypes.byref(user)):
            return ProcessInfo(process.pid, process.parent_pid, process.name,
                               error=f"GetProcessTimes error {ctypes.get_last_error()}")
        def value(item):
            return item.dwLowDateTime | (item.dwHighDateTime << 32)
        exit_status = self.kernel.WaitForSingleObject(handle, 0)
        if exit_status not in (0, 258):  # signaled process object or WAIT_TIMEOUT
            return ProcessInfo(process.pid, process.parent_pid, process.name,
                               error=f"Process exit-state query error {ctypes.get_last_error()}")
        return ProcessInfo(process.pid, process.parent_pid, process.name,
                           value(creation), value(kernel) + value(user),
                           exited_during_snapshot=exit_status == 0)

    def close(self):
        for handle, _ in self.engine_handles.values():
            self.kernel.CloseHandle(handle)
        self.engine_handles.clear()

    def snapshot(self):
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
        processes = {}
        try:
            entry = self.entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            success = self.kernel.Process32FirstW(handle, ctypes.byref(entry))
            while success:
                process = ProcessInfo(int(entry.th32ProcessID), int(entry.th32ParentProcessID),
                                      str(entry.szExeFile))
                processes[process.pid] = process
                success = self.kernel.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise OSError(ctypes.get_last_error(), "Native process enumeration did not complete")
        finally:
            self.kernel.CloseHandle(handle)
        # Only query engines and their ancestors, including the actual Python
        # runtime. No per-process timers for the rest of the host are collected.
        selected = [process.pid for process in processes.values()
                    if self.is_engine(process) or process.pid == os.getpid()]
        for pid in selected:
            seen = set()
            while pid in processes and pid not in seen:
                seen.add(pid)
                process = processes[pid]
                if process.creation_ticks is None and process.error is None:
                    process = self.times(process)
                    processes[pid] = process
                pid = process.parent_pid
        return processes


class ExternalEngineLoad(RuntimeError):
    pass


class EngineCpuGuard:
    def __init__(self, provider, output, interval=0.5, abort_file=None):
        self.provider, self.output, self.interval = provider, Path(output), interval
        self.abort_file = Path(abort_file) if abort_file else None
        self.root_identity = None
        self.known_owned = {}
        self.previous_external = None
        self.lock = threading.RLock()
        self.stop_event, self.failed_event = threading.Event(), threading.Event()
        self.thread = None
        self.stream = None
        self.start_monotonic = time.monotonic()
        self.last_monotonic = None
        self.summary = {"schema_version": 1, "started_utc": utc_now(), "finished_utc": None,
                        "monitor_root_pid": os.getpid(), "monitor_root_creation_ticks": None,
                        "poll_seconds": interval, "max_allowed_snapshot_gap_seconds": 2.0,
                        "cpu_ticks_per_second": CPU_TICKS_PER_SECOND,
                        "external_cpu_tolerance_ticks": 0, "engine_names": sorted(provider.engine_names),
                        "new_or_disappearing_external_engine_is_failure": True,
                        "snapshot_count": 0, "max_snapshot_gap_seconds": 0.0,
                        "max_snapshot_duration_seconds": 0.0,
                        "external_cpu_increase_ticks": 0, "owned_engines": [], "transport_engines": {},
                        "external_engine_identities": [], "observed_external_engine_cpu_increase": False,
                        "contaminated": False, "valid_for_comparison": False, "failure": None,
                        "completed_idle_seconds": 0.0,
                        "scope": "Zero observed external named engine CPU ticks; sampled process discovery. Unknown/transient engines between samples and non-engine host load are not excluded."}

    def fail(self, reason, kind="monitor_error", retry_allowed=False):
        if not self.failed_event.is_set():
            self.summary.update(contaminated=True, failure={"utc": utc_now(), "reason": reason,
                                                           "kind": kind, "retry_allowed": retry_allowed})
            self.failed_event.set()

    def sample(self, phase):
        with self.lock:
            now = time.monotonic()
            try:
                processes = self.provider.snapshot()
                if self.root_identity is None:
                    root = processes.get(os.getpid())
                    if root is None or root.creation_ticks is None:
                        raise RuntimeError("Cannot establish the actual monitor Python identity")
                    self.root_identity = root.identity
                    self.summary["monitor_root_creation_ticks"] = root.creation_ticks
                external, rows = {}, []
                for process in processes.values():
                    if not self.provider.is_engine(process):
                        continue
                    if process.error is not None or process.creation_ticks is None or process.cpu_ticks is None:
                        self.fail(f"Engine CPU cannot be measured: PID {process.pid}: {process.error}")
                        rows.append({"pid": process.pid, "error": process.error})
                        continue
                    chain = descendant_chain(process, processes, self.root_identity)
                    owned = process.identity in self.known_owned or chain is not None
                    if owned:
                        if process.identity not in self.known_owned:
                            evidence = {"pid": process.pid, "creation_ticks": process.creation_ticks,
                                        "parent_pid": process.parent_pid, "name": process.name,
                                        "ancestry_to_monitor": chain,
                                        "ancestry_processes": [
                                            {"pid": processes[pid].pid,
                                             "parent_pid": processes[pid].parent_pid,
                                             "creation_ticks": processes[pid].creation_ticks} for pid in chain]}
                            self.known_owned[process.identity] = evidence
                            self.summary["owned_engines"].append(evidence)
                            if len(self.known_owned) > 2:
                                self.fail("An unexpected third descendant engine appeared")
                    else:
                        external[process.identity] = process
                        if process.exited_during_snapshot:
                            self.fail("External engine exited during the native CPU snapshot",
                                      "external_identity_change", True)
                    rows.append({"pid": process.pid, "parent_pid": process.parent_pid,
                                 "name": process.name, "creation_ticks": process.creation_ticks,
                                 "cpu_ticks": process.cpu_ticks, "owned": owned,
                                 "exited_during_snapshot": process.exited_during_snapshot,
                                 "ancestry_to_monitor": chain,
                                 "ancestry_processes": [
                                     {"pid": processes[pid].pid, "parent_pid": processes[pid].parent_pid,
                                      "creation_ticks": processes[pid].creation_ticks}
                                     for pid in chain] if chain is not None else None})
                delta = 0
                if self.previous_external is not None:
                    if set(external) != set(self.previous_external):
                        self.fail("External engine identity set changed during the guarded interval",
                                  "external_identity_change", True)
                    for identity, process in external.items():
                        if identity in self.previous_external:
                            increase = process.cpu_ticks - self.previous_external[identity].cpu_ticks
                            if increase < 0:
                                self.fail("External engine CPU counter decreased unexpectedly")
                            delta += max(0, increase)
                    if delta > 0:
                        self.summary["external_cpu_increase_ticks"] += delta
                        self.summary["observed_external_engine_cpu_increase"] = True
                        self.fail(f"External engine CPU increased by {delta} native ticks", "external_cpu", True)
                else:
                    self.summary["external_engine_identities"] = [
                        {"pid": process.pid, "creation_ticks": process.creation_ticks,
                         "name": process.name, "initial_cpu_ticks": process.cpu_ticks}
                        for process in external.values()]
                self.previous_external = external
                if self.abort_file is not None and self.abort_file.exists():
                    self.fail("Runner requested an abort; no later game or time block is allowed")
                finished_now = time.monotonic()
                duration = finished_now - now
                gap = finished_now - self.last_monotonic if self.last_monotonic is not None else duration
                if duration > self.summary["max_allowed_snapshot_gap_seconds"]:
                    self.fail("A native CPU snapshot exceeded the fail-closed 2 second bound", "sampling_gap", True)
                if gap > self.summary["max_allowed_snapshot_gap_seconds"]:
                    self.fail("CPU sampling gap exceeded the fail-closed 2 second bound", "sampling_gap", True)
                self.last_monotonic = finished_now
                self.summary["max_snapshot_gap_seconds"] = max(self.summary["max_snapshot_gap_seconds"], gap)
                self.summary["max_snapshot_duration_seconds"] = max(self.summary["max_snapshot_duration_seconds"], duration)
                self.summary["snapshot_count"] += 1
                record = {"utc": utc_now(), "elapsed_seconds": finished_now - self.start_monotonic,
                          "phase": phase, "gap_seconds": gap, "processes": rows,
                          "snapshot_duration_seconds": duration,
                          "external_cpu_delta_ticks": delta, "contaminated": self.summary["contaminated"]}
                if self.stream is not None:
                    self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    self.stream.flush()
            except Exception as exc:
                self.fail(f"CPU guard failed closed: {type(exc).__name__}: {exc}")

    def assert_clean(self):
        if self.failed_event.is_set():
            raise ExternalEngineLoad(self.summary["failure"]["reason"])

    def register_transport(self, pid, role, command):
        self.sample("owned-engine-started")
        self.assert_clean()
        matches = [item for identity, item in self.known_owned.items() if identity[0] == pid]
        if len(matches) != 1 or role not in ("baseline", "candidate") or role in self.summary["transport_engines"]:
            self.fail("UCI transport PID could not be linked uniquely to its declared engine role")
            self.assert_clean()
        self.summary["transport_engines"][role] = dict(matches[0], command=list(command))

    def assert_transports(self):
        transports = self.summary["transport_engines"]
        registered = {(item["pid"], item["creation_ticks"]) for item in transports.values()}
        if set(transports) != {"baseline", "candidate"} or registered != set(self.known_owned) or len(registered) != 2:
            self.fail("Exactly two distinct UCI transports must match every excluded owned engine")
        self.assert_clean()

    def start(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.output.open("x", encoding="utf-8", newline="\n")
        self.sample("initial")
        self.assert_clean()
        self.thread = threading.Thread(target=self._loop, name="external-engine-cpu-guard", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            self.sample("running")

    def require_idle(self, seconds=10.0):
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            self.assert_clean()
            time.sleep(min(0.1, seconds))
        self.sample("idle-complete")
        self.assert_clean()
        self.summary["completed_idle_seconds"] = time.monotonic() - start

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                self.fail("CPU monitor thread did not stop")
        if self.stream is not None:
            self.sample("finished")
            self.stream.close()
            self.stream = None
        if hasattr(self.provider, "close"):
            self.provider.close()
        self.summary["finished_utc"] = utc_now()


class GuardedEngine:
    def __init__(self, engine, guard):
        self.engine, self.guard = engine, guard

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def play(self, *arguments, **keywords):
        self.guard.assert_clean()
        result = self.engine.play(*arguments, **keywords)
        # Preserve the board before an in-flight move if its interval was dirty.
        self.guard.assert_clean()
        return result


def execute_guarded_match(args, guard):
    original_launch = comparison.chess.engine.SimpleEngine.popen_uci
    original_play = comparison.play_one

    def launch(*arguments, **keywords):
        guard.assert_clean()
        command = arguments[0]
        executable = Path(command[0]).resolve()
        roles = [role for role in ("baseline", "candidate") if executable == getattr(args, role)]
        if len(roles) != 1:
            raise ValueError("UCI launch command does not identify exactly one declared binary")
        engine = original_launch(*arguments, **keywords)
        try:
            guard.register_transport(engine.transport.get_pid(), roles[0], command)
        except BaseException:
            engine.quit()
            engine.close()
            raise
        return GuardedEngine(engine, guard)

    def play_one(*arguments, **keywords):
        guard.sample("before-game")
        guard.assert_transports()
        original_play(*arguments, **keywords)
        guard.sample("after-game")
        guard.assert_clean()

    with patch.object(comparison.chess.engine.SimpleEngine, "popen_uci", side_effect=launch), \
            patch.object(comparison, "play_one", side_effect=play_one):
        return comparison.run_match(args)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--harness-commit", required=True)
    parser.add_argument("--baseline-sha256", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--openings", type=Path, required=True)
    parser.add_argument("--movetime", choices=(100, 1000), type=int, required=True)
    parser.add_argument("--pgn", type=Path, required=True)
    parser.add_argument("--monitor-json", type=Path, required=True)
    parser.add_argument("--monitor-log", type=Path, required=True)
    parser.add_argument("--abort-file", type=Path, required=True)
    args = parser.parse_args(argv)
    return args


def main(argv=None):
    declared = parse_arguments(argv)
    for path in (declared.monitor_json, declared.monitor_log, declared.pgn, declared.pgn.with_suffix(".json")):
        if path.exists():
            raise ValueError(f"Preserve previous output: {path}")
    for role in ("baseline", "candidate"):
        if sha256(getattr(declared, role)) != getattr(declared, role + "_sha256"):
            raise ValueError(f"The {role} binary differs from the declared frozen executable")
    if comparison.git_command("rev-parse", "HEAD") != declared.harness_commit:
        raise ValueError("The harness checkout is not the declared commit")
    if comparison.git_command("status", "--porcelain"):
        raise ValueError("The harness checkout is not clean")
    args = comparison.arguments([
        str(declared.baseline), str(declared.candidate), "--movetime", str(declared.movetime),
        "--openings", str(declared.openings), "--max-plies", "500", "--draw-policy", "claim",
        "--pgn", str(declared.pgn), "--baseline-commit", declared.baseline_commit,
        "--candidate-commit", declared.candidate_commit,
        "--candidate-options", '{"Hash":16,"Move Overhead":20,"PVS":true,"UseSEE":true}'])
    guard = EngineCpuGuard(WindowsProcesses(), declared.monitor_log, abort_file=declared.abort_file)
    metadata, code, error = None, 2, None
    try:
        guard.start()
        guard.require_idle()
        metadata, code = execute_guarded_match(args, guard)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr, flush=True)
    finally:
        guard.close()
        if metadata is not None and code == 0 and metadata["completed"]:
            try:
                guard.assert_transports()
            except ExternalEngineLoad as exc:
                error = f"{type(exc).__name__}: {exc}"
                code = 2
        if guard.summary["contaminated"]:
            code = 2
        guard.summary["valid_for_comparison"] = bool(code == 0 and metadata is not None and metadata["completed"])
        guard.summary.update(harness_commit=declared.harness_commit, candidate_commit=declared.candidate_commit,
                             baseline_commit=declared.baseline_commit, movetime_ms=declared.movetime,
                             monitor_log_sha256=sha256(declared.monitor_log) if declared.monitor_log.exists() else None,
                             error=error, exit_code=code)
        with declared.monitor_json.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(guard.summary, ensure_ascii=False, indent=2) + "\n")
        if metadata is not None:
            metadata["external_engine_guard"] = guard.summary
            metadata["valid_for_comparison"] = guard.summary["valid_for_comparison"]
            metadata["harness_source_commit"] = declared.harness_commit
            if guard.summary["contaminated"]:
                metadata["errors"].append("Entire time block invalid: " + guard.summary["failure"]["reason"])
                metadata["exit_code"] = 2
            comparison.save_outputs(args, metadata)
    return code


if __name__ == "__main__":
    sys.exit(main())
