"""Short owned-process Windows probes; never touch an existing process.

Run from the repository root using its test Python. Both attempts preserve raw
receipts and exact code/binary hashes; an occupied output directory is refused.
"""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'scripts'))
from m2_formal import OwnedJob
from m2_load_guard import LoadGuardViolation, LoadPolicy, M2LoadGuard


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def persist(path, content):
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def reap_created(process):
    """Fallback applies only to the Popen object created in this helper."""
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=10)


def positive(folder):
    guard = M2LoadGuard(folder / 'load.jsonl', LoadPolicy(.20))
    job, workers, events = OwnedJob(), [], []
    result = {'expected': 'clean sampled load receipt', 'passed': False, 'events': events}
    code = ('import subprocess,sys; sys.stdin.readline(); '
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(2.5)"]); '
            'p.wait()')
    try:
        guard.require_idle(5.)
        for _ in range(4):
            process = subprocess.Popen([sys._base_executable, '-u', '-c', code],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            workers.append(process)
            job.assign(process)
            identity = guard.identify_worker(process.pid)
            guard.attach_worker(*identity)
            events.append({'event': 'registered', 'pid': process.pid, 'creation_ticks': identity[1]})
        # No host snapshot occurs between launching/attaching the four roots.
        for process in workers:
            process.stdin.write(b'go\n')
            process.stdin.flush()
        remaining = {p.pid: p for p in workers}
        deadline = time.monotonic() + 15
        while remaining and time.monotonic() < deadline:
            time.sleep(.5)
            guard.sample('positive-cohort')
            guard.assert_clean()
            for pid, process in tuple(remaining.items()):
                if process.poll() is not None:
                    if process.returncode:
                        raise RuntimeError(f'owned sleep worker failed: {pid}={process.returncode}')
                    guard.detach_worker(pid)
                    events.append({'event': 'detached', 'pid': pid, 'returncode': process.returncode})
                    del remaining[pid]
        if remaining:
            raise RuntimeError('owned sleep cohort exceeded 15 seconds')
        guard.close()
        guard.assert_clean()
        result['passed'] = guard.summary['valid_for_load_check']
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    finally:
        # This job contains only workers created above, including their children.
        job.close()
        for process in workers:
            reap_created(process)
            if process.stdin:
                process.stdin.close()
        guard.close()
        result['summary'] = guard.summary
        result['workers_exited'] = all(p.poll() is not None for p in workers)
        result['remaining_native_owned_handles'] = len(guard.provider.owned_handles)
        persist(folder / 'result.json', result)
    return result


def negative(folder):
    canary = folder / 'regicide-load-canary.exe'
    shutil.copyfile(ROOT / 'build' / 'regicide.exe', canary)
    raw = (folder / 'uci.txt').open('x', encoding='utf-8')
    lines = queue.Queue()
    job, guard, process, reader = OwnedJob(), None, None, None
    result = {'expected': 'external_cpu rejection of newly created canary only',
              'canary_sha256': sha(canary), 'passed': False}
    def send(command):
        raw.write('in ' + command + '\n')
        raw.flush()
        process.stdin.write(command + '\n')
        process.stdin.flush()
    try:
        process = subprocess.Popen([str(canary), 'uci'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding='utf-8', bufsize=1)
        job.assign(process)
        result['canary_pid'] = process.pid
        def read_output():
            for line in process.stdout:
                raw.write('out ' + line)
                raw.flush()
                lines.put(line.strip())
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        def wait_for(word):
            end = time.monotonic() + 10
            while time.monotonic() < end:
                if lines.get(timeout=max(.01, end - time.monotonic())) == word:
                    return
            raise RuntimeError(f'canary UCI did not answer {word}')
        send('uci')
        wait_for('uciok')
        send('isready')
        wait_for('readyok')
        guard = M2LoadGuard(folder / 'load.jsonl', LoadPolicy(.20))
        guard.require_idle(2.)
        result['idle_baseline_completed'] = True
        send('position startpos')
        send('go infinite')
        for _ in range(8):
            time.sleep(.5)
            guard.sample('negative-canary')
            if guard.stop_reason:
                break
        try:
            guard.assert_clean()
        except LoadGuardViolation as error:
            result['assert_clean_rejected'] = str(error)
        result['passed'] = (guard.stop_reason is not None and guard.stop_reason['kind'] == 'external_cpu'
                            and bool(result.get('assert_clean_rejected')))
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    finally:
        # Stop/quit first; the job provides fallback cleanup for this canary only.
        if process is not None and process.poll() is None:
            try:
                send('stop')
                send('quit')
                process.wait(timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
        if guard is not None:
            guard.close()
            result['summary'] = guard.summary
        job.close()
        if process is not None:
            reap_created(process)
            result['canary_exited'] = process.poll() is not None
            if process.stdin:
                process.stdin.close()
            if reader is not None:
                reader.join(timeout=5)
            process.stdout.close()
        raw.close()
        persist(folder / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ('positive', 'negative'):
        (args.output / name).mkdir()
    sources = ('scripts/m2_load_guard.py', 'scripts/see_clean_guard.py', 'scripts/m2_formal.py',
               'tests/m2_load_guard_tests.py')
    execution = {'argv': sys.argv, 'python': sys.executable, 'base_python': sys._base_executable,
                 'python_version': sys.version, 'helper_sha256': sha(__file__),
                 'sources': {path: sha(ROOT / path) for path in sources}, 'results': {}}
    persist(args.output / 'execution.json', execution)
    for name, operation in (('positive', positive), ('negative', negative)):
        execution['results'][name] = operation(args.output / name)
        persist(args.output / 'execution.json', execution)
    execution['passed'] = all(result['passed'] for result in execution['results'].values())
    execution['sources_after'] = {path: sha(ROOT / path) for path in sources}
    persist(args.output / 'execution.json', execution)
    print(json.dumps({'passed': execution['passed'], 'results': {
        name: {'passed': result['passed'], 'failure': result.get('summary', {}).get('failure'),
               'error': result.get('error')} for name, result in execution['results'].items()}}, indent=2))
    return 0 if execution['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
