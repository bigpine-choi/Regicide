"""Recalculate per-search endpoint CPU and response receipts from raw fields.

No CPU utilization floor/ceiling is applied. In particular, quantized zero CPU,
immediate moves, and CPU/wall ratios above one are legitimate diagnostics. Full
chess/PGN legality and frozen executable/image proof are separate audits.
"""
from __future__ import annotations

import math
import re

TICKS_PER_SECOND = 10_000_000
ROLES = ('baseline', 'candidate')
SCOPE = 'actual UCI transport process, all threads; descendants excluded'


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _integer(value, label, minimum=0):
    _require(type(value) is int and value >= minimum, label + ' must be an integer in range')
    return value


def _number(value, label, minimum=0.):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= minimum,
             label + ' must be finite and in range')
    return value


def _same_number(actual, expected, label, tolerance=1e-9):
    _number(actual, label)
    _require(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=tolerance), label + ' differs from raw recalculation')


def _identity(row, label):
    _require(isinstance(row, dict), label + ' is missing')
    return (_integer(row.get('pid'), label + ' PID', 1),
            _integer(row.get('creation_ticks'), label + ' creation', 1))


def _raw(row, identity, label):
    _require(_identity(row, label) == identity, label + ' process identity differs')
    for name in ('kernel_ticks', 'user_ticks', 'cpu_ticks'):
        _integer(row.get(name), label + ' ' + name)
    _require(row['cpu_ticks'] == row['kernel_ticks'] + row['user_ticks'], label + ' CPU sum differs')
    start = _number(row.get('read_started_monotonic'), label + ' read start')
    finish = _number(row.get('read_finished_monotonic'), label + ' read finish')
    _require(start <= finish, label + ' read clock regressed')
    return row


def _ordered(previous, current, label):
    _require(all(current[name] >= previous[name] for name in ('kernel_ticks', 'user_ticks', 'cpu_ticks')),
             label + ' CPU counter regressed')
    _require(current['read_started_monotonic'] >= previous['read_finished_monotonic'],
             label + ' read clock overlaps or regressed')


