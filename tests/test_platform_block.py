"""平台限流/验证码识别与整体熔断：全部用构造响应，不触网。"""
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from coverage_audit import write_coverage_audit
from drive import drive_rounds
from gap_collect import collect_gaps, gap_candidates
from network import blocked_status, collect_network, is_blocked, load_target

logger = logging.getLogger(__name__)
TARGET = {'kind': 'restaurant', 'resource_id': 1, 'resource_type': 12, 'poi_id': 2,
          'name': '餐馆', 'display_total': 20, 'details': {},
          'url': 'https://you.ctrip.com/food/a1/1.html'}


def raising(error):
    # 模拟请求层直接抛异常（真实 HTTPError 由 urlopen 在非 2xx 时抛出）。
    def fetch(_index):
        raise error
    return fetch


def object_snapshot(root, identity, total):
    url = f'https://you.ctrip.com/sight/a1/{identity}.html'
    data = {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}',
            'display_total': total, 'url': url}
    (Path(root) / f'sight-{identity}-object.json').write_text(json.dumps(data), encoding='utf-8')
    return url


def blocked_result(status='432'):
    # 与被拦的 collect_network 报告同形：reason 仍是 error，但 blocked 必须为真。
    return {'reason': 'error', 'error': f'HTTP Error {status}: Unknown', 'export_error': '',
            'blocked': True, 'blocked_status': status, 'complete': False}


class BlockedSignalTests(unittest.TestCase):
    def test_blocked_signals_are_recognized(self):
        # /* ===== 步骤1：核对识别；数据源：状态码/异常/响应体；操作：1) 四种状态码 2) 验证与稍后再试原文 ===== */
        logger.info('开始风控信号识别测试')
        for status in (403, 429, 432, 511):
            self.assertTrue(is_blocked(status))
            self.assertEqual(blocked_status(status), status)
            self.assertEqual(blocked_status(str(status)), status)
        error = HTTPError('https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList',
                          432, 'Unknown', None, None)
        self.assertTrue(is_blocked(error))
        self.assertEqual(blocked_status(error), 432)
        for text in ('访问验证：请输入验证码', '请完成人机校验后继续', '请求过于频繁，请稍后再试'):
            self.assertTrue(is_blocked(ValueError(text)), text)
        # 1.1 响应体里的状态位与提示语也算被拦；只看提示字段，不扫点评正文。
        self.assertTrue(is_blocked({'code': 432, 'msg': '被限制'}))
        self.assertEqual(blocked_status({'code': 200, 'msg': '检测到异常请求，请稍后再试'}), '请稍后再试')
        # 1.2 详情页读取被拦：抛给上层的异常本身就是可识别的信号。
        with patch('network.urlopen', side_effect=HTTPError(
                'https://you.ctrip.com/sight/city1/12.html', 432, 'Unknown', None, None)):
            with self.assertRaises(HTTPError) as caught:
                load_target('https://you.ctrip.com/sight/city1/12.html')
        self.assertTrue(is_blocked(caught.exception))
        # 1.3 点评请求被拦：报告写 blocked=True 与原始状态码，不能报成 empty_page。
        with tempfile.TemporaryDirectory() as folder:
            report = collect_network(TARGET, Path(folder), 10, delay=0, fetch=raising(HTTPError(
                'https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList', 432, 'Unknown', None, None)))
            self.assertEqual(report['reason'], 'error')
            self.assertTrue(report['blocked'])
            self.assertEqual(report['blocked_status'], '432')
            persisted = next(Path(folder).glob('restaurant-1-report-*.json'))
            saved = json.loads(persisted.read_text(encoding='utf-8'))
            self.assertEqual((saved['blocked'], saved['blocked_status'], saved['reason']),
                             (True, '432', 'error'))
        # 1.4 HTTP 200 的错误体同样识别为被拦。
        with tempfile.TemporaryDirectory() as folder:
            report = collect_network(TARGET, Path(folder), 10, delay=0,
                                     fetch=lambda _index: {'code': 432, 'msg': '请输入验证码'})
            self.assertTrue(report['blocked'])
            self.assertEqual(report['blocked_status'], '432')
        logger.info('风控信号识别测试完成')

    def test_ordinary_errors_are_not_blocked(self):
        # /* ===== 步骤2：核对不误判；数据源：500/超时/连接重置；操作：1) 都不算 blocked 2) 报告不写被拦 ===== */
        logger.info('开始普通错误不误判测试')
        self.assertFalse(is_blocked(500))
        self.assertFalse(is_blocked(HTTPError('https://you.ctrip.com/food/a1/1.html', 500,
                                              'Server Error', None, None)))
        self.assertFalse(is_blocked(TimeoutError('timed out')))
        self.assertFalse(is_blocked(URLError('Connection reset by peer')))
        self.assertFalse(is_blocked(ValueError('接口返回未知分页结构，已停止')))
        self.assertFalse(is_blocked({'code': 500, 'msg': '服务异常'}))
        self.assertFalse(is_blocked({'code': 200, 'result': {'items': [], 'totalCount': 0}}))
        with tempfile.TemporaryDirectory() as folder:
            report = collect_network(TARGET, Path(folder), 10, delay=0, fetch=raising(HTTPError(
                'https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList', 500, 'Server Error', None, None)))
            self.assertEqual(report['reason'], 'error')
            self.assertFalse(report['blocked'])
            self.assertEqual(report['blocked_status'], '')
            # 2.1 超时也不是被拦，不能因此整轮停手。
            timeouts = collect_network(TARGET, Path(folder), 10, delay=0,
                                       fetch=raising(TimeoutError('timed out')))
            self.assertFalse(timeouts['blocked'])
        logger.info('普通错误不误判测试完成')


