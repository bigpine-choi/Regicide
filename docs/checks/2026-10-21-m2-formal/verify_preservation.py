"""Read-only check of native bytes and the two original dirty checkouts."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent
def sha(data):return hashlib.sha256(data).hexdigest()
def git(path,*args):return subprocess.run(['git','-C',str(path),*args],capture_output=True,check=True).stdout
def check():
 before=json.loads((HERE/'native-before.json').read_text())
 native={name:sha((ROOT/name).read_bytes()) for name in before['native_source_files']}
 binary=sha((ROOT/'build/regicide.exe').read_bytes())
 originals={}
 originals_before=json.loads((HERE/'originals-before.json').read_text())
 for key in ('research','installed'):
  previous=originals_before[key]
  path=Path(previous['path'])
  raw=git(path,'status','--porcelain','-z')
  pieces=raw.split(b'\0');items=[];i=0
  while i<len(pieces):
   entry=pieces[i];i+=1
   if not entry:continue
   status=entry[:2].decode();name=entry[3:].decode('utf-8')
   if 'R' in status or 'C' in status:i+=1
   items.append({'status':status,'path':name,'sha256':sha((path/name).read_bytes()) if (path/name).is_file() else None})
  head=git(path,'rev-parse','HEAD').decode().strip()
  raw_hashes={'porcelain-z':sha(raw),'porcelain':sha(git(path,'status','--porcelain'))}
  originals[key]={'path':str(path),'head':head,'head_unchanged':head==previous['head'],
   'dirty_items_unchanged':sorted(items,key=lambda x:x['path'])==sorted(previous['items'],key=lambda x:x['path']),
   'previous_raw_hash':previous['status_raw_sha256'],'current_raw_hashes':raw_hashes,
   'raw_hash_match':previous['status_raw_sha256'] in raw_hashes.values(),'items':items}
 passed=(native==before['native_source_files'] and binary==before['engine_binary_sha256']
         and all(x['head_unchanged'] and x['dirty_items_unchanged'] for x in originals.values()))
 return {'status':'PASS' if passed else 'FAIL','verified_utc':datetime.now(timezone.utc).isoformat(),
  'native_source_files_unchanged':native==before['native_source_files'],'native_source_files':native,
  'engine_binary_unchanged':binary==before['engine_binary_sha256'],'engine_binary_sha256':binary,
  'originals':originals,'scope':'Read-only bytes, Git heads and dirty path/status/content comparison. No native rebuild or GUI/manual regression rerun.'}

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
 result=check()
 with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2);stream.write('\n')
 print(json.dumps({k:result[k] for k in ('status','native_source_files_unchanged','engine_binary_unchanged')}))
 raise SystemExit(0 if result['status']=='PASS' else 2)
