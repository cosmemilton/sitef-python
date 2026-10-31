"""Opt-in Windows x64 smoke: load/version/configure only. No transaction/pinpad."""
import argparse
import asyncio
import json
from pathlib import Path

from sitef import Configuration, SitefError, create_sitef


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--server", default="127.0.0.1")
    parser.add_argument("--store-id", default="00000000")
    parser.add_argument("--terminal-id", default="AA123456")
    parser.add_argument("--additional-parameters", default="")
    args = parser.parse_args()
    async def unsupported_interaction(request, context):
        # This smoke never starts an iterative function.
        raise SitefError("UNEXPECTED_INTERACTION", "O teste não solicita interação transacional.")
    try:
        client = await create_sitef(
            bridge_executable=args.bridge.resolve(), bridge_directory=args.bridge.resolve().parent,
            library_path=args.library.resolve(), state_directory=args.state_directory.resolve(),
            configuration=Configuration(args.server, args.store_id, args.terminal_id, args.additional_parameters),
            interaction=unsupported_interaction)
        try:
            capabilities = await client.capabilities()
            versions = await client.call_native("version")
            print(json.dumps({
                "protocolVersion": capabilities.get("protocolVersion"),
                "platform": capabilities.get("platform"),
                "architecture": capabilities.get("architecture"),
                "fieldTypeBytes": capabilities.get("fieldTypeBytes"),
                "versionStatus": versions.get("status"),
                "libraryVersion": versions.get("libraryVersion", {}).get("text"),
                "interfaceVersion": versions.get("interfaceVersion", {}).get("text"),
                "configurationStatus": 0,
                "transactionsRequested": False,
                "pinpadOperationsRequested": False,
            }, ensure_ascii=False))
            return 0 if versions.get("status") == 0 else 2
        finally:
            await client.close()
    except SitefError as exc:
        print(json.dumps({"error": exc.code, "uncertain": exc.uncertain}))
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
