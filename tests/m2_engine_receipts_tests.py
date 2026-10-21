"""Offline raw-receipt mutation checks; no process or CPU workload is created."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from m2_engine_receipts import SCOPE, validate_cell_cpu

START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1'


def raw(pid, creation, kernel, user, started, finished=None):
    return {'pid': pid, 'creation_ticks': creation, 'kernel_ticks': kernel,
            'user_ticks': user, 'cpu_ticks': kernel + user,
            'read_started_monotonic': started,
            'read_finished_monotonic': started if finished is None else finished}


def attach_valid_cpu_fixture(metadata, cell):
    """Attach explicitly synthetic zero-CPU receipts to existing offline games.

    Preserve existing moves, roles, FENs, elapsed values, engine provenance and
    affinities. Zero counters are deliberate fixture data, not real CPU claims.
    The production plan's time controls are validated by load_plan separately.
    """
    metadata.pop('engine_cpu_summary', None)
    processes, states, trace = [], {}, []
    requested = cell['movetime_ms'] / 1000
    for index, role in enumerate(('baseline', 'candidate')):
        transport = metadata['affinity']['transports'][index]
        pid, creation = transport['pid'], transport['creation_ticks']
        image = {'pid': pid, 'creation_ticks': creation,
                 'image_path': metadata['engines'][role].get('command', ['/fixture/' + role + '.exe'])[0],
                 'image_sha256': metadata['engines'][role]['sha256']}
        process = {'role': role, 'transport_index': index, 'transport_pid': pid,
                   'transport_creation_ticks': creation, 'meter_closed': True,
                   'endpoint_resolution': {'mode': 'native-direct', 'transport': deepcopy(image), 'endpoint': deepcopy(image)},
                   'baseline': raw(pid, creation, 0, 0, 0.)}
        processes.append(process)
        states[role] = {'process': process, 'sequence': 0}
    for game_index, game in enumerate(metadata['games']):
        for move in game['search_moves']:
            ordinal = len(trace) + 1
            state = states[move['role']]
            process = state['process']
            state['sequence'] += 1
            started = float(ordinal)
            elapsed = max(0., min(.01, move['elapsed_seconds'] * .5))
            finished = started + elapsed
            actual_elapsed = finished - started
            before = raw(process['transport_pid'], process['transport_creation_ticks'], 0, 0, started)
            after = raw(process['transport_pid'], process['transport_creation_ticks'], 0, 0, finished)
            receipt = {'schema_version': 1, 'sequence': state['sequence'], 'ordinal': ordinal,
                       'scope': SCOPE, 'status': 'OK', 'outcome': 'complete',
                       'before': before, 'after': after,
                       'call_started_monotonic': started, 'call_finished_monotonic': finished,
                       'response_elapsed_seconds': actual_elapsed,
                       'cpu_per_wall_ratio': 0. if actual_elapsed > 0 else None,
                       'ratio_is_diagnostic_only': True,
                       'counter_resolution_note': 'Synthetic offline fixture; quantized zero is valid.',
                       'kernel_delta_ticks': 0, 'user_delta_ticks': 0, 'cpu_delta_ticks': 0,
                       'kernel_seconds': 0., 'user_seconds': 0., 'cpu_seconds': 0.,
                       'transport_index': process['transport_index'], 'role': move['role'],
                       'transport_pid': process['transport_pid'],
                       'transport_creation_ticks': process['transport_creation_ticks'],
                       'pid': process['transport_pid'], 'creation_ticks': process['transport_creation_ticks'],
                       'fen_before': move['fen_before'], 'move': move['move'],
                       'requested_seconds': requested, 'response_deadline_seconds': requested * 2 + .25,
                       'envelope_started_monotonic': started, 'envelope_finished_monotonic': finished,
                       'envelope_elapsed_seconds': actual_elapsed, 'deadline_exceeded': False,
                       'game_index': game_index, 'ply': move['ply']}
            trace.append(receipt)
            move['engine_cpu'] = deepcopy(receipt)
    for process in processes:
        process['final'] = raw(process['transport_pid'], process['transport_creation_ticks'], 0, 0, len(trace) + 1.)
    metadata['engine_cpu_processes'] = processes
    metadata['engine_cpu_trace'] = trace
    metadata['engine_cpu_summary'] = validate_cell_cpu(metadata, cell)
    return metadata


def fixture(child=False):
    metadata = {'affinity': {'transports': [{'pid': 101, 'creation_ticks': 1001}, {'pid': 102, 'creation_ticks': 1002}]},
                'engines': {'baseline': {'sha256': 'a' * 64}, 'candidate': {'sha256': 'b' * 64}},
                'games': []}
    for white in ('baseline', 'candidate'):
        black = 'candidate' if white == 'baseline' else 'baseline'
        metadata['games'].append({'white': white, 'black': black, 'opening_uci_moves': [],
                                 'search_moves': [
                                     {'ply': 1, 'role': white, 'fen_before': START, 'move': 'e2e4', 'elapsed_seconds': .05},
                                     {'ply': 2, 'role': black, 'fen_before': AFTER_E4, 'move': 'e7e5', 'elapsed_seconds': .05}]})
    cell = {'movetime_ms': 100}
    attach_valid_cpu_fixture(metadata, cell)
    metadata.pop('engine_cpu_summary')
    if child:
        process = metadata['engine_cpu_processes'][0]
        process['endpoint_resolution']['mode'] = 'sunfish-venv-child'
        endpoint = process['endpoint_resolution']['endpoint']
        endpoint.update(pid=201, creation_ticks=1011, parent_pid=101, image_sha256='c' * 64)
        for row in (process['baseline'], process['final']): row.update(pid=201, creation_ticks=1011)
        for receipt in metadata['engine_cpu_trace']:
            if receipt['role'] == 'baseline':
                receipt.update(pid=201, creation_ticks=1011)
                for name in ('before', 'after'): receipt[name].update(pid=201, creation_ticks=1011)
    # A nonzero, quantized CPU interval exercises raw arithmetic and >1 ratio.
    totals = {'baseline': (10, 20), 'candidate': (10, 20)}
    for process in metadata['engine_cpu_processes']:
        process['baseline'].update(kernel_ticks=10, user_ticks=20, cpu_ticks=30)
    for receipt in metadata['engine_cpu_trace']:
        role = receipt['role']
        kernel, user = totals[role]
        receipt['before'].update(kernel_ticks=kernel, user_ticks=user, cpu_ticks=kernel + user)
        delta = 0 if receipt['ordinal'] == 1 else 156250
        user += delta
        receipt['after'].update(kernel_ticks=kernel, user_ticks=user, cpu_ticks=kernel + user)
        receipt.update(kernel_delta_ticks=0, user_delta_ticks=delta, cpu_delta_ticks=delta,
                       kernel_seconds=0., user_seconds=delta / 1e7, cpu_seconds=delta / 1e7,
                       cpu_per_wall_ratio=(delta / 1e7) / receipt['response_elapsed_seconds'])
        totals[role] = kernel, user
    for process in metadata['engine_cpu_processes']:
        kernel, user = totals[process['role']]
        process['final'].update(kernel_ticks=kernel, user_ticks=user, cpu_ticks=kernel + user)
    relink(metadata)
    return metadata, cell


def relink(metadata):
    moves = [move for game in metadata['games'] for move in game['search_moves']]
    for move, receipt in zip(moves, metadata['engine_cpu_trace']):
        move['engine_cpu'] = deepcopy(receipt)


class EngineReceiptTests(unittest.TestCase):
    def setUp(self):
        self.metadata, self.cell = fixture()

    def reject(self, mutate, match=None, relink_trace=True):
        metadata = deepcopy(self.metadata)
        mutate(metadata)
        if relink_trace: relink(metadata)
        with self.assertRaisesRegex(ValueError, match or '.'):
            validate_cell_cpu(metadata, self.cell)

    def test_native_and_resolved_sunfish_child_validate_and_summarize(self):
        for child in (False, True):
            metadata, cell = fixture(child)
            summary = validate_cell_cpu(metadata, cell)
            self.assertTrue(summary['valid'])
            self.assertEqual(summary['searched_moves'], 4)
            self.assertEqual(summary['total_cpu_ticks'], 468750)
            self.assertEqual(summary['roles']['baseline']['zero_cpu_delta_count'], 1)
            self.assertGreater(summary['roles']['candidate']['cpu_per_wall_ratio']['max'], 1.)
            self.assertAlmostEqual(summary['roles']['candidate']['response_seconds']['p50'], .01)

    def test_fixture_can_attach_to_existing_search_moves_and_round_zero_elapsed(self):
        metadata = deepcopy(self.metadata)
        metadata['games'][0]['search_moves'][0]['elapsed_seconds'] = 0.
        attach_valid_cpu_fixture(metadata, {'movetime_ms': 50})
        summary = validate_cell_cpu(metadata, {'movetime_ms': 50})
        self.assertEqual(summary['total_cpu_ticks'], 0)
        self.assertEqual(summary['roles']['baseline']['cpu_per_wall_ratio']['count_none'], 1)

    def test_missing_trace_extra_trace_and_mismatched_link_rejected(self):
        self.reject(lambda x: x['engine_cpu_trace'].pop(), 'shorter')
        self.reject(lambda x: x['engine_cpu_trace'].append(deepcopy(x['engine_cpu_trace'][-1])), 'unlinked')
        self.reject(lambda x: x['games'][0]['search_moves'][0]['engine_cpu'].update(cpu_ticks=1),
                    'differs', relink_trace=False)

    def test_cpu_raw_sum_delta_seconds_and_ratio_tampering_rejected(self):
        for field, value in [('cpu_delta_ticks', 99), ('cpu_seconds', .99), ('kernel_seconds', .99),
                             ('cpu_per_wall_ratio', 999), ('user_delta_ticks', True)]:
            with self.subTest(field=field):
                self.reject(lambda x: x['engine_cpu_trace'][1].update({field: value}))
        self.reject(lambda x: x['engine_cpu_trace'][0]['before'].update(cpu_ticks=999), 'sum')

    def test_cpu_component_regressions_lifetime_bounds_and_identity_rejected(self):
        self.reject(lambda x: x['engine_cpu_trace'][0]['after'].update(kernel_ticks=9, cpu_ticks=29), 'regressed')
        self.reject(lambda x: x['engine_cpu_trace'][0]['before'].update(pid=999), 'identity')
        self.reject(lambda x: x['engine_cpu_processes'][0]['baseline'].update(user_ticks=99, cpu_ticks=109), 'regressed')
        self.reject(lambda x: x['engine_cpu_processes'][0]['final'].update(user_ticks=20, cpu_ticks=30), 'regressed')
        self.reject(lambda x: x['engine_cpu_processes'][0].update(meter_closed=False), 'closed')

    def test_trace_context_and_per_engine_sequence_rejected(self):
        for field, value in [('ordinal', 2), ('sequence', 3), ('transport_index', 1),
                             ('transport_pid', 999), ('transport_creation_ticks', 999),
                             ('pid', 999), ('creation_ticks', 999), ('game_index', 1),
                             ('ply', 2), ('fen_before', AFTER_E4), ('move', 'd2d4'), ('role', 'candidate')]:
            with self.subTest(field=field):
                self.reject(lambda x: x['engine_cpu_trace'][0].update({field: value}))

    def test_search_role_cannot_be_changed_together_with_cpu_link(self):
        def mutate(metadata):
            metadata['games'][0]['search_moves'][0]['role'] = 'candidate'
            metadata['engine_cpu_trace'][0]['role'] = 'candidate'
        self.reject(mutate)

    def test_response_envelope_and_outer_elapsed_clocks_rejected(self):
        for field, value in [('call_started_monotonic', .1), ('call_finished_monotonic', .1),
                             ('envelope_started_monotonic', 1.1), ('envelope_finished_monotonic', .1),
                             ('response_elapsed_seconds', .2), ('envelope_elapsed_seconds', .2),
                             ('requested_seconds', 1.), ('response_deadline_seconds', 1.),
                             ('deadline_exceeded', True)]:
            with self.subTest(field=field):
                self.reject(lambda x: x['engine_cpu_trace'][0].update({field: value}))
        self.reject(lambda x: x['engine_cpu_trace'][0]['before'].update(read_finished_monotonic=2.), 'clock')
        self.reject(lambda x: x['games'][0]['search_moves'][0].update(elapsed_seconds=0.), 'elapsed')

    def test_post_response_log_latency_is_diagnostic_not_engine_deadline_failure(self):
        self.metadata['games'][0]['search_moves'][0]['elapsed_seconds'] = 1.5
        summary = validate_cell_cpu(self.metadata, self.cell)
        self.assertTrue(summary['valid'])
        self.assertEqual(summary['roles']['baseline']['comparison_outer_seconds']['max'], 1.5)
        self.assertTrue(summary['comparison_outer_elapsed_is_diagnostic_only'])
        self.assertGreater(summary['roles']['baseline']['outer_minus_envelope_seconds']['max'], 1.)

    def test_actual_envelope_overrun_rejected_even_when_cpu_low(self):
        def mutate(metadata):
            row = metadata['engine_cpu_trace'][0]
            row['envelope_finished_monotonic'] = row['envelope_started_monotonic'] + .46
            row['envelope_elapsed_seconds'] = .46
        self.reject(mutate, 'deadline')

    def test_searches_of_different_roles_cannot_overlap_in_time(self):
        def mutate(metadata):
            row = metadata['engine_cpu_trace'][1]
            for name in ('call_started_monotonic', 'call_finished_monotonic',
                         'envelope_started_monotonic', 'envelope_finished_monotonic'):
                row[name] -= 1.
            for phase in ('before', 'after'):
                for name in ('read_started_monotonic', 'read_finished_monotonic'):
                    row[phase][name] -= 1.
        self.reject(mutate, 'global search')

    def test_bad_resolution_proof_process_role_and_sha_rejected(self):
        self.reject(lambda x: x['engine_cpu_processes'][0]['endpoint_resolution'].update(mode='any-child'))
        self.reject(lambda x: x['engine_cpu_processes'][0]['endpoint_resolution']['transport'].update(pid=999))
        self.reject(lambda x: x['engine_cpu_processes'][0]['endpoint_resolution']['endpoint'].update(pid=201))
        self.reject(lambda x: x['engine_cpu_processes'][0]['endpoint_resolution']['transport'].update(image_sha256='c' * 64))
        self.reject(lambda x: x['engine_cpu_processes'][0].update(role='candidate'))
        metadata, cell = fixture(True)
        metadata['engine_cpu_processes'][0]['endpoint_resolution']['endpoint']['parent_pid'] = 999
        with self.assertRaisesRegex(ValueError, 'child identity'): validate_cell_cpu(metadata, cell)

    def test_nonfinite_negative_boolean_or_missing_values_fail_closed(self):
        for field, value in [('response_elapsed_seconds', float('nan')), ('cpu_per_wall_ratio', float('inf')),
                             ('cpu_seconds', -1.), ('cpu_delta_ticks', True), ('schema_version', True)]:
            with self.subTest(field=field):
                self.reject(lambda x: x['engine_cpu_trace'][0].update({field: value}))
        self.reject(lambda x: x.pop('engine_cpu_processes'))
        self.reject(lambda x: x['engine_cpu_trace'][0].update(before=None))
        self.reject(lambda x: x['games'].pop())

    def test_fixed_deadline_and_stored_summary_recalculation(self):
        summary = validate_cell_cpu(self.metadata, self.cell, {'move_deadline': {'multiplier': 2., 'slack_seconds': .25}})
        self.metadata['engine_cpu_summary'] = deepcopy(summary)
        self.assertEqual(validate_cell_cpu(self.metadata, self.cell), summary)
        self.metadata['engine_cpu_summary']['total_cpu_ticks'] += 1
        with self.assertRaisesRegex(ValueError, 'stored'): validate_cell_cpu(self.metadata, self.cell)
        with self.assertRaisesRegex(ValueError, 'deadline'):
            validate_cell_cpu(self.metadata, self.cell, {'multiplier': 3., 'slack_seconds': .25})


if __name__ == '__main__':
    unittest.main()
