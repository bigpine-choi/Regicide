"""Bounded native schema-4 guard diagnostics on newly created own processes."""
import argparse
from dataclasses import asdict, replace
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'scripts'))
import chess
import chess.engine
from engine_profile import load_profile,compare_arguments
from m2_engine_cpu import EngineCpuMeter
from m2_job_accounting import OwnedJob
from m2_load_guard import LoadPolicy,LoadGuardViolation
from m2_warning_load_guard_v2 import WarningGuardMonitor,ScopedJobAccountingProvider
from m2_warning_receipts_v2 import validate_job_receipts


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    with Path(path).open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')


def worker(folder,lane):
    gate=folder/f'gate-{lane}.json';deadline=time.monotonic()+30
    while not gate.exists():
        if time.monotonic()>deadline:raise TimeoutError('owned worker gate not published')
        time.sleep(.025)
    params=json.loads(gate.read_text())
    if params['worker_pid']!=os.getpid() or params['coordinator_pid']!=os.getppid():raise RuntimeError('gate identity differs')
    report={'worker_pid':os.getpid(),'coordinator_pid':os.getppid(),'lane':lane,'moves':[],'status':'RUNNING'}
    try:
        if params['mode']=='idle':
            stop=folder/'stop-idle.json';deadline=time.monotonic()+30
            while not stop.exists():
                if time.monotonic()>deadline:raise TimeoutError('idle worker stop not published')
                time.sleep(.025)
        else:
            for index in range(4):
                engine=meter=None
                try:
                    engine=chess.engine.SimpleEngine.popen_uci(params['engine_command'],timeout=5.)
                    engine.configure(params['engine_options'])
                    meter=EngineCpuMeter(engine.transport.get_pid())
                    board=chess.Board()
                    reply,cpu=meter.measure_play(engine.play,board,chess.engine.Limit(time=.05))
                    if reply.move is None or reply.move not in board.legal_moves:raise RuntimeError('illegal diagnostic move')
                    report['moves'].append(dict(index=index,move=reply.move.uci(),legal=True,cpu=cpu))
                finally:
                    if engine is not None:
                        try:engine.quit()
                        finally:engine.close()
                    if meter is not None:meter.close()
        report['status']='PASS'
    except BaseException as error:
        report.update(status='FAIL',error=f'{type(error).__name__}: {error}');raise
    finally:write(folder/f'worker-{lane}.json',report)


def instrument(provider, report, parent_getter, *, protected=True, outside_pid=None):
    """Only rewrite PPID metadata; every CPU/Job query remains native."""
    scoped=provider.scoped
    original=scoped._inventory
    target_set=set()
    def inventory(native):
        rows=original(native)
        parent=parent_getter()
        if parent is None:return rows
        modifications=[]
        if protected:
            members=set((scoped.last_scope or {}).get('job_before',{}).get('pids',[]))
            targets=[pid for pid in (34924,1396,1612,4) if pid in rows and pid not in members]
            target_set.update(targets)
            for pid in targets:
                modifications.append(dict(original=asdict(rows[pid]),synthetic_parent_pid=parent))
                rows[pid]=replace(rows[pid],parent_pid=parent)
        if outside_pid is not None and outside_pid in rows:
            modifications.append(dict(original=asdict(rows[outside_pid]),synthetic_parent_pid=parent))
            rows[outside_pid]=replace(rows[outside_pid],parent_pid=parent)
        if modifications:report['synthetic_inventory_modifications'].append(modifications)
        return rows
    scoped._inventory=inventory
    original_open=scoped.native.kernel.OpenProcess
    def opened(access,inherit,pid):
        report['native_openprocess_calls'].append(dict(pid=int(pid),access_mask=int(access)))
        if protected and pid in target_set:raise AssertionError('outside protected metadata match was opened')
        return original_open(access,inherit,pid)
    # The original callable retains its ctypes signatures. The Python wrapper
    # changes only this instance's attribute and delegates with native values.
    scoped.native.kernel.OpenProcess=opened
    report['synthetic_only']=True
    report['historical_reproduction_claim']=False
    return target_set


