"""Read-only representation checks on the frozen diversity model and old losses.

This never invokes an engine, teacher, optimizer or game. Material-channel
labels refer to the six material-initialized hidden channels, not an exact
separation of all learned material and positional knowledge.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
import json
from pathlib import Path
import sys

import chess
import numpy as np

import neural_stm as stm
from prepare_neural_huber import read, sha, write
from train_neural_stm import ROOT


def diagnose(output: Path):
    source = ROOT / 'docs/checks/2026-10-22-nnue-diversity'
    attempt, dataset = source / 'attempt-001', source / 'data-001'
    model_path, float_path = attempt / 'model.nnue', attempt / 'float_weights.npz'
    game_path = attempt / 'games.json'
    partitions = [dataset / (name + '.jsonl') for name in ('train', 'tuning', 'test')]
    model = stm.load_model(model_path)
    with np.load(float_path, allow_pickle=False) as archive:
        parameters = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    rows = [json.loads(line) for path in partitions for line in path.read_text(encoding='utf-8').splitlines()]
    indices, targets, turns = stm.pack_records(rows)
    predictions, cache = stm._integer_forward(model, indices, turns)
    float_predictions = stm._float_predictions(parameters, indices, turns, True)
    difference = np.abs(predictions - float_predictions)
    sums = cache['sums']
    bins = {}
    for name, mask in (('abs_teacher_below_100cp', abs(targets) < 100),
                       ('abs_teacher_100_to_399cp', (abs(targets) >= 100) & (abs(targets) < 400)),
                       ('abs_teacher_400cp_or_more', abs(targets) >= 400)):
        bins[name] = {'count': int(mask.sum()),
                      'mae_cp': float(np.mean(abs(predictions[mask] - targets[mask]))),
                      'opposite_nonzero_sign_count': int(((predictions * targets < 0) & mask).sum())}
    sampled = rows[::10]
    original, _, original_turns = stm.pack_records(sampled)
    mirrored, _, mirror_turns = stm.pack_records([
        {'fen': chess.Board(row['fen']).mirror().fen(), 'target_white_cp': 0} for row in sampled])
    mirror_errors = stm.predict(model, original, original_turns) + stm.predict(model, mirrored, mirror_turns)
    games = read(game_path)['games']
    loss_records = [{'fen': record['fen_before'], 'target_white_cp': 0}
                    for game in games if game['candidate_points'] == 0
                    for record in game['search_moves'] if record['role'] == 'candidate']
    loss_indices, _, loss_turns = stm.pack_records(loss_records)
    _, loss_cache = stm._integer_forward(model, loss_indices, loss_turns)
    channels = {}
    for name, section in (('first_six_material_initialized_channels', slice(0, 6)),
                          ('remaining_ten_channels', slice(6, None))):
        contributions = loss_cache['differences'][:, section] @ model['output_weights'][section] / model['output_scale']
        channels[name] = {'mean_absolute_cp': float(np.mean(abs(contributions))),
                          'median_absolute_cp': float(np.median(abs(contributions))),
                          'maximum_absolute_cp': float(np.max(abs(contributions)))}
    search = {}
    for role in ('baseline', 'candidate'):
        records = [record for game in games for record in game['search_moves'] if record['role'] == role]
        search[role] = {'moves': len(records), 'reported_nodes': sum(record['nodes'] for record in records),
                        'elapsed_seconds': sum(record['elapsed_seconds'] for record in records),
                        'mean_depth': float(np.mean([record['depth'] for record in records]))}
    inputs = [Path(__file__), ROOT / 'scripts/neural_stm.py', ROOT / 'scripts/neural_precision.py',
              ROOT / 'scripts/neural_training.py', ROOT / 'scripts/neural_qat.py',
              model_path, float_path, game_path, *partitions]
    result = {'schema_version': 1, 'read_only': True, 'engines_invoked': 0, 'trainings': 0,
              'model_sha256': sha(model_path), 'positions': len(rows), 'target_bins': bins,
              'integer_float_absolute_difference_cp': {'mean': float(np.mean(difference)), 'maximum': float(np.max(difference))},
              'hidden_activation': {'zero_or_below_percent': float(np.mean(sums <= 0) * 100),
                                    'upper_saturated_percent': float(np.mean(sums >= model['clip']) * 100)},
              'mirror_symmetry': {'sampled_positions': len(sampled),
                                  'maximum_absolute_score_sum_cp': float(np.max(abs(mirror_errors)))},
              'loss_game_candidate_positions': len(loss_records), 'loss_channel_contributions': channels,
              'prior_match_search': search,
              'interpretation_limit': 'Frozen development positions, including previously exposed test labels. Channel groups describe initialization and are not a causal decomposition. Static error and search-depth aggregates do not prove a loss cause. This diagnostic does not alter the experimental plan or model selection.',
              'environment': {'python': sys.version, 'numpy': np.__version__},
              'input_sha256': {str(path): sha(path) for path in inputs}}
    output.parent.mkdir(parents=True, exist_ok=True)
    write(output, result)
    print(json.dumps({'output': str(output), 'positions': len(rows), 'loss_positions': len(loss_records)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    diagnose(args.output.resolve())
