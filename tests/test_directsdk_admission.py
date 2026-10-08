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
    while the relay is answering, the write raises BrokenPipeError/ConnectionResetError out of the
    handler and socketserver's default handle_error() dumps '-'*40 / 'Exception occurred during
    processing of request from ...' / two tracebacks into the user's terminal (observed live,
    2026-09-25 and again 2026-10-05). The relay must stay silent for a client disconnect.

    FORK 2026-10-05: the socket is closed with SO_LINGER(1, 0) so the peer sends an RST instead of
    a FIN -- a plain close() left the first write to succeed in the kernel buffer (timing luck: the
    old test passed while the field kept printing), an RST makes the server-side write raise on the
    next send, deterministically reproducing the field traceback.
    """
    import socket
    import struct
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
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        conn.sock.close()  # native hung up with an RST; the relay's 502 now writes to a dead peer
        end = time.monotonic() + 4
        while time.monotonic() < end:  # bounded: let the handler thread run and (pre-fix) print
            time.sleep(0.05)
        err = capsys.readouterr().err
        assert 'Traceback' not in err, err
        assert 'Exception occurred during processing of request' not in err, err
    finally:
        gate.close()


def test_server_handle_error_suppresses_disconnects_and_notes_the_rest(capsys):
    """The suppression must live where socketserver calls it: SERVER.handle_error.

    socketserver._handle_request_noblock dispatches a handler exception to self.handle_error (self
    = the server). The pre-2026-10-05 override sat on the Handler class and therefore never ran;
    this pins the server-level behavior directly: disconnect classes are silent, anything else is a
    one-line note (never a traceback dump).
    """
    import admission
    import socket as _socket
    gate = admission.Admission('https://upstream.invalid', 5)
    dummy = _socket.socket()
    try:
        capsys.readouterr()
        for exc in (BrokenPipeError(32, 'Broken pipe'), ConnectionResetError(54, 'Connection reset by peer')):
            try:
                raise exc
            except (BrokenPipeError, ConnectionResetError):
                gate.server.handle_error(dummy, ('127.0.0.1', 41234))
        err = capsys.readouterr().err
        assert err == '', err
        try:
            raise ValueError('boom')
        except ValueError:
            gate.server.handle_error(dummy, ('127.0.0.1', 41234))
        err = capsys.readouterr().err
        assert 'ValueError' in err, err
        assert 'Traceback' not in err, err
    finally:
        dummy.close()
        gate.close()


def test_messages_post_is_answered_while_a_preflight_keepalive_connection_is_still_open():
    """Native opens /v1/messages on a NEW connection while its /api/hello keep-alive connection is
    still open. The relay must answer that POST without waiting for the idle connection to close;
    when it did not, native gave up with "No response from API (waited 3m)" (FORK.md 2026-10-08)."""
    import socket
    import admission

    class Peer(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    gate = admission.Admission(f'http://127.0.0.1:{peer.server_port}', 30)
    preflight = socket.create_connection(('127.0.0.1', gate.server.server_port), timeout=5)
    messages = socket.create_connection(('127.0.0.1', gate.server.server_port), timeout=5)
    try:
        preflight.sendall(f'HEAD {gate.prefix}/api/hello HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n'.encode())
        assert preflight.recv(4096).startswith(b'HTTP/1.1 200')
        body = b'{}'
        messages.sendall(f'POST {gate.prefix}/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                         f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
        assert messages.recv(4096).startswith(b'HTTP/1.1 200')  # socket.timeout here = the 3-minute stall
        assert gate.status == 200
    finally:
        preflight.close()
        messages.close()
        gate.close()
        peer.shutdown()
        peer.server_close()
