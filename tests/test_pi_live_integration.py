"""Opt-in real Pi, deterministic loopback provider, no paid model or home writes.

Run: LTC_TEST_REAL_PI=1 PYTHONPATH=src python -m unittest discover -s tests -p test_pi_live_integration.py -q
"""
import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time

import tempfile
import unittest
from unittest import mock

from long_task_callback import cli, pi_callback as bridge
from long_task_callback.runtime import worker_command



def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(0.03)
    raise AssertionError('timed out waiting for fixture state')


class Provider:
    def __init__(self):
        self.requests = []
        self.callbacks = {}
        self.busy = threading.Event()
        self.release = threading.Event()
        self.tool_loop = None
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                assert self.path == '/v1/chat/completions'
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                parent.requests.append(body)
                assert body['stream'] is True
                messages = body['messages']
                last = messages[-1]
                text = str(last.get('content', ''))
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                def chunk(delta, finish=None):
                    value = {'id':'chatcmpl-fixture', 'object':'chat.completion.chunk', 'created':1,
                             'model':'fixture', 'choices':[{'index':0, 'delta':delta, 'finish_reason':finish}]}
                    self.wfile.write(('data: '+json.dumps(value)+'\n\n').encode()); self.wfile.flush()
                if last['role'] == 'user' and 'BUSY_FIXTURE' in text:
                    chunk({'role':'assistant','content':'Holding the existing turn.'})
                    parent.busy.set()
                    if not parent.release.wait(20): raise AssertionError('busy barrier not released')
                command = next((cmd for token, cmd in parent.callbacks.items() if token in text), None)
                if parent.tool_loop:
                    def loop_tool(stage):
                        prefix = parent.tool_loop/f'tool-{stage}'
                        return (f'touch {shlex.quote(str(prefix)+"-started")}; '
                                f'while [ ! -f {shlex.quote(str(prefix)+"-release")} ]; do sleep 0.03; done; '
                                f'echo LOOP_{stage}_DONE')
                    if last['role'] == 'user' and 'TOOL_LOOP_FIXTURE' in text:
                        command = loop_tool('FIRST')
                    elif not command and any(m['role'] == 'tool' and 'LOOP_FIRST_DONE' in str(m.get('content')) for m in messages) and not any(m['role'] == 'tool' and 'LOOP_SECOND_DONE' in str(m.get('content')) for m in messages):
                        command = loop_tool('SECOND')
                if command:
                    chunk({'role':'assistant','tool_calls':[{'index':0,'id':'call_fixture','type':'function',
                        'function':{'name':'bash','arguments':json.dumps({'command':command, 'timeout':15})}}]})
                    chunk({}, 'tool_calls')
                else:
                    chunk({'role':'assistant','content':'Fixture completed.'}); chunk({}, 'stop')
                self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
    def close(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join()


class RpcPi:
    def __init__(self, command, environment, cwd):
        self.process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        self.events = queue.Queue(); self.stderr = []; self.history = []
        self.profile = Path(environment['PI_CODING_AGENT_DIR'])
        def read():
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                    self.history.append(event)
                    self.events.put(event)
                except json.JSONDecodeError: self.events.put({'type':'invalid','line':line})
        def errors():
            self.stderr.extend(self.process.stderr)
        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=errors, daemon=True).start()
    def send(self, value):
        self.process.stdin.write(json.dumps(value)+'\n'); self.process.stdin.flush()
    def response(self, request_id):
        deadline = time.monotonic()+20
        while time.monotonic()<deadline:
            try: event=self.events.get(timeout=0.1)
            except queue.Empty:
                if self.process.poll() is not None: raise AssertionError('Pi exited: '+''.join(self.stderr))
                continue
            if event.get('type')=='response' and event.get('id')==request_id: return event
        raise AssertionError('missing RPC response '+request_id+': '+''.join(self.stderr))
    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try: self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # End the actual fixture Pi first, keeping its wrapper's lease
                # until Pi exits. Never terminate an unrelated session.
                for file in (self.profile/'long-task-callback/channels').glob('*/owner.json'):
                    owner = bridge.read_record(file)
                    if owner.get('lease_pid') == self.process.pid and cli.pid_is_running(owner['owner_pid']):
                        os.kill(owner['owner_pid'], signal.SIGTERM)
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired: self.process.terminate(); self.process.wait(timeout=5)
        if self.process.stdin and not self.process.stdin.closed: self.process.stdin.close()
        self.process.stdout.close(); self.process.stderr.close()


