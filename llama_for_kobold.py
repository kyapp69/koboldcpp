# A hacky little script from Concedo that exposes llama.cpp function bindings 
# allowing it to be used via a simulated kobold api endpoint
# it's not very usable as there is a fundamental flaw with llama.cpp 
# which causes generation delay to scale linearly with original prompt length.

import ctypes
import os
import argparse
import json, http.server, threading, socket, sys, time

class load_model_inputs(ctypes.Structure):
    _fields_ = [("threads", ctypes.c_int),
                ("max_context_length", ctypes.c_int),
                ("batch_size", ctypes.c_int),
                ("f16_kv", ctypes.c_bool),
                ("model_filename", ctypes.c_char_p),
                ("n_parts_overwrite", ctypes.c_int)]

class generation_inputs(ctypes.Structure):
    _fields_ = [("seed", ctypes.c_int),
                ("prompt", ctypes.c_char_p),
                ("max_context_length", ctypes.c_int),
                ("max_length", ctypes.c_int),
                ("temperature", ctypes.c_float),
                ("top_k", ctypes.c_int),
                ("top_p", ctypes.c_float),
                ("rep_pen", ctypes.c_float),
                ("rep_pen_range", ctypes.c_int)]

class generation_outputs(ctypes.Structure):
    _fields_ = [("status", ctypes.c_int),
                ("text", ctypes.c_char * 16384)]

dir_path = os.path.dirname(os.path.realpath(__file__))
handle = ctypes.CDLL(os.path.join(dir_path, "llamacpp.dll"))

handle.load_model.argtypes = [load_model_inputs] 
handle.load_model.restype = ctypes.c_bool
handle.generate.argtypes = [generation_inputs, ctypes.c_wchar_p] #apparently needed for osx to work. i duno why they need to interpret it that way but whatever
handle.generate.restype = generation_outputs
  
def load_model(model_filename,batch_size=8,max_context_length=512,n_parts_overwrite=-1):
    inputs = load_model_inputs()
    inputs.model_filename = model_filename.encode("UTF-8")
    inputs.batch_size = batch_size
    inputs.max_context_length = max_context_length #initial value to use for ctx, can be overwritten
    inputs.threads = os.cpu_count()
    inputs.n_parts_overwrite = n_parts_overwrite
    inputs.f16_kv = True
    ret = handle.load_model(inputs)
    return ret

def generate(prompt,max_length=20, max_context_length=512,temperature=0.8,top_k=100,top_p=0.85,rep_pen=1.1,rep_pen_range=128,seed=-1):
    inputs = generation_inputs()
    outputs = ctypes.create_unicode_buffer(ctypes.sizeof(generation_outputs))
    inputs.prompt = prompt.encode("UTF-8")
    inputs.max_context_length = max_context_length   # this will resize the context buffer if changed
    inputs.max_length = max_length
    inputs.temperature = temperature
    inputs.top_k = top_k
    inputs.top_p = top_p
    inputs.rep_pen = rep_pen
    inputs.rep_pen_range = rep_pen_range
    inputs.seed = seed
    ret = handle.generate(inputs,outputs)
    if(ret.status==1):
        return ret.text.decode("UTF-8")
    return ""


friendlymodelname = "concedo/llamacpp"  # local kobold api apparently needs a hardcoded known HF model name
maxctx = 2048
maxlen = 128
modelbusy = False


