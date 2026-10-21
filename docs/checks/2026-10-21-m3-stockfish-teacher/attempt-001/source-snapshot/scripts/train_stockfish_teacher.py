"""One bounded Stockfish-teacher fine-tune of the existing MSE STM network.

No new games, architecture search, validation-based selection, or promotion.
The existing frozen positions are reused; only their offline teacher changes.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import chess
import numpy as np

import neural_stm as stm
import neural_target_ab_data as data
import neural_training as base
from prepare_neural_huber import read, require, sha, special_positions, write
from stockfish_teacher import label_rows
from train_neural_stm import ROOT, SOURCE

WARM = ROOT / 'docs/checks/2026-10-21-m3-nnue-stm/attempt-001'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'
COUNTS = {'train': 512, 'validation': 128}
CONFIG = {'candidate_id': 'sf18-mse-stm-small-20261020', 'input_scale': 32, 'epochs': 500,
          'input_bias_lr': .0002, 'output_lr': .005, 'seed': 20261020, 'batch_size': 128, 'loss': 'mse'}


def train(output):
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=False)
    profile = read(PROFILE)
    teacher = Path(profile['binary']['path'])
    require(sha(teacher) == profile['binary']['sha256'], 'Frozen Stockfish binary differs')
    require(sha(WARM / 'model.nnue') == 'f001f1d3bd9b710e046d37c88040c13a90e8507b28bfb2c416814b9427332f26',
            'Current STM checkpoint differs')
    loaded = data.load_dataset(SOURCE / 'trainval')
    generator = np.random.default_rng(20261021)
    selected = {p: [loaded[p][int(i)] for i in generator.permutation(len(loaded[p]))[:count]]
                for p, count in COUNTS.items()}
    require(not ({r['family_id'] for r in selected['train']} & {r['family_id'] for r in selected['validation']}),
            'Source families cross the training partition')
    require(not ({r['feature_hash'] for r in selected['train']} & {r['feature_hash'] for r in selected['validation']}),
            'Piece inputs cross the training partition')
    with np.load(WARM / 'float_weights.npz', allow_pickle=False) as archive:
        initial = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    require(stm.model_checksum(stm.quantize(initial, 32)) == stm.model_checksum(stm.load_model(WARM / 'model.nnue')),
            'Warm float checkpoint does not produce the existing network')
    sources = [Path(__file__), ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_stm.py',
               ROOT / 'scripts/neural_precision.py', ROOT / 'scripts/neural_training.py',
               ROOT / 'scripts/neural_qat.py', ROOT / 'scripts/neural_target_ab_data.py',
               ROOT / 'scripts/neural_search_data/schema.py']
    inputs = sources + [PROFILE, teacher, WARM / 'float_weights.npz', WARM / 'model.nnue',
                        WARM / 'validation.json', ROOT / 'build-m3-stm/regicide.exe',
                        ROOT / 'config/engine-profiles/classical.json']
    inputs += [SOURCE / 'trainval' / name for name in ('manifest.json', 'train.jsonl', 'validation.jsonl')]
    inputs += [Path(item['path']) for item in profile['artifacts']]
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'config': CONFIG,
                'architecture': {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                                 'hidden': 16, 'added_parameters': 0, 'changed': False},
                'initial': {'kind': 'fine-tune current STM float checkpoint', 'path': str(WARM / 'float_weights.npz'),
                            'sha256': sha(WARM / 'float_weights.npz'), 'model_sha256': sha(WARM / 'model.nnue')},
                'sampling': {'seed': 20261021, 'requested': COUNTS, 'selected_before_teacher': True,
                             'replacement_after_rejection': False, 'selection': 'Seeded permutation of each frozen partition',
                             'holdout_read': False, 'minimum_accepted': {'train': 256, 'validation': 64}},
                'teacher': {'profile': profile, 'requested_nodes': 10000, 'max_abs_cp': 1500,
                            'scores': 'Stockfish normalized UCI cp, white POV; no calibration to material pawn units',
                            'excluded': ['terminal', 'mate score', 'bounded score', 'invalid/missing score or PV', 'abs(cp)>1500'],
                            'role': 'Separate unmodified GPL-3.0-or-later executable, used only to generate numerical labels',
                            'source_url': profile['source']['source_url'], 'license_url': profile['source']['license_url']},
                'training': {'candidate_runs': 1, 'selection': 'Fixed last epoch 500; no validation selection or sweep',
                             'optimizer': 'Existing MSE STE Adam, beta .9/.999 epsilon1e-8, norm cap10',
                             'learning_rate_reason': 'Small fixed set uses 500 epochs and input rate .0002 to allow useful fine-tuning; no causal label-only claim'},
                'comparison': 'Basic classical Regicide versus this candidate, same search options, 8 starts x both colors, 100ms, exactly16games',
                'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'environment': {'python': sys.version, 'numpy': np.__version__, 'chess': chess.__version__, 'blas_threads': 1},
                'input_sha256': {str(path): sha(path) for path in inputs}, 'default_changed': False}
    write(output / 'protocol.json', protocol)
    write(output / 'selected.json', selected)
    shutil.copyfile(WARM / 'float_weights.npz', output / 'initial-floats.npz')
    for source in sources:
        target = output / 'source-snapshot' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    print('Generating at most 640 Stockfish labels, 10000 nodes each.', flush=True)
    accepted, teacher_report = label_rows(teacher, selected['train'] + selected['validation'], output / 'teacher',
                                          nodes=10000, max_abs_cp=1500)
    partitions = {p: [row for row in accepted if row['partition'] == p] for p in COUNTS}
    require(all(len(partitions[p]) >= protocol['sampling']['minimum_accepted'][p] for p in COUNTS),
            'Too few accepted labels; preserve this attempt without replacing samples')
    packed = {p: stm.pack_records(rows) for p, rows in partitions.items()}
    print('Fine-tuning one fixed MSE candidate.', flush=True)
    tick = time.monotonic()
    parameters, history = stm.train_candidate(initial, packed['train'][0], packed['train'][1], CONFIG,
                                               turns=packed['train'][2])
    learning_seconds = time.monotonic() - tick
    np.savez(output / 'float_weights.npz', **parameters)
    stm.save_integer_model(output / 'model.nnue', parameters, 32)
    model = stm.load_model(output / 'model.nnue')
    before = stm.quantize(initial, 32)
    metrics = {p: {'count': len(rows), 'before': base.error_metrics(stm.predict(before, packed[p][0], packed[p][2]), packed[p][1]),
                   'after': base.error_metrics(stm.predict(model, packed[p][0], packed[p][2]), packed[p][1]),
                   'kinds': dict(Counter(row['kind'] for row in rows))}
               for p, rows in partitions.items()}
    report = {'config': CONFIG, 'teacher_seconds': teacher_report.get('wall_seconds'),
              'training_wall_seconds': learning_seconds, 'total_wall_seconds': time.monotonic() - started,
              'metrics': metrics, 'model_sha256': sha(output / 'model.nnue'),
              'history': history, 'architecture_unchanged': True, 'loss': 'mse', 'candidate_runs': 1,
              'parameter_movement': {key: {'changed_integer_count': int(np.count_nonzero(model[key] != before[key])),
                                           'max_abs_shadow_change': float(np.max(np.abs(parameters[key] - initial[key])))}
                                    for key in stm.PARAMETER_KEYS},
              'interpretation': 'Teacher imitation errors and match strength are separate; validation is observational.'}
    write(output / 'training.json', report)
    # New weights require parity checks; unchanged legacy/native regressions are reused by hash.
    specials = special_positions()
    fens = [row['fen'] for row in accepted] + specials
    fixture_rows = [{'fen': fen, 'target_white_cp': 0} for fen in specials]
    indices, _, turns = stm.pack_records(accepted + fixture_rows)
    expected = stm.predict(model, indices, turns) * np.where(turns == 0, 1, -1)
    engine = ROOT / 'build-m3-stm/regicide.exe'
    values = base.evaluate_engine(engine, fens, output / 'model.nnue')
    require(np.array_equal(expected, [r[1] for r in values]), 'New teacher model Python/native full/incremental mismatch')
    tester = engine.parent / ('regicide_neural_model_tests' + engine.suffix)
    checked = subprocess.run([str(tester), str(output / 'model.nnue')], text=True, capture_output=True, timeout=120)
    (output / 'native-model-tests.txt').write_text(checked.stdout + checked.stderr, encoding='utf-8')
    require(checked.returncode == 0 and '0 failures' in checked.stdout, 'Actual trained model regression failed')
    prior = read(WARM / 'validation.json')
    require(sha(engine) == prior['input_sha256'][str(engine)], 'Native binary changed after the full legacy compatibility check')
    validation = {'positions': len(fens), 'corpus_positions': len(accepted), 'special_positions': len(specials),
                  'python_native_full_incremental_mismatches': 0, 'native_model_returncode': checked.returncode,
                  'native_model_summary': checked.stdout.splitlines()[-1],
                  'reused_legacy_validation': {'path': str(WARM / 'validation.json'), 'sha256': sha(WARM / 'validation.json')},
                  'input_sha256': {str(path): sha(path) for path in (engine, tester, output / 'model.nnue')}}
    write(output / 'validation.json', validation)
    print(json.dumps({key: report[key] for key in ('training_wall_seconds', 'metrics', 'model_sha256')}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    train(parser.parse_args().output.resolve())
