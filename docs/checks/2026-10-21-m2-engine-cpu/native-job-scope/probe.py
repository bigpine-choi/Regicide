"""One owned gated worker: real Job scope, three 100 ms legal endpoint moves."""
import argparse
import ctypes as C
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'scripts'))
import chess
import chess.engine
from engine_profile import load_profile,compare_arguments
from m2_engine_cpu import EngineCpuMeter
import m2_engine_identity_v2 as identity
from m2_job_accounting import OwnedJob


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    with Path(path).open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')


def worker(folder):
    gate=folder/'gate.json'
    limit=time.monotonic()+30
    while not gate.exists():
        if time.monotonic()>limit:raise TimeoutError('parent did not gate owned worker')
        time.sleep(.025)
    declared=json.loads(gate.read_text())
    if declared['worker_pid']!=os.getpid() or declared['coordinator_pid']!=os.getppid():
        raise RuntimeError('gate worker/coordinator identity differs')
    plan=json.loads((ROOT/'docs/experiments/2026-10-21-m2-engine-cpu-plan.json').read_text())
    runtime=plan['worker_runtime']['executable']
    cases=[('regicide',ROOT/'config/engine-profiles/classical.json'),
           ('stockfish',ROOT/'build-m2-opponents/profiles/stockfish18.json'),
           ('sunfish',ROOT/'build-m2-opponents/profiles/sunfish.json')]
    report={'schema_version':1,'status':'RUNNING','worker_pid':os.getpid(),'coordinator_pid':os.getppid(),
            'gate':declared,'formal_games':0,'functional_moves':0,'cases':[]}
    try:
        for label,path in cases:
            entry={'label':label,'profile':str(path),'profile_sha256':digest(path),'status':'RUNNING'}
            report['cases'].append(entry)
            profile=load_profile(path)
            command=[profile['binary']['path'],*profile['args']]
            entry['literal_command']=command
            engine=transport_meter=endpoint_meter=None
            try:
                # Match the formal worker's default python-chess child flags.
                engine=chess.engine.SimpleEngine.popen_uci(command,timeout=10.)
                entry['options']=compare_arguments(profile)['options']
                engine.configure(entry['options'])
                transport_meter=EngineCpuMeter(engine.transport.get_pid())
                created=transport_meter.initial_read['creation_ticks']
                resolved=identity.resolve_engine_endpoint(engine.transport.get_pid(),created,profile,runtime)
                entry['resolution']=resolved
                entry['scope_audit']=identity.validate_resolution_scope(resolved,expected_worker_pid=os.getpid())
                if label=='regicide':
                    # Synthesize only the parent-PID field of an existing
                    # outside-Job process; all Job/native identities stay real.
                    members=set(resolved['endpoint_job_scope']['job_snapshots'][0]['pids'])
                    rows={row['pid']:row for row in resolved['endpoint_job_scope']['global_inventories'][0]}
                    candidates=[pid for pid in (1396,1612,4) if pid in rows and pid not in members]
                    if not candidates:raise RuntimeError('no declared outside-Job process available for synthetic edge')
                    outside=candidates[0]
                    original=identity._inventory
                    inventories=[]
                    def injected(api):
                        actual=original(api)
                        inventories.append({'original_parent_row':dict(actual[outside]),'synthetic_parent_pid':engine.transport.get_pid()})
                        actual[outside]=dict(actual[outside],parent_pid=engine.transport.get_pid())
                        return actual
                    api=identity._api(None)
                    opens=[]
                    class Proxy:
                        def __getattr__(self,name):return getattr(api,name)
                    proxy=Proxy()
                    def opened(access,inherit,pid):
                        opens.append({'pid':int(pid),'access_mask':int(access)})
                        if pid==outside:raise AssertionError('outside-Job PID was opened')
                        return api.OpenProcess(access,inherit,pid)
                    proxy.OpenProcess=opened
                    with patch.object(identity,'_inventory',side_effect=injected):
                        synthetic=identity.resolve_engine_endpoint(engine.transport.get_pid(),created,profile,runtime,_kernel=proxy)
                    entry['synthetic_outside_edge']={'synthetic_only':True,'historical_reproduction_claim':False,
                        'pid':outside,'modified_parent_rows':inventories,'native_openprocess_calls':opens,
                        'outside_pid_opened':any(row['pid']==outside for row in opens),
                        'resolution':synthetic,'scope_audit':identity.validate_resolution_scope(synthetic,expected_worker_pid=os.getpid())}
                endpoint=resolved['endpoint']
                endpoint_meter=EngineCpuMeter(endpoint['pid'],endpoint['creation_ticks'])
                board=chess.Board()
                reply,cpu=endpoint_meter.measure_play(engine.play,board,chess.engine.Limit(time=.1))
                if reply.move is None or reply.move not in board.legal_moves:raise RuntimeError('illegal probe move')
                entry['move']={'fen':chess.STARTING_FEN,'requested_seconds':.1,'move':reply.move.uci(),
                               'legal':True,'cpu_receipt':cpu}
                report['functional_moves']+=1
                entry['status']='PASS'
            finally:
                if engine is not None:
                    try:engine.quit();entry['graceful_quit']=True
                    finally:engine.close()
                for name,meter in (('endpoint',endpoint_meter),('transport',transport_meter)):
                    if meter is not None:
                        try:entry[name+'_final_cpu']=meter.read()
                        finally:meter.close();entry[name+'_meter_closed']=True
        report['status']='PASS'
    except BaseException as error:
        report.update(status='FAIL',error={'type':type(error).__name__,'message':str(error),
            'traceback':traceback.format_exc(),'identity_receipt':getattr(error,'identity_receipt',None)})
        raise
    finally:write(folder/'worker-receipt.json',report)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--worker',type=Path);args=parser.parse_args()
    if args.worker:return worker(args.worker)
    folder=Path(__file__).parent/'attempt-001';folder.mkdir(exist_ok=False)
    planpath=ROOT/'docs/experiments/2026-10-21-m2-engine-cpu-plan.json'
    plan=json.loads(planpath.read_text());runtime=plan['worker_runtime']['executable']
    sources=[Path(__file__),ROOT/'scripts/m2_engine_identity_v2.py',ROOT/'scripts/m2_engine_identity.py',
             ROOT/'scripts/m2_engine_cpu.py',ROOT/'scripts/m2_job_accounting.py']
    report={'schema_version':1,'status':'RUNNING','started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'formal_games':0,'formal_load_validation':False,'playing_strength_claim':False,
            'source_sha256':{str(path.relative_to(ROOT)):digest(path) for path in sources},
            'runtime':runtime,'coordinator_pid':os.getpid(),
            'scope':'One newly created owned Job and one gated direct-base-Python worker; no user apps or services changed.'}
    job=OwnedJob();process=None
    try:
        command=[runtime['path'],'-s','-B',str(Path(__file__).resolve()),'--worker',str(folder)]
        report['command']=command
        environment=dict(os.environ,PYTHONPATH=str(ROOT/'.venv-tests/Lib/site-packages'),
                         PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
        with (folder/'worker.log').open('x',encoding='utf-8') as log:
            process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=environment,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            assigned=job.assign(process);report['assignment']=assigned
            write(folder/'gate.json',{'worker_pid':process.pid,'creation_ticks':assigned['creation_ticks'],
                                      'coordinator_pid':os.getpid()})
            report['worker_returncode']=process.wait(timeout=90)
        if report['worker_returncode']!=0:raise RuntimeError('native worker failed; preserve raw worker receipt/log')
        result=json.loads((folder/'worker-receipt.json').read_text())
        if result['status']!='PASS' or result['functional_moves']!=3:raise RuntimeError('native scope cases incomplete')
        report['status']='PASS';report['functional_moves']=result['functional_moves']
    except BaseException as error:
        report.update(status='FAIL',error={'type':type(error).__name__,'message':str(error),'traceback':traceback.format_exc()})
        raise
    finally:
        try:
            report['final_job_accounting']=job.terminate_and_snapshot(5.)
            if process is not None:report['final_worker_returncode']=process.wait(timeout=5)
            report['cleanup']={'active_owned_processes':report['final_job_accounting']['active_processes'],
                               'unknown_owned_processes':0 if report['final_job_accounting']['active_processes']==0 else None}
        finally:
            job.close();report['job_closed']=True
            report['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
            write(folder/'receipt.json',report)


if __name__=='__main__':main()
