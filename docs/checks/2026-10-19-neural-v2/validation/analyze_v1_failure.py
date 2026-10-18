"""Read-only V1 failure description; held-out arena is NEVER training input."""
import os
for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics

import chess
import numpy as np

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "scripts").is_dir())
RUN = ROOT / "build/learning-cycle/runs/000002-95a3500d5501"
OUT = Path(__file__).resolve().parent
VALUES = {1: 100, 2: 320, 3: 330, 4: 500, 5: 900, 6: 0}
PHASE = {1: 0, 2: 1, 3: 1, 4: 2, 5: 4, 6: 0}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def material(board, color):
    return sum(VALUES[piece.piece_type] * (1 if piece.color == color else -1)
               for piece in board.piece_map().values())


def stage(board):
    phase = min(24, sum(PHASE[piece.piece_type] for piece in board.piece_map().values()))
    return "high_20_24" if phase >= 20 else "middle_7_19" if phase >= 7 else "low_0_6"


def features(board):
    x = np.zeros(768, dtype=np.float64)
    for square, piece in board.piece_map().items():
        index = (piece.piece_type - 1 + (0 if piece.color == board.turn else 6)) * 64
        x[index + (square if board.turn else square ^ 56)] = 1
    return x


def summary(values):
    if not values:
        return {"count": 0}
    return {"count": len(values), "mean": statistics.mean(values),
            "median": statistics.median(values), "min": min(values), "max": max(values)}


