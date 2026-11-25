import io
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch
from PIL import Image

from workbench_media_jobs import prepare_media_job

logger = logging.getLogger(__name__)


class MediaJobsTests(unittest.TestCase):
    def test_video_mode_forwards_restricted_source_and_cancellation(self):
        # /* ===== 步骤1：检查视频入口；数据源：工作台表单；操作：1) 限定评论文件 2) 传递停止事件 ===== */
        logger.info('开始工作台视频任务测试')
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'sight-136039.jsonl'
            path.write_text('{}\n', encoding='utf-8')
            stop, emit = Event(), Mock()
            with patch('workbench_media_jobs.download_videos', return_value={'reason':'source_exhausted','complete':False}) as downloader:
                job = prepare_media_job({'mode':'video_media','source_file':path.name,'media_limit':2}, folder)
                self.assertEqual(job(stop, emit)['reason'], 'source_exhausted')
                downloader.assert_called_once_with(path.resolve(), Path(folder).resolve(), limit=2, stop=stop)
            with self.assertRaises(ValueError):
                prepare_media_job({'mode':'video_media','source_file':'../sight-136039.jsonl'}, folder)
        logger.info('工作台视频任务测试完成')

    def test_adapter_saves_image_and_reuses_it(self):
        # /* ===== 步骤1：验证真实落盘链路；数据源：模拟图片响应；操作：1) 保存 2) 复用 ===== */
        logger.info('开始图片落盘与续跑测试')
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'shopping-7.jsonl'
            # 1.1 只模拟网络正文，其余使用真实媒体解析器与数据库。
            row = {'object_key': 'shopping-7', 'data': {'commentId': 1, 'images': [{'imageSrcUrl': 'https://dimg04.c-ctrip.com/images/example.png'}]}}
            source.write_text(json.dumps(row) + '\n', encoding='utf-8')
            data = io.BytesIO()
            Image.new('RGB', (3, 2)).save(data, format='PNG')
            job = prepare_media_job({'mode': 'media', 'source_file': source.name}, folder)
            with patch('media.fetch_image', return_value=data.getvalue()) as fetch:
                first = job(Event(), Mock())
                second = job(Event(), Mock())
            self.assertEqual(first['downloaded'], 1)
            self.assertEqual(second['reused'], 1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(list((Path(folder) / 'media').glob('*.png'))), 1)
            self.assertFalse(first['original_verified'])
        logger.info('图片落盘与续跑测试完成')

    def test_inputs_are_restricted_and_cancel_is_forwarded(self):
        # /* ===== 步骤1：校验来源；数据源：工作台输入；操作：1) 拒绝越界 2) 核对取消事件 ===== */
        logger.info('开始评论图片任务测试')
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'sight-136039.jsonl'
            path.write_text('{}\n', encoding='utf-8')
            for invalid in ('../sight-136039.jsonl', 'sight-136039.csv', 'sight-0.jsonl', 'sight-1.jsonl/../sight-136039.jsonl', 'unknown-1.jsonl', '', None):
                with self.subTest(source=invalid), self.assertRaises(ValueError):
                    prepare_media_job({'mode': 'media', 'source_file': invalid}, folder)
            for invalid in (0, True, 100001, '5'):
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    prepare_media_job({'mode': 'media', 'source_file': path.name, 'media_limit': invalid}, folder)
            # 1.1 使用已验证文件和同一个stop事件调用既有图片采集。
            stop = Event()
            emit = Mock()
            with patch('workbench_media_jobs.download_media', return_value={'reason': 'cancelled', 'complete': False}) as download:
                job = prepare_media_job({'mode': 'media', 'source_file': path.name, 'media_limit': 2}, folder)
                self.assertEqual(job(stop, emit)['reason'], 'cancelled')
                download.assert_called_once_with(path.resolve(), Path(folder).resolve(), limit=2, stop=stop)
                self.assertTrue(emit.called)
        logger.info('评论图片任务测试完成')

    def test_refuses_symlink_and_changed_source(self):
        # /* ===== 步骤1：核对文件替换；数据源：任务输入文件；操作：1) 拒绝符号链接 2) 启动前重新检查 ===== */
        logger.info('开始来源文件复核测试')
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'hotel-42.jsonl'
            path.write_text('{}\n', encoding='utf-8')
            request = {'mode': 'media', 'source_file': path.name}
            # 1.1 mock 仅覆盖链接判断，不依赖 Windows 账户的链接创建权限。
            with patch.object(Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError):
                    prepare_media_job(request, folder)
            job = prepare_media_job(request, folder)
            path.unlink()
            with patch('workbench_media_jobs.download_media') as download:
                with self.assertRaises(ValueError):
                    job(Event(), Mock())
                download.assert_not_called()
        logger.info('来源文件复核测试完成')

    def test_pre_cancel_does_not_touch_media(self):
        # /* ===== 步骤1：检查提前取消；数据源：停止事件；操作：1) 停止 2) 不打开下载器 ===== */
        logger.info('开始提前取消测试')
        with TemporaryDirectory() as folder:
            (Path(folder) / 'restaurant-7.jsonl').write_text('{}', encoding='utf-8')
            job = prepare_media_job({'mode': 'media', 'source_file': 'restaurant-7.jsonl'}, folder)
            stop = Event()
            stop.set()
            with patch('workbench_media_jobs.download_media') as download:
                self.assertEqual(job(stop, Mock())['reason'], 'cancelled')
                download.assert_not_called()
        logger.info('提前取消测试完成')
