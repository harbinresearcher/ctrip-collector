"""被平台拦住只挡当天：隔天的 blocked 报告要放行续采。"""
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def write_object(folder, identity=9, total=100):
    (folder / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}), encoding='utf-8')


def write_blocked_report(folder, identity=9, age_days=0):
    path = folder / f'sight-{identity}-report-20261126T120000000000Z.json'
    path.write_text(json.dumps({'object_key': f'sight-{identity}', 'kind': 'sight', 'reason': 'error',
                                'error': 'HTTP Error 432', 'blocked': True, 'blocked_status': '432',
                                'pages': 0, 'count': 0}), encoding='utf-8')
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


class BlockedExpiryTests(unittest.TestCase):
    def test_blocked_report_from_today_still_blocks(self):
        # /* ===== 步骤1：当天被拦；数据源：今天的 blocked 报告；操作：1) 标 blocked 2) 不进续采 ===== */
        logger.info('开始当天拦截测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            write_object(root)
            write_blocked_report(root, age_days=0)
            obj = write_coverage_audit(root)['objects'][0]
            self.assertTrue(obj['blocked'])
            self.assertEqual(obj['gap_reason'], 'blocked')
            self.assertEqual(obj['blocked_status'], '432')
        logger.info('当天拦截测试完成')

    def test_blocked_report_from_yesterday_is_retryable(self):
        # /* ===== 步骤2：隔天被拦；数据源：昨天的 blocked 报告；操作：1) 解除 2) 回到可续采 ===== */
        logger.info('开始隔天放行测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            write_object(root)
            write_blocked_report(root, age_days=1)
            obj = write_coverage_audit(root)['objects'][0]
            # 2.1 隔天不再算「被拦」，缺口回到 missing 才会被 --collect-gaps 重新捡起。
            self.assertFalse(obj['blocked'])
            self.assertNotEqual(obj['gap_reason'], 'blocked')
            self.assertEqual(obj['missing'], 100)
        logger.info('隔天放行测试完成')


if __name__ == '__main__':
    unittest.main()