def _distribution(values):
    ordered = sorted(values)
    def percentile(fraction):
        if not ordered:
            return None
        position = (len(ordered) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return {'p50': percentile(.5), 'p95': percentile(.95),
            'max': max(ordered) if ordered else None, 'total': math.fsum(ordered)}


def _resolution(process, transport, role, metadata):
    resolution = process.get('endpoint_resolution')
    _require(isinstance(resolution, dict), role + ' endpoint resolution missing')
    mode = resolution.get('mode')
    _require(mode in ('native-direct', 'sunfish-venv-child'), role + ' endpoint mode invalid')
    transport_identity = (process.get('transport_pid'), process.get('transport_creation_ticks'))
    _require(_identity(resolution.get('transport'), role + ' resolved transport') == transport_identity,
             role + ' resolved transport differs')
    _require(transport_identity == _identity(transport, role + ' affinity transport'),
             role + ' affinity transport differs')
    endpoint = resolution.get('endpoint')
    endpoint_identity = _identity(endpoint, role + ' endpoint')
    for label, row in (('transport', resolution['transport']), ('endpoint', endpoint)):
        _require(isinstance(row.get('image_path'), str) and bool(row['image_path']), role + ' ' + label + ' image missing')
        _require(isinstance(row.get('image_sha256'), str)
                 and re.fullmatch('[0-9a-f]{64}', row['image_sha256']) is not None,
                 role + ' ' + label + ' image SHA malformed')
    _require(resolution['transport']['image_sha256'] == metadata['engines'][role]['sha256'],
             role + ' transport image SHA differs from declared engine')
    if mode == 'native-direct':
        _require(endpoint_identity == transport_identity, role + ' native endpoint differs from transport')
        _require(endpoint['image_sha256'] == resolution['transport']['image_sha256'],
                 role + ' native endpoint image SHA differs')
    else:
        _require(endpoint_identity[0] != transport_identity[0]
                 and endpoint.get('parent_pid') == transport_identity[0]
                 and endpoint_identity[1] >= transport_identity[1], role + ' Sunfish child identity differs')
    return endpoint_identity


def validate_cell_cpu(metadata, cell, policy=None):
    """Return a raw-derived summary or raise ValueError on any invalid receipt.

    policy may be the full frozen plan, its engine_cpu_policy, or the fixed
    move_deadline dictionary. The existing 2x + 0.25 second deadline is retained.
    Percentiles use sorted linear interpolation at (n-1)*p (Hyndman-Fan type 7).
    An already stored metadata.engine_cpu_summary is checked for exact equality.
    """
    try:
        return _validate(metadata, cell, policy)
    except (KeyError, TypeError, IndexError, AttributeError, OverflowError) as error:
        raise ValueError('malformed per-engine CPU receipt: ' + str(error)) from error


def _validate(metadata, cell, policy):
    _require(isinstance(metadata, dict) and isinstance(cell, dict), 'metadata/cell must be dictionaries')
    _require(policy is None or isinstance(policy, dict), 'CPU policy must be a dictionary')
    fixed_rule = {'multiplier': 2.0, 'slack_seconds': .25}
    supplied = policy or {}
    rule = supplied.get('move_deadline', supplied if 'multiplier' in supplied else fixed_rule)
    _require(rule == fixed_rule, 'per-search response deadline policy changed')
    cpu_policy = supplied.get('engine_cpu_policy', supplied)
    _require(cpu_policy.get('cpu_ticks_per_second', TICKS_PER_SECOND) == TICKS_PER_SECOND,
             'native CPU tick scale differs')
    movetime_ms = _integer(cell.get('movetime_ms'), 'cell movetime', 1)
    requested = movetime_ms / 1000
    deadline = requested * 2 + .25
    transports = metadata['affinity']['transports']
    processes = metadata.get('engine_cpu_processes')
    trace = metadata.get('engine_cpu_trace')
    games = metadata.get('games')
    _require(isinstance(transports, list) and len(transports) == 2, 'two affinity transports required')
    _require(isinstance(processes, list) and len(processes) == 2, 'two closed CPU process receipts required')
    _require(isinstance(trace, list), 'complete engine CPU trace required')
    _require(isinstance(games, list) and len(games) == 2, 'a cell must contain two games')
    states = {}
    for process in processes:
        _require(isinstance(process, dict), 'CPU process receipt malformed')
        role = process.get('role')
        _require(role in ROLES and role not in states, 'CPU process role missing or duplicated')
        index = ROLES.index(role)
        _require(type(process.get('transport_index')) is int and process['transport_index'] == index,
                 role + ' transport index differs')
        _require(process.get('meter_closed') is True, role + ' meter is not closed')
        _integer(process.get('transport_pid'), role + ' transport PID', 1)
        _integer(process.get('transport_creation_ticks'), role + ' transport creation', 1)
        endpoint_identity = _resolution(process, transports[index], role, metadata)
        baseline = _raw(process.get('baseline'), endpoint_identity, role + ' baseline')
        final = _raw(process.get('final'), endpoint_identity, role + ' final')
        _ordered(baseline, final, role + ' lifetime')
        states[role] = {'process': process, 'identity': endpoint_identity, 'previous': baseline,
                        'final': final, 'sequence': 0, 'receipts': [], 'outer_elapsed': []}
    _require(len({state['identity'] for state in states.values()}) == 2, 'engine endpoint identities duplicated')
    _require(len({(row['pid'], row['creation_ticks']) for row in transports}) == 2,
             'affinity transport identities duplicated')
    ordinal = 0
    previous_envelope_finish = None
    for game_index, game in enumerate(games):
        _require(isinstance(game, dict) and isinstance(game.get('search_moves'), list), 'game search records missing')
        _require({game.get('white'), game.get('black')} == set(ROLES), 'game color-role assignment invalid')
        opening = game.get('opening_uci_moves')
        _require(isinstance(opening, list), 'game opening moves missing')
        for move_index, move in enumerate(game['search_moves']):
            ordinal += 1
            _require(ordinal <= len(trace), 'CPU trace shorter than actual searches')
            receipt = trace[ordinal - 1]
            _require(isinstance(receipt, dict) and move.get('engine_cpu') == receipt,
                     'search receipt differs from complete ordered CPU trace')
            _require(type(receipt.get('schema_version')) is int and receipt['schema_version'] == 1 and receipt.get('status') == 'OK'
                     and receipt.get('outcome') == 'complete', 'search CPU receipt is not successful')
            _require(receipt.get('scope') == SCOPE and receipt.get('ratio_is_diagnostic_only') is True,
                     'search CPU scope/ratio policy differs')
            _require(type(receipt.get('ordinal')) is int and receipt['ordinal'] == ordinal,
                     'global CPU trace ordinal differs')
            role = move.get('role')
            _require(role in states and receipt.get('role') == role, 'search role differs')
            state, index = states[role], ROLES.index(role)
            process = state['process']
            for name, expected in (('transport_index', index), ('transport_pid', process['transport_pid']),
                                   ('transport_creation_ticks', process['transport_creation_ticks']),
                                   ('pid', state['identity'][0]), ('creation_ticks', state['identity'][1]),
                                   ('game_index', game_index), ('ply', len(opening) + move_index + 1)):
                _require(type(receipt.get(name)) is int and receipt[name] == expected, name + ' search context differs')
            _require(type(move.get('ply')) is int and move['ply'] == receipt['ply'], 'search move ply differs')
            _require(isinstance(move.get('fen_before'), str) and move['fen_before'] == receipt.get('fen_before'),
                     'search FEN context differs')
            fen = move['fen_before'].split()
            _require(len(fen) == 6 and fen[1] in ('w', 'b'), 'search FEN turn missing')
            _require(game['white' if fen[1] == 'w' else 'black'] == role, 'search role differs from FEN turn')
            _require(isinstance(move.get('move'), str) and bool(move['move']) and move['move'] == receipt.get('move'),
                     'search move context differs')
            state['sequence'] += 1
            _require(type(receipt.get('sequence')) is int and receipt['sequence'] == state['sequence'],
                     'per-engine CPU sequence differs')
            before = _raw(receipt.get('before'), state['identity'], role + ' search before')
            after = _raw(receipt.get('after'), state['identity'], role + ' search after')
            _ordered(state['previous'], before, role + ' inter-search')
            _ordered(before, after, role + ' within-search')
            call_start = _number(receipt.get('call_started_monotonic'), 'search call start')
            call_finish = _number(receipt.get('call_finished_monotonic'), 'search call finish')
            envelope_start = _number(receipt.get('envelope_started_monotonic'), 'search envelope start')
            envelope_finish = _number(receipt.get('envelope_finished_monotonic'), 'search envelope finish')
            _require(previous_envelope_finish is None or envelope_start >= previous_envelope_finish,
                     'global search envelopes overlap or clock regressed')
            _require(envelope_start <= before['read_started_monotonic']
                     <= before['read_finished_monotonic'] <= call_start <= call_finish
                     <= after['read_started_monotonic'] <= after['read_finished_monotonic'] <= envelope_finish,
                     'search response/read/envelope clock order differs')
            response = call_finish - call_start
            envelope = envelope_finish - envelope_start
            _same_number(receipt.get('response_elapsed_seconds'), response, 'search response elapsed')
            _same_number(receipt.get('envelope_elapsed_seconds'), envelope, 'search envelope elapsed')
            _same_number(receipt.get('requested_seconds'), requested, 'search requested movetime')
            _same_number(receipt.get('response_deadline_seconds'), deadline, 'search response deadline')
            _require(receipt.get('deadline_exceeded') is False and envelope <= deadline and response <= deadline,
                     'search exceeded unchanged response deadline')
            outer = _number(move.get('elapsed_seconds'), 'comparison outer elapsed')
            # Comparison surrounds the entire wrapper, including post-response
            # JSON serialization/flush. Only callable/envelope time has the
            # unchanged engine deadline; storage latency is not engine latency.
            _require(outer + 1e-6 >= envelope,
                     'comparison elapsed does not contain measured envelope')
            for name in ('kernel', 'user', 'cpu'):
                delta = after[name + '_ticks'] - before[name + '_ticks']
                _require(type(receipt.get(name + '_delta_ticks')) is int and receipt[name + '_delta_ticks'] == delta,
                         name + ' search CPU raw delta differs')
                _same_number(receipt.get(name + '_seconds'), delta / TICKS_PER_SECOND, name + ' search CPU seconds')
            expected_ratio = receipt['cpu_seconds'] / response if response > 0 else None
            if expected_ratio is None:
                _require(receipt.get('cpu_per_wall_ratio') is None, 'zero-time CPU ratio must be null')
            else:
                _same_number(receipt.get('cpu_per_wall_ratio'), expected_ratio, 'search CPU/wall diagnostic ratio')
            state['previous'] = after
            state['receipts'].append(receipt)
            state['outer_elapsed'].append(outer)
            previous_envelope_finish = envelope_finish
    _require(ordinal == len(trace), 'CPU trace contains unlinked or duplicate searches')
    roles = {}
    for role, state in states.items():
        _ordered(state['previous'], state['final'], role + ' final lifetime')
        rows = state['receipts']
        ratios = [row['cpu_per_wall_ratio'] for row in rows if row['cpu_per_wall_ratio'] is not None]
        ratio_summary = _distribution(ratios)
        ratio_summary.pop('total')
        ratio_summary['count_none'] = len(rows) - len(ratios)
        cpu_ticks = sum(row['cpu_delta_ticks'] for row in rows)
        roles[role] = {'searched_moves': len(rows), 'cpu_ticks': cpu_ticks,
                       'cpu_seconds': cpu_ticks / TICKS_PER_SECOND,
                       'kernel_ticks': sum(row['kernel_delta_ticks'] for row in rows),
                       'user_ticks': sum(row['user_delta_ticks'] for row in rows),
                       'response_seconds': _distribution([row['response_elapsed_seconds'] for row in rows]),
                       'envelope_seconds': _distribution([row['envelope_elapsed_seconds'] for row in rows]),
                       'comparison_outer_seconds': _distribution(state['outer_elapsed']),
                       'outer_minus_envelope_seconds': _distribution([
                           outer - row['envelope_elapsed_seconds'] for outer, row in zip(state['outer_elapsed'], rows)]),
                       'requested_exceeded_count': sum(row['response_elapsed_seconds'] > requested for row in rows),
                       'envelope_requested_exceeded_count': sum(row['envelope_elapsed_seconds'] > requested for row in rows),
                       'zero_cpu_delta_count': sum(row['cpu_delta_ticks'] == 0 for row in rows),
                       'cpu_per_wall_ratio': ratio_summary,
                       'lifetime_cpu_seconds': (state['final']['cpu_ticks'] - state['process']['baseline']['cpu_ticks']) / TICKS_PER_SECOND}
        _require(cpu_ticks <= state['final']['cpu_ticks'] - state['process']['baseline']['cpu_ticks'],
                 role + ' search CPU exceeds observed lifetime CPU')
    summary = {'schema_version': 1, 'valid': True, 'searched_moves': ordinal,
               'total_cpu_ticks': sum(row['cpu_ticks'] for row in roles.values()),
               'total_cpu_seconds': sum(row['cpu_ticks'] for row in roles.values()) / TICKS_PER_SECOND,
               'roles': roles, 'percentile_method': 'sorted linear interpolation at (n-1)*p; Hyndman-Fan type 7',
               'scope': SCOPE, 'cpu_wall_ratio_is_diagnostic_only': True,
               'comparison_outer_elapsed_is_diagnostic_only': True,
               'requested_seconds': requested, 'response_deadline_seconds': deadline}
    if 'engine_cpu_summary' in metadata:
        _require(metadata['engine_cpu_summary'] == summary, 'stored engine CPU summary differs from raw recalculation')
    return summary
