"""Python names map explicitly to the shared bridge's camelCase contract."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from .errors import SitefError


@dataclass(frozen=True)
class Configuration:
    server: str
    store_id: str
    terminal_id: str
    additional_parameters: str = field(default="", repr=False)
    reserved: int = 0

    def native(self) -> dict[str, Any]:
        return {"server": self.server, "storeId": self.store_id,
                "terminalId": self.terminal_id, "reserved": self.reserved,
                "additionalParameters": self.additional_parameters}


@dataclass(frozen=True)
class TransactionInput:
    function_id: int
    amount: str = field(repr=False)
    fiscal_document: str
    fiscal_date: str
    fiscal_time: str
    operator: str = field(repr=False)
    additional_parameters: str = field(default="", repr=False)

    def native(self) -> dict[str, Any]:
        return {"functionId": self.function_id, "amount": self.amount,
                "fiscalDocument": self.fiscal_document, "fiscalDate": self.fiscal_date,
                "fiscalTime": self.fiscal_time, "operator": self.operator,
                "additionalParameters": self.additional_parameters}


@dataclass(frozen=True)
class NativeStart:
    method: str
    params: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class NativeExchange:
    status: int
    command: int
    field_type: int
    min_length: int
    max_length: int
    buffer: str = field(repr=False)
    kind: str = "unknown"
    buffer_base64: str | None = field(default=None, repr=False)
    decoding_error: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    field_spec: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    typed_value: Any = field(default=None, repr=False, compare=False)
    field_context: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_native(cls, value: Mapping[str, Any], kind: str) -> NativeExchange:
        integers = ["status", "command", "fieldType", "minLength", "maxLength"]
        if any(type(value.get(key)) is not int for key in integers) or not isinstance(value.get("buffer"), str):
            raise SitefError("INVALID_NATIVE_RESULT", "Continuação nativa malformada.", uncertain=True)
        if value.get("bufferBase64") is not None and not isinstance(value["bufferBase64"], str):
            raise SitefError("INVALID_NATIVE_RESULT", "Bytes nativos malformados.", uncertain=True)
        if "decodingError" in value and type(value["decodingError"]) is not bool:
            raise SitefError("INVALID_NATIVE_RESULT", "Indicador de codificação inválido.", uncertain=True)
        return cls(value["status"], value["command"], value["fieldType"], value["minLength"],
                   value["maxLength"], value["buffer"], kind, value.get("bufferBase64"),
                   value.get("decodingError", False), MappingProxyType(dict(value)))


@dataclass(frozen=True)
class UiResponse:
    buffer: str = field(default="", repr=False)
    continue_flag: int = 0
    buffer_base64: str | None = field(default=None, repr=False)
    confirmed: bool | None = None


@dataclass(frozen=True)
class InteractionContext:
    cancel_event: asyncio.Event = field(repr=False)
    fields: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


@dataclass(frozen=True)
class DataField:
    field_type: int
    buffer: str = field(repr=False)
    buffer_base64: str | None = field(default=None, repr=False)
    decoding_error: bool = False
    value: Any = field(default=None, repr=False)
    data_type: str = "text"


@dataclass(frozen=True)
class Receipt:
    id: str
    field_type: int
    receipt_type: str | None
    timing: str
    body: str = field(repr=False)
    body_base64: str | None = field(default=None, repr=False)
    hash: str = ""


@dataclass(frozen=True)
class TransactionResult:
    status: int
    transaction_id: str
    requires_finalization: bool
    fields: tuple[DataField, ...] = field(default=(), repr=False)
    context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    payment_id: str | None = None
    receipts: tuple[Receipt, ...] = field(default=(), repr=False)


InteractionHandler = Callable[[NativeExchange, InteractionContext], Awaitable[UiResponse]]
ExchangeObserver = Callable[[NativeExchange], Awaitable[None]]
