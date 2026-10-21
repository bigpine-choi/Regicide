"""Assemble authenticated live pairs from completed receipts; zero processes.

The collector may honestly accept an older completed PV prefix. The matched
horizon learning bank must additionally require the requested depth itself.
Preserve every raw receipt and drop incomplete pairs without replacing them.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import shutil

import chess

import neural_stm as stm
import neural_training as base
import nnue_choice_pairs as historical
import nnue_live_pairs as live
from prepare_neural_huber import read, require, sha, write
from prepare_nnue_generalization import jsonl, lines
from train_neural_stm import ROOT
from train_nnue_loss_repair import load_bank


def finalize(source, output):
    require(not output.exists(), 'Refuse to overwrite cached canonical bank')
    collection = read(source / 'protocol.json')
    resolved = {}
    for text, digest in collection['input_sha256'].items():
        path = Path(text)
        snapshot = source / 'source-snapshot' / path.relative_to(ROOT) if path.suffix in ('.py', '.cpp', '.hpp', '.ps1') else path
        actual = path if path.is_file() and sha(path) == digest else snapshot
        require(actual.is_file() and sha(actual) == digest, 'Original input changed: ' + text)
        resolved[text] = {'path': str(actual), 'sha256': digest}
    old = Path(collection['old_data'])
    old_manifest, rows, _ = load_bank(old)
    existing, _ = historical.load_pairs(old, rows, old_manifest)
    identities, _ = live.existing_features(existing, rows, old_manifest)
    model_path = Path(collection['warm_model'])
    model, model_sha = stm.load_model(model_path), sha(model_path)
    block_path = Path(collection['gate_plan']).parent / 'blocked-features.json'
    blocked = set(read(block_path))
    teacher_sha = sha(collection['teacher'])
    decisions = read(source / 'student-choices.json')
    branches = {r['id']: r for depth in range(1, 5) if (source / f'forced-depth-{depth}-receipts.jsonl').exists()
                for r in lines(source / f'forced-depth-{depth}-receipts.jsonl')}
    child_rows = {r['query_id']: r for r in lines(source / 'child-teacher/labels.jsonl')}
    child_receipts = {r['source_query_id']: r for r in lines(source / 'child-teacher/teacher-queries.jsonl')}
    verified, removed, exclusions = [], [], Counter()
    for decision in decisions:
        root = decision['root']; depth = decision['depth_decision']['branch_depth']
        ids = [root['query_id'] + '-' + side for side in ('a', 'b')]
        if not all(key in branches for key in ids):
            exclusions['no_original_branch_pair'] += 1; continue
        receipts = [branches[key] for key in ids]
        if not all(r.get('accepted') is True and r.get('depth_limit') == depth
                   and r.get('last_completed_iteration', {}).get('depth') == depth
                   and r.get('last_completed_iteration', {}).get('stopped') is False for r in receipts):
            removed.append({'root_id': root['query_id'], 'partition': root['partition'], 'requested_depth': depth,
                'branch_depths': [r.get('last_completed_iteration', {}).get('depth') for r in receipts],
                'reason': 'requested_matched_iteration_incomplete'})
            exclusions['unproved_or_incomplete_matched_branch_pair'] += 1; continue
        if not all(key in child_rows for key in ids):
            exclusions['missing_exact_child_teacher'] += 1; continue
        board = chess.Board(root['fen'])
        record = {'kind': 'live_search_backed_gap', 'root_id': root['query_id'],
            'initial_fen': root['initial_fen'], 'root_history': root['history'], 'root_fen': root['fen'],
            'root_sign': 1 if board.turn else -1, 'family_id': root['family_id'], 'partition': root['partition'],
            'branch_depth': depth, 'depth_decision': decision['depth_decision'],
            'critical_refutation': bool(root.get('critical_refutation')),
            'move_a': decision['teacher_move'], 'move_b': decision['student_move']}
        for side in ('a', 'b'):
            key = root['query_id'] + '-' + side
            support = {**branches[key], 'model_sha256': model_sha}
            leaf, label = support['support_leaf'], child_rows[key]
            record[side] = {'fen': leaf['fen'], 'initial_fen': root['initial_fen'], 'history': leaf['history'],
                'feature_hash': base.feature_identity(chess.Board(leaf['fen'])), 'family_id': root['family_id'],
                'partition': root['partition'], 'target_white_cp': label['target_white_cp'],
                'teacher_root_child_white_cp': label['target_white_cp'], 'target_semantics': live.TARGET_SEMANTICS,
                'root_child': {'fen': label['fen'], 'history': label['history'], 'teacher_receipt': child_receipts[key]},
                'support_receipt': support}
        record['target_gap_white_cp'] = record['a']['target_white_cp'] - record['b']['target_white_cp']
        if record['a']['feature_hash'] == record['b']['feature_hash']:
            exclusions['identical_leaf_inputs'] += 1; continue
        live.validate_decision(record, decision, model_sha)
        features = live.validate_pair(record, model, model_sha256=model_sha, teacher_sha256=teacher_sha)
        verified.append((record, features))
    # This fixes exactly the one honest-but-short completed-prefix receipt.
    require(len(removed) == 1 and removed[0]['root_id'] == 'head-live-family-query-011943'
            and removed[0]['requested_depth'] == 2 and 1 in removed[0]['branch_depths'],
            'Unexpected matched-depth correction; do not alter collection policy')
    owners = defaultdict(set)
    for record, features in verified:
        for feature in features: owners[feature].add(record['partition'])
    cross = {feature for feature, parts in owners.items() if len(parts) > 1}
    added = {'train': [], 'tuning': []}
    keys = {(p, r['root_id'], tuple(sorted((r['move_a'], r['move_b'])))) for p, group in existing.items() for r in group}
    for record, features in verified:
        part = record['partition']
        other = set().union(*(values for p, values in identities.items() if p != part))
        key = (part, record['root_id'], tuple(sorted((record['move_a'], record['move_b']))))
        if key in keys or features & (blocked | cross | other):
            exclusions['duplicate_or_gate_or_cross_partition_full_path'] += 1; continue
        keys.add(key); added[part].append(record)
    require(all(added.values()), 'No usable live comparisons in a partition; no refill permitted')
    raw_hashes = {str(path): sha(path) for path in sorted(source.rglob('*')) if path.is_file()}
    output.mkdir(parents=True)
    for name in (*old_manifest['files'], *old_manifest['pair_files'], *old_manifest['backed_pair_files']):
        shutil.copyfile(old / name, output / name)
    shutil.copyfile(source / 'student-choices.json', output / 'student-choices.json')
    for part, records in added.items(): jsonl(output / f'pairs-live-{part}.jsonl', records)
    sources = (Path(__file__).resolve(), Path(__file__).with_name('prepare_nnue_head_repair.py').resolve(),
               Path(__file__).with_name('nnue_live_pairs.py').resolve())
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'assembly_only': True,
        'original_collection_protocol': {'path': str(source / 'protocol.json'), 'sha256': sha(source / 'protocol.json')},
        'original_collection_started': {'path': str(source / 'collection-started.json'), 'sha256': sha(source / 'collection-started.json')},
        'new_teacher_queries': 0, 'new_native_searches': 0, 'new_training_runs': 0,
        'removed_incomplete_pairs': removed, 'no_refill_or_policy_change': True,
        'historical_input_resolution': resolved, 'raw_collection_sha256': raw_hashes,
        'current_sources': {str(path): sha(path) for path in sources}}
    write(output / 'protocol.json', protocol)
    default_rows = lines(source / 'default-receipts.jsonl')
    reply_path = source / 'critical-reply-default-receipts.jsonl'
    default_count = len(default_rows) + (len(lines(reply_path)) if reply_path.exists() else 0)
    root_receipts = lines(source / 'root-teacher/teacher-queries.jsonl')
    root_report, child_report = read(source / 'root-teacher/teacher-report.json'), read(source / 'child-teacher/teacher-report.json')
    manifest = {**old_manifest, 'collection_source': str(source),
        'source_protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
        'historical_source': {'path': str(old / 'manifest.json'), 'sha256': sha(old / 'manifest.json')},
        'live_warm_model': {'path': str(model_path), 'sha256': model_sha},
        'live_teacher_sha256': teacher_sha, 'live_decisions_sha256': sha(output / 'student-choices.json'),
        'live_gate': {'blocked_features': str(block_path), 'blocked_features_sha256': sha(block_path)},
        'live_pair_files': {f'pairs-live-{p}.jsonl': sha(output / f'pairs-live-{p}.jsonl') for p in added},
        'live_pair_rows': {p: len(v) for p, v in added.items()},
        'live_combined_pair_rows': {p: len(v) + len(existing[p]) for p, v in added.items()},
        'live_critical_pair_rows': {p: sum(bool(r['critical_refutation']) for r in v) for p, v in added.items()},
        'live_exclusions': dict(exclusions), 'live_native_searches': default_count + len(branches),
        'live_teacher_queries': len(root_receipts) + len(child_receipts),
        'live_teacher_seconds': root_report['elapsed_seconds'] + child_report['elapsed_seconds'],
        'cached_assembly': True, 'removed_incomplete_pairs': removed}
    write(output / 'manifest.json', manifest)
    _, checked, _ = load_bank(output); live.load_pairs(output, checked, manifest)
    require(all(sha(path) == digest for path, digest in raw_hashes.items()), 'Original collection bytes changed during assembly')
    write(output / 'assembly-receipt.json', {'completed': True, 'original_receipts_preserved': True,
        'new_teacher_queries': 0, 'new_native_searches': 0, 'new_training_runs': 0,
        'removed_incomplete_pairs': removed, 'canonical_manifest_sha256': sha(output / 'manifest.json'),
        'live_pair_rows': manifest['live_pair_rows'], 'live_critical_pair_rows': manifest['live_critical_pair_rows']})
    print(str({'live_pair_rows': manifest['live_pair_rows'], 'combined': manifest['live_combined_pair_rows'],
               'critical': manifest['live_critical_pair_rows'], 'original_teacher_queries': manifest['live_teacher_queries'],
               'original_native_searches': manifest['live_native_searches'], 'new_queries_and_searches': 0}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path); parser.add_argument('output', type=Path)
    args = parser.parse_args(); finalize(args.source.resolve(), args.output.resolve())
