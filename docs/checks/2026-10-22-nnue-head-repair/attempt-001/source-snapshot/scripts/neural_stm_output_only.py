"""Output-only MSE QAT with an exactly frozen NNUE hidden representation.

The unchanged format-v3 network still has 768 piece inputs, one turn input and
16 hidden units. Only its existing 16 output weights receive gradients or Adam
state. All previous TRAIN losses, integer-step backtracking and held-out guards
keep their meanings. Frozen float bytes and quantized tensors are audited.
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
import neural_stm_preservation as preservation
import neural_stm_preservation_step as guarded

_copy = ordering._copy
_validate_pairs = ordering._validate_pairs
_slice_pairs = ordering._slice_pairs
_validate_pair_families = ordering._validate_pair_families
_family_pair_batches = ordering._family_pair_batches
pair_metrics = ordering.pair_metrics
_preservation_weight = preservation._preservation_weight
_validate_training_subset = preservation._validate_training_subset
preservation_metrics = preservation.preservation_metrics
training_objective = guarded.training_objective
STEP_SCALES = guarded.STEP_SCALES
FROZEN_KEYS = ('input_weights', 'hidden_bias', 'turn_weights')


def _tensor_digest(value):
    value = np.asarray(value)
    metadata = f'{value.dtype.str}:{value.shape}:'.encode('ascii')
    return hashlib.sha256(metadata + value.tobytes(order='C')).hexdigest()


def _frozen_snapshot(parameters, input_scale=32):
    model = stm.quantize(parameters, input_scale)
    return {'float': {key: _tensor_digest(parameters[key]) for key in FROZEN_KEYS},
            'quantized': {key: _tensor_digest(model[key]) for key in FROZEN_KEYS}}


def _check_frozen(parameters, snapshot, input_scale=32):
    actual = _frozen_snapshot(parameters, input_scale)
    if actual != snapshot:
        raise ValueError('Frozen NNUE representation changed')
    return {'float_bytes_unchanged': True, 'quantized_unchanged': True,
            'float_sha256': actual['float'], 'quantized_sha256': actual['quantized']}


def _mse_output_gradient(parameters, indices, targets, input_scale, turns, enable_turn,
                         forward=None):
    """The existing output STE, without forming any hidden/input derivatives."""
    qat._validate_batch(indices, targets)
    prediction, cache = (stm.qat_forward(parameters, indices, input_scale,
                                         turns=turns, enable_turn=enable_turn)
                         if forward is None else forward)
    derivative = precision.loss_derivative(prediction, targets, 'mse')
    derivative *= (cache['raw_scores'] > -28000) & (cache['raw_scores'] < 28000)
    result = (cache['differences'] / input_scale).T @ derivative
    bound = stm._bounds(input_scale)['output_weights']
    result *= ((parameters['output_weights'] >= -bound)
               & (parameters['output_weights'] <= bound))
    return result


def qat_gradients(parameters, indices, targets, input_scale=32, *, turns, pairs,
                  preservation_indices, preservation_targets, preservation_turns,
                  preservation_weight=4.0, enable_turn=True):
    """Only d(output): point mean + gap mean + lambda*old-TRAIN drift mean.

    The derivatives equal the previous trainer's output slice exactly. No
    frozen-parameter gradient arrays are allocated, clipped or accumulated.
    """
    weight = _preservation_weight(preservation_weight)
    ia, ta, sa, ib, tb, sb = _validate_pairs(pairs, enable_turn)
    qat._validate_batch(preservation_indices, preservation_targets)
    stm._turn_mask(preservation_turns, len(preservation_indices), enable_turn)
    result = _mse_output_gradient(parameters, indices, targets, input_scale,
                                  turns, enable_turn)
    fa = stm.qat_forward(parameters, ia, input_scale, turns=sa, enable_turn=enable_turn)
    fb = stm.qat_forward(parameters, ib, input_scale, turns=sb, enable_turn=enable_turn)
    pa, pb = fa[0], fb[0]
    residual = (pa - pb) - (ta - tb)
    result += (_mse_output_gradient(parameters, ia, pa - residual, input_scale,
                                    sa, enable_turn, fa)
               + _mse_output_gradient(parameters, ib, pb + residual, input_scale,
                                      sb, enable_turn, fb))
    if weight:
        result += weight * _mse_output_gradient(
            parameters, preservation_indices, preservation_targets, input_scale,
            preservation_turns, enable_turn)
    if not np.all(np.isfinite(result)):
        raise ValueError('Nonfinite output-only MSE gradient')
    return {'output_weights': result}


def backtracked_step(parameters, output_proposal, objective, current_objective):
    """Interpolate only the bounded output delta using the existing scale rule."""
    current = guarded._finite_objective(current_objective)
    output_proposal = np.asarray(output_proposal)
    old_output = parameters['output_weights']
    if (output_proposal.shape != old_output.shape
            or not np.issubdtype(output_proposal.dtype, np.floating)
            or not np.all(np.isfinite(output_proposal))):
        raise ValueError('Invalid output-only Adam proposal')
    delta = output_proposal - old_output
    trials = []
    for scale in STEP_SCALES:
        candidate = {**{key: parameters[key].copy() for key in FROZEN_KEYS},
                     'output_weights': old_output + scale * delta}
        for key in FROZEN_KEYS:
            candidate[key].setflags(write=False)
        tested = guarded._finite_objective(objective(candidate))
        trials.append({'scale': scale, 'objective_cp2': tested})
        if tested <= current:
            return candidate, tested, scale, trials
    return _copy(parameters), current, 0., trials


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
    parameters = _copy(initial_parameters)
    parameters['output_weights'] = parameters['output_weights'].astype(np.float64, copy=True)
    frozen = _frozen_snapshot(parameters, config['input_scale'])
    for key in FROZEN_KEYS:
        parameters[key].setflags(write=False)
    frozen_checks = 0
    if any(np.any(np.abs(value) > bounds[key]) for key, value in parameters.items()):
        raise ValueError('Initial shadow parameter outside model bounds')

    def evaluate(values, epoch, anchor_limit=None):
        frozen_audit = _check_frozen(values, frozen, config['input_scale'])
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
                'eligible': anchor_limit is None or anchor['mse_cp2'] <= anchor_limit,
                'frozen_parameters': frozen_audit}
        if preservation_weight:
            evaluation['preservation_train'] = preservation_metrics(
                values, preservation_indices, preservation_targets, preservation_turns,
                config['input_scale'], enable_turn=enable_turn)
        return evaluation

    def objective(values):
        return training_objective(
            values, indices, targets, turns=turns, pairs=pairs,
            preservation_indices=preservation_indices,
            preservation_targets=preservation_targets,
            preservation_turns=preservation_turns, input_scale=config['input_scale'],
            preservation_weight=preservation_weight, enable_turn=enable_turn)

    current_train_objective = objective(parameters)
    initial = evaluate(parameters, 0)
    evaluations = [initial]
    anchor_limit = initial['anchor']['mse_cp2']
    best, best_evaluation = _copy(parameters), initial.copy()
    significant_best, stale, step, history = initial['mse_cp2'], 0, 0, []
    first = np.zeros_like(parameters['output_weights'])
    second = np.zeros_like(parameters['output_weights'])
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
        epoch_objective_before = current_train_objective
        scale_counts = {str(scale): 0 for scale in STEP_SCALES}
        skipped_updates, trial_evaluations, skip_log = 0, 0, []
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
            if set(gradients) != {'output_weights'}:
                raise ValueError('Frozen parameters must not have gradients')
            gradient = gradients['output_weights'] * factor
            first *= 0.9
            first += 0.1 * gradient
            second *= 0.999
            second += 0.001 * gradient * gradient
            estimate = ((first / (1 - 0.9 ** step))
                        / (np.sqrt(second / (1 - 0.999 ** step)) + 1e-8))
            proposal = np.clip(parameters['output_weights'] - config['output_lr'] * estimate,
                               -bounds['output_weights'], bounds['output_weights'])
            if not np.all(np.isfinite(proposal)):
                raise ValueError('Nonfinite output-only QAT proposal')
            parameters, current_train_objective, scale, trials = backtracked_step(
                parameters, proposal, objective, current_train_objective)
            _check_frozen(parameters, frozen, config['input_scale'])
            frozen_checks += 1
            trial_evaluations += len(trials)
            if scale:
                scale_counts[str(scale)] += 1
            else:
                skipped_updates += 1
                skip_log.append({'optimizer_step': step, 'batch': batch_no,
                                 'current_objective_cp2': current_train_objective,
                                 'trial_objectives': trials})
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
                        'gradient_clipped_batches': clipped_batches,
                        'gradient_norm_parameter_keys': ['output_weights'],
                        'frozen_parameters': _check_frozen(parameters, frozen, config['input_scale']),
                        'integer_step_control': {
                            'objective_before_cp2': epoch_objective_before,
                            'objective_after_cp2': current_train_objective,
                            'adam_proposals': point_batch_count,
                            'trial_evaluations': trial_evaluations,
                            'accepted_scale_histogram': scale_counts,
                            'skipped_updates': skipped_updates, 'skip_log': skip_log}})
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
    selection['integer_step_control'] = {
        'objective': 'Full TRAIN integer point MSE + all-pair gap MSE + preservation_weight*old-TRAIN warm-output MSE',
        'scales': list(STEP_SCALES), 'acceptance': 'First scale with finite objective <= current objective; otherwise skip',
        'equal_objective_allowed': True,
        'adam_moments_policy': 'Update moments and optimizer step once per proposal, including skipped proposals; scale only the bounded parameter delta',
        'validation_used_for_step_scale': False, 'anchor_used_for_step_scale': False,
        'total_proposals': step,
        'total_skipped': sum(row['integer_step_control']['skipped_updates'] for row in history),
        'total_trial_evaluations': sum(row['integer_step_control']['trial_evaluations'] for row in history),
        'initial_train_objective_cp2': history[0]['integer_step_control']['objective_before_cp2'],
        'last_train_objective_cp2': current_train_objective}
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
    selection['output_only'] = {
        'trainable_keys': ['output_weights'], 'trainable_parameters': stm.HIDDEN,
        'frozen_keys': list(FROZEN_KEYS),
        'gradient_norm_keys': ['output_weights'],
        'adam_moment_keys': ['output_weights'],
        'proposal_delta_keys': ['output_weights'],
        'frozen_checks_after_proposals': frozen_checks,
        'selected_frozen_parameters': _check_frozen(best, frozen, config['input_scale'])}
    return best, history, selection