def main():
    arena = json.loads((RUN / "arena/arena.json").read_text())
    training = json.loads((RUN / "model/report.json").read_text())
    model_path = RUN / "model/small-v1.nn"
    tokens = model_path.read_text(encoding="ascii").split()
    assert tokens[:5] == ["REGICIDE_NN_V1", "768", "32", "16", "600"]
    values = np.array([float(token) for token in tokens[5:]], dtype=np.float64)
    assert len(values) == 25153
    cursor, weights = 0, []
    for shape in ((32, 768), (32,), (16, 32), (16,), (16,), (1,)):
        count = int(np.prod(shape))
        weights.append(values[cursor:cursor + count].reshape(shape))
        cursor += count

    def predict(boards):
        x = np.stack([features(board) for board in boards])
        h1 = np.maximum(0, x @ weights[0].T + weights[1])
        h2 = np.maximum(0, h1 @ weights[2].T + weights[3])
        z = h2 @ weights[4] + weights[5][0]
        return np.clip(np.array([material(board, board.turn) for board in boards]) + 600 * z, -10000, 10000)

    input_paths = [ROOT / "docs/checks/2026-10-18-neural-v1/data/positions.jsonl",
                   ROOT / "build/learning-cycle/runs/000001-88bbee0e1ae3/data/positions.jsonl",
                   RUN / "data/positions.jsonl", RUN / "corpus/positions.jsonl"]
    coverage = {}
    for path in input_paths:
        data = read_rows(path)
        coverage[path.relative_to(ROOT).as_posix()] = {
            "rows": len(data), "turns": dict(Counter(row["turn"] for row in data)),
            "turns_by_split": {split: dict(Counter(row["turn"] for row in data if row["split"] == split))
                               for split in ("train", "validation", "test")},
            "source_by_turn": {source: dict(Counter(row["turn"] for row in data if row.get("source", "regular") == source))
                               for source in sorted({row.get("source", "regular") for row in data})}}
    data = read_rows(RUN / "corpus/positions.jsonl")
    quality = {}
    for split in ("train", "validation", "test"):
        subset = [row for row in data if row["split"] == split]
        boards = [chess.Board(row["fen"]) for row in subset]
        predictions = predict(boards)
        x = np.stack([features(board) for board in boards])
        legal_indices = [index for index in range(768) if not
                         (index // 64 in (0, 6) and index % 64 // 8 in (0, 7))]
        stage_rows = {}
        for name in ("high_20_24", "middle_7_19", "low_0_6"):
            indices = [index for index, board in enumerate(boards) if stage(board) == name]
            stage_rows[name] = {"count": len(indices),
                "teacher_depth": summary([subset[index]["teacher_depth"] for index in indices]),
                "absolute_nn_teacher_cp_gap": summary([abs(float(predictions[index]) - subset[index]["teacher_cp_stm"]) for index in indices]),
                "absolute_nn_material_residual": summary([abs(float(predictions[index]) - subset[index]["material_cp_stm"]) for index in indices])}
        quality[split] = {"rows": len(subset), "games": len({row["game_id"] for row in subset}),
                         "families": len({row["family"] for row in subset}), "stages": stage_rows,
                         "legal_input_channels_never_active": int(sum(x[:, index].sum() == 0 for index in legal_indices)),
                         "legal_input_channels_active_fewer_than_five_times": int(sum(x[:, index].sum() < 5 for index in legal_indices)),
                         "teacher_outcome_strong_disagreements": sum(abs(row["teacher_cp_stm"]) >= 300 and
                             row["outcome_stm"] != 0 and row["teacher_cp_stm"] * row["outcome_stm"] < 0 for row in subset),
                         "teacher_abs_cp_ge_1200": sum(abs(row["teacher_cp_stm"]) >= 1200 for row in subset)}
    controlled = []
    for index in range(0, len(arena["games"]), 2):
        left, right = arena["games"][index:index + 2]
        candidate, baseline = left["search_moves"][0], right["search_moves"][0]
        assert candidate["role"] == "candidate" and baseline["role"] == "baseline"
        assert candidate["fen_before"] == baseline["fen_before"]
        controlled.append({"pair": left["pair"], "node_ratio": candidate["nodes"] / baseline["nodes"],
                           "candidate_depth": candidate["depth"], "baseline_depth": baseline["depth"]})
    role_stages = {role: {name: {"decisions_with_two_plies": 0, "net_material_concessions_ge_300cp": 0,
                                 "reported_negative_mate": 0}
                         for name in ("high_20_24", "middle_7_19", "low_0_6")}
                   for role in ("candidate", "baseline")}
    examples = []
    loss_states = Counter()
    end_material = defaultdict(list)
    for game in arena["games"]:
        board, boards = chess.Board(), [chess.Board()]
        for text in game["uci_moves"]:
            move = chess.Move.from_uci(text)
            assert move in board.legal_moves
            board.push(move)
            boards.append(board.copy(stack=False))
        candidate_color = game["white"] == "candidate"
        for search in game["search_moves"]:
            ply = search["ply"] - 1
            before = boards[ply]
            bucket = role_stages[search["role"]][stage(before)]
            score = search.get("score") or {}
            if score.get("mate") is not None and score["mate"] < 0:
                bucket["reported_negative_mate"] += 1
            if ply + 2 < len(boards):
                bucket["decisions_with_two_plies"] += 1
                concession = material(before, before.turn) - material(boards[ply + 2], before.turn)
                if concession >= 300:
                    bucket["net_material_concessions_ge_300cp"] += 1
                    examples.append({"game": game["game"], "ply": search["ply"], "role": search["role"],
                                     "phase": stage(before), "move": search["move"], "two_ply_material_concession_cp": concession})
        end_material[str(game["candidate_points"])].append(material(boards[-1], candidate_color))
        if game["candidate_points"] == 0:
            states = [material(state, candidate_color) for state in boards[12:]]
            loss_states["losses"] += 1
            loss_states["started_down_ge_300cp"] += int(states[0] <= -300)
            loss_states["never_down_ge_300cp"] += int(min(states) > -300)
            loss_states["entered_low_material_phase"] += int(any(stage(state) == "low_0_6" for state in boards[12:]))
            candidate_searches = [search for search in game["search_moves"] if search["role"] == "candidate"]
            loss_states["candidate_ever_reported_cp_ge_100"] += int(any((search.get("score") or {}).get("cp") is not None and
                search["score"]["cp"] >= 100 for search in candidate_searches))
    report = {"schema": "regicide.neural-v1.readonly-diagnosis.v1", "training_prohibited": True,
              "scope": "Held-out arena outcomes/positions are descriptive evidence only, never labels or hyperparameter selection signals",
              "source_sha256": {str(path.relative_to(ROOT)): sha(path) for path in input_paths + [model_path, RUN / "arena/arena.json"]},
              "coverage": coverage, "label_and_static_prediction_diagnostics": quality,
              "paired_same_root_first_search": {"pairs": len(controlled), "node_ratio": summary([row["node_ratio"] for row in controlled]),
                  "candidate_lower_depth": sum(row["candidate_depth"] < row["baseline_depth"] for row in controlled),
                  "candidate_equal_depth": sum(row["candidate_depth"] == row["baseline_depth"] for row in controlled),
                  "candidate_greater_depth": sum(row["candidate_depth"] > row["baseline_depth"] for row in controlled)},
              "role_phase_material_patterns": role_stages, "loss_material_states": dict(loss_states),
              "terminal_material_by_candidate_points": {key: summary(value) for key, value in end_material.items()},
              "largest_material_concession_examples": sorted(examples, key=lambda row: -row["two_ply_material_concession_cp"])[:16],
              "existing_training_metrics": training["metrics"],
              "stage_definition": "Same nonpawn phase weights as the engine: N/B=1, R=2, Q=4, both colors sum capped24; descriptive buckets high20..24, middle7..19, low0..6",
              "uncertainty": ["Two-ply material concessions can be sound sacrifices; they are not engine-verified blunders.",
                              "Teacher cp is searched while the NN observation is static; their cp gap is not alone an evaluation-error proof.",
                              "Only paired first searches share exactly the same board. Later role-specific paths differ, so their aggregate node/depth rates are not controlled speed measurements.",
                              "Material phase is not a complete strategic definition of opening/middlegame/endgame.",
                              "No controlled ablation or new native engine search was performed by this helper."]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "v1-failure-diagnosis.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (OUT / "README.md").write_text("# V1 read-only diagnosis\n\nReserved arena evidence is diagnostic only. It must never become training labels or a hyperparameter-selection set.\n\n```json\n" +
        json.dumps({"coverage": coverage, "paired_same_root_first_search": report["paired_same_root_first_search"],
                    "role_phase_material_patterns": role_stages, "loss_material_states": dict(loss_states)}, indent=2) +
        "\n```\n\n" + "\n".join("- " + text for text in report["uncertainty"]) + "\n", encoding="utf-8")
    print(json.dumps({"coverage": coverage, "paired_search": report["paired_same_root_first_search"],
                      "material_patterns": role_stages, "loss_material_states": dict(loss_states)}, indent=2))


if __name__ == "__main__":
    main()
