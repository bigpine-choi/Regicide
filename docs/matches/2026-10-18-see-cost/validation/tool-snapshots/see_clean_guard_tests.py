"""Cheap guard unit checks and optional real Windows owned-worker CPU smoke.

The smoke creates only its own waiting Python worker and monitor child. It never
signals, closes or modifies a user engine. Run before measured engine matches.
"""

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import see_clean_guard as guard_module


ProcessInfo = guard_module.ProcessInfo


def root_process():
    return ProcessInfo(os.getpid(), 1, "python.exe", 100, 0)


def engine(pid, parent=None, creation=200, cpu=0, error=None):
    return ProcessInfo(pid, parent if parent is not None else 1, "regicide.exe", creation, cpu, error)


class FakeProcesses:
    engine_names = frozenset({"regicide.exe"})

    def __init__(self, *snapshots):
        self.snapshots = [{item.pid: item for item in (root_process(), *snapshot)} for snapshot in snapshots]
        self.index = 0

    def is_engine(self, process):
        return process.name == "regicide.exe"

    def snapshot(self):
        result = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return result


class GuardTests(unittest.TestCase):
    def new_guard(self, *snapshots):
        return guard_module.EngineCpuGuard(FakeProcesses(*snapshots), Path("unused-test-output.jsonl"))

    def test_venv_runtime_ancestry_excludes_both_owned_engines(self):
        launcher = ProcessInfo(910001, os.getpid(), "python.exe", 150, 0)
        child = engine(910002, launcher.pid)
        other = engine(910003, os.getpid(), creation=201)
        external = engine(920001, cpu=10)
        guard = self.new_guard([launcher, child, other, external],
                               [launcher, engine(child.pid, launcher.pid, cpu=10000), other, external])
        guard.sample("start")
        guard.sample("next")
        self.assertFalse(guard.summary["contaminated"])
        self.assertEqual(len(guard.known_owned), 2)
        self.assertEqual(guard.summary["owned_engines"][0]["ancestry_to_monitor"],
                         [child.pid, launcher.pid, os.getpid()])
        self.assertEqual([item["pid"] for item in guard.summary["external_engine_identities"]], [external.pid])

    def test_one_native_cpu_tick_contaminates(self):
        guard = self.new_guard([engine(920001, cpu=10)], [engine(920001, cpu=11)])
        guard.sample("start")
        guard.sample("next")
        self.assertTrue(guard.summary["contaminated"])
        self.assertEqual(guard.summary["external_cpu_increase_ticks"], 1)
        with self.assertRaises(guard_module.ExternalEngineLoad):
            guard.assert_clean()

    def test_new_even_idle_external_engine_contaminates(self):
        guard = self.new_guard([], [engine(920001)])
        guard.sample("start")
        guard.sample("next")
        self.assertIn("identity set changed", guard.summary["failure"]["reason"])

    def test_disappearing_external_engine_contaminates(self):
        guard = self.new_guard([engine(920001)], [])
        guard.sample("start")
        guard.sample("next")
        self.assertTrue(guard.summary["contaminated"])

    def test_external_exit_inside_final_snapshot_contaminates(self):
        process = ProcessInfo(920001, 1, "regicide.exe", 200, 10, exited_during_snapshot=True)
        guard = self.new_guard([engine(920001, cpu=10)], [process])
        guard.sample("start")
        guard.sample("finished")
        self.assertTrue(guard.summary["contaminated"])
        self.assertEqual(guard.summary["failure"]["kind"], "external_identity_change")
        self.assertTrue(guard.summary["failure"]["retry_allowed"])
        self.assertEqual(guard.summary["external_cpu_increase_ticks"], 0)

    def test_exact_owned_exit_inside_snapshot_is_safe_cleanup(self):
        process = ProcessInfo(910001, os.getpid(), "regicide.exe", 200, 10,
                              exited_during_snapshot=True)
        guard = self.new_guard([engine(910001, os.getpid())], [process])
        guard.sample("start")
        guard.sample("finished")
        self.assertFalse(guard.summary["contaminated"])

    def test_reused_pid_is_a_new_identity(self):
        guard = self.new_guard([engine(920001)], [engine(920001, creation=300)])
        guard.sample("start")
        guard.sample("next")
        self.assertTrue(guard.summary["contaminated"])

    def test_inaccessible_engine_fails_closed(self):
        guard = self.new_guard([engine(920001, creation=None, cpu=None, error="access denied")])
        guard.sample("start")
        self.assertIn("cannot be measured", guard.summary["failure"]["reason"])

    def test_counter_decrease_fails_closed(self):
        guard = self.new_guard([engine(920001, cpu=10)], [engine(920001, cpu=9)])
        guard.sample("start")
        guard.sample("next")
        self.assertIn("decreased", guard.summary["failure"]["reason"])

    def test_owned_identity_survives_departed_launcher(self):
        launcher = ProcessInfo(910001, os.getpid(), "python.exe", 150, 0)
        child = engine(910002, launcher.pid)
        guard = self.new_guard([launcher, child], [engine(child.pid, launcher.pid, cpu=100)])
        guard.sample("start")
        guard.sample("next")
        self.assertFalse(guard.summary["contaminated"])

    def test_reused_parent_pid_cannot_prove_ownership(self):
        future_parent = ProcessInfo(910001, os.getpid(), "python.exe", 999, 0)
        child = engine(910002, future_parent.pid)
        self.assertIsNone(guard_module.descendant_chain(
            child, {item.pid: item for item in (root_process(), future_parent, child)}, root_process().identity))

    def test_unexpected_third_descendant_engine_contaminates(self):
        guard = self.new_guard([engine(910001, os.getpid()), engine(910002, os.getpid()),
                                engine(910003, os.getpid())])
        guard.sample("start")
        self.assertIn("third descendant", guard.summary["failure"]["reason"])

    def test_two_transport_roles_match_every_owned_identity(self):
        guard = self.new_guard([engine(910001, os.getpid()), engine(910002, os.getpid())])
        guard.register_transport(910001, "baseline", ["old.exe", "uci"])
        guard.register_transport(910002, "candidate", ["new.exe", "uci"])
        guard.assert_transports()
        self.assertEqual(set(guard.summary["transport_engines"]), {"baseline", "candidate"})

    def test_transport_pid_must_be_owned(self):
        guard = self.new_guard([engine(910001, os.getpid())])
        with self.assertRaises(guard_module.ExternalEngineLoad):
            guard.register_transport(920001, "baseline", ["old.exe", "uci"])

    def test_sampling_gap_fails_closed(self):
        guard = self.new_guard([])
        guard.last_monotonic = time.monotonic() - 2.5
        guard.sample("late")
        self.assertIn("2 second", guard.summary["failure"]["reason"])

    def test_slow_final_snapshot_itself_fails_closed(self):
        guard = self.new_guard([])
        guard.start_monotonic = 99
        with patch.object(guard_module.time, "monotonic", side_effect=[100.0, 103.0]):
            guard.sample("finished")
        self.assertIn("snapshot exceeded", guard.summary["failure"]["reason"])
        self.assertEqual(guard.summary["max_snapshot_duration_seconds"], 3.0)

    def test_guard_prevents_subsequent_search(self):
        guard = self.new_guard([])
        guard.fail("injected external load")
        class NeverCalled:
            def play(self):
                raise AssertionError("a later search was started")
        with self.assertRaises(guard_module.ExternalEngineLoad):
            guard_module.GuardedEngine(NeverCalled(), guard).play()

    def test_guard_rejects_inflight_move_and_preserves_unfinished_board(self):
        import chess
        import chess.engine
        from types import SimpleNamespace
        guard = self.new_guard([])
        class Inflight:
            def play(self, *arguments, **keywords):
                guard.fail("injected load during search")
                return chess.engine.PlayResult(chess.Move.from_uci("e2e4"), None)
        original = {"name": "empty", "moves": [], "fen_after_opening": chess.STARTING_FEN}
        record = guard_module.comparison.new_record(1, 1, original, True)
        engines = {role: guard_module.GuardedEngine(Inflight(), guard) for role in ("baseline", "candidate")}
        args = SimpleNamespace(nodes=None, depth=None, movetime=100, max_plies=500, draw_policy="claim")
        with self.assertRaises(guard_module.ExternalEngineLoad):
            guard_module.comparison.play_one(args, engines, record)
        self.assertFalse(record["completed"])
        self.assertEqual(record["uci_moves"], [])
        self.assertEqual(record["result"], "*")
        self.assertEqual(record["final_fen"], chess.STARTING_FEN)


