"""Small, independently trained residual evaluator shared by learning tools.

The features and text format are also implemented by the native evaluator.
Scores are from the side-to-move perspective. No external engine weights are used.
"""

from dataclasses import dataclass
import math
from pathlib import Path
import re

import chess
import numpy as np

INPUTS, HIDDEN1, HIDDEN2 = 768, 32, 16
SCALE = 600.0
MAX_FILE_BYTES = 4 * 1024 * 1024
MAGIC = "REGICIDE_NN_V1"
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
SHAPES = {"w1": (32, 768), "b1": (32,), "w2": (16, 32),
          "b2": (16,), "w3": (16,), "b3": (1,)}
PIECE_VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
                chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def board_from(value):
    board = chess.Board(value) if isinstance(value, str) else value
    if not isinstance(board, chess.Board) or not board.is_valid():
        raise ValueError("features require a valid standard chess position")
    if board.chess960:
        raise ValueError("Chess960 is not supported")
    return board


def feature_key(position):
    """Exact active network-input indices, including color/rank symmetry."""
    board = board_from(position)
    indices = []
    for square, piece in board.piece_map().items():
        channel = piece.piece_type - 1 + (0 if piece.color == board.turn else 6)
        oriented = square if board.turn == chess.WHITE else square ^ 56
        indices.append(channel * 64 + oriented)
    return tuple(sorted(indices))


def encode_features(position):
    """Own PNBRQK then opponent PNBRQK, a1=0; Black flips ranks."""
    result = np.zeros(INPUTS, dtype=np.float64)
    result[list(feature_key(position))] = 1.0
    return result


def material_cp_stm(position):
    board = board_from(position)
    white = sum(PIECE_VALUES[p.piece_type] * (1 if p.color else -1)
                for p in board.piece_map().values())
    return white if board.turn == chess.WHITE else -white


@dataclass
class Model:
    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    w3: np.ndarray
    b3: np.ndarray

    def parameters(self):
        return {name: getattr(self, name) for name in SHAPES}

    def copy(self):
        return Model(**{name: value.copy() for name, value in self.parameters().items()})

    def validate(self):
        for name, shape in SHAPES.items():
            value = getattr(self, name)
            if value.shape != shape or value.dtype != np.float64:
                raise ValueError(f"invalid {name} shape or dtype")
            if not np.all(np.isfinite(value)) or np.any(np.abs(value) > 100):
                raise ValueError(f"{name} contains nonfinite or excessive weights")
        return self


def initialize_model(seed=20261018):
    rng = np.random.default_rng(seed)
    return Model(rng.normal(0, math.sqrt(2 / INPUTS), SHAPES["w1"]),
                 np.zeros(32), rng.normal(0, math.sqrt(2 / 32), SHAPES["w2"]),
                 np.zeros(16), rng.normal(0, .02, 16), np.zeros(1)).validate()


def predict_z(features, model):
    values = np.asarray(features, dtype=np.float64)
    if values.ndim not in (1, 2) or values.shape[-1] != INPUTS:
        raise ValueError("features must have final dimension 768")
    if not np.all(np.isfinite(values)):
        raise ValueError("nonfinite features")
    first = np.maximum(0, values @ model.w1.T + model.b1)
    second = np.maximum(0, first @ model.w2.T + model.b2)
    return second @ model.w3 + model.b3[0]


def evaluate_cp(position, model):
    score = material_cp_stm(position) + SCALE * float(predict_z(encode_features(position), model))
    if not math.isfinite(score):
        raise ValueError("nonfinite model prediction")
    score = min(10000.0, max(-10000.0, score))
    return math.floor(score + .5) if score >= 0 else math.ceil(score - .5)


