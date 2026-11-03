"""Private loopback SSE-format adapter over the EXISTING shared budget gateway.

No provider credentials are read here. Native tools use only a scoped local
token. Real requests are single-shot, common-model nonstream calls; response
buffering is disclosed. Duplicate/in-doubt requests are rejected, never retried.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import os


def stream_bytes(body):
    base={key:body[key] for key in ('id','created','model') if key in body}
    base['object']='chat.completion.chunk'
    choice=body['choices'][0];message=choice['message']
    delta={'role':'assistant'}
    if message.get('content') is not None:delta['content']=message['content']
    if message.get('tool_calls'):
        delta['tool_calls']=[{'index':index,**call} for index,call in enumerate(message['tool_calls'])]
    chunks=[{**base,'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
            {**base,'choices':[{'index':0,'delta':{},'finish_reason':choice.get('finish_reason','stop')}]}]
    if body.get('usage') is not None:chunks.append({**base,'choices':[],'usage':body['usage']})
    return b''.join(b'data: '+json.dumps(chunk,separators=(',',':')).encode()+b'\n\n' for chunk in chunks)+b'data: [DONE]\n\n'


class NativeProxy:
    def __init__(self,root,upstream,token,*,maximum_requests=64,arm='linux-native-upstream-v1',listen_host='127.0.0.1'):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.base=upstream.rstrip('/');self.arm=arm;self.listen_host=listen_host
        self.upstream=self.base+'/'+arm+'/v1/chat/completions'
        self.relay_token=(Path(__file__).resolve().parent/'private-bridge/scoped-token.txt').read_text().strip()
        self.token=token;self.maximum_requests=maximum_requests;self.lock=threading.Lock()

    def forward(self,payload):
        if payload.get('model')!='deepseek-flash':
            return 400,b'{"error":"Only the declared common DeepSeek alias is allowed"}'
        admission=urllib.request.Request(self.base+'/admission/'+self.arm,headers={'Authorization':'Bearer '+self.relay_token})
        try:
            with urllib.request.urlopen(admission,timeout=10) as response:allowed=json.load(response).get('allowed') is True
        except Exception:
            return 503,b'{"error":"Native admission unavailable; no provider dispatch or intent"}'
        if not allowed:
            return 423,b'{"error":"Native admission held; no provider dispatch or intent"}'
        body={key:value for key,value in payload.items() if key not in
              ('stream','stream_options','max_completion_tokens','reasoning_effort','store','metadata')}
        body.update(model='deepseek-flash',stream=False,temperature=0,max_tokens=8192,thinking={'type':'disabled'})
        wire=json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        digest=hashlib.sha256(wire).hexdigest();intent=self.root/(digest+'.intent.json')
        with self.lock:
            if intent.exists():
                return 409,b'{"error":"Duplicate or in-doubt native request; no provider retry is authorized"}'
            if len(list(self.root.glob('*.intent.json')))>=self.maximum_requests:
                return 429,b'{"error":"Declared native request ceiling reached"}'
            with intent.open('x') as handle:
                json.dump({'request_sha256':digest,'started_epoch':time.time(),'status':'dispatched_or_uncertain'},handle)
        request=urllib.request.Request(self.upstream,data=wire,method='POST',headers={
            'Content-Type':'application/json','Authorization':'Bearer '+self.relay_token,
            'X-Proteus-Request-ID':'native-'+hashlib.sha256((self.arm+'|'+digest).encode()).hexdigest()})
        try:
            with urllib.request.urlopen(request,timeout=610) as response:
                status,data=response.status,response.read(16*1024**2+1)
            if len(data)>16*1024**2:raise ValueError('native response exceeds transport bound')
        except urllib.error.HTTPError as error:
            status,data=error.code,b'{"error":"Shared budget gateway rejected the native request"}'
        except Exception as error:
            (self.root/(digest+'.transport-error.json')).write_text(json.dumps({'type':type(error).__name__,
                'request_retry_performed':False,'usage':None,'cost':None})+'\n')
            return 599,b'{"error":"Native response uncertain; shared reservations remain and no retry was performed"}'
        (self.root/(digest+'.response.json')).write_bytes(data)
        (self.root/(digest+'.finished.json')).write_text(json.dumps({'status':status,'finished_epoch':time.time(),
            'request_sha256':digest,'response_sha256':hashlib.sha256(data).hexdigest()})+'\n')
        return status,data

    def server(self):
        proxy=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def answer(self,status,data,kind='application/json'):
                self.send_response(status);self.send_header('Content-Type',kind)
                self.send_header('Content-Length',str(len(data)));self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                if self.path=='/health':self.answer(200,b'{"ok":true,"stream_format_emulation":true}')
                else:self.answer(404,b'{}')
            def do_POST(self):
                if self.path not in ('/v1/chat/completions','/chat/completions'):
                    self.answer(404,b'{}');return
                if self.headers.get('Authorization')!='Bearer '+proxy.token:
                    self.answer(401,b'{"error":"Scoped native token required"}');return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=8*1024**2:raise ValueError('invalid request size')
                    payload=json.loads(self.rfile.read(length))
                    status,data=proxy.forward(payload)
                    if status==200 and payload.get('stream'):
                        data=stream_bytes(json.loads(data));kind='text/event-stream'
                    else:kind='application/json'
                    self.answer(status,data,kind)
                except (BrokenPipeError,ConnectionError):
                    pass  # The shared gateway retains actual usage/reservation.
                except Exception:
                    self.answer(400,b'{"error":"Native transport request/response format is unsupported"}')
        class Server(ThreadingHTTPServer):request_queue_size=128
        return Server((self.listen_host,0),Handler)
