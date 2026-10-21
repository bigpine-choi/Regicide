"""Read existing frozen training artifacts; never invoke training or an engine."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import chess
import numpy as np


ROOT = Path(__file__).resolve().parents[3]
PREPARATION = ROOT / 'docs/checks/2026-10-21-m3-huber-preparation/attempt-001'
MODEL_IDS = ('B-mse-20261020', 'B-h100-20261020')
MODEL_SHAS = ('267f65646c24292efa28f11a5f5990311ed7da4f6c134d7c4a6b6c1e0f7be1eb',
              '9610be208a160ca60d593a4b35d3bd92d201b146311b65e2e68e8e6b79897210')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify():
    freeze, protocol = read(PREPARATION / 'freeze.json'), read(PREPARATION / 'protocol.json')
    checked = {}

    def check(path, expected=None):
        actual = sha(path)
        require(expected is None or actual == expected, 'Frozen artifact differs: ' + str(path))
        checked[str(Path(path).resolve())] = actual
        return actual

    check(PREPARATION / 'freeze.json')
    check(PREPARATION / 'protocol.json', freeze['protocol_sha256'])
    records = []
    headers = []
    for identity, expected_model in zip(MODEL_IDS, MODEL_SHAS, strict=True):
        folder = PREPARATION / 'training' / identity
        for name in ('metrics.json', 'model.nnue', 'float_weights.npz', 'history.json', 'checkpoints.json'):
            relative = (folder / name).relative_to(PREPARATION).as_posix()
            check(folder / name, freeze['artifact_sha256'][relative])
        record = read(folder / 'metrics.json')
        require(record == next(item for item in freeze['models'] if item['id'] == identity),
                'Metrics and frozen record differ: ' + identity)
        require(record['model_sha256'] == expected_model == sha(folder / 'model.nnue'),
                'Unexpected model identity')
        require(record['config'] == next(item for item in protocol['conditions'] if item['candidate_id'] == identity),
                'Protocol condition differs')
        history = read(folder / 'history.json')
        require([entry['epoch'] for entry in history] == list(range(1, 121)), 'Expected final epoch 120')
        require(history[-1]['optimizer_steps'] == 2160, 'Expected identical 18 batches per epoch')
        with (folder / 'model.nnue').open(encoding='ascii') as stream:
            headers.append(stream.readline().strip())
        records.append(record)
    mse, huber = records
    common = {key: value for key, value in mse['config'].items() if key not in ('candidate_id', 'loss')}
    require(common == {key: value for key, value in huber['config'].items() if key not in ('candidate_id', 'loss')},
            'Training configuration besides loss differs')
    require(mse['loss'] == 'mse' and huber['loss'] == 'h100' and mse['arm'] == huber['arm'] == 'B',
            'Expected B target and MSE/H100 pair')
    for key in ('feature_sha256', 'batch_order_sha256', 'initial_sha256', 'seed', 'train_rows', 'validation_rows'):
        require(mse[key] == huber[key], 'Paired training condition differs: ' + key)
    require(headers == ['REGICIDE_NNUE 2 768 16 4064 256 32'] * 2, 'Structure or precision differs')
    initial = Path(protocol['initial']['path'])
    check(initial, protocol['initial']['sha256'])
    require(mse['initial_sha256'] == protocol['initial']['sha256'], 'Initial checkpoint differs')
    with np.load(initial, allow_pickle=False) as arrays:
        initial_arrays = {key: {'shape': list(arrays[key].shape),
                                'sha256': hashlib.sha256(arrays[key].astype('<f8').tobytes()).hexdigest()}
                          for key in sorted(arrays.files)}
    source = Path(protocol['source_attempt'])
    partitions = {}
    for name, expected_count in (('train', 2209), ('validation', 723)):
        path = source / 'trainval' / (name + '.jsonl')
        check(path, protocol['input_sha256'][str(path)])
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
        require(len(rows) == expected_count, 'Partition count differs')
        require(all(type(row['target_white_cp']) is int and abs(row['target_white_cp']) <= 28000 for row in rows),
                'Invalid B target')
        target = np.asarray([row['target_white_cp'] for row in rows], dtype='<f8')
        features = np.full((len(rows), 2, 32), 768, dtype='<i8')
        for number, row in enumerate(rows):
            for index, (square, piece) in enumerate(sorted(chess.Board(row['fen']).piece_map().items())):
                kind = 'PNBRQKpnbrqk'.index(piece.symbol())
                features[number, 0, index] = kind * 64 + square
                features[number, 1, index] = ((kind + 6) % 12) * 64 + (square ^ 56)
        feature_sha = hashlib.sha256(features.tobytes()).hexdigest()
        if name == 'train':
            require(feature_sha == mse['feature_sha256'], 'Reconstructed features differ')
        partitions[name] = {'rows': len(rows), 'source_sha256': sha(path),
                            'feature_array_sha256': feature_sha,
                            'target_array_sha256': hashlib.sha256(target.tobytes()).hexdigest()}
    generator, order = np.random.default_rng(common['seed']), hashlib.sha256()
    for _ in range(common['epochs']):
        order.update(generator.permutation(2209).astype('<i8').tobytes())
    require(order.hexdigest() == mse['batch_order_sha256'], 'Reconstructed batch order differs')
    # These frozen sources show one target-selection path and one training path;
    # loss is passed only to the fixed MSE/H100 gradient branch. No source is executed.
    for name in ('prepare_neural_huber.py', 'neural_precision.py', 'neural_training.py', 'neural_qat.py'):
        path = PREPARATION / 'source-snapshot/scripts' / name
        check(path, freeze['artifact_sha256'][path.relative_to(PREPARATION).as_posix()])
    prior_mse = source / 'training/B-20261020/model.nnue'
    check(prior_mse, MODEL_SHAS[0])
    return {'schema_version': 1, 'status': 'PASS', 'created_utc': datetime.now(timezone.utc).isoformat(),
            'verifier_source_sha256': sha(__file__), 'checked_inputs': checked,
            'candidate_id': MODEL_IDS[1], 'baseline_id': MODEL_IDS[0],
            'model_sha256': dict(zip(('baseline', 'candidate'), MODEL_SHAS, strict=True)),
            'loss_only_training_comparison': True, 'shared_config': common,
            'target': 'target_white_cp; same B own-engine 10000-node search labels, no clipping or fallback',
            'partitions': partitions, 'model_header': headers[0], 'initial_sha256': sha(initial),
            'initial_arrays': initial_arrays, 'batch_order_sha256': order.hexdigest(),
            'losses': protocol['loss'], 'historical_mse_bytes_reused_exactly': True,
            'training_invoked': False, 'engine_invoked': False,
            'limits': ['Training provenance is verified from immutable protocol, metrics, source and input bytes; training is not replayed.',
                       'A fixed seed pair and 16 exposed development games cannot establish a general loss-function strength advantage.']}


if __name__ == '__main__':
    result = verify()
    with Path(sys.argv[1]).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: result[key] for key in ('status', 'model_sha256', 'shared_config', 'training_invoked', 'engine_invoked')}))
