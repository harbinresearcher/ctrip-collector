"""手机队列：目标未知时按网页缺口排优先级。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from phone_queue import write_phone_queue

logger = logging.getLogger(__name__)


def snapshot(root, identity, total, name=None):
    data = {'kind': 'sight', 'resource_id': identity, 'name': name or f'景点{identity}',
            'display_total': total, 'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}
    (Path(root) / f'sight-{identity}-object.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')


class QueueOrderTests(unittest.TestCase):
    def test_unknown_phone_target_orders_by_web_gap(self):
        # /* ===== 步骤1：都没有手机数据；数据源：三个大小不同的对象；操作：1) 按网页缺口降序 ===== */
        logger.info('开始网页缺口排序测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 11, 10)     # 小对象
            snapshot(root, 12, 900)    # 大对象
            snapshot(root, 13, 120)    # 中对象
            queue = write_phone_queue(root, max_objects=5)['queue']
            keys = [entry['object_key'] for entry in queue]
            # 1.1 手机目标都未知 → 网页缺口最大的排最前，避免城市级队列按对象键乱序。
            self.assertEqual(keys, ['sight-12', 'sight-13', 'sight-11'])
            self.assertEqual(queue[0]['web_missing'], 900)
            self.assertTrue(queue[0]['target_unknown'])
        logger.info('网页缺口排序测试完成')

    def test_known_phone_gap_still_outranks_unknown(self):
        # /* ===== 步骤2：有手机缺口的排在未知前面；数据源：一个已知缺口 + 一个大对象；操作：1) 顺序 ===== */
        logger.info('开始已知缺口优先测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 21, 50)
            snapshot(root, 22, 5000)
            report = {'kind': 'sight', 'object_id': '21', 'place': '景点21', 'reason': 'target_reached',
                      'run_id': '20261128T100000-abcdef', 'count': 5,
                      'sampling_context': {'observations': [{'visible_filters': [
                          {'label': '全部', 'display_count': 50}]}]}}
            (root / 'report-20261128T100000-abcdef.json').write_text(
                json.dumps(report, ensure_ascii=False), encoding='utf-8')
            queue = write_phone_queue(root, max_objects=5)['queue']
            # 2.1 21 已经有手机目标（缺口 45），排首位；22 目标未知、缺口 5000 也只能排后面。
            self.assertEqual(queue[0]['object_key'], 'sight-21')
            # 2.2 手机库里没有这行记录时「已采」按 0 算，所以缺口等于目标 50。
            self.assertEqual(queue[0]['missing'], 50)
        logger.info('已知缺口优先测试完成')


if __name__ == '__main__':
    unittest.main()