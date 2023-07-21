from pylspclient import lsp_structs
from pylspclient import LspClient
from pylspclient import JsonRpcEndpoint
from pylspclient import LspEndpoint
import subprocess
import json
import re
import queue
from collections import defaultdict
from pprint import pprint

JSON_RPC_REQ_FORMAT = "Content-Length: {json_string_len}\r\n\r\n{json_string}"
JSON_RPC_RES_REGEX = "Content-Length: ([0-9]*)\r\n"

class DidChangeTextDocumentParams(object):
    def __init__(self, uri, version, text):
        self.textDocument = {uri: uri, version: version}
        self.contentChanges = [{text: text}]

class Lean4JsonEncoder(json.JSONEncoder):
    def default(self, o):
        return o.__dict__

class Lean4JsonRpcEndpoint(JsonRpcEndpoint):
    def add_header(self, json_string):
        return JSON_RPC_REQ_FORMAT.format(
            json_string_len=len(json_string), json_string=json_string
        )

    def send_request(self, message):
        json_string = json.dumps(message, cls=Lean4JsonEncoder)
        jsonrpc_req = self.add_header(json_string)
        with self.write_lock:
            self.stdin.write(jsonrpc_req.encode())
            self.stdin.flush()

    def recv_response(self):
        with self.read_lock:
            line = self.stdout.readline()
            if not line:
                return None
            line = line.decode()

            match = re.match(JSON_RPC_RES_REGEX, line)
            if match is None or not match.groups():
                raise RuntimeError("Bad header: " + line)
            size = int(match.groups()[0])
            line = self.stdout.readline()
            if not line:
                return None
            line = line.decode()
            if line != "\r\n":
                raise RuntimeError("Bad header: missing newline")
            jsonrpc_res = self.stdout.read(size)
            return json.loads(jsonrpc_res)


class Lean4LspEndpoint(LspEndpoint):
    def run(self):
        while not self.shutdown_flag:
            jsonrpc_message = self.json_rpc_endpoint.recv_response()

            if jsonrpc_message is None:
                print("server quit")
                break

            if "result" in jsonrpc_message or "error" in jsonrpc_message:
                self.handle_result(jsonrpc_message)
            elif "method" in jsonrpc_message:
                if jsonrpc_message["method"] in self.callbacks:
                    self.callbacks[jsonrpc_message["method"]](jsonrpc_message)
                else:
                    self.default_callback(jsonrpc_message)
            else:
                print("unknown jsonrpc message")


class Lean4LspClient(LspClient):
    def didChange(self, textDocument, contentChanges):
        return self.lsp_endpoint.send_notification("textDocument/didChange",
                                                   textDocument=textDocument,
                                                   contentChanges=contentChanges)

    def didClose(self, textDocumentIdentifier):
        return self.lsp_endpoint.send_notification("textDocument/didClose",
                                                   textDocument=textDocumentIdentifier)

def severity_to_string(severity):
    if severity == 1:
        return "Error"
    elif severity == 2:
        return "Warning"
    elif severity == 3:
        return "Info"
    elif severity == 4:
        return "Hint"
    else:
        return "Unknown"


