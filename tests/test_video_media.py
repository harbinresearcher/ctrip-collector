"""评论视频的下载、续跑和失败行为。"""
import io
import json
import logging
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from threading import Event
from unittest.mock import patch
from urllib.error import HTTPError

from video_media import download_videos, validate_video_url

logger = logging.getLogger(__name__)
URL = 'https://video.c-ctrip.com/example.mp4?token=temporary-secret'
MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2' + b'\x00\x00\x00\x08mdat'


class Response(io.BytesIO):
    headers = {}


class Opener:
    def __init__(self, body=MP4, error=None):
        self.body = body
        self.error = error
        self.calls = 0

    def open(self, url, timeout):
        self.calls += 1
        if self.error:
            raise self.error
        return Response(self.body)


class VideoMediaTests(unittest.TestCase):
    def test_incomplete_read_preserves_prior_download_and_manifest(self):
        # /* ===== 步骤1：核验网络中断；数据源：模拟两条视频；操作：1) 首条成功 2) 中断后保留清单 ===== */
        logger.info('开始视频网络中断测试')

        class InterruptedResponse(Response):
            def read(self, size):
                # 1.1 HTTP 客户端抛出不属于 OSError 的中断异常。
                raise IncompleteRead(b'partial', 20)

        class InterruptedOpener:
            def __init__(self):
                self.calls = 0

            def open(self, url, timeout):
                self.calls += 1
                return Response(MP4) if self.calls == 1 else InterruptedResponse()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'sight-123.jsonl'
            rows = [{'object_key': 'sight-123', 'data': {'commentId': number,
                     'videos': [{'playUrl': f'https://video.c-ctrip.com/{number}.mp4'}]}}
                    for number in (1, 2)]
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            result = download_videos(source, root, opener=InterruptedOpener(), delay=0)
            self.assertEqual(result['reason'], 'error')
            self.assertEqual(result['downloaded'], 1)
            manifest = [json.loads(line) for line in (root / 'video-manifest.jsonl').read_text('utf-8').splitlines()]
            self.assertEqual(len(manifest), 2)
            saved = next(item for item in manifest if item['comment_id'] == '1')
            failed = next(item for item in manifest if item['comment_id'] == '2')
            self.assertEqual((root / 'videos' / saved['file']).read_bytes(), MP4)
            self.assertIsNone(failed['file'])
        logger.info('视频网络中断测试完成')

    def test_truncated_response_is_not_recorded_or_reused(self):
        # /* ===== 步骤1：校验响应长度；数据源：模拟中断下载；操作：1) 拒绝短文件 2) 允许下次重试 ===== */
        logger.info('开始视频截断测试')
        class TruncatedResponse(Response):
            headers = {'Content-Length': str(len(MP4))}

        class TruncatedOpener:
            def open(self, url, timeout):
                return TruncatedResponse(MP4[:12])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'sight-123.jsonl'
            source.write_text(json.dumps({'object_key': 'sight-123', 'data': {'commentId': 1,
                              'videos': [{'playUrl': URL}]}}) + '\n', encoding='utf-8')
            # 1.1 文件仍有 MP4 标记，但比 HTTP 承诺长度短，不能作为下载成功。
            failed = download_videos(source, root, opener=TruncatedOpener(), delay=0)
            self.assertEqual(failed['reason'], 'error')
            self.assertEqual(failed['downloaded'], 0)
            self.assertEqual(list((root / 'videos').glob('*.mp4')), [])
            retry = download_videos(source, root, opener=Opener(), delay=0)
            self.assertEqual((retry['downloaded'], retry['reused']), (1, 0))
        logger.info('视频截断测试完成')

    def test_budget_still_links_all_comments_to_selected_video(self):
        # /* ===== 步骤1：保留预算内关联；数据源：跨评论视频地址；操作：1) 跳过超额地址 2) 继续扫描 ===== */
        logger.info('开始视频预算关联测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'sight-123.jsonl'
            # 1.1 第二条出现新地址超出预算，第三条仍引用预算内地址。
            urls = [URL, 'https://video.c-ctrip.com/other.mp4', URL]
            rows = [{'object_key': 'sight-123', 'data': {'commentId': index,
                     'videos': [{'playUrl': url}]}} for index, url in enumerate(urls, 1)]
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            opener = Opener()
            report = download_videos(source, root, limit=1, opener=opener, delay=0)
            manifest = [json.loads(line) for line in (root / 'video-manifest.jsonl').read_text('utf-8').splitlines()]
            self.assertEqual(report['reason'], 'video_budget_reached')
            self.assertEqual((report['downloaded'], opener.calls), (1, 1))
            self.assertEqual({item['comment_id'] for item in manifest}, {'1', '3'})
        logger.info('视频预算关联测试完成')

    def test_partial_http_response_is_not_treated_as_full_video(self):
        # /* ===== 步骤1：核验 HTTP 响应；数据源：非完整响应；操作：1) 拒绝部分内容 2) 不保留视频 ===== */
        logger.info('开始视频部分响应测试')
        class PartialResponse(Response):
            status = 206
            headers = {'Content-Length': str(len(MP4)), 'Content-Range': 'bytes 0-31/1000'}

        class PartialOpener:
            def open(self, url, timeout):
                return PartialResponse(MP4)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'sight-123.jsonl'
            source.write_text(json.dumps({'object_key': 'sight-123', 'data': {'commentId': 1,
                              'videos': [{'playUrl': URL}]}}) + '\n', encoding='utf-8')
            # 1.1 未请求分段时，206 只证明获取了部分字节。
            report = download_videos(source, root, opener=PartialOpener(), delay=0)
            self.assertEqual(report['reason'], 'error')
            self.assertEqual(report['downloaded'], 0)
            self.assertFalse(list((root / 'videos').glob('*.mp4')))
        logger.info('视频部分响应测试完成')

    def test_download_reuse_and_link_multiple_comments(self):
        # /* ===== 步骤1：验证视频续跑；数据源：合成评论；操作：1) 同地址去重 2) 保留评论关联 ===== */
        logger.info('开始评论视频续跑测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'sight-123.jsonl'
            rows = [{'object_key':'sight-123', 'data':{'commentId':i, 'videos':[{'playUrl':URL,'videoId':1}]}}
                    for i in (1, 2)]
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            opener = Opener()
            report = download_videos(source, root, opener=opener, delay=0)
            self.assertEqual((report['downloaded'], report['reused'], opener.calls), (1, 0, 1))
            canonical_root = root.resolve()
            self.assertEqual(Path(report['output_folder']), canonical_root / 'videos')
            self.assertEqual(Path(report['manifest_file']), canonical_root / 'video-manifest.jsonl')
            self.assertFalse(report['complete'])
            manifest = [json.loads(line) for line in (root / 'video-manifest.jsonl').read_text('utf-8').splitlines()]
            self.assertEqual({row['comment_id'] for row in manifest}, {'1', '2'})
            self.assertEqual(len({row['file'] for row in manifest}), 1)
            self.assertEqual((root / 'videos' / manifest[0]['file']).read_bytes(), MP4)
            self.assertNotIn('temporary-secret', json.dumps(manifest))
            reused = download_videos(source, root, opener=opener, delay=0)
            self.assertEqual((reused['downloaded'], reused['reused'], opener.calls), (0, 1, 1))
        logger.info('评论视频续跑测试完成')

    def test_invalid_media_and_http_failure_are_visible(self):
        # /* ===== 步骤1：核验异常；数据源：错误响应；操作：1) 拒绝非视频 2) HTTP 错误停止 ===== */
        logger.info('开始评论视频错误测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'sight-123.jsonl'
            source.write_text(json.dumps({'object_key':'sight-123','data':{'commentId':1,'videos':[{'playUrl':URL}]}})+'\n',encoding='utf-8')
            bad = download_videos(source, root, opener=Opener(b'<html>error</html>'), delay=0)
            self.assertEqual(bad['reason'], 'error')
            self.assertEqual(bad['downloaded'], 0)
            self.assertEqual(list((root / 'videos').glob('*.mp4')), [])
            failure = HTTPError(URL, 432, 'blocked', {}, None)
            report = download_videos(source, root, opener=Opener(error=failure), delay=0)
            self.assertEqual(report['reason'], 'error')
            self.assertIn('432', report['error'])
            self.assertNotIn('temporary-secret', json.dumps(report))
        logger.info('评论视频错误测试完成')

    def test_url_boundaries_and_precancel(self):
        # /* ===== 步骤1：核验地址及取消；数据源：评论视频链接；操作：1) 限定域名 2) 取消不请求 ===== */
        logger.info('开始评论视频边界测试')
        for bad in ('http://video.c-ctrip.com/a.mp4', 'https://video.c-ctrip.com.evil/a',
                    'https://user@video.c-ctrip.com/a', 'https://video.c-ctrip.com:444/a'):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                validate_video_url(bad)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'sight-123.jsonl'
            source.write_text(json.dumps({'object_key':'sight-123','data':{'commentId':1,'videos':[{'playUrl':URL}]}})+'\n',encoding='utf-8')
            stop = Event()
            stop.set()
            opener = Opener()
            report = download_videos(source, folder, stop=stop, opener=opener, delay=0)
            self.assertEqual(report['reason'], 'cancelled')
            self.assertEqual(opener.calls, 0)
        logger.info('评论视频边界测试完成')

    def test_oversized_video_is_not_published(self):
        # /* ===== 步骤1：限制视频大小；数据源：流式响应；操作：1) 超限停止 2) 删除未完成文件 ===== */
        logger.info('开始视频大小测试')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'sight-123.jsonl'
            source.write_text(json.dumps({'object_key':'sight-123','data':{'commentId':1,'videos':[{'playUrl':URL}]}})+'\n',encoding='utf-8')
            with patch('video_media.MAX_VIDEO_BYTES', 20):
                result = download_videos(source, folder, opener=Opener(), delay=0)
            self.assertEqual(result['reason'], 'error')
            self.assertFalse(list((Path(folder)/'videos').iterdir()))
        logger.info('视频大小测试完成')
