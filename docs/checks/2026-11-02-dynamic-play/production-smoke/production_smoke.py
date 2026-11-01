"""Start only an owned temporary backend, verify production policy, then close it."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='regicide-dynamic-launch-') as folder:
    temporary = Path(folder)
    startup_file = temporary / 'startup.json'
    command = [sys.executable, str(ROOT / 'scripts/play_human.py'), '--no-browser', '--desktop-client',
               '--classical', '--port', '0', '--save-dir', str(temporary/'games'),
               '--startup-file', str(startup_file)]
    extra = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with (temporary/'stdout.txt').open('w',encoding='utf-8') as stdout, (temporary/'stderr.txt').open('w',encoding='utf-8') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, **extra)
        startup = None
        def request(path, payload=None):
            headers = {'X-Regicide-Token': startup['token'], 'Origin': startup['url']}
            if payload is not None:
                headers['Content-Type'] = 'application/json'
            data = json.dumps(payload).encode() if payload is not None else None
            with urllib.request.urlopen(urllib.request.Request(startup['url']+path, data=data, headers=headers), timeout=12) as response:
                return json.loads(response.read())
        try:
            deadline = time.monotonic()+15
            while not startup_file.exists() and time.monotonic()<deadline:
                if process.poll() is not None:
                    raise RuntimeError('Owned backend exited before startup')
                time.sleep(.05)
            startup = json.loads(startup_file.read_text(encoding='utf-8'))
            state = request('/api/state')
            assert state['engine']['dynamic_play_enabled'] is True
            assert state['engine']['evaluation'] == 'classical_activity_v1'
            assert state['engine']['extra_budget_schedule'] == [
                {'until_ply':16,'budget_ms':1000}, {'until_ply':80,'budget_ms':500}]
            after_human = request('/api/move', {'move':'e2e4','revision':state['revision']})
            after_engine = request('/api/engine', {'revision':after_human['revision']})
            assert not after_engine.get('error'), after_engine.get('error')
            record = json.loads(next((temporary/'games').glob('*.json')).read_text(encoding='utf-8'))
            decision = record['dynamic_play']['decisions'][0]
            assert decision['selection_rule'] == 'opening_variety_preserved'
            assert decision['selected_uci'] == decision['opening_receipt']['selected_uci']
            assert len(record['uci_moves']) == 2
            assert '[DynamicPlayPolicy "regicide-dynamic-play-v1"]' in next((temporary/'games').glob('*.pgn')).read_text(encoding='utf-8')
            result = {'status':'passed','scope':'Real production CLI and local HTTP; one owned temporary game; no GUI or personal archive',
                      'binary_sha256':startup['binary_sha256'],'evaluation':state['engine']['evaluation'],
                      'dynamic_play_enabled':True,'schedule':state['engine']['extra_budget_schedule'],
                      'moves':record['uci_moves'],'decision_rule':decision['selection_rule'],
                      'opening_v3_selection_preserved':True}
            Path(__file__).with_name('production-smoke.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(result))
        finally:
            if startup is not None and process.poll() is None:
                try:
                    current = request('/api/state')
                    request('/api/shutdown', {'revision':current['revision']})
                except Exception:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Exact process created above only; never discover or target a user session.
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,check=True)
                else:
                    process.kill()
                process.wait(timeout=5)
            if process.returncode != 0:
                raise RuntimeError('Owned backend shutdown failed: '+str(process.returncode))
