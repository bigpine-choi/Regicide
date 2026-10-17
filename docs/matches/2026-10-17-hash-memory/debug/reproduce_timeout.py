import hashlib
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
data = json.loads((OUT.parent / 'time100.json').read_text(encoding='utf-8'))
game = data['games'][-1]
history = game['uci_moves']
results = []


class RawEngine:
    def __init__(self, role, label):
        self.role = role
        self.label = label
        self.process = subprocess.Popen(data['engines'][role]['command'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1)
        self.queue = queue.Queue()
        self.log = (OUT / (label + '.stdout.log')).open('w', encoding='utf-8')
        self.start = time.perf_counter()
        def reader():
            for line in self.process.stdout:
                self.queue.put(line.rstrip('\n'))
            self.queue.put(None)
        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()
        self.send('uci')
        self.until('uciok')
        self.send('isready')
        self.until('readyok')
        self.send('ucinewgame')

    def send(self, command):
        self.log.write(f'{time.perf_counter()-self.start:.6f} > {command}\n')
        self.log.flush()
        self.process.stdin.write(command + '\n')
        self.process.stdin.flush()

    def until(self, prefix, timeout=3):
        deadline = time.perf_counter() + timeout
        lines = []
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f'{self.label}: missing {prefix}')
            line = self.queue.get(timeout=remaining)
            if line is None:
                raise RuntimeError(f'{self.label}: process EOF {self.process.poll()}')
            self.log.write(f'{time.perf_counter()-self.start:.6f} < {line}\n')
            self.log.flush()
            lines.append(line)
            if line.startswith(prefix):
                return lines

    def search(self, moves=None, fen=None, label='target'):
        self.send('position fen ' + fen if fen is not None else
                  'position startpos moves ' + ' '.join(moves))
        started = time.perf_counter()
        self.send('go movetime 100')
        try:
            lines = self.until('bestmove')
            record = dict(role=self.role, session=self.label, search=label, plies=len(moves or []),
                          elapsed=time.perf_counter()-started, success=True,
                          final_info=[line for line in lines if line.startswith('info ')][-2:],
                          bestmove=lines[-1])
        except Exception as exc:
            self.send('stop')
            try:
                tail = self.until('bestmove', 2)
            except Exception as stop_exc:
                tail = [repr(stop_exc)]
            record = dict(role=self.role, session=self.label, search=label, success=False,
                          elapsed=time.perf_counter()-started, error=repr(exc), stop_tail=tail)
        results.append(record)
        (OUT / 'raw-reproduction.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(json.dumps(record), flush=True)
        return record['success']

    def close(self):
        if self.process.poll() is None:
            self.send('quit')
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.log.close()


for role in ('candidate', 'baseline'):
    for repeat in range(3):
        engine = RawEngine(role, f'{role}-fresh-history-{repeat}')
        try:
            engine.search(history)
        finally:
            engine.close()
    engine = RawEngine(role, f'{role}-fen-and-warm')
    try:
        engine.search(fen=game['final_fen'], label='fen-import')
        for repeat in range(5):
            if not engine.search(history, label=f'warm-target-{repeat}'):
                break
    finally:
        engine.close()
    engine = RawEngine(role, f'{role}-full-black-replay')
    try:
        for plies in range(len(game['opening_uci_moves']) + 1, len(history), 2):
            if not engine.search(history[:plies], label=f'historic-ply-{plies+1}'):
                break
        else:
            engine.search(history, label='target-after-full-replay')
    finally:
        engine.close()
