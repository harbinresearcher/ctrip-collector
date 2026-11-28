"""缺口续采：瞬时读取失败重试一次（实测 IncompleteRead 会让对象直接记 failed）。"""
import logging
import unittest
from unittest.mock import patch

from gap_collect import call_with_retry

logger = logging.getLogger(__name__)


class RetryTests(unittest.TestCase):
    def test_transient_read_error_is_retried_once(self):
        # /* ===== 步骤1：第一次读失败；数据源：假采集入口；操作：1) 重试 2) 第二次成功 ===== */
        logger.info('开始续采重试测试')
        calls = []

        def collector(url, output, add, stop, emit):
            calls.append(url)
            if len(calls) == 1:
                raise ConnectionResetError('连接被重置')
            return {'reason': 'target_reached', 'error': ''}

        result = call_with_retry(collector, 'https://you.ctrip.com/sight/a1/1.html', '.', 10,
                                 None, lambda message: None)
        # 1.1 重试后拿到结果，且确实调用了两次。
        self.assertEqual(result['reason'], 'target_reached')
        self.assertEqual(len(calls), 2)
        logger.info('续采重试测试完成')

    def test_non_transient_error_is_not_retried(self):
        # /* ===== 步骤2：非瞬时错误；数据源：普通 ValueError；操作：1) 不重试 2) 直接抛出 ===== */
        logger.info('开始非瞬时错误测试')
        calls = []

        def collector(url, output, add, stop, emit):
            calls.append(url)
            raise ValueError('对象结构变化')

        with self.assertRaises(ValueError):
            call_with_retry(collector, 'https://you.ctrip.com/sight/a1/2.html', '.', 10,
                            None, lambda message: None)
        # 2.1 结构错误重试也没用，不该浪费一次请求。
        self.assertEqual(len(calls), 1)
        logger.info('非瞬时错误测试完成')


if __name__ == '__main__':
    unittest.main()