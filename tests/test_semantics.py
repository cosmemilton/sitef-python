"""Own deterministic field/payment flows; no vendor DLL, bank or printer."""
import asyncio
import base64
import copy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import unittest

from sitef import (Configuration, FieldHooks, FieldProcessor, NativeExchange, PaymentMethod,
                   SitefClient, SitefError, TransactionInput, UiResponse, NativeStart, decimal_text, to_minor_units)
from sitef.journal import validate_record

CONFIG = Configuration("127.0.0.1", "00000000", "AA123456")
SALE = TransactionInput(0, "12,34", "123", "20261030", "120000", "test")


def data(code, text, command=0, **extra):
    return {"status": 10000, "command": command, "fieldType": code,
            "minLength": 0, "maxLength": 80, "buffer": text, **extra}


def request(code, text, command=0):
    return NativeExchange.from_native(data(code, text, command), "field")


class MemoryJournal:
    def __init__(self):
        self.record, self.writes, self.locked = None, [], False
    async def acquire(self):
        assert not self.locked
        self.locked = True
    async def read(self):
        return copy.deepcopy(self.record)
    async def write(self, value):
        self.record = copy.deepcopy(validate_record(value))
        self.writes.append(copy.deepcopy(self.record))
    async def release(self):
        self.locked = False


class ScriptedTransport:
    def __init__(self, cycles=(), *, fail_finish=0, cancel_status=-2, log=None):
        self.cycles, self.steps, self.calls = list(cycles), [], []
        self.fail_finish, self.finishes, self.cancel_status = fail_finish, 0, cancel_status
        self.log = log if log is not None else []
    async def call(self, method, params=None):
        self.calls.append((method, copy.deepcopy(params or {})))
        if method == "configure":
            return {"status": 0}
        if method in {"start", "startBanking", "readSecureCard", "readTrack3", "readPassword", "readChip"}:
            self.steps = list(self.cycles.pop(0))
            return {"status": 10000}
        if method == "continue":
            if params["continueFlag"] == -1:
                return {"status": self.cancel_status}
            return self.steps.pop(0) if self.steps else {"status": 0}
        if method == "finish":
            self.finishes += 1
            self.log.append("finish")
            if self.finishes == self.fail_finish:
                raise SitefError("BRIDGE_EOF", "Synthetic lost reply", uncertain=True)
            return {"returned": True}
        return {}
    async def close(self):
        pass


class SemanticTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, transport, *, journal=None, hooks=None, **options):
        journal = journal or MemoryJournal()
        async def answer(_request, _context):
            return UiResponse("1234")
        client = await SitefClient.connect(configuration=CONFIG, transport=transport, journal=journal,
                                            interaction=answer, hooks=hooks, **options)
        return client, journal

    async def test_all_shared_entries_route_and_secure_collisions_require_context(self):
        normal = FieldProcessor(0)
        secure = FieldProcessor(430, FieldHooks(resolve_field=lambda event, _: event.buffer))
        self.assertEqual(len(normal.specs["entries"]), 536)
        self.assertEqual(normal.field_spec(2053)["contexts"], ["transaction"])
        self.assertIn("secure-card-read", secure.field_spec(2053)["contexts"])
        event = await secure.process(request(2053, "protected"))
        self.assertTrue(event.field_spec["sensitive"])
        for code, spec in normal.specs["entries"].items():
            self.assertIn("dataType", normal.field_spec(int(code)))
            self.assertIn("action", spec)

    async def test_format_hint_survives_messages_and_precision_is_next_collection(self):
        processor = FieldProcessor(0)
        await processor.process(request(2355, "N"))
        await processor.process(request(0, "Caption", 1))
        collect = await processor.process(request(999, "Input", 30))
        with self.assertRaises(SitefError):
            processor.validate_response(collect, UiResponse("ABC"))
        processor.validate_response(collect, UiResponse("123"))
        await processor.process(request(2470, "3"))
        await processor.process(request(1121, "4"))
        await processor.process(request(1126, "5"))
        collect = await processor.process(request(999, "Amount", 34))
        processor.validate_response(collect, UiResponse("1,234"))
        with self.assertRaises(SitefError):
            processor.validate_response(collect, UiResponse("1,2345"))
        collect = await processor.process(request(999, "Next", 34))
        with self.assertRaises(SitefError):
            processor.validate_response(collect, UiResponse("1,234"))
        self.assertEqual(processor.snapshot()["precision"]["unitPrice"], 4)
        self.assertEqual(processor.snapshot()["precision"]["quantity"], 5)

    async def test_ambiguous_amounts_stay_raw_until_explicit_format(self):
        processor = FieldProcessor(0)
        await processor.process(request(137, "1234"))
        self.assertEqual(processor.values[137], "1234")
        with self.assertRaises(SitefError):
            processor.monetary_value(137)
        explicit = FieldProcessor(0, monetary_formats={137: "minor-units"})
        await explicit.process(request(137, "1234"))
        self.assertEqual(explicit.monetary_value(137), Decimal("12.34"))
        self.assertEqual(to_minor_units("999999999999999999,99"), "99999999999999999999")
        with self.assertRaises(SitefError):
            decimal_text(12.34)

    async def test_receipts_store_during_loop_print_then_signature_before_finish(self):
        log, stored = [], {}
        def store(receipt, _):
            log.append("store")
            stored[receipt.id] = receipt
            return True
        hooks = FieldHooks(store_receipt=store, print_receipt=lambda *_: log.append("print") or True,
                           capture_signature=lambda *_: log.append("sign") or True)
        raw = b"A\xe7\xe3o\n"
        transport = ScriptedTransport([[data(5074, ""), data(123, "00"),
                                        data(121, "Ação\n", bufferBase64=base64.b64encode(raw).decode())]], log=log)
        client, journal = await self.client(transport, hooks=hooks)
        result = await client.transact(SALE)
        self.assertEqual(log, ["store"])
        meta = journal.record["payments"][0]["receipts"][0]
        self.assertEqual(meta["hash"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.receipts[0].id, result.payment_id + ":121:1")
        self.assertNotIn("Ação", json.dumps(journal.record))
        await client.finalize(True)
        self.assertEqual(log, ["store", "print", "sign", "finish"])
        self.assertTrue(journal.record["payments"][0]["signatureCaptured"])
        await client.close()

    async def test_immediate_print_failure_cancels_before_next_command(self):
        transport = ScriptedTransport([[data(123, "05"), data(121, "receipt"), data(161, "1")]])
        client, journal = await self.client(transport, hooks=FieldHooks(store_receipt=lambda *_: True, print_receipt=lambda *_: False))
        with self.assertRaises(SitefError) as caught:
            await client.transact(replace(SALE, function_id=310))
        self.assertEqual(caught.exception.native_status, -2)
        self.assertEqual(transport.calls[-1][1]["continueFlag"], -1)
        self.assertEqual(transport.finishes, 0)
        await client.close()

    async def test_required_actions_failure_after_approved_result_never_becomes_uncertain(self):
        transport = ScriptedTransport([[data(4039, "5")]])
        client, journal = await self.client(transport)
        with self.assertRaises(SitefError) as caught:
            await client.transact(SALE)
        self.assertEqual(caught.exception.native_status, 0)
        self.assertEqual(client.state, "awaiting-finalization")
        self.assertFalse(journal.record["payments"][0]["confirmationAllowed"])
        with self.assertRaises(SitefError):
            await client.finalize(True)
        await client.finalize(False)
        await client.close()

    async def test_resource_lock_is_released_after_cancel_without_5043(self):
        log = []
        transport = ScriptedTransport([[data(5042, ""), {"status": -2}]])
        hooks = FieldHooks(lock_resources=lambda *_: log.append("lock") or True,
                           unlock_resources=lambda *_: log.append("unlock") or True)
        client, _ = await self.client(transport, hooks=hooks)
        result = await client.transact(SALE)
        self.assertEqual(result.status, -2)
        self.assertEqual(log, ["lock", "unlock"])
        await client.close()

    async def test_multiple_payments_selective_finish_and_failure_preserve_siblings(self):
        transport = ScriptedTransport([[data(161, "1")], [data(161, "2")], [{"status": -40}]])
        client, journal = await self.client(transport)
        first = await client.transact(SALE)
        second = await client.append_payment(SALE)
        rejected = await client.append_payment(SALE)
        self.assertEqual(rejected.status, -40)
        self.assertEqual(len(client._pending()), 2)
        self.assertEqual(len(client._processors), 2)  # Ended attempts retain no sensitive runtime data.
        with self.assertRaises(SitefError):
            await client.append_payment(replace(SALE, fiscal_document="other"))
        with self.assertRaises(SitefError):
            await client.finalize(True, "{NumeroPagamentoCupom=1}")
        await client.finalize_payment(False, first.payment_id)
        self.assertNotIn(first.payment_id, client._processors)
        self.assertIn(second.payment_id, client._processors)
        self.assertEqual(client.state, "awaiting-finalization")
        self.assertEqual(transport.calls[-1][1]["additionalParameters"], "{NumeroPagamentoCupom=1}")
        self.assertEqual(client._pending()[0]["id"], second.payment_id)
        await client.finalize(False)
        self.assertEqual(client._processors, {})
        await client.close()

    async def test_multidate_plan_is_durable_before_first_finish_and_recovery_skips_finished(self):
        transport = ScriptedTransport([[data(161, "1")], [data(161, "1")]], fail_finish=2)
        client, journal = await self.client(transport)
        first = await client.transact(SALE)
        await client.append_payment(replace(SALE, fiscal_document="124", fiscal_date="20261031", fiscal_time="010000"))
        with self.assertRaises(SitefError):
            await client.finalize(True)
        plan = next(row for row in journal.writes if row["state"] == "finalizing")
        self.assertEqual([p["decision"] for p in plan["payments"]], [1, 1])
        self.assertEqual([p["state"] for p in journal.record["payments"]], ["finalized-local", "uncertain"])
        await client.close()
        recovered_transport = ScriptedTransport()
        recovered, _ = await self.client(recovered_transport, journal=journal)
        await recovered.finalize_recovery(True, first.transaction_id)
        calls = [params for method, params in recovered_transport.calls if method == "finish"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["fiscalDocument"], calls[0]["fiscalDate"], calls[0]["fiscalTime"]), ("124", "20261031", "010000"))
        await recovered.close()

    async def test_pending_query_and_nonfinancial_operation_do_not_replace_financial_journal(self):
        pending = [data(210, "1"), data(160, "123"), data(161, "1"), data(163, "20261030"),
                   data(164, "120000"), data(211, "0"), data(1319, "12,34")]
        transport = ScriptedTransport([[data(161, "1")], pending, []])
        client, journal = await self.client(transport)
        await client.transact(SALE)
        before = copy.deepcopy(journal.record)
        rows = await client.query_pending_transactions()
        self.assertEqual(rows[0]["amount"], "12,34")
        self.assertEqual(journal.record, before)
        await client.run_function(111)
        self.assertEqual(journal.record, before)
        self.assertEqual(client.state, "awaiting-finalization")
        await client.finalize(False)
        await client.close()

    async def test_nf_pag_restrictions_sum_and_all_document_scope(self):
        cycle = [data(161, "1"), data(730, "2"), data(731, "00"), data(731, "02"),
                 data(732, "03"), data(732, "13(5,10)"), data(131, "5")]
        transport = ScriptedTransport([cycle, [data(161, "2")]])
        stored = {}
        def store(payload, context):
            stored[payload["id"]] = payload["parameters"]
            return True
        client, journal = await self.client(transport, hooks=FieldHooks(store_finalization_payload=store))
        first = await client.transact(SALE)
        await client.append_payment(SALE)
        methods = [PaymentMethod("00", "10,00"), PaymentMethod("02", "2,34")]
        with self.assertRaises(SitefError):
            await client.finalize_with_payments(True, first.payment_id, methods, "12,35")
        await client.finalize_with_payments(True, first.payment_id, methods, "12,34")
        extra = transport.calls[-1][1]["additionalParameters"]
        self.assertEqual(extra, "{NumeroPagamentoNFPAG=1}{NFPAG=00:1000;02:234:03:5;}")
        self.assertNotIn("13:", extra)
        serialized = json.dumps(journal.record)
        self.assertNotIn("{NFPAG=", serialized)
        self.assertIn("finalizationPayload", serialized)
        self.assertEqual([p["state"] for p in journal.record["payments"]], ["finalized-local", "finalized-local"])
        await client.close()

    async def test_network_requires_resolver_and_known_function_policy_is_strict(self):
        transport = ScriptedTransport([[data(161, "1"), data(131, "5"), data(158, "6")]])
        client, _ = await self.client(transport)
        for value, policy in [(SALE, False), (replace(SALE, function_id=111), True)]:
            with self.assertRaises(SitefError) as caught:
                await client.transact(value, requires_finalization=policy)
            self.assertEqual(caught.exception.code, "FINALIZATION_POLICY_MISMATCH")
        await client.transact(SALE)
        with self.assertRaises(SitefError):
            await client.finalize_network(False, "5")
        client._hooks.resolve_confirmation_network = lambda _: "5"
        await client.finalize_network(False, "5")
        self.assertEqual(transport.calls[-1][1]["additionalParameters"], "{RedeConfirmacao=5}")
        await client.close()

    async def test_partial_dates_structures_and_menu_confirmation_are_typed_without_guesses(self):
        processor = FieldProcessor(0)
        self.assertEqual((await processor.process(request(2469, "1229"))).typed_value,
                         {"month": 12, "yearWithinCentury": 29})
        self.assertEqual((await processor.process(request(2468, "2902"))).typed_value, {"day": 29, "month": 2})
        with self.assertRaises(SitefError):
            await processor.process(request(2468, "3102"))
        mode = await processor.process(request(100, "0302"))
        self.assertEqual(mode.typed_value, {"group": "03", "variant": "02"})
        cheque = await processor.process(request(517, "0" + "1" * 29))
        self.assertEqual(cheque.typed_value["origin"], "manual-first-line")
        await processor.process(request(2355, "Mc"))
        menu = await processor.process(request(999, "1:First;2:Second", 30))
        with self.assertRaises(SitefError) as caught:
            processor.validate_response(menu, UiResponse("1"))
        self.assertEqual(caught.exception.code, "MENU_CONFIRMATION_REQUIRED")
        processor.validate_response(menu, UiResponse("1", confirmed=True))

    async def test_system_data_carnet_fallback_and_pbm_handoffs_are_required(self):
        seen = []
        hooks = FieldHooks(resolve_application_data=lambda event, ctx: seen.append(event.field_type) or "1234",
                           accept_fallback=lambda event, ctx: seen.append(event.field_type) or True)
        transport = ScriptedTransport([[data(999, "application data", 29)]])
        client, _ = await self.client(transport, hooks=hooks)
        await client.transact(replace(SALE, function_id=16))
        self.assertEqual(seen, [999, 2361])
        continuation = [p for m, p in transport.calls if m == "continue"]
        self.assertEqual(continuation[-1]["buffer"], "1234")
        await client.finalize(False)
        await client.close()
        transport = ScriptedTransport([[data(4100, "CUSTOM-DIAGNOSTIC")]])
        client, _ = await self.client(transport)
        with self.assertRaises(SitefError):
            await client.transact(SALE)
        self.assertEqual(transport.calls[-1][1]["continueFlag"], -1)
        await client.close()

    async def test_receipt_recovery_hash_failure_blocks_confirm_but_not_void(self):
        body = "EXTERNAL RECEIPT ONLY"
        transport = ScriptedTransport([[data(121, body)]])
        client, journal = await self.client(transport, hooks=FieldHooks(store_receipt=lambda *args: True))
        result = await client.transact(SALE)
        # Closing an in-doubt session models a restart without a new bank call.
        client._state = "uncertain"
        await client.close()
        recovered_transport = ScriptedTransport()
        recovered, _ = await self.client(recovered_transport, journal=journal,
            hooks=FieldHooks(load_receipt=lambda *args: {"body": body + "CHANGED"}, print_receipt=lambda *args: True))
        with self.assertRaises(SitefError) as caught:
            await recovered.finalize_recovery(True, result.transaction_id)
        self.assertEqual(caught.exception.code, "RECEIPT_HASH_MISMATCH")
        self.assertFalse(any(method == "finish" for method, _ in recovered_transport.calls))
        await recovered.finalize_recovery(False, result.transaction_id)
        self.assertEqual(recovered_transport.calls[-1][1]["confirm"], 0)
        await recovered.close()

    async def test_recovered_print_is_persisted_and_selective_plan_keeps_sibling_pending(self):
        body = "receipt"
        transport = ScriptedTransport([[data(161, "1"), data(121, body)], [data(161, "2")]], fail_finish=1)
        client, journal = await self.client(transport, hooks=FieldHooks(store_receipt=lambda *args: True,
            print_receipt=lambda *args: True))
        first = await client.transact(SALE)
        second = await client.append_payment(SALE)
        with self.assertRaises(SitefError):
            await client.finalize_payment(True, first.payment_id)
        self.assertTrue(journal.record["payments"][0]["receipts"][0]["printed"])
        self.assertNotIn("decision", journal.record["payments"][1])
        await client.close()
        recovered_transport = ScriptedTransport()
        recovered, _ = await self.client(recovered_transport, journal=journal)
        await recovered.finalize_recovery(True, first.transaction_id)
        finishes = [p for m, p in recovered_transport.calls if m == "finish"]
        self.assertEqual(len(finishes), 1)
        self.assertEqual(finishes[0]["additionalParameters"], "{NumeroPagamentoCupom=1}")
        self.assertEqual(recovered._pending()[0]["id"], second.payment_id)
        await recovered.finalize(False)
        await recovered.close()

    async def test_nf_pag_external_payload_recovery_and_legacy_migration_preserve_privacy(self):
        store = {}
        def stored(payload, context):
            store[payload["id"]] = payload["parameters"]
            return True
        hooks = FieldHooks(store_finalization_payload=stored)
        cycle = [data(161, "1"), data(730, "0"), data(731, "01"), data(732, "01"), data(732, "02")]
        transport = ScriptedTransport([cycle], fail_finish=1)
        client, journal = await self.client(transport, hooks=hooks)
        first = await client.transact(SALE)
        cheque = "12345678901234567890123456789"
        with self.assertRaises(SitefError):
            await client.finalize_with_payments(True, first.payment_id,
                [PaymentMethod("01", "12,34", {"01": "1", "02": cheque})], "12,34")
        self.assertNotIn(cheque, json.dumps(journal.record))
        self.assertNotIn("{NFPAG=", json.dumps(journal.record))
        self.assertEqual(journal.record["payments"][0]["finalizationParameters"], "{NumeroPagamentoNFPAG=1}")
        await client.close()
        recovered_transport = ScriptedTransport()
        recovered, _ = await self.client(recovered_transport, journal=journal,
            hooks=FieldHooks(load_finalization_payload=lambda meta, ctx: store[meta["id"]] + "tamper"))
        with self.assertRaises(SitefError) as caught:
            await recovered.finalize_recovery(True, first.transaction_id)
        self.assertEqual(caught.exception.code, "FINALIZATION_PAYLOAD_HASH_MISMATCH")
        self.assertEqual(recovered.state, "uncertain")
        self.assertFalse(any(m == "finish" for m, _ in recovered_transport.calls))
        async def restore_without_reentrancy(meta, context):
            with self.assertRaises(SitefError) as busy:
                await recovered.finalize_recovery(True, first.transaction_id)
            self.assertEqual(busy.exception.code, "SESSION_BUSY")
            return store[meta["id"]]
        recovered._hooks.load_finalization_payload = restore_without_reentrancy
        await recovered.finalize_recovery(True, first.transaction_id)
        self.assertIn(cheque, recovered_transport.calls[-1][1]["additionalParameters"])
        await recovered.close()
        legacy = MemoryJournal()
        legacy.record = {"version": 1, "id": "old", "storeId": CONFIG.store_id, "terminalId": CONFIG.terminal_id,
            "functionId": 0, "entryPoint": "start", "fiscalDocument": "123", "fiscalDate": "20261030",
            "fiscalTime": "120000", "state": "finalizing", "updatedAt": "2026-10-30T12:00:00Z", "decision": 1,
            "finalizationParameters": "{NumeroPagamentoNFPAG=1}{NFPAG=01:1234:01:1-02:" + cheque + ";}"}
        original = copy.deepcopy(legacy.record)
        with self.assertRaises(SitefError) as caught:
            await self.client(ScriptedTransport(), journal=legacy)
        self.assertEqual(caught.exception.code, "LEGACY_PAYLOAD_MIGRATION_REQUIRED")
        self.assertEqual(legacy.record, original)
        migrated, _ = await self.client(ScriptedTransport(), journal=legacy,
            hooks=FieldHooks(store_finalization_payload=stored, load_finalization_payload=lambda meta, _: store[meta["id"]],
                             attest_legacy_obligations=lambda *args: True))
        self.assertEqual(legacy.record["version"], 2)
        self.assertNotIn(cheque, json.dumps(legacy.record))
        await migrated.finalize_recovery(True, "old")
        await migrated.close()

    async def test_unknown_policy_and_malformed_pending_query_fail_without_journal_changes(self):
        client, journal = await self.client(ScriptedTransport([[data(210, "1"), data(161, "1")]]))
        initial_calls = len(client._transport.calls)
        with self.assertRaises(SitefError) as caught:
            await client.transact(replace(SALE, function_id=987654))
        self.assertEqual(caught.exception.code, "FINALIZATION_POLICY_REQUIRED")
        self.assertEqual(len(client._transport.calls), initial_calls)
        with self.assertRaises(SitefError) as caught:
            await client.query_pending_transactions()
        self.assertEqual(caught.exception.code, "INVALID_PENDING_QUERY")
        self.assertIsNone(journal.record)
        await client.close()

    async def test_recovered_receipt_print_and_paper_signature_precede_recovery_finish(self):
        events = []
        hooks = FieldHooks(store_receipt=lambda *args: True, capture_signature=lambda *args: True)
        original_transport = ScriptedTransport([[data(5074, "paper"), data(121, "saved receipt")]])
        client, journal = await self.client(original_transport, hooks=hooks)
        result = await client.transact(SALE)
        client._state = "uncertain"
        await client.close()
        def load(meta, payment):
            events.append("load")
            return {"body": "saved receipt"}
        recovered_transport = ScriptedTransport(log=events)
        recovered, _ = await self.client(recovered_transport, journal=journal, hooks=FieldHooks(
            load_receipt=load, print_receipt=lambda *args: events.append("print") or True,
            capture_signature=lambda *args: events.append("signature") or True))
        await recovered.finalize_recovery(True, result.transaction_id)
        self.assertEqual(events, ["load", "print", "signature", "finish"])
        self.assertTrue(journal.record["payments"][0]["receipts"][0]["printed"])
        self.assertTrue(journal.record["payments"][0]["signatureCaptured"])
        await recovered.close()

    async def test_product_pix_structures_validate_boundaries_without_guessing_quantity_grammar(self):
        processor = FieldProcessor(0, FieldHooks(resolve_application_data=lambda *args: True))
        value = "Id=123,d=Item,Q=1.5,VU=1234,T=S,TQ=3<#>"
        product = await processor.process(request(1323, value))
        self.assertEqual(product.typed_value[0]["Q"], "1.5")
        self.assertEqual(product.typed_value[0]["unitPrice"], Decimal("12.34"))
        with self.assertRaises(SitefError):
            await processor.process(request(1323, value + value))  # duplicate code
        with self.assertRaises(SitefError):
            await processor.process(request(1323, value.replace("Item", "X" * 31)))
        pix = await processor.process(request(4156, "name<{|}>value<{#}>other<{|}>data"))
        self.assertEqual(pix.typed_value, [{"name": "name", "value": "value"}, {"name": "other", "value": "data"}])
        with self.assertRaises(SitefError):
            await processor.process(request(4156, "name<{|}>" + "X" * 201))

    async def test_pending_query_in_uncertain_state_preserves_plan_and_supports_cooperative_cancel(self):
        transport = ScriptedTransport([[data(161, "1")]], fail_finish=1)
        client, journal = await self.client(transport)
        result = await client.transact(SALE)
        with self.assertRaises(SitefError):
            await client.finalize_payment(False, result.payment_id)
        snapshot = copy.deepcopy(journal.record)
        transport.cycles.append([data(210, "0")])
        self.assertEqual(await client.query_pending_transactions(), ())
        self.assertEqual(journal.record, snapshot)
        self.assertEqual(client.state, "uncertain")
        entered = asyncio.Event()
        async def wait_for_cancel(event, context):
            entered.set()
            await context.cancel_event.wait()
            return UiResponse(continue_flag=-1)
        client._interaction = wait_for_cancel
        transport.cycles.append([data(999, "waiting", 30)])
        query = asyncio.create_task(client.query_pending_transactions())
        await asyncio.wait_for(entered.wait(), 1)
        client.cancel()
        with self.assertRaises(SitefError) as caught:
            await asyncio.wait_for(query, 1)
        self.assertEqual(caught.exception.native_status, -2)
        self.assertEqual(journal.record, snapshot)
        await client.finalize_recovery(False, result.transaction_id)
        await client.close()

    async def test_v1_recovery_requires_explicit_attestation_to_confirm_unknown_obligations(self):
        journal = MemoryJournal()
        journal.record = {"version": 1, "id": "legacy-payment", "storeId": CONFIG.store_id, "terminalId": CONFIG.terminal_id,
            "functionId": 0, "entryPoint": "start", "fiscalDocument": "123", "fiscalDate": "20261030",
            "fiscalTime": "120000", "state": "awaiting-finalization", "updatedAt": "2026-10-30T12:00:00Z"}
        transport = ScriptedTransport()
        client, _ = await self.client(transport, journal=journal)
        self.assertTrue(client.recovery["payments"][0]["legacyReceiptStateUnknown"])
        with self.assertRaises(SitefError) as caught:
            await client.finalize_recovery(True, "legacy-payment")
        self.assertEqual(caught.exception.code, "LEGACY_OBLIGATIONS_ATTESTATION_REQUIRED")
        self.assertFalse(any(m == "finish" for m, _ in transport.calls))
        client._hooks.attest_legacy_obligations = lambda payment, context: True
        await client.finalize_recovery(True, "legacy-payment")
        self.assertFalse(journal.record["payments"][0]["legacyReceiptStateUnknown"])
        await client.close()

    async def test_pricing_is_scoped_by_medicine_and_known_violation_cannot_be_overridden(self):
        processor = FieldProcessor(0, FieldHooks(validate_transaction=lambda *args: True),
                                   monetary_formats={1043: "minor-units", 1044: "minor-units"})
        await processor.process(request(1011, "1"))
        await processor.process(request(1042, "1"))
        await processor.process(request(1043, "1000"))
        collect = await processor.process(request(1044, "actual", 30))
        processor.observe_response(collect, UiResponse("900"))
        await processor.complete()
        self.assertEqual(processor.snapshot()["productConstraints"][0]["state"], "satisfied")
        self.assertEqual(processor.snapshot()["fields"][-1]["source"], "collection")
        await processor.process(request(1011, "2"))
        await processor.process(request(1042, "0"))
        await processor.process(request(1043, "1000"))
        await processor.process(request(1044, "900"))
        with self.assertRaises(SitefError) as caught:
            await processor.complete()
        self.assertEqual(caught.exception.code, "PRODUCT_CONSTRAINT_VIOLATED")
        assessments = processor.snapshot()["productConstraints"]
        self.assertEqual([item["scope"]["medicineIndex"] for item in assessments], ["1", "2"])
        self.assertEqual([item["state"] for item in assessments], ["satisfied", "violated"])

    async def test_unresolved_prices_and_installment_limits_require_business_validation(self):
        transport = ScriptedTransport([[data(1042, "0"), data(1043, "1000"), data(1044, "1000")], [data(171, "2")]])
        client, journal = await self.client(transport)
        with self.assertRaises(SitefError) as caught:
            await client.transact(SALE)
        self.assertEqual(caught.exception.native_status, 0)
        self.assertEqual(caught.exception.result.context["productConstraints"][0]["state"], "unresolved")
        self.assertFalse(journal.record["payments"][0]["confirmationAllowed"])
        await client.finalize(False)
        assessments = []
        client._hooks.validate_transaction = lambda event, ctx: assessments.append(ctx) or True
        await client.transact(SALE)
        self.assertEqual(assessments[0]["fields"][0]["fieldType"], 171)
        await client.finalize(False)
        await client.close()

    async def test_fleet_count_and_code_length_are_checked_when_unambiguous(self):
        hooks = FieldHooks(resolve_application_data=lambda *args: True)
        processor = FieldProcessor(0, hooks)
        await processor.process(request(1122, "1"))
        await processor.process(request(1123, "3"))
        await processor.process(request(1323, "Id=123,d=Item,Q=1,VU=1234,T=S,TQ=3<#>"))
        await processor.complete()
        self.assertEqual([p["state"] for p in processor.product_constraints], ["satisfied", "satisfied"])
        await processor.process(request(1323, "Id=123,d=Item,Q=1,VU=1234,T=S,TQ=3<#>Id=456,d=Other,Q=1,VU=1234,T=S,TQ=3<#>"))
        with self.assertRaises(SitefError) as caught:
            await processor.complete()
        self.assertEqual(caught.exception.code, "PRODUCT_CONSTRAINT_VIOLATED")
        shorter = FieldProcessor(0, hooks)
        await shorter.process(request(1123, "5"))
        await shorter.process(request(1323, "Id=123,d=Item,Q=1,VU=1234,T=S,TQ=3<#>"))
        with self.assertRaises(SitefError):
            await shorter.complete()
        self.assertEqual(shorter.product_constraints[0]["state"], "unresolved")

    async def test_precollected_data_requires_confirmation_and_base64_cannot_change_validated_text(self):
        client, _ = await self.client(ScriptedTransport())
        with self.assertRaises(SitefError) as caught:
            client._validate_response(UiResponse("1", continue_flag=10000))
        self.assertEqual(caught.exception.code, "PRECOLLECTED_CONFIRMATION_REQUIRED")
        client._validate_response(UiResponse("1", continue_flag=10000, confirmed=True))
        encoded = base64.b64encode("Ação".encode("cp1252")).decode()
        with self.assertRaises(SitefError) as caught:
            client._validate_response(UiResponse("different", buffer_base64=encoded))
        self.assertEqual(caught.exception.code, "RESPONSE_BYTES_MISMATCH")
        validated = client._validate_response(UiResponse("Ação", buffer_base64=encoded))
        self.assertEqual(validated.buffer, "Ação")
        await client.close()

    async def test_reader_context_and_banking_receipts_follow_entrypoint_not_nominal_function(self):
        reader = ScriptedTransport([[data(2053, "protected")]])
        client, journal = await self.client(reader, hooks=FieldHooks(resolve_field=lambda event, context: event.buffer))
        result = await client.transact(TransactionInput(16, "", "", "", "", ""),
                                       native_start=NativeStart("readPassword"), requires_finalization=False)
        self.assertEqual(result.context["context"], "secure-card-read")
        self.assertEqual(result.status, 0)  # No unrelated Carnê fallback obligation.
        self.assertFalse(result.requires_finalization)
        self.assertIsNone(journal.record)
        await client.close()
        log = []
        banking = ScriptedTransport([[data(121, "immediate receipt")]])
        hooks = FieldHooks(store_receipt=lambda *args: log.append("store") or True,
                           print_receipt=lambda *args: log.append("print") or True,
                           validate_transaction=lambda *args: True)
        client, journal = await self.client(banking, hooks=hooks)
        result = await client.transact(SALE, native_start=NativeStart("startBanking"))
        self.assertEqual(log, ["store", "print"])
        self.assertEqual(result.receipts[0].timing, "immediate")
        await client.finalize(False)
        await client.close()

    async def test_pending_query_native_failure_preserves_partial_fields_and_original_status(self):
        transport = ScriptedTransport([[data(210, "1"), data(160, "123"), {"status": -40}]])
        client, journal = await self.client(transport)
        with self.assertRaises(SitefError) as caught:
            await client.query_pending_transactions()
        self.assertEqual(caught.exception.code, "PENDING_QUERY_FAILED")
        self.assertEqual(caught.exception.native_status, -40)
        self.assertEqual([field.field_type for field in caught.exception.result.fields], [210, 160])
        self.assertIsNone(journal.record)
        await client.close()

    async def test_journal_failure_after_local_finish_keeps_the_partition_recoverable(self):
        class FailAfterLocalFinish(MemoryJournal):
            failed = False
            async def write(self, record):
                if not self.failed and any(p["state"] == "finalized-local" for p in record["payments"]):
                    self.failed = True
                    raise OSError("Synthetic durable-write failure after native void return")
                await super().write(record)
        journal = FailAfterLocalFinish()
        transport = ScriptedTransport([[data(161, "1")]])
        client, _ = await self.client(transport, journal=journal)
        result = await client.transact(SALE)
        with self.assertRaises(SitefError) as caught:
            await client.finalize(True)
        self.assertEqual(caught.exception.code, "FINALIZATION_UNCERTAIN")
        self.assertEqual(client.state, "uncertain")
        self.assertEqual(len(client._pending()), 1)
        self.assertEqual(journal.record["payments"][0]["state"], "uncertain")
        self.assertEqual(journal.record["payments"][0]["decision"], 1)
        await client.close()
        recovered_transport = ScriptedTransport()
        recovered, _ = await self.client(recovered_transport, journal=journal)
        await recovered.finalize_recovery(True, result.transaction_id)
        self.assertEqual(recovered_transport.finishes, 1)
        self.assertEqual(recovered.state, "idle")
        await recovered.close()
