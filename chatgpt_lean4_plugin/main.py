import json

import quart
import quart_cors
from quart import request
from lean4_client import Lean4Client
from pprint import pprint

app = quart_cors.cors(quart.Quart(__name__), allow_origin="https://chat.openai.com")

# Does not persist if Python session is restarted.
lean4client = Lean4Client()

@app.post("/diagnose/<string:userid>")
async def post_source_code(userid):
    print("### POST /diagnose/<string:userid> is called ###")
    request = await quart.request.get_json(force=True)
    pprint(request)
    lean4client.post_source_code(userid, request["code"])
    return quart.Response(response='OK', status=200)

@app.get("/diagnose/<string:userid>")
async def get_diagnostics(userid):
    print("### GET /diagnose/<string:userid> is called ###")
    progress = lean4client.get_progress(userid)
    print("progress: ", progress)
    if progress < 100:
        return quart.Response(response=json.dumps({"progress": progress}), status=204)

    diagnostics = lean4client.get_diagnostics(userid)
    print("diagnostics: ", diagnostics)
    return quart.Response(response=json.dumps(diagnostics), status=200)
        

@app.get("/logo.png")
async def plugin_logo():
    filename = 'logo.png'
    return await quart.send_file(filename, mimetype='image/png')

@app.get("/.well-known/ai-plugin.json")
async def plugin_manifest():
    host = request.headers['Host']
    with open("./.well-known/ai-plugin.json") as f:
        text = f.read()
        return quart.Response(text, mimetype="text/json")

@app.get("/openapi.yaml")
async def openapi_spec():
    host = request.headers['Host']
    with open("openapi.yaml") as f:
        text = f.read()
        return quart.Response(text, mimetype="text/yaml")

def main():
    app.run(debug=True, host="0.0.0.0", port=5004)

if __name__ == "__main__":
    main()