"""目标数不一致时要标出来：两条通道对「这个对象有多少点评」说法不同。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def object_snapshot(folder, identity, total):
    (folder / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}), encoding='utf-8')


def phone_report(folder, identity, all_count, run_id='20261128T120000-abcdef'):
    data = {'kind': 'sight', 'object_id': str(identity), 'place': f'景点{identity}',
            'reason': 'target_reached', 'run_id': run_id, 'count': 1,
            'sampling_context': {'observations': [{'visible_filters': [
                {'label': '全部', 'display_count': all_count}]}]}}
    (folder / f'report-{run_id}.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def row_for(root, key):
    return [row for row in write_coverage_audit(root)['objects'] if row['object_key'] == key][0]


class TargetMismatchTests(unittest.TestCase):
    def test_different_targets_are_flagged(self):
        # /* ===== 步骤1：两通道目标不同；数据源：网页 420 / 手机 450；操作：1) 标出差值 ===== */
        logger.info('开始目标不一致测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root, 2506647, 420)
            phone_report(root, 2506647, 450)
            row = row_for(root, 'sight-2506647')
            # 1.1 不静默选一个，而是把两边数字和差值都留下。
            self.assertEqual(row['target_mismatch'], {'web': 420, 'phone': 450, 'delta': 30})
        logger.info('目标不一致测试完成')

    def test_same_targets_stay_null(self):
        # /* ===== 步骤2：目标一致；数据源：都是 520；操作：1) 不报不一致 ===== */
        logger.info('开始目标一致测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root, 143364, 520)
            phone_report(root, 143364, 520)
            self.assertIsNone(row_for(root, 'sight-143364')['target_mismatch'])
        logger.info('目标一致测试完成')

    def test_single_channel_never_flags(self):
        # /* ===== 步骤3：只有一边有目标；数据源：只有网页；操作：1) 不报不一致 ===== */
        logger.info('开始单通道测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root, 136039, 1099)
            self.assertIsNone(row_for(root, 'sight-136039')['target_mismatch'])
        logger.info('单通道测试完成')


if __name__ == '__main__':
    unittest.main()