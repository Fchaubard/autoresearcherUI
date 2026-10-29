#!/usr/bin/env python3
"""Opt-in OpenCode/Flint coding-role probes with actual interactive CLI tools.

All work and the OpenCode DB stay in temporary directories. Synthetic tasks
exercise read/write/test tools; no live research tree or GPU job is touched.
Results contain approved metadata only, never CLI output or credentials.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app import agent_cli, opencode_runtime
from backend.app.model_registry import MODELS, provider_for
from backend.app.provider_config import requirement_error
from scripts.probe_advisor_roles import load_credentials

ROLES = ('research', 'author')
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def classify_error(text):
    plain = ANSI.sub('', text).lower()
    if any(x in plain for x in ('usage limit', 'rate limit', 'quota', 'too many requests', 'status: 429', 'statuscode: 429')):
        return {'status': 'provider_error', 'http_status': 429, 'error_category': 'quota_exhausted'}
    if re.search(r'\bmodel\b[^\n]{0,100}\b(?:unavailable|not found)\b', plain):
        return {'status': 'provider_error', 'error_category': 'model_unavailable'}
    if 'authentication' in plain or 'unauthorized' in plain or 'invalid api key' in plain:
        return {'status': 'provider_error', 'error_category': 'authentication'}
    return None


def task(role, folder):
    (folder/'AGENTS.md').write_text('Only work inside this temporary directory. Do not access other paths, credentials, network, GPUs, or subprocess services. Complete only the requested synthetic fixture task.\n')
    (folder/'fixture.json').write_text(json.dumps({'predictions':[1,3], 'targets':[2,2]}))
    if role == 'research':
        (folder/'test_solution.py').write_text('import unittest\nfrom solution import mse\nclass TestMSE(unittest.TestCase):\n def test_fixture(self): self.assertEqual(mse([1,3],[2,2]),1.0)\n def test_zero(self): self.assertEqual(mse([1,2],[1,2]),0.0)\n def test_fraction(self): self.assertEqual(mse([0,1],[0,2]),0.5)\nif __name__ == "__main__": unittest.main()\n')
        return ('You are the research coding agent in a temporary isolated test project. Read AGENTS.md and fixture.json. '
                'Create solution.py implementing mse(predictions, targets), the mean squared error. '
                'Run python3 -m unittest test_solution.py and fix any failures. '
                'Write result.json containing {"role":"research","mse":1.0,"tests_passed":true}. '
                'Use your file tools and terminal; do not merely describe the files. Then say TASK_COMPLETE. Stay in this directory only.')
    return ('You are the paper author coding agent in a temporary isolated test project. Read AGENTS.md and fixture.json. '
            'Create appendix.tex with a LaTeX section named Synthetic validation, describing the two predictions and targets and correctly computed mean squared error 1.0. '
            'Create paper-check.json containing {"role":"author","mse":1.0,"observations":2}. '
            'Use your file tools; do not merely describe the files. Then say TASK_COMPLETE. Stay in this directory only.')


def verify(role, folder):
    try:
        if role == 'research':
            data=json.loads((folder/'result.json').read_text())
            if data != {'role':'research','mse':1.0,'tests_passed':True}: return False
            if not (folder/'solution.py').is_file(): return False
            check=subprocess.run([sys.executable,'-m','unittest','test_solution.py'],cwd=folder,env={'PATH':os.environ['PATH']},capture_output=True,timeout=5)
            return check.returncode == 0
        data=json.loads((folder/'paper-check.json').read_text())
        text=(folder/'appendix.tex').read_text()
        return data == {'role':'author','mse':1.0,'observations':2} and '\\section' in text and '1.0' in text
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return False


def session_evidence(folder, model):
    """Verify the runtime selected the requested workspace/model, not defaults."""
    target=folder/'opencode.db'
    if not target.exists():return {}
    try:
        with sqlite3.connect(target.as_uri()+'?mode=ro',uri=True) as db:
            rows=db.execute('SELECT directory,model FROM session_v2').fetchall()
            models=[]
            for directory,raw in rows:
                if Path(directory).resolve()!=folder.resolve():
                    return {'isolation_error':True}
                value=json.loads(raw)
                chosen=value.get('providerID','')+'/'+value.get('id','')
                if chosen!=model:return {'model_mismatch':True}
                models.append(chosen)
            tools=[]
            for raw, in db.execute('SELECT data FROM session_message'):
                for part in json.loads(raw).get('content',[]):
                    if part.get('type')=='tool' and part.get('state',{}).get('status')=='completed':
                        tools.append(part.get('name'))
            return {'observed_models':sorted(set(models)),'completed_tools':sorted(set(tools)),
                    'workspace_verified':bool(rows)}
    except (sqlite3.Error,ValueError,TypeError):return {}


def probe_coding(model, role, timeout=120):
    began=time.monotonic()
    result={'model':model,'provider':provider_for(model),'role':role,'tested_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'inference':'real','fallback_allowed':False,'transport':'agent_cli.command/mini/pty','timeout_seconds':timeout}
    error=requirement_error(model,agent=True)
    if error:
        return dict(result,status='not_configured',error_category='missing_provider_configuration',elapsed_seconds=round(time.monotonic()-began,3))
    with tempfile.TemporaryDirectory(prefix='arui-coding-') as temp:
        folder=Path(temp)
        prompt=task(role,folder)
        provider,command=agent_cli.command(model,prompt=prompt)
        env={**os.environ,**opencode_runtime.agent_environment(model, runtime_dir=str(folder/'runtime')),'OPENCODE_DB':str(folder/'opencode.db'),
             'PWD':str(folder),
             'OPENCODE_DISABLE_PROJECT_CONFIG':'1','ARUI_DISABLE_BG':'1','ARUI_TELEMETRY_DISABLED':'1',
             'ARUI_DATA_DIR':str(folder/'data'),'ARUI_WORKSPACE_DIR':str(folder),'TMUX':'','TMUX_TMPDIR':str(folder/'tmux'),
             'TERM':'xterm-256color','NO_COLOR':'1'}
        # Only the selected provider can access its credential in the child.
        for key in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','GEMINI_API_KEY','GOOGLE_API_KEY'):
            env.pop(key,None)
        (folder/'tmux').mkdir(mode=0o700)
        master,slave=pty.openpty()
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,0,0))
        proc=subprocess.Popen(shlex.split(command),stdin=slave,stdout=slave,stderr=slave,cwd=folder,env=env,start_new_session=True)
        os.close(slave)
        output=''
        result.update(dispatched_models=[model],status='timeout',tool_task_passed=False)
        try:
            while time.monotonic()-began < timeout:
                ready,_,_=select.select([master],[],[],0.25)
                if ready:
                    try: raw=os.read(master,65536)
                    except OSError: raw=b''
                    output += raw.decode(errors='replace')
                    if len(output)>500000: output=output[-500000:]
                    error=classify_error(output)
                    if error:
                        result.update(error);break
                evidence=session_evidence(folder,model)
                result.update(evidence)
                if evidence.get('isolation_error') or evidence.get('model_mismatch'):
                    result['status']='isolation_failed';break
                if verify(role,folder):
                    result.update(status='passed' if evidence.get('workspace_verified') else 'unverified_workspace',tool_task_passed=True);break
                if proc.poll() is not None:
                    result.update(status='cli_exited',exit_code=proc.returncode);break
            result['created_files']=[name for name in ('solution.py','result.json','appendix.tex','paper-check.json') if (folder/name).is_file()]
            result['file_sha256']={name:hashlib.sha256((folder/name).read_bytes()).hexdigest()
                                   for name in result['created_files'] if (folder/name).stat().st_size < 1000000}
        finally:
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait(timeout=5);os.close(master)
    result['elapsed_seconds']=round(time.monotonic()-began,3)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',action='append')
    parser.add_argument('--role',action='append',choices=ROLES)
    parser.add_argument('--credentials-db')
    parser.add_argument('--timeout',type=int,default=120)
    parser.add_argument('--output',default='/tmp/arui-model-matrix/opencode-coding-results.json')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    if args.credentials_db:load_credentials(args.credentials_db)
    models=args.model or [m['id'] for m in MODELS if m['provider'] in {'opencode','flint'}]
    roles=args.role or ROLES
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    results=json.loads(output.read_text())['results'] if args.resume and output.exists() else []
    done={(r['model'],r['role']) for r in results}
    for model in models:
        for role in roles:
            if (model,role) in done:continue
            row=probe_coding(model,role,args.timeout);results.append(row)
            temp=output.with_suffix('.tmp');temp.write_text(json.dumps({'selected_models':models,'roles':roles,'results':results},indent=2));temp.replace(output)
            print(json.dumps(row),flush=True)
    return 0 if all(r['status']=='passed' for r in results) else 1

if __name__ == '__main__':raise SystemExit(main())
