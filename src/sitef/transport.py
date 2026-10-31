"""Bounded, serialized NDJSON over an isolated helper's stdin/stdout."""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
from typing import Any, Mapping, Protocol, Sequence

from .errors import SitefError


class Transport(Protocol):
    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]: ...
    async def close(self) -> None: ...


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


class BridgeTransport:
    """No shell, network port, automatic restart or automatic operation retry.

    On Linux, test_mode=True is required and only synthetic fixtures are
    supported. It is not production Linux support for the vendor library.
    """

    def __init__(self, process: asyncio.subprocess.Process, *, timeout: float,
                 max_frame_bytes: int, test_mode: bool) -> None:
        self._process = process
        self._timeout = timeout
        self._max_frame_bytes = max_frame_bytes
        self._test_mode = test_mode
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._dead = False
        self._close_timeout = 5.0
        self._stderr_task = asyncio.create_task(self._discard_stderr())

    @classmethod
    async def open(cls, executable: str | os.PathLike[str], *, cwd: str | os.PathLike[str],
                   args: Sequence[str] = (), timeout: float = 120.0,
                   max_frame_bytes: int = 2 * 1024 * 1024,
                   test_mode: bool = False) -> BridgeTransport:
        executable_path, cwd_path = Path(executable), Path(cwd)
        if not executable_path.is_absolute() or not cwd_path.is_absolute():
            raise SitefError("INVALID_PATH", "executable e cwd precisam ser absolutos.")
        if type(test_mode) is not bool:
            raise SitefError("INVALID_OPTIONS", "test_mode deve ser boolean.")
        if not test_mode and not (sys.platform == "win32" and struct.calcsize("P") == 8
                                  and platform.machine().lower() in {"amd64", "x86_64"}):
            raise SitefError("UNSUPPORTED_PLATFORM", "O runtime de produção é somente Windows x64.")
        if (type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0
                or type(max_frame_bytes) is not int or not 1024 <= max_frame_bytes <= 2 * 1024 * 1024):
            raise SitefError("INVALID_OPTIONS", "Limites IPC inválidos.")
        if isinstance(args, (str, bytes)) or any(not isinstance(arg, str) for arg in args):
            raise SitefError("INVALID_OPTIONS", "args deve ser uma sequência de argumentos.")
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable_path), *args, cwd=str(cwd_path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=max_frame_bytes + 1, **options)
        except (OSError, ValueError) as exc:
            raise SitefError("BRIDGE_SPAWN", "Não foi possível iniciar a ponte nativa.") from exc
        return cls(process, timeout=float(timeout), max_frame_bytes=max_frame_bytes, test_mode=test_mode)

    @property
    def closed(self) -> bool:
        return self._dead

    async def load(self, library_path: str | os.PathLike[str], *,
                   working_directory: str | os.PathLike[str] | None = None,
                   native_encoding: str = "windows-1252") -> dict[str, Any]:
        path = Path(library_path)
        if not path.is_absolute():
            raise SitefError("INVALID_PATH", "library_path precisa ser absoluto.")
        params: dict[str, Any] = {"libraryPath": str(path), "nativeEncoding": native_encoding,
                                  "testMode": self._test_mode}
        if working_directory is not None:
            directory = Path(working_directory)
            if not directory.is_absolute():
                raise SitefError("INVALID_PATH", "working_directory precisa ser absoluto.")
            params["workingDirectory"] = str(directory)
        return await self.call("load", params)

    async def _discard_stderr(self) -> None:
        # External diagnostics can include sensitive buffers; do not retain/log.
        assert self._process.stderr is not None
        with contextlib.suppress(OSError):
            while await self._process.stderr.read(65536):
                pass

    async def _discard_stdout(self) -> None:
        # After a fatal frame, drain bounded chunks so a paused StreamReader
        # cannot prevent subprocess.wait() from observing pipe shutdown.
        assert self._process.stdout is not None
        with contextlib.suppress(OSError):
            while await self._process.stdout.read(65536):
                pass

    def _fail(self) -> None:
        self._dead = True
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            if self._dead:
                raise SitefError("BRIDGE_CLOSED", "Ponte encerrada; reconcilie a operação antes de prosseguir.", uncertain=True)
            if not isinstance(method, str) or not method or not isinstance(params if params is not None else {}, Mapping):
                raise SitefError("INVALID_ARGUMENT", "Método e parâmetros inválidos.")
            request_id = self._next_id
            try:
                frame = (json.dumps({"id": request_id, "method": method, "params": dict(params or {})},
                                    ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            except (ValueError, TypeError, UnicodeError) as exc:
                raise SitefError("INVALID_ARGUMENT", "Parâmetros não podem ser codificados no protocolo.") from exc
            if len(frame) > self._max_frame_bytes:
                raise SitefError("FRAME_TOO_LARGE", "Requisição excede o limite IPC.")
            self._next_id += 1
            try:
                response = await asyncio.wait_for(self._exchange(frame, request_id), self._timeout)
            except asyncio.TimeoutError as exc:
                self._fail()
                raise SitefError("BRIDGE_TIMEOUT", "A ponte excedeu o prazo; o resultado nativo é incerto.", uncertain=True) from exc
            except asyncio.CancelledError:
                # A cancelled Python task cannot safely match a later response.
                self._fail()
                raise
            except SitefError as exc:
                if exc.uncertain:
                    self._fail()
                raise
            except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError) as exc:
                self._fail()
                raise SitefError("INVALID_FRAME", "Falha no canal da ponte; reconcilie antes de prosseguir.", uncertain=True) from exc
            return response

    async def _exchange(self, frame: bytes, request_id: int) -> dict[str, Any]:
        assert self._process.stdin is not None and self._process.stdout is not None
        try:
            self._process.stdin.write(frame)
            await self._process.stdin.drain()
            reply_line = await self._process.stdout.readline()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise SitefError("BRIDGE_PIPE", "Canal da ponte interrompido.", uncertain=True) from exc
        except ValueError as exc:
            raise SitefError("FRAME_TOO_LARGE", "Resposta excede o limite IPC.", uncertain=True) from exc
        if not reply_line:
            raise SitefError("BRIDGE_EOF", "A ponte encerrou sem resposta; o resultado pode ser incerto.", uncertain=True)
        if len(reply_line) > self._max_frame_bytes or not reply_line.endswith(b"\n"):
            raise SitefError("INVALID_FRAME", "Resposta incompleta ou excedendo o limite IPC.", uncertain=True)
        try:
            reply = json.loads(reply_line.decode("utf-8", errors="strict"), object_pairs_hook=_json_object,
                               parse_constant=_invalid_constant)
        except (ValueError, UnicodeError) as exc:
            raise SitefError("INVALID_FRAME", "A ponte retornou um frame inválido.", uncertain=True) from exc
        if (not isinstance(reply, dict) or type(reply.get("id")) is not int or reply["id"] != request_id
                or (("result" in reply) == ("error" in reply))):
            raise SitefError("INVALID_FRAME", "Resposta sem correspondência com a requisição.", uncertain=True)
        if "error" in reply:
            error = reply["error"]
            if (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                    or not isinstance(error.get("message"), str)):
                raise SitefError("INVALID_FRAME", "Erro nativo malformado.", uncertain=True)
            raise SitefError(error["code"], error["message"])
        if not isinstance(reply["result"], dict):
            raise SitefError("INVALID_FRAME", "Resultado nativo malformado.", uncertain=True)
        return reply["result"]

    async def close(self) -> None:
        if not self._dead:
            self._dead = True
            if self._lock.locked():
                self._fail()
            elif self._process.stdin is not None:
                self._process.stdin.close()
        async with self._lock:
            drain = asyncio.create_task(self._discard_stdout())
            try:
                await asyncio.wait_for(self._process.wait(), self._close_timeout)
            except asyncio.TimeoutError as exc:
                self._fail()
                try:
                    await asyncio.wait_for(self._process.wait(), 2.0)
                except asyncio.TimeoutError:
                    raise SitefError("BRIDGE_CLOSE_TIMEOUT", "O processo auxiliar não encerrou; mantenha a exclusividade do terminal.", uncertain=True) from exc
                raise SitefError("BRIDGE_CLOSE_TIMEOUT", "A ponte precisou ser encerrada à força; verifique o processo antes de liberar o terminal.", uncertain=True) from exc
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await drain
                if self._process.returncode is not None:
                    self._stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._stderr_task

    async def __aenter__(self) -> BridgeTransport:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()
