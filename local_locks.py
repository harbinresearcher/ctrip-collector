"""Cross-process file locks for local collector resources."""

import errno
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path


class FileLockBusy(RuntimeError):
    """Another process has acquired the lock."""


@contextmanager
def exclusive_file_lock(path):
    with Path(path).open('a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if (exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK)
                    or getattr(exc, 'winerror', None) == 33):
                raise FileLockBusy('此资源正在被另一个进程使用') from exc
            raise
        # Keep the shared filename stable. Closing the handle releases the lock.
        yield


@contextmanager
def reserve_android_device(serial):
    if not isinstance(serial, str) or not serial:
        raise ValueError('需要已连接设备的序列号')
    local_appdata = os.environ.get('LOCALAPPDATA')
    state = Path(local_appdata) if local_appdata else Path.home() / '.local' / 'state'
    folder = state / 'CtripCollector' / 'android-device-locks'
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(serial.encode('utf-8')).hexdigest() + '.lock'
    with exclusive_file_lock(folder / name):
        yield
