"""Expand existing legal positions with a fixed 2,000-query Stockfish budget.

No new games or random chess moves. Keep original source-family splits, reuse
431 exact labels, and report the actual distribution after teacher exclusions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time

from neural_diverse_bank import build_bank, distribution
from prepare_neural_huber import read, require, sha, write
from stockfish_teacher import label_rows
from train_neural_stm import ROOT, SOURCE

OLD = ROOT / 'docs/checks/2026-10-21-m3-stockfish-teacher/attempt-001'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'
COUNTS = {'train': 1600, 'validation': 300, 'test': 100}


def jsonl(path, rows):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')


def prepare(output):
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=False)
    profile = read(PROFILE)
    engine = Path(profile['binary']['path'])
    require(sha(engine) == profile['binary']['sha256'], 'Frozen Stockfish binary differs')
    reused_path = OLD / 'teacher/labels.jsonl'
    require(sha(reused_path) == '2117ece8b2671906d4f754f9d907751df59b8404904571db9df791e1841d0fc3',
            'Original 431 labels differ')
    reused = [json.loads(line) for line in reused_path.read_text(encoding='utf-8').splitlines()]
    sources = [Path(__file__), ROOT / 'scripts/neural_diverse_bank.py', ROOT / 'scripts/stockfish_teacher.py',
               ROOT / 'scripts/neural_search_data/schema.py']
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'source_root': str(SOURCE),
                'seed': 20261022, 'requested_new_rows': COUNTS, 'maximum_teacher_queries': sum(COUNTS.values()),
                'teacher_nodes_per_query': 10000, 'teacher_max_abs_cp': 1500,
                'budget': 'Fixed maximum 2000 queries x10000 requested nodes, no refill or retry after exclusions',
                'phase_target_percent': {'early': 50, 'middle': 35, 'late': 15},
                'reused_labels': {'path': str(reused_path), 'sha256': sha(reused_path)},
                'teacher_profile': profile, 'no_new_games': True,
                'test_limit': 'Unused by this fine-tune and checkpoint selection; exposed prior development source, not a new independent bank',
                'input_sha256': {str(p): sha(p) for p in [*sources, PROFILE, engine, reused_path]}}
    write(output / 'protocol.json', protocol)
    for source in sources:
        target = output / 'source-snapshot' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    print('Selecting legal, non-overlapping early/middle/late positions.', flush=True)
    bank_start = time.monotonic()
    selected, bank = build_bank(SOURCE, reused, counts=COUNTS, seed=20261022,
                                match_openings_path=ROOT / 'tests/data/comparison_openings.json')
    bank_seconds = time.monotonic() - bank_start
    require(len(selected) == sum(COUNTS.values()), 'Query budget or selected size differs')
    write(output / 'selected.json', selected)
    write(output / 'bank-manifest.json', bank)
    print('Labeling at most 2000 positions with the existing Stockfish18.', flush=True)
    def progress(value):
        if value['completed'] % 128 == 0 or value['completed'] == value['requested']:
            print(json.dumps(value), flush=True)
    accepted, teacher = label_rows(engine, selected, output / 'teacher', nodes=10000, max_abs_cp=1500,
                                   progress=progress)
    rows = []
    for batch, records in (('reused-small', reused), ('expanded-001', accepted)):
        for row in records:
            original_partition = row['partition']
            rows.append({**row, 'source_partition': original_partition,
                         'partition': 'tuning' if original_partition == 'validation' else original_partition,
                         'teacher_batch': batch})
    partitions = {p: [row for row in rows if row['partition'] == p] for p in ('train', 'tuning', 'test')}
    for key in ('family_id', 'feature_hash'):
        sets = {p: {row[key] for row in group} for p, group in partitions.items()}
        require(all(not sets[a] & sets[b] for a, b in (('train', 'tuning'), ('train', 'test'), ('tuning', 'test'))),
                'Expanded dataset family or mirror-feature leakage')
        if key == 'feature_hash':
            require(sum(len(values) for values in sets.values()) == len(rows), 'Duplicate effective piece inputs')
    minimum = {'train': 900, 'tuning': 150, 'test': 30}
    require(all(len(partitions[p]) >= minimum[p] for p in partitions), 'Too few accepted rows; no automatic retry')
    for partition, records in partitions.items():
        jsonl(output / (partition + '.jsonl'), records)
    paths = [output / (p + '.jsonl') for p in partitions]
    manifest = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'files': {path.name: sha(path) for path in paths}, 'rows': {p: len(r) for p, r in partitions.items()},
                'distribution': {p: distribution(r) for p, r in partitions.items()},
                'reused_labels': {'path': str(reused_path), 'sha256': sha(reused_path), 'batch': 'reused-small', 'rows': len(reused)},
                'new_labels': {'path': str(output / 'teacher/labels.jsonl'), 'sha256': sha(output / 'teacher/labels.jsonl'),
                               'batch': 'expanded-001', 'rows': len(accepted)},
                'selection': {'path': str(output / 'selected.json'), 'sha256': sha(output / 'selected.json')},
                'bank_manifest': {'path': str(output / 'bank-manifest.json'), 'sha256': sha(output / 'bank-manifest.json')},
                'generation_protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
                'teacher_report': {'path': str(output / 'teacher/teacher-report.json'), 'sha256': sha(output / 'teacher/teacher-report.json')},
                'partition_policy': 'Original validation renamed tuning; original families and all piece/mirror identities disjoint',
                'test_limit': protocol['test_limit'], 'new_teacher_rows_by_partition': dict(Counter(r['partition'] for r in accepted)),
                'bank_seconds': bank_seconds, 'teacher_seconds': teacher['elapsed_seconds'],
                'total_wall_seconds': time.monotonic() - started}
    write(output / 'manifest.json', manifest)
    print(json.dumps({key: manifest[key] for key in ('rows', 'distribution', 'bank_seconds', 'teacher_seconds')}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    prepare(parser.parse_args().output.resolve())
