import json, sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'scripts'))
from see_clean_guard import EngineCpuGuard,WindowsProcesses
folder=Path('build/see-idle-observation-001')
folder.mkdir()
guard=EngineCpuGuard(WindowsProcesses(),folder/'idle.jsonl')
passed=False
try:
    guard.start()
    guard.require_idle(30)
    passed=True
except Exception as exc:
    print(type(exc).__name__+': '+str(exc),flush=True)
finally:
    guard.close()
    (folder/'idle.json').write_text(json.dumps(guard.summary,indent=2)+'\n',encoding='utf-8')
print('THIRTY_SECOND_IDLE='+str(passed),flush=True)
sys.exit(0 if passed else 2)