class BlockedCircuitTests(unittest.TestCase):
    def test_collect_gaps_stops_immediately_when_blocked(self):
        # /* ===== 步骤3：核对熔断；数据源：第一个对象被拦；操作：1) 整轮 reason=blocked 2) 后续对象零请求 ===== */
        logger.info('开始缺口续采熔断测试')
        with tempfile.TemporaryDirectory() as folder:
            for identity in (1, 2, 3):
                object_snapshot(folder, identity, 10)
            seen = []

            def collector(url, output, limit, stop, emit):
                seen.append(url)
                return blocked_result()

            report = collect_gaps(folder, 5, gap_limit=3, collector=collector)
            self.assertEqual(report['reason'], 'blocked')
            self.assertEqual(report['blocked'], {'object_key': 'sight-1', 'status': '432'})
            # 3.1 第一个对象被拦后，第 2、3 个对象一个请求都没发。
            self.assertEqual(len(seen), 1)
            self.assertEqual([item['object_key'] for item in report['processed']], ['sight-1'])
            self.assertTrue(report['processed'][0]['blocked'])
            self.assertEqual(report['processed'][0]['blocked_status'], '432')
        # 3.2 并发：一个线程被拦后，排队的后续对象不再提交，在途请求跑完就停。
        with tempfile.TemporaryDirectory() as folder:
            first = second = None
            for identity in (1, 2, 3, 4):
                url = object_snapshot(folder, identity, 10)
                if identity == 1:
                    first = url
                elif identity == 2:
                    second = url
            seen = []

            def collector(url, output, limit, stop, emit):
                seen.append(url)
                if url == first:
                    return blocked_result()
                return {'reason': 'target_reached', 'error': '', 'export_error': '', 'complete': False}

            report = collect_gaps(folder, 5, gap_limit=4, workers=2, collector=collector)
            self.assertEqual(report['reason'], 'blocked')
            # 已经开出去的对象最多两个（sight-1 被拦、sight-2 在途）；sight-3、sight-4 不得发请求。
            self.assertTrue(set(seen) <= {first, second}, seen)
            self.assertNotIn('https://you.ctrip.com/sight/a1/3.html', seen)
            self.assertNotIn('https://you.ctrip.com/sight/a1/4.html', seen)
            self.assertLessEqual(len(report['processed']), 2)
        # 3.3 对账能区分「被拦」与「没数据」：对象行带 blocked，且不再进入续采队列。
        with tempfile.TemporaryDirectory() as folder:
            url = object_snapshot(folder, 7, 10)
            collect_network({'kind': 'sight', 'resource_id': 7, 'name': '景点7', 'display_total': 10,
                             'url': url}, Path(folder), 10, delay=0,
                            fetch=raising(HTTPError(url, 432, 'Unknown', None, None)))
            audit = write_coverage_audit(folder)
            row = next(item for item in audit['objects'] if item['object_key'] == 'sight-7')
            self.assertEqual((row['blocked'], row['blocked_status'], row['gap_reason']),
                             (True, '432', 'blocked'))
            self.assertEqual(gap_candidates(audit['objects']), [])
        # 3.4 驱动循环同规则：看到被拦对象就整轮停，不再打下一个。
        with tempfile.TemporaryDirectory() as folder:
            for identity in (1, 2, 3):
                object_snapshot(folder, identity, 10)
            seen = []

            def collector(url, output, limit, stop, emit):
                seen.append(url)
                return blocked_result()

            report = drive_rounds(folder, 3, 1, 1, 5, 3, collector=collector, emit=lambda _msg: None)
            self.assertEqual((report['reason'], report['rounds_used']), ('blocked', 1))
            self.assertEqual(report['blocked'], {'object_key': 'sight-1', 'status': '432'})
            self.assertEqual(report['rounds'][0]['reason'], 'blocked')
            self.assertEqual(len(seen), 1)
        logger.info('缺口续采熔断测试完成')


class BlockedCliTests(unittest.TestCase):
    def test_cli_blocked_prints_one_hint_and_exits_one(self):
        # /* ===== 步骤4：核对命令行；数据源：真实 cli() + 被拦链接；操作：1) 一句提示 2) 退出 1 3) 无堆栈 ===== */
        logger.info('开始命令行被拦提示测试')
        import app
        with tempfile.TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 10)
            blocked = HTTPError('https://you.ctrip.com/sight/a1/1.html', 432, 'Unknown', None, None)
            with patch('sys.argv', ['app.py', '--collect-gaps', '--output', folder]), \
                    patch('app.collect_url', side_effect=blocked), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                    self.assertLogs(level='ERROR') as logs, \
                    self.assertRaises(SystemExit) as caught:
                app.cli()
            self.assertEqual(caught.exception.code, 1)
            hint = '\n'.join(logs.output)
            self.assertIn('平台限流（HTTP 432）', hint)
            self.assertIn('请停止今天的采集', hint)
            self.assertIn('不要换 IP 或换账号重试', hint)
            self.assertNotIn('Traceback', hint)
            # 4.1 报告仍照常输出，说明这一轮是被拦而不是普通无数据。
            self.assertEqual(json.loads(stdout.getvalue())['reason'], 'blocked')
        logger.info('命令行被拦提示测试完成')


if __name__ == '__main__':
    unittest.main()