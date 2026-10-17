import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
data = json.loads((ROOT / 'docs/matches/2026-10-17-hash-memory/time100-retry.json').read_text())
rows = []


def query(game, ply, extra=None, depth=7, nodes=1000000):
    suffix = '-' + extra if extra else ''
    label = f'game{game["game"]}-ply{ply}{suffix}'
    command = data['engines']['candidate']['command']
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
    messages = queue.Queue()
    def reader():
        for line in process.stdout:
            messages.put(line.rstrip())
        messages.put(None)
    threading.Thread(target=reader, daemon=True).start()
    log = (OUT / (label + '.log')).open('w')
    def send(text):
        log.write('> ' + text + '\n')
        log.flush()
        process.stdin.write(text + '\n')
        process.stdin.flush()
    def until(prefix, timeout=15):
        lines = []
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            line = messages.get(timeout=deadline - time.perf_counter())
            if line is None:
                raise RuntimeError('process exited')
            log.write('< ' + line + '\n')
            log.flush()
            lines.append(line)
            if line.startswith(prefix):
                return lines
        raise TimeoutError(prefix)
    try:
        send('uci')
        until('uciok')
        send('ucinewgame')
        moves = game['uci_moves'][:ply - 1]
        if extra:
            moves = moves + [extra]
        send('position startpos moves ' + ' '.join(moves))
        send(f'go depth {depth} nodes {nodes}')
        lines = until('bestmove')
        iterations = []
        for line in lines:
            match = re.match(r'info depth (\d+) seldepth (\d+) score (cp|mate) (-?\d+) nodes (\d+) time (\d+)(?: pv (.*))?', line)
            if match:
                iterations.append(dict(depth=int(match[1]), seldepth=int(match[2]), kind=match[3],
                                       score=int(match[4]), nodes=int(match[5]), time_ms=int(match[6]),
                                       pv=(match[7] or '').split()))
        original = next(x for x in game['search_moves'] if x['ply'] == ply)
        record = dict(game=game['game'], ply=ply, fen=original['fen_before'], played=original['move'],
                      recorded_depth=original['depth'], recorded_score=original['score'],
                      forced_first=extra, node_limit=nodes, iterations=iterations,
                      bestmove=lines[-1], success=True)
    except Exception as exc:
        record = dict(game=game['game'], ply=ply, forced_first=extra, success=False, error=repr(exc))
    finally:
        send('quit')
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        log.close()
    rows.append(record)
    (OUT / 'queries.json').write_text(json.dumps(rows, indent=2))
    print(json.dumps(record), flush=True)


picks = {1: [47], 2: [94, 96], 7: [41], 10: [44], 11: [57, 59], 13: [17]}
for game in data['games']:
    for ply in picks.get(game['game'], []):
        query(game, ply)
