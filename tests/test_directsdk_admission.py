"""One upstream admission and first-response authority over native recovery."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

NATIVE = r'''
import json, os, sys, urllib.request, urllib.error
for line in sys.stdin:
    frame=json.loads(line)
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type':'result','num_turns':0}),flush=True)
        continue
    break
url=os.environ['ANTHROPIC_BASE_URL']+'/v1/messages'
for _ in range(2):
    try:
        urllib.request.urlopen(urllib.request.Request(url,data=b'{}',headers={'Content-Type':'application/json'}),timeout=5).read()
    except urllib.error.HTTPError:
        break
print(json.dumps({'type':'assistant','message':{'id':'first','role':'assistant','content':[{'type':'text','text':'FIRST'}]}}))
print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}))
print(json.dumps({'type':'result','subtype':'success','usage':{'input_tokens':0,'output_tokens':0}}))
'''


@pytest.mark.parametrize('stop', ['end_turn', 'max_tokens', 'model_context_window_exceeded'])
def test_first_response_owns_usage_and_stops_recovery(tmp_path, stop):
    calls = []
    usage = {'input_tokens':0, 'output_tokens':0, 'cache_read_input_tokens':0, 'cache_creation_input_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            events = [
                {'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[], 'usage':usage}},
                {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'FIRST'}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':stop},'usage':usage},
                {'type':'message_stop'},
            ]
            self.wfile.write(''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode())
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        result=client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
        assert len(calls)==1
        assert result.choices[0].message.content=='FIRST'
        assert result.choices[0].finish_reason==('stop' if stop=='end_turn' else 'length')
        assert result.usage.prompt_tokens==0
        assert result.choices[0].message.reasoning_details[0]['messages'][0]['stop_reason']==stop
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_empty_tool_input_completes_the_capture(tmp_path):
    """A no-argument tool call streams an empty input_json_delta; the capture must still complete."""
    calls = []
    usage = {'input_tokens':0, 'output_tokens':0, 'cache_read_input_tokens':0, 'cache_creation_input_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            events = [
                {'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[], 'usage':usage}},
                {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'toolu_1','name':'mcp__hermes__list_things','input':{}}},
                {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':''}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':usage},
                {'type':'message_stop'},
            ]
            self.wfile.write(''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode())
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    tools=[{'type':'function','function':{'name':'list_things','description':'list','parameters':{'type':'object','properties':{}}}}]
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        result=client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}],tools=tools)
        assert len(calls)==1
        call=result.choices[0].message.tool_calls[0]
        assert call.function.name=='list_things'
        assert json.loads(call.function.arguments)=={}
        assert result.choices[0].finish_reason=='tool_calls'
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_cancel_closes_the_active_upstream_socket(tmp_path):
    entered, disconnected = threading.Event(), threading.Event()
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            entered.set()
            self.rfile.read(1)
            disconnected.set()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    client = directsdk.Client(command=[sys.executable, str(native)], env={'PATH':os.defpath, 'HOME':str(tmp_path), 'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(client.create, model='sonnet', messages=[{'role':'user', 'content':'fixture'}])
            try:
                assert entered.wait(5)
            finally:
                client.cancel()
            with pytest.raises(RuntimeError, match='cancelled'):
                result.result(timeout=3)
            assert disconnected.wait(2)
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_incomplete_upstream_error_names_the_first_attempt(tmp_path):
    """Native's retries are denied with ADMISSION_CONSUMED; the raised error must carry the first attempt's status."""
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body=b'{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 213000 tokens > 200000 maximum"}}'
            self.send_response(529); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        with pytest.raises(RuntimeError, match=r'status 529, capture incomplete.*upstream said: prompt is too long: 213000 tokens'):
            client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_invalid_stream_json_error_names_the_offending_line(tmp_path):
    """A native that prints a non-JSON stdout line (a shim banner) fails with that line in the error, not a bare label."""
    native=tmp_path/'native.py'; native.write_text("import sys\nprint('mise WARN tool not activated')\nsys.exit(0)\n")
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':'http://127.0.0.1:9'})
    try:
        with pytest.raises(RuntimeError, match=r"Invalid native stream-json output: 'mise WARN tool not activated"):
            client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
    finally:
        client.close()


def test_client_disconnect_does_not_dump_a_traceback(capsys):
    """FORK: a native disconnect mid-proxy must not print socketserver's traceback.

    Native connects per request and hangs up as soon as it has what it needs. When that happens
    while the relay is answering, the write raises BrokenPipeError out of the handler and
    socketserver's default handle_error() dumps '-'*40 / 'Exception occurred during processing of
    request from ...' / two tracebacks into the user's terminal (observed live on the corp box,
    2026-09-25). The relay must stay silent for a client disconnect.
    """
    import socket
    import time
    from http.client import HTTPConnection
    import admission
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    dead_port = probe.getsockname()[1]
    probe.close()  # nothing listens here: _passthrough's connect() fails, so it answers send_error(502)
    gate = admission.Admission(f'http://127.0.0.1:{dead_port}', 5)
    try:
        capsys.readouterr()  # drop anything printed before the exchange
        conn = HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
        conn.connect()
        conn.putrequest('HEAD', gate.prefix + '/api/hello')
        conn.endheaders()
        conn.sock.close()  # native hung up; the relay's 502 now writes to a dead peer
        end = time.monotonic() + 4
        while time.monotonic() < end:  # bounded: let the handler thread run and (pre-fix) print
            time.sleep(0.05)
        err = capsys.readouterr().err
        assert 'Traceback' not in err, err
        assert 'Exception occurred during processing of request' not in err, err
    finally:
        gate.close()
