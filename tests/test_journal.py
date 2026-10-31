import asyncio
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sitef import FileJournal, SitefError


def record():
    return {"version": 1, "id": "synthetic-id", "storeId": "00000000", "terminalId": "AA123456",
            "functionId": 0, "entryPoint": "start", "fiscalDocument": "123", "fiscalDate": "20261030",
            "fiscalTime": "120000", "state": "started", "updatedAt": "2026-10-30T15:00:00Z"}


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_atomic_record_has_no_payment_buffers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.journal.json"
            journal = FileJournal(path)
            await journal.acquire()
            second = FileJournal(path)
            with self.assertRaises(SitefError) as caught:
                await second.acquire()
            self.assertEqual(caught.exception.code, "TERMINAL_LOCKED")
            await journal.write(record())
            loaded = await journal.read()
            self.assertEqual(loaded["fiscalDocument"], "123")
            with self.assertRaises(SitefError):
                await journal.write({**record(), "buffer": "sensitive"})
            self.assertNotIn("sensitive", path.read_text())
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            await journal.release()
            await second.acquire()
            await second.release()

    async def test_failed_atomic_replace_preserves_previous_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.journal.json"
            journal = FileJournal(path)
            await journal.acquire()
            await journal.write(record())
            before = path.read_bytes()
            with patch("sitef.journal.os.replace", side_effect=PermissionError("synthetic disk failure")):
                with self.assertRaises(PermissionError):
                    await journal.write({**record(), "state": "finalizing", "decision": 1})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            await journal.release()

    async def test_crashed_process_lock_is_not_automatically_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.journal.json"
            code = "import asyncio,os,sys; from sitef import FileJournal; j=FileJournal(sys.argv[1]); asyncio.run(j.acquire()); os._exit(0)"
            child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(path),
                                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(await child.wait(), 0)
            with self.assertRaises(SitefError) as caught:
                await FileJournal(path).acquire()
            self.assertEqual(caught.exception.code, "TERMINAL_LOCKED")
            self.assertTrue(Path(str(path) + ".lock").exists())

    async def test_corrupt_journal_is_not_overwritten_by_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.journal.json"
            path.write_text('{"state":"started"}', encoding="utf-8")
            journal = FileJournal(path)
            await journal.acquire()
            with self.assertRaises(SitefError) as caught:
                await journal.read()
            self.assertEqual(caught.exception.code, "JOURNAL_INVALID")
            self.assertEqual(path.read_text(), '{"state":"started"}')
            await journal.release()

    async def test_changed_lock_owner_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.journal.json"
            journal = FileJournal(path)
            await journal.acquire()
            Path(str(path) + ".lock").write_text('{"token":"different-owner"}', encoding="utf-8")
            with self.assertRaises(SitefError) as caught:
                await journal.release()
            self.assertEqual(caught.exception.code, "JOURNAL_LOCK_CHANGED")
            self.assertTrue(Path(str(path) + ".lock").exists())
            os.close(journal._fd)
            journal._fd = None
