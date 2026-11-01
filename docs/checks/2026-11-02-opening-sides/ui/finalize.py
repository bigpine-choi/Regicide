from pathlib import Path
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sources = ['godot/scripts/main.gd','godot/tests/opening_guide_tests.gd','godot/tests/opening_guide_capture.gd',
           'web/human-play/app.js','web/human-play/index.html','web/human-play/style.css',
           'tests/opening_guide_web_tests.js']
result = {'baseline_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
          'sources_normalized_lf_sha256': {name:hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in sources},
          'checks':{'browser_synthetic_cases':12,'browser_with_real_backend_case':13,'real_backend_rendered_positions':2,
                    'godot_scene_checks':51,'godot_scene_failures':0,'web_time_regression':'passed',
                    'godot_isolated_import':'passed','pixel_inspection':'final compact images inspected by UI agent; root inspected the prior name version'},
          'scope':'Actual client code; headless scene and hidden SubViewport render using actual backend opening data with fake engine peers.',
          'manual_play':False,'user_game_accessed':False,'source_freeze':True,
          'test_setup_notes':['First isolated import ended with native Windows 0xC0000005; explicit --quit import succeeded. Original log preserved.',
                              'Long-name scene assertion initially expected trailing whitespace trimmed by UI; assertion corrected.',
                              'Final fixtures use verified Chess.com names where the backend confirms them; empty ECO supplement display is covered.'],
          'evidence_files':{p.relative_to(OUT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='source-manifest.json'
                            and '__pycache__' not in p.parts}}
(OUT/'source-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'source_count':len(sources),'evidence_files':len(result['evidence_files']),'status':'frozen'}))
