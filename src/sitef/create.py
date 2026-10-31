"""Simple factory with protocol validation and a stable terminal journal key."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Sequence

from .client import SitefClient
from .errors import SitefError
from .journal import FileJournal
from .models import Configuration, ExchangeObserver, InteractionHandler
from .fields import FieldHooks
from typing import Mapping
from .transport import BridgeTransport


async def create_sitef(*, bridge_executable: str | os.PathLike[str],
                       bridge_directory: str | os.PathLike[str],
                       library_path: str | os.PathLike[str], state_directory: str | os.PathLike[str],
                       configuration: Configuration, interaction: InteractionHandler,
                       on_exchange: ExchangeObserver | None = None,
                       working_directory: str | os.PathLike[str] | None = None,
                       native_encoding: str = "windows-1252", timeout: float = 120.0,
                       max_frame_bytes: int = 2 * 1024 * 1024,
                       buffer_size: int = 20000, max_steps: int = 100000,
                       test_mode: bool = False, bridge_args: Sequence[str] = (), hooks: FieldHooks | None = None,
                       receipt_storage: str = "persist", monetary_formats: Mapping[int, str] | None = None,
                       confirmation_network_field: int | None = None) -> SitefClient:
    """Open -> load -> validate -> acquire journal -> configure.

    Journal identity is SHA256(UTF8(storeId + NUL + terminalId)), matching the
    Node/PHP factory contract when they use the same persistent state directory.
    test_mode is exclusively for original synthetic fixtures.
    """
    state_path = Path(state_directory)
    if not state_path.is_absolute():
        raise SitefError("INVALID_PATH", "state_directory precisa ser absoluto e persistente.")
    if (type(test_mode) is not bool or not isinstance(configuration, Configuration)
            or not isinstance(configuration.store_id, str) or not isinstance(configuration.terminal_id, str)):
        raise SitefError("INVALID_ARGUMENT", "Configuração ou test_mode inválido.")
    transport = await BridgeTransport.open(bridge_executable, cwd=bridge_directory, args=bridge_args,
                                          timeout=timeout, max_frame_bytes=max_frame_bytes, test_mode=test_mode)
    try:
        capabilities = await transport.load(library_path, working_directory=working_directory,
                                            native_encoding=native_encoding)
        if type(capabilities.get("protocolVersion")) is not int or capabilities["protocolVersion"] != 1:
            raise SitefError("PROTOCOL_MISMATCH", "Versão incompatível da ponte nativa.")
        if not test_mode:
            if capabilities.get("platform") != "windows":
                raise SitefError("UNSUPPORTED_PLATFORM", "A ponte de produção precisa executar no Windows.")
            if capabilities.get("architecture") != "x86_64":
                raise SitefError("UNSUPPORTED_ARCHITECTURE", "A ponte de produção precisa ser Windows x64.")
            if type(capabilities.get("fieldTypeBytes")) is not int or capabilities["fieldTypeBytes"] != 4:
                raise SitefError("ABI_MISMATCH", "TipoCampo precisa usar long Windows de 32 bits.")
            if capabilities.get("testMode") is not False:
                raise SitefError("PROTOCOL_MISMATCH", "A ponte de produção não pode estar em modo de testes.")
        terminal_key = hashlib.sha256((configuration.store_id + "\0" + configuration.terminal_id).encode("utf-8")).hexdigest()
        return await SitefClient.connect(
            configuration=configuration, transport=transport,
            journal=FileJournal(state_path / (terminal_key + ".journal.json")),
            interaction=interaction, on_exchange=on_exchange, native_encoding=native_encoding,
            buffer_size=buffer_size, max_steps=max_steps, hooks=hooks, receipt_storage=receipt_storage,
            monetary_formats=monetary_formats, confirmation_network_field=confirmation_network_field)
    except BaseException:
        await transport.close()
        raise