def run_case(folder,mode,params,runtime):
    folder.mkdir()
    report={'mode':mode,'status':'RUNNING','synthetic_inventory_modifications':[],
            'native_openprocess_calls':[],'workers':[],'formal_games':0}
    job=OwnedJob();guard=None;workers=[];outside=None;outside_meter=None;parent=[None];attached=set()
    policy=LoadPolicy(.2)
    def launch_worker(lane):
        command=[runtime['path'],'-s','-B',str(Path(__file__).resolve()),'--worker',str(folder),'--lane',str(lane)]
        environment=dict(os.environ,PYTHONPATH=str(ROOT/'.venv-tests/Lib/site-packages'),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
        log=(folder/f'worker-{lane}.log').open('x',encoding='utf-8')
        process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=environment,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        workers.append((process,log,lane))
        assignment=job.assign(process)
        report['workers'].append(dict(pid=process.pid,command=command,assignment=assignment))
        if parent[0] is None:parent[0]=process.pid
    try:
        if mode=='external-negative':
            # Make the outside engine newer than its synthesized owned parent,
            # so birth ordering alone could not prevent a false ownership claim.
            launch_worker(0)
            outside=chess.engine.SimpleEngine.popen_uci(params['engine_command'],timeout=5.)
            outside.configure(params['engine_options'])
            outside_meter=EngineCpuMeter(outside.transport.get_pid())
            report['outside_endpoint_initial']=outside_meter.initial_read
        provider=ScopedJobAccountingProvider(policy,job)
        protected=instrument(provider,report,lambda:parent[0],protected=mode=='owned-burst',
            outside_pid=None if outside is None else outside.transport.get_pid())
        guard=WarningGuardMonitor(folder/'load.jsonl',policy,job,provider)
        guard.require_idle(5.)
        guard.start_timer()
        if mode=='owned-burst':
            for lane in range(2):launch_worker(lane)
        # Intentionally expose assigned but not-yet-registered gated workers.
        time.sleep(.6)
        guard.sample('pending-gated-workers');guard.assert_clean()
        for process,_log,lane in workers:
            identity=guard.identify_worker(process.pid);guard.attach_worker(*identity)
            attached.add(process.pid)
            write(folder/f'gate-{lane}.json',dict(params,mode='burst' if mode=='owned-burst' else 'idle',
                worker_pid=process.pid,coordinator_pid=os.getpid()))
        if mode=='owned-burst':
            limit=time.monotonic()+30
            remaining=list(workers)
            while remaining:
                guard.assert_clean()
                for item in list(remaining):
                    process,_log,_lane=item
                    if process.poll() is not None:
                        if process.returncode!=0:raise RuntimeError('owned burst worker failed')
                        guard.sample('worker-completed');guard.assert_clean();guard.detach_worker(process.pid)
                        attached.discard(process.pid)
                        remaining.remove(item)
                if time.monotonic()>limit:raise TimeoutError('bounded burst did not finish')
                time.sleep(.1)
            time.sleep(1.1)  # Retired exact objects get two absent inventories.
            guard.sample('post-retirement');guard.assert_clean()
            report['status']='PASS'
        else:
            board=chess.Board()
            reply,cpu=outside_meter.measure_play(outside.play,board,chess.engine.Limit(time=.1))
            report['outside_move']=dict(move=reply.move.uci(),legal=reply.move in board.legal_moves,cpu=cpu)
            # The outside identity was baselined while idle. It must remain
            # external despite its synthesized parent matching an owned worker.
            try:guard.sample('outside-owned-diagnostic-engine-cpu')
            except LoadGuardViolation:
                if guard.summary.get('failure',{}).get('kind')!='external_cpu':raise
            failure=guard.summary.get('failure')
            report['expected_failure']=failure
            if failure is None or failure.get('kind')!='external_cpu':raise RuntimeError('external CPU hard check failed to fire')
            report['status']='PASS_EXPECTED_EXTERNAL_CPU_FAILURE'
        report['protected_pid_targets']=sorted(protected)
        report['protected_pid_opened']=any(row['pid'] in protected for row in report['native_openprocess_calls'])
        if mode=='owned-burst' and (not protected or report['protected_pid_opened']):
            raise RuntimeError('outside protected-parent metadata test lacked a target or queried one')
    except BaseException as error:
        report.update(status='FAIL',error={'type':type(error).__name__,'message':str(error),'traceback':traceback.format_exc()});raise
    finally:
        if mode=='external-negative' and not (folder/'stop-idle.json').exists():write(folder/'stop-idle.json',{'stop':True})
        for process,_log,_lane in workers:
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:pass
        if guard is not None:
            for process,_log,_lane in workers:
                if process.poll() is not None and process.pid in attached:
                    try:guard.detach_worker(process.pid);attached.discard(process.pid)
                    except Exception as error:report.setdefault('cleanup_notes',[]).append(str(error))
        report['final_job_accounting']=job.terminate_and_snapshot(5.)
        for process,log,_lane in workers:
            process.wait(timeout=5);log.close()
        if guard is not None:
            guard.close();report['guard_summary']=guard.summary
            write(folder/'load-summary.json',guard.summary)
        job.close();report['job_closed']=True
        if outside is not None:
            try:outside.quit()
            finally:outside.close()
            report['outside_final_cpu']=outside_meter.read();outside_meter.close();report['outside_meter_closed']=True
        if guard is not None:
            try:
                report['raw_audit']=validate_job_receipts(folder/'load.jsonl',guard.summary,report['final_job_accounting'],
                    dict(asdict(policy),idle_probe_seconds=5.))
            except Exception as error:
                report['raw_audit_error']=f'{type(error).__name__}: {error}'
                report['status']='FAIL_RAW_AUDIT'
        report['cleanup']=dict(active_owned_processes=report['final_job_accounting']['active_processes'],
                              unknown_owned_processes=0 if report['final_job_accounting']['active_processes']==0 else None)
        write(folder/'receipt.json',report)
    return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--worker',type=Path);parser.add_argument('--lane',type=int,default=0);args=parser.parse_args()
    if args.worker:return worker(args.worker,args.lane)
    folder=Path(__file__).parent/'attempt-001';folder.mkdir(exist_ok=False)
    plan=json.loads((ROOT/'docs/experiments/2026-10-21-m2-engine-cpu-plan.json').read_text())
    profile=load_profile(ROOT/'config/engine-profiles/classical.json')
    params=dict(engine_command=[profile['binary']['path'],*profile['args']],engine_options=compare_arguments(profile)['options'])
    sources=[Path(__file__),ROOT/'scripts/m2_job_process_scope.py',ROOT/'scripts/m2_warning_load_guard_v2.py',
             ROOT/'scripts/m2_warning_receipts_v2.py',ROOT/'scripts/m2_load_guard.py',ROOT/'scripts/see_clean_guard.py']
    report=dict(schema_version=1,status='RUNNING',formal_games=0,playing_strength_claim=False,
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        source_sha256={str(path.relative_to(ROOT)):sha(path) for path in sources},cases=[])
    try:
        for name in ('owned-burst','external-negative'):
            value=run_case(folder/name,name,params,plan['worker_runtime']['executable'])
            report['cases'].append(dict(name=name,status=value['status']))
            expected='PASS' if name=='owned-burst' else 'PASS_EXPECTED_EXTERNAL_CPU_FAILURE'
            if value['status']!=expected or 'raw_audit_error' in value:
                raise RuntimeError('native case or raw replay did not pass its declared positive/negative expectation')
            if name=='owned-burst' and not value.get('raw_audit',{}).get('valid_for_load_check'):
                raise RuntimeError('owned burst did not finish with a valid guard/raw replay')
        report['status']='PASS'
    except BaseException as error:
        report.update(status='FAIL',error={'type':type(error).__name__,'message':str(error),'traceback':traceback.format_exc()})
        raise
    finally:
        report['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        write(folder/'receipt.json',report)


if __name__=='__main__':main()
