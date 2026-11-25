"""Save only the visible review-media rectangles when the review screen remains unchanged."""
import hashlib
import io
import json
import logging
import subprocess
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageStat

from gallery_capture import read_gallery_screenshot

logger = logging.getLogger(__name__)
MAX_TILES = 300


class ReviewThumbnails:
    def __init__(self, folder, run_id):
        self.path = Path(folder) / f'hotel-review-thumbnails-{run_id}.zip'
        self.run_id = run_id
        self.staged = self.path.with_suffix('.part')
        self.items = []
        self.seen = set()
        self.skipped_screens = 0
        self.limit_reached = False
        self.archive = None

    def capture(self, adb, xml, eligible, screen, stop, viewport):
        if not eligible or stop.is_set() or self.limit_reached:
            return
        try:
            adb.ensure_foreground()
            payload = read_gallery_screenshot(adb)
            adb.ensure_foreground()
            if stop.is_set():
                return
            # Check the entire snapshot again, including card identity and tile positions.
            if adb.snapshot() != xml:
                self.skipped_screens += 1
                return False
            with Image.open(io.BytesIO(payload)) as frame:
                if frame.format != 'PNG' or frame.width * frame.height > 20_000_000:
                    raise ValueError('截图格式或尺寸不受支持')
                if list(viewport) != [0, 0, frame.width, frame.height]:
                    raise ValueError('截图与界面坐标范围不一致')
                for key, order, boxes in eligible:
                    for box in boxes:
                        if stop.is_set():
                            return
                        x, y, right, bottom = box
                        if not (0 <= x < right <= frame.width and 0 <= y < bottom <= frame.height):
                            continue
                        crop = frame.crop(box).convert('RGB')
                        # Reject flat placeholders and use a fresh image so full-screen metadata is not retained.
                        if max(ImageStat.Stat(crop.resize((32, 32))).stddev) < 4:
                            continue
                        buffer = io.BytesIO()
                        Image.frombytes('RGB', crop.size, crop.tobytes()).save(buffer, format='PNG')
                        blob = buffer.getvalue()
                        digest = hashlib.sha256(blob).hexdigest()
                        if (key, digest) in self.seen:
                            continue
                        if len(self.items) == MAX_TILES:
                            self.limit_reached = True
                            return
                        if self.archive is None:
                            self.archive = ZipFile(self.staged, 'x')
                        name = f'thumbnails/{order:05d}-{len(self.items) + 1:04d}.png'
                        self.archive.writestr(name, blob)
                        self.items.append({'file': name, 'sample_order': order, 'review_key': key,
                                           'screen': screen, 'crop_bounds': list(box), 'sha256': digest})
                        self.seen.add((key, digest))
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired, Image.DecompressionBombError):
            self.skipped_screens += 1
            logger.warning('点评缩略图本屏未全部保存', exc_info=True)

    def finish(self):
        ready = False
        if self.archive is not None:
            try:
                with self.archive as archive:
                    manifest = {'run_id': self.run_id, 'source': 'hotel_review_display_crops',
                                'sample_csv': f'hotel-sample-{self.run_id}.csv',
                                'identity_verification': 'operator_declared_unverified',
                                'complete': False, 'original_files': False, 'thumbnails': self.items}
                    archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                self.staged.replace(self.path)
                ready = True
            except OSError:
                logger.warning('点评缩略图压缩包写入失败', exc_info=True)
            finally:
                self.staged.unlink(missing_ok=True)
        return {'archive': self.path.name if ready else None,
                'saved_count': len(self.items) if ready else 0, 'skipped_screens': self.skipped_screens,
                'limit_reached': self.limit_reached, 'original_files': False, 'complete': False}
