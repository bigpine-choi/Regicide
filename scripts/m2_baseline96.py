"""Select a fixed 96-game baseline and reuse a verified, unchanged v6 preflight.

The frozen v6 runner, worker, counters and audits are reused without edits.
No preflight games are copied, rerun or counted as the 96 new main games.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import m2_formal_v6 as formal

V6_HARNESS = tuple(formal.HARNESS)
HARNESS = V6_HARNESS + (Path(__file__).name,)
ORIGINAL_LOAD_PLAN = formal.load_plan
ORIGINAL_AUDIT = formal.audit_phase
SELECTION = {'main_opening_count': 4, 'main_games': 96, 'selection': 'first4bybankorder'}
PRECONDITIONS = ('candidates', 'opponents', 'preflight_openings', 'preflight_opening_count',
    'movetimes_ms', 'max_plies', 'draw_policy', 'lane_affinity_masks', 'topology',
    'load_policy', 'accounting_policy', 'residual_load_policy', 'engine_cpu_policy',
    'process_discovery_policy', 'move_deadline', 'idle_probe_seconds',
    'cell_timeout_seconds', 'preflight_timeout_seconds', 'worker_runtime', 'retry_policy')


def require(condition, message):
    if not condition:
        raise ValueError('Baseline96: ' + message)


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def selection(plan):
    require(plan.get('baseline96_selection') == SELECTION, 'exact fixed first-four/96 selection required')
    transfer = plan.get('preflight_transfer', {})
    require(isinstance(transfer.get('directory'), str) and Path(transfer['directory']).is_absolute(),
            'absolute parent preflight directory required')
    for key in ('peer_audit', 'product_audit'):
        value = transfer.get(key)
        require(isinstance(value, dict) and set(value) == {'path', 'sha256'}
            and isinstance(value['path'], str) and isinstance(value['sha256'], str)
            and len(value['sha256']) == 64, 'fixed completed ' + key + ' reference required')


def load_plan(path):
    # Validate the entire original 32-family bank and every v6 condition first.
    plan, profiles, banks = ORIGINAL_LOAD_PLAN(path)
    selection(plan)
    source, rows = banks['main']
    require(len(rows) == 32, 'original bank must retain all 32 families')
    return plan, profiles, dict(banks, main=(source, rows[:4]))


def verify(directory):
    directory = Path(directory).resolve()
    frozen, profiles = formal.verify_frozen(directory)
    selection(frozen['plan'])
    actual_sources = {Path(path).name for path in frozen['harness_source_sha256']}
    require(actual_sources == set(HARNESS), 'exact original 33 sources plus adapter required')
    base = Path(frozen['source_plan_path']).parent
    original = formal.pilot.referenced_file(frozen['plan']['openings'], base)
    _, full = formal.comparison.load_openings(original)
    _, selected = formal.comparison.load_openings(frozen['banks']['main']['path'])
    require(len(full) == 32 and selected == full[:4], 'copied bank differs from fixed original first four')
    require(len(frozen['cells']['main']) == 48 and len(frozen['cells']['preflight']) == 12,
            'exact 48 main pairs/12 separate preflight pairs required')
    declared = frozen['plan'].get('main_selection_policy')
    if declared is not None:
        require(declared['selected_family_ids'] == [row['family_id'] for row in selected]
            and declared['selected_cell_ids'] == [row['id'] for row in frozen['cells']['main']]
            and declared['selected_cells_canonical_sha256'] == canonical(frozen['cells']['main'])
            and declared['expected_main_pairs'] == 48 and declared['expected_main_games'] == 96
            and declared['result_adaptive_selection'] is False, 'declared main selection differs')
    return frozen, profiles


def freeze_plan(path, destination):
    with patch.object(formal, 'load_plan', side_effect=load_plan), patch.object(formal, 'HARNESS', HARNESS):
        frozen = formal.freeze_plan(path, destination)
    verify(destination)
    return frozen


def _reference(entry, base):
    return Path(formal.pilot.referenced_file(entry, base)).resolve()


def verify_transfer(directory):
    """Check cached product/independent audits and their exact current inputs.

    The parent's full product audit is generated once by the coordinator before
    this adapter runs. This function hashes recorded evidence; it never repeats
    the parent's PGN/PV/CPU replay or launches native/engine work.
    """
    directory = Path(directory).resolve()
    frozen, _ = verify(directory)
    plan, spec = frozen['plan'], frozen['plan']['preflight_transfer']
    parent = Path(spec['directory']).resolve()
    require(parent != directory, 'preflight source must be a distinct original directory')
    old, _ = formal.verify_frozen(parent)
    base = Path(frozen['source_plan_path']).parent
    declared = plan.get('preflight_transfer_policy')
    if declared is not None:
        for key, target in (('source_frozen_plan', parent/'frozen-plan.json'),
                ('source_seal', parent/'seal.json'), ('source_preflight_bank', parent/'preflight-openings.json')):
            require(_reference(declared[key], base) == target, 'declared transfer source binding differs: ' + key)
        _reference(declared['source_plan'], base)
        require(declared['source_plan']['sha256'] == old['source_plan_sha256']
            and declared['source_frozen_runtime_sha256'] == old['harness_source_sha256']
            and declared['source_engine_profiles'] == old['profiles']
            and declared['source_preflight_cells_canonical_sha256'] == canonical(old['cells']['preflight']),
            'declared parent plan/source/profile/schedule identities differ')
    old_sources = {Path(path).name: value for path, value in old['harness_source_sha256'].items()}
    new_sources = {Path(path).name: value for path, value in frozen['harness_source_sha256'].items()}
    require(set(old_sources) == set(V6_HARNESS)
        and all(new_sources.get(name) == value for name, value in old_sources.items()),
        'parent/new original 33 runtime source identities differ')
    require(all(key in plan and key in old['plan'] and plan[key] == old['plan'][key]
                for key in PRECONDITIONS), 'parent/new preflight conditions differ')
    require(plan['openings'] == old['plan']['openings'], 'original full main-bank binding differs')
    require(frozen['cells']['preflight'] == old['cells']['preflight']
        and frozen['banks']['preflight']['sha256'] == old['banks']['preflight']['sha256'],
        'parent/new preflight bank or schedule differs')
    require({name: row['sha256'] for name, row in frozen['profiles'].items()}
        == {name: row['sha256'] for name, row in old['profiles'].items()}, 'profile snapshots differ')
    product_path, peer_path = (_reference(spec[key], base) for key in ('product_audit', 'peer_audit'))
    product, peer = formal.read_json(product_path), formal.read_json(peer_path)
    require(product.get('status') == 'PASS' and product.get('phase') == 'preflight'
        and product.get('phase_status') == 'COMPLETE' and product.get('valid_for_comparison') is True
        and product.get('audited_pairs') == product.get('scheduled_pairs') == 12,
        'parent product preflight audit is not complete/valid')
    require(product.get('guard_summary') == formal.read_json(parent/'preflight'/'load-summary.json')
        and product.get('raw_cpu_audit', {}).get('status') == 'PASS'
        and product['raw_cpu_audit'].get('valid_for_load_check') is True,
        'parent product audit is not bound to the current valid load evidence')
    require(peer.get('status') == 'PASS' and peer.get('phase') == 'preflight'
        and Path(peer.get('directory', '')).resolve() == parent and peer.get('plan_id') == old['plan']['id']
        and peer.get('valid_for_declared_local_comparison') is True
        and peer.get('scheduled_pairs') == peer.get('audited_pairs') == 12
        and peer.get('scheduled_games') == peer.get('completed_games') == 24
        and peer.get('incomplete_games') == peer.get('errors') == 0,
        'parent independent preflight audit is not complete/valid')
    inputs = peer.get('input_sha256')
    require(isinstance(inputs, dict) and bool(inputs), 'independent input SHA map missing')
    normalized = {str(Path(path).resolve()): digest for path, digest in inputs.items()}
    required = [parent/name for name in ('frozen-plan.json', 'seal.json', 'source-plan.json')]
    required += [parent/'preflight'/name for name in ('report.json', 'load-summary.json', 'load.jsonl', 'job-final-accounting.json')]
    for cell in old['cells']['preflight']:
        required += [parent/'preflight'/'cells'/cell['id']/name
                     for name in ('games.json', 'games.pgn', 'engine-cpu.jsonl', 'invocation.json')]
    require(all(str(path.resolve()) in normalized for path in required), 'independent raw-input coverage missing')
    require(all(Path(path).is_absolute() and formal.sha256(path) == digest for path, digest in inputs.items()),
            'independent audit inputs changed')
    report = formal.read_json(parent/'preflight'/'report.json')
    require(report.get('status') == 'COMPLETE' and report.get('valid_for_comparison') is True
        and report.get('completed_games') == report.get('scheduled_games') == 24
        and report.get('frozen_plan_sha256') == formal.sha256(parent/'frozen-plan.json'),
        'parent raw preflight report differs')
    return {'schema_version': 1, 'status': 'PASS', 'phase': 'preflight', 'phase_status': 'TRANSFERRED',
        'valid_for_comparison': True, 'directory': str(directory), 'source_directory': str(parent),
        'frozen_plan_sha256': formal.sha256(directory/'frozen-plan.json'),
        'source_frozen_plan_sha256': formal.sha256(parent/'frozen-plan.json'),
        'source_plan_sha256': old['source_plan_sha256'], 'new_plan_sha256': frozen['source_plan_sha256'],
        'source_preflight_report_sha256': formal.sha256(parent/'preflight'/'report.json'),
        'product_audit': {'path': str(product_path), 'sha256': formal.sha256(product_path)},
        'peer_audit': {'path': str(peer_path), 'sha256': formal.sha256(peer_path)},
        'verified_runtime_sources': len(V6_HARNESS), 'identical_preconditions': list(PRECONDITIONS),
        'source_preflight_games': 24, 'new_main_games': 96,
        'scope': 'Unchanged completed preflight reference; no copied, rerun or pooled games.'}


def run_phase(directory):
    directory = Path(directory).resolve()
    transfer = verify_transfer(directory)
    with (directory/'preflight-transfer.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(dict(transfer, verified_utc=formal.utc_now()), stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    def gate(target, phase):
        if Path(target).resolve() == directory and phase == 'preflight':
            return deepcopy(transfer)
        return ORIGINAL_AUDIT(target, phase)
    with patch.object(formal, 'audit_phase', side_effect=gate):
        return formal.run_phase(directory, 'main')


def audit_phase(directory, phase='main'):
    require(phase in ('preflight', 'main'), 'unknown phase')
    transfer = verify_transfer(directory)
    if phase == 'preflight':
        return transfer
    recorded = formal.read_json(Path(directory)/'preflight-transfer.json')
    require(all(recorded.get(key) == value for key, value in transfer.items()), 'saved transfer reference changed')
    return dict(ORIGINAL_AUDIT(directory, 'main'), preflight_transfer=transfer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'run', 'audit'))
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--phase', choices=('main', 'preflight'), default='main')
    args = parser.parse_args(argv)
    try:
        if args.command == 'freeze':
            frozen = freeze_plan(args.plan, args.directory)
            result = {'status': 'FROZEN', 'scheduled_main_games': 2*len(frozen['cells']['main'])}
        elif args.command == 'run':
            require(args.phase == 'main', 'preflight is referenced, not rerun')
            result = run_phase(args.directory)
        else:
            result = audit_phase(args.directory, args.phase)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status') in ('FROZEN', 'COMPLETE', 'PASS') else 2
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('error: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
