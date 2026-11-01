"""Owned production CLI/HTTP check; never contacts the user's running game."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='regicide-opening-sides-owned-') as folder:
    temporary = Path(folder)
    startup_file = temporary/'startup.json'
    command = [sys.executable, str(ROOT/'scripts/play_human.py'), '--no-browser', '--desktop-client',
               '--classical', '--port', '0', '--save-dir', str(temporary/'games'),
               '--startup-file', str(startup_file)]
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with (temporary/'stdout.txt').open('w', encoding='utf-8') as stdout, (temporary/'stderr.txt').open('w', encoding='utf-8') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **options)
        startup = None
        def request(path, payload=None):
            headers = {'X-Regicide-Token': startup['token'], 'Origin': startup['url']}
            if payload is not None:
                headers['Content-Type'] = 'application/json'
            data = json.dumps(payload).encode() if payload is not None else None
            with urllib.request.urlopen(urllib.request.Request(startup['url']+path, data=data, headers=headers), timeout=15) as response:
                return json.loads(response.read())
        try:
            deadline = time.monotonic()+15
            while not startup_file.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Owned backend exited before startup')
                time.sleep(.05)
            startup = json.loads(startup_file.read_text(encoding='utf-8'))
            initial = request('/api/state')
            assert initial['opening_guide']['sides']['white']['status'] == 'pending'
            assert initial['opening_guide']['sides']['black']['status'] == 'pending'
            human = request('/api/move', {'move':'e2e4', 'revision':initial['revision']})
            assert human['opening_guide']['sides']['white']['name'] == "King's Pawn Opening"
            assert human['opening_guide']['sides']['black']['status'] == 'pending'
            reply = request('/api/engine', {'revision':human['revision']})
            assert not reply.get('error'), reply.get('error')
            black = reply['opening_guide']['sides']['black']
            assert black['name'] or black['detail'].startswith('확인한 대응:')
            restored = request('/api/undo', {'revision':reply['revision']})
            assert not restored.get('error'), restored.get('error')
            assert restored['opening_guide']['sides'] == initial['opening_guide']['sides']
            result = {'status':'passed','scope':'Owned production CLI and local HTTP; temporary game; no manual GUI',
                      'binary_sha256':startup['binary_sha256'], 'checks':['initial pending sides','e4 white name and black pending',
                      'real engine reply has black response','undo clears both names'],
                      'states':{label:state['opening_guide'] for label,state in
                                [('initial',initial),('after_e4',human),('after_reply',reply),('after_undo',restored)]}}
            Path(__file__).with_name('production-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'status':result['status'],'checks':result['checks']}))
        finally:
            if startup is not None and process.poll() is None:
                try:
                    state = request('/api/state')
                    request('/api/shutdown', {'revision':state['revision']})
                except Exception:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,check=True)
                else:
                    process.kill()
                process.wait(timeout=5)
            if process.returncode != 0:
                raise RuntimeError('Owned backend did not exit cleanly: '+str(process.returncode))
