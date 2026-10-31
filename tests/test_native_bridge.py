"""Optional real Rust IPC + original C ABI fixture; never a vendor DLL."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from sitef import BridgeTransport, Configuration, FileJournal, SitefClient, TransactionInput, UiResponse, FieldHooks

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("SITEF_TEST_BRIDGE") and
                     ((sys.platform == "win32" and os.environ.get("SITEF_TEST_FIXTURE_LIBRARY") and
                       os.environ.get("SITEF_TEST_OWNED_FIXTURE") == "1") or
                      (sys.platform != "win32" and shutil.which("cc"))),
                     "Use cc/Linux ou SITEF_TEST_FIXTURE_LIBRARY + SITEF_TEST_OWNED_FIXTURE=1, sempre com fixture própria.")
class NativeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rust_process_and_original_c_fixture_exchange_actual_cp1252_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = Path(os.environ.get("SITEF_TEST_FIXTURE_SOURCE", str(ROOT / "native/tests/fixture.c")))
            self.assertTrue(source.is_file(), "Fonte de fixture própria necessária; nunca use biblioteca do fornecedor neste teste.")
            if sys.platform == "win32":
                library = Path(os.environ["SITEF_TEST_FIXTURE_LIBRARY"]).resolve()
                self.assertTrue(library.is_file(), "DLL de fixture própria precisa existir fora do pacote.")
            else:
                library = directory / "original-contract-fixture.so"
                subprocess.run([shutil.which("cc"), "-shared", "-fPIC", str(source), "-o", str(library)], check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            transport = await BridgeTransport.open(Path(os.environ["SITEF_TEST_BRIDGE"]).resolve(), cwd=directory, test_mode=True)
            await transport.load(library)
            async def answer(request, context):
                return UiResponse("1234")
            client = await SitefClient.connect(
                configuration=Configuration("127.0.0.1", "00000000", "AA123456", "[X=1]"),
                transport=transport, journal=FileJournal(directory / "terminal.journal.json"), interaction=answer,
                hooks=FieldHooks(store_receipt=lambda *_: True, print_receipt=lambda *_: True,
                                 validate_transaction=lambda *_: True))
            try:
                value = TransactionInput(0, "12,34", "123", "20261030", "120000", "test", "{X=1}")
                result = await client.transact(value, requires_finalization=True)
                self.assertEqual(result.status, 0)
                self.assertEqual(result.fields[0].field_type, 121)
                self.assertEqual(result.fields[0].buffer, "Ação\n")
                await client.finalize(False)
                sentinel = TransactionInput(999, "12,34", "123", "20261030", "120000", "test", "{X=1}")
                result = await client.transact(sentinel, requires_finalization=False)
                self.assertEqual(result.status, -98765)  # Artificial fixture sentinel, not the real function 999.
            finally:
                await client.close()