def runtime_fixture(tmp_path):
    provider = Provider()
    temporary_home = tmp_path / 'home'; temporary_home.mkdir()
    profile = tmp_path / 'pi'; profile.mkdir(mode=0o700)
    (profile/'models.json').write_text(json.dumps({'providers':{'ltc-fixture':{
        'baseUrl':f'http://127.0.0.1:{provider.server.server_port}/v1', 'api':'openai-completions',
        'apiKey':'fixture', 'models':[{'id':'fixture','reasoning':False,'input':['text'],
                                    'contextWindow':65536,'maxTokens':1024}]}}}))
    (profile/'settings.json').write_text(json.dumps({'defaultProvider':'ltc-fixture','defaultModel':'fixture',
        'compaction':{'enabled':False}, 'retry':{'enabled':False}}))
    # No inherited real-provider credentials or personal Pi configuration.
    environment = {'PATH':os.environ['PATH'], 'HOME':str(temporary_home), 'LANG':'C.UTF-8',
        'PI_CODING_AGENT_DIR':str(profile), 'CODEX_HOME':str(tmp_path/'codex'),
        cli.TARGET_LOCK_DIR_ENV:str(tmp_path/'target-locks'), 'NO_PROXY':'127.0.0.1', 'no_proxy':'127.0.0.1'}
    with mock.patch.dict(os.environ, environment, clear=True):
        try:
            yield provider, environment, tmp_path, profile
        finally:
            provider.close()


def callback(runtime, session, callback_id, delivery=None):
    provider, _, directory, _ = runtime
    queue_dir = directory/'queue'; cli.ensure_daemon_dirs(queue_dir)
    token = 'CALLBACK_'+callback_id
    args=argparse.Namespace(agent='pi',session=str(session),last=False,cwd=str(directory),task=token,callback_mode='cli')
    request=cli.make_request(args, token); request['id']=callback_id
    if delivery == 'follow-up':
        # Retained callbacks from the earlier Pi preview keep their old timing.
        request['version'] = 3
        request.pop('pi_delivery')
    provider.callbacks[token] = shlex.join(worker_command('ack','--queue-dir',str(queue_dir),'--id',callback_id))
    payload={'request':request,'queue_dir':str(queue_dir),'prompt':token,'ack_path':str(cli.ack_path(queue_dir,callback_id)),
        'canceled_path':str(cli.request_path(queue_dir,'canceled',callback_id)), 'command':cli.resume_command(request),
        'cwd':str(directory),'timeout':20}
    return request, payload


def entries(session):
    return [json.loads(line) for line in session.read_text().splitlines()]


@unittest.skipUnless(os.environ.get('LTC_TEST_REAL_PI') == '1' and shutil.which('pi'),
                     'opt-in installed real Pi and loopback provider')
class NativePiCallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ltc-native-pi-')
        self.addCleanup(temporary.cleanup)
        self.fixture = runtime_fixture(Path(temporary.name))
        self.runtime = next(self.fixture)
        self.addCleanup(self.fixture.close)

    def _callback_during_tool_loop(self, delivery, managed=True):
        provider, environment, directory, profile = self.runtime
        provider.tool_loop = directory
        if managed:
            command = worker_command('pi', '--cwd', str(directory), '--', '--mode', 'rpc',
                                     '--model', 'ltc-fixture/fixture')
        else:
            session = directory/'ordinary-loop.jsonl'
            session.write_text(json.dumps({'type':'session', 'version':3, 'id':'ordinary-loop',
                                           'cwd':str(directory), 'timestamp':'2026-10-09T00:00:00.000Z'})+'\n')
            command = [shutil.which('pi'), '--session', str(session), '--extension', str(bridge.extension_path()),
                       '--mode', 'rpc', '--model', 'ltc-fixture/fixture']
        rpc = RpcPi(command, environment, directory)
        try:
            owner_path = wait_for(lambda: next((profile/'long-task-callback/channels').glob('*/owner.json'), None))
            owner = wait_for(lambda: (v if (v := bridge.read_record(owner_path)).get('state') == 'active' else None))
            session = Path(owner['session_file'])
            rpc.send({'id':'loop', 'type':'prompt', 'message':'TOOL_LOOP_FIXTURE'})
            assert rpc.response('loop')['success']
            wait_for(lambda: (directory/'tool-FIRST-started').exists())
            callback_id = 'loop'
            if delivery == 'steer':
                canceled, canceled_payload = callback(self.runtime, session, 'canceled-steer', 'steer')
                cli.write_request(Path(canceled_payload['canceled_path']), canceled)
                canceled_plan = bridge.prepare_delivery(canceled_payload)
                assert cli.wait_for_remote_delivery(canceled_payload, canceled_plan, 20)['returncode'] == 0
            request, payload = callback(self.runtime, session, callback_id, delivery)
            plan = bridge.prepare_delivery(payload)
            receipts = owner_path.parent/'receipts'
            def receipt():
                return next((json.loads(p.read_text()) for p in receipts.glob('*.json')
                             if json.loads(p.read_text()).get('callback_id') == callback_id), None)
            if delivery == 'steer':
                assert wait_for(receipt)['state'] == 'admitted'
            else:
                time.sleep(0.3)
                assert receipt() is None
            assert not Path(payload['ack_path']).exists()
            assert not any('CALLBACK_'+callback_id in str(e.get('message', {})) for e in entries(session))
            (directory/'tool-FIRST-release').touch()
            wait_for(lambda: (directory/'tool-SECOND-started').exists())
            if delivery == 'steer':
                assert Path(payload['ack_path']).exists(), 'steer missed the next model decision'
                assert not any(e.get('type') == 'agent_end' for e in rpc.history)
                assert not any('CALLBACK_canceled-steer' in str(e.get('message', {})) for e in entries(session))
                assert provider.requests[1]['messages'][-1]['role'] == 'user'
                assert 'CALLBACK_'+callback_id in str(provider.requests[1]['messages'][-1]['content'])
            else:
                assert not Path(payload['ack_path']).exists()
                assert receipt() is None
            (directory/'tool-SECOND-release').touch()
            assert cli.wait_for_remote_delivery(payload, plan, 20)['returncode'] == 0
            wait_for(lambda: receipt().get('state') in ('session_observed', 'acknowledged'))
            data = entries(session)
            users = [str(e['message']['content']) for e in data if e.get('type') == 'message' and e['message']['role'] == 'user']
            assert sum('CALLBACK_'+callback_id in text for text in users) == 1
            children = Counter(e.get('parentId') for e in data if e.get('parentId'))
            assert max(children.values(), default=0) == 1
            assert bridge.read_record(owner_path)['owner_pid'] == owner['owner_pid']
        finally:
            (directory/'tool-FIRST-release').touch()
            (directory/'tool-SECOND-release').touch()
            rpc.close()

    def test_steer_at_next_model_call_without_ending_run(self):
        self._callback_during_tool_loop('steer')

    def test_follow_up_waits_for_entire_tool_loop(self):
        self._callback_during_tool_loop('follow-up')

    def test_ordinary_pi_steers_without_ending_run(self):
        self._callback_during_tool_loop('steer', managed=False)

    def test_native_idle_busy_single_branch_and_managed_offline(self):
        runtime = self.runtime
        provider, environment, directory, profile=runtime
        # An installed copy plus bundled --extension must create only one owner/consumer.
        bridge.install_extension(argparse.Namespace(profile=str(profile),force=False))
        rpc=RpcPi(worker_command('pi','--cwd',str(directory),'--','--mode','rpc','--model','ltc-fixture/fixture'),
                  environment,directory)
        try:
            channels=profile/'long-task-callback/channels'
            owner_path=wait_for(lambda: next(channels.glob('*/owner.json'),None))
            owner=wait_for(lambda: (v if (v:=bridge.read_record(owner_path)).get('state')=='active' and v.get('owner_identity') else None))
            session=Path(owner['session_file']); owner_pid=owner['owner_pid']
            assert owner_pid != rpc.process.pid  # launcher and one actual Pi
            assert cli.acquire_path_lock(bridge.writer_lock_path(str(session)),blocking=False) is None
            rpc.send({'id':'initial','type':'prompt','message':'baseline'})
            assert rpc.response('initial')['success']
            wait_for(lambda: any(e.get('type')=='message' and e['message']['role']=='assistant' for e in entries(session)))
            request,payload=callback(runtime,session,'idle')
            root=Path(payload['queue_dir'])
            request=cli.prepare_request_for_queue(root,request,payload['prompt'])
            # Exercise the actual LTC delivery worker. An online callback must not
            # execute this deliberately invalid Pi binary.
            with mock.patch.dict(os.environ, {'LONG_TASK_WAKEUP_PI_BIN':str(directory/'must-not-launch-pi')}):
                cli.write_request(root/'pending'/f"{request['id']}.json",request)
                assert cli.process_one(root,argparse.Namespace(resume_timeout=20,retries=0,retry_delay=0,retry_backoff=1))
            def reaped():
                cli.reap_background_resumes()
                return not cli._BACKGROUND_RESUMES
            wait_for(reaped)
            cli.recover_running(root)
            assert (root/'done'/f"{request['id']}.json").exists()
            assert not cli.retained_target_lease_is_held(request)
            assert bridge.read_record(owner_path)['owner_pid']==owner_pid
            # Wait until native Pi itself completes its tool turn before starting busy.
            def idle():
                rpc.send({'id':'state','type':'get_state'})
                return not rpc.response('state')['data']['isStreaming']
            wait_for(idle)
            rpc.send({'id':'busy','type':'prompt','message':'BUSY_FIXTURE'})
            assert rpc.response('busy')['success']; assert provider.busy.wait(20)
            request,payload=callback(runtime,session,'busy')
            plan=bridge.prepare_delivery(payload); assert plan.online
            time.sleep(0.3)
            assert not Path(payload['ack_path']).exists()
            assert not any('CALLBACK_busy' in str(e.get('message',{})) for e in entries(session))
            provider.release.set()
            assert cli.wait_for_remote_delivery(payload,plan,20)['returncode']==0
            assert bridge.read_record(owner_path)['owner_pid']==owner_pid
            # Native veto must reject new/switch before any new file is opened.
            rpc.send({'id':'new','type':'new_session'})
            assert rpc.response('new')['data']['cancelled']
            rpc.send({'id':'switch','type':'switch_session','sessionPath':str(directory/'never-opened.jsonl')})
            assert rpc.response('switch')['data']['cancelled']
            assert not (directory/'never-opened.jsonl').exists()
            user_entry=next(e for e in entries(session) if e.get('type')=='message' and e['message']['role']=='user')
            rpc.send({'id':'fork','type':'fork','entryId':user_entry['id']})
            assert rpc.response('fork')['data']['cancelled']
            rpc.send({'id':'same','type':'switch_session','sessionPath':str(session)})
            assert rpc.response('same')['data']['cancelled']
            request,payload=callback(runtime,session,'same')
            plan=bridge.prepare_delivery(payload)
            assert cli.wait_for_remote_delivery(payload,plan,20)['returncode']==0
        finally: rpc.close()
        assert not cli.pid_is_running(owner_pid)
        request,payload=callback(runtime,session,'offline')
        assert bridge.inspect_route(request)['status']=='offline_candidate'
        root=Path(payload['queue_dir']); request=cli.prepare_request_for_queue(root,request,payload['prompt'])
        cli.write_request(root/'pending'/f"{request['id']}.json",request)
        assert cli.process_one(root,argparse.Namespace(resume_timeout=20,retries=0,retry_delay=0,retry_backoff=1))
        wait_for(reaped); cli.recover_running(root)
        assert (root/'done'/f"{request['id']}.json").exists()
        assert not cli.retained_target_lease_is_held(request)
        owner=bridge.read_record(owner_path)
        assert bridge.confirmed_owner_dead(owner)  # another short recovery stays available
        data=entries(session)
        children=Counter(e.get('parentId') for e in data if e.get('parentId'))
        assert max(children.values(),default=0)==1, 'native callback created a sibling branch'
        users=[str(e['message']['content']) for e in data if e.get('type')=='message' and e['message']['role']=='user']
        for token in ('CALLBACK_idle','CALLBACK_busy','CALLBACK_same','CALLBACK_offline'):
            assert sum(token in value for value in users)==1



    def test_ordinary_pi_online_only_after_exit(self):
        runtime = self.runtime
        _,environment,directory,profile=runtime
        session=directory/'ordinary.jsonl'
        session.write_text(json.dumps({'type':'session','version':3,'id':'ordinary',
            'timestamp':'2026-10-04T00:00:00.000Z','cwd':str(directory)})+'\n')
        rpc=RpcPi([shutil.which('pi'),'--session',str(session),'--extension',str(bridge.extension_path()),
                   '--mode','rpc','--model','ltc-fixture/fixture'],environment,directory)
        try:
            owner_path=bridge.channel_dir(str(session),profile/'long-task-callback/channels')/'owner.json'
            wait_for(lambda: owner_path.exists())
            request,payload=callback(runtime,session,'ordinary')
            plan=bridge.prepare_delivery(payload)
            assert plan.online
            assert cli.wait_for_remote_delivery(payload,plan,20)['returncode']==0
        finally: rpc.close()
        assert bridge.inspect_route(request)['status']=='blocked'
        with self.assertRaises(cli.CallbackTransportBlocked): bridge.prepare_delivery(payload)



    def test_busy_cancel_and_managed_launcher_loss_never_dispatch(self):
        runtime = self.runtime
        provider,environment,directory,profile=runtime
        rpc=RpcPi(worker_command('pi','--cwd',str(directory),'--','--mode','rpc','--model','ltc-fixture/fixture'),
                  environment,directory)
        owner_pid=None
        try:
            owner_path=wait_for(lambda: next((profile/'long-task-callback/channels').glob('*/owner.json'),None))
            def active_owner():
                assert rpc.process.poll() is None, ''.join(rpc.stderr)
                value=bridge.read_record(owner_path)
                return value if value.get('state')=='active' else None
            owner=wait_for(active_owner)
            session=Path(owner['session_file']); owner_pid=owner['owner_pid']
            rpc.send({'id':'hold','type':'prompt','message':'BUSY_FIXTURE'})
            assert rpc.response('hold')['success']; assert provider.busy.wait(20)
            cancel_request,cancel_payload=callback(runtime,session,'cancel')
            cancel_plan=bridge.prepare_delivery(cancel_payload)
            cli.write_request(Path(cancel_payload['canceled_path']),cancel_request)
            assert cli.wait_for_remote_delivery(cancel_payload,cancel_plan,1)['returncode']==0
            request,payload=callback(runtime,session,'orphan')
            plan=bridge.prepare_delivery(payload)
            # Crash only our managed wrapper; leave the actual Pi alive deliberately.
            rpc.process.kill(); rpc.process.wait()
            assert cli.pid_is_running(owner_pid)
            assert bridge.inspect_route(request)['status']=='blocked'
            provider.release.set()
            assert cli.wait_for_remote_delivery(payload,plan,0.5)['returncode']==125
            assert cli.retained_target_lease_is_held(request)
            assert not any('CALLBACK_cancel' in str(e.get('message',{})) or
                           'CALLBACK_orphan' in str(e.get('message',{})) for e in entries(session))
            with self.assertRaises(cli.CallbackTransportBlocked): bridge.prepare_delivery(payload)
        finally:
            provider.release.set()
            if owner_pid and cli.pid_is_running(owner_pid): os.kill(owner_pid,signal.SIGTERM)
            rpc.close()



    def test_mailbox_error_keeps_author_input_and_offline_cancel_blocks_model(self):
        runtime = self.runtime
        provider,environment,directory,profile=runtime
        rpc=RpcPi(worker_command('pi','--cwd',str(directory),'--','--mode','rpc','--model','ltc-fixture/fixture'),
                  environment,directory)
        try:
            owner_path=wait_for(lambda: next((profile/'long-task-callback/channels').glob('*/owner.json'),None))
            owner=wait_for(lambda: (v if (v:=bridge.read_record(owner_path)).get('state')=='active' and v.get('owner_identity') else None))
            session=Path(owner['session_file'])
            (owner_path.parent/'inbox'/('0'*64+'.json')).write_text('broken-json')
            time.sleep(0.3)  # Let the native mailbox tick encounter the bad envelope.
            rpc.send({'id':'author','type':'prompt','message':'Continue author work despite a corrupt mailbox'})
            assert rpc.response('author')['success']
            wait_for(lambda: any(e.get('type')=='message' and e['message']['role']=='assistant' for e in entries(session)))
        finally: rpc.close()
        request,payload=callback(runtime,session,'cancel-offline')
        plan=bridge.prepare_delivery(payload); assert not plan.online
        # Cancellation after recovery preparation, before print's input hook.
        cli.write_request(Path(payload['canceled_path']),request)
        requests_before=len(provider.requests)
        process=subprocess.Popen(cli.process_command(plan.command),env=plan.environment,cwd=directory,
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            plan.monitor_owner(process)
            _,stderr=process.communicate(payload['prompt'],timeout=20)
            assert process.returncode==0,stderr
            assert len(provider.requests)==requests_before
            assert not any('CALLBACK_cancel-offline' in str(e.get('message',{})) for e in entries(session))
            cli.release_retained_target_lease(Path(payload['queue_dir']),request)
        finally:
            if process.poll() is None: process.kill(); process.wait()
            plan.close()
