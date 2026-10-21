"""Train-only warm-output preservation for the unchanged point/pair MSE NNUE.

A frozen warm network supplies preservation targets for OLD TRAIN positions.
Those targets are integer inference values, never teacher/held-out labels.
Point MSE, sibling-gap MSE, STE, Adam, clipping, and guarded checkpoint selection
retain their existing meanings.  This module is isolated from the prior trainer.
"""
from __future__ import annotations

import hashlib
import math

import numpy as np

import neural_precision as precision
import neural_qat as qat
import neural_stm as stm
import neural_training as base
import neural_stm_ordering as ordering


# Reuse the prior, validated pair handling and deterministic family scheduling.
_copy = ordering._copy
_validate_pairs = ordering._validate_pairs
_slice_pairs = ordering._slice_pairs
_validate_pair_families = ordering._validate_pair_families
_family_pair_batches = ordering._family_pair_batches
pair_metrics = ordering.pair_metrics


def _preservation_weight(value):
    if (isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError('preservation_weight must be finite and nonnegative')
    return float(value)


def _validate_training_subset(indices, turns, preservation_indices, preservation_turns):
    """Only actual training inputs may supply frozen warm preservation targets."""
    qat._validate_batch(preservation_indices)
    stm._turn_mask(preservation_turns, len(preservation_indices))
    keys = {(np.asarray(row, dtype=np.int64).tobytes(), int(turn))
            for row, turn in zip(indices, turns)}
    preservation_keys = [(np.asarray(row, dtype=np.int64).tobytes(), int(turn))
                         for row, turn in zip(preservation_indices, preservation_turns)]
    if len(set(preservation_keys)) != len(preservation_keys):
        raise ValueError('Preservation positions and turns must be unique')
    if any(key not in keys for key in preservation_keys):
        raise ValueError('Preservation positions and turns must be a TRAIN subset')


def preservation_metrics(parameters, indices, targets, turns, input_scale=32,
                         *, enable_turn=True):
    predicted, _ = stm.qat_forward(parameters, indices, input_scale,
                                  turns=turns, enable_turn=enable_turn)
    residual = predicted - targets
    return {**base.error_metrics(predicted, targets), 'positions': len(indices),
            'integer_scores_changed': int(np.count_nonzero(residual)),
            'maximum_absolute_drift_cp': float(np.max(np.abs(residual)))}


def qat_gradients(parameters, indices, targets, input_scale=32, *, turns, pairs,
                  preservation_indices, preservation_targets, preservation_turns,
                  preservation_weight=4.0, enable_turn=True):
    """Point MSE + 1*gap MSE + lambda*warm-prediction preservation MSE.

    Each mean has its own sample count.  All terms reuse the deployed-integer
    forward pass and existing approximate STE; this does not differentiate
    the discontinuous integer rounding operation mathematically.
    """
    weight = _preservation_weight(preservation_weight)
    qat._validate_batch(preservation_indices, preservation_targets)
    stm._turn_mask(preservation_turns, len(preservation_indices), enable_turn)
    result = ordering.qat_gradients(parameters, indices, targets, input_scale,
                                    turns=turns, pairs=pairs, enable_turn=enable_turn)
    if weight == 0:
        return result
    preservation = stm.qat_gradients(parameters, preservation_indices,
                                     preservation_targets, input_scale,
                                     turns=preservation_turns, enable_turn=enable_turn)
    for key in stm.PARAMETER_KEYS:
        result[key] += weight * preservation[key]
        if not np.all(np.isfinite(result[key])):
            raise ValueError('Nonfinite preservation MSE gradient')
    return result


def train_candidate(initial_parameters, indices, targets, config, *, turns, pairs,
                    validation_indices, validation_targets, validation_turns,
                    validation_pairs, anchor_indices, anchor_targets, anchor_turns,
                    patience=5, min_delta_cp2=1.0, eval_interval=5,
                    progress=None, enable_turn=True, pair_family_ids=None,
                    preservation_indices, preservation_turns, preservation_weight=4.0):
    """Return one validation-selected checkpoint, retaining the epoch-zero oracle.

    Selection minimizes integer point MSE + integer pair-gap MSE, subject to
    old-distribution anchor MSE never exceeding its epoch-zero value. An
    ineligible checkpoint cannot improve either best copy or patience state.
    """
    stm._validate_parameters(initial_parameters)
    preservation_weight = _preservation_weight(preservation_weight)
    precision._validate_config(config)
    if config.get('loss', 'mse') != 'mse':
        raise ValueError('Ordering trainer keeps MSE fixed')
    if type(enable_turn) is not bool:
        raise ValueError('enable_turn must be bool')
    if type(patience) is not int or patience < 1:
        raise ValueError('patience must be a positive integer')
    if type(eval_interval) is not int or not 1 <= eval_interval <= config['epochs']:
        raise ValueError('eval_interval must be within the epoch budget')
    if (isinstance(min_delta_cp2, (bool, np.bool_))
            or not isinstance(min_delta_cp2, (int, float))
            or not math.isfinite(min_delta_cp2) or min_delta_cp2 < 0):
        raise ValueError('min_delta_cp2 must be finite and nonnegative')
    if progress is not None and not callable(progress):
        raise ValueError('progress must be callable or None')
    for ix, target, side in ((indices, targets, turns),
                             (validation_indices, validation_targets, validation_turns),
                             (anchor_indices, anchor_targets, anchor_turns)):
        qat._validate_batch(ix, target)
        stm._turn_mask(side, len(ix), enable_turn)
    pairs = _validate_pairs(pairs, enable_turn)
    validation_pairs = _validate_pairs(validation_pairs, enable_turn)
    point_batch_count = math.ceil(len(targets) / config['batch_size'])
    if pair_family_ids is not None:
        _validate_pair_families(pair_family_ids, len(pairs[0]), point_batch_count)
    turns = np.asarray(turns)
    _validate_training_subset(indices, turns, preservation_indices, preservation_turns)
    preservation_indices = preservation_indices.copy()
    preservation_turns = np.asarray(preservation_turns).copy()
    preservation_targets, _ = stm.qat_forward(initial_parameters, preservation_indices,
                                               config['input_scale'],
                                               turns=preservation_turns,
                                               enable_turn=enable_turn)
    preservation_targets = preservation_targets.copy()
    preservation_targets.setflags(write=False)
    bounds = stm._bounds(config['input_scale'])
    parameters = {key: value.astype(np.float64, copy=True)
                  for key, value in initial_parameters.items()}
    if any(np.any(np.abs(value) > bounds[key]) for key, value in parameters.items()):
        raise ValueError('Initial shadow parameter outside model bounds')

    def evaluate(values, epoch, anchor_limit=None):
        predicted, _ = stm.qat_forward(values, validation_indices, config['input_scale'],
                                       turns=validation_turns, enable_turn=enable_turn)
        point = base.error_metrics(predicted, validation_targets)
        pair = pair_metrics(values, validation_pairs, config['input_scale'],
                             enable_turn=enable_turn)
        anchor_predicted, _ = stm.qat_forward(values, anchor_indices, config['input_scale'],
                                              turns=anchor_turns, enable_turn=enable_turn)
        anchor = base.error_metrics(anchor_predicted, anchor_targets)
        objective = point['mse_cp2'] + pair['mse_cp2']
        evaluation = {'epoch': epoch, 'mse_cp2': objective, 'objective_cp2': objective,
                'point': point, 'pair': pair, 'anchor': anchor, 'pair_weight': 1.0,
                'anchor_limit_cp2': anchor['mse_cp2'] if anchor_limit is None else anchor_limit,
                'eligible': anchor_limit is None or anchor['mse_cp2'] <= anchor_limit}
        if preservation_weight:
            evaluation['preservation_train'] = preservation_metrics(
                values, preservation_indices, preservation_targets, preservation_turns,
                config['input_scale'], enable_turn=enable_turn)
        return evaluation

    initial = evaluate(parameters, 0)
    evaluations = [initial]
    anchor_limit = initial['anchor']['mse_cp2']
    best, best_evaluation = _copy(parameters), initial.copy()
    significant_best, stale, step, history = initial['mse_cp2'], 0, 0, []
    first = {key: np.zeros_like(value) for key, value in parameters.items()}
    second = {key: np.zeros_like(value) for key, value in parameters.items()}
    rates = {key: config['output_lr'] if key == 'output_weights' else config['input_bias_lr']
             for key in stm.PARAMETER_KEYS}
    rng = np.random.default_rng(config['seed'])
    reason = 'max_epochs'
    observed_pairs_per_epoch = []
    for epoch in range(1, config['epochs'] + 1):
        order = rng.permutation(len(targets))
        if pair_family_ids is None:
            pair_order = rng.permutation(len(pairs[0]))
            pair_batches = [pair_order[o:o + config['batch_size']]
                            for o in range(0, len(pair_order), config['batch_size'])]
        else:
            pair_batches = _family_pair_batches(pair_family_ids, point_batch_count, rng)
        maximum_norm, clipped_batches = 0.0, 0
        observed_pairs = 0
        for batch_no, offset in enumerate(range(0, len(order), config['batch_size'])):
            chosen = order[offset:offset + config['batch_size']]
            pair_chosen = pair_batches[batch_no % len(pair_batches)]
            observed_pairs += len(pair_chosen)
            gradients = qat_gradients(parameters, indices[chosen], targets[chosen],
                                       config['input_scale'], turns=turns[chosen],
                                       pairs=_slice_pairs(pairs, pair_chosen),
                                       preservation_indices=preservation_indices,
                                       preservation_targets=preservation_targets,
                                       preservation_turns=preservation_turns,
                                       preservation_weight=preservation_weight,
                                       enable_turn=enable_turn)
            norm = math.sqrt(sum(float(np.sum(value * value)) for value in gradients.values()))
            maximum_norm = max(maximum_norm, norm)
            clipped_batches += int(norm > 10)
            factor, step = min(1.0, 10.0 / max(norm, 1e-30)), step + 1
            for key, parameter in parameters.items():
                gradient = gradients[key] * factor
                first[key] *= 0.9
                first[key] += 0.1 * gradient
                second[key] *= 0.999
                second[key] += 0.001 * gradient * gradient
                estimate = ((first[key] / (1 - 0.9 ** step))
                            / (np.sqrt(second[key] / (1 - 0.999 ** step)) + 1e-8))
                parameter -= rates[key] * estimate
                np.clip(parameter, -bounds[key], bounds[key], out=parameter)
                if not np.all(np.isfinite(parameter)):
                    raise ValueError('Nonfinite fixed-point QAT shadow')
        observed_pairs_per_epoch.append(observed_pairs)
        predictions, _ = stm.qat_forward(parameters, indices, config['input_scale'],
                                         turns=turns, enable_turn=enable_turn)
        history.append({'epoch': epoch, 'optimizer_steps': step,
                        'fake_quantized_train': base.error_metrics(predictions, targets),
                        'pair_train': pair_metrics(parameters, pairs, config['input_scale'],
                                                   enable_turn=enable_turn),
                        'shadow_float_train': base.error_metrics(
                            stm._float_predictions(parameters, indices, turns, enable_turn), targets),
                        'max_batch_gradient_global_norm': maximum_norm,
                        'gradient_clipped_batches': clipped_batches})
        if preservation_weight:
            history[-1]['preservation_train'] = preservation_metrics(
                parameters, preservation_indices, preservation_targets,
                preservation_turns, config['input_scale'], enable_turn=enable_turn)
        if epoch % eval_interval != 0 and epoch != config['epochs']:
            continue
        current = evaluate(parameters, epoch, anchor_limit)
        evaluations.append(current)
        if current['eligible'] and current['mse_cp2'] < best_evaluation['mse_cp2']:
            best, best_evaluation = _copy(parameters), current.copy()
        if current['eligible'] and current['mse_cp2'] < significant_best - min_delta_cp2:
            significant_best, stale = current['mse_cp2'], 0
        else:
            stale += 1
        if progress is not None:
            import copy
            progress(epoch, _copy(parameters), copy.deepcopy(current))
        if stale >= patience:
            reason = 'patience_exhausted'
            break
    selection = {
        'loss': 'mse', 'objective': 'held_out_integer_point_mse_plus_pair_gap_mse',
        'pair_weight': 1.0, 'mode': 'validation_mse_early_stopping_with_anchor',
        'selected_epoch': best_evaluation['epoch'], 'stopped_epoch': history[-1]['epoch'],
        'stopped_early': history[-1]['epoch'] < config['epochs'], 'stop_reason': reason,
        'max_epochs': config['epochs'], 'optimizer_steps': step,
        'patience_evaluations': patience, 'min_delta_cp2': float(min_delta_cp2),
        'eval_interval_epochs': eval_interval, 'initial_validation': initial,
        'selected_validation': best_evaluation, 'last_validation': evaluations[-1],
        'validation_evaluations': evaluations, 'validation_used_in_gradients': False,
        'validation_pairs_used_in_gradients': False, 'anchor_used_in_gradients': False,
        'anchor_limit_cp2': anchor_limit, 'anchor_policy': 'MSE <= epoch-zero anchor MSE',
        'pair_batch_policy': 'Seeded pair permutation each epoch; cycle pair batches per point batch',
    }
    if preservation_weight:
        selection.update({
            'preservation_weight': preservation_weight,
            'preservation_targets': {
                'source': 'Frozen epoch-zero deployed-integer white-CP outputs on old TRAIN inputs only',
                'positions': len(preservation_indices),
                'sha256_float64_le': hashlib.sha256(
                    preservation_targets.astype('<f8').tobytes()).hexdigest(),
                'batch_policy': 'Full old-TRAIN preservation mean in every point/pair minibatch',
                'selected_drift': preservation_metrics(
                    best, preservation_indices, preservation_targets, preservation_turns,
                    config['input_scale'], enable_turn=enable_turn)},
            'preservation_inputs_verified_training_subset': True,
            'preservation_targets_used_in_selection': False})
    if pair_family_ids is not None:
        selection['pair_batch_policy'] = ('Shuffle each family and family order with the training seed; '
                                          'round-robin families, split across point batches; each pair once per epoch')
        selection['observed_pairs_per_epoch'] = observed_pairs_per_epoch
        selection['pair_family_counts'] = {family: pair_family_ids.count(family)
                                            for family in sorted(set(pair_family_ids))}
    return best, history, selection
