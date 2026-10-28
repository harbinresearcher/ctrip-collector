import json
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace

from PIL import Image

from gallery_capture import save_gallery_frame, capture_current_gallery_frame
from test_gallery_match import picture

logger = logging.getLogger(__name__)


def viewer(index=4, package='ctrip.android.view'):
    return f'<hierarchy><node package="{package}" text="AI优选 30"/><node package="{package}" text="{index}/30"/></hierarchy>'


class GalleryCaptureTests(unittest.TestCase):
    def test_ambiguous_versions_are_retained_only_as_review_candidates(self):
        # /* ===== 步骤1：保留歧义版本；数据源：同画面两个编码；操作：1) 不指定唯一图片 2) 隔离待核对文件 ===== */
        logger.info('开始图集多版本保存测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photo = picture()
            first, second = root / 'first.png', root / 'second.png'
            photo.save(first)
            variant = photo.copy()
            variant.putpixel((50, 50), (1, 2, 3))
            variant.save(second)
            screen = Image.new('RGB', (240, 500), 'gray')
            screen.paste(photo, (0, 120))
            # 1.1 歧义状态不改变，不将两个文件都计作两张图集图片。
            for _ in range(2):
                result = save_gallery_frame(screen, [first, second], viewer(), viewer(), (0, 40, 240, 400), root / 'out', 'hotel-427100', 'AI优选')
                self.assertEqual(result['status'], 'ambiguous')
                self.assertIsNone(result['match']['candidate'])
                self.assertFalse(result['complete'])
                self.assertEqual(len(result['review_candidates']), 2)
                self.assertFalse(list((root / 'out').glob('*.png')))
                self.assertEqual({(root / 'out' / row['file']).read_bytes() for row in result['review_candidates']}, {first.read_bytes(), second.read_bytes()})
            self.assertEqual(len(list((root / 'out' / 'review').glob('*.png'))), 2)
            self.assertEqual(len(list((root / 'out' / 'review').glob('*.json'))), 1)
        logger.info('图集多版本保存测试完成')

    def test_device_frame_pipeline_and_late_cancel(self):
        # /* ===== 步骤1：验证设备串接；数据源：模拟ADB和真图片字节；操作：1) 截图前后校验 2) 取消不保存 ===== */
        logger.info('开始单页设备串接验证')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photo = picture()
            source = root / 'candidate.png'
            photo.save(source)
            screen = Image.new('RGB', (240, 500), 'gray')
            screen.paste(photo, (0, 120))
            buffer = io.BytesIO()
            screen.save(buffer, format='PNG')
            adb = Mock(executable='adb', serial='device')
            adb.snapshot.return_value = viewer()
            stop = Event()
            # 1.1 设备方法须贯穿候选读取、截图、前后页码核对和保存。
            with patch('gallery_capture.changed_candidates', return_value=nullcontext([source])), patch('gallery_capture.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=buffer.getvalue())):
                result = capture_current_gallery_frame(adb, {}, (0, 40, 240, 400), root / 'out', 'hotel-427100', 'AI优选', stop)
                self.assertEqual(result['status'], 'saved')
                self.assertEqual(adb.snapshot.call_count, 2)
            # 1.2 同一会话候选可用于当前对象，切换对象必须重新建立会话。
            session = Mock(adb=adb, baseline={}, scope=None)
            session.refresh.return_value = [source]
            with patch('gallery_capture.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=buffer.getvalue())):
                result = capture_current_gallery_frame(adb, {}, (0, 40, 240, 400), root / 'session', 'hotel-427100', 'AI优选', stop, candidate_session=session)
                self.assertEqual(result['status'], 'saved')
                self.assertEqual(session.scope, ('hotel-427100', 'AI优选'))
                with self.assertRaises(ValueError):
                    capture_current_gallery_frame(adb, {}, (0, 40, 240, 400), root / 'wrong', 'hotel-other', 'AI优选', stop, candidate_session=session)
                self.assertFalse((root / 'wrong').exists())
            # 1.2 截图后取消也须阻止文件保存。
            snapshots = iter((False, True))
            def cancel_after_snapshot():
                if next(snapshots):
                    stop.set()
                return viewer()
            adb.snapshot.side_effect = cancel_after_snapshot
            with patch('gallery_capture.changed_candidates', return_value=nullcontext([source])), patch('gallery_capture.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=buffer.getvalue())):
                with self.assertRaises(InterruptedError):
                    capture_current_gallery_frame(adb, {}, (0, 40, 240, 400), root / 'cancelled', 'hotel-427100', 'AI优选', stop)
                self.assertFalse((root / 'cancelled').exists())
        logger.info('单页设备串接验证完成')

    def test_interrupted_image_write_leaves_no_success_record(self):
        # /* ===== 步骤1：验证中断；数据源：文件替换失败；操作：1) 注入保存故障 2) 核对没有完成清单 ===== */
        logger.info('开始保存中断验证')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photo = picture()
            source = root / 'candidate.png'
            photo.save(source)
            screen = Image.new('RGB', (240, 500), 'gray')
            screen.paste(photo, (0, 120))
            # 1.1 图片未完成替换时，不得写入关联成功记录。
            with patch.object(Path, 'replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    save_gallery_frame(screen, [source], viewer(), viewer(), (0, 40, 240, 400), root / 'out', 'hotel-427100', 'AI优选')
            self.assertFalse(list((root / 'out').glob('*.json')))
        logger.info('保存中断验证完成')

    def test_saves_original_bytes_and_provenance(self):
        # /* ===== 步骤1：验证保存；数据源：合成主图；操作：1) 保存原字节 2) 核对来源与重复执行 ===== */
        logger.info('开始图集保存验证')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            photo = picture()
            source = root / 'candidate.png'
            photo.save(source)
            screen = Image.new('RGB', (240, 500), 'gray')
            screen.paste(photo, (0, 120))
            # 1.1 重复输入不得制造重复图片，声明的对象身份不能变成已核验。
            for _ in range(2):
                result = save_gallery_frame(screen, [source, source], viewer(), viewer(), (0, 40, 240, 400), root / 'out', 'hotel-427100', 'AI优选')
                self.assertEqual(result['status'], 'saved')
                self.assertEqual((root / 'out' / result['file']).read_bytes(), source.read_bytes())
                self.assertFalse(result['original_verified'])
                self.assertEqual(result['category_verification'], 'operator_confirmed')
                self.assertEqual(result['identity_verification'], 'operator_declared')
            self.assertEqual(len(list((root / 'out').glob('*.png'))), 1)
            records = list((root / 'out').glob('*.json'))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text(encoding='utf-8'))['gallery_index'], 4)
        logger.info('图集保存验证完成')

    def test_rejects_changed_page_foreign_app_and_unconfirmed_category(self):
        # /* ===== 步骤2：验证页面边界；数据源：变化的页码与包名；操作：1) 拒绝串页 2) 拒绝未确认分类 ===== */
        logger.info('开始页面边界验证')
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'out'
            # 2.1 这些失败发生在读取图片前，不生成输出目录。
            for before, after, category in [(viewer(), viewer(5), 'AI优选'),
                                             (viewer(), viewer(package='other.app'), 'AI优选'),
                                             (viewer(), viewer(), '')]:
                with self.subTest(after=after, category=category):
                    with self.assertRaises(ValueError):
                        save_gallery_frame(Image.new('RGB', (240, 500)), [], before, after, (0, 40, 240, 400), out, 'hotel-427100', category)
            self.assertFalse(out.exists())
        logger.info('页面边界验证完成')

    def test_invalid_or_unmatched_candidates_are_not_exported(self):
        # /* ===== 步骤3：验证候选隔离；数据源：损坏文件和无关图片；操作：1) 忽略坏文件 2) 不导出未匹配候选 ===== */
        logger.info('开始候选隔离验证')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            invalid = root / 'invalid.bin'
            invalid.write_bytes(b'not an image')
            # 3.1 候选不足时只返回结果，不将缓存混入交付目录。
            result = save_gallery_frame(Image.new('RGB', (240, 500)), [invalid], viewer(), viewer(), (0, 40, 240, 400), root / 'out', 'hotel-427100', 'AI优选')
            self.assertEqual(result['status'], 'no_match')
            self.assertFalse((root / 'out').exists())
        logger.info('候选隔离验证完成')
