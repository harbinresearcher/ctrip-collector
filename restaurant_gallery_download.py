"""Copy an image created by Ctrip's public in-viewer Download button."""
import io
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image


PHOTO_DIRECTORY = '/sdcard/Pictures/Trip'
PHOTO_NAME = re.compile(r'image_name_[0-9]{10,17}\.(webp|jpe?g|png)')
EXTENSION_FORMATS = {'webp': 'WEBP', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG'}
MAX_PHOTO_BYTES = 32 * 1024 * 1024
MAX_PHOTO_PIXELS = 30_000_000
SAVE_POLLS = 20
SAVE_POLL_INTERVAL = 0.4


def _check_cancelled(stop):
    if stop.is_set():
        raise InterruptedError('点评大图下载已取消')


def _saved_names(device):
    listing = device.run('shell', f'if [ -d {PHOTO_DIRECTORY} ]; then ls -1 {PHOTO_DIRECTORY}; fi', timeout=5)
    return {line for line in listing.splitlines() if PHOTO_NAME.fullmatch(line)}


def _saved_size(device, remote):
    # The filename has already matched PHOTO_NAME. Check it is a regular file before copying it.
    command = f'if [ -f {remote} ] && [ ! -L {remote} ]; then stat -c %s {remote}; fi'
    size = device.run('shell', command, timeout=5).strip()
    if not size:
        return 0
    if not re.fullmatch(r'[0-9]{1,18}', size):
        raise ValueError('无法核验刚保存的点评图片大小')
    return int(size)


def _wait_for_new_photo(device, stop, baseline):
    previous = None
    seen = set()
    for _ in range(SAVE_POLLS):
        if stop.wait(SAVE_POLL_INTERVAL):
            raise InterruptedError('点评大图下载已取消')
        new = _saved_names(device) - baseline
        seen.update(new)
        if len(seen) > 1:
            raise ValueError('相册出现多张新保存的图片，无法核验当前页')
        if not new:
            previous = None
            continue
        name = next(iter(new))
        size = _saved_size(device, f'{PHOTO_DIRECTORY}/{name}')
        if size > MAX_PHOTO_BYTES:
            raise ValueError('新保存的点评图片文件过大')
        if size and previous == (name, size):
            return name, size
        previous = name, size
    raise ValueError('未在相册核验到新保存的完整点评图片')


def _validate_photo(payload, extension):
    expected_format = EXTENSION_FORMATS[extension]
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != expected_format:
                raise ValueError('点评图片格式与下载文件后缀不符')
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_PHOTO_PIXELS:
                raise ValueError('点评图片尺寸超出范围')
            image.verify()
        # Some Pillow formats only test their header in verify().
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('点评图片文件损坏或格式无法读取') from exc
    return width, height


def download_visible_photo(device, stop, download_box, verify_view):
    """Tap one verified Download button and return the saved bytes, suffix and decoded size."""
    _check_cancelled(stop)
    left, top, right, bottom = download_box
    if not (0 <= left < right and 0 <= top < bottom):
        raise ValueError('点评大图下载按钮位置无效')
    verify_view()
    # A previous save can finish while the viewer is being verified.
    baseline = _saved_names(device)
    _check_cancelled(stop)
    device.run('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2), timeout=5)

    name, size = _wait_for_new_photo(device, stop, baseline)
    remote = f'{PHOTO_DIRECTORY}/{name}'
    _check_cancelled(stop)
    with TemporaryDirectory(prefix='ctrip-review-photo-') as folder:
        local = Path(folder) / name
        device.run('pull', remote, str(local), timeout=25)
        _check_cancelled(stop)
        with local.open('rb') as file:
            payload = file.read(MAX_PHOTO_BYTES + 1)
    if len(payload) != size or _saved_names(device) - baseline != {name} or _saved_size(device, remote) != size:
        raise ValueError('复制期间新保存的点评图片发生变化')
    extension = PHOTO_NAME.fullmatch(name)[1]
    dimensions = _validate_photo(payload, extension)
    verify_view()
    _check_cancelled(stop)
    return payload, extension, dimensions
