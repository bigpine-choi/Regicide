import json,os,subprocess,sys,time
from pathlib import Path
if sys.argv[1]=="child":
 time.sleep(60)
else:
 while not Path(sys.argv[2]).exists(): time.sleep(.01)
 p=subprocess.Popen([sys.executable,__file__,"child"])
 Path(sys.argv[3]).write_text(json.dumps({"child_pid":p.pid,"parent_pid":os.getpid()}))
