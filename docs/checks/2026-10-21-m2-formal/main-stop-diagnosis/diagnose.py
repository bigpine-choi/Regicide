"""Read only resource receipts; write a compact independent stop diagnosis.

No game files, engine state or process controls are accessed. All time-window
weights are recomputed from the recorded end-to-end sampling gaps.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(row):
    return row['pid'], row['creation_ticks']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.directory / 'main' / 'load.jsonl'
    before = sha(source)
    samples, events = [], []
    elapsed, previous, previous_owned = 0., None, {}
    ever_seen, names = {}, {}
    mismatches, missing_live, retirement_mismatches = [], [], []
    with source.open(encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            record = json.loads(line)
            if record.get('event') != 'sample':
                events.append({'line': line_number, **record})
                continue
            elapsed += record['gap_seconds']
            owned = {}
            for row in record['processes']:
                if row.get('owned') and not row.get('error'):
                    owned[identity(row)] = row
                    names[identity(row)] = row['name']
            for row in record['retained_owned']:
                owned[identity(row)] = row
            omitted, first_seen, delta = [], [], 0
            if previous is not None:
                for key, row in owned.items():
                    if key not in ever_seen:
                        first_seen.append({'identity': key, 'name': names.get(key), 'cpu_ticks': row['cpu_ticks'],
                            'creation_at_or_after_previous_wall': row['creation_ticks'] >= previous['wall_ticks']})
                    if key in previous_owned:
                        delta += row['cpu_ticks'] - previous_owned[key]['cpu_ticks']
                    elif row['creation_ticks'] >= previous['wall_ticks']:
                        delta += row['cpu_ticks']
                    else:
                        omitted.append({'identity': key, 'name': names.get(key), 'cpu_ticks': row['cpu_ticks'],
                            'exited': row.get('exited'), 'first_ever': key not in ever_seen,
                            'previously_observed_cpu_ticks': ever_seen.get(key),
                            'new_cpu_since_last_observation_ticks': row['cpu_ticks'] - ever_seen.get(key, 0),
                            'created_before_previous_sample_ticks': previous['wall_ticks'] - row['creation_ticks']})
                for key in set(previous_owned) - set(owned):
                    if not previous_owned[key].get('exited'):
                        missing_live.append({'line': line_number, 'identity': key, 'name': names.get(key),
                                             'previous_cpu_ticks': previous_owned[key]['cpu_ticks']})
                expected_total = (record['system']['kernel_ticks'] + record['system']['user_ticks']
                                  - previous['system']['kernel_ticks'] - previous['system']['user_ticks'])
                expected_busy = expected_total - record['system']['idle_ticks'] + previous['system']['idle_ticks']
                expected = {'total_delta_ticks': expected_total, 'busy_delta_ticks': expected_busy,
                            'owned_delta_ticks': delta, 'nonworker_delta_ticks': max(0, expected_busy - delta),
                            'accounting_underflow_ticks': max(0, delta - expected_busy)}
                if any(record['host'][key] != value for key, value in expected.items()):
                    mismatches.append({'line': line_number, 'expected': expected, 'recorded': record['host']})
            for event in record.get('retirement_events', []):
                key = tuple(event['identity'])
                expected_cpu = owned[key]['cpu_ticks'] if key in owned else ever_seen.get(key)
                if event['cpu_ticks'] != expected_cpu:
                    retirement_mismatches.append({'line': line_number, 'event': event,
                                                   'observed_owned_cpu_ticks': expected_cpu})
            samples.append({'line': line_number, 'end': elapsed, 'utc': record['utc'],
                'phase': record['phase'], 'gap_seconds': record['gap_seconds'], 'host': record.get('host'),
                'failure': record.get('failure'), 'omitted': omitted,
                'first_seen_owned': first_seen,
                'retiring_owned': record.get('retiring_owned', []),
                'retirement_events': record.get('retirement_events', []),
                'retired_handle_count': record.get('retired_handle_count'),
                'exited_owned_count': sum(bool(row.get('exited')) for row in owned.values()),
                'owned_count': len(owned),
                'external_rows': [row for row in record['processes'] if row.get('owned') is False]})
            for key, row in owned.items():
                ever_seen[key] = max(ever_seen.get(key, 0), row['cpu_ticks'])
            previous, previous_owned = record, owned
    failing = next(row for row in samples if (row.get('failure') or {}).get('kind') == 'host_nonworker_cpu')
    stop = failing['end']
    for row in samples:
        row['end_from_stop_seconds'] = row['end'] - stop

    def window(left, right):
        fields = ('total_delta_ticks', 'busy_delta_ticks', 'owned_delta_ticks',
                  'nonworker_delta_ticks', 'accounting_underflow_ticks')
        sums = {key: 0. for key in fields}
        rows, omitted = [], []
        for row in samples:
            end = row['end_from_stop_seconds']
            start = end - row['gap_seconds']
            overlap = max(0., min(end, right) - max(start, left))
            if not overlap or not row['host'] or row['gap_seconds'] <= 0:
                continue
            weight = overlap / row['gap_seconds']
            for key in fields:
                sums[key] += row['host'][key] * weight
            rows.append({'line': row['line'], 'utc': row['utc'], 'phase': row['phase'],
                'start_from_stop_seconds': start, 'end_from_stop_seconds': end,
                'overlap_weight': weight, **{key: row['host'][key] for key in fields},
                'window_fraction': row['host']['window_fraction'],
                'new_unbaselined_owned': row['omitted'],
                'first_seen_owned': row['first_seen_owned'],
                'owned_count': row['owned_count'], 'exited_owned_count': row['exited_owned_count'],
                'retired_handle_count': row['retired_handle_count'],
                'retirement_events': row['retirement_events']})
            omitted.extend({'line': row['line'], 'overlap_weight': weight, **entry} for entry in row['omitted'])
        total = sums['total_delta_ticks']
        return {'range_from_stop_seconds': [left, right], 'weighted_ticks': sums,
            'cpu_seconds': {key.removesuffix('_ticks'): value / 10_000_000 for key, value in sums.items()},
            'fraction': {key.removesuffix('_delta_ticks'): sums[key] / total for key in
                         ('busy_delta_ticks', 'owned_delta_ticks', 'nonworker_delta_ticks')},
            'unclipped_residual_fraction': (sums['busy_delta_ticks'] - sums['owned_delta_ticks']) / total,
            'clip_added_fraction': sums['accounting_underflow_ticks'] / total,
            'new_first_ever_owned_unbaselined_cpu_ticks': sum(entry['cpu_ticks'] * entry['overlap_weight']
                                                            for entry in omitted if entry['first_ever']),
            'new_first_ever_owned_unbaselined_entries': [entry for entry in omitted if entry['first_ever']],
            'reappeared_unchanged_final_entries': sum(not entry['first_ever'] and
                entry['new_cpu_since_last_observation_ticks'] == 0 and entry['exited'] for entry in omitted),
            'intervals': rows}
    final = window(-2., 0.)
    preceding = window(-12., -2.)
    frozen_guard = args.directory / 'harness' / 'm2_load_guard.py'
    terminal = samples[-1]
    policy = next(event['policy'] for event in events if event['event'] == 'policy')
    report = {'schema_version': 1, 'scope': 'Resources only. No game results read. No source/settings/processes modified.',
        'inputs': {'load_jsonl': str(source), 'sha256_before': before, 'sha256_after': sha(source),
                   'frozen_guard_sha256': sha(frozen_guard), 'analysis_script_sha256': sha(Path(__file__))},
        'stop': {'line': failing['line'], 'sample_utc': failing['utc'], 'failure': failing['failure'],
                 'recorded_host': failing['host'], 'recomputed_fraction_error':
                     final['fraction']['nonworker'] - failing['host']['window_fraction']},
        'terminal_sample': {'line': terminal['line'], 'utc': terminal['utc'], 'phase': terminal['phase'],
            'seconds_after_first_failure': terminal['end_from_stop_seconds'], 'recorded_host': terminal['host'],
            'note': 'The summary maximum includes this later closed sample; it is not the initial trigger window.'},
        'replay': {'sample_count': len(samples), 'counter_mismatch_count': len(mismatches),
                   'counter_mismatches': mismatches, 'live_owned_disappeared_without_final': missing_live,
                   'retirement_final_cpu_mismatches': retirement_mismatches,
                   'external_rows_last12s': [entry for row in samples if -12 <= row['end_from_stop_seconds'] <= 0
                                             for entry in row['external_rows']]},
        'final_2_seconds': final, 'preceding_10_seconds': preceding,
        'conclusion': {'recorded_policy_exceeded_at_first_failure':
            final['fraction']['nonworker'] > policy['max_nonworker_cpu_fraction'],
            'threshold': policy['max_nonworker_cpu_fraction'],
            'excess_nonworker_cpu_seconds_over_threshold':
                (final['weighted_ticks']['nonworker_delta_ticks'] - policy['max_nonworker_cpu_fraction'] *
                 final['weighted_ticks']['total_delta_ticks']) / 10_000_000,
            'counter_arithmetic_matches_all_samples': not mismatches,
            'observed_startup_baseline_omission_explains_final_window':
                bool(final['new_first_ever_owned_unbaselined_entries']),
            'observed_retirement_or_final_timer_loss': bool(missing_live or retirement_mismatches),
            'clipping_explains_final_window_excess': final['clip_added_fraction'] > 0,
            'cause_attribution': 'Unidentified aggregate nonworker residual. No per-nonworker-process CPU timers were captured.',
            'validity': 'The preregistered load check failed. This analysis does not relabel the run or change its threshold.'},
        'nearby_lifecycle': [event for event in events if event['event'] in ('attach', 'detach') and
            preceding['intervals'][0]['utc'] <= event['utc'] <= failing['utc']],
        'limits': ['Per-process timers for nonworker host tasks were not recorded, so the residual cannot identify its cause.',
                   'System and process CPU counters are asynchronous/quantized; per-interval clipping may overstate a window residual.',
                   'First-observed pre-existing cumulative CPU cannot be assigned entirely to this interval or safely subtracted.',
                   'Transient workloads entirely between samples cannot be attributed from these receipts.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'stop': report['stop'], 'replay': report['replay'],
                     'windows': {key: {field: value for field, value in report[key].items() if field != 'intervals'}
                                 for key in ('final_2_seconds', 'preceding_10_seconds')}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
