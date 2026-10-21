"""Native Windows owned-cohort retirement probe. Touches only its own Popen objects."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))
from m2_formal import OwnedJob
from m2_load_guard import GuardMonitor, LoadPolicy

EXPECTED_GUARD = "f22ef91446b59b74db101bd7ddb96426fe8a50d52b1fdf6f76bdcbadf6873610"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def persist(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    source_paths = [ROOT / "scripts" / name for name in ("m2_load_guard.py", "see_clean_guard.py", "m2_formal.py")]
    receipt = {"status": "RUNNING", "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "command": [sys.executable, *sys.argv], "python": sys.version, "base_python": sys._base_executable,
               "probe_sha256": sha(__file__), "sources_before": {str(p): sha(p) for p in source_paths},
               "policy": {"max_nonworker_cpu_fraction": .20, "interval_seconds": .5,
                          "max_sample_gap_seconds": 2., "host_window_seconds": 2.},
               "scope": "Four newly created Python roots and one short-lived child each. No chess engine or existing process is stopped.",
               "workers": [], "post_detach_samples": []}
    persist(folder / "execution.json", receipt)
    guard, job, workers, identities = None, None, [], []
    started = time.monotonic()
    worker_code = ("import json,pathlib,subprocess,sys;sys.stdin.readline();"
                   "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(2.5)']);"
                   "pathlib.Path(sys.argv[1]).write_text(json.dumps({'root':__import__('os').getpid(),'child':p.pid}));"
                   "p.wait();raise SystemExit(p.returncode)")
    try:
        if receipt["sources_before"][str(source_paths[0])] != EXPECTED_GUARD:
            raise RuntimeError("Guard source differs from the peer-reviewed frozen source")
        guard = GuardMonitor(folder / "load.jsonl", LoadPolicy(.20))
        job = OwnedJob()
        guard.require_idle(5.)
        guard.start_timer()
        for index in range(4):
            state_file = folder / f"worker-{index}.json"
            process = subprocess.Popen([sys._base_executable, "-u", "-c", worker_code, str(state_file)],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            workers.append(process)
            job.assign(process)
            identity = guard.identify_worker(process.pid)
            guard.attach_worker(*identity)
            identities.append(identity)
            receipt["workers"].append({"pid": process.pid, "creation_ticks": identity[1], "state_file": str(state_file)})
        for process in workers:
            process.stdin.write(b"go\n")
            process.stdin.flush()
            process.stdin.close()
        deadline = time.monotonic() + 15.
        remaining = {p.pid: p for p in workers}
        while remaining and time.monotonic() < deadline:
            time.sleep(.5)
            guard.sample("native-owned-cohort")
            guard.assert_clean()
            for pid, process in tuple(remaining.items()):
                if process.poll() is not None:
                    if process.returncode:
                        raise RuntimeError(f"Owned worker failed: {pid}={process.returncode}")
                    # poll may observe exit just after the previous snapshot.
                    guard.sample("root-exited-final-timer")
                    guard.assert_clean()
                    guard.detach_worker(pid)
                    # Release this probe's own Popen process-object reference;
                    # the monitor still owns the verified retired timer handle.
                    process._handle.Close()
                    del remaining[pid]
        if remaining:
            raise RuntimeError("Owned cohort exceeded its 15-second budget")
        for worker in receipt["workers"]:
            worker.update(json.loads(Path(worker["state_file"]).read_text()))
        # Check convergence while the guard remains open, rather than treating
        # close() clearing a dictionary as evidence that the lifecycle converged.
        for index in range(12):
            time.sleep(.5)
            sample = guard.sample("post-detach-convergence")
            guard.assert_clean()
            receipt["post_detach_samples"].append({"index": index + 1,
                "retired_handle_count": sample["retired_handle_count"],
                "retirement_events": sample.get("retirement_events", []), "failure": sample["failure"]})
            if index >= 1 and sample["retired_handle_count"] == 0:
                break
        if receipt["post_detach_samples"][-1]["retired_handle_count"] != 0:
            raise RuntimeError("Retired handles did not converge to zero while the guard remained open")
        guard.close()
        guard.assert_clean()
        summary = guard.summary
        if not summary["valid_for_load_check"]:
            raise RuntimeError("Native lifecycle guard summary is invalid")
        receipt["status"] = "PASS"
    except BaseException as error:
        receipt["status"] = "FAIL"
        receipt["error"] = f"{type(error).__name__}: {error}"
    finally:
        if job is not None:
            job.close()
        for process in workers:
            if process.returncode is None:
                try:
                    process.wait(timeout=5.)
                except subprocess.TimeoutExpired:
                    process.terminate()  # Only this helper's exact Popen child.
                    process.wait(timeout=5.)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process._handle.Close()
        if guard is not None:
            guard.close()
            receipt["summary"] = guard.summary
            receipt["remaining_owned_handles_after_close"] = len(guard.provider.owned_handles)
            receipt["remaining_retired_handles_after_close"] = len(guard.provider.retired_handles)
        receipt["all_own_roots_exited"] = all(p.returncode is not None for p in workers)
        receipt["sources_after"] = {str(p): sha(p) for p in source_paths}
        receipt["sources_unchanged"] = receipt["sources_before"] == receipt["sources_after"]
        if not receipt["sources_unchanged"]:
            receipt["status"] = "FAIL"
        receipt["elapsed_seconds"] = time.monotonic() - started
        receipt["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        persist(folder / "execution.json", receipt)
    print(json.dumps({"status": receipt["status"], "error": receipt.get("error"),
                      "post_detach_samples": receipt["post_detach_samples"],
                      "summary": receipt.get("summary")}, indent=2))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
