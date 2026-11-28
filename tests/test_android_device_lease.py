import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

from local_locks import FileLockBusy, reserve_android_device


BUSY_EXIT_CODE = 47
WORKER = """
import sys
from pathlib import Path
from time import sleep
from local_locks import FileLockBusy, reserve_android_device

try:
    with reserve_android_device(sys.argv[1]):
        if len(sys.argv) > 2:
            Path(sys.argv[2]).touch()
            sleep(180)
except FileLockBusy:
    sys.exit(47)
"""


class AndroidDeviceLeaseTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.repo = Path(__file__).resolve().parent.parent

    def probe(self, serial):
        return subprocess.run([sys.executable, '-c', WORKER, serial], cwd=self.repo,
                              capture_output=True, timeout=10)

    def test_same_serial_excludes_a_second_process_without_blocking_other_serials(self):
        target = '192.0.2.18:5555'
        with reserve_android_device(target):
            same = self.probe(target)
            other = self.probe('emulator-5556')
            self.assertEqual(same.returncode, BUSY_EXIT_CODE, same.stderr)
            self.assertEqual(other.returncode, 0, other.stderr)
        released = self.probe(target)
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_abrupt_exit_releases_device_lock_without_deleting_the_file(self):
        target = 'emulator-5556'
        ready = self.state / 'worker-ready'
        command = [sys.executable, '-c', WORKER, target, str(ready)]
        worker = subprocess.Popen(command, cwd=self.repo, stderr=subprocess.PIPE)
        try:
            deadline = monotonic() + 10
            while not ready.is_file() and worker.poll() is None and monotonic() < deadline:
                sleep(0.02)
            self.assertTrue(ready.is_file(), '子进程未获取设备占用锁')
            with self.assertRaises(FileLockBusy), reserve_android_device(target):
                self.fail('另一进程持锁时不应进入设备任务')
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=10)

        paths = list(self.state.rglob('*.lock'))
        self.assertEqual(len(paths), 1)
        with reserve_android_device(target):
            self.assertEqual(list(self.state.rglob('*.lock')), paths)
