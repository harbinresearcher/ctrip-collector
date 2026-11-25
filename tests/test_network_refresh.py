"""验证已达累计目标后显式读取新来源。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from batch import collect_batch
from network import collect_network

logger = logging.getLogger(__name__)
URL = 'https://hotels.ctrip.com/hotels/123.html'


def target(method):
    return {'kind': 'hotel', 'resource_id': 123, 'name': '酒店', 'url': URL,
            'display_total': 1, 'source_method': method}


def page(items):
    return {'code': 200, 'result': {'items': items, 'totalCount': 1}}


class RefreshTests(unittest.TestCase):
    def test_refresh_reads_new_source_after_existing_target(self):
        # /* ===== 步骤1：刷新不同来源；数据源：模拟酒店响应；操作：1) 旧库达标 2) 接收新来源补充 ===== */
        logger.info('开始不同来源刷新测试')
        first = {'commentId': 1, 'content': '好'}
        newer = {'commentId': 1, 'content': '好住', 'images': [{'imageSrcUrl': 'https://example.com/x'}]}
        for source in ('hotel_browser_response', 'app_proxy_capture'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as folder:
                collect_network(target('hotel_public_html_snapshot'), folder, 1, fetch=lambda _: page([first]), delay=0)
                fetch = Mock(return_value=page([newer]))
                report = collect_network(target(source), folder, 1, fetch=fetch, delay=0, refresh=True)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual((report['count'], report['new_count'], report['pages'], report['read_unique']), (1, 0, 1, 1))
                self.assertEqual(report['target_scope'], 'current_run')
                data = json.loads(Path(report['jsonl']).read_text('utf-8').splitlines()[0])['data']
                self.assertEqual(data['content'], '好住')
                observations = [json.loads(line) for line in Path(report['observations_jsonl']).read_text('utf-8').splitlines()]
                self.assertEqual({item['source_method'] for item in observations}, {'hotel_public_html_snapshot', source})
        logger.info('不同来源刷新测试完成')

    def test_refresh_empty_and_cancelled_do_not_claim_target(self):
        # /* ===== 步骤2：核对空页和取消；数据源：已有评论；操作：1) 刷新空响应 2) 请求中停止 ===== */
        logger.info('开始刷新中断测试')
        with tempfile.TemporaryDirectory() as folder:
            collect_network(target('hotel_browser_response'), folder, 1, fetch=lambda _: page([{'commentId': 1}]), delay=0)
            result = collect_network(target('hotel_browser_response'), folder, 1, fetch=lambda _: page([]), delay=0, refresh=True)
            self.assertEqual((result['reason'], result['read_unique'], result['count']), ('empty_page', 0, 1))
            stop = Event()
            def cancelled(index):
                stop.set()
                return page([{'commentId': 1, 'content': '不得保存'}])
            result = collect_network(target('hotel_browser_response'), folder, 1, stop=stop, fetch=cancelled, delay=0, refresh=True)
            self.assertEqual((result['reason'], result['pages'], result['read_unique']), ('cancelled', 0, 0))
            # 2.1 请求失败明确显示错误，既有记录和文件仍可访问。
            def failed(index):
                raise ValueError('接口拒绝')
            result = collect_network(target('hotel_browser_response'), folder, 1, fetch=failed, delay=0, refresh=True)
            self.assertEqual((result['reason'], result['pages'], result['read_unique'], result['count']), ('error', 0, 0, 1))
            self.assertIn('接口拒绝', result['error'])
            self.assertEqual(json.loads(Path(result['jsonl']).read_text('utf-8').splitlines()[0])['data']['commentId'], 1)
            with self.assertRaises(ValueError):
                collect_network(target('hotel_browser_response'), folder, 1, refresh='false')
        logger.info('刷新中断测试完成')

    def test_batch_refresh_bypasses_verified_skip_and_forwards_flag(self):
        # /* ===== 步骤3：核对批任务；数据源：已完成记录；操作：1) 默认跳过 2) 显式刷新 ===== */
        logger.info('开始批任务刷新测试')
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            def collector(url, output, limit, stop, emit, *, refresh=False):
                calls.append(refresh)
                current = {'commentId': 1, 'content': '好住' if refresh else '好'}
                return collect_network(target('hotel_browser_response'), output, limit, stop=stop, emit=emit,
                                       fetch=lambda _: page([current]), delay=0, refresh=refresh)
            first = collect_batch([URL], folder, 1, collector)
            self.assertEqual(first['processed'], 1)
            self.assertEqual(collect_batch([URL], folder, 1, collector)['skipped'], 1)
            refreshed = collect_batch([URL], folder, 1, collector, refresh=True)
            self.assertEqual((refreshed['processed'], refreshed['skipped'], calls), (1, 0, [False, True]))
            self.assertEqual(refreshed['results'][0]['read_unique'], 1)
            with self.assertRaises(ValueError):
                collect_batch([URL], folder, 1, collector, refresh='false')
        logger.info('批任务刷新测试完成')
