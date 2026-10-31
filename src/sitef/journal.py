"""Durable terminal decisions, exclusive lease, no payment buffers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .errors import SitefError

STATES = {"started", "awaiting-finalization", "finalizing", "finalized-local", "ended", "uncertain"}
RECORD_KEYS = {"version", "id", "storeId", "terminalId", "functionId", "entryPoint", "fiscalDocument",
               "fiscalDate", "fiscalTime", "state", "decision", "status", "updatedAt", "finalizationParameters"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Journal(Protocol):
    async def acquire(self) -> None: ...
    async def read(self) -> dict[str, Any] | None: ...
    async def write(self, record: Mapping[str, Any]) -> None: ...
    async def release(self) -> None: ...


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1:
        raise SitefError("JOURNAL_INVALID", "Versão de journal inválida.")
    for key in ["id", "storeId", "terminalId", "fiscalDocument", "fiscalDate", "fiscalTime", "updatedAt"]:
        if not isinstance(record.get(key), str) or not record[key] or "\0" in record[key]:
            raise SitefError("JOURNAL_INVALID", "Journal sem identidade e estado válidos.")
    if (record.get("state") not in STATES or type(record.get("functionId")) is not int
            or not re.fullmatch(r"[0-9]{8}", record["fiscalDate"])
            or not re.fullmatch(r"[0-9]{6}", record["fiscalTime"])):
        raise SitefError("JOURNAL_INVALID", "Journal com identidade ou estado inválido.")
    if "decision" in record and (type(record["decision"]) is not int or record["decision"] not in {0, 1}):
        raise SitefError("JOURNAL_INVALID", "Decisão persistida inválida.")
    if "status" in record and type(record["status"]) is not int:
        raise SitefError("JOURNAL_INVALID", "Retorno persistido inválido.")
    if "finalizationParameters" in record and (not isinstance(record["finalizationParameters"], str)
                                                or "\0" in record["finalizationParameters"]):
        raise SitefError("JOURNAL_INVALID", "Escopo persistido inválido.")
    return dict(record)


class FileJournal:
    """Use one stable local path per (store, terminal); never auto-delete stale locks.

    Record keys intentionally match the Node client's version 1 journal.
    Unknown record versions are rejected instead of being overwritten.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise SitefError("INVALID_PATH", "Journal precisa de caminho absoluto e persistente.")
        self._lock_path = Path(str(self.path) + ".lock")
        self._fd: int | None = None
        self._token: str | None = None
        self._io_lock = asyncio.Lock()

    def _sync_directory(self) -> None:
        if os.name != "nt":
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    async def acquire(self) -> None:
        async with self._io_lock:
            if self._fd is not None:
                raise SitefError("TERMINAL_LOCKED", "Journal já está em uso.")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise SitefError("TERMINAL_LOCKED", "Terminal bloqueado; confira o processo e reconcilie antes de remover um lock antigo.") from exc
            token = uuid4().hex
            self._fd, self._token = fd, token
            try:
                payload = json.dumps({"pid": os.getpid(), "createdAt": utc_now(), "token": token}).encode("utf-8")
                with os.fdopen(os.dup(fd), "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._sync_directory()
            except BaseException:
                os.close(fd)
                self._fd = self._token = None
                self._lock_path.unlink(missing_ok=True)
                raise

    async def read(self) -> dict[str, Any] | None:
        async with self._io_lock:
            if self._fd is None:
                raise SitefError("JOURNAL_UNLOCKED", "Journal sem exclusividade.")
            try:
                if self.path.stat().st_size > 1024 * 1024:
                    raise ValueError("Oversized journal")
                value = json.loads(self.path.read_text(encoding="utf-8"))
                return validate_record(value)
            except FileNotFoundError:
                return None
            except (OSError, ValueError, TypeError, SitefError) as exc:
                raise SitefError("JOURNAL_INVALID", "Journal não pode ser validado; reconciliação necessária.") from exc

    async def write(self, record: Mapping[str, Any]) -> None:
        async with self._io_lock:
            if self._fd is None:
                raise SitefError("JOURNAL_UNLOCKED", "Journal sem exclusividade.")
            checked = validate_record(dict(record))
            if set(checked) - RECORD_KEYS:
                raise SitefError("JOURNAL_INVALID", "Campos de journal não autorizados; não grave buffers financeiros.")
            payload = json.dumps(checked, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > 1024 * 1024:
                raise SitefError("JOURNAL_INVALID", "Registro de journal excede o limite.")
            temporary = Path(str(self.path) + "." + uuid4().hex + ".lock.tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                self._sync_directory()
            finally:
                temporary.unlink(missing_ok=True)

    async def release(self) -> None:
        async with self._io_lock:
            if self._fd is None:
                return
            try:
                value = json.loads(self._lock_path.read_text(encoding="utf-8"))
                if value.get("token") != self._token:
                    raise SitefError("JOURNAL_LOCK_CHANGED", "Lock foi alterado; não será removido automaticamente.")
                os.close(self._fd)
                self._fd = None
                self._lock_path.unlink()
                self._sync_directory()
                self._token = None
            except (OSError, ValueError, AttributeError) as exc:
                raise SitefError("JOURNAL_LOCK_CHANGED", "Não foi possível liberar o lock com segurança.") from exc
