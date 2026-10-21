"""Fixed-topology QAT with point MSE plus same-root score-difference MSE.

All scores and targets are white centipawns. Pair ordering is an observation,
not a different neural output or a classification loss. No validation row is
ever sent to the gradient function. The production pair weight is fixed at one.
"""
from __future__ import annotations

import math

import numpy as np

import neural_precision as precision
import neural_qat as qat
import neural_stm as stm
import neural_training as base


def _copy(parameters):
    return {key: value.copy() for key, value in parameters.items()}


def _validate_pairs(pairs, enable_turn=True):
    if not isinstance(pairs, (tuple, list)) or len(pairs) != 6:
        raise ValueError('Pairs must contain indices, targets, turns for both children')
    ia, ta, sa, ib, tb, sb = pairs
    qat._validate_batch(ia, ta)
    qat._validate_batch(ib, tb)
    if len(ia) != len(ib):
        raise ValueError('Both sides of each pair must have the same length')
    stm._turn_mask(sa, len(ia), enable_turn)
    stm._turn_mask(sb, len(ib), enable_turn)
    return (ia, np.asarray(ta), np.asarray(sa), ib, np.asarray(tb), np.asarray(sb))


def _weight(value):
    if (isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError('pair_weight must be finite and nonnegative')
    return float(value)


def _slice_pairs(pairs, chosen):
    return tuple(array[chosen] for array in pairs)


def pair_metrics(parameters, pairs, input_scale=32, *, enable_turn=True):
    ia, ta, sa, ib, tb, sb = _validate_pairs(pairs, enable_turn)
    pa, _ = stm.qat_forward(parameters, ia, input_scale, turns=sa, enable_turn=enable_turn)
    pb, _ = stm.qat_forward(parameters, ib, input_scale, turns=sb, enable_turn=enable_turn)
    target = ta - tb
    actual = pa - pb
    return {**base.error_metrics(actual, target), 'pairs': len(ia),
            'strict_order_matches': int(np.sum(actual * target > 0)),
            'target_ties': int(np.sum(target == 0)),
            'predicted_ties': int(np.sum(actual == 0))}


def qat_gradients(parameters, indices, targets, input_scale=32, *, turns, pairs,
                  pair_weight=1.0, enable_turn=True):
    """Reuse exactly the existing STE for point and pair MSE derivatives.

    A pseudo target p_a-r produces +2r/(P*10000); p_b+r produces
    -2r/(P*10000). Summing both gives the derivative of mean gap MSE.
    Weight zero exists only as a regression control for the legacy gradient.
    """
    weight = _weight(pair_weight)
    pairs = _validate_pairs(pairs, enable_turn)
    result = stm.qat_gradients(parameters, indices, targets, input_scale,
                               turns=turns, enable_turn=enable_turn)
    if weight == 0:
        return result
    ia, ta, sa, ib, tb, sb = pairs
    pa, _ = stm.qat_forward(parameters, ia, input_scale, turns=sa, enable_turn=enable_turn)
    pb, _ = stm.qat_forward(parameters, ib, input_scale, turns=sb, enable_turn=enable_turn)
    residual = (pa - pb) - (ta - tb)
    ga = stm.qat_gradients(parameters, ia, pa - residual, input_scale,
                           turns=sa, enable_turn=enable_turn)
    gb = stm.qat_gradients(parameters, ib, pb + residual, input_scale,
                           turns=sb, enable_turn=enable_turn)
    for key in stm.PARAMETER_KEYS:
        result[key] += weight * (ga[key] + gb[key])
        if not np.all(np.isfinite(result[key])):
            raise ValueError('Nonfinite combined MSE gradient')
    return result


def train_candidate(initial_parameters, indices, targets, config, *, turns, pairs,
                    validation_indices, validation_targets, validation_turns,
                    validation_pairs, anchor_indices, anchor_targets, anchor_turns,
                    patience=5, min_delta_cp2=1.0, eval_interval=5,
                    progress=None, enable_turn=True):
    """Return one validation-selected checkpoint, retaining the epoch-zero oracle.

    Selection minimizes integer point MSE + integer pair-gap MSE, subject to
    old-distribution anchor MSE never exceeding its epoch-zero value. An
    ineligible checkpoint cannot improve either best copy or patience state.
    """
    stm._validate_parameters(initial_parameters)
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
    turns = np.asarray(turns)
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
        return {'epoch': epoch, 'mse_cp2': objective, 'objective_cp2': objective,
                'point': point, 'pair': pair, 'anchor': anchor, 'pair_weight': 1.0,
                'anchor_limit_cp2': anchor['mse_cp2'] if anchor_limit is None else anchor_limit,
                'eligible': anchor_limit is None or anchor['mse_cp2'] <= anchor_limit}

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
    for epoch in range(1, config['epochs'] + 1):
        order = rng.permutation(len(targets))
        pair_order = rng.permutation(len(pairs[0]))
        pair_batches = [pair_order[o:o + config['batch_size']]
                        for o in range(0, len(pair_order), config['batch_size'])]
        maximum_norm, clipped_batches = 0.0, 0
        for batch_no, offset in enumerate(range(0, len(order), config['batch_size'])):
            chosen = order[offset:offset + config['batch_size']]
            pair_chosen = pair_batches[batch_no % len(pair_batches)]
            gradients = qat_gradients(parameters, indices[chosen], targets[chosen],
                                       config['input_scale'], turns=turns[chosen],
                                       pairs=_slice_pairs(pairs, pair_chosen),
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
    return best, history, selection
