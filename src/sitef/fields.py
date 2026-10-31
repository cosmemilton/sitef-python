"""Typed field routing and mandatory application handoffs; no binary floats."""
from __future__ import annotations

import copy
import base64
import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
import inspect
import json
import re
from typing import Any, Callable, Mapping

from .errors import SitefError
from .models import NativeExchange, Receipt, UiResponse


def document(name: str) -> dict[str, Any]:
    try:
        value = json.loads(files("sitef").joinpath("data/" + name + ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SitefError("FIELD_CATALOG_UNAVAILABLE", "Catálogo semântico compartilhado indisponível.") from exc
    if value.get("schemaVersion") != 1 or not isinstance(value.get("entries"), dict):
        raise SitefError("FIELD_CATALOG_INVALID", "Catálogo semântico compartilhado inválido.")
    return value


def decimal_text(value: str, *, scale: int | None = None, minor_units: bool = False) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]+(?:[,.][0-9]+)?", value):
        raise SitefError("INVALID_DECIMAL", "Valor decimal deve ser texto, sem separador de milhares.")
    if scale is not None and (type(scale) is not int or not 0 <= scale <= 32767):
        raise SitefError("INVALID_PRECISION", "Precisão decimal incompatível com campo nativo.")
    normalized = value.replace(",", ".")
    if minor_units:
        if "." in normalized or scale is None:
            raise SitefError("INVALID_DECIMAL", "Centavos exigem texto inteiro e escala explícita.")
        negative = normalized.startswith("-")
        digits = normalized.lstrip("+-").rjust(scale + 1, "0")
        normalized = ("-" if negative else "") + (digits[:-scale] + "." + digits[-scale:] if scale else digits)
    elif scale is not None and "." in normalized and len(normalized.split(".")[1]) > scale:
        raise SitefError("INVALID_PRECISION", "Valor possui mais casas decimais que as informadas.")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise SitefError("INVALID_DECIMAL", "Valor decimal inválido.") from exc


def to_minor_units(value: str, scale: int = 2) -> str:
    decimal_text(value, scale=scale)
    sign = "-" if value.startswith("-") else ""
    whole, _, fraction = value.lstrip("+-").replace(",", ".").partition(".")
    return sign + ((whole + fraction.ljust(scale, "0")).lstrip("0") or "0")


async def invoke(callback: Callable[..., Any], *args: Any) -> Any:
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


@dataclass
class FieldHooks:
    system_data: Callable[..., Any] | None = None
    resolve_application_data: Callable[..., Any] | None = None
    store_receipt: Callable[..., Any] | None = None
    load_receipt: Callable[..., Any] | None = None
    print_receipt: Callable[..., Any] | None = None
    lock_resources: Callable[..., Any] | None = None
    unlock_resources: Callable[..., Any] | None = None
    capture_signature: Callable[..., Any] | None = None
    on_diagnostic: Callable[..., Any] | None = None
    accept_fallback: Callable[..., Any] | None = None
    validate_transaction: Callable[..., Any] | None = None
    resolve_field: Callable[..., Any] | None = None
    resolve_confirmation_network: Callable[..., Any] | None = None
    payment_data: Callable[..., Any] | None = None
    store_finalization_payload: Callable[..., Any] | None = None
    load_finalization_payload: Callable[..., Any] | None = None
    attest_legacy_obligations: Callable[..., Any] | None = None
    actions: Mapping[str, Callable[..., Any]] = field(default_factory=dict)

    def get(self, name: str) -> Callable[..., Any] | None:
        aliases = {"systemData": "system_data", "printReceipt": "print_receipt",
                   "resolveApplicationData": "resolve_application_data", "storeReceipt": "store_receipt",
                   "loadReceipt": "load_receipt",
                   "lockResources": "lock_resources", "unlockResources": "unlock_resources",
                   "captureSignature": "capture_signature", "onDiagnostic": "on_diagnostic",
                   "acceptFallback": "accept_fallback", "validateTransaction": "validate_transaction",
                   "resolveField": "resolve_field", "resolveConfirmationNetwork": "resolve_confirmation_network",
                   "paymentData": "payment_data", "storeFinalizationPayload": "store_finalization_payload",
                   "loadFinalizationPayload": "load_finalization_payload", "attestLegacyObligations": "attest_legacy_obligations"}
        return self.actions.get(name) or getattr(self, aliases.get(name, name), None)


class FieldProcessor:
    def __init__(self, function_id: int, hooks: FieldHooks | None = None, *,
                 fields_document: Mapping[str, Any] | None = None,
                 functions_document: Mapping[str, Any] | None = None,
                 receipt_storage: str = "persist", monetary_formats: Mapping[int, str] | None = None,
                 confirmation_network_field: int | None = None, payment_id: str = "standalone",
                 native_encoding: str = "windows-1252", context: str | None = None) -> None:
        self.hooks = hooks or FieldHooks()
        self.specs = dict(fields_document or document("fields"))
        self.function = dict((functions_document or document("functions"))["entries"].get(str(function_id), {}))
        self.function_id = function_id
        self.payment_id = payment_id
        if context not in {None, "transaction", "secure-card-read"}:
            raise SitefError("INVALID_OPTIONS", "Contexto semântico inválido.")
        self.context = context or ("secure-card-read" if self.function.get("domain") == "secure-card-read" else "transaction")
        self.encoding = {"windows-1252": "cp1252", "utf-8": "utf-8", "ascii": "ascii"}.get(native_encoding)
        if self.encoding is None:
            raise SitefError("INVALID_OPTIONS", "Codificação nativa não suportada.")
        if receipt_storage not in {"persist", "explicit-return"} or confirmation_network_field not in {None, 131, 158}:
            raise SitefError("INVALID_OPTIONS", "Política de recibos ou rede de confirmação inválida.")
        self.receipt_storage = receipt_storage
        self.monetary_formats = dict(monetary_formats or {})
        if any(value not in {"decimal-comma", "minor-units"} for value in self.monetary_formats.values()):
            raise SitefError("INVALID_OPTIONS", "Formato monetário precisa ser explícito.")
        self.confirmation_network_field = confirmation_network_field
        self.confirmation_blocked = False
        self._pbm_resolved = False
        self.signature_required = False
        self.signature_captured = False
        self._signature_request: NativeExchange | None = None
        self.values: dict[int, Any] = {}
        self.raw_values: dict[int, list[str]] = {}
        self.receipts: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.receipt_type: str | None = None
        self.format_hint: str | None = None
        self._collection_hint: str | None = None
        self.precision = {"general": 2, "unitPrice": None, "quantity": None}
        self._next_precision: int | None = None
        self._collection_precision = 2
        self.payment_index: str | None = None
        self.authorizer_network: str | None = None
        self.immediate_receipts = self.function.get("receiptPolicy") == "immediate"
        self.resources_locked = False
        self.nfpag_max: int | None = None
        self.nfpag_methods: dict[str, list[str]] = {}
        self._nfpag_current: str | None = None
        self._deferred_hooks: dict[int, str] = {}
        self.corban_documents: list[dict[str, Any]] = []
        self.fields: list[dict[str, Any]] = []
        self.product_constraints: list[dict[str, Any]] = []
        self._medicine_groups: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        return {"functionId": self.function_id, "context": self.context, "values": copy.deepcopy(self.values),
                "precision": dict(self.precision), "formatHint": self.format_hint,
                "receiptType": self.receipt_type, "paymentIndex": self.payment_index,
                "authorizerNetwork": self.authorizer_network,
                "diagnostics": copy.deepcopy(self.diagnostics),
                "corbanDocuments": copy.deepcopy(self.corban_documents),
                "fields": copy.deepcopy(self.fields), "productConstraints": copy.deepcopy(self.product_constraints),
                "balances": {str(k): self.raw_values[k][-1] for k in (137, 138) if k in self.raw_values}}

    async def _required(self, name: str, request: NativeExchange, *, success: bool = False) -> Any:
        hook = self.hooks.get(name)
        if not callable(hook):
            raise SitefError("REQUIRED_FIELD_HOOK", f"Implemente o hook obrigatório {name} para o campo {request.field_type}.")
        result = await invoke(hook, request, self.snapshot())
        if success and result is not True:
            raise SitefError("FIELD_ACTION_REJECTED", f"O hook {name} não confirmou a conclusão da ação.")
        return result

    def decode(self, spec: Mapping[str, Any], raw: str) -> Any:
        kind = spec.get("dataType", "text")
        format_value = spec.get("format", {})
        fmt = format_value if isinstance(format_value, Mapping) else {"kind": format_value}
        if raw == "":
            return ""
        if fmt.get("kind") in {"digits", "decimal-identifier"}:
            if (not re.fullmatch(r"[0-9]+", raw)
                    or (fmt.get("fixedLength") is not None and len(raw) != fmt["fixedLength"])
                    or (fmt.get("maxLength") is not None and len(raw) > fmt["maxLength"])
                    or (fmt.get("maxSignificantDigits") is not None and len(raw.lstrip("0") or "0") > fmt["maxSignificantDigits"])):
                raise SitefError("INVALID_FIELD_VALUE", "Identificador numérico fora do formato documentado.")
        maximum_bytes = fmt.get("extendedMaxBytes") if fmt.get("kind") == "authorization-code" else fmt.get("maxBytes")
        if isinstance(maximum_bytes, int) and len(raw.encode(self.encoding, errors="strict")) > maximum_bytes:
            raise SitefError("INVALID_FIELD_VALUE", "Campo excede limite documentado em bytes.")
        if kind == "integer":
            if not re.fullmatch(r"[0-9]+" if fmt.get("kind") == "nonnegative-integer" else r"[+-]?[0-9]+", raw):
                raise SitefError("INVALID_FIELD_VALUE", "Campo inteiro inválido.")
            return int(raw)
        if kind == "decimal":
            if fmt.get("kind") == "application-defined-decimal":
                return raw
            if fmt.get("kind") == "minor-units":
                return decimal_text(raw, scale=fmt.get("scale", 2), minor_units=True)
            if fmt.get("kind") == "decimal-comma" and "." in raw:
                raise SitefError("INVALID_FIELD_VALUE", "Campo decimal exige vírgula.")
            scope = {1121: "unitPrice", 1126: "quantity"}.get(fmt.get("scaleFromField"))
            return decimal_text(raw, scale=self.precision.get(scope) if scope else fmt.get("scale"))
        if kind in {"date", "time", "datetime"}:
            fmt = str(fmt.get("pattern", fmt.get("kind", ""))).upper()
            if fmt in {"DDMM", "MMYY", "MMYYYY", "DDMMYY", "DDMMAA"}:
                width = {"DDMM": 4, "MMYY": 4, "MMYYYY": 6, "DDMMYY": 6, "DDMMAA": 6}[fmt]
                if not re.fullmatch(r"[0-9]{" + str(width) + r"}", raw):
                    raise SitefError("INVALID_FIELD_VALUE", "Data parcial inválida.")
                month = int(raw[2:4] if fmt.startswith("DD") else raw[:2])
                result = {"month": month}
                if fmt.startswith("DD"):
                    result["day"] = int(raw[:2])
                if fmt == "MMYYYY":
                    result["year"] = int(raw[2:])
                elif fmt in {"MMYY", "DDMMYY", "DDMMAA"}:
                    result["yearWithinCentury"] = int(raw[-2:])
                # Leap day can exist when no full year is supplied. Never
                # invent a century using strptime's platform pivot year.
                try:
                    datetime(result.get("year", 2000), month, result.get("day", 1))
                except ValueError as exc:
                    raise SitefError("INVALID_FIELD_VALUE", "Data parcial fora do calendário.") from exc
                return result
            patterns = {"AAAAMMDD": "%Y%m%d", "YYYYMMDD": "%Y%m%d", "DDMMAAAA": "%d%m%Y",
                        "DDMMYYYY": "%d%m%Y", "DDMMAA": "%d%m%y", "DDMMYY": "%d%m%y",
                        "HHMMSS": "%H%M%S", "AAAAMMDDHHMMSS": "%Y%m%d%H%M%S",
                        "YYYYMMDDHHMMSS": "%Y%m%d%H%M%S"}
            if fmt in patterns:
                try:
                    parsed = datetime.strptime(raw, patterns[fmt])
                    if parsed.strftime(patterns[fmt]) != raw:
                        raise ValueError("Noncanonical date")
                    return parsed.date() if kind == "date" else parsed.time() if kind == "time" else parsed
                except ValueError as exc:
                    raise SitefError("INVALID_FIELD_VALUE", "Campo de data/hora inválido.") from exc
        if kind == "structured":
            structure = fmt.get("kind")
            if structure == "confirmation-data":
                parts = raw.split(";")
                if len(parts) > 3:
                    raise SitefError("INVALID_FIELD_VALUE", "Campo 1 possui mais partes que o formato documentado.")
                return dict(zip(("serviceZ", "sitefIndex", "sitefAddress"), parts))
            if structure == "payment-mode":
                if not re.fullmatch(r"[0-9]{4}", raw):
                    raise SitefError("INVALID_FIELD_VALUE", "Modalidade deve conter quatro dígitos.")
                return {"group": raw[:2], "variant": raw[2:]}
            if structure in {"cheque-command31", "barcode-origin"}:
                prefixes = fmt.get("prefixes", {})
                if raw[:1] not in prefixes:
                    raise SitefError("INVALID_FIELD_VALUE", "Origem de cheque/código de barras inválida.")
                if structure == "cheque-command31" and raw[0] == "0" and not re.fullmatch(r"[0-9]{29}", raw[1:]):
                    raise SitefError("INVALID_FIELD_VALUE", "Primeira linha manual exige 29 dígitos com DVs.")
                return {"origin": prefixes[raw[0]], "content": raw[1:]}
            if structure == "cheque-without-check-digits":
                parts, offset, result = fmt.get("parts", []), 0, {}
                if not re.fullmatch(r"[0-9]{" + str(sum(p["length"] for p in parts)) + r"}", raw):
                    raise SitefError("INVALID_FIELD_VALUE", "Cheque sem DVs exige 26 dígitos.")
                for part in parts:
                    result[part["name"]] = raw[offset:offset + part["length"]]
                    offset += part["length"]
                return result
            if structure == "nonfuel-product-list":
                if not raw.endswith("<#>"):
                    raise SitefError("INVALID_PRODUCT_DATA", "Lista de produtos exige terminador <#>.")
                rows, ids = [], set()
                for record in raw[:-3].split("<#>"):
                    pairs = record.split(",")
                    expected_keys = fmt["fields"]
                    if len(pairs) != len(expected_keys) or any("=" not in pair for pair in pairs):
                        raise SitefError("INVALID_PRODUCT_DATA", "Registro de produto incompleto ou com delimitadores ambíguos.")
                    parsed_pairs = [pair.split("=", 1) for pair in pairs]
                    if [key for key, _ in parsed_pairs] != expected_keys:
                        raise SitefError("INVALID_PRODUCT_DATA", "Ordem de campos do produto incompatível com o formato documentado.")
                    product = dict(parsed_pairs)
                    if (product["Id"] in ids or not product["Id"] or product["T"] not in fmt["codeTypes"]
                            or product["TQ"] not in fmt["quantityTypes"]):
                        raise SitefError("INVALID_PRODUCT_DATA", "Código duplicado ou tipo de produto/quantidade inválido.")
                    if any(len(product[key].encode(self.encoding, errors="strict")) > maximum
                           for key, maximum in fmt["maxBytes"].items()):
                        raise SitefError("INVALID_PRODUCT_DATA", "Campo de produto excede o máximo em bytes.")
                    product["unitPrice"] = decimal_text(product["VU"], scale=2, minor_units=True)
                    ids.add(product["Id"])
                    rows.append(product)
                return rows
            if structure == "pix-additional-fields":
                records = raw.split(fmt["recordSeparator"])
                if len(records) > fmt["maxRecords"]:
                    raise SitefError("INVALID_PIX_DATA", "Quantidade de informações adicionais Pix excedida.")
                pairs = []
                for record in records:
                    row = record.split(fmt["pairSeparator"])
                    if (len(row) != 2 or len(row[0].encode(self.encoding)) > fmt["maxNameBytes"]
                            or len(row[1].encode(self.encoding)) > fmt["maxValueBytes"]):
                        raise SitefError("INVALID_PIX_DATA", "Informação adicional Pix malformada ou maior que o limite.")
                    pairs.append({"name": row[0], "value": row[1]})
                return pairs
        return raw  # Unspecified grammar retains the lossless vendor representation.

    def field_spec(self, code: int) -> dict[str, Any]:
        context = self.context
        entry = self.specs.get("entries", {}).get(str(code))
        if entry and (not entry.get("contexts") or context in entry["contexts"]):
            return dict(entry)
        for candidate in self.specs.get("ranges", []):
            if candidate["from"] <= code <= candidate["to"] and context in candidate.get("contexts", ["transaction"]):
                return dict(candidate)
        # Product-specific ranges are routed, never reinterpreted as secure data.
        for candidate in self.specs.get("ranges", []):
            if candidate["from"] <= code <= candidate["to"] and "secure-card-read" not in candidate.get("contexts", []):
                return dict(candidate)
        return {"name": "unknown", "dataType": "text", "known": False}

    async def process(self, request: NativeExchange) -> NativeExchange:
        spec = self.field_spec(request.field_type)
        if request.command != 0:
            if request.command in {20, 21, 29, 30, 31, 34, 35, 41, 42}:
                self._collection_hint, self.format_hint = self.format_hint, None
                if request.command in {30, 34}:
                    self._collection_precision = self._next_precision if self._next_precision is not None else 2
                    self._next_precision = None
            enriched = replace(request, field_spec=spec, field_context={**self.snapshot(), "formatHint": self._collection_hint, "decimalPlaces": self._collection_precision})
            if request.command in {20, 21, 29, 30, 31, 34, 35, 41, 42} and (self._collection_hint == "AN" or (self._collection_hint == "S" and request.command != 20)):
                await self._required("resolveField", enriched, success=True)
            return enriched
        raw, code = request.buffer, request.field_type
        value = raw if code in {109, 137, 138} else self.decode(spec, raw)
        if code in {109, 137, 138, 1319} or code in self.monetary_formats:
            if code in self.monetary_formats:
                fmt = self.monetary_formats[code]
                if fmt == "decimal-comma" and "." in raw:
                    raise SitefError("INVALID_FIELD_VALUE", "Formato decimal-comma não aceita ponto.")
                value = decimal_text(raw, scale=2 if fmt == "minor-units" else None, minor_units=fmt == "minor-units")
            elif self.hooks.resolve_field is not None:
                resolved = await invoke(self.hooks.resolve_field, request, self.snapshot())
                if not isinstance(resolved, Mapping) or type(resolved.get("scale")) is not int or not isinstance(resolved.get("unscaled"), str):
                    raise SitefError("INVALID_MONETARY_RESOLUTION", "Resolver monetário deve informar unscaled textual e scale inteiro.")
                value = decimal_text(resolved["unscaled"], scale=resolved["scale"], minor_units=True)
        self._remember(code, raw, value, spec)
        enriched = replace(request, field_spec=spec, typed_value=value, field_context=self.snapshot())
        action = spec.get("action") or {}
        if action.get("kind") == "corban-document-index":
            self.corban_documents.append({"index": raw, "fields": {}})
        elif code in {600, 601, 602, 603, 604} and self.corban_documents:
            self.corban_documents[-1]["fields"].setdefault(str(code), []).append(value)
        if action.get("phase") == "beforeFinalize" and action.get("requiredHook") == "validateTransaction":
            self._deferred_hooks[code] = "validateTransaction"
        if code == 2355:
            if not re.fullmatch(r"A|AN|LN|N|V[0-9]+|S|M|Mc", raw):
                raise SitefError("INVALID_FORMAT_HINT", "Dica de formato 2355 desconhecida; implemente resolução explícita.")
            self.format_hint = raw
        elif code in {2470, 1121, 1126}:
            if not re.fullmatch(r"[0-9]+", raw) or not 0 <= int(raw) <= 32767:
                raise SitefError("INVALID_PRECISION", "Precisão informada inválida.")
            self.precision[{2470: "general", 1121: "unitPrice", 1126: "quantity"}[code]] = int(raw)
            if code == 2470:
                self._next_precision = int(raw)
        elif code == 123:
            self.receipt_type = raw
        elif code in {121, 122}:
            immediate = self.immediate_receipts or self.receipt_type in {"04", "05"}
            ordinal = sum(item["receipt"].field_type == code for item in self.receipts) + 1
            body_bytes = base64.b64decode(request.buffer_base64, validate=True) if request.buffer_base64 is not None else raw.encode("utf-8")
            receipt_value = Receipt(f"{self.payment_id}:{code}:{ordinal}", code, self.receipt_type,
                                    "immediate" if immediate else "deferred", raw, request.buffer_base64,
                                    hashlib.sha256(body_bytes).hexdigest())
            receipt = {"receipt": receipt_value, "printed": False}
            self.receipts.append(receipt)
            if self.receipt_storage == "persist":
                await self._required("storeReceipt", receipt_value, success=True)
            if immediate:
                await self._required("printReceipt", receipt_value, success=True)
                receipt["printed"] = True
        elif code == 161:
            self.payment_index = raw
        elif code == 131:
            self.authorizer_network = raw
        elif code in {5042, 5043, 5074}:
            hook = "lockResources" if code == 5042 else "unlockResources" if code == 5043 else "captureSignature"
            if code == 5042 and not callable(self.hooks.unlock_resources):
                raise SitefError("REQUIRED_FIELD_HOOK", "Bloqueio exige também unlock_resources para liberação segura.")
            if code == 5074 and spec.get("action", {}).get("phase") == "beforeFinalize":
                if not callable(self.hooks.capture_signature):
                    raise SitefError("REQUIRED_FIELD_HOOK", "Assinatura em papel exige capture_signature.")
                self.signature_required, self._signature_request = True, enriched
                return replace(enriched, field_context=self.snapshot())
            await self._required(hook, enriched, success=True)
            if code in {5042, 5043}:
                self.resources_locked = code == 5042
        elif code in {4039, 4100, 5084}:
            self.diagnostics.append({"fieldType": code, "value": raw, "error": code == 4039 and raw != "0"})
            if code == 4100 or self.hooks.on_diagnostic is not None:
                await self._required("onDiagnostic", enriched)
        elif code == 730:
            if not re.fullmatch(r"[0-9]+", raw):
                raise SitefError("INVALID_NFPAG", "Limite de formas de pagamento inválido.")
            self.nfpag_max = int(raw)
        elif code == 731:
            self._nfpag_current = raw
            self.nfpag_methods.setdefault(raw, [])
        elif code == 732:
            if self._nfpag_current is None:
                raise SitefError("INVALID_NFPAG", "Campo 732 sem método 731 anterior.")
            self.nfpag_methods[self._nfpag_current].append(raw)
        else:
            action = spec.get("action") or {}
            if action.get("requiredHook") and action["requiredHook"] not in {"acceptFallback", "validateTransaction"} and action.get("phase") not in {"beforeArithmetic", "beforeFinalize"}:
                resolved = await self._required(action["requiredHook"], enriched)
                if action["requiredHook"] == "resolveField" and resolved is not None and resolved is not True:
                    self.values[code] = resolved
                elif action.get("kind") == "application-product-list" and resolved is not None and resolved is not True:
                    if not isinstance(resolved, list) or any(not isinstance(product, Mapping) for product in resolved):
                        raise SitefError("INVALID_PRODUCT_DATA", "Resolver de lista de produtos deve retornar uma lista de objetos.")
                    self.values[code] = resolved
            elif spec.get("resolverRequired") and action.get("phase") not in {"beforeArithmetic", "beforeFinalize"}:
                self.values[code] = await self._required("resolveField", enriched)
        return replace(enriched, typed_value=self.values[code], field_context=self.snapshot())

    def _remember(self, code: int, raw: str, value: Any, spec: Mapping[str, Any], source: str = "native") -> None:
        self.values[code] = value
        self.raw_values.setdefault(code, []).append(raw)
        self.fields.append({"fieldType": code, "name": spec.get("name", "unknown"),
                            "dataType": spec.get("dataType", "text"), "raw": raw, "value": value, "source": source})
        if code == 1011:
            self._medicine_groups.append({"medicineIndex": raw, "fields": {}})
        elif code in {1042, 1043, 1044}:
            if not self._medicine_groups:
                self._medicine_groups.append({"fields": {}})
            self._medicine_groups[-1]["fields"].setdefault(code, []).append({"raw": raw, "value": value})

    def observe_response(self, request: NativeExchange, response: UiResponse) -> None:
        if response.continue_flag != 0 or request.command not in {20, 21, 29, 30, 31, 34, 35, 41, 42}:
            return
        spec, raw = request.field_spec, response.buffer
        value = self.decode(spec, raw)
        if request.field_type in self.monetary_formats:
            fmt = self.monetary_formats[request.field_type]
            if fmt == "decimal-comma" and "." in raw:
                raise SitefError("INVALID_FIELD_VALUE", "Valor monetário exige vírgula.")
            value = decimal_text(raw, scale=2 if fmt == "minor-units" else None, minor_units=fmt == "minor-units")
        self._remember(request.field_type, raw, value, spec, "collection")

    def _assess_product_constraints(self) -> None:
        constraints = []
        for group in self._medicine_groups:
            fields = group["fields"]
            if 1042 not in fields:
                continue
            raw = {str(code): rows[-1]["raw"] for code, rows in fields.items()}
            assessment = {"kind": "functional-card-price", "scope": {"medicineIndex": group["medicineIndex"]} if "medicineIndex" in group else {},
                          "fieldTypes": sorted(fields), "raw": raw, "state": "unresolved", "reason": "missing-medicine-index"}
            if "medicineIndex" in group:
                if any(len(rows) != 1 for rows in fields.values()):
                    assessment["reason"] = "ambiguous-price-occurrences"
                elif 1043 not in fields or 1044 not in fields:
                    assessment["reason"] = "missing-price-comparison"
                else:
                    fixed, actual = fields[1043][-1]["value"], fields[1044][-1]["value"]
                    mode = fields[1042][-1]["raw"]
                    if not isinstance(fixed, Decimal) or not isinstance(actual, Decimal):
                        assessment["reason"] = "monetary-units-required"
                    elif mode not in {"0", "1"}:
                        assessment["reason"] = "unknown-price-policy"
                    else:
                        satisfied = actual == fixed if mode == "0" else actual <= fixed
                        assessment.update(state="satisfied" if satisfied else "violated", reason="price-policy-comparison")
            constraints.append(assessment)
        product_lists = [self.values[code] for code in (1115, 1323) if isinstance(self.values.get(code), list)]
        for code in (1122, 1123):
            if code not in self.raw_values:
                continue
            raw = self.raw_values[code][-1]
            item = {"kind": "maximum-products" if code == 1122 else "product-code-length", "fieldTypes": [code],
                    "raw": {str(code): raw}, "state": "unresolved", "reason": "product-list-required"}
            if not re.fullmatch(r"[0-9]+", raw) or int(raw) == 0:
                item["reason"] = "zero-or-undefined-limit"
            elif len(product_lists) == 1:
                products = product_lists[0]
                if code == 1122:
                    item.update(state="satisfied" if len(products) <= int(raw) else "violated", reason="product-count-comparison")
                else:
                    identifiers = [p.get("Id", p.get("productCode")) for p in products]
                    if not identifiers or any(not isinstance(identifier, str) for identifier in identifiers):
                        item["reason"] = "product-code-required"
                    elif any(len(identifier) > int(raw) for identifier in identifiers):
                        item.update(state="violated", reason="product-code-exceeds-length")
                    elif all(len(identifier) == int(raw) for identifier in identifiers):
                        item.update(state="satisfied", reason="product-code-length-equal")
                    else:
                        item["reason"] = "short-code-exact-versus-maximum-ambiguous"
            constraints.append(item)
        self.product_constraints = constraints

    async def collect_system(self, request: NativeExchange) -> UiResponse:
        name = "resolveApplicationData" if self.hooks.resolve_application_data is not None else "systemData"
        response = await self._required(name, request)
        if isinstance(response, str):
            return UiResponse(response)
        if not isinstance(response, UiResponse):
            raise SitefError("INVALID_RESPONSE", "system_data deve retornar texto ou UiResponse.")
        return response

    def validate_response(self, request: NativeExchange, response: UiResponse) -> None:
        if response.continue_flag not in {0, 10000} or request.command not in {20, 21, 29, 30, 31, 34, 35, 41, 42}:
            return
        raw, hint = response.buffer, self._collection_hint
        if hint == "Mc" and response.confirmed is not True:
            raise SitefError("MENU_CONFIRMATION_REQUIRED", "Menu Mc exige confirmação explícita separada da seleção.")
        if hint == "A" and not all(ch.isalpha() or ch.isspace() for ch in raw):
            raise SitefError("INVALID_FIELD_FORMAT", "Campo alfabético inválido.")
        if hint == "LN" and not re.fullmatch(r"[A-Za-z0-9 ]*", raw):
            raise SitefError("INVALID_FIELD_FORMAT", "Campo permite apenas letras não acentuadas e números.")
        if hint == "N" and not re.fullmatch(r"[0-9]*", raw):
            raise SitefError("INVALID_FIELD_FORMAT", "Campo numérico inválido.")
        if hint and hint.startswith("V"):
            decimal_text(raw, scale=int(hint[1:]))
        elif request.command == 34 and raw:
            decimal_text(raw, scale=self._collection_precision)
        if hint == "S" and raw not in {"0", "1"}:
            raise SitefError("INVALID_FIELD_FORMAT", "Dica Sim/Não exige 0 ou 1.")
        choices = request.field_spec.get("choices")
        if choices and isinstance(choices, Mapping) and raw not in choices:
            raise SitefError("INVALID_FIELD_CHOICE", "Resposta não pertence às opções documentadas.")
        self.decode(request.field_spec, raw)

    async def release_resources(self) -> None:
        if self.resources_locked:
            dummy = NativeExchange(0, 0, 5043, 0, 0, "")
            await self._required("unlockResources", dummy, success=True)
            self.resources_locked = False

    def monetary_value(self, field_type: int) -> Decimal:
        value = self.values.get(field_type)
        if not isinstance(value, Decimal):
            raise SitefError("MONETARY_FORMAT_REQUIRED", "Aritmética exige formato monetário explícito ou resolve_field.")
        return value

    async def resolve_monetary_value(self, field_type: int) -> Decimal:
        """Resolve arithmetic lazily where the manufacturer omits units/scale."""
        if isinstance(self.values.get(field_type), Decimal):
            return self.values[field_type]
        if not self.raw_values.get(field_type) or not callable(self.hooks.resolve_field):
            raise SitefError("MONETARY_FORMAT_REQUIRED", "Aritmética exige escala/unidade explícita.")
        request = NativeExchange(0, 0, field_type, 0, 0, self.raw_values[field_type][-1],
                                 field_spec=self.field_spec(field_type), field_context=self.snapshot())
        resolved = await invoke(self.hooks.resolve_field, request, self.snapshot())
        if not isinstance(resolved, Mapping) or type(resolved.get("scale")) is not int or not isinstance(resolved.get("unscaled"), str):
            raise SitefError("INVALID_MONETARY_RESOLUTION", "Resolver monetário exige unscaled textual e scale inteiro.")
        self.values[field_type] = decimal_text(resolved["unscaled"], scale=resolved["scale"], minor_units=True)
        return self.values[field_type]

    async def complete(self) -> None:
        self._assess_product_constraints()
        if any(constraint["state"] == "violated" for constraint in self.product_constraints):
            raise SitefError("PRODUCT_CONSTRAINT_VIOLATED", "O resultado viola uma restrição de produto documentada.")
        unresolved_products = any(constraint["state"] == "unresolved" for constraint in self.product_constraints)
        acceptance = self.function.get("acceptanceField")
        fallback_indicators = {acceptance} if acceptance in {2361, 2363} else set()
        fallback_indicators.update(k for k in (2361, 2363) if k in self.raw_values)
        for indicator in fallback_indicators:
            if self.raw_values.get(indicator, ["0"])[-1] != "1":
                dummy = NativeExchange(0, 0, indicator, 0, 0, "")
                await self._required("acceptFallback", dummy, success=True)
        pbm_error = any(item["error"] for item in self.diagnostics)
        deferred_business = any(code not in {1042, 1043, 1044, 1122, 1123} for code in self._deferred_hooks)
        if unresolved_products or pbm_error or deferred_business or self.function.get("requiredHook") == "validateTransaction" or self.hooks.validate_transaction is not None:
            dummy = NativeExchange(0, 0, 0, 0, 0, "")
            await self._required("validateTransaction", dummy, success=True)
            self._pbm_resolved = pbm_error

    def receipt_metadata(self) -> list[dict[str, Any]]:
        return [{"id": value["receipt"].id, "fieldType": value["receipt"].field_type,
                 "type": value["receipt"].receipt_type, "timing": value["receipt"].timing,
                 "hash": value["receipt"].hash, "printed": value["printed"]} for value in self.receipts]

    async def before_confirm(self, on_change: Callable[..., Any] | None = None) -> None:
        if self.confirmation_blocked:
            raise SitefError("FIELD_ACTION_INCOMPLETE", "Há ação obrigatória incompleta; somente desfazimento é permitido.")
        if any(item["error"] for item in self.diagnostics) and not self._pbm_resolved:
            raise SitefError("PBM_REJECTED", "O retorno PBM não permite confirmar a transação.")
        for receipt in self.receipts:
            if not receipt["printed"]:
                await self._required("printReceipt", receipt["receipt"], success=True)
                receipt["printed"] = True
                if on_change is not None:
                    await invoke(on_change)
        if self.signature_required and not self.signature_captured:
            await self._required("captureSignature", self._signature_request, success=True)
            self.signature_captured = True
            if on_change is not None:
                await invoke(on_change)

    async def confirmation_network(self) -> str:
        if self.confirmation_network_field is not None:
            raw = self.raw_values.get(self.confirmation_network_field, [])
            if raw:
                return raw[-1]
        first, second = self.raw_values.get(131, []), self.raw_values.get(158, [])
        if first and second and first[-1] == second[-1]:
            return first[-1]
        hook = self.hooks.resolve_confirmation_network
        if callable(hook):
            result = await invoke(hook, self.snapshot())
            if isinstance(result, str) and re.fullmatch(r"[0-9]+", result):
                return result
        raise SitefError("CONFIRMATION_NETWORK_REQUIRED", "RedeConfirmacao exige política explícita para campos 131/158 ou resolver.")
