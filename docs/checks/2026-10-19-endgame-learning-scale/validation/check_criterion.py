import ast
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT/'build/endgame-learning-scale'
BASE = '527289e74dbdfb28a98625ef961c7470e9c78ff9'

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def body(source, name):
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return '\n'.join(source.splitlines()[node.lineno-1:node.end_lineno]).encode('utf-8')

old = subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE+':scripts/learning_arena.py']).decode('utf-8')
path = ROOT/'scripts/learning_arena.py'
current = path.read_text(encoding='utf-8')
functions = {}
for name in ('promotion_decision', 'verify_match_records'):
    first, second = digest(body(old, name)), digest(body(current, name))
    functions[name] = {'baseline_sha256': first, 'current_sha256': second, 'identical': first == second}
before = json.loads((WORK/'protection-before.json').read_text())
registry = {'baseline_sha256': before['protected']['scripts/learning_registry.py'],
            'current_sha256': digest((ROOT/'scripts/learning_registry.py').read_bytes())}
registry['identical'] = registry['baseline_sha256'] == registry['current_sha256']
passed = all(row['identical'] for row in functions.values()) and registry['identical']
record = {'schema': 'regicide.learning.criterion-preservation.v1', 'status': 'passed' if passed else 'failed',
          'baseline_commit': BASE,
          'current_source_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
          'functions': functions, 'registry': registry, 'learning_arena_whole_sha256': digest(path.read_bytes()),
          'scope': 'Only optional mathematically certain rejection and resource replay were added; full acceptance formula, full rule replay and atomic registry stay unchanged.'}
(WORK/'criterion-preservation.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
print(json.dumps(record, indent=2))
if not passed:
    raise SystemExit(1)
