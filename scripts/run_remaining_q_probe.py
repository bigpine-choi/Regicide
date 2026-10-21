"""Six hook-only production-policy probes; no training, teacher or held-out use."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build-m3-remaining-q-diagnosis'
OUT = ROOT / 'docs/checks/2026-10-22-nnue-qsearch-reply-budget'
BASE = '7bae01f'
MODEL = ROOT / 'docs/checks/2026-10-22-nnue-head-repair/attempt-001/model.nnue'

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    target = OUT / 'diagnosis-profile.json'
    if target.exists():
        raise SystemExit('Refuse duplicate six-search diagnosis')
    BUILD.mkdir(exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    original = subprocess.check_output(['git', 'show', f'{BASE}:src/search.cpp'], cwd=ROOT).decode('utf-8')
    header = subprocess.check_output(['git', 'show', f'{BASE}:include/regicide/search.hpp'], cwd=ROOT)
    (BUILD / 'include/regicide').mkdir(parents=True, exist_ok=True)
    (BUILD / 'include/regicide/search.hpp').write_bytes(header)
    (OUT / 'diagnosis-search-original.cpp.txt').write_text(original, encoding='utf-8', newline='\n')
    source = original.replace('namespace regicide {\n', '''namespace regicide {
void qremain_visit(const Board&, int, const std::vector<Move>&);
void qremain_event(const char*, const std::vector<Move>&);
void qremain_edge(const Board&, Move, int, bool, bool, bool, int, const std::vector<Move>&);
''', 1)
    def replace(marker, replacement):
        nonlocal source
        assert source.count(marker) == 1, (marker, source.count(marker))
        source = source.replace(marker, replacement)
    replace('        ++qnodes;\n', '        ++qnodes;\n        qremain_visit(board, qply, observed_path_);\n')
    replace('        if (const auto cached = qcache_probe(key, ply, alpha, beta, pv)) return *cached;', '''        if (const auto cached = qcache_probe(key, ply, alpha, beta, pv)) {
            qremain_event("cache_cutoff", observed_path_); return *cached;
        }''')
    replace('            if (best >= beta || qply >= max_quiescence_depth) return finish(best);', '''            if (best >= beta || qply >= max_quiescence_depth) {
                qremain_event(best >= beta ? "standpat_cutoff" : "qdepth_stop", observed_path_);
                return finish(best);
            }''')
    replace('            if (threat) ++quiet_threat_extensions;', '''            if (threat) ++quiet_threat_extensions;
            qremain_edge(board, move, qply, checked, defensive_reply, threat, checks_left, observed_path_);''')
    instrumented = BUILD / 'search-profile.cpp'
    instrumented.write_text(source, encoding='utf-8', newline='\n')
    (OUT / 'diagnosis-search-instrumented.cpp.txt').write_text(source, encoding='utf-8', newline='\n')
    objects = ['board', 'evaluation', 'transposition', 'see', 'neural', 'nnue']
    args = [f'build-m3-qsearch-priority\\{item}.obj' for item in objects]
    commands = [
        '@echo off', 'chcp 65001 >nul',
        'call "C:\\Program Files\\Microsoft Visual Studio\\18\\Community\\VC\\Auxiliary\\Build\\vcvars64.bat" >nul',
        'if errorlevel 1 exit /b 1', f'cd /d "{ROOT}"',
        'cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Ibuild-m3-remaining-q-diagnosis\\include /Iinclude /c build-m3-remaining-q-diagnosis\\search-profile.cpp /Fobuild-m3-remaining-q-diagnosis\\search-profile.obj',
        'if errorlevel 1 exit /b 1',
        'cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Ibuild-m3-remaining-q-diagnosis\\include /Iinclude scripts\\profile_remaining_q.cpp build-m3-remaining-q-diagnosis\\search-profile.obj ' + ' '.join(args) + ' /Fobuild-m3-remaining-q-diagnosis\\profile_remaining_q.obj /Febuild-m3-remaining-q-diagnosis\\regicide_remaining_q_profile.exe',
        'exit /b %errorlevel%',
    ]
    command = BUILD / 'compile.cmd'
    command.write_text('\r\n'.join(commands) + '\r\n', encoding='utf-8')
    built = subprocess.run(['cmd.exe', '/c', str(command)], cwd=ROOT, capture_output=True, text=True)
    (OUT / 'diagnosis-build.txt').write_text(built.stdout + built.stderr, encoding='utf-8')
    if built.returncode:
        print(built.stdout + built.stderr)
        raise SystemExit(built.returncode)
    old = json.loads((ROOT / 'docs/checks/2026-10-22-nnue-qsearch-cost/known-position-check.json').read_text(encoding='utf-8'))
    lines = []
    for row in old['cases']:
        case = row['source']
        for ordering in (False, True):
            lines.append('\t'.join([case['name'], case['initial_fen'], ' '.join(case['history']), str(int(ordering))]))
    binary = BUILD / 'regicide_remaining_q_profile.exe'
    run = subprocess.run([str(binary), str(MODEL)], input='\n'.join(lines) + '\n', cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    (OUT / 'diagnosis-raw.jsonl').write_text(run.stdout, encoding='utf-8')
    if run.returncode:
        raise SystemExit(run.stderr)
    rows = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(rows) == 6
    bound = [Path(__file__), ROOT / 'scripts/profile_remaining_q.cpp', instrumented,
             binary, MODEL, BUILD / 'include/regicide/search.hpp']
    bound.extend(ROOT / 'build-m3-qsearch-priority' / f'{item}.obj' for item in objects)
    parity = []
    for row in rows:
        if not row['ordering']:
            continue
        previous = next(item for item in old['cases'] if item['source']['name'] == row['id'])['searches']['candidate']
        turn = row['fen'].split()[1]
        same = row['move'] == previous['move_uci'] and row['depth'] == previous['depth'] and row['nodes'] == previous['nodes'] and row['score'] * (1 if turn == 'w' else -1) == previous['white_cp'] and (row['first_iteration_nodes'] or None) == previous['first_iteration_nodes']
        parity.append(dict(id=row['id'], matches_archived_production=same))
        assert same, parity[-1]
    receipt = dict(schema_version=1, completed=True, source_commit=BASE,
        diagnostic_searches=6, nodes_per_search=10000, teacher_queries=0, training_runs=0,
        heldout_position_queries=0, input_sha256={str(path.relative_to(ROOT)): sha(path) for path in bound},
        production_parity=parity,
        policy='All old defaults true; NNUE current frozen model; threats and cache true in both arms. Root threat ordering false/true is the sole matrix variation.',
        accounting='Each qnode is charged once to its root branch and earliest optional quiet-check qply bucket. under_checking_capture and under_optional_threat overlap these partitions. Edges count actual entered recursive calls, not all generated candidates. first_iteration_* freezes counters only at a completed depth-one callback. All visited FENs archived for future holdout exclusion.',
        limits='Instrumentation-only wall time is not a speed benchmark. Descendant node charges identify costs, not the counterfactual savings of skipping a move; alpha/beta ordering and cache interactions can redistribute work.',
        cases=rows)
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for row in rows:
        metrics = row['first_iteration_totals'] or row['totals']
        print(row['id'], row['ordering'], 'depth', row['depth'], 'first', row['first_iteration_nodes'],
              'qnodes', metrics['qnodes'], 'quietchecksubtrees', {key: value for key, value in metrics.items() if key.startswith('under_optional_quiet_check')},
              'branches', {key: value['qnodes'] for key, value in (row['first_iteration_branches'] or row['branches']).items()})

if __name__ == '__main__':
    main()
