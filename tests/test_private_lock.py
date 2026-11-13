"""Real process/thread exclusion and bounded waiting, using temporary state."""
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from familiekilder.private_lock import private_source_lock, PrivateLockBusy


def _hold_gmail(root, connection):
    try:
        connection.send('waiting')
        with private_source_lock(root, 'gmail', wait=True, timeout=3):
            connection.send('acquired')
            connection.recv()
        connection.send('released')
    finally:
        connection.close()


class PrivateLockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_waiting_process_serializes_and_timeout_leaves_lock_usable(self):
        ctx = multiprocessing.get_context('spawn')
        parent, child = ctx.Pipe()
        process = ctx.Process(target=_hold_gmail, args=(self.root, child))
        try:
            with private_source_lock(self.root, 'gmail'):
                process.start()
                child.close()
                self.assertTrue(parent.poll(5))
                self.assertEqual(parent.recv(), 'waiting')
                self.assertFalse(parent.poll(.1))
            self.assertTrue(parent.poll(3))
            self.assertEqual(parent.recv(), 'acquired')
            with self.assertRaisesRegex(PrivateLockBusy, 'Tidsavbrudd') as error:
                with private_source_lock(self.root, 'gmail', wait=True, timeout=.03):
                    self.fail('Concurrent acquisition')
            self.assertTrue(error.exception.retryable)
            parent.send('release')
            self.assertTrue(parent.poll(3))
            self.assertEqual(parent.recv(), 'released')
            process.join(3)
            self.assertEqual(process.exitcode, 0)
            with private_source_lock(self.root, 'gmail', wait=True, timeout=.03):
                pass
        finally:
            if process.is_alive():
                process.terminate()
                process.join(3)
            parent.close()
            child.close()

    def test_reentrant_lock_and_isolated_sources_preserve_fail_fast_default(self):
        def acquire(root, source):
            with private_source_lock(root, source):
                return True

        with private_source_lock(self.root, 'gmail', wait=True, timeout=.03):
            with private_source_lock(self.root, 'gmail', wait=True, timeout=0):
                pass
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(acquire, self.root, 'spond').result(timeout=3))
                (self.root/'other').mkdir()
                self.assertTrue(pool.submit(acquire, self.root/'other', 'gmail').result(timeout=3))
                with self.assertRaises(PrivateLockBusy):
                    pool.submit(acquire, self.root, 'gmail').result(timeout=3)
        with private_source_lock(self.root, 'gmail'):
            pass


if __name__ == '__main__':
    unittest.main()
