"""Authenticated loopback transport against own C fixture, never vendor code."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
subprocess.run(["cargo", "build", "--locked", "--manifest-path", str(ROOT / "Cargo.toml")], check=True)
with tempfile.TemporaryDirectory(prefix="sitef-loopback-contract-") as directory:
    shared = Path(directory) / "libsynthetic.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", str(ROOT / "tests/fixture_tcp.c"), "-o", str(shared)], check=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        nonce = secrets.token_hex(32)
        environment = dict(os.environ, SITEF_BRIDGE_TOKEN=nonce)
        endpoint = "127.0.0.1:" + str(listener.getsockname()[1])
        child = subprocess.Popen([str(ROOT / "target/debug/sitef-bridge"), "--connect", endpoint, "--token-env", "SITEF_BRIDGE_TOKEN"], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            connection, address = listener.accept()
            assert address[0] == "127.0.0.1"
            with connection:
                connection.settimeout(10)
                wire = connection.makefile("rwb", buffering=0)
                def receive():
                    frame = wire.readline(2 * 1024 * 1024 + 1)
                    assert frame.endswith(b"\n") and len(frame) <= 2 * 1024 * 1024
                    return json.loads(frame)
                hello = receive()
                assert hello["protocolVersion"] == 1 and hello["transport"] == "tcp-loopback"
                assert secrets.compare_digest(hello["token"], nonce)
                sequence = 0
                def call(method, params=None):
                    global sequence
                    sequence += 1
                    wire.write((json.dumps({"id": sequence, "method": method, "params": params or {}}) + "\n").encode("utf-8"))
                    reply = receive()
                    assert reply["id"] == sequence and "error" not in reply, reply
                    return reply["result"]
                assert call("load", {"libraryPath": str(shared), "testMode": True})["runtimeTarget"] == "windows-x64"
                # The fixture rejects configuration if the nonce remains in getenv.
                assert call("configure", {"server": "127.0.0.1", "storeId": "00000000", "terminalId": "AA123456", "additionalParameters": "[X=1]"})["status"] == 0
                start = {"functionId": 0, "amount": "12,34", "fiscalDocument": "123", "fiscalDate": "20261030", "fiscalTime": "120000", "operator": "test", "additionalParameters": "{X=1}"}
                assert call("start", start)["status"] == 10000
                step = call("continue", {"buffer": "", "continueFlag": 0, "bufferSize": 20000})
                assert step["command"] == 0 and step["buffer"] == "Ação\n"
                step = call("continue", {"bufferBase64": step["bufferBase64"], "continueFlag": 0, "bufferSize": 20000})
                assert step["command"] == 30 and step["fieldType"] == 999123
                assert call("continue", {"buffer": "1234", "continueFlag": 0, "bufferSize": 20000})["status"] == 0
                wire.close()
            assert child.wait(timeout=5) == 0
            assert child.stdout.read() == b"" and child.stderr.read() == b""
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
print("Own fixture: authenticated loopback handshake, nonce removal and NDJSON checks passed")
