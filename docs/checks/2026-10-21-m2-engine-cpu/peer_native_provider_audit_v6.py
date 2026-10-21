"""Read-only independent native negative-case audit; starts no processes."""
from pathlib import Path
import importlib.util
import json
from collections import Counter
from datetime import datetime, timezone
import traceback

HERE = Path(__file__).resolve().parent
CORE = HERE/'peer_game_audit_engine_cpu_v6.py'
spec = importlib.util.spec_from_file_location('independent_native_v6_negative', CORE)
peer = importlib.util.module_from_spec(spec); spec.loader.exec_module(peer)


def audit():
    folder = HERE/'native-process-scope/attempt-002'
    result = dict(schema_version=1, status='FAIL', audited_utc=datetime.now(timezone.utc).isoformat(),
        audit_script_sha256=peer.sha(Path(__file__)), core_audit_sha256=peer.sha(CORE),
        formal_games_run=0, directory=str(folder),
        original_diagnostic_driver_failure_sha256=peer.sha(HERE/'peer-v6-native-provider-external-negative.json'),
        diagnostic_correction='The native idle baseline contains an assigned but not yet attached Job worker. '
        'A synthetic outside PPID may equal that pending Job member. Exact Job list membership, '
        'rather than active attach state, proves the pending parent cohort; it gives the outside engine no ownership.')
    try:
        top = peer.read(folder/'receipt.json')
        receipt = peer.read(folder/'external-negative/receipt.json')
        summary = peer.read(folder/'external-negative/load-summary.json')
        peer.check(top['status'] == 'PASS_EXPECTED_NEGATIVE'
            and receipt['status'] == 'PASS_EXPECTED_EXTERNAL_CPU_FAILURE', 'Native diagnostic status differs')
        peer.check(top['source_unchanged'] is True and top['source_sha256'] == top['source_sha256_after'],
                   'Native source changed during diagnostics')
        for path, digest in top['source_sha256'].items():
            peer.check(peer.sha(peer.REPOSITORY/path) == digest, 'Native source now differs')
        peer.check(top['cleanup'] == receipt['cleanup'] == dict(active_owned_processes=0, unknown_owned_processes=0)
            and receipt['job_closed'] is True and receipt['final_job_accounting']['active_processes'] == 0,
            'Native Job cleanup failed')
        peer.check(top['raw_audit']['status'] == receipt['raw_audit']['status'] == 'INVALID'
            and summary['valid_for_load_check'] is False
            and summary['failure']['kind'] == receipt['expected_failure']['kind'] == 'external_cpu',
            'Expected outside CPU failure was lost')
        path = folder/'external-negative/load.jsonl'; peer.sha(path)
        rows = [peer.decoded(line) for line in path.read_text().splitlines()]
        outside = receipt['outside_endpoint_initial']['pid'], receipt['outside_endpoint_initial']['creation_ticks']
        own = top['owned_idle_engine_pid']
        known, active, counts, own_observations = {}, {}, Counter(), []
        scopes = accepted = external_delta = 0
        previous_external = None
        failure_kinds = set()
        for row in rows:
            if row['event'] == 'attach':
                key = tuple(row['identity']); known[key] = key; active[key[0]] = key
            elif row['event'] == 'detach':
                del active[row['identity'][0]]
            elif row['event'] == 'sample':
                peer.check({tuple(key) for key in row['roots']} == set(active.values()), 'Native roots differ')
                scope = peer.provider_scope_records(row, summary['policy'], summary['monitor_pid'], known)
                scopes += 1; counts.update(scope['counts'])
                if row['accounting_baseline_accepted']:
                    accepted += 1
                    peer.check(row['process_ownership_scope_index'] == accepted, 'Native scope index differs')
                else:
                    peer.check(row['process_ownership_scope_index'] is None, 'Unaccepted scope has admitted index')
                externals = {}
                raw_scope = row['process_ownership_scope']
                pending_or_attached_parents = set(raw_scope['job_before']['pids']) | set(raw_scope['job_after']['pids']) | set(active)
                for item in row['processes']:
                    key = item['pid'], item['creation_ticks']
                    if item['owned']:
                        known[key] = tuple(item['worker_root'])
                    elif peer.declared_engine(item, summary['policy']):
                        externals[key] = item['cpu_ticks']
                    if item['pid'] == own:
                        peer.check(item['job_scope_member'] is True and item['owned'] is True
                            and key in scope['proven_owned'], 'Actual owned idle engine was misclassified')
                        own_observations.append(dict(in_owned_job_at_read=True,
                            result=next(query['result'] for query in raw_scope['queries'] if query['pid'] == own)))
                    if key == outside:
                        peer.check(item['owned'] is False and item['job_scope_member'] is False
                            and item['worker_root'] is None and item['parent_pid'] in pending_or_attached_parents,
                            'Outside engine was promoted through numeric parent')
                peer.check(outside in externals, 'Outside engine disappeared')
                if previous_external is not None:
                    peer.check(externals.keys() == previous_external.keys(), 'External identity set changed')
                    deltas = [externals[key]-previous_external[key] for key in externals]
                    peer.check(min(deltas, default=0) >= 0, 'External counter regressed')
                    external_delta += sum(deltas)
                previous_external = externals
                if row['failure']:
                    failure_kinds.add(row['failure']['kind'])
        peer.check(scopes == accepted == summary['snapshot_count'] == summary['process_ownership_scope_count'] == 17,
                   'Native scope coverage differs')
        peer.check(own_observations == top['owned_idle_engine_observations'] and len(own_observations) == 4,
                   'Actual owned idle engine native observations differ')
        peer.check(external_delta == summary['external_cpu_increase_ticks'] == 937500
            and failure_kinds == {'external_cpu'}, 'Native external CPU delta/failure differs')
        cpu = receipt['outside_move']['cpu']; peer.process_cpu_receipt(cpu, outside)
        peer.check(cpu['cpu_delta_ticks'] == 937500 and
            peer.chess.Move.from_uci(receipt['outside_move']['move']) in peer.chess.Board().legal_moves,
            'Native diagnostic CPU/move differs')
        peer.check(receipt['outside_final_cpu']['cpu_ticks']-receipt['outside_endpoint_initial']['cpu_ticks'] == external_delta,
                   'Held outside final CPU differs')
        for item in top['outside_exit_proofs']:
            peer.check((item['pid'], item['creation_ticks']) == outside and item['wait_status'] == 0
                and item['handle_closed'] is True
                and item['final_cpu']['cpu_ticks'] == receipt['outside_final_cpu']['cpu_ticks'],
                'Outside exact held exit/close proof differs')
        result.update(status='PASS_EXPECTED_NEGATIVE', provider_scope_count=scopes, provider_scope_totals=dict(counts),
            owned_idle_engine_pid=own, owned_idle_engine_native_membership_and_owned_observations=len(own_observations),
            external_identity=list(outside), external_cpu_delta_ticks=external_delta, external_cpu_seconds=external_delta/1e7,
            expected_failure_kind='external_cpu', raw_run_valid_for_comparison=False,
            final_owned_job_active_processes=0, outside_held_exited_and_closed=True,
            scope='Actual native same-handle explicit Job membership separates an owned idle Regicide from a newer '
                  'outside Regicide despite synthetic numeric parent equality. Expected external CPU failure remains '
                  'INVALID. No historical collision reproduction, formal game or strength claim.')
    except BaseException as error:
        result.update(error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
    result['input_sha256'] = dict(peer.helper.FILES)
    return result


if __name__ == '__main__':
    result = audit()
    output = HERE/'peer-v6-native-provider-external-negative-r2.json'
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False); stream.write('\n')
    print(json.dumps({key: value for key, value in result.items()
        if key not in ('input_sha256', 'directory', 'scope', 'diagnostic_correction')}, ensure_ascii=False))
    raise SystemExit(0 if result['status'] == 'PASS_EXPECTED_NEGATIVE' else 1)
