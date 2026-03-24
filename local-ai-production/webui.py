#!/usr/bin/env python3
import http.server
import socketserver
import json
import urllib.request
import urllib.error

PORT = 3000
OLLAMA_URL = "http://localhost:11434"

HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Ollama</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui,-apple-system;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;display:flex;justify-content:center;align-items:center}.container{width:90%;max-width:800px;background:white;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,0.3);display:flex;flex-direction:column;height:85vh}.header{background:linear-gradient(135deg,#667eea,#764ba2);color:white;padding:20px;border-radius:12px 12px 0 0;text-align:center}.header h1{margin-bottom:10px}.messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:10px}.msg{padding:12px 16px;border-radius:8px;max-width:85%;word-wrap:break-word}.user{align-self:flex-end;background:#667eea;color:white}.ai{align-self:flex-start;background:#f0f0f0}.input-area{padding:15px;border-top:1px solid #eee;display:flex;gap:10px}input{flex:1;padding:10px;border:1px solid #ddd;border-radius:20px;outline:none}button{padding:10px 20px;background:#667eea;color:white;border:none;border-radius:20px;cursor:pointer;font-weight:600}select{padding:8px;border:1px solid #ddd;border-radius:6px}</style></head><body><div class="container"><div class="header"><h1>Chat</h1><select id="m" style="margin-top:10px;width:200px"><option>Loading...</option></select></div><div id="msgs" class="messages"></div><div class="input-area"><input id="in" placeholder="Type message..."><button onclick="send()">Send</button></div></div><script>let models=[];async function init(){try{let r=await fetch('/models');let d=await r.json();models=d.m;document.getElementById('m').innerHTML=models.map(x=>`<option>${x}</option>`).join('')}catch(e){alert('Error: '+e.message)}}async function send(){let msg=document.getElementById('in').value.trim();if(!msg)return;let m=document.getElementById('m').value;document.getElementById('msgs').innerHTML+=`<div class="msg user">${msg}</div>`;document.getElementById('in').value='';let loading=`<div class="msg ai">Thinking...</div>`;document.getElementById('msgs').innerHTML+=loading;try{let r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({m:m,q:msg})});let d=await r.json();document.getElementById('msgs').innerHTML=document.getElementById('msgs').innerHTML.replace(loading,`<div class="msg ai">${d.r}</div>`);document.getElementById('msgs').scrollTop=document.getElementById('msgs').scrollHeight}catch(e){document.getElementById('msgs').innerHTML=document.getElementById('msgs').innerHTML.replace(loading,`<div class="msg ai">Error: ${e.message}</div>`)}}document.getElementById('in').addEventListener('keypress',e=>{if(e.key=='Enter')send()});init()</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == '/models':
            try:
                req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    models = [m['name'] for m in data.get('models', [])]
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'m': models}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/chat':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length))
                req = urllib.request.Request(
                    f"{OLLAMA_URL}/api/generate",
                    data=json.dumps({'model': body['m'], 'prompt': body['q'], 'stream': False}).encode(),
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    result = json.loads(resp.read())
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'r': result.get('response', 'No response')}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Server at http://localhost:{PORT}")
        httpd.serve_forever()
