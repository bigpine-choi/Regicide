"""Freeze then collect one bounded default-horizon head-only learning bank.

One root per existing source family is extended by two predetermined quiet
moves. Three exposed QGA development roots teach the missed opponent reply.
No teacher/engine call occurs in reserve(); collection refuses a second start.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time

import chess

import neural_stm as stm
import neural_training as base
import nnue_choice_pairs as historical
import nnue_live_pairs as live
from prepare_neural_huber import read, require, sha, write
from prepare_nnue_generalization import jsonl, lines
from stockfish_teacher import label_rows, source_board
from train_neural_stm import ROOT
from train_nnue_loss_repair import load_bank

OLD = ROOT / 'docs/checks/2026-10-22-nnue-choice/data-002'
ROOTS = ROOT / 'docs/checks/2026-10-22-nnue-generalization/data-001/selected-roots.json'
FAILED = ROOT / 'docs/checks/2026-10-22-nnue-output-only/attempt-001/move-choice-gate.json'
WARM = ROOT / 'docs/checks/2026-10-22-nnue-output-only/attempt-001/model.nnue'
WARM_SHA = '4e100302474cc3861ee5ef2ec69a641dad0e08cc94c1b685f80c3bb2e8883700'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'
CRITICAL_FAMILY = 'm2-formal-queen-gambit-accepted-e4-20261021'


def _quiet_extension(row):
    board = source_board(row)
    extension = []
    for _ in range(2):
        choices = []
        for move in sorted(board.legal_moves, key=lambda m: m.uci()):
            if board.is_capture(move) or board.gives_check(move):
                continue
            board.push(move)
            eligible = board.outcome(claim_draw=True) is None
            board.pop()
            if eligible:
                choices.append(move)
        if not choices:
            return None
        board.push(choices[0]); extension.append(choices[0].uci())
    return {**row, 'query_id': 'head-live-' + row['query_id'],
            'history': row['history'] + extension, 'fen': board.fen(en_passant='fen'),
            'feature_hash': base.feature_identity(board), 'kind': 'two_quiet_extended_live_root',
            'source_query_id': row['query_id'], 'deterministic_extension_uci': extension,
            'target_white_cp': 0}


def _root_features(row):
    board = source_board(row)
    features = {base.feature_identity(board)}
    for move in board.legal_moves:
        board.push(move); features.add(base.feature_identity(board)); board.pop()
    return features


def _critical_roots():
    case = next(c for c in read(FAILED)['cases'] if c['source']['query_id'] == 'choice-gate-opening-30')
    root = case['source']
    require(root['family_id'] == CRITICAL_FAMILY, 'Critical source family changed')
    result = []
    for suffix, move, receipt in (
            ('root', None, case['teacher_receipt']),
            ('after-bf5', 'c8f5', case['child_teacher_receipts']['candidate']),
            ('after-ng4', 'f6g4', case['child_teacher_receipts']['warm'])):
        board = source_board(root)
        history = root['history'].copy()
        if move:
            board.push_uci(move); history.append(move)
        require(board.fen(en_passant='fen') == chess.Board(receipt['fen']).fen(en_passant='fen'),
                'Critical reused teacher source differs')
        result.append({'query_id': 'head-critical-qga-' + suffix,
                       'initial_fen': root['initial_fen'], 'history': history,
                       'fen': board.fen(en_passant='fen'), 'feature_hash': base.feature_identity(board),
                       'family_id': CRITICAL_FAMILY, 'partition': 'train', 'target_white_cp': 0,
                       'kind': 'exposed_qga_refutation_development', 'critical_refutation': True,
                       'depth_policy': 'default_last_completed_depth' if move is None else 'critical_parent_completed_depth_minus_one',
                       'reused_root_teacher_receipt': receipt,
                       'reused_root_teacher_source': {'path': str(FAILED), 'sha256': sha(FAILED)}})
    return result


def reserve(output, gate_path, collector):
    require(not output.exists(), 'Refuse to replace reserved head-repair bank')
    require(sha(WARM) == WARM_SHA, 'Head-repair warm model changed')
    gate = read(gate_path)
    block = gate_path.parent / 'blocked-features.json'
    require(sha(block) == gate['blocked_features_sha256'] and gate['warm_sha256'] == WARM_SHA,
            'New gate/model reservation binding changed')
    blocked = set(read(block))
    manifest, rows, _ = load_bank(OLD)
    pairs, _ = historical.load_pairs(OLD, rows, manifest)
    identities, families = live.existing_features(pairs, rows, manifest)
    require(all(CRITICAL_FAMILY not in value for value in families.values()),
            'Critical QGA family already occurs in historical data')
    chosen = {}
    for row in sorted(read(ROOTS), key=lambda r: (r['family_id'], r['query_id'])):
        chosen.setdefault(row['family_id'], row)
    require(len(chosen) == 32 and Counter(r['partition'] for r in chosen.values()) == {'train': 24, 'tuning': 8},
            'Expected original 24/8 source families')
    candidates = [r for row in chosen.values() if (r := _quiet_extension(row)) is not None] + _critical_roots()
    feature_sets = {r['query_id']: _root_features(r) for r in candidates}
    owners = defaultdict(set)
    for row in candidates:
        for feature in feature_sets[row['query_id']]:
            owners[feature].add(row['partition'])
    cross = {feature for feature, parts in owners.items() if len(parts) > 1}
    selected, rejected = [], []
    for row in candidates:
        part = row['partition']
        other = set().union(*(values for p, values in identities.items() if p != part))
        bad_family = any(row['family_id'] in values for p, values in families.items() if p != part)
        if bad_family or feature_sets[row['query_id']] & (other | blocked | cross):
            rejected.append({'query_id': row['query_id'], 'reason': 'prelabel_gate_or_cross_partition'})
        else:
            selected.append(row)
    require(all(any(r['partition'] == p and not r.get('critical_refutation') for r in selected)
                for p in ('train', 'tuning')), 'Need predetermined fresh roots in both partitions')
    require(sum(bool(r.get('critical_refutation')) for r in selected) == 3, 'Critical roots violate reserved input boundaries')
    profile = read(PROFILE)
    teacher, engine = Path(profile['binary']['path']), Path(gate['binary_path'])
    require(sha(teacher) == profile['binary']['sha256'] and sha(engine) == gate['binary_sha256'], 'Engine/teacher bytes changed')
    sources = [Path(__file__), ROOT / 'scripts/nnue_live_pairs.py', ROOT / 'scripts/nnue_choice_pairs.py',
               ROOT / 'scripts/collect_live_choice.cpp', ROOT / 'scripts/live_choice_support.hpp',
               ROOT / 'scripts/choice_leaf_support.hpp',
               ROOT / 'scripts/build_live_choice.ps1',
               ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_stm.py', ROOT / 'scripts/neural_training.py']
    require(all(p.is_file() for p in sources), 'Missing required collection source')
    inputs = [*sources, ROOTS, FAILED, WARM, PROFILE, collector, teacher, engine, gate_path, block,
              gate_path.parent / 'frozen-roots.json', OLD / 'manifest.json',
              *[OLD / name for name in (*manifest['files'], *manifest['pair_files'], *manifest['backed_pair_files'])]]
    output.mkdir(parents=True)
    write(output / 'selected-roots.json', selected)
    write(output / 'reservation-audit.json', {'selected': len(selected), 'selected_fresh': len(selected) - 3,
        'critical_development_roots': 3, 'rejected_prelabel': rejected,
        'root_and_all_legal_children_gate_overlap': 0, 'critical_family_historical_rows_and_pairs': 0,
        'selection': 'Lexicographically first query per original family; exactly two first eligible UCI-lex quiet moves; no outcome refill',
        'new_gate_calls': 0, 'teacher_queries': 0, 'native_searches': 0})
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(),
        'old_data': str(OLD), 'gate_plan': str(gate_path), 'collector': str(collector),
        'native_engine': str(engine), 'teacher': str(teacher), 'warm_model': str(WARM),
        'selected_roots_sha256': sha(output / 'selected-roots.json'),
        'maximum_teacher_queries': 110, 'maximum_root_teacher_queries': 32, 'maximum_child_teacher_queries': 70,
        'maximum_default_native_searches': 35, 'maximum_forced_native_searches': 70,
        'maximum_total_native_searches': 105, 'default_nodes': 10000, 'maximum_branch_depth': 4,
        'depth_policy': 'Fresh and QGA parent use last completed default 10k depth D; QGA reply roots use parent D-1; reject D outside1..4',
        'branch_profile': 'All default engine options, forced full-window first move, depth D and <=10k nodes',
        'support': 'Observed final completed PV endpoint, actual prefix witness, full history/raw integer root-score match; not complete causal TT provenance',
        'target': 'Teacher root-child white-score gap surrogate through two observed support leaves',
        'point_data_and_old_pairs_byte_unchanged': True, 'old_support_model': manifest['warm_model'],
        'critical_roots': 'Previously exposed QGA root/Bf5/Ng4 child are TRAIN development, never independent evidence',
        'no_outcome_refill': True, 'maximum_fits': 1,
        'input_sha256': {str(p): sha(p) for p in inputs}}
    write(output / 'protocol.json', protocol)
    for source in sources:
        target = output / 'source-snapshot' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)
    print(json.dumps({'reserved_roots': len(selected), 'protocol_sha256': sha(output / 'protocol.json')}), flush=True)


def _collector_call(collector, model, depth, entries, output, label):
    tsv = ''.join(row['query_id'] + '\t' + row['initial_fen'] + '\t' + ' '.join(row['history'])
                  + '\t' + move + '\n' for row, move in entries)
    (output / (label + '-input.tsv')).write_text(tsv, encoding='utf-8', newline='\n')
    receipt_path = output / (label + '-receipts.jsonl')
    with receipt_path.open('w', encoding='utf-8', newline='\n') as stdout, \
            (output / (label + '-stderr.txt')).open('w', encoding='utf-8') as stderr:
        result = subprocess.run([str(collector), '--model', str(model), '--depth', str(depth), '--nodes', '10000'],
                                input=tsv, text=True, stdout=stdout, stderr=stderr, timeout=120)
    require(result.returncode == 0, 'Live collector failed; retain partial receipts')
    receipts = lines(receipt_path)
    require(len(receipts) == len(entries) and len({r['id'] for r in receipts}) == len(entries), 'Missing/duplicate native receipts')
    return receipts


def collect(output):
    started = time.monotonic()
    protocol = read(output / 'protocol.json')
    require(not (output / 'collection-started.json').exists(), 'Collection already started; never re-query or restart')
    require(sha(output / 'selected-roots.json') == protocol['selected_roots_sha256'], 'Reserved roots changed')
    for path, digest in protocol['input_sha256'].items():
        require(sha(path) == digest, 'Reserved input changed: ' + path)
    write(output / 'collection-started.json', {'created_utc': datetime.now(timezone.utc).isoformat(),
                                              'protocol_sha256': sha(output / 'protocol.json')})
    roots = read(output / 'selected-roots.json')
    collector, teacher = Path(protocol['collector']), Path(protocol['teacher'])
    model = stm.load_model(WARM)
    old_manifest, rows, _ = load_bank(OLD)
    existing, _ = historical.load_pairs(OLD, rows, old_manifest)
    identities, families = live.existing_features(existing, rows, old_manifest)
    blocked_path = Path(protocol['gate_plan']).parent / 'blocked-features.json'
    blocked = set(read(blocked_path))
    fresh = [r for r in roots if not r.get('critical_refutation')]
    labels, root_report = label_rows(teacher, fresh, output / 'root-teacher', nodes=100000, max_depth=10, max_abs_cp=1500)
    root_receipts = {r['source_query_id']: r for r in lines(output / 'root-teacher/teacher-queries.jsonl')}
    root_map = {r['query_id']: r for r in roots}
    first_roots = [r for r in roots if r.get('depth_policy') != 'critical_parent_completed_depth_minus_one']
    reply_roots = [r for r in roots if r.get('depth_policy') == 'critical_parent_completed_depth_minus_one']
    native_rows = _collector_call(collector, WARM, 64, [(r, '-') for r in first_roots], output, 'default')
    native_map = {r['id']: r for r in native_rows}
    parent = native_map['head-critical-qga-root']['last_completed_iteration']['depth']
    if 2 <= parent <= 4:
        reply_native = _collector_call(collector, WARM, parent - 1, [(r, '-') for r in reply_roots], output, 'critical-reply-default')
        native_rows.extend(reply_native)
        native_map.update({r['id']: r for r in reply_native})
    else:
        write(output / 'skipped-native-roots.json', [{'query_id': r['query_id'],
            'reason': 'parent_completed_depth_outside_2_to_4', 'parent_completed_depth': parent} for r in reply_roots])
    candidates, decisions, exclusions = [], [], Counter()
    for root in roots:
        native = native_map.get(root['query_id'])
        if native is not None:
            native = {**native, 'model_sha256': WARM_SHA}
        complete = native.get('last_completed_iteration', {}) if native else {}
        default_depth = complete.get('depth', 0)
        override = root.get('depth_policy') == 'critical_parent_completed_depth_minus_one'
        depth = parent - 1 if override else default_depth
        receipt = root.get('reused_root_teacher_receipt', root_receipts.get(root['query_id']))
        move_a = receipt.get('bestmove_uci') if receipt and receipt.get('status') == 'accepted' else None
        move_b = complete.get('bestmove')
        decision = {'root': root, 'native_receipt': native, 'teacher_recommendation_receipt': receipt,
                    'teacher_move': move_a, 'student_move': move_b,
                    'depth_decision': {'requested_nodes': 10000, 'default_completed_depth': default_depth,
                        'parent_completed_depth': parent if override else None, 'branch_depth': depth,
                        'policy': 'critical_parent_completed_depth_minus_one' if override else 'default_last_completed_depth'}}
        decisions.append(decision)
        if not (type(depth) is int and 1 <= depth <= 4) or not move_a or not move_b:
            exclusions['missing_exact_root_or_supported_depth'] += 1; continue
        if move_a == move_b:
            exclusions['already_teacher_first_move'] += 1; continue
        candidates.append(decision)
    write(output / 'student-choices.json', decisions)
    branches = {}
    for depth in sorted({c['depth_decision']['branch_depth'] for c in candidates}):
        entries = []
        for pair in candidates:
            if pair['depth_decision']['branch_depth'] != depth: continue
            for side in ('a', 'b'):
                entries.append(({**pair['root'], 'query_id': pair['root']['query_id'] + '-' + side}, pair['teacher_move' if side == 'a' else 'student_move']))
        branches.update({r['id']: r for r in _collector_call(collector, WARM, depth, entries, output, 'forced-depth-' + str(depth))})
    usable, children = [], []
    for pair in candidates:
        root = pair['root']
        if not all(branches[root['query_id'] + '-' + side].get('accepted') for side in ('a', 'b')):
            exclusions['unproved_branch_pair'] += 1; continue
        usable.append(pair)
        for side in ('a', 'b'):
            move = pair['teacher_move' if side == 'a' else 'student_move']
            board = source_board(root); board.push_uci(move)
            children.append({'query_id': root['query_id'] + '-' + side, 'initial_fen': root['initial_fen'],
                'history': root['history'] + [move], 'fen': board.fen(en_passant='fen'), 'target_white_cp': 0,
                'family_id': root['family_id'], 'partition': root['partition'], 'kind': 'live_root_child_teacher'})
    write(output / 'selected-root-children.json', children)
    require(len(fresh) <= 32 and len(children) <= 70 and len(native_rows) + len(branches) <= 105, 'Fixed collection budget exceeded')
    child_labels, child_report = label_rows(teacher, children, output / 'child-teacher', nodes=100000, max_depth=10, max_abs_cp=1500)
    child_map = {r['query_id']: r for r in child_labels}
    child_receipts = {r['source_query_id']: r for r in lines(output / 'child-teacher/teacher-queries.jsonl')}
    verified = []
    for pair in usable:
        root = pair['root']
        if not all(root['query_id'] + '-' + side in child_map for side in ('a', 'b')):
            exclusions['missing_exact_child_teacher'] += 1; continue
        record = {'kind': 'live_search_backed_gap', 'root_id': root['query_id'], 'initial_fen': root['initial_fen'],
            'root_history': root['history'], 'root_fen': root['fen'], 'root_sign': 1 if source_board(root).turn else -1,
            'family_id': root['family_id'], 'partition': root['partition'],
            'branch_depth': pair['depth_decision']['branch_depth'], 'depth_decision': pair['depth_decision'],
            'critical_refutation': bool(root.get('critical_refutation')),
            'move_a': pair['teacher_move'], 'move_b': pair['student_move']}
        for side in ('a', 'b'):
            key = root['query_id'] + '-' + side
            support = {**branches[key], 'model_sha256': WARM_SHA}
            leaf, label = support['support_leaf'], child_map[key]
            record[side] = {'fen': leaf['fen'], 'initial_fen': root['initial_fen'], 'history': leaf['history'],
                'feature_hash': base.feature_identity(chess.Board(leaf['fen'])), 'family_id': root['family_id'],
                'partition': root['partition'], 'target_white_cp': label['target_white_cp'],
                'teacher_root_child_white_cp': label['target_white_cp'], 'target_semantics': live.TARGET_SEMANTICS,
                'root_child': {'fen': label['fen'], 'history': label['history'], 'teacher_receipt': child_receipts[key]},
                'support_receipt': support}
        record['target_gap_white_cp'] = record['a']['target_white_cp'] - record['b']['target_white_cp']
        if record['a']['feature_hash'] == record['b']['feature_hash']:
            exclusions['identical_leaf_inputs'] += 1; continue
        features = live.validate_pair(record, model, model_sha256=WARM_SHA, teacher_sha256=read(PROFILE)['binary']['sha256'])
        verified.append((record, features))
    feature_owners = defaultdict(set)
    for record, features in verified:
        for feature in features: feature_owners[feature].add(record['partition'])
    cross = {f for f, parts in feature_owners.items() if len(parts) > 1}
    added = {'train': [], 'tuning': []}
    keys = {(p, r['root_id'], tuple(sorted((r['move_a'], r['move_b'])))) for p, group in existing.items() for r in group}
    for record, features in verified:
        part = record['partition']
        other = set().union(*(values for p, values in identities.items() if p != part))
        key = (part, record['root_id'], tuple(sorted((record['move_a'], record['move_b']))))
        if key in keys or features & (blocked | cross | other):
            exclusions['duplicate_or_gate_or_cross_partition_full_path'] += 1; continue
        keys.add(key); added[part].append(record)
    require(all(added.values()), 'No authenticated live comparisons in one partition; no refill permitted')
    for name in (*old_manifest['files'], *old_manifest['pair_files'], *old_manifest['backed_pair_files']):
        shutil.copyfile(OLD / name, output / name)
    for part in added: jsonl(output / ('pairs-live-' + part + '.jsonl'), added[part])
    manifest = {**old_manifest, 'collection_source': str(output),
        'source_protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
        'historical_source': {'path': str(OLD / 'manifest.json'), 'sha256': sha(OLD / 'manifest.json')},
        'live_warm_model': {'path': str(WARM), 'sha256': WARM_SHA},
        'live_teacher_sha256': read(PROFILE)['binary']['sha256'],
        'live_decisions_sha256': sha(output / 'student-choices.json'),
        'live_gate': {'blocked_features': str(blocked_path), 'blocked_features_sha256': sha(blocked_path)},
        'live_pair_files': {'pairs-live-' + p + '.jsonl': sha(output / ('pairs-live-' + p + '.jsonl')) for p in added},
        'live_pair_rows': {p: len(v) for p, v in added.items()},
        'live_combined_pair_rows': {p: len(v) + len(existing[p]) for p, v in added.items()},
        'live_critical_pair_rows': {p: sum(bool(r['critical_refutation']) for r in v) for p, v in added.items()},
        'live_exclusions': dict(exclusions), 'live_native_searches': len(native_rows) + len(branches),
        'live_teacher_queries': len(fresh) + len(children),
        'live_teacher_seconds': root_report['elapsed_seconds'] + child_report['elapsed_seconds'],
        'live_elapsed_seconds': time.monotonic() - started}
    write(output / 'manifest.json', manifest)
    _, checked, _ = load_bank(output); live.load_pairs(output, checked, manifest)
    for path, digest in protocol['input_sha256'].items():
        require(sha(path) == digest, 'Collection input changed: ' + path)
    write(output / 'collection-complete.json', {'manifest_sha256': sha(output / 'manifest.json'),
        'native_searches': manifest['live_native_searches'], 'teacher_queries': manifest['live_teacher_queries'],
        'live_pair_rows': manifest['live_pair_rows'], 'elapsed_seconds': manifest['live_elapsed_seconds']})
    print(json.dumps({'live_pair_rows': manifest['live_pair_rows'], 'live_combined_pair_rows': manifest['live_combined_pair_rows'],
                      'teacher_queries': manifest['live_teacher_queries'], 'native_searches': manifest['live_native_searches']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    reserve_parser = sub.add_parser('reserve')
    reserve_parser.add_argument('output', type=Path)
    reserve_parser.add_argument('--gate-plan', type=Path, required=True)
    reserve_parser.add_argument('--collector', type=Path, required=True)
    collect_parser = sub.add_parser('collect'); collect_parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'reserve': reserve(args.output.resolve(), args.gate_plan.resolve(), args.collector.resolve())
    else: collect(args.output.resolve())
