import json
from pathlib import Path
import audit_data as A
import collect_endgame_data as E

heldout_raw = A.source(A.RUN / 'endgame-heldout')
heldout_quality = A.source(A.RUN / 'endgame-heldout-quality')
train_raw = A.source(A.RUN / 'endgame-train')
bootstrap = A.source(A.ROOT / 'build/learning-cycle/bootstrap')
baseline = A.source(A.BASE.parent)
arena = E.ExclusionInventory()
arena.add_openings(A.ROOT / 'tests/data/learning_arena_openings_v1.json')
for directory in sorted((A.ROOT / 'build/learning-cycle/runs').iterdir()):
    path = directory / 'arena/arena.json'
    if directory.name < A.RUN.name and path.is_file():
        arena.add_games(path)
inventories = {'new_full_heldout': A.full_keys(heldout_raw['games']),
               'bootstrap_full_heldout': A.full_keys(bootstrap['games'], {'validation', 'test'}),
               'baseline_all_samples': A.sample_keys(baseline['positions']),
               'reserved_arena': (arena.canonical, arena.features)}
collisions = {}
for name, inventory in inventories.items():
    result = A.collision_count(train_raw['positions'], inventory)
    A.require(not any(result.values()), 'raw training overlaps ' + name)
    collisions['raw_train/' + name] = result
    if name != 'new_full_heldout':
        result = A.collision_count(heldout_quality['positions'], inventory)
        A.require(not any(result.values()), 'new independent holdout overlaps ' + name)
        collisions['quality_heldout/' + name] = result
A.full_keys(train_raw['games'])
quality = A.audit_quality(heldout_raw, heldout_quality, 'holdout-quality-report.json')
report = {'status': 'partial-passed', 'remaining': 'Training quality and final augmentation are still running; this report is not final audit.',
          'heldout_quality': quality, 'collisions': collisions,
          'datasets': {name: A.describe(source) for name, source in (
              ('raw_heldout', heldout_raw), ('quality_heldout', heldout_quality), ('raw_train', train_raw))},
          'checks': dict(A.checks)}
Path(__file__).with_name('data-audit-partial.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'status': report['status'], 'heldout_quality': quality, 'checks': dict(A.checks)}))
