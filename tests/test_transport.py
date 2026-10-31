import asyncio
import sys
from pathlib import Path
import unittest

from sitef import BridgeTransport, SitefError

HERE = Path(__file__).resolve().parent


async def fake_transport(mode="normal", **options):
    return await BridgeTransport.open(sys.executable, cwd=HERE,
                                      args=[str(HERE / "fake_host.py"), mode], test_mode=True, **options)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_calls_are_correlated_and_unicode_is_not_shell_input(self):
        transport = await fake_transport()
        try:
            results = await asyncio.gather(*(transport.call("echo", {"text": f"ação\n{i};$(echo unsafe)"}) for i in range(8)))
            self.assertEqual([result["echo"]["text"] for result in results], [f"ação\n{i};$(echo unsafe)" for i in range(8)])
        finally:
            await transport.close()

    async def test_fatal_frames_close_host_and_never_retry(self):
        for mode in ["wrong-id", "duplicate-key", "partial", "oversize", "invalid-utf8", "exit", "timeout"]:
            with self.subTest(mode=mode):
                transport = await fake_transport(mode, timeout=3.0, max_frame_bytes=2048)
                await transport.call("capabilities")
                if mode == "timeout":
                    transport._timeout = 0.05
                with self.assertRaises(SitefError) as caught:
                    await transport.call("invalid")
                self.assertTrue(caught.exception.uncertain)
                self.assertNotIn("synthetic-private-payload", str(caught.exception))
                with self.assertRaises(SitefError) as closed:
                    await transport.call("echo")
                self.assertEqual(closed.exception.code, "BRIDGE_CLOSED")
                await transport.close()

    async def test_local_oversize_does_not_send_or_destroy_host(self):
        transport = await fake_transport(max_frame_bytes=2048)
        try:
            with self.assertRaises(SitefError) as caught:
                await transport.call("echo", {"text": "a" * 3000})
            self.assertEqual(caught.exception.code, "FRAME_TOO_LARGE")
            self.assertFalse(caught.exception.uncertain)
            self.assertEqual((await transport.call("echo", {"ok": True}))["echo"], {"ok": True})
        finally:
            await transport.close()

    async def test_valid_native_error_does_not_desynchronize_channel(self):
        transport = await fake_transport()
        try:
            with self.assertRaises(SitefError) as caught:
                await transport.call("native-error")
            self.assertEqual(caught.exception.code, "MISSING_SYMBOL")
            self.assertEqual((await transport.call("echo", {"ok": 1}))["echo"]["ok"], 1)
        finally:
            await transport.close()

    async def test_production_platform_gate(self):
        if sys.platform != "win32":
            with self.assertRaises(SitefError) as caught:
                await BridgeTransport.open(sys.executable, cwd=HERE)
            self.assertEqual(caught.exception.code, "UNSUPPORTED_PLATFORM")
        with self.assertRaises(SitefError):
            await BridgeTransport.open("relative-host", cwd=HERE, test_mode=True)

    async def test_cancelled_ipc_task_makes_channel_uncertain(self):
        transport = await fake_transport("timeout")
        await transport.call("capabilities")
        task = asyncio.create_task(transport.call("invalid"))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(transport.closed)
        await transport.close()

    async def test_forced_close_reports_uncertainty_even_when_kill_succeeds(self):
        transport = await fake_transport("blocked-close")
        await transport.call("capabilities")
        transport._close_timeout = 0.05
        with self.assertRaises(SitefError) as caught:
            await transport.close()
        self.assertEqual(caught.exception.code, "BRIDGE_CLOSE_TIMEOUT")
        self.assertTrue(caught.exception.uncertain)
        self.assertIsNotNone(transport._process.returncode)
