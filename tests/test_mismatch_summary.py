"""summary 要列出两通道目标数不一致的对象。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def snapshot(folder, identity, total, name):
    (Path(folder) / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': name, 'display_total': total,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}, ensure_ascii=False), encoding='utf-8')


def phone_report(folder, identity, all_count, place):
    data = {'kind': 'sight', 'object_id': str(identity), 'place': place, 'reason': 'target_scanned',
            'count': 0, 'run_id': '20261128T120000-abcde1',
            'sampling_context': {'scope': 'current_run_ui_controls_only',
                                 'observations': [{'visible_filters': [
                                     {'label': '全部', 'display_count': all_count}]}]}}
    (folder / '20261128T120000-abcde1.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    (folder / 'report-20261128T120000-abcde1.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


class MismatchSummaryTests(unittest.TestCase):
    def test_mismatch_objects_are_listed_in_summary(self):
        # /* ===== 步骤1：网页 1、手机 2562；数据源：真实分叉案例；操作：1) 进 summary 列表 ===== */
        logger.info('开始分叉汇总测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 151273659, 1, '南山滑雪场')
            phone_report(root, 151273659, 2562, '南山滑雪场')
            summary = write_coverage_audit(root)['summary']
            # 1.1 两个数字都要留着，并且能被一眼看到，而不是静默取一个。
            self.assertEqual(summary['target_mismatch'],
                             [{'object_key': 'sight-151273659', 'name': '南山滑雪场',
                               'web': 1, 'phone': 2562, 'delta': 2561}])
        logger.info('分叉汇总测试完成')

    def test_no_mismatch_gives_empty_list(self):
        # /* ===== 步骤2：两边一致；数据源：相同目标；操作：1) 列表为空 ===== */
        logger.info('开始一致无分叉测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 136039, 1099, '长白山西景区')
            phone_report(root, 136039, 1099, '长白山西景区')
            self.assertEqual(write_coverage_audit(root)['summary']['target_mismatch'], [])
        logger.info('一致无分叉测试完成')


if __name__ == '__main__':
    unittest.main()