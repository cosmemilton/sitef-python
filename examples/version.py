"""Read library version without starting a transaction or configuring a pinpad."""
import argparse
import asyncio
from pathlib import Path

from sitef import BridgeTransport


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", required=True, type=Path)
    parser.add_argument("--library", required=True, type=Path)
    args = parser.parse_args()
    async with await BridgeTransport.open(args.bridge.resolve(), cwd=args.bridge.resolve().parent) as transport:
        await transport.load(args.library.resolve())
        version = await transport.call("version")
        print(version)


if __name__ == "__main__":
    asyncio.run(main())
