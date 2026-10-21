"""Bounded diagnosis using a hook-only archived copy of an explicit Git search source.

Six searches, 10,000 nodes each. No training, teacher queries or held-out use.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build-m3-qcost-diagnosis'
OUT = ROOT / 'docs/checks/2026-10-22-nnue-qsearch-cost'
BASE = 'da0444e'
MODEL = ROOT / 'docs/checks/2026-10-22-nnue-head-repair/attempt-001/model.nnue'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    BUILD.mkdir(exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / 'diagnosis-profile.json'
    if target.exists():
        raise SystemExit('diagnosis receipt already exists; refusing repeat')
    original = subprocess.check_output(['git', 'show', f'{BASE}:src/search.cpp'], cwd=ROOT).decode('utf-8')
    header = subprocess.check_output(['git', 'show', f'{BASE}:include/regicide/search.hpp'], cwd=ROOT)
    (BUILD / 'include/regicide').mkdir(parents=True, exist_ok=True)
    (BUILD / 'include/regicide/search.hpp').write_bytes(header)
    (OUT / 'diagnosis-search-original.cpp.txt').write_text(original, encoding='utf-8', newline='\n')
    source = original.replace('namespace regicide {\n', '''namespace regicide {
void qcost_visit(const Board&, int, int, int, int, bool, int, int, const std::vector<Move>&);
void qcost_stop(const char*);
''', 1)
    marker = '        ++qnodes;\n'
    assert source.count(marker) == 1
    source = source.replace(marker, marker + '''        qcost_visit(board, ply, qply, checks_left, threats_left, defensive_reply,
                    alpha, beta, observed_path_);
''')
    marker = '        if ((checked || defensive_reply) && qply >= max_quiescence_depth) throw Interrupted{};'
    assert source.count(marker) == 1
    source = source.replace(marker, '''        if ((checked || defensive_reply) && qply >= max_quiescence_depth) {
            qcost_stop("unresolved_qdepth_ceiling"); throw Interrupted{};
        }''')
    marker = '    void poll(bool entering_node = true) const {\n'
    assert source.count(marker) == 1
    source = source.replace(marker, marker + '''        if (entering_node && limits_.nodes && nodes >= limits_.nodes)
            qcost_stop("node_budget");
''')
    instrumented = BUILD / 'search-profile.cpp'
    instrumented.write_text(source, encoding='utf-8', newline='\n')
    (OUT / 'diagnosis-search-instrumented.cpp.txt').write_text(source, encoding='utf-8', newline='\n')
    objects = ['board', 'evaluation', 'transposition', 'see', 'neural', 'nnue']
    args = [f'build-m3-tactical-guarded\\{item}.obj' for item in objects]
    commands = [
        '@echo off',
        'chcp 65001 >nul',
        'call "C:\\Program Files\\Microsoft Visual Studio\\18\\Community\\VC\\Auxiliary\\Build\\vcvars64.bat" >nul',
        'if errorlevel 1 exit /b 1',
        f'cd /d "{ROOT}"',
        'cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Ibuild-m3-qcost-diagnosis\\include /Iinclude /c build-m3-qcost-diagnosis\\search-profile.cpp /Fobuild-m3-qcost-diagnosis\\search-profile.obj',
        'if errorlevel 1 exit /b 1',
        'cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Ibuild-m3-qcost-diagnosis\\include /Iinclude scripts\\profile_quiescence_cost.cpp build-m3-qcost-diagnosis\\search-profile.obj ' + ' '.join(args) + ' /Fobuild-m3-qcost-diagnosis\\profile_quiescence_cost.obj /Febuild-m3-qcost-diagnosis\\regicide_qcost_profile.exe',
        'exit /b %errorlevel%',
    ]
    command = BUILD / 'compile.cmd'
    command.write_text('\r\n'.join(commands) + '\r\n', encoding='utf-8')
    built = subprocess.run(['cmd.exe', '/c', str(command)], cwd=ROOT, capture_output=True, text=True)
    (OUT / 'diagnosis-build.txt').write_text(built.stdout + built.stderr, encoding='utf-8')
    if built.returncode:
        print(built.stdout + built.stderr)
        raise SystemExit(built.returncode)
    old = json.loads((ROOT / 'docs/checks/2026-10-22-nnue-tactical-search/known-position-check.json').read_text(encoding='utf-8'))
    lines = []
    for row in old['cases']:
        case = row['source']
        for threats in (False, True):
            lines.append('\t'.join([case['name'], case['initial_fen'], ' '.join(case['history']), str(int(threats))]))
    binary = BUILD / 'regicide_qcost_profile.exe'
    run = subprocess.run([str(binary), str(MODEL)], input='\n'.join(lines) + '\n', cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    (OUT / 'diagnosis-raw.jsonl').write_text(run.stdout, encoding='utf-8')
    if run.returncode:
        raise SystemExit(run.stderr)
    rows = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(rows) == 6
    bound = [Path(__file__), ROOT / 'scripts/profile_quiescence_cost.cpp', instrumented,
             binary, MODEL, BUILD / 'include/regicide/search.hpp']
    bound.extend(ROOT / 'build-m3-tactical-guarded' / f'{item}.obj' for item in objects)
    receipt = {
        'schema_version': 1, 'completed': True, 'source_commit': BASE,
        'diagnostic_searches': 6, 'nodes_per_search': 10000, 'teacher_queries': 0,
        'training_runs': 0, 'heldout_position_queries': 0,
        'input_sha256': {str(path.relative_to(ROOT)): sha(path) for path in bound},
        'instrumentation': 'Hook-only copy of explicit Git source; production source/binaries untouched. All qnode entries counted including checks, defensive replies, terminal and stand-pat cutoffs. Static observer used only to count evaluations and maintain exact search paths.',
        'qstate_identity': 'position_key, repetition_context_key, halfmove, remaining_qplies, ply, checks_left, threats_left, defensive_reply; full-window identity additionally includes alpha,beta',
        'limits': 'Instrumentation wall cost is excluded from speed claims. Duplicate visits are opportunities, not measured cache hits or saved nodes; table bounds/replacement may reduce achievable reuse. Hash identity is not independent mathematical collision proof.',
        'cases': rows,
    }
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for row in rows:
        print(row['id'], row['threats'], 'depth', row['depth'], 'qnodes', row['qnodes'],
              'checked', row['checked_qnodes'], 'repeat', row['qstate_repeat_visits'],
              'samewindow', row['same_window_repeat_visits'], row['stop_reason'],
              'branches', row['root_branch_qnodes'])


if __name__ == '__main__':
    main()