def monitor_child(worker_pid, ready, result):
    provider = guard_module.WindowsProcesses(engine_names=(), extra_engine_pids={worker_pid})
    guard = guard_module.EngineCpuGuard(provider, result.with_suffix(".jsonl"), interval=0.5)
    try:
        guard.start()
        guard.require_idle(1.0)
        ready.write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 15
        while not guard.failed_event.wait(0.1) and time.monotonic() < deadline:
            pass
    finally:
        guard.close()
        result.write_text(json.dumps(guard.summary, indent=2) + "\n", encoding="utf-8")
    return 0 if guard.summary["external_cpu_increase_ticks"] > 0 else 2


def windows_smoke(output):
    if sys.platform != "win32":
        print("SKIP real native Windows CPU smoke on this platform", flush=True)
        return
    output.mkdir(parents=True, exist_ok=True)
    event_name = f"Local\\RegicideGuardSmoke-{os.getpid()}-{time.time_ns()}"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateEventW.restype = ctypes.c_void_p
    kernel.SetEvent.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    event = kernel.CreateEventW(None, True, False, event_name)
    if not event:
        raise OSError(ctypes.get_last_error(), "Cannot create owned smoke event")
    worker_ready, monitor_ready = output / "worker.ready", output / "monitor.ready"
    result = output / "windows-smoke.json"
    if result.exists():
        raise ValueError("Preserve previous Windows smoke evidence")
    worker_code = r'''
import ctypes, pathlib, sys, time
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
kernel.OpenEventW.restype = ctypes.c_void_p
kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
handle = kernel.OpenEventW(0x100000, False, sys.argv[1])
if not handle: raise OSError(ctypes.get_last_error())
pathlib.Path(sys.argv[2]).write_text("ready", encoding="ascii")
kernel.WaitForSingleObject(handle, 30000)
deadline = time.perf_counter() + 8
while time.perf_counter() < deadline: pass
'''
    worker, monitor = None, None
    creationflags = subprocess.CREATE_NO_WINDOW
    try:
        # sys.executable is the real runtime; a venv launcher can add another
        # layer. Identify the CPU worker's actual PID in its own ready receipt.
        worker_code = worker_code.replace('write_text("ready", encoding="ascii")',
                                          'write_text(str(__import__("os").getpid()), encoding="ascii")')
        worker = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", worker_code, event_name, str(worker_ready)],
                                  creationflags=creationflags)
        deadline = time.monotonic() + 15
        while not worker_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not worker_ready.exists():
            raise RuntimeError("Owned smoke worker did not become ready")
        worker_pid = int(worker_ready.read_text(encoding="ascii"))
        if worker_pid != worker.pid:
            raise RuntimeError("The smoke CPU worker must use the direct runtime without a launcher")
        # Let interpreter startup settle, then the native worker waits without
        # polling. It computes only after this test signals its private event.
        time.sleep(0.25)
        monitor = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    "--monitor-child", str(worker_pid), str(monitor_ready), str(result)],
                                   creationflags=creationflags)
        deadline = time.monotonic() + 15
        while not monitor_ready.exists() and monitor.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not monitor_ready.exists():
            raise RuntimeError("Native monitor did not confirm the idle owned worker")
        if not kernel.SetEvent(event):
            raise OSError(ctypes.get_last_error(), "Cannot activate the owned smoke worker")
        if monitor.wait(timeout=15) != 0:
            raise AssertionError("The real native guard did not detect worker CPU")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["contaminated"] and data["external_cpu_increase_ticks"] > 0, data
        assert data["completed_idle_seconds"] >= 1.0 and not data["owned_engines"], data
        print("PASS native owned-worker idle -> positive CPU -> fail-closed contamination; evidence: " + str(result), flush=True)
    finally:
        # Only the exact two subprocesses created above are cleaned up.
        for child in (monitor, worker):
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
        kernel.CloseHandle(event)