class Lean4Client:
    def __init__(self):
        self.init_lean_server()
        self.job_queue = queue.Queue()
        self.active_userid = None
        self.user_status = defaultdict(dict)

    def init_lean_server(self):
        self.proc = subprocess.Popen(
            ["lean", "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE
        )
        self.json_rpc_endpoint = Lean4JsonRpcEndpoint(self.proc.stdin, self.proc.stdout)

        default_callback = lambda x: None
        callbacks = {
            "textDocument/publishDiagnostics": self.publish_diagnostics_callback,
            "$/lean/fileProgress": self.file_progress_callback,
        }
        self.lsp_endpoint = Lean4LspEndpoint(
            self.json_rpc_endpoint,
            default_callback=default_callback,
            callbacks=callbacks
        )
        self.lsp_client = Lean4LspClient(self.lsp_endpoint)

        capabilities = {
            "textDocument": {
                "codeAction": {"dynamicRegistration": True},
                "publishDiagnostics": {"relatedInformation": True},
                "synchronization": {
                    "didSave": True,
                    "dynamicRegistration": True,
                    "willSave": True,
                    "willSaveWaitUntil": True,
                },
            }
        }
        self.root_uri = "file:///home/nakasho/lean4-lsp-client/"
        workspace_folders = [{"name": "work", "uri": self.root_uri}]
        self.lsp_client.initialize(
            self.proc.pid,
            None,
            self.root_uri,
            None,
            capabilities,
            "off",
            workspace_folders
        )
        self.lsp_client.initialized()
    
    def invoke_lean4_verify(self):
        if self.active_userid is not None:
            return
        
        try:
            userid, source_code = self.job_queue.get(block=False)
        except:
            return
        
        self.active_userid = userid
        if userid in self.user_status:
            version = self.user_status[userid]["version"] + 1
        else:
            version = 1

        self.user_status[userid].update({
            "status": "processing",
            "progress": 0,
            "diagnostics": None,
            "version": version
            })

        uri = self.root_uri + userid + ".lean"
        if version == 1:
            self.lsp_client.didOpen(lsp_structs.TextDocumentItem(uri, "lean", version, source_code))
        else:
            textDocument = {'uri': uri, 'version': version}
            contentChanges = [{'text': source_code}]
            self.lsp_client.didChange(textDocument, contentChanges)

    def post_source_code(self, userid, source_code):
        print("### post_source_code is called ###")
        print("userid: ", userid, "source code: ", source_code)
        self.job_queue.put((userid, source_code))
        self.invoke_lean4_verify()

    def get_progress(self, userid):
        print("### get_progress is called ###")
        print("userid: ", userid)
        if not self.user_status[userid]:
            return -1
        return self.user_status[userid]["progress"]
    
    def get_diagnostics(self, userid):
        print("### get_diagnostics is called ###")
        print("userid: ", userid)
        if not self.user_status[userid]:
            return None
        return self.user_status[userid]["diagnostics"]

    def publish_diagnostics_callback(self, jsonrpc_message):
        print("### publish_diagnostics_callback is called ###")
        pprint(jsonrpc_message)
        if self.active_userid is None:
            print("publish_diagnostics_callback is called, but active_userid is empty")
            return
        
        try:
            json_diagnostics = jsonrpc_message["params"]["diagnostics"]
            if json_diagnostics is None:
                return
            
            self.user_status[self.active_userid]["diagnostics"] = []

            for json_diagnostic in json_diagnostics:
                line_no = json_diagnostic["range"]["start"]["line"]
                column_no = json_diagnostic["range"]["start"]["character"]
                severity = severity_to_string(json_diagnostic["severity"])
                message = json_diagnostic["message"]
                self.user_status[self.active_userid]["diagnostics"].append(
                    {
                        "line_no": line_no,
                        "column_no": column_no,
                        "severity": severity,
                        "message": message
                    }
                )
        except:
            print("Diagnostics: Unknown error occurred")

    def file_progress_callback(self, jsonrpc_message):
        print("### file_progress_callback is called ###")
        pprint(jsonrpc_message)
        if self.active_userid is None:
            print("file_progress_callback is called, but active_userid is empty")
            return
        
        progress = 0
        try:
            json_processing = jsonrpc_message["params"]["processing"]
            if json_processing:
                start_line = json_processing[0]["range"]["start"]["line"]
                end_line = json_processing[0]["range"]["end"]["line"]
                progress = max(int(start_line / end_line * 100)-1, 0)
            else:
                progress = 100
        except:
            print("Progress: jsonrpc_message is broken")
        else:
            self.user_status[self.active_userid]["progress"] = progress
            if progress == 100:
                self.active_userid = None
                self.invoke_lean4_verify()
