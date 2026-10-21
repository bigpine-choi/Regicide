"""Finish duplicate filtering from immutable completed receipts; no processes.

The first collection's final loader caught three old/backed root comparisons
being repeated. Preserve that collection and assemble a separate canonical
dataset without launching native searches, teacher queries or training.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil

from prepare_neural_huber import read, require, sha, write
from prepare_nnue_generalization import jsonl, lines
from train_nnue_loss_repair import load_bank
from nnue_choice_pairs import load_pairs


def finalize(source, output):
    require(not output.exists(), 'Refuse to overwrite canonical assembly')
    original = read(source / 'manifest.json')
    collection = read(source / 'protocol.json')
    resolved = {}
    for text, digest in collection['input_sha256'].items():
        path = Path(text)
        snapshot = source / 'source-snapshot' / path.relative_to(source.parents[3]) if path.suffix in ('.py', '.cpp', '.hpp', '.ps1') else path
        actual = path if sha(path) == digest else snapshot
        require(sha(actual) == digest, 'Original collection input changed: ' + text)
        resolved[text] = {'path': str(actual), 'sha256': digest}
    output.mkdir(parents=True)
    kept, removed = {}, []
    for part in ('train', 'tuning'):
        existing = lines(source / ('pairs-' + part + '.jsonl'))
        keys = {(r['root_id'], tuple(sorted((r['move_a'], r['move_b'])))) for r in existing}
        kept[part] = []
        for row in lines(source / ('pairs-backed-' + part + '.jsonl')):
            key = (row['root_id'], tuple(sorted((row['move_a'], row['move_b']))))
            if key in keys:
                removed.append({'partition': part, 'root_id': key[0], 'moves': list(key[1])})
                continue
            keys.add(key); kept[part].append(row)
        jsonl(output / ('pairs-backed-' + part + '.jsonl'), kept[part])
    require(len(removed) == 3 and all(r['partition'] == 'train' for r in removed), 'Unexpected duplicate correction')
    for name in (*original['files'], *original['pair_files']): shutil.copyfile(source / name, output / name)
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'assembly_only': True,
                'original_collection_protocol': {'path': str(source / 'protocol.json'), 'sha256': sha(source / 'protocol.json')},
                'original_collection_manifest': {'path': str(source / 'manifest.json'), 'sha256': sha(source / 'manifest.json')},
                'new_teacher_queries': 0, 'new_native_searches': 0, 'new_training_runs': 0,
                'removed_comparisons': removed, 'old_data_and_labels_unchanged': True,
                'historical_input_resolution': resolved,
                'sources': {str(p): sha(p) for p in (Path(__file__).resolve(), Path(__file__).with_name('prepare_nnue_choice.py').resolve())}}
    write(output / 'protocol.json', protocol)
    manifest = dict(original)
    manifest.update(source_protocol={'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
                    collection_source=str(source),
                    backed_pair_files={'pairs-backed-' + p + '.jsonl': sha(output / ('pairs-backed-' + p + '.jsonl')) for p in kept},
                    backed_pair_rows={p: len(v) for p, v in kept.items()},
                    combined_pair_rows={p: len(v) + original['pair_rows'][p] for p, v in kept.items()},
                    duplicate_old_comparisons_removed=3)
    write(output / 'manifest.json', manifest)
    _, rows, _ = load_bank(output); load_pairs(output, rows, manifest)
    write(output / 'assembly-receipt.json', {'completed': True, 'original_receipts_preserved': True,
        'new_teacher_queries': 0, 'new_native_searches': 0, 'removed': removed,
        'canonical_manifest_sha256': sha(output / 'manifest.json')})
    print('Canonical cached assembly: new29train/8tuning, combined55/24, no repeated search or teacher calls.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path); parser.add_argument('output', type=Path)
    args = parser.parse_args(); finalize(args.source.resolve(), args.output.resolve())
