"""Persist actual endpoint CPU receipts and bind them to searched moves."""
import hashlib
import json
from pathlib import Path
import time

from m2_engine_cpu import EngineCpuMeter
from m2_engine_identity import resolve_engine_endpoint
from m2_engine_receipts import validate_cell_cpu


class EngineCpuTrace:
    def __init__(self, directory, cell, expected_base_runtime):
        self.path = Path(directory)/'engine-cpu.jsonl'
        self.cell, self.runtime = cell, expected_base_runtime
        self.trace, self.processes, self.meters, self.errors = [], [], [], []
        self.games, self.closed = {}, False
        self.stream = self.path.open('x', encoding='utf-8', newline='\n')
        self.write('policy', {'schema_version': 1, 'cell_id': cell['id'],
            'scope': 'Actual verified UCI endpoint, all threads; launcher/bootstrap CPU remains in the full Job.',
            'cpu_ratio_is_diagnostic_only': True})

    def write(self, event, record):
        self.stream.write(json.dumps({'event': event, 'record': record}, ensure_ascii=False, allow_nan=False)+'\n')
        self.stream.flush()

    def wrap(self, engine, transport, index, profile):
        role = ('baseline', 'candidate')[index]
        meter = None
        try:
            resolution = resolve_engine_endpoint(transport['pid'], transport['creation_ticks'], profile, self.runtime)
            endpoint = resolution['endpoint']
            meter = EngineCpuMeter(endpoint['pid'], endpoint['creation_ticks'])
            baseline = meter.read()
            process = {'role': role, 'transport_index': index, 'transport_pid': transport['pid'],
                       'transport_creation_ticks': transport['creation_ticks'],
                       'endpoint_resolution': resolution, 'baseline': baseline, 'final': None,
                       'meter_closed': False}
            self.processes.append(process)
            self.meters.append(meter)
            self.write('endpoint', process)
            return MeasuredTransport(engine, meter, self, process)
        except BaseException as error:
            if meter is not None and meter not in self.meters:
                meter.close()
            self.write('setup_error', {'role': role, 'transport_pid': transport['pid'],
                                      'error': f'{type(error).__name__}: {error}'})
            raise

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        if self.closed:
            return False
        logging_errors = []
        def record(event, value):
            try:
                self.write(event, value)
            except Exception as issue:
                logging_errors.append(issue)
                self.errors.append(f'CPU trace {event}: {type(issue).__name__}: {issue}')
        try:
            if error is not None:
                record('worker_error', {'error': f'{type(error).__name__}: {error}'})
            for meter, process in zip(self.meters, self.processes):
                try:
                    process['final'] = meter.read()
                except Exception as issue:
                    self.errors.append(f"{process['role']} final CPU: {type(issue).__name__}: {issue}")
                finally:
                    try:
                        meter.close()
                        process['meter_closed'] = True
                    except Exception as issue:
                        self.errors.append(f"{process['role']} CPU handle: {type(issue).__name__}: {issue}")
                record('process_final', process)
            if self.errors:
                record('accounting_errors', {'errors': self.errors})
        finally:
            try:
                self.stream.close()
            finally:
                self.closed = True
        if logging_errors and error is None:
            raise logging_errors[0]
        return False

    def attach(self, metadata, code):
        if not self.closed:
            raise RuntimeError('finalize held CPU handles before saving match metadata')
        metadata['engine_cpu_trace'] = self.trace
        metadata['engine_cpu_processes'] = self.processes
        metadata['engine_cpu_trace_file'] = {'path': str(self.path),
            'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest()}
        moves = [(game_index, move) for game_index, game in enumerate(metadata['games'])
                 for move in game['search_moves']]
        if len(moves) != len(self.trace):
            self.errors.append('CPU calls and recorded searched moves differ')
        for (game_index, move), receipt in zip(moves, self.trace):
            if (receipt['game_index'] != game_index or any(receipt[key] != move[key]
                    for key in ('role', 'fen_before', 'move', 'ply'))):
                self.errors.append('CPU call identity differs from the played move')
            move['engine_cpu'] = receipt
        if not self.errors:
            try:
                metadata['engine_cpu_summary'] = validate_cell_cpu(metadata, self.cell)
            except Exception as issue:
                self.errors.append(f'CPU quality audit: {type(issue).__name__}: {issue}')
        if self.errors:
            metadata['engine_cpu_summary'] = {'valid': False, 'errors': list(self.errors)}
            metadata['errors'].extend(self.errors)
            code = 2
        metadata['exit_code'] = code
        return metadata, code


class MeasuredTransport:
    def __init__(self, engine, meter, tracker, process):
        self.engine, self.meter, self.tracker, self.process = engine, meter, tracker, process

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def play(self, *values, **kwargs):
        board = values[0]
        requested = self.tracker.cell['movetime_ms']/1000
        deadline = requested*2 + .25
        previous = self.engine.timeout
        self.engine.timeout = deadline-requested
        started = time.monotonic()
        reply = error = None
        receipt = None
        try:
            reply, receipt = self.meter.measure_play(self.engine.play, *values, **kwargs)
        except BaseException as issue:
            error = issue
            receipt = self.meter.last_measurement
        finally:
            ended = time.monotonic()
            self.engine.timeout = previous
        receipt = dict(receipt or {'status': 'ERROR', 'outcome': 'accounting_failed'})
        token = kwargs.get('game')
        if token not in self.tracker.games:
            self.tracker.games[token] = len(self.tracker.games)
        endpoint = self.process['endpoint_resolution']['endpoint']
        response_elapsed = receipt.get('response_elapsed_seconds')
        receipt.update(ordinal=len(self.tracker.trace)+1, role=self.process['role'],
            transport_index=self.process['transport_index'], transport_pid=self.process['transport_pid'],
            transport_creation_ticks=self.process['transport_creation_ticks'], pid=endpoint['pid'],
            creation_ticks=endpoint['creation_ticks'], fen_before=board.fen(en_passant='fen'),
            move=reply.move.uci() if reply is not None and reply.move is not None else None,
            requested_seconds=requested, response_deadline_seconds=deadline,
            envelope_started_monotonic=started, envelope_finished_monotonic=ended,
            envelope_elapsed_seconds=ended-started,
            deadline_exceeded=(ended-started > deadline or
                (isinstance(response_elapsed, (int, float)) and response_elapsed > deadline)),
            game_index=self.tracker.games[token], ply=board.ply()+1)
        self.tracker.trace.append(receipt)
        self.tracker.write('search', receipt)
        if error is not None:
            raise error
        if receipt['deadline_exceeded']:
            raise TimeoutError('UCI move exceeded preregistered response deadline')
        return reply


def validate_trace_file(metadata, directory):
    path = Path(directory)/'engine-cpu.jsonl'
    declared = metadata['engine_cpu_trace_file']
    if Path(declared['path']).resolve() != path.resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != declared['sha256']:
        raise ValueError('CPU trace file identity changed')
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    if not rows or rows[0]['event'] != 'policy':
        raise ValueError('missing CPU trace policy')
    if any(row['event'] not in ('policy', 'endpoint', 'search', 'process_final') for row in rows):
        raise ValueError('failed or unknown CPU trace event in completed match')
    if [row['record'] for row in rows if row['event'] == 'search'] != metadata['engine_cpu_trace']:
        raise ValueError('raw CPU trace and searched-move metadata differ')
    final = [row['record'] for row in rows if row['event'] == 'process_final']
    initial = [row['record'] for row in rows if row['event'] == 'endpoint']
    if final != metadata['engine_cpu_processes'] or len(initial) != 2:
        raise ValueError('CPU process final receipts differ or endpoint setup missing')
    for before, after in zip(initial, final):
        if any(before[key] != after[key] for key in ('role', 'transport_index', 'transport_pid',
                'transport_creation_ticks', 'endpoint_resolution', 'baseline')):
            raise ValueError('CPU endpoint setup and final identities differ')
    return {'status': 'PASS', 'search_receipts': len(metadata['engine_cpu_trace']), 'processes': len(final)}