def loss_and_gradients(model, features, material, targets):
    """Mean squared error after tanh(material/600 + residual)."""
    x = np.asarray(features, dtype=np.float64)
    material = np.asarray(material, dtype=np.float64)
    targets = np.asarray(targets, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != INPUTS or len(x) == 0:
        raise ValueError("nonempty batch of 768 features required")
    if material.shape != (len(x),) or targets.shape != (len(x),):
        raise ValueError("material and targets must match batch length")
    if not all(np.all(np.isfinite(a)) for a in (x, material, targets)):
        raise ValueError("nonfinite training data")
    if np.any(np.abs(targets) > 1):
        raise ValueError("targets must be bounded to [-1,1]")
    h1 = np.maximum(0, x @ model.w1.T + model.b1)
    h2 = np.maximum(0, h1 @ model.w2.T + model.b2)
    predicted = np.tanh(material / SCALE + h2 @ model.w3 + model.b3[0])
    error = predicted - targets
    loss = float(np.mean(error * error))
    dz = 2 * error * (1 - predicted * predicted) / len(x)
    dh2 = dz[:, None] * model.w3[None, :] * (h2 > 0)
    dh1 = (dh2 @ model.w2) * (h1 > 0)
    gradients = {"w1": dh1.T @ x, "b1": dh1.sum(axis=0),
                 "w2": dh2.T @ h1, "b2": dh2.sum(axis=0),
                 "w3": h2.T @ dz, "b3": np.array([dz.sum()])}
    if not math.isfinite(loss) or not all(np.all(np.isfinite(a)) for a in gradients.values()):
        raise ValueError("nonfinite loss or gradient")
    return loss, gradients


class Adam:
    def __init__(self, model, learning_rate=.003):
        if not math.isfinite(learning_rate) or learning_rate <= 0:
            raise ValueError("learning rate must be positive")
        self.learning_rate = learning_rate
        self.m = {name: np.zeros_like(a) for name, a in model.parameters().items()}
        self.v = {name: np.zeros_like(a) for name, a in model.parameters().items()}
        self.steps = 0

    def step(self, model, gradients):
        norm = math.sqrt(sum(float(np.sum(g * g)) for g in gradients.values()))
        if not math.isfinite(norm):
            raise ValueError("nonfinite gradient norm")
        scale = min(1.0, 10.0 / max(norm, 1e-30))
        self.steps += 1
        for name, value in model.parameters().items():
            grad = gradients[name] * scale
            self.m[name] = .9 * self.m[name] + .1 * grad
            self.v[name] = .999 * self.v[name] + .001 * grad * grad
            corrected_m = self.m[name] / (1 - .9 ** self.steps)
            corrected_v = self.v[name] / (1 - .999 ** self.steps)
            value -= self.learning_rate * corrected_m / (np.sqrt(corrected_v) + 1e-8)
        model.validate()


def export_model(model, path):
    model.validate()
    path = Path(path)
    header = f"{MAGIC} {INPUTS} {HIDDEN1} {HIDDEN2} {int(SCALE)}\n"
    lines = [header] + [" ".join(format(float(v), ".17g") for v in value.flat) + "\n"
                        for value in model.parameters().values()]
    payload = "".join(lines)
    if len(payload.encode("ascii")) >= MAX_FILE_BYTES:
        raise ValueError("model file exceeds size limit")
    with path.open("x", encoding="ascii", newline="\n") as handle:
        handle.write(payload)


def load_model(path):
    path = Path(path)
    if path.stat().st_size >= MAX_FILE_BYTES:
        raise ValueError("model file exceeds size limit")
    text = path.read_text(encoding="ascii")
    header, separator, body = text.partition("\n")
    if not separator or header.split() != [MAGIC, "768", "32", "16", "600"]:
        raise ValueError("invalid model header")
    tokens = body.split()
    expected = sum(math.prod(shape) for shape in SHAPES.values())
    if len(tokens) != expected:
        raise ValueError("incorrect model parameter count")
    if not all(NUMBER.fullmatch(token) for token in tokens):
        raise ValueError("invalid model number")
    try:
        values = np.array([float(token) for token in tokens], dtype=np.float64)
    except (ValueError, OverflowError) as error:
        raise ValueError("invalid model number") from error
    for token, value in zip(tokens, values):
        mantissa = re.split("[eE]", token, maxsplit=1)[0]
        if value == 0 and any(character in "123456789" for character in mantissa):
            raise ValueError("model number underflows double precision")
    parameters, offset = {}, 0
    for name, shape in SHAPES.items():
        count = math.prod(shape)
        parameters[name] = values[offset:offset + count].reshape(shape).copy()
        offset += count
    return Model(**parameters).validate()
