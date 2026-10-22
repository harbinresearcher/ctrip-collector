import io
import json
import logging
import sqlite3
from contextlib import closing
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from urllib.request import Request

from PIL import Image
from media import CheckedRedirect, download_media, image_links, validate_image_url

logger = logging.getLogger(__name__)
URL = 'https://dimg04.c-ctrip.com/images/example.jpg'


class MediaTests(unittest.TestCase):
    def test_truncated_animation_is_not_saved(self):
        # /* ===== 步骤0：核对动画后续帧；数据源：截断GIF；操作：1) 保留首帧 2) 拒绝损坏后帧 ===== */
        logger.info('开始动画完整性检查')
        payload = io.BytesIO()
        Image.new('RGB', (10, 10), 'red').save(payload, format='GIF', save_all=True,
                    append_images=[Image.new('RGB', (10, 10), 'blue')], duration=100, loop=0)
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'comments.jsonl'
            source.write_text(json.dumps({'object_key': 'sight-1', 'data': {'commentId': 1,
                              'images': [{'imageSrcUrl': URL}]}})+'\n', encoding='utf-8')
            result = download_media(source, Path(folder) / 'out', 1,
                                    fetch=lambda _: payload.getvalue()[:-10], delay=0)
            self.assertEqual(result['reason'], 'error')
            self.assertEqual(result['downloaded'], 0)
        logger.info('动画完整性检查完成')

    def test_only_comment_images_and_allowed_hosts(self):
        # /* ===== 步骤1：核对图片来源；数据源：评论字段；操作：1) 忽略头像 2) 校验地址边界 ===== */
        logger.info('开始图片来源检查')
        row = {'data': {'commentId': 1, 'userInfo': {'userImage': URL},
                        'images': [{'imageSrcUrl': URL}]}}
        self.assertEqual(list(image_links(row)), [URL])
        self.assertEqual(validate_image_url(URL), URL)
        for bad in ['http://127.0.0.1/x', 'https://dimg04.c-ctrip.com.evil.test/x',
                    'https://user@dimg04.c-ctrip.com/x', 'https://dimg04.c-ctrip.com:444/x']:
            with self.assertRaises(ValueError):
                validate_image_url(bad)
        with self.assertRaises(ValueError):
            CheckedRedirect().redirect_request(Request(URL), None, 302, 'redirect', {}, 'https://127.0.0.1/image')
        logger.info('图片来源检查完成')

    def test_download_resume_and_repair_corrupt_file(self):
        # /* ===== 步骤2：核对下载续跑；数据源：模拟PNG；操作：1) 保存尺寸 2) 损坏后重新下载 ===== */
        logger.info('开始图片续跑检查')
        data = io.BytesIO()
        Image.new('RGB', (3, 2)).save(data, format='PNG')
        fetch = Mock(return_value=data.getvalue())
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'comments.jsonl'
            source.write_text(json.dumps({'object_key': 'hotel-1', 'data': {'commentId': 1,
                              'imageCuttingsList': [{'bigImageUrl': URL}]}})+'\n', encoding='utf-8')
            output = Path(folder) / 'out'
            self.assertEqual(download_media(source, output, 5, fetch=fetch, delay=0)['downloaded'], 1)
            self.assertEqual(download_media(source, output, 5, fetch=fetch, delay=0)['reused'], 1)
            asset = next((output / 'media').glob('*.png'))
            asset.write_bytes(b'broken')
            self.assertEqual(download_media(source, output, 5, fetch=fetch, delay=0)['downloaded'], 1)
            self.assertEqual(fetch.call_count, 2)
            # 2.1 损坏后修复失败，不保留旧成功记录。
            asset.write_bytes(b'broken again')
            failed = download_media(source, output, 5, fetch=lambda _: b'<html>error</html>', delay=0)
            self.assertEqual(failed['reason'], 'error')
            with closing(sqlite3.connect(output / 'media.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM assets').fetchone()[0], 0)
        logger.info('图片续跑检查完成')

    def test_html_response_is_not_an_image(self):
        # /* ===== 步骤3：核对错误正文；数据源：验证页；操作：1) 拒绝HTML 2) 保留失败报告 ===== */
        logger.info('开始错误图片检查')
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'comments.jsonl'
            source.write_text(json.dumps({'object_key': 'sight-1', 'data': {'commentId': 1,
                              'images': [{'imageSrcUrl': URL}]}})+'\n', encoding='utf-8')
            result = download_media(source, Path(folder) / 'out', 1, fetch=lambda _: b'<html>verify</html>', delay=0)
            self.assertEqual(result['reason'], 'error')
            self.assertEqual(result['downloaded'], 0)
        logger.info('错误图片检查完成')
