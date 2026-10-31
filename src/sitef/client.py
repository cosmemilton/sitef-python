"""Explicit fiscal decisions and cooperative transaction interaction."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
from datetime import datetime
import re
from typing import Any, Awaitable, Mapping
from uuid import uuid4

from .catalog import command_info
from .errors import SitefError
from .journal import Journal, utc_now, validate_record
from .models import (Configuration, DataField, ExchangeObserver, InteractionContext,
                     InteractionHandler, NativeExchange, NativeStart, TransactionInput,
                     TransactionResult, UiResponse)
from .transport import Transport

_STARTS = {"startBanking", "readSecureCard", "readTrack3", "readPassword", "readChip"}
_RESERVED = _STARTS | {"load", "configure", "configureEx", "start", "continue", "finish", "readChipEx"}
_READS = {"pendingCount", "version", "capabilities"}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise SitefError("INVALID_ARGUMENT", f"{name} deve ser texto sem NUL.")
    return value


def _status(value: Mapping[str, Any]) -> int:
    if not isinstance(value, Mapping) or type(value.get("status")) is not int:
        raise SitefError("INVALID_NATIVE_RESULT", "Retorno nativo sem status inteiro.", uncertain=True)
    return value["status"]


class SitefClient:
    def __init__(self, *, configuration: Configuration, transport: Transport, journal: Journal,
                 interaction: InteractionHandler, on_exchange: ExchangeObserver | None,
                 native_encoding: str, buffer_size: int, max_steps: int) -> None:
        self._configuration = configuration
        self._transport, self._journal = transport, journal
        self._interaction, self._observer = interaction, on_exchange
        self._encoding = {"windows-1252": "cp1252", "utf-8": "utf-8", "ascii": "ascii"}.get(native_encoding, "")
        if not self._encoding:
            raise SitefError("INVALID_OPTIONS", "Codificação nativa não suportada.")
        self._buffer_size, self._max_steps = buffer_size, max_steps
        self._state = "idle"
        self._operation = False
        self._record: dict[str, Any] | None = None
        self._cancel_event: asyncio.Event | None = None
        self._collect_command: int | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def recovery(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._record)

    def _bytes(self, value: str, name: str) -> bytes:
        try:
            return _text(value, name).encode(self._encoding, errors="strict")
        except UnicodeError as exc:
            raise SitefError("ENCODING_ERROR", f"{name} não pode ser representado na codificação nativa.") from exc

    @classmethod
    async def connect(cls, *, configuration: Configuration, transport: Transport, journal: Journal,
                      interaction: InteractionHandler, on_exchange: ExchangeObserver | None = None,
                      native_encoding: str = "windows-1252", buffer_size: int = 20000,
                      max_steps: int = 100000) -> SitefClient:
        if (not isinstance(configuration, Configuration) or not callable(interaction)
                or (on_exchange is not None and not callable(on_exchange))):
            raise SitefError("INVALID_ARGUMENT", "Configuração e callbacks inválidos.")
        if type(buffer_size) is not int or not 20000 <= buffer_size <= 1048576:
            raise SitefError("INVALID_BUFFER", "buffer_size deve estar entre 20000 e 1048576 bytes.")
        if type(max_steps) is not int or max_steps < 1:
            raise SitefError("INVALID_OPTIONS", "max_steps inválido.")
        self = cls(configuration=configuration, transport=transport, journal=journal,
                   interaction=interaction, on_exchange=on_exchange, native_encoding=native_encoding,
                   buffer_size=buffer_size, max_steps=max_steps)
        config = configuration
        for name, value in config.native().items():
            if name != "reserved":
                self._bytes(value, name)
        if (not config.server or not re.fullmatch(r"[0-9]{8}", config.store_id)
                or not re.fullmatch(r"[A-Za-z]{2}[0-9]{6}", config.terminal_id)
                or 900 <= int(config.terminal_id[2:]) <= 999
                or type(config.reserved) is not int or config.reserved != 0):
            raise SitefError("INVALID_CONFIGURATION", "Loja, terminal, servidor ou reservado inválido.")
        acquired = False
        try:
            await journal.acquire()
            acquired = True
            record = await journal.read()
            self._record = validate_record(record) if record is not None else None
            if record and (record["storeId"] != config.store_id or record["terminalId"] != config.terminal_id):
                raise SitefError("JOURNAL_TERMINAL_MISMATCH", "Journal pertence a outro terminal.")
            status = _status(await transport.call("configure", config.native()))
            if status != 0:
                raise SitefError("CONFIGURATION_FAILED", f"CliSiTef recusou a configuração ({status}).", native_status=status)
            if record and record["state"] not in {"ended", "finalized-local"}:
                self._state = "uncertain"
            return self
        except BaseException:
            try:
                await transport.close()
            finally:
                if acquired:
                    await journal.release()
            raise

    def _validate_input(self, value: TransactionInput) -> None:
        if not isinstance(value, TransactionInput) or type(value.function_id) is not int or not -(2**31) <= value.function_id < 2**31:
            raise SitefError("INVALID_ARGUMENT", "function_id deve ser int32.")
        for name, entry in value.native().items():
            if name != "functionId":
                self._bytes(entry, name)
        if value.amount != "" and not re.fullmatch(r"[0-9]+,[0-9]{2}", value.amount):
            raise SitefError("INVALID_AMOUNT", "amount deve ser decimal textual com duas casas e vírgula, ou vazio.")
        if (not value.fiscal_document or len(self._bytes(value.fiscal_document, "fiscal_document")) > 20
                or len(self._bytes(value.operator, "operator")) > 20
                or not re.fullmatch(r"[0-9]{8}", value.fiscal_date)
                or not re.fullmatch(r"[0-9]{6}", value.fiscal_time)):
            raise SitefError("INVALID_FISCAL_ID", "Identidade fiscal inválida ou fora dos limites nativos.")
        try:
            datetime.strptime(value.fiscal_date + value.fiscal_time, "%Y%m%d%H%M%S")
        except ValueError as exc:
            raise SitefError("INVALID_FISCAL_ID", "Data ou hora fiscal inválida.") from exc

    def cancel(self) -> None:
        """Request native cancellation at the next safe continuation point."""
        if self._cancel_event is not None:
            self._cancel_event.set()

    async def _save(self, state: str, **patch: Any) -> None:
        assert self._record is not None
        self._record = {**self._record, **patch, "state": state, "updatedAt": utc_now()}
        await self._journal.write(self._record)

    @staticmethod
    def _consume(task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    async def _until_cancel(self, operation: Awaitable[Any], cancel_event: asyncio.Event,
                            aborted_value: Any) -> Any:
        task = asyncio.ensure_future(operation)
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({task, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                task.cancel()
                task.add_done_callback(self._consume)
                return aborted_value
            return task.result()
        finally:
            cancelled.cancel()
            if not task.done():
                task.cancel()
                task.add_done_callback(self._consume)

    def _validate_response(self, response: Any) -> UiResponse:
        if not isinstance(response, UiResponse) or type(response.continue_flag) is not int or response.continue_flag not in {-1, 0, 1, 2, 10000}:
            raise SitefError("INVALID_RESPONSE", "Callback deve retornar UiResponse com controle documentado.")
        self._bytes(response.buffer, "buffer")
        if response.buffer_base64 is not None:
            try:
                raw = base64.b64decode(response.buffer_base64, validate=True)
            except (ValueError, TypeError) as exc:
                raise SitefError("INVALID_RESPONSE", "buffer_base64 inválido.") from exc
            if b"\0" in raw or len(raw) >= self._buffer_size:
                raise SitefError("INVALID_RESPONSE", "Bytes coletados inválidos ou muito longos.")
        return response

    async def transact(self, value: TransactionInput, *, requires_finalization: bool,
                       native_start: NativeStart | None = None,
                       cancel_event: asyncio.Event | None = None) -> TransactionResult:
        """Every operation declares whether successful completion needs finish()."""
        if self._state != "idle" or self._operation:
            raise SitefError("SESSION_BUSY", f"Sessão em {self._state}.")
        self._validate_input(value)
        if type(requires_finalization) is not bool:
            raise SitefError("INVALID_ARGUMENT", "requires_finalization deve ser uma decisão booleana explícita.")
        if native_start is not None and (not isinstance(native_start, NativeStart) or native_start.method not in _STARTS
                                          or not isinstance(native_start.params, Mapping)):
            raise SitefError("INVALID_ARGUMENT", "Rotina inicial não suportada pela sessão.")
        cancellation = cancel_event if cancel_event is not None else asyncio.Event()
        if not isinstance(cancellation, asyncio.Event):
            raise SitefError("INVALID_ARGUMENT", "cancel_event deve ser asyncio.Event.")
        if cancellation.is_set():
            raise SitefError("ABORTED", "Cancelado antes do início.")
        self._operation, self._cancel_event = True, cancellation
        self._record = {"version": 1, "id": str(uuid4()), "storeId": self._configuration.store_id,
                        "terminalId": self._configuration.terminal_id, "functionId": value.function_id,
                        "entryPoint": native_start.method if native_start else "start",
                        "fiscalDocument": value.fiscal_document, "fiscalDate": value.fiscal_date,
                        "fiscalTime": value.fiscal_time, "state": "started", "updatedAt": utc_now()}
        fields: list[DataField] = []
        started = False
        interaction_failure: list[BaseException] = []
        observers: set[asyncio.Task[Any]] = set()
        try:
            await self._save("started")
            self._state = "running"
            method, params = (native_start.method, dict(native_start.params)) if native_start else ("start", value.native())
            if method == "startBanking":
                params.update(fiscalDocument=value.fiscal_document, fiscalDate=value.fiscal_date,
                              fiscalTime=value.fiscal_time, operator=value.operator)
            started = True
            status = _status(await self._transport.call(method, params))
            response = UiResponse()
            steps = 0
            while status == 10000:
                steps += 1
                if steps > self._max_steps:
                    raise SitefError("LOOP_LIMIT", "Limite de passos excedido; reconciliar estado.", uncertain=True)
                cancelled = cancellation.is_set() or bool(interaction_failure)
                params = {"buffer": "" if cancelled else response.buffer,
                          "continueFlag": -1 if cancelled else response.continue_flag,
                          "bufferSize": self._buffer_size}
                if not cancelled and response.buffer_base64 is not None:
                    params["bufferBase64"] = response.buffer_base64
                raw = await self._transport.call("continue", params)
                status = _status(raw)
                if status != 10000:
                    break
                request = NativeExchange.from_native(raw, command_info(raw.get("command", -999999))["kind"])
                response = UiResponse()
                if request.command == 0:
                    fields.append(DataField(request.field_type, request.buffer, request.buffer_base64, request.decoding_error))
                    response = UiResponse(request.buffer, buffer_base64=request.buffer_base64)
                if cancelled:
                    continue
                try:
                    if request.command == 23:
                        # Never await rendering/device polling observers here.
                        if self._observer is not None:
                            task = asyncio.create_task(self._observer(request))
                            observers.add(task)
                            def observed(done: asyncio.Task[Any]) -> None:
                                observers.discard(done)
                                if not done.cancelled() and done.exception() is not None:
                                    interaction_failure.append(done.exception())
                                    cancellation.set()
                            task.add_done_callback(observed)
                        continue
                    if self._observer is not None:
                        await self._until_cancel(self._observer(request), cancellation, None)
                    if request.command != 0 and not cancellation.is_set():
                        self._collect_command = request.command
                        response = self._validate_response(await self._until_cancel(
                            self._interaction(request, InteractionContext(cancellation)), cancellation,
                            UiResponse(continue_flag=-1)))
                        self._collect_command = None
                    if response.continue_flag == -1:
                        cancellation.set()
                except Exception as exc:
                    interaction_failure.append(exc)
                    cancellation.set()
                    self._collect_command = None
            needs_finish = status == 0 and requires_finalization
            self._state = "awaiting-finalization" if needs_finish else "idle"
            await self._save("awaiting-finalization" if needs_finish else "ended", status=status)
            if interaction_failure:
                raise SitefError("INTERACTION_FAILED", f"Interação falhou; ciclo terminou com retorno {status}. Confira state.", native_status=status) from interaction_failure[0]
            return TransactionResult(status, self._record["id"], needs_finish, tuple(fields))
        except asyncio.CancelledError:
            if started:
                self._state = "uncertain"
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._save("uncertain"))
            raise
        except BaseException as exc:
            if started and not (isinstance(exc, SitefError) and exc.code == "INTERACTION_FAILED"):
                self._state = "uncertain"
                with contextlib.suppress(Exception):
                    await self._save("uncertain")
                raise SitefError("TRANSACTION_UNCERTAIN", "Resultado indeterminado. Não repetir; reconciliar pendências.", uncertain=True) from exc
            raise
        finally:
            for task in observers:
                task.cancel()
                task.add_done_callback(self._consume)
            self._collect_command = None
            self._cancel_event, self._operation = None, False

    async def finalize(self, confirm: bool, additional_parameters: str = "") -> dict[str, Any]:
        if self._state != "awaiting-finalization" or self._operation:
            raise SitefError("INVALID_STATE", "Não há transação aguardando decisão.")
        return await self._finish(confirm, additional_parameters)

    async def finalize_recovery(self, confirm: bool, expected_transaction_id: str,
                                additional_parameters: str | None = None) -> dict[str, Any]:
        if self._state != "uncertain" or self._operation or self._record is None or self._record["id"] != expected_transaction_id:
            raise SitefError("INVALID_RECOVERY", "Reconciliação exige estado incerto e identidade exata do journal.")
        if type(confirm) is not bool:
            raise SitefError("INVALID_ARGUMENT", "confirm deve ser boolean.")
        if "decision" in self._record and self._record["decision"] != int(confirm):
            raise SitefError("RECOVERY_DECISION_MISMATCH", "A decisão diverge da persistida.")
        previous = self._record.get("finalizationParameters")
        if additional_parameters is not None and previous is not None and additional_parameters != previous:
            raise SitefError("RECOVERY_SCOPE_MISMATCH", "O escopo da finalização persistida não pode ser alterado.")
        return await self._finish(confirm, previous if additional_parameters is None and previous is not None else additional_parameters or "")

    async def _finish(self, confirm: bool, additional_parameters: str) -> dict[str, Any]:
        if type(confirm) is not bool:
            raise SitefError("INVALID_ARGUMENT", "confirm deve ser boolean.")
        self._bytes(additional_parameters, "additional_parameters")
        self._operation = True
        try:
            await self._save("finalizing", decision=int(confirm), finalizationParameters=additional_parameters)
            assert self._record is not None
            result = await self._transport.call("finish", {
                "confirm": int(confirm), "fiscalDocument": self._record["fiscalDocument"],
                "fiscalDate": self._record["fiscalDate"], "fiscalTime": self._record["fiscalTime"],
                "additionalParameters": additional_parameters})
            if result.get("returned") is not True:
                raise SitefError("INVALID_NATIVE_RESULT", "Finalização sem retorno local válido.", uncertain=True)
            await self._save("finalized-local")
            self._state = "idle"
            return {"returned": True, "remoteConfirmation": "not-reported"}
        except BaseException as exc:
            self._state = "uncertain"
            with contextlib.suppress(Exception):
                await asyncio.shield(self._save("uncertain"))
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SitefError("FINALIZATION_UNCERTAIN", "Finalização incerta; consultar pendências sem repetição automática.", uncertain=True) from exc
        finally:
            self._operation = False

    async def call_native(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _text(method, "method")
        if self._operation or self._state not in {"idle", "uncertain"}:
            raise SitefError("SESSION_BUSY", "Auxiliares indisponíveis durante ciclo ou decisão.")
        if method in _RESERVED:
            raise SitefError("RESERVED_METHOD", "Use as operações da sessão para esta rotina.")
        if self._state == "uncertain" and method not in _READS:
            raise SitefError("RECOVERY_REQUIRED", "Somente consultas de recuperação são permitidas.")
        self._operation = True
        try:
            return await self._transport.call(method, params)
        finally:
            self._operation = False

    async def capabilities(self) -> dict[str, Any]:
        return await self.call_native("capabilities")

    async def validate_barcode(self, data: str) -> dict[str, Any]:
        self._bytes(data, "data")
        if self._state == "running":
            if self._collect_command not in {31, 35}:
                raise SitefError("INVALID_STATE", "Validação durante o laço exige coleta de cheque ou barras.")
            return await self._transport.call("validateBarcode", {"data": data})
        return await self.call_native("validateBarcode", {"data": data})

    async def close(self) -> None:
        if self._operation or self._state == "running":
            raise SitefError("SESSION_BUSY", "Cancele cooperativamente e aguarde o ciclo antes de fechar.")
        if self._state == "awaiting-finalization":
            raise SitefError("FINALIZATION_REQUIRED", "Confirme ou desfaça antes de fechar.")
        if self._state == "closed":
            return
        # Release exclusivity only after helper termination is established.
        await self._transport.close()
        await self._journal.release()
        self._state = "closed"
