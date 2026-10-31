"""Typed NFPAG construction. Values are exact decimal strings, never floats."""
from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Mapping, Sequence

from .errors import SitefError
from .fields import FieldProcessor, to_minor_units


@dataclass(frozen=True)
class PaymentMethod:
    payment_type: str
    amount: str = field(repr=False)
    data: Mapping[str, str] = field(default_factory=dict, repr=False)


def _safe(value: str) -> str:
    if not isinstance(value, str) or any(ch in value for ch in "\0{};:-"):
        raise SitefError("INVALID_PAYMENT_DATA", "Dado NFPAG contém delimitador ou tipo inválido.")
    return value


def build_payment_parameters(processor: FieldProcessor, methods: Sequence[PaymentMethod], total_amount: str) -> str:
    if not isinstance(methods, Sequence) or isinstance(methods, (str, bytes)):
        raise SitefError("INVALID_PAYMENT_METHODS", "Formas de pagamento devem ser uma sequência de PaymentMethod.")
    index = processor.payment_index
    if not index or not re.fullmatch(r"[0-9]+", index):
        raise SitefError("PAYMENT_INDEX_REQUIRED", "NFPAG exige índice 161 conhecido.")
    if processor.nfpag_max is None or not processor.nfpag_methods or not methods:
        raise SitefError("PAYMENT_POLICY_REQUIRED", "Receba os campos 730/731/732 antes de montar NFPAG.")
    if processor.nfpag_max and len(methods) > processor.nfpag_max:
        raise SitefError("TOO_MANY_PAYMENT_METHODS", "Número de formas de pagamento excede o campo 730.")
    expected, total = int(to_minor_units(total_amount)), 0
    if expected < 0:
        raise SitefError("INVALID_PAYMENT_AMOUNT", "Total não pode ser negativo.")
    rows = []
    origin = {"03": 131, "04": 133, "07": 134, "09": 157, "11": 135, "16": 106}
    for method in methods:
        if not isinstance(method, PaymentMethod) or method.payment_type not in processor.nfpag_methods:
            raise SitefError("INVALID_PAYMENT_TYPE", "Forma não habilitada no campo 731.")
        if not isinstance(method.data, Mapping):
            raise SitefError("INVALID_PAYMENT_DATA", "Dados da forma de pagamento devem ser um mapping textual.")
        amount = to_minor_units(method.amount)
        if int(amount) < 0:
            raise SitefError("INVALID_PAYMENT_AMOUNT", "Valor da forma não pode ser negativo.")
        total += int(amount)
        required = processor.nfpag_methods[method.payment_type]
        data = dict(method.data)
        if "13" in data:
            raise SitefError("INVALID_PAYMENT_DATA", "ID 13 é metadado de restrição; não deve ser enviado.")
        limits = []
        ids = []
        for requirement in required:
            match = re.fullmatch(r"13\(([0-9]+(?:,[0-9]+)*)\)", requirement)
            if match:
                limits += match[1].split(",")
            elif re.fullmatch(r"[0-9]{2}", requirement) and requirement not in {"00", "05", "13"}:
                ids.append(requirement)
            elif requirement not in {"00", "05", "13"}:
                raise SitefError("UNSUPPORTED_PAYMENT_REQUIREMENT", "Requisito NFPAG precisa de resolução explícita.")
        if set(data) - set(ids):
            raise SitefError("INVALID_PAYMENT_DATA", "Dado não solicitado no campo 732.")
        for identifier in ids:
            if identifier not in data:
                if identifier in origin and processor.raw_values.get(origin[identifier]):
                    data[identifier] = processor.raw_values[origin[identifier]][-1]
                elif identifier == "08" and processor.raw_values.get(105):
                    data[identifier] = datetime.strptime(processor.raw_values[105][-1], "%Y%m%d%H%M%S").strftime("%d%m%Y")
                elif identifier == "10" and processor.raw_values.get(1):
                    data[identifier] = processor.raw_values[1][-1].split(";")[0]
            if identifier not in data:
                raise SitefError("PAYMENT_DATA_REQUIRED", f"Forneça o dado NFPAG {identifier}.")
            if identifier == "10" and isinstance(data[identifier], str):
                data[identifier] = data[identifier].split(";", 1)[0]
            _safe(data[identifier])
        if limits and data.get("03") not in limits:
            raise SitefError("PAYMENT_NETWORK_RESTRICTED", "Rede não permitida pelo ID 13 do campo 732.")
        if "01" in data:
            if data["01"] not in {"0", "1", "2"}:
                raise SitefError("INVALID_PAYMENT_DATA", "Tipo de entrada de cheque NFPAG inválido.")
            if data["01"] == "1" and not re.fullmatch(r"[0-9]{29}", data.get("02", "")):
                raise SitefError("INVALID_PAYMENT_DATA", "Primeira linha de cheque NFPAG exige 29 dígitos com DVs.")
        if "15" in data and data["15"] not in {"00", "01", "02", "03"}:
            raise SitefError("INVALID_PAYMENT_DATA", "Tipo de pagamento NFPAG inválido.")
        if "08" in data:
            try:
                if not re.fullmatch(r"[0-9]{8}", data["08"]):
                    raise ValueError("Host date width")
                datetime.strptime(data["08"], "%d%m%Y")
            except ValueError as exc:
                raise SitefError("INVALID_PAYMENT_DATA", "Data Host NFPAG deve ser DDMMAAAA válida.") from exc
        suffix = "-".join(identifier + ":" + data[identifier] for identifier in ids)
        rows.append(method.payment_type + ":" + amount + (":" + suffix if suffix else ""))
    if total != expected:
        raise SitefError("PAYMENT_TOTAL_MISMATCH", "Soma exata das formas diverge do total informado.")
    return "{NumeroPagamentoNFPAG=" + index + "}{NFPAG=" + ";".join(rows) + ";}"
