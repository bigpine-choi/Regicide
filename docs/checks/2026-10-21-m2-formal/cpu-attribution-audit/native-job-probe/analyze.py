"""Read-only full-lifetime CPU reconciliation of the two synthetic workloads."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile(directory):
    path = directory/'receipt.json'
    receipt = json.loads(path.read_text(encoding='utf-8'))
    raw_path = directory/'guard-load.jsonl'
    rows = [json.loads(text) for text in raw_path.read_text(encoding='utf-8').splitlines()]
    guard_ticks = sum(row.get('host', {}).get('owned_delta_ticks', 0) for row in rows)
    roots = receipt['roots']
    leaves = [leaf for root in receipt['workers'] for leaf in root['leaves']]
    root_ticks = sum(root['cpu_ticks'] for root in roots)
    leaf_ticks = sum(leaf['cpu_ticks'] for leaf in leaves)
    job_ticks = receipt['final_job']['cpu_ticks']
    identities = {(item['pid'], item['creation_ticks']) for row in rows for item in row.get('processes', []) if item.get('owned')}
    identities |= {(item['pid'], item['creation_ticks']) for row in rows for item in row.get('retained_owned', [])}
    unseen = [leaf for leaf in leaves if (leaf['pid'], leaf['creation_ticks']) not in identities]
    unseen_ticks = sum(leaf['cpu_ticks'] for leaf in unseen)
    assert all(root['exit_ticks'] > 0 and root['exit_code'] == 0 for root in roots)
    assert all(leaf['exit_ticks'] > 0 and leaf['exit_code'] == 0 for leaf in leaves)
    assert receipt['final_job']['active_processes'] == receipt['job_after_workload_cleanup']['active_processes'] == 0
    assert receipt['final_job']['total_processes'] == len(roots)+len(leaves)
    observed_leaf_ticks = leaf_ticks-unseen_ticks
    assert guard_ticks == root_ticks+observed_leaf_ticks, 'Guard must match all final timers of observed roots/children'
    residual = root_ticks+leaf_ticks-job_ticks
    assert job_ticks-guard_ticks+residual == unseen_ticks
    return {'scenario': receipt['scenario'], 'status': 'NATIVE_COUNTER_COMPARISON_COMPLETE',
            'job_total_cpu_ticks': job_ticks, 'guard_lifetime_owned_delta_ticks': guard_ticks,
            'held_final_root_cpu_ticks': root_ticks, 'held_final_leaf_cpu_ticks': leaf_ticks,
            'unobserved_leaf_count': len(unseen), 'unobserved_leaf_cpu_ticks': unseen_ticks,
            'job_minus_guard_cpu_ticks': job_ticks-guard_ticks,
            'missing_cpu_seconds': (job_ticks-guard_ticks)/10000000,
            'guard_matches_final_timers_of_observed_processes': True,
            'job_vs_sum_held_process_timers_residual_ticks': residual,
            'job_additivity_exact': residual == 0,
            'guard_max_nonworker_cpu_fraction': receipt['guard_summary']['max_nonworker_cpu_fraction'],
            'guard_declared_threshold': receipt['guard_summary']['policy']['max_nonworker_cpu_fraction'],
            'guard_recorded_failure': receipt['guard_summary']['failure'],
            'would_violate_original_formal20': receipt['guard_summary']['max_nonworker_cpu_fraction'] > .2,
            'formal_load_validation': False,
            'query_endpoint_work_delta_deficit_ticks': receipt['job_minus_guard_lower_ticks'],
            'query_endpoint_difference_from_full_unseen_leaf_ticks': receipt['job_minus_guard_lower_ticks']-unseen_ticks,
            'job_active_after_cleanup': receipt['job_after_workload_cleanup']['active_processes'],
            'input_sha256': {str(path): sha(path), str(raw_path): sha(raw_path)}}


def main():
    output = HERE/'analysis.json'
    if output.exists():
        raise ValueError('Preserve existing analysis')
    control = reconcile(HERE/'attempt-001'/'long-lived')
    churn = reconcile(HERE/'attribution-only-001'/'short-child-churn')
    result = {'schema_version': 1, 'status': 'NATIVE_COUNTER_COMPARISON_COMPLETE',
              'analysis_source_sha256': sha(Path(__file__)), 'scenarios': [control, churn],
              'primary_finding': 'Long-lived work has zero Job/guard/final-root CPU deficit. In churn, the guard exactly matches final timers of the four roots; all 32 short-child identities are absent. Their held final CPU is 7.25 seconds; the independent Job-minus-guard deficit is 7.203125 seconds.',
              'measurement_caveat': 'Churn Job total is 0.046875 CPU seconds below the sum of held final root+leaf timers. This residual is preserved, not relabeled as exact additivity. Process creation preceding Job assignment and native counter accounting precision are possible boundaries, not proven explanations. Before/after query endpoints are observations, not certified instantaneous bounds on separately updated counters.',
              'initial_analysis_failure': {'source_sha256': 'd14babdc8b9cc2da3ec32e2cd5bc0a520623cf965139508f6b1df4f01f9c5e26',
                                           'error': 'AssertionError: Job total must match held final root/leaf native timers',
                                           'resolution': 'Rejected exact-additivity assumption; report observed residual explicitly. All raw probe data unchanged.'},
              'formal_load_validation': False, 'original_threshold_changed': False,
              'strict20_failures_preserved': True, 'strength_eligible': False,
              'scope': 'Demonstrates a synthetic between-snapshot descendant-attribution failure mode. Does not prove that short-child churn caused previous formal failures, authorize a formal threshold change, or certify OS isolation.'}
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'status': result['status'], 'long_lived_missing_seconds': control['missing_cpu_seconds'],
                      'short_child_missing_seconds': churn['missing_cpu_seconds'], 'unobserved_children': churn['unobserved_leaf_count']}))


if __name__ == '__main__':
    main()
