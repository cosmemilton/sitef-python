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
               "fiscalDate", "fiscalTime", "state", "updatedAt", "payments"}
PAYMENT_KEYS = {"id", "functionId", "entryPoint", "fiscalDocument", "fiscalDate", "fiscalTime", "state",
                "decision", "status", "paymentIndex", "network", "finalizationParameters", "receipts",
                "confirmationAllowed", "assessmentIssues", "finalizationPayload"}
PAYMENT_KEYS |= {"signatureRequired", "signatureCaptured", "legacyReceiptStateUnknown"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Journal(Protocol):
    async def acquire(self) -> None: ...
    async def read(self) -> dict[str, Any] | None: ...
    async def write(self, record: Mapping[str, Any]) -> None: ...
    async def release(self) -> None: ...


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] not in {1, 2}:
        raise SitefError("JOURNAL_INVALID", "Versão de journal inválida.")
    for key in ["id", "storeId", "terminalId", "fiscalDocument", "fiscalDate", "fiscalTime", "updatedAt"]:
        if not isinstance(record.get(key), str) or not record[key] or "\0" in record[key]:
            raise SitefError("JOURNAL_INVALID", "Journal sem identidade e estado válidos.")
    if (record.get("state") not in STATES or type(record.get("functionId")) is not int
            or not re.fullmatch(r"[0-9]{8}", record["fiscalDate"])
            or not re.fullmatch(r"[0-9]{6}", record["fiscalTime"])):
        raise SitefError("JOURNAL_INVALID", "Journal com identidade ou estado inválido.")
    value = dict(record)
    if value["version"] == 1:
        allowed = (RECORD_KEYS - {"payments"}) | {"decision", "status", "finalizationParameters"}
        if set(value) - allowed:
            raise SitefError("JOURNAL_INVALID", "Journal contém dados não autorizados.")
        payment = {key: entry for key, entry in value.items() if key in PAYMENT_KEYS}
        payment.update(legacyReceiptStateUnknown=True, confirmationAllowed=False)
        parameters = payment.get("finalizationParameters", "")
        for name, target in [("NumeroPagamentoCupom", "paymentIndex"), ("RedeConfirmacao", "network")]:
            match = re.search(r"\{" + name + r"=([0-9]+)\}", parameters)
            if match:
                payment[target] = match[1]
        value = {key: entry for key, entry in value.items() if key in RECORD_KEYS}
        value.update(version=2, payments=[payment])
    if set(value) - RECORD_KEYS or not isinstance(value.get("payments"), list) or not value["payments"]:
        raise SitefError("JOURNAL_INVALID", "Journal V2 sem pagamentos válidos.")
    ids = set()
    for payment in value["payments"]:
        if not isinstance(payment, dict) or set(payment) - PAYMENT_KEYS:
            raise SitefError("JOURNAL_INVALID", "Metadados de pagamento não autorizados.")
        for key in ("id", "fiscalDocument", "fiscalDate", "fiscalTime"):
            if not isinstance(payment.get(key), str) or not payment[key] or "\0" in payment[key]:
                raise SitefError("JOURNAL_INVALID", "Identidade de pagamento inválida.")
        if (payment["id"] in ids or payment.get("state") not in STATES
                or type(payment.get("functionId")) is not int
                or not re.fullmatch(r"[0-9]{8}", payment["fiscalDate"])
                or not re.fullmatch(r"[0-9]{6}", payment["fiscalTime"])):
            raise SitefError("JOURNAL_INVALID", "Pagamento duplicado ou inválido.")
        ids.add(payment["id"])
        if "confirmationAllowed" in payment and type(payment["confirmationAllowed"]) is not bool:
            raise SitefError("JOURNAL_INVALID", "Avaliação de confirmação inválida.")
        if any(key in payment and type(payment[key]) is not bool for key in ("signatureRequired", "signatureCaptured", "legacyReceiptStateUnknown")):
            raise SitefError("JOURNAL_INVALID", "Metadados de assinatura inválidos.")
        if "assessmentIssues" in payment and (not isinstance(payment["assessmentIssues"], list) or any(not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]+", code) for code in payment["assessmentIssues"])):
            raise SitefError("JOURNAL_INVALID", "Códigos de avaliação inválidos.")
        if "decision" in payment and (type(payment["decision"]) is not int or payment["decision"] not in {0, 1}):
            raise SitefError("JOURNAL_INVALID", "Decisão persistida inválida.")
        if "status" in payment and type(payment["status"]) is not int:
            raise SitefError("JOURNAL_INVALID", "Retorno persistido inválido.")
        for key in ("paymentIndex", "network"):
            if key in payment and (not isinstance(payment[key], str) or not re.fullmatch(r"[0-9]+", payment[key])):
                raise SitefError("JOURNAL_INVALID", "Seletor persistido inválido.")
        if "finalizationParameters" in payment and (not isinstance(payment["finalizationParameters"], str) or "\0" in payment["finalizationParameters"]):
            raise SitefError("JOURNAL_INVALID", "Escopo persistido inválido.")
        if "finalizationPayload" in payment:
            payload = payment["finalizationPayload"]
            if (not isinstance(payload, dict) or set(payload) != {"id", "hash"}
                    or not isinstance(payload["id"], str) or not payload["id"] or "\0" in payload["id"]
                    or not isinstance(payload["hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["hash"])):
                raise SitefError("JOURNAL_INVALID", "Metadados de payload de finalização inválidos.")
        receipts = payment.get("receipts", [])
        if not isinstance(receipts, list):
            raise SitefError("JOURNAL_INVALID", "Metadados de recibos inválidos.")
        receipt_ids = set()
        for receipt in receipts:
            if (not isinstance(receipt, dict) or set(receipt) != {"id", "fieldType", "type", "timing", "hash", "printed"}
                    or not isinstance(receipt["id"], str) or "\0" in receipt["id"] or receipt["id"] in receipt_ids
                    or not re.fullmatch(re.escape(payment["id"]) + r":(?:121|122):[1-9][0-9]*", receipt["id"])
                    or receipt["fieldType"] not in {121, 122}
                    or receipt["timing"] not in {"immediate", "deferred"}
                    or not isinstance(receipt["hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["hash"])
                    or type(receipt["printed"]) is not bool or (receipt["type"] is not None and not isinstance(receipt["type"], str))):
                raise SitefError("JOURNAL_INVALID", "Journal contém corpo ou metadados inválidos de recibo.")
            receipt_ids.add(receipt["id"])
    if value["id"] != value["payments"][0]["id"]:
        raise SitefError("JOURNAL_INVALID", "Identidade do grupo diverge do primeiro pagamento.")
    return value


class FileJournal:
    """Use one stable local path per (store, terminal); never auto-delete stale locks.

    Version 1 single-payment records are migrated in memory to version 2.
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
            if any(re.search(r"\{NFPAG\s*=", p.get("finalizationParameters", ""), re.I) for p in checked["payments"]):
                raise SitefError("LEGACY_PAYLOAD_MIGRATION_REQUIRED", "O payload NFPAG deve ficar no armazenamento externo da aplicação.")
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
