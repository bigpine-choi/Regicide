import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build-m2-opponents'
EVIDENCE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents'
EVIDENCE.mkdir(parents=True, exist_ok=True)
SF_COMMIT = 'cb3d4ee9b47d0c5aae855b12379378ea1439675c'
SUN_COMMIT = 'e396948acbd80cdcbad3dccfb07e878fba9df8f4'
SF_URL = 'https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-windows-x86-64.zip'
SF_ZIP_SHA = '40cc975817e7eee270b03f354810d20956df565420d320f6dd37d454dc81a139'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'Regicide-M2-opponent-preparation'})
    with urllib.request.urlopen(request, timeout=120) as response, target.open('wb') as stream:
        shutil.copyfileobj(response, stream)
    return {'url': url, 'path': str(target.relative_to(ROOT)), 'sha256': digest(target), 'bytes': target.stat().st_size}


def smoke(command, options):
    engine = chess.engine.SimpleEngine.popen_uci(list(map(str, command)), timeout=15)
    try:
        advertised = {key: {'type': value.type, 'default': value.default, 'min': value.min, 'max': value.max}
                      for key, value in engine.options.items()}
        engine.configure(options)
        engine.ping()
        board = chess.Board()
        reply = engine.play(board, chess.engine.Limit(time=.05), game=object(), info=chess.engine.INFO_ALL)
        assert reply.move is not None and reply.move in board.legal_moves
        return {'command': list(map(str, command)), 'id': dict(engine.id), 'advertised_options': advertised,
                'requested_options': options, 'legal_bestmove': reply.move.uci(),
                'requested_movetime_ms': 50, 'scope': 'UCI handshake/configuration/legal-move smoke only; not playing-strength evidence'}
    finally:
        engine.quit()
        engine.close()


report = {'status': 'RUNNING', 'stockfish': {}, 'sunfish': {}, 'original_checkouts_modified': False,
          'shared_regicide_binary_used': False, 'existing_gui_process_used': False}
(EVIDENCE / 'provenance.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
try:
    sf_dir = BUILD / 'stockfish18'
    archive = sf_dir / 'stockfish-windows-x86-64.zip'
    print('Fetching official pinned Stockfish 18 asset', flush=True)
    asset = download(SF_URL, archive)
    assert asset['sha256'] == SF_ZIP_SHA, asset
    package = sf_dir / 'package'
    package.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            (package / item.filename).resolve().relative_to(package.resolve())
        bundle.extractall(package)
    binary = next(package.rglob('stockfish-windows-x86-64.exe'))
    sf_license = next(package.rglob('Copying.txt'))
    report['stockfish'] = {'name': 'Stockfish', 'version': '18', 'revision': SF_COMMIT, 'tag': 'sf_18',
                          'source_url': 'https://github.com/official-stockfish/Stockfish',
                          'release_url': 'https://github.com/official-stockfish/Stockfish/releases/tag/sf_18',
                          'asset': asset, 'asset_digest_verified': True,
                          'binary_path': str(binary.relative_to(ROOT)), 'binary_sha256': digest(binary),
                          'license': 'GPL-3.0-or-later',
                          'license_url': f'https://github.com/official-stockfish/Stockfish/blob/{SF_COMMIT}/Copying.txt',
                          'license_sha256': digest(sf_license),
                          'cpu_variant': 'official generic Windows x86-64; no AVX2/AVX512 requirement selected'}
    report['stockfish']['uci_smoke'] = smoke([binary], {'Threads': 1, 'Hash': 16})
    print('Stockfish UCI smoke passed', flush=True)
    sun_dir = BUILD / 'sunfish'
    source_dir = sun_dir / 'source'
    files = []
    for relative in ['sunfish.py', 'sunfish_ui/uci.py', 'sunfish_ui/__init__.py', 'LICENSE.md']:
        files.append(download(f'https://raw.githubusercontent.com/thomasahle/sunfish/{SUN_COMMIT}/{relative}', source_dir / relative))
    runtime = sun_dir / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    interpreter = runtime / 'python.exe'
    cfg = runtime / 'pyvenv.cfg'
    shutil.copyfile(ROOT / '.venv-tests/Scripts/python.exe', interpreter)
    shutil.copyfile(ROOT / '.venv-tests/pyvenv.cfg', cfg)
    probe = subprocess.run([str(interpreter), '-c', 'import json,sys; print(json.dumps({"version":sys.version,"executable":sys.executable,"base_executable":sys._base_executable}))'],
                           cwd=runtime, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
    report['sunfish'] = {'name': 'Sunfish', 'version': 'sunfish 2026', 'revision': SUN_COMMIT,
                        'source_url': 'https://github.com/thomasahle/sunfish',
                        'license': 'GPL-3.0', 'license_url': f'https://github.com/thomasahle/sunfish/blob/{SUN_COMMIT}/LICENSE.md',
                        'source_files': files, 'binary_path': str(interpreter.relative_to(ROOT)),
                        'binary_sha256': digest(interpreter), 'pyvenv_cfg_sha256': digest(cfg),
                        'runtime_probe_exit_code': probe.returncode, 'runtime_probe_stdout': probe.stdout,
                        'runtime_probe_stderr': probe.stderr, 'runtime_scope': 'copied venv redirector and config depend on the recorded existing absolute base Python home; not a self-contained Python distribution'}
    assert probe.returncode == 0, (probe.returncode, probe.stdout, probe.stderr)
    report['sunfish']['uci_smoke'] = smoke([interpreter, '-u', source_dir / 'sunfish.py'], {'Hash': 16})
    print('Sunfish copied Python/UCI smoke passed', flush=True)
    report['status'] = 'PASS'
except BaseException as error:
    report['status'] = 'FAIL'
    report['error'] = f'{type(error).__name__}: {error}'
    raise
finally:
    (EVIDENCE / 'provenance.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
