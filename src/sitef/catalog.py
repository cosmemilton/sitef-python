"""Shared JSON catalog, not a whitelist or a complete field implementation."""
from __future__ import annotations

import copy
from functools import lru_cache
from importlib.resources import files
import json
from typing import Any

from .errors import SitefError


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    try:
        value = json.loads(files("sitef").joinpath("data/catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SitefError("CATALOG_UNAVAILABLE", "Catálogo comum indisponível ou inválido.") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise SitefError("CATALOG_VERSION", "Versão do catálogo comum não suportada.")
    return value


def catalog() -> dict[str, Any]:
    """Return a copy so application changes cannot alter SDK behavior."""
    return copy.deepcopy(_catalog())


def command_info(code: int) -> dict[str, Any]:
    return dict(_catalog().get("commands", {}).get(str(code), {
        "kind": "unknown", "blocking": True,
        "description": f"Comando não catalogado ({code}); exige callback explícito.",
    }))


def describe_return(code: int, context: str = "transaction") -> dict[str, Any]:
    """Cataloged description and raw code; the caller selects the native domain.

    Specialized pinpad/count/barcode outcomes remain raw; do not apply the
    transaction success/decline rules to them.
    """
    if type(code) is not int:
        raise SitefError("INVALID_ARGUMENT", "Retorno nativo deve ser inteiro.")
    keys = {"configuration": "configurationReturns", "transaction": "transactionReturns", "secure-read": "secureReadReturns"}
    if context not in keys:
        return {"code": code, "known": False, "kind": "raw", "message": f"Retorno nativo ({code})"}
    table = _catalog().get(keys[context], {})
    message = table.get(str(code))
    if context == "transaction":
        kind = "continue" if code == 10000 else "success" if code == 0 else "authorizer-declined" if code > 0 else "error"
    else:
        kind = "success" if code == 0 else "error"
    return {"code": code, "known": message is not None, "kind": kind,
            "message": message or f"Retorno nativo não catalogado ({code})"}
