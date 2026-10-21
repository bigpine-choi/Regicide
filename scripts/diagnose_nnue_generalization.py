"""Read-only reproduction of the frozen ordering candidate's generalization limits.

This reads prior evidence and weights only. It never trains, runs a chess engine,
or reads any new held-out gate labels. Feature coverage measures are descriptive;
shared nonlinear activations and Adam prevent treating them as a causal proof.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

import neural_stm as stm
import train_nnue_ordering as ordering

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'docs/checks/2026-10-22-nnue-ordering'
WARM = ROOT / 'docs/checks/2026-10-22-nnue-loss-repair/attempt-001/model.nnue'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changed_features(records, perspectives):
    result = []
    for pair in records:
        ia, _, _, ib, _, _ = ordering.pack_pairs([pair])
        # 768 is only a zero-padding row, never a chess feature.
        left = set(ia[0, perspectives].ravel().tolist()) - {768}
        right = set(ib[0, perspectives].ravel().tolist()) - {768}
        result.append(left ^ right)
    return result


def coverage(pairs, perspectives):
    changed = {part: changed_features(records, perspectives) for part, records in pairs.items()}
    unions = {part: set.union(*values) for part, values in changed.items()}
    fractions = [len(value & unions['train']) / len(value) for value in changed['tuning']]
    return {'train_changed_feature_count': len(unions['train']),
            'tuning_changed_feature_count': len(unions['tuning']),
            'changed_feature_intersection_count': len(unions['train'] & unions['tuning']),
            'tuning_pair_fraction_seen_in_train': fractions,
            'mean_tuning_pair_fraction_seen_in_train': float(np.mean(fractions)),
            'zero_covered_tuning_pairs': sum(value == 0 for value in fractions)}


def diagnose(output):
    paths = [FROZEN / 'data-001' / ('pairs-' + part + '.jsonl') for part in ('train', 'tuning')]
    pairs = {part: [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
             for part, path in zip(('train', 'tuning'), paths)}
    candidate_path = FROZEN / 'attempt-001/model.nnue'
    selection_path = FROZEN / 'attempt-001/selection.json'
    training_path = FROZEN / 'attempt-001/training.json'
    candidate, warm = stm.load_model(candidate_path), stm.load_model(WARM)
    selection = json.loads(selection_path.read_text(encoding='utf-8'))
    training = json.loads(training_path.read_text(encoding='utf-8'))
    groups, summaries = {}, {}
    for part, records in pairs.items():
        by_family = defaultdict(list)
        for record in records:
            by_family[record['family_id']].append(record)
        groups[part] = {family: ordering.score_pairs(rows, warm, candidate)
                        for family, rows in sorted(by_family.items())}
        metrics = ordering.score_pairs(records, warm, candidate)
        ia, ta, sa, ib, tb, sb = ordering.pack_pairs(records)
        target = ta - tb
        correlations = {}
        for label, model in (('warm', warm), ('ordering', candidate)):
            prediction = stm.predict(model, ia, sa) - stm.predict(model, ib, sb)
            correlations[label] = float(np.corrcoef(target, prediction)[0, 1])
        summaries[part] = {'pairs': len(records), 'families': len(by_family),
                           'family_pair_counts': dict(Counter(row['family_id'] for row in records)),
                           'roots': len({row['root_id'] for row in records}),
                           'metrics': metrics, 'white_gap_target_pearson': correlations}
    evaluations = [{'epoch': row['epoch'], 'eligible': row['eligible'],
                    'point_mse_cp2': row['point']['mse_cp2'],
                    'pair_mse_cp2': row['pair']['mse_cp2'],
                    'pair_strict_order_matches': row['pair']['strict_order_matches'],
                    'anchor_mse_cp2': row['anchor']['mse_cp2'],
                    'objective_cp2': row['objective_cp2']}
                   for row in selection['validation_evaluations']]
    point_rows = training['history'][0]['optimizer_steps']
    inputs = paths + [candidate_path, WARM, selection_path, training_path, Path(__file__),
                      ROOT / 'scripts/neural_stm.py', ROOT / 'scripts/train_nnue_ordering.py',
                      ROOT / 'scripts/neural_training.py']
    report = {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'purpose': 'Reproduce prior ordering generalization diagnosis without fitting or engine execution',
        'input_sha256': {str(path): sha(path) for path in inputs},
        'partitions': summaries, 'by_family': groups,
        'changed_piece_features_white_view': coverage(pairs, slice(0, 1)),
        'changed_shared_weight_features_both_views': coverage(pairs, slice(0, 2)),
        'coverage_definition': 'Set symmetric difference of sibling active piece features; both-view union is the shared weight-row view. White-only coverage does not include mirrored perspective reuse.',
        'selected_epoch': selection['selected_epoch'], 'stopped_epoch': selection['stopped_epoch'],
        'anchor_limit_cp2': selection['anchor_limit_cp2'],
        'evaluations': evaluations,
        'anchor_rejected_epochs': [row['epoch'] for row in evaluations if not row['eligible']],
        'original_pair_exposure': {'point_batches_per_epoch': point_rows,
                                   'unique_training_pairs': len(pairs['train']),
                                   'pair_visits_per_epoch': point_rows * len(pairs['train']),
                                   'batch_policy': selection['pair_batch_policy']},
        'interpretation': [
            'Training pair ordering improves, but the two selection families retain inverse score-gap correlation.',
            'Pair coverage is narrow and the same 18 pairs are revisited for every point minibatch.',
            'Later checkpoints with more order matches violate anchor protection and/or worsen the selection objective.',
            'This supports broader family-disjoint comparisons and fewer repeated pair visits; it does not establish either as the sole cause.',
            'Raw channel gradient size is omitted because Adam normalizes gradients adaptively, so its magnitude alone does not establish update dominance.'
        ],
        'training_runs': 0, 'engine_searches': 0, 'teacher_queries': 0,
        'new_gate_labels_read': False,
        'environment': {'python': sys.version, 'numpy': np.__version__},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8', newline='\n') as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write('\n')
    print(json.dumps({'output': str(output), 'train_pairs': summaries['train']['pairs'],
                      'tuning_pairs': summaries['tuning']['pairs'],
                      'shared_delta_coverage': report['changed_shared_weight_features_both_views'],
                      'anchor_rejected_epochs': report['anchor_rejected_epochs']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    diagnose(args.output.resolve())
