"""Stop MSE STM training on held-out integer-score error and restore its best copy.

The network, quantization, loss, minibatch order and Adam update are unchanged.
Validation is evaluated separately and is never passed to the gradient function.
The untouched initializer is checkpoint zero, so fine-tuning may safely return it.
"""
from __future__ import annotations

import math

import numpy as np

import neural_precision as precision
import neural_qat as qat
import neural_stm as stm
import neural_training as base


def _copy(parameters: dict) -> dict:
    return {key: value.copy() for key, value in parameters.items()}


def train_candidate(initial_parameters: dict, indices: np.ndarray, targets: np.ndarray,
                    config: dict, *, turns, validation_indices: np.ndarray,
                    validation_targets: np.ndarray, validation_turns, patience: int = 10,
                    min_delta_cp2: float = 0.0, eval_interval: int = 1,
                    guard_enabled: bool = True, enable_turn: bool = True,
                    progress=None) -> tuple[dict, list[dict], dict]:
    """Return parameters, ordinary train history, and validation-only selection.

    ``config['epochs']`` is a maximum budget, not a requirement to finish it.
    Patience counts consecutive validation evaluations without an improvement
    exceeding ``min_delta_cp2``.  Every strictly smaller validation MSE is still
    saved, including improvements smaller than that stopping threshold.  Ties
    retain the earlier checkpoint.  ``eval_interval`` is measured in epochs;
    epoch zero and the final budget epoch are always evaluated.

    ``guard_enabled=False`` delegates to the historical final-epoch trainer.
    This mode exists to verify that validation has no role in Adam arithmetic.
    """
    stm._validate_parameters(initial_parameters)
    qat._validate_batch(indices, targets)
    qat._validate_batch(validation_indices, validation_targets)
    stm._turn_mask(turns, len(indices), enable_turn)
    stm._turn_mask(validation_turns, len(validation_indices), enable_turn)
    precision._validate_config(config)
    if config.get('loss', 'mse') != 'mse':
        raise ValueError('The guarded STM trainer keeps MSE fixed')
    if type(patience) is not int or patience < 1:
        raise ValueError('patience must be a positive integer number of evaluations')
    if type(eval_interval) is not int or not 1 <= eval_interval <= config['epochs']:
        raise ValueError('eval_interval must be between one and the epoch budget')
    if (isinstance(min_delta_cp2, (bool, np.bool_))
            or not isinstance(min_delta_cp2, (int, float))
            or not math.isfinite(min_delta_cp2) or min_delta_cp2 < 0):
        raise ValueError('min_delta_cp2 must be a finite nonnegative MSE difference')
    if type(guard_enabled) is not bool or type(enable_turn) is not bool:
        raise ValueError('guard_enabled and enable_turn must be bool')
    if progress is not None and not callable(progress):
        raise ValueError('progress must be callable or None')
    turns = np.asarray(turns)
    bounds = stm._bounds(config['input_scale'])
    parameters = {key: value.astype(np.float64, copy=True)
                  for key, value in initial_parameters.items()}
    if any(np.any(np.abs(value) > bounds[key]) for key, value in parameters.items()):
        raise ValueError('Initial shadow parameter outside model bounds')

    def evaluate(values: dict, epoch: int) -> dict:
        scores, _ = stm.qat_forward(values, validation_indices, config['input_scale'],
                                    turns=validation_turns, enable_turn=enable_turn)
        return {'epoch': epoch, **base.error_metrics(scores, validation_targets)}

    evaluations = [evaluate(parameters, 0)]
    if not guard_enabled:
        final, history = stm.train_candidate(initial_parameters, indices, targets, config,
                                              turns=turns, enable_turn=enable_turn)
        evaluations.append(evaluate(final, config['epochs']))
        return final, history, {
            'loss': 'mse', 'objective': 'held_out_integer_white_cp_mse',
            'mode': 'guard_disabled_last_epoch', 'selected_epoch': config['epochs'],
            'stopped_epoch': config['epochs'], 'stopped_early': False,
            'stop_reason': 'guard_disabled', 'max_epochs': config['epochs'],
            'optimizer_steps': history[-1]['optimizer_steps'],
            'initial_validation': evaluations[0], 'selected_validation': evaluations[-1],
            'last_validation': evaluations[-1], 'validation_evaluations': evaluations,
            'validation_used_in_gradients': False,
        }

    best = _copy(parameters)
    best_evaluation = evaluations[0].copy()
    significant_best = evaluations[0]['mse_cp2']
    stale, step, history = 0, 0, []
    first = {key: np.zeros_like(value) for key, value in parameters.items()}
    second = {key: np.zeros_like(value) for key, value in parameters.items()}
    rates = {key: config['output_lr'] if key == 'output_weights' else config['input_bias_lr']
             for key in stm.PARAMETER_KEYS}
    rng = np.random.default_rng(config['seed'])
    reason = 'max_epochs'
    for epoch in range(1, config['epochs'] + 1):
        order = rng.permutation(len(targets))
        maximum_norm, clipped_batches = 0.0, 0
        for offset in range(0, len(order), config['batch_size']):
            chosen = order[offset:offset + config['batch_size']]
            gradients = stm.qat_gradients(parameters, indices[chosen], targets[chosen],
                                          config['input_scale'], turns=turns[chosen],
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
        history.append({
            'epoch': epoch, 'optimizer_steps': step,
            'fake_quantized_train': base.error_metrics(predictions, targets),
            'shadow_float_train': base.error_metrics(
                stm._float_predictions(parameters, indices, turns, enable_turn), targets),
            'max_batch_gradient_global_norm': maximum_norm,
            'gradient_clipped_batches': clipped_batches,
        })
        if epoch % eval_interval != 0 and epoch != config['epochs']:
            continue
        current = evaluate(parameters, epoch)
        evaluations.append(current)
        if current['mse_cp2'] < best_evaluation['mse_cp2']:
            best, best_evaluation = _copy(parameters), current.copy()
        if current['mse_cp2'] < significant_best - min_delta_cp2:
            significant_best, stale = current['mse_cp2'], 0
        else:
            stale += 1
        if progress is not None:
            progress(epoch, _copy(parameters), current.copy())
        if stale >= patience:
            reason = 'patience_exhausted'
            break
    selection = {
        'loss': 'mse', 'objective': 'held_out_integer_white_cp_mse',
        'mode': 'validation_mse_early_stopping', 'selected_epoch': best_evaluation['epoch'],
        'stopped_epoch': history[-1]['epoch'],
        'stopped_early': history[-1]['epoch'] < config['epochs'],
        'stop_reason': reason, 'max_epochs': config['epochs'],
        'optimizer_steps': step, 'patience_evaluations': patience,
        'min_delta_cp2': float(min_delta_cp2), 'eval_interval_epochs': eval_interval,
        'initial_validation': evaluations[0], 'selected_validation': best_evaluation,
        'last_validation': evaluations[-1], 'validation_evaluations': evaluations,
        'validation_used_in_gradients': False,
    }
    return best, history, selection
