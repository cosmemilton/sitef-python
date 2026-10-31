"""Explicit fiscal decisions and cooperative transaction interaction."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
from dataclasses import replace
from datetime import datetime
import re
from typing import Any, Awaitable, Mapping
from uuid import uuid4

from .catalog import command_info
from .errors import SitefError
from .journal import Journal, utc_now, validate_record
from .fields import FieldHooks, FieldProcessor, document, invoke
from .payments import build_payment_parameters
from .models import (Configuration, DataField, ExchangeObserver, InteractionContext,
                     InteractionHandler, NativeExchange, NativeStart, TransactionInput,
                     TransactionResult, UiResponse, Receipt)
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
        self._hooks = FieldHooks()
        self._field_options: dict[str, Any] = {}
        self._processors: dict[str, FieldProcessor] = {}

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
                      max_steps: int = 100000, hooks: FieldHooks | None = None,
                      receipt_storage: str = "persist", monetary_formats: Mapping[int, str] | None = None,
                      confirmation_network_field: int | None = None) -> SitefClient:
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
        if hooks is not None and not isinstance(hooks, FieldHooks):
            raise SitefError("INVALID_OPTIONS", "hooks precisa ser FieldHooks.")
        self._hooks = hooks or FieldHooks()
        self._field_options = {"receipt_storage": receipt_storage, "monetary_formats": monetary_formats,
                               "confirmation_network_field": confirmation_network_field,
                               "native_encoding": native_encoding}
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
            # Legacy journals are preserved unless the application acknowledges
            # durable external storage before removing the sensitive payload.
            migrated_payload = False
            for payment in self._record.get("payments", []) if self._record else []:
                parameters = payment.get("finalizationParameters", "")
                if re.search(r"\{NFPAG\s*=", parameters, re.I):
                    if not callable(self._hooks.store_finalization_payload):
                        raise SitefError("LEGACY_PAYLOAD_MIGRATION_REQUIRED", "Migração NFPAG exige store_finalization_payload; o arquivo original foi preservado.")
                    payment["finalizationPayload"] = await self._store_finalization_payload(parameters, payment)
                    payment["finalizationParameters"] = re.sub(r"\{NFPAG\s*=[^{}]*\}", "", parameters, flags=re.I)
                    if re.search(r"\{NFPAG\s*=", payment["finalizationParameters"], re.I):
                        raise SitefError("LEGACY_PAYLOAD_MIGRATION_REQUIRED", "Payload legado precisa de migração explícita pelo aplicativo.")
                    migrated_payload = True
            if migrated_payload:
                await journal.write(self._record)
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

    def _validate_input(self, value: TransactionInput, *, allow_empty_fiscal: bool = False) -> None:
        if not isinstance(value, TransactionInput) or type(value.function_id) is not int or not -(2**31) <= value.function_id < 2**31:
            raise SitefError("INVALID_ARGUMENT", "function_id deve ser int32.")
        for name, entry in value.native().items():
            if name != "functionId":
                self._bytes(entry, name)
        if value.amount != "" and not re.fullmatch(r"[0-9]+,[0-9]{2}", value.amount):
            raise SitefError("INVALID_AMOUNT", "amount deve ser decimal textual com duas casas e vírgula, ou vazio.")
        if len(self._bytes(value.operator, "operator")) > 20:
            raise SitefError("INVALID_FISCAL_ID", "Operador excede o limite em bytes.")
        if allow_empty_fiscal and not any((value.fiscal_document, value.fiscal_date, value.fiscal_time)):
            return
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
        if response.confirmed is not None and type(response.confirmed) is not bool:
            raise SitefError("INVALID_RESPONSE", "confirmed deve ser booleano quando informado.")
        if response.continue_flag == 10000 and response.confirmed is not True:
            raise SitefError("PRECOLLECTED_CONFIRMATION_REQUIRED", "Controle10000 exige dado pré-coletado confirmado explicitamente pelo operador.")
        text_bytes = self._bytes(response.buffer, "buffer")
        if response.buffer_base64 is not None:
            try:
                raw = base64.b64decode(response.buffer_base64, validate=True)
            except (ValueError, TypeError) as exc:
                raise SitefError("INVALID_RESPONSE", "buffer_base64 inválido.") from exc
            if b"\0" in raw or len(raw) >= self._buffer_size:
                raise SitefError("INVALID_RESPONSE", "Bytes coletados inválidos ou muito longos.")
            if raw != text_bytes:
                raise SitefError("RESPONSE_BYTES_MISMATCH", "Texto e Base64 da resposta devem representar os mesmos bytes na codificação nativa.")
            try:
                response = replace(response, buffer=raw.decode(self._encoding, errors="strict"))
            except UnicodeError as exc:
                raise SitefError("ENCODING_ERROR", "Resposta Base64 não representa texto na codificação nativa.") from exc
        return response

    def _policy(self, value: TransactionInput, explicit: bool | None, native_start: NativeStart | None) -> bool:
        if explicit is not None and type(explicit) is not bool:
            raise SitefError("INVALID_ARGUMENT", "requires_finalization deve ser boolean.")
        policy = ("financial" if native_start.method == "startBanking" else "nonfinancial") if native_start else document("functions")["entries"].get(str(value.function_id), {}).get("finalizationPolicy", "explicit")
        if policy == "explicit":
            if explicit is None:
                raise SitefError("FINALIZATION_POLICY_REQUIRED", "Função exige política explícita de finalização.")
            return explicit
        if explicit is not None and explicit != (policy == "financial"):
            raise SitefError("FINALIZATION_POLICY_MISMATCH", "Política explícita diverge da função documentada.")
        return policy == "financial"

    @property
    def recovery(self) -> dict[str, Any] | None:
        result = copy.deepcopy(self._record)
        if result and len(result["payments"]) == 1:
            for key in ("decision", "status", "finalizationParameters"):
                if key in result["payments"][0]:
                    result[key] = result["payments"][0][key]
        return result

    def _pending(self) -> list[dict[str, Any]]:
        return [p for p in (self._record or {}).get("payments", []) if p["state"] not in {"ended", "finalized-local"}]

    def _derive_state(self) -> None:
        pending = self._pending()
        if any(p["state"] in {"started", "uncertain", "finalizing"} for p in pending):
            self._state = "uncertain"
        else:
            self._state = "awaiting-finalization" if pending else "idle"
        if self._record is not None:
            self._record["state"] = ("uncertain" if self._state == "uncertain" else "awaiting-finalization" if pending
                                      else "finalized-local" if any(p["state"] == "finalized-local" for p in self._record["payments"]) else "ended")

    async def _persist(self) -> None:
        assert self._record is not None
        self._record["updatedAt"] = utc_now()
        await self._journal.write(copy.deepcopy(self._record))

    async def _sync_fields(self, payment: dict[str, Any], processor: FieldProcessor) -> None:
        if processor.payment_index is not None:
            if not re.fullmatch(r"[0-9]+", processor.payment_index):
                raise SitefError("INVALID_PAYMENT_INDEX", "Índice 161 inválido.")
            payment["paymentIndex"] = processor.payment_index
        payment["receipts"] = processor.receipt_metadata()
        payment.update(signatureRequired=processor.signature_required, signatureCaptured=processor.signature_captured)
        await self._persist()

    async def _cycle(self, method: str, params: Mapping[str, Any], cancellation: asyncio.Event,
                     processor: FieldProcessor, payment: dict[str, Any] | None) -> tuple[int, list[DataField], list[BaseException]]:
        status = _status(await self._transport.call(method, params))
        response, steps = UiResponse(), 0
        output: list[DataField] = []
        failures: list[BaseException] = []
        observers: set[asyncio.Task[Any]] = set()
        try:
            while status == 10000:
                steps += 1
                if steps > self._max_steps:
                    raise SitefError("LOOP_LIMIT", "Limite de passos excedido; reconciliar estado.", uncertain=True)
                cancelled = cancellation.is_set() or bool(failures)
                args = {"buffer": "" if cancelled else response.buffer,
                        "continueFlag": -1 if cancelled else response.continue_flag, "bufferSize": self._buffer_size}
                if not cancelled and response.buffer_base64 is not None:
                    args["bufferBase64"] = response.buffer_base64
                raw = await self._transport.call("continue", args)
                status = _status(raw)
                if status != 10000:
                    break
                request = NativeExchange.from_native(raw, command_info(raw.get("command", -999999))["kind"])
                response = UiResponse(request.buffer, buffer_base64=request.buffer_base64) if request.command == 0 else UiResponse()
                if cancelled:
                    continue
                try:
                    if request.command == 23:
                        if self._observer is not None:
                            task = asyncio.create_task(self._observer(request))
                            observers.add(task)
                            def observed(done: asyncio.Task[Any]) -> None:
                                observers.discard(done)
                                if not done.cancelled() and done.exception() is not None:
                                    failures.append(done.exception())
                                    cancellation.set()
                            task.add_done_callback(observed)
                        continue
                    request = await self._until_cancel(processor.process(request), cancellation, request)
                    if request.command == 0:
                        output.append(DataField(request.field_type, request.buffer, request.buffer_base64,
                                                request.decoding_error, request.typed_value,
                                                str(request.field_spec.get("dataType", "text"))))
                        if payment is not None:
                            await self._sync_fields(payment, processor)
                    if self._observer is not None:
                        await self._until_cancel(self._observer(request), cancellation, None)
                    if request.command != 0 and not cancellation.is_set():
                        self._collect_command = request.command
                        has_system_hook = callable(self._hooks.resolve_application_data) or callable(self._hooks.system_data)
                        operation = (processor.collect_system(request) if request.command == 29 and has_system_hook else
                                     self._interaction(request, InteractionContext(cancellation, processor.snapshot())))
                        response = self._validate_response(await self._until_cancel(operation, cancellation, UiResponse(continue_flag=-1)))
                        processor.validate_response(request, response)
                        processor.observe_response(request, response)
                        self._collect_command = None
                    if response.continue_flag == -1:
                        cancellation.set()
                except Exception as exc:
                    failures.append(exc)
                    processor.confirmation_blocked = True
                    cancellation.set()
                    self._collect_command = None
            try:
                await processor.release_resources()
            except Exception as exc:
                failures.append(exc)
                processor.confirmation_blocked = True
            if status == 0:
                try:
                    async def assessed() -> bool:
                        await processor.complete()
                        return True
                    if not await self._until_cancel(assessed(), cancellation, False):
                        raise SitefError("ASSESSMENT_CANCELLED", "Resultado bancário aprovado exige desfazimento ou reconciliação.")
                except Exception as exc:
                    failures.append(exc)
                    processor.confirmation_blocked = True
            return status, output, failures
        finally:
            for task in observers:
                task.cancel()
                task.add_done_callback(self._consume)
            self._collect_command = None
            if processor.resources_locked:
                try:
                    await asyncio.shield(processor.release_resources())
                except BaseException as cleanup_error:
                    failures.append(cleanup_error)
                    processor.confirmation_blocked = True

    async def transact(self, value: TransactionInput, *, requires_finalization: bool | None = None,
                       append_payment: bool = False, native_start: NativeStart | None = None,
                       cancel_event: asyncio.Event | None = None) -> TransactionResult:
        if self._operation or self._state not in ({"idle", "awaiting-finalization"} if append_payment else {"idle"}):
            raise SitefError("SESSION_BUSY", f"Sessão em {self._state}.")
        if not isinstance(value, TransactionInput) or type(value.function_id) is not int or not -(2**31) <= value.function_id < 2**31:
            raise SitefError("INVALID_ARGUMENT", "function_id deve ser int32.")
        if type(append_payment) is not bool:
            raise SitefError("INVALID_ARGUMENT", "append_payment deve ser boolean.")
        if native_start is not None and (not isinstance(native_start, NativeStart) or native_start.method not in _STARTS or not isinstance(native_start.params, Mapping)):
            raise SitefError("INVALID_ARGUMENT", "Rotina inicial inválida.")
        finalization = self._policy(value, requires_finalization, native_start)
        self._validate_input(value, allow_empty_fiscal=not finalization)
        cancellation = cancel_event if cancel_event is not None else asyncio.Event()
        if not isinstance(cancellation, asyncio.Event):
            raise SitefError("INVALID_ARGUMENT", "cancel_event deve ser asyncio.Event.")
        if cancellation.is_set():
            raise SitefError("ABORTED", "Cancelado antes do início.")
        if not finalization:
            method, args = (native_start.method, dict(native_start.params)) if native_start else ("start", value.native())
            return await self._ephemeral(value.function_id, method, args, cancellation)
        pending = self._pending()
        if pending and not append_payment:
            raise SitefError("APPEND_PAYMENT_REQUIRED", "Declare append_payment para acrescentar pagamento pendente.")
        if any(p["fiscalDate"] == value.fiscal_date and p["fiscalDocument"] != value.fiscal_document for p in pending):
            raise SitefError("PENDING_FISCAL_DOCUMENT", "Resolva o cupom pendente antes de trocar o cupom na mesma data.")
        previous, previous_state = copy.deepcopy(self._record), self._state
        payment = {"id": str(uuid4()), "functionId": value.function_id,
                   "entryPoint": native_start.method if native_start else "start",
                   "fiscalDocument": value.fiscal_document, "fiscalDate": value.fiscal_date,
                   "fiscalTime": value.fiscal_time, "state": "started", "confirmationAllowed": True, "assessmentIssues": []}
        definitions = ({"entries": {str(value.function_id): {"name": "startBanking", "domain": "corban",
                         "receiptPolicy": "immediate", "finalizationPolicy": "financial", "requiredHook": "validateTransaction"}}}
                       if native_start is not None and native_start.method == "startBanking" else None)
        processor = FieldProcessor(value.function_id, self._hooks, payment_id=payment["id"],
                                   functions_document=definitions, **self._field_options)
        if not pending:
            self._record = {"version": 2, "id": payment["id"], "storeId": self._configuration.store_id,
                            "terminalId": self._configuration.terminal_id, "functionId": value.function_id,
                            "entryPoint": payment["entryPoint"], "fiscalDocument": value.fiscal_document,
                            "fiscalDate": value.fiscal_date, "fiscalTime": value.fiscal_time,
                            "state": "started", "updatedAt": utc_now(), "payments": []}
        assert self._record is not None
        self._record["payments"].append(payment)
        self._processors[payment["id"]] = processor
        self._operation, self._cancel_event = True, cancellation
        started = False
        try:
            self._record["state"] = "started"
            await self._persist()
            self._state = "running"
            method, args = (native_start.method, dict(native_start.params)) if native_start else ("start", value.native())
            if method == "startBanking":
                args.update(fiscalDocument=value.fiscal_document, fiscalDate=value.fiscal_date,
                            fiscalTime=value.fiscal_time, operator=value.operator)
            started = True
            status, fields, failures = await self._cycle(method, args, cancellation, processor, payment)
            needs_finish = status == 0 and finalization
            payment.update(state="awaiting-finalization" if needs_finish else "ended", status=status,
                           confirmationAllowed=not processor.confirmation_blocked,
                           assessmentIssues=[exc.code if isinstance(exc, SitefError) else "APPLICATION_HOOK_FAILED" for exc in failures])
            payment["receipts"] = processor.receipt_metadata()
            payment.update(signatureRequired=processor.signature_required, signatureCaptured=processor.signature_captured)
            self._derive_state()
            await self._persist()
            result = TransactionResult(status, self._record["id"], needs_finish, tuple(fields),
                                       processor.snapshot(), payment["id"], tuple(item["receipt"] for item in processor.receipts))
            if failures:
                error = SitefError("INTERACTION_FAILED", f"Ação de campo falhou; ciclo terminou com retorno {status}.", native_status=status)
                error.result = result
                if not needs_finish:
                    self._processors.pop(payment["id"], None)
                raise error from failures[0]
            if not needs_finish:
                self._processors.pop(payment["id"], None)
            return result
        except BaseException as exc:
            if not started:
                self._record, self._state = previous, previous_state
                self._processors.pop(payment["id"], None)
            elif not (isinstance(exc, SitefError) and exc.code == "INTERACTION_FAILED"):
                payment["state"] = "uncertain"
                self._derive_state()
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._persist())
                if not isinstance(exc, asyncio.CancelledError):
                    raise SitefError("TRANSACTION_UNCERTAIN", "Resultado indeterminado; não repetir operação.", uncertain=True) from exc
            raise
        finally:
            self._cancel_event, self._operation = None, False

    async def append_payment(self, value: TransactionInput, **options: Any) -> TransactionResult:
        return await self.transact(value, append_payment=True, **options)

    async def _ephemeral(self, function_id: int, method: str, args: Mapping[str, Any],
                         cancellation: asyncio.Event) -> TransactionResult:
        previous_state = self._state
        operation_id = str(uuid4())
        context = "secure-card-read" if method in _STARTS - {"startBanking"} else None
        definitions = ({"entries": {str(function_id): {**document("functions")["entries"]["430"], "name": method}}}
                       if context else None)
        processor = FieldProcessor(function_id, self._hooks, payment_id=operation_id, context=context,
                                   functions_document=definitions, **self._field_options)
        self._operation, self._cancel_event, self._state = True, cancellation, "running"
        try:
            status, fields, failures = await self._cycle(method, args, cancellation, processor, None)
            result = TransactionResult(status, operation_id, False, tuple(fields), processor.snapshot(), None,
                                       tuple(item["receipt"] for item in processor.receipts))
            if failures:
                error = SitefError("INTERACTION_FAILED", "Ação administrativa incompleta.", native_status=status)
                error.result = result
                raise error from failures[0]
            return result
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError) or getattr(exc, "uncertain", False):
                previous_state = "uncertain"
            raise
        finally:
            self._operation, self._cancel_event, self._state = False, None, previous_state

    async def run_function(self, function_id: int, *, amount: str = "", fiscal_document: str = "",
                           fiscal_date: str = "", fiscal_time: str = "", operator: str = "",
                           additional_parameters: str = "", requires_finalization: bool | None = None,
                           cancel_event: asyncio.Event | None = None) -> TransactionResult:
        if self._operation or self._state not in {"idle", "awaiting-finalization"}:
            raise SitefError("SESSION_BUSY", "Operação indisponível no estado atual.")
        value = TransactionInput(function_id, amount, fiscal_document, fiscal_date, fiscal_time, operator, additional_parameters)
        if type(function_id) is not int or not -(2**31) <= function_id < 2**31:
            raise SitefError("INVALID_ARGUMENT", "function_id deve ser int32.")
        financial = self._policy(value, requires_finalization, None)
        if financial:
            return await self.transact(value, requires_finalization=True, cancel_event=cancel_event)
        for key, entry in value.native().items():
            if key != "functionId":
                self._bytes(entry, key)
        if amount and not re.fullmatch(r"[0-9]+,[0-9]{2}", amount):
            raise SitefError("INVALID_AMOUNT", "Valor administrativo precisa ser decimal textual ou vazio.")
        if any((fiscal_document, fiscal_date, fiscal_time)):
            self._validate_input(value)
        if len(self._bytes(operator, "operator")) > 20:
            raise SitefError("INVALID_ARGUMENT", "Operador excede limite em bytes.")
        cancellation = cancel_event or asyncio.Event()
        if not isinstance(cancellation, asyncio.Event) or cancellation.is_set():
            raise SitefError("ABORTED", "Operação cancelada antes do início.")
        return await self._ephemeral(function_id, "start", value.native(), cancellation)

    @staticmethod
    def _extra(value: str) -> str:
        _text(value, "additional_parameters")
        if re.search(r"(?:NumeroPagamentoCupom|RedeConfirmacao|NumeroPagamentoNFPAG|NFPAG)\s*=", value, re.I):
            raise SitefError("TYPED_FINALIZATION_REQUIRED", "Use a API tipada para selecionar pagamentos, redes ou NFPAG.")
        return value

    async def _before_confirm(self, payment: dict[str, Any]) -> None:
        if payment.get("legacyReceiptStateUnknown"):
            if not callable(self._hooks.attest_legacy_obligations) or await invoke(
                    self._hooks.attest_legacy_obligations, copy.deepcopy(payment),
                    {"storeId": self._configuration.store_id, "terminalId": self._configuration.terminal_id}) is not True:
                raise SitefError("LEGACY_OBLIGATIONS_ATTESTATION_REQUIRED", "Confirmação de V1 exige atestar recibos, assinatura e obrigações comerciais.")
            payment.update(legacyReceiptStateUnknown=False, confirmationAllowed=True)
            await self._persist()
        if payment.get("confirmationAllowed") is False:
            raise SitefError("CONFIRMATION_BLOCKED", "Pagamento contém ação obrigatória incompleta; somente desfazimento é permitido.")
        processor = self._processors.get(payment["id"])
        if processor is not None:
            async def changed() -> None:
                payment["receipts"] = processor.receipt_metadata()
                payment.update(signatureRequired=processor.signature_required, signatureCaptured=processor.signature_captured)
                await self._persist()
            await processor.before_confirm(changed)
            return
        for meta in payment.get("receipts", []):
            if meta["printed"]:
                continue
            if not callable(self._hooks.load_receipt) or not callable(self._hooks.print_receipt):
                raise SitefError("RECEIPT_RECOVERY_REQUIRED", "Confirmação após reinício exige load_receipt e print_receipt.")
            loaded = await invoke(self._hooks.load_receipt, copy.deepcopy(meta), copy.deepcopy(payment))
            if isinstance(loaded, Receipt):
                body, encoded = loaded.body, loaded.body_base64
            elif isinstance(loaded, Mapping):
                body, encoded = loaded.get("body"), loaded.get("bodyBase64", loaded.get("body_base64"))
            else:
                raise SitefError("RECEIPT_RECOVERY_INVALID", "Recibo recuperado inválido.")
            if not isinstance(body, str) or (encoded is not None and not isinstance(encoded, str)):
                raise SitefError("RECEIPT_RECOVERY_INVALID", "Corpo do recibo inválido.")
            try:
                raw = base64.b64decode(encoded, validate=True) if encoded is not None else body.encode("utf-8")
            except (ValueError, TypeError) as exc:
                raise SitefError("RECEIPT_RECOVERY_INVALID", "Bytes do recibo inválidos.") from exc
            if hashlib.sha256(raw).hexdigest() != meta["hash"]:
                raise SitefError("RECEIPT_HASH_MISMATCH", "Recibo recuperado diverge do hash persistido.")
            receipt = Receipt(meta["id"], meta["fieldType"], meta["type"], meta["timing"], body, encoded, meta["hash"])
            if await invoke(self._hooks.print_receipt, receipt, copy.deepcopy(payment)) is not True:
                raise SitefError("RECEIPT_PRINT_FAILED", "Impressão de recibo não confirmada.")
            meta["printed"] = True
            await self._persist()
        if payment.get("signatureRequired") and not payment.get("signatureCaptured"):
            if not callable(self._hooks.capture_signature) or await invoke(self._hooks.capture_signature, {"fieldType": 5074, "medium": "paper"}, copy.deepcopy(payment)) is not True:
                raise SitefError("SIGNATURE_REQUIRED", "Confirmação exige assinatura em papel após impressão.")
            payment["signatureCaptured"] = True
            await self._persist()

    async def _finish_selected(self, confirm: bool, selected: list[dict[str, Any]], extra: str,
                               selector: str = "", *, recovering: bool = False,
                               payload_parameters: str | None = None) -> dict[str, Any]:
        if type(confirm) is not bool or not selected:
            raise SitefError("INVALID_ARGUMENT", "Decisão ou seleção inválida.")
        if self._operation:
            raise SitefError("SESSION_BUSY", "Operação em andamento.")
        self._operation = True
        partitions: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for payment in selected:
            scope = payment.get("finalizationParameters", selector + extra) if recovering else selector + extra
            partitions.setdefault((payment["fiscalDocument"], payment["fiscalDate"], scope), []).append(payment)
        invoked = False
        planned = False
        previous = None
        previous_state = self._state
        active: list[dict[str, Any]] = []
        try:
            if confirm:
                for payment in selected:
                    await self._before_confirm(payment)
            if recovering and any("decision" in p and p["decision"] != int(confirm) for p in selected):
                raise SitefError("RECOVERY_DECISION_MISMATCH", "A decisão persistida não pode ser alterada.")
            previous = copy.deepcopy(self._record)
            native_parameters = {}
            if payload_parameters is not None:
                metadata = await self._store_finalization_payload(payload_parameters, selected[0])
                for payment in selected:
                    payment["finalizationPayload"] = dict(metadata)
            # Verify every external payload before the first FFI, including
            # later dates in a multi-partition recovery plan.
            for key, group in partitions.items():
                payloads = [p.get("finalizationPayload") for p in group]
                if any(p is not None for p in payloads):
                    if any(p != payloads[0] for p in payloads):
                        raise SitefError("FINALIZATION_PAYLOAD_SCOPE_MISMATCH", "A partição contém referências de payload diferentes.")
                    native_parameters[key] = (payload_parameters if payload_parameters is not None
                                              else await self._load_finalization_payload(payloads[0], group[0]))
                    if re.sub(r"\{NFPAG\s*=[^{}]*\}", "", native_parameters[key], flags=re.I) != key[2]:
                        raise SitefError("FINALIZATION_PAYLOAD_SCOPE_MISMATCH", "Payload externo diverge do escopo seguro persistido.")
                else:
                    native_parameters[key] = key[2]
            # Persist the entire multi-date decision plan before the first FFI.
            for (_document, _date, parameters), group in partitions.items():
                for payment in group:
                    payment.update(state="finalizing", decision=int(confirm), finalizationParameters=parameters)
            self._record["state"] = "finalizing"
            await self._persist()
            planned = True
            for (_document, _date, parameters), active in partitions.items():
                first = active[0]
                invoked = True
                result = await self._transport.call("finish", {"confirm": int(confirm), "fiscalDocument": first["fiscalDocument"],
                    "fiscalDate": first["fiscalDate"], "fiscalTime": first["fiscalTime"],
                    "additionalParameters": native_parameters[(_document, _date, parameters)]})
                if result.get("returned") is not True:
                    raise SitefError("INVALID_NATIVE_RESULT", "Finalização sem retorno local válido.", uncertain=True)
                for payment in active:
                    payment["state"] = "finalized-local"
                self._derive_state()
                await self._persist()
                for payment in active:
                    self._processors.pop(payment["id"], None)
                invoked = False
            self._derive_state()
            return {"returned": True, "remoteConfirmation": "not-reported"}
        except BaseException as exc:
            if invoked:
                for payment in active:
                    payment["state"] = "uncertain"
                self._derive_state()
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._persist())
                if not isinstance(exc, asyncio.CancelledError):
                    raise SitefError("FINALIZATION_UNCERTAIN", "Finalização incerta; reconciliar sem replay automático.", uncertain=True) from exc
            elif not planned and previous is not None:
                self._record = previous
                self._state = previous_state
            raise
        finally:
            self._operation = False

    async def _store_finalization_payload(self, parameters: str, payment: Mapping[str, Any]) -> dict[str, str]:
        if not callable(self._hooks.store_finalization_payload):
            raise SitefError("FINALIZATION_PAYLOAD_STORAGE_REQUIRED", "NFPAG exige armazenamento externo durável do payload.")
        payload = {"id": payment["id"] + ":nfpag:" + uuid4().hex,
                   "parameters": parameters, "hash": hashlib.sha256(parameters.encode("utf-8")).hexdigest()}
        if await invoke(self._hooks.store_finalization_payload, copy.deepcopy(payload), copy.deepcopy(payment)) is not True:
            raise SitefError("FINALIZATION_PAYLOAD_STORAGE_FAILED", "Aplicação não confirmou a persistência do payload NFPAG.")
        return {key: payload[key] for key in ("id", "hash")}

    async def _load_finalization_payload(self, metadata: Mapping[str, str], payment: Mapping[str, Any]) -> str:
        if not callable(self._hooks.load_finalization_payload):
            raise SitefError("FINALIZATION_PAYLOAD_RECOVERY_REQUIRED", "Recuperação NFPAG exige load_finalization_payload.")
        parameters = await invoke(self._hooks.load_finalization_payload, copy.deepcopy(metadata), copy.deepcopy(payment))
        if (not isinstance(parameters, str) or "\0" in parameters
                or hashlib.sha256(parameters.encode("utf-8")).hexdigest() != metadata["hash"]):
            raise SitefError("FINALIZATION_PAYLOAD_HASH_MISMATCH", "Payload recuperado diverge do hash persistido.")
        return parameters

    async def finalize(self, confirm: bool, additional_parameters: str = "") -> dict[str, Any]:
        if self._state != "awaiting-finalization":
            raise SitefError("INVALID_STATE", "Não há grupo aguardando decisão.")
        return await self._finish_selected(confirm, self._pending(), self._extra(additional_parameters))

    def _payment(self, payment_id: str) -> dict[str, Any]:
        matches = [p for p in self._pending() if p["id"] == payment_id]
        if len(matches) != 1:
            raise SitefError("INVALID_PAYMENT", "Pagamento pendente não encontrado.")
        return matches[0]

    async def finalize_payment(self, confirm: bool, payment_id: str, additional_parameters: str = "") -> dict[str, Any]:
        if self._state != "awaiting-finalization":
            raise SitefError("INVALID_STATE", "Grupo não aguarda finalização.")
        payment = self._payment(payment_id)
        index = payment.get("paymentIndex")
        if not index:
            raise SitefError("PAYMENT_INDEX_REQUIRED", "Finalização individual exige campo 161.")
        partition = [p for p in self._pending() if p["fiscalDocument"] == payment["fiscalDocument"] and p["fiscalDate"] == payment["fiscalDate"] and p.get("paymentIndex") == index]
        if len(partition) != 1:
            raise SitefError("AMBIGUOUS_PAYMENT_INDEX", "Índice 161 não identifica um pagamento único.")
        return await self._finish_selected(confirm, [payment], self._extra(additional_parameters), "{NumeroPagamentoCupom=" + index + "}")

    async def finalize_network(self, confirm: bool, network: str, additional_parameters: str = "") -> dict[str, Any]:
        if self._state != "awaiting-finalization" or not isinstance(network, str) or not re.fullmatch(r"[0-9]+", network):
            raise SitefError("INVALID_STATE", "Rede ou estado inválido.")
        selected = []
        for payment in self._pending():
            processor = self._processors.get(payment["id"])
            if not payment.get("network"):
                if processor is not None:
                    payment["network"] = await processor.confirmation_network()
                elif callable(self._hooks.resolve_confirmation_network):
                    resolved = await invoke(self._hooks.resolve_confirmation_network, copy.deepcopy(payment))
                    if not isinstance(resolved, str) or not re.fullmatch(r"[0-9]+", resolved):
                        raise SitefError("CONFIRMATION_NETWORK_REQUIRED", "Resolver de rede inválido.")
                    payment["network"] = resolved
                else:
                    raise SitefError("CONFIRMATION_NETWORK_REQUIRED", "Pagamentos exigem rede explicitamente resolvida.")
            if payment["network"] == network:
                selected.append(payment)
        return await self._finish_selected(confirm, selected, self._extra(additional_parameters), "{RedeConfirmacao=" + network + "}")

    async def finalize_with_payments(self, confirm: bool, payment_id: str, methods: Any = None,
                                     total_amount: str | None = None, additional_parameters: str = "") -> dict[str, Any]:
        if self._state != "awaiting-finalization":
            raise SitefError("INVALID_STATE", "Grupo não aguarda finalização.")
        if not isinstance(total_amount, str):
            raise SitefError("PAYMENT_TOTAL_REQUIRED", "Informe total_amount textual explícito para conferir NFPAG.")
        payment = self._payment(payment_id)
        processor = self._processors.get(payment_id)
        if processor is None:
            raise SitefError("PAYMENT_POLICY_REQUIRED", "Recupere os requisitos de NFPAG antes de finalizar.")
        same_index = [p for p in self._pending() if p["fiscalDocument"] == payment["fiscalDocument"]
                      and p["fiscalDate"] == payment["fiscalDate"] and p.get("paymentIndex") == processor.payment_index]
        if not processor.payment_index or len(same_index) != 1:
            raise SitefError("AMBIGUOUS_PAYMENT_INDEX", "NFPAG exige índice161 único na partição cupom/data.")
        if methods is None:
            if not callable(self._hooks.payment_data):
                raise SitefError("PAYMENT_DATA_REQUIRED", "Forneça PaymentMethod ou implemente payment_data.")
            methods = await invoke(self._hooks.payment_data,
                                   {"paymentId": payment_id, "totalAmount": total_amount,
                                    "maximum": processor.nfpag_max, "requirements": copy.deepcopy(processor.nfpag_methods)},
                                   processor.snapshot())
        parameters = build_payment_parameters(processor, methods, total_amount)
        selected = [p for p in self._pending() if p["fiscalDocument"] == payment["fiscalDocument"] and p["fiscalDate"] == payment["fiscalDate"]]
        safe_scope = "{NumeroPagamentoNFPAG=" + processor.payment_index + "}"
        extra = self._extra(additional_parameters)
        return await self._finish_selected(confirm, selected, extra, safe_scope,
                                           payload_parameters=parameters + extra)

    async def finalize_recovery(self, confirm: bool, expected_transaction_id: str,
                                additional_parameters: str | None = None) -> dict[str, Any]:
        if self._state != "uncertain" or self._record is None or self._record["id"] != expected_transaction_id:
            raise SitefError("INVALID_RECOVERY", "Recuperação exige grupo incerto e identidade exata.")
        if type(confirm) is not bool:
            raise SitefError("INVALID_ARGUMENT", "A decisão de recuperação precisa ser booleana.")
        if additional_parameters is not None:
            _text(additional_parameters, "additional_parameters")
        pending = self._pending()
        plans = [p for p in pending if "decision" in p]
        if plans:
            pending = plans
        elif additional_parameters is not None:
            self._extra(additional_parameters)
        for payment in pending:
            if "decision" in payment and payment["decision"] != int(confirm):
                raise SitefError("RECOVERY_DECISION_MISMATCH", "A decisão diverge da persistida.")
            if additional_parameters is not None and "finalizationParameters" in payment and payment["finalizationParameters"] != additional_parameters:
                raise SitefError("RECOVERY_SCOPE_MISMATCH", "O escopo da finalização persistida não pode ser alterado.")
        return await self._finish_selected(confirm, pending, additional_parameters or "", recovering=True)

    async def query_pending_transactions(self, *, fiscal_document: str | None = None,
                                         fiscal_date: str | None = None,
                                         cancel_event: asyncio.Event | None = None) -> tuple[dict[str, Any], ...]:
        if self._operation or self._state not in {"idle", "awaiting-finalization", "uncertain"}:
            raise SitefError("SESSION_BUSY", "Consulta indisponível durante operação.")
        specific = fiscal_document is not None or fiscal_date is not None
        if specific and (not isinstance(fiscal_document, str) or not isinstance(fiscal_date, str)):
            raise SitefError("INVALID_ARGUMENT", "Consulta específica exige cupom e data.")
        if specific:
            if (not fiscal_document or len(self._bytes(fiscal_document, "fiscal_document")) > 20
                    or not re.fullmatch(r"[0-9]{8}", fiscal_date)):
                raise SitefError("INVALID_FISCAL_ID", "Consulta específica exige cupom e data fiscal válidos.")
            try:
                datetime.strptime(fiscal_date, "%Y%m%d")
            except ValueError as exc:
                raise SitefError("INVALID_FISCAL_ID", "Data da consulta inválida.") from exc
        cancellation = cancel_event or asyncio.Event()
        if not isinstance(cancellation, asyncio.Event) or cancellation.is_set():
            raise SitefError("ABORTED", "Consulta cancelada antes do início.")
        processor = FieldProcessor(131 if specific else 130, self._hooks, **self._field_options)
        operation_id = str(uuid4())
        query_result = None
        self._operation, self._cancel_event = True, cancellation
        try:
            args = {"functionId": 131 if specific else 130, "amount": "", "fiscalDocument": fiscal_document or "",
                    "fiscalDate": fiscal_date or "", "fiscalTime": "", "operator": "", "additionalParameters": ""}
            status, fields, failures = await self._cycle("start", args, cancellation, processor, None)
            query_result = TransactionResult(status, operation_id, False, tuple(fields), processor.snapshot())
            if failures:
                error = SitefError("INTERACTION_FAILED", "Consulta teve ação obrigatória incompleta.", native_status=status)
                error.result = query_result
                raise error from failures[0]
            if status != 0:
                error = SitefError("PENDING_QUERY_FAILED", "Consulta de pendências não concluída.", native_status=status)
                error.result = query_result
                raise error
            result: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            names = {160: "fiscalDocument", 161: "paymentIndex", 163: "fiscalDate", 164: "fiscalTime", 211: "functionId", 1319: "amount"}
            for entry in fields:
                if entry.field_type == 160:
                    if current is not None:
                        result.append(current)
                    current = {}
                if entry.field_type in names:
                    if current is None:
                        raise SitefError("INVALID_PENDING_QUERY", "Campos de pendência chegaram fora de ordem.")
                    if names[entry.field_type] in current:
                        raise SitefError("INVALID_PENDING_QUERY", "Campo repetido dentro de uma pendência; não é seguro sobrescrever o dado.")
                    current[names[entry.field_type]] = entry.buffer
            if current is not None:
                result.append(current)
            if any(set(row) != set(names.values()) for row in result):
                raise SitefError("INVALID_PENDING_QUERY", "Registro de pendência incompleto.")
            counts = processor.raw_values.get(210, [])
            if counts and (not re.fullmatch(r"[0-9]+", counts[-1]) or int(counts[-1]) != len(result)):
                raise SitefError("INVALID_PENDING_QUERY", "Quantidade 210 diverge dos registros recebidos.")
            return tuple(result)
        except BaseException as exc:
            if isinstance(exc, SitefError) and exc.result is None:
                exc.result = query_result
            if isinstance(exc, asyncio.CancelledError) or getattr(exc, "uncertain", False):
                self._state = "uncertain"
            raise
        finally:
            self._operation, self._cancel_event = False, None

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
        await self._transport.close()
        await self._journal.release()
        self._processors.clear()
        self._state = "closed"
