"""Re-evaluate exposed loss probes after selection; no search or training."""
from pathlib import Path
import sys
import chess

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
from prepare_neural_huber import read, sha, write

output = Path(__file__).resolve().parent
diagnosis_path = output.parent / 'diagnosis/diagnosis.json'
diagnosis = read(diagnosis_path)
models = {
    'warm': ROOT / 'docs/checks/2026-10-21-m3-nnue-stm/attempt-001/model.nnue',
    'failed500': ROOT / 'docs/checks/2026-10-21-m3-stockfish-teacher/attempt-001/model.nnue',
    'guarded': output / 'model.nnue',
}
nets = {name: stm.load_model(path) for name, path in models.items()}
rows = []
for row in diagnosis['recorded_move_teacher_probes']['rows']:
    values = {name: {side: stm.quantized_white(chess.Board(row[side]['fen']), net)
                     for side in ('before', 'after')} for name, net in nets.items()}
    rows.append({'ply': row['ply'], 'move': row['move'], 'model_white_cp': values,
                 'teacher': {side: row[side] for side in ('before', 'after')}})
report = {'rows': rows, 'candidate_selected_before_recheck': True,
          'new_searches': 0, 'new_training': 0,
          'interpretation': 'Exposed diagnostic cases only; never checkpoint selection or independent test evidence.',
          'input_sha256': {str(p): sha(p) for p in [diagnosis_path, *models.values()]}}
write(output / 'regression-probes.json', report)
print([{'ply': row['ply'], 'move': row['move'], 'scores': row['model_white_cp']}
       for row in rows if row['ply'] in (7, 15)])