class ServerRequestHandler(http.server.SimpleHTTPRequestHandler):
    sys_version = ""
    server_version = "ConcedoLlamaForKoboldServer"

    def __init__(self, addr, port, embedded_kailite):
        self.addr = addr
        self.port = port
        self.embedded_kailite = embedded_kailite

    def __call__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path=="/" or self.path.startswith('/?') or self.path.startswith('?'):
            if self.embedded_kailite is None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Embedded Kobold Lite is not found.<br>You will have to connect via the main KoboldAI client, or <a href=\'https://lite.koboldai.net?local=1&port='+str(self.port).encode()+b'\'>use this URL</a> to connect.')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.embedded_kailite)
            return
                       
        if self.path.endswith('/api/v1/model/') or self.path.endswith('/api/latest/model/') or self.path.endswith('/api/v1/model') or self.path.endswith('/api/latest/model'):
            self.send_response(200)
            self.end_headers()
            global friendlymodelname
            self.wfile.write(json.dumps({"result": friendlymodelname }).encode())
            return

        if self.path.endswith('/api/v1/config/max_length/') or self.path.endswith('/api/latest/config/max_length/') or self.path.endswith('/api/v1/config/max_length') or self.path.endswith('/api/latest/config/max_length'):
            self.send_response(200)
            self.end_headers()
            global maxlen
            self.wfile.write(json.dumps({"value":maxlen}).encode())
            return

        if self.path.endswith('/api/v1/config/max_context_length/') or self.path.endswith('/api/latest/config/max_context_length/') or self.path.endswith('/api/v1/config/max_context_length') or self.path.endswith('/api/latest/config/max_context_length'):
            self.send_response(200)
            self.end_headers()
            global maxctx
            self.wfile.write(json.dumps({"value":maxctx}).encode())
            return

        if self.path.endswith('/api/v1/config/soft_prompt') or self.path.endswith('/api/v1/config/soft_prompt/') or self.path.endswith('/api/latest/config/soft_prompt') or self.path.endswith('/api/latest/config/soft_prompt/'):
            self.send_response(200)
            self.end_headers()           
            self.wfile.write(json.dumps({"value":""}).encode())
            return
        
        self.send_response(404)
        self.end_headers()
        rp = 'Error: HTTP Server is running, but this endpoint does not exist. Please check the URL.'
        self.wfile.write(rp.encode())
        return

    
    def do_POST(self):
        global modelbusy
        content_length = int(self.headers['Content-Length'])
        body = self.rfile.read(content_length)  

        if modelbusy:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(json.dumps({"detail": {
                    "msg": "Server is busy; please try again later.",
                    "type": "service_unavailable",
                }}).encode())
            return

        basic_api_flag = False
        kai_api_flag = False
        if self.path.endswith('/request') or self.path.endswith('/request'):
            basic_api_flag = True
        if self.path.endswith('/api/v1/generate/') or self.path.endswith('/api/latest/generate/') or self.path.endswith('/api/v1/generate') or self.path.endswith('/api/latest/generate'):
            kai_api_flag = True

        if basic_api_flag or kai_api_flag:
            genparams = None
            try:
                genparams = json.loads(body)
            except ValueError as e:
                self.send_response(503)
                self.end_headers()
                return       
            print("\nInput: " + json.dumps(genparams))
            
            modelbusy = True
            if kai_api_flag:
                fullprompt = genparams.get('prompt', "")
            else:
                fullprompt = genparams.get('text', "")
            newprompt = fullprompt
            
                
            recvtxt = ""
            if kai_api_flag:
                recvtxt = generate(
                    prompt=newprompt,
                    max_context_length=genparams.get('max_context_length', maxctx),
                    max_length=genparams.get('max_length', 50),
                    temperature=genparams.get('temperature', 0.8),
                    top_k=genparams.get('top_k', 200),
                    top_p=genparams.get('top_p', 0.85),
                    rep_pen=genparams.get('rep_pen', 1.1),
                    rep_pen_range=genparams.get('rep_pen_range', 128),
                    seed=-1
                    )
                print("\nOutput: " + recvtxt)
                res = {"results": [{"text": recvtxt}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(res).encode())               
            else:
                recvtxt = generate(
                    prompt=newprompt,
                    max_length=genparams.get('max', 50),
                    temperature=genparams.get('temperature', 0.8),
                    top_k=genparams.get('top_k', 200),
                    top_p=genparams.get('top_p', 0.85),
                    rep_pen=genparams.get('rep_pen', 1.1),
                    rep_pen_range=genparams.get('rep_pen_range', 128),
                    seed=-1
                    )
                print("\nOutput: " + recvtxt)
                res = {"data": {"seqs":[recvtxt]}}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(res).encode())
            modelbusy = False
            return    

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()
    
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        if "/api" in self.path:
            self.send_header('Content-type', 'application/json')
        else:
            self.send_header('Content-type', 'text/html')
           
        return super(ServerRequestHandler, self).end_headers()


def RunServerMultiThreaded(addr, port, embedded_kailite = None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((addr, port))
    sock.listen(5)

    class Thread(threading.Thread):
        def __init__(self, i):
            threading.Thread.__init__(self)
            self.i = i
            self.daemon = True
            self.start()

        def run(self):
            handler = ServerRequestHandler(addr, port, embedded_kailite)
            with http.server.HTTPServer((addr, port), handler, False) as self.httpd:
                try:
                    self.httpd.socket = sock
                    self.httpd.server_bind = self.server_close = lambda self: None
                    self.httpd.serve_forever()
                except (KeyboardInterrupt,SystemExit):
                    self.httpd.server_close()
                    sys.exit(0)
                finally:
                    self.httpd.server_close()
                    sys.exit(0)
        def stop(self):
            self.httpd.server_close()

    numThreads = 5
    threadArr = []
    for i in range(numThreads):
        threadArr.append(Thread(i))
    while 1:
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            for i in range(numThreads):
                threadArr[i].stop()
            sys.exit(0)

def main(args): 
    ggml_selected_file = args.model_file

    if not os.path.exists(ggml_selected_file):
        print(f"Cannot find model file: {ggml_selected_file}")
        time.sleep(1)
        sys.exit(2)

    mdl_nparts = sum(1 for n in range(1, 9) if os.path.exists(f"{ggml_selected_file}.{n}")) + 1
    modelname = os.path.abspath(ggml_selected_file)
    print("Loading model: " + modelname)
    loadok = load_model(modelname,8,maxctx,mdl_nparts)
    print("Load Model OK: " + str(loadok))

    if not loadok:
        print("Could not load model: " + modelname)
        sys.exit(3)
    try:
        basepath = os.path.abspath(os.path.dirname(__file__))
        with open(os.path.join(basepath, "klite.embd"), mode='rb') as f:
            embedded_kailite = f.read().decode().replace('var localmodehost = "127.0.0.1";' , f'var localmodehost = "{args.host}";').encode()
        print("Embedded Kobold Lite loaded.")
    except:
        print("Could not find Kobold Lite. Embedded Kobold Lite will not be available.")

    print(f"Starting Kobold HTTP Server on port {args.port}") 
    print(f"Please connect to custom endpoint at http://{args.host}:{args.port}")
    RunServerMultiThreaded(args.host, args.port, embedded_kailite)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Kobold llama.cpp server')
    parser.add_argument("model_file", help="Model file to load")
    parser.add_argument("--port", help="Port to listen on", default=5001)
    parser.add_argument("--host", help="Host IP to listen on", default="127.0.0.1")
    args = parser.parse_args()
    main(args)