def real_uci_smoke(output, baseline, candidate):
    """Already public Fool's Mate, never any fresh comparison opening."""
    if sys.platform != "win32":
        return
    output.mkdir(parents=True, exist_ok=True)
    opening = output / "fools-mate-only.json"
    if opening.exists():
        raise ValueError("Preserve previous real UCI smoke output")
    opening.write_text(json.dumps({"openings": [{"name": "Existing public Fool's Mate in one",
                                               "moves": ["f2f3", "e7e5", "g2g4"]}]}) + "\n", encoding="utf-8")
    for number in range(1, 5):
        stem = output / f"cycle-{number}"
        args = guard_module.comparison.arguments([
            str(baseline.resolve()), str(candidate.resolve()), "--depth", "1", "--max-plies", "10",
            "--openings", str(opening), "--pgn", str(stem.with_suffix(".pgn")),
            "--baseline-commit", "1441d5ec23733af570b05521620935619c39cefa",
            "--candidate-commit", "0edc06e437b934d177fc1e9b0907600b6287be72",
            "--candidate-options", '{"Hash":16,"Move Overhead":20,"PVS":true,"UseSEE":true}'])
        guard = guard_module.EngineCpuGuard(guard_module.WindowsProcesses(), stem.with_suffix(".jsonl"), interval=0.01)
        try:
            guard.start()
            guard.require_idle(0.1)
            metadata, code = guard_module.execute_guarded_match(args, guard)
        finally:
            guard.close()
        guard.assert_transports()
        assert code == 0 and metadata["completed"], metadata
        assert not guard.summary["contaminated"] and guard.summary["external_cpu_increase_ticks"] == 0, guard.summary
        assert all(game["termination"] == "checkmate" and len(game["search_moves"]) == 1
                   for game in metadata["games"]), metadata
        metadata["guard_smoke"] = guard.summary
        guard_module.comparison.save_outputs(args, metadata)
    print("PASS real UCI transport ownership, four paired mate cycles, native launch/cleanup and preserved complete PGNs", flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--monitor-child":
        return monitor_child(int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-smoke", type=Path)
    parser.add_argument("--uci-smoke", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GuardTests))
    if not result.wasSuccessful():
        return 2
    if args.windows_smoke:
        windows_smoke(args.windows_smoke.resolve())
    if args.uci_smoke:
        if args.baseline is None or args.candidate is None:
            parser.error("--uci-smoke requires --baseline and --candidate")
        real_uci_smoke(args.uci_smoke.resolve(), args.baseline, args.candidate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
