"""Original process-protocol fixture. Never loads any vendor library."""
import base64
import json
import os
from pathlib import Path
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", newline="\n")

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
step = 0

for line in sys.stdin.buffer:
    request = json.loads(line)
    request_id, method, params = request["id"], request["method"], request["params"]
    if method == "invalid":
        if mode == "wrong-id":
            print(json.dumps({"id": request_id + 1, "result": {}}), flush=True)
        elif mode == "duplicate-key":
            print('{"id":' + str(request_id) + ',"id":' + str(request_id) + ',"result":{}}', flush=True)
        elif mode == "partial":
            sys.stdout.write('{"id":')
            sys.stdout.flush()
            break
        elif mode == "oversize":
            sys.stdout.buffer.write(b"x" * (3 * 1024 * 1024))
            sys.stdout.buffer.flush()
        elif mode == "timeout":
            time.sleep(30)
        elif mode == "invalid-utf8":
            sys.stdout.buffer.write(b'\xff\n')
            sys.stdout.buffer.flush()
        elif mode == "exit":
            sys.stderr.write("synthetic-private-payload")
            sys.stderr.flush()
            os._exit(7)
        continue
    if method in {"load", "capabilities"}:
        result = {"protocolVersion": 1, "testMode": True, "runtimeTarget": "windows-x64", "functions": {}}
        if mode == "bad-protocol":
            result["protocolVersion"] = 2
    elif method == "configure":
        result = {"status": 0}
    elif method in {"start", "startBanking"}:
        if mode == "start-crash":
            os._exit(8)
        if params.get("functionId") == 999:
            result = {"status": -98765}
        else:
            step = 0
            result = {"status": 10000}
    elif method == "continue":
        if params["continueFlag"] == -1:
            result = {"status": -2}
        elif mode == "poll" and step < 3:
            step += 1
            result = {"status": 10000, "command": 23, "fieldType": 0, "minLength": 0, "maxLength": 0, "buffer": ""}
        else:
            index = step - 3 if mode == "poll" else step
            if index == 0:
                raw = bytes([65, 231, 227, 111, 10])
                result = {"status": 10000, "command": 0, "fieldType": 999123,
                          "minLength": 0, "maxLength": 0, "buffer": "Ação\n",
                          "bufferBase64": base64.b64encode(raw).decode("ascii")}
            elif index == 1:
                if params.get("bufferBase64") != base64.b64encode(bytes([65, 231, 227, 111, 10])).decode("ascii"):
                    result = {"status": -10}
                else:
                    result = {"status": 10000, "command": 30, "fieldType": 999124,
                              "minLength": 1, "maxLength": 4, "buffer": "Collect"}
            else:
                result = {"status": 0 if params.get("buffer") == "1234" else -41}
            step += 1
    elif method == "finish":
        if mode == "finish-crash":
            os._exit(9)
        result = {"returned": True, "received": params}
    elif method == "pendingCount":
        result = {"status": 1, "count": 1}
    elif method == "version":
        result = {"status": 0, "libraryVersion": {"text": "synthetic", "rawBase64": "", "decodingError": False}}
    elif method == "native-error":
        print(json.dumps({"id": request_id, "error": {"code": "MISSING_SYMBOL", "message": "Synthetic unavailable entrypoint"}}), flush=True)
        continue
    else:
        result = {"echo": params}
    print(json.dumps({"id": request_id, "result": result}, ensure_ascii=False), flush=True)

if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text("closed", encoding="utf-8")
if mode == "blocked-close":
    time.sleep(30)
