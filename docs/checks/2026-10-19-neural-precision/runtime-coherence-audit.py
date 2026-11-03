"""Independent format/integer audit; does not import the training implementation."""
from pathlib import Path
import hashlib
import json
import struct
import subprocess
import tempfile

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/checks/2026-10-19-neural-precision'
ENGINE = ROOT / 'build-neural-precision-runtime/regicide.exe'
CURRENT = OUT / 'selection/model.nnue'
OLD = ROOT / 'docs/checks/2026-10-19-neural-qat/selection-final/model.nnue'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checksum(model):
    words = [model['version'], 768, 16, model['clip'], model['denominator']]
    if model['version'] == 2:
        words.append(model['scale'])
    words += model['bias'] + model['output'] + model['input']
    result = 14695981039346656037
    for value in words:
        for byte in struct.pack('<I', value & 0xffffffff):
            result = ((result ^ byte) * 1099511628211) & 0xffffffffffffffff
    return result


def parse(path):
    tokens = Path(path).read_text(encoding='ascii').split()
    assert tokens[0] == 'REGICIDE_NNUE'
    version, features, hidden, clip, denominator = map(int, tokens[1:6])
    assert features == 768 and hidden == 16 and version in (1, 2)
    offset = 6 if version == 1 else 7
    scale = 1 if version == 1 else int(tokens[6])
    assert clip == 127 * scale
    assert 1 <= denominator <= 65536 and denominator % scale == 0
    assert (scale == 1 if version == 1 else 2 <= scale <= 256 and not scale & (scale - 1))
    assert len(tokens) == offset + 12320 + 2 and tokens[-2] == 'checksum'
    values = list(map(int, tokens[offset:-2]))
    model = dict(version=version, scale=scale, clip=clip, denominator=denominator,
                 bias=values[:16], output=values[16:32], input=values[32:])
    assert max(map(abs, model['bias'])) <= (4096 if version == 1 else 1048576)
    assert max(map(abs, model['output'])) <= 8192
    assert max(map(abs, model['input'])) <= (127 if version == 1 else 32767)
    assert checksum(model) == int(tokens[-1])
    return model


def predict(board, model):
    views = [model['bias'].copy(), model['bias'].copy()]
    pieces = 'PNBRQKpnbrqk'
    for square, piece in board.piece_map().items():
        kind = pieces.index(piece.symbol())
        for sums, feature in zip(views, (kind * 64 + square, ((kind + 6) % 12) * 64 + (square ^ 56))):
            for hidden in range(16):
                sums[hidden] += model['input'][feature * 16 + hidden]
    numerator = sum((min(max(views[0][h], 0), model['clip']) -
                     min(max(views[1][h], 0), model['clip'])) * model['output'][h]
                    for h in range(16))
    white = (abs(numerator) // model['denominator']) * (-1 if numerator < 0 else 1)
    return min(max(white, -28000), 28000) * (1 if board.turn else -1)


def run(path, fens):
    result = subprocess.run([str(ENGINE), 'eval-batch', '--neural-model', str(path)],
                            input='\n'.join(fens) + '\n', text=True, capture_output=True, check=True)
    assert not result.stderr
    scores = [tuple(map(int, line.split('\t'))) for line in result.stdout.splitlines()]
    assert len(scores) == len(fens)
    return scores


def serialize(model, path):
    header = f"REGICIDE_NNUE {model['version']} 768 16 {model['clip']} {model['denominator']}"
    if model['version'] == 2:
        header += f" {model['scale']}"
    values = model['bias'] + model['output'] + model['input']
    Path(path).write_text(header + '\n' + ' '.join(map(str, values)) +
                          f"\nchecksum {checksum(model)}\n", encoding='ascii')


def main():
    before = {str(path.relative_to(ROOT)): sha(path) for path in (CURRENT, OLD)}
    current, old = parse(CURRENT), parse(OLD)
    with np.load(OUT / 'selection/float_weights.npz', allow_pickle=False) as shadows:
        for key, exported, multiplier in [('input_weights', current['input'], current['scale']),
                                           ('hidden_bias', current['bias'], current['scale']),
                                           ('output_weights', current['output'], 8)]:
            assert np.array_equal(np.rint(shadows[key] * multiplier).astype(np.int64).ravel(), exported)
    initial_path = ROOT / 'docs/checks/2026-10-19-neural-training/first-run/training/float_weights.npz'
    with np.load(initial_path, allow_pickle=False) as initial:
        changes = {key: int(np.count_nonzero(np.rint(initial[key] * multiplier).astype(np.int64).ravel() != exported))
                   for key, exported, multiplier in [('input_weights', current['input'], current['scale']),
                                                      ('hidden_bias', current['bias'], current['scale']),
                                                      ('output_weights', current['output'], 8)]}
    assert changes == {'input_weights': 975, 'hidden_bias': 2, 'output_weights': 11}
    records = [json.loads(line) for line in (ROOT / 'docs/checks/2026-10-19-neural-training/first-run/data/train.jsonl').read_text().splitlines()]
    fens = [row['fen'] for row in records]
    boards = [chess.Board(fen) for fen in fens]
    scores = {}
    for name, path, model in [('old_qat_v1', OLD, old), ('selected_v2', CURRENT, current)]:
        cpp = run(path, fens)
        expected = [predict(board, model) for board in boards]
        assert all(full == inc == reference for (_, full, inc), reference in zip(cpp, expected))
        errors = [reference * (1 if board.turn else -1) - row['target_white_cp']
                  for reference, board, row in zip(expected, boards, records)]
        scores[name] = {'version': model['version'], 'input_scale': model['scale'],
                        'activation_clip': model['clip'], 'denominator': model['denominator'],
                        'checksum': checksum(model), 'sha256': sha(path),
                        'rows': len(records), 'cpp_full_incremental_independent_mismatches': 0,
                        'train_mae_cp': sum(map(abs, errors)) / len(errors),
                        'train_mse_cp2': sum(error * error for error in errors) / len(errors)}
    with tempfile.TemporaryDirectory() as directory:
        for name, model in [('v1', old), ('v2', current)]:
            rewritten = Path(directory) / (name + '.nnue')
            serialize(model, rewritten)
            assert parse(rewritten) == model
            assert run(rewritten, fens) == run(OLD if name == 'v1' else CURRENT, fens)
        rescaled = dict(old, version=2, scale=current['scale'], clip=127 * current['scale'],
                        denominator=old['denominator'] * current['scale'],
                        bias=[v * current['scale'] for v in old['bias']],
                        input=[v * current['scale'] for v in old['input']])
        rewritten = Path(directory) / 'exact-rescaled-baseline.nnue'
        serialize(rescaled, rewritten)
        assert run(rewritten, fens) == run(OLD, fens)
    assert before == {str(path.relative_to(ROOT)): sha(path) for path in (CURRENT, OLD)}
    provenance = json.loads((OUT / 'build-provenance.json').read_text(encoding='utf-8-sig'))
    assert all(sha(ROOT / path) == expected for path, expected in provenance['hashes'].items())
    result = dict(method='Independent token parser, FNV1a uint32 little-endian and scalar Python integer forward; no training-module imports',
                  holdout_read=False, source_or_model_modified=False, training_rows=len(records), models=scores,
                  exact_rewrite_cpp_rows_per_model=len(records), exact_v1_rescale_v2_cpp_rows=len(records),
                  selected_integer_changes_vs_same_scale_initial=changes,
                  selected_shadow_export_exact=True, build_sources_and_binaries_match_provenance=True)
    (OUT / 'runtime-coherence-audit.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
