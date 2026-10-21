"""One MSE fine-tune with validation stopping and best integer checkpoint restore.

Reuse the prior Stockfish labels and pre-failure initializer.  Only the training
guard changes.  The exposed 81-row validation set is now explicitly a tuning
set; it is not a new independent test.  This command never runs matches.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import numpy as np

import neural_stm as stm
import neural_stm_guarded as guarded
import neural_training as base
from prepare_neural_huber import read, require, sha, special_positions, write
from train_neural_stm import ROOT

PRIOR = ROOT / 'docs/checks/2026-10-21-m3-stockfish-teacher/attempt-001'
WARM = ROOT / 'docs/checks/2026-10-21-m3-nnue-stm/attempt-001'
DIAGNOSIS = ROOT / 'docs/checks/2026-10-22-nnue-overfit/diagnosis/diagnosis.json'
GUARDS = {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}


def train(output):
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=False)
    prior_protocol = read(PRIOR / 'protocol.json')
    config = dict(prior_protocol['config'], candidate_id='sf18-mse-stm-guarded-20261020')
    label_path = PRIOR / 'teacher/labels.jsonl'
    require(sha(label_path) == '2117ece8b2671906d4f754f9d907751df59b8404904571db9df791e1841d0fc3',
            'Frozen Stockfish labels differ')
    rows = [json.loads(line) for line in label_path.read_text(encoding='utf-8').splitlines()]
    partitions = {p: [row for row in rows if row['partition'] == p] for p in ('train', 'validation')}
    require([len(partitions[p]) for p in partitions] == [350, 81], 'Unexpected fixed partitions')
    for key in ('family_id', 'feature_hash'):
        require(not ({r[key] for r in partitions['train']} & {r[key] for r in partitions['validation']}),
                'Source family or piece inputs cross partitions')
    with np.load(WARM / 'float_weights.npz', allow_pickle=False) as archive:
        initial = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    require(stm.model_checksum(stm.quantize(initial, 32)) == stm.model_checksum(stm.load_model(WARM / 'model.nnue')),
            'Pre-failure warm floats differ from the warm model')
    packed = {p: stm.pack_records(records) for p, records in partitions.items()}
    sources = [Path(__file__), ROOT / 'scripts/neural_stm_guarded.py', ROOT / 'scripts/neural_stm.py',
               ROOT / 'scripts/neural_precision.py', ROOT / 'scripts/neural_training.py', ROOT / 'scripts/neural_qat.py']
    inputs = sources + [label_path, PRIOR / 'protocol.json', PRIOR / 'training.json', PRIOR / 'model.nnue',
                        PRIOR / 'teacher/teacher-report.json', WARM / 'float_weights.npz', WARM / 'model.nnue',
                        WARM / 'validation.json', DIAGNOSIS, ROOT / 'build-m3-stm/regicide.exe',
                        ROOT / 'config/engine-profiles/classical.json']
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'config': config, 'guards': GUARDS,
                'architecture': {'format': 3, 'piece_features': 768, 'relative_turn_features': 1, 'hidden': 16,
                                 'changed': False, 'added_parameters': 0},
                'change': 'Validation-based early stop and restoration of best integer MSE checkpoint, including epoch0',
                'initial': {'path': str(WARM / 'float_weights.npz'), 'sha256': sha(WARM / 'float_weights.npz'),
                            'model_sha256': sha(WARM / 'model.nnue'), 'failed_model_used_as_initial': False},
                'data': {'labels_path': str(label_path), 'labels_sha256': sha(label_path), 'train_rows': 350,
                         'tuning_rows': 81, 'fresh_independent_validation': False,
                         'policy': 'Previously exposed validation is used only for stopping/checkpoint selection, never Adam gradients',
                         'new_teacher_labels': 0},
                'prior_failure': {'path': str(PRIOR / 'model.nnue'), 'sha256': sha(PRIOR / 'model.nnue'),
                                  'training': str(PRIOR / 'training.json')},
                'selection': 'One run, fixed seed/order/learning rates and maximum500 epochs; minimum tuning integer MSE, earliest tie',
                'comparison': 'Basic classical Regicide, same8starts bothcolors100ms, exactly16games; no match-based selection',
                'diagnosis': {'path': str(DIAGNOSIS), 'sha256': sha(DIAGNOSIS)},
                'environment': {'python': sys.version, 'numpy': np.__version__, 'blas_threads': 1},
                'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'input_sha256': {str(path): sha(path) for path in inputs}, 'default_changed': False}
    write(output / 'protocol.json', protocol)
    shutil.copyfile(WARM / 'float_weights.npz', output / 'initial-floats.npz')
    for source in sources:
        target = output / 'source-snapshot' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    tick = time.monotonic()
    parameters, history, selection = guarded.train_candidate(
        initial, packed['train'][0], packed['train'][1], config, turns=packed['train'][2],
        validation_indices=packed['validation'][0], validation_targets=packed['validation'][1],
        validation_turns=packed['validation'][2], **GUARDS)
    learning_seconds = time.monotonic() - tick
    np.savez(output / 'float_weights.npz', **parameters)
    stm.save_integer_model(output / 'model.nnue', parameters, 32)
    model = stm.load_model(output / 'model.nnue')
    warm, failed = stm.load_model(WARM / 'model.nnue'), stm.load_model(PRIOR / 'model.nnue')
    metrics = {}
    for partition, (indices, targets, turns) in packed.items():
        metrics['train' if partition == 'train' else 'tuning'] = {
            'count': len(targets), **{name: base.error_metrics(stm.predict(net, indices, turns), targets)
                                     for name, net in (('warm', warm), ('failed_500', failed), ('guarded', model))}}
    require(metrics['tuning']['guarded']['mse_cp2'] <= metrics['tuning']['warm']['mse_cp2'],
            'Best restore violated epoch-zero protection')
    write(output / 'selection.json', selection)
    report = {'config': config, 'guards': GUARDS, 'candidate_runs': 1, 'training_wall_seconds': learning_seconds,
              'selection': selection, 'metrics': metrics, 'model_sha256': sha(output / 'model.nnue'),
              'model_changed_from_warm': stm.model_checksum(model) != stm.model_checksum(warm),
              'architecture_unchanged': True, 'loss': 'mse', 'history': history,
              'interpretation': 'Tuning error is used for selection, not fresh test evidence or Elo.'}
    write(output / 'training.json', report)
    specials = special_positions()
    fens = [row['fen'] for row in rows] + specials
    indices, _, turns = stm.pack_records(rows + [{'fen': fen, 'target_white_cp': 0} for fen in specials])
    expected = stm.predict(model, indices, turns) * np.where(turns == 0, 1, -1)
    engine = ROOT / 'build-m3-stm/regicide.exe'
    values = base.evaluate_engine(engine, fens, output / 'model.nnue')
    require(np.array_equal(expected, [value[1] for value in values]), 'New model Python/native full/incremental mismatch')
    tester = engine.parent / ('regicide_neural_model_tests' + engine.suffix)
    checked = subprocess.run([str(tester), str(output / 'model.nnue')], capture_output=True, text=True, timeout=120)
    (output / 'native-model-tests.txt').write_text(checked.stdout + checked.stderr, encoding='utf-8')
    require(checked.returncode == 0 and '0 failures' in checked.stdout, 'Actual guarded model regressions failed')
    cached = read(WARM / 'validation.json')
    require(sha(engine) == cached['input_sha256'][str(engine)], 'Unchanged native executable identity differs')
    validation = {'positions': len(fens), 'corpus_positions': len(rows), 'special_positions': len(specials),
                  'python_native_full_incremental_mismatches': 0, 'native_model_returncode': checked.returncode,
                  'native_model_summary': checked.stdout.splitlines()[-1],
                  'reused_legacy_validation': {'path': str(WARM / 'validation.json'), 'sha256': sha(WARM / 'validation.json')},
                  'input_sha256': {str(path): sha(path) for path in (engine, tester, output / 'model.nnue')}}
    write(output / 'validation.json', validation)
    print(json.dumps({'training_seconds': learning_seconds, 'selection': selection,
                      'metrics': metrics, 'model_sha256': report['model_sha256']}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    train(parser.parse_args().output.resolve())
