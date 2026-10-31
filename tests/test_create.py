import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from sitef import FileJournal, SitefError, create_sitef
from test_client import CONFIG, answer
from test_transport import HERE


class FactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_uses_shared_terminal_journal_key_and_returns_ready_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            client = await create_sitef(
                bridge_executable=sys.executable, bridge_directory=HERE,
                library_path=directory / "synthetic-only.dll", state_directory=directory,
                configuration=CONFIG, interaction=answer, test_mode=True,
                bridge_args=[str(HERE / "fake_host.py")])
            try:
                self.assertEqual(client.state, "idle")
                key = hashlib.sha256(b"00000000\0AA123456").hexdigest()
                expected_path = directory / (key + ".journal.json")
                self.assertTrue(Path(str(expected_path) + ".lock").exists())
                with self.assertRaises(SitefError) as caught:
                    await FileJournal(expected_path).acquire()
                self.assertEqual(caught.exception.code, "TERMINAL_LOCKED")
            finally:
                await client.close()

    async def test_protocol_mismatch_closes_child_before_returning_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            marker = directory / "closed-marker.txt"
            with self.assertRaises(SitefError) as caught:
                await create_sitef(
                    bridge_executable=sys.executable, bridge_directory=HERE,
                    library_path=directory / "synthetic-only.dll", state_directory=directory,
                    configuration=CONFIG, interaction=answer, test_mode=True,
                    bridge_args=[str(HERE / "fake_host.py"), "bad-protocol", str(marker)])
            self.assertEqual(caught.exception.code, "PROTOCOL_MISMATCH")
            self.assertEqual(marker.read_text(), "closed")
            self.assertEqual(list(directory.glob("*.lock")), [])

    async def test_test_mode_requires_an_explicit_boolean_before_process_spawn(self):
        with self.assertRaises(SitefError) as caught:
            await create_sitef(
                bridge_executable=sys.executable, bridge_directory=HERE,
                library_path=HERE / "synthetic-only.dll", state_directory=HERE,
                configuration=CONFIG, interaction=answer, test_mode="true")
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
