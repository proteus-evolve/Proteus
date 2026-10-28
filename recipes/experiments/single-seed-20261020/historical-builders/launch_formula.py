"""Prepare all full Formula locks, then launch once; no retry/resubmission loop."""
import json,os
from pathlib import Path
import subprocess,sys
ROOT=Path(__file__).resolve().parent

if __name__=='__main__':
    os.umask(0o077)
    receipt=ROOT/'formula-launch-receipt.json'
    if receipt.exists():raise RuntimeError('Formula launch already recorded; never duplicate')
    commands=[]
    for mode in ('offline','online'):
        commands.append((f'linux-ace-formula-{mode}-v2',['--family','ace','--mode',mode]))
    for mode in ('offline','online'):
        for variant in ('ace','naive_parallel','combee_fixed','combee_dynamic'):
            commands.append((f'linux-combee-formula-{mode}-{variant}-v2',['--family','combee','--mode',mode,'--variant',variant]))
    for label,args in commands:
        subprocess.run([sys.executable,str(ROOT/'run_api_formal_v2.py'),*args,'--prepare-only'],check=True)
    rows=[]
    for label,args in commands:
        log=ROOT/'logs'/(label+'.log')
        with log.open('ab') as file:
            child=subprocess.Popen([sys.executable,str(ROOT/'run_api_formal_v2.py'),*args,'--execute-prepared'],
                stdin=subprocess.DEVNULL,stdout=file,stderr=subprocess.STDOUT,start_new_session=True)
        rows.append({'label':label,'pid':child.pid,'log':str(log)})
        receipt.write_text(json.dumps({'status':'launched_once_not_completed','conditions':rows},indent=2)+'\n')
    print(json.dumps({'formal_Formula_conditions_started':rows}),flush=True)
