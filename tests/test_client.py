import asyncio
import base64
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from sitef import Configuration, FileJournal, NativeStart, SitefClient, SitefError, TransactionInput, UiResponse
from test_transport import fake_transport

CONFIG = Configuration("127.0.0.1", "00000000", "AA123456", "[X=1]")
INPUT = TransactionInput(0, "12,34", "123", "20261030", "120000", "test", "{X=1}")


async def answer(request, context):
    return UiResponse("1234" if request.command == 30 else "")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            if client.state == "awaiting-finalization":
                await client.finalize(False)
            if client.state != "closed":
                await client.close()
        self.temporary.cleanup()

    async def connect(self, mode="normal", interaction=answer, on_exchange=None, path=None):
        transport = await fake_transport(mode)
        await transport.load(self.directory / "synthetic-only.dll")
        journal = FileJournal(path or self.directory / "terminal.journal.json")
        client = await SitefClient.connect(configuration=CONFIG, transport=transport, journal=journal,
                                            interaction=interaction, on_exchange=on_exchange)
        self.clients.append(client)
        return client

    async def test_preserves_unknown_bytes_and_requires_explicit_void_finalization(self):
        client = await self.connect()
        result = await client.transact(INPUT, requires_finalization=True)
        self.assertEqual(result.status, 0)
        self.assertEqual(result.fields[0].field_type, 999123)
        self.assertEqual(base64.b64decode(result.fields[0].buffer_base64), bytes([65, 231, 227, 111, 10]))
        self.assertEqual(client.state, "awaiting-finalization")
        with self.assertRaises(SitefError) as close_error:
            await client.close()
        self.assertEqual(close_error.exception.code, "FINALIZATION_REQUIRED")
        final = await client.finalize(True, "{Future=1}")
        self.assertEqual(final, {"returned": True, "remoteConfirmation": "not-reported"})
        self.assertEqual(client.recovery["fiscalDocument"], INPUT.fiscal_document)
        self.assertNotIn("Ação", (self.directory / "terminal.journal.json").read_text())

    async def test_cooperative_cancel_interrupts_pending_callback_and_drains_native_loop(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def waiting(request, context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        client = await self.connect(interaction=waiting)
        transaction = asyncio.create_task(client.transact(INPUT, requires_finalization=True))
        await asyncio.wait_for(entered.wait(), 2)
        client.cancel()
        result = await asyncio.wait_for(transaction, 2)
        await asyncio.wait_for(cancelled.wait(), 2)
        self.assertEqual(result.status, -2)
        self.assertEqual(client.state, "idle")
        self.assertFalse(result.requires_finalization)

    async def test_callback_failure_cancels_and_exposes_terminal_status(self):
        async def broken(request, context):
            raise ValueError("synthetic callback error")
        client = await self.connect(interaction=broken)
        with self.assertRaises(SitefError) as caught:
            await client.transact(INPUT, requires_finalization=True)
        self.assertEqual(caught.exception.code, "INTERACTION_FAILED")
        self.assertEqual(caught.exception.native_status, -2)
        self.assertEqual(client.recovery["state"], "ended")

    async def test_slow_poll_observer_does_not_delay_peripheral_loop(self):
        async def observer(request):
            if request.command == 23:
                await asyncio.Event().wait()
        client = await self.connect(mode="poll", on_exchange=observer)
        result = await asyncio.wait_for(client.transact(replace(INPUT, function_id=111)), 2)
        self.assertEqual(result.status, 0)
        self.assertEqual(client.state, "idle")

    async def test_crash_after_start_persists_uncertainty_and_does_not_retry(self):
        client = await self.connect(mode="start-crash")
        with self.assertRaises(SitefError) as caught:
            await client.transact(INPUT, requires_finalization=True)
        self.assertEqual(caught.exception.code, "TRANSACTION_UNCERTAIN")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(client.recovery["state"], "uncertain")
        with self.assertRaises(SitefError):
            await client.transact(INPUT, requires_finalization=True)
        await client.close()
        recovered = await self.connect()
        self.assertEqual(recovered.state, "uncertain")
        self.assertEqual((await recovered.call_native("pendingCount", {"fiscalDate": INPUT.fiscal_date, "fiscalDocument": INPUT.fiscal_document}))["count"], 1)
        with self.assertRaises(SitefError):
            await recovered.call_native("flushMessages")

    async def test_finalization_crash_recovery_keeps_decision_scope_and_original_identity(self):
        client = await self.connect(mode="finish-crash")
        sale = await client.transact(INPUT, requires_finalization=True)
        scope = "{Future=1}"
        with self.assertRaises(SitefError) as caught:
            await client.finalize(True, scope)
        self.assertEqual(caught.exception.code, "FINALIZATION_UNCERTAIN")
        await client.close()
        recovered = await self.connect()
        self.assertEqual(recovered.recovery["finalizationParameters"], scope)
        with self.assertRaises(SitefError) as mismatch:
            await recovered.finalize_recovery(False, sale.transaction_id)
        self.assertEqual(mismatch.exception.code, "RECOVERY_DECISION_MISMATCH")
        with self.assertRaises(SitefError) as mismatch:
            await recovered.finalize_recovery(True, sale.transaction_id, "{NumeroPagamentoCupom=3}")
        self.assertEqual(mismatch.exception.code, "RECOVERY_SCOPE_MISMATCH")
        await recovered.finalize_recovery(True, sale.transaction_id)
        self.assertEqual(recovered.recovery["fiscalDocument"], INPUT.fiscal_document)
        self.assertEqual(recovered.recovery["finalizationParameters"], scope)
        self.assertEqual(recovered.state, "idle")

    async def test_unknown_status_remains_numeric_and_queries_need_no_financial_finish(self):
        client = await self.connect()
        result = await client.transact(replace(INPUT, function_id=999), requires_finalization=False)
        self.assertEqual(result.status, -98765)
        self.assertFalse(result.requires_finalization)
        self.assertEqual(client.state, "idle")
        query = await client.transact(replace(INPUT, function_id=111), requires_finalization=False)
        self.assertFalse(query.requires_finalization)

    async def test_input_validation_happens_before_native_start(self):
        client = await self.connect()
        for invalid in [replace(INPUT, amount=12.34), replace(INPUT, function_id=True),
                        replace(INPUT, operator=None), replace(INPUT, fiscal_date="20260230"),
                        replace(INPUT, fiscal_document="x" * 21), replace(INPUT, amount="12.34")]:
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(SitefError):
                    await client.transact(invalid, requires_finalization=True)
                self.assertIsNone(client.recovery)
        result = await client.transact(INPUT)
        self.assertTrue(result.requires_finalization)

    async def test_external_event_can_abort_before_start(self):
        event = asyncio.Event()
        event.set()
        client = await self.connect()
        with self.assertRaises(SitefError) as caught:
            await client.transact(INPUT, requires_finalization=True, cancel_event=event)
        self.assertEqual(caught.exception.code, "ABORTED")
        self.assertIsNone(client.recovery)

    async def test_forced_host_close_keeps_journal_exclusive_until_process_is_checked(self):
        client = await self.connect(mode="blocked-close")
        client._transport._close_timeout = 0.05
        with self.assertRaises(SitefError) as caught:
            await client.close()
        self.assertEqual(caught.exception.code, "BRIDGE_CLOSE_TIMEOUT")
        self.assertTrue((self.directory / "terminal.journal.json.lock").exists())
        with self.assertRaises(SitefError):
            await FileJournal(self.directory / "terminal.journal.json").acquire()
        self.assertIsNotNone(client._transport._process.returncode)
        # Explicit second close after the process termination check releases it.
        await client.close()
        self.assertFalse((self.directory / "terminal.journal.json.lock").exists())
