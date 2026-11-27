"""对账的手机侧口径：目标、已采、缺口与来源标注都要能被判读。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import phone_all_label, write_coverage_audit

logger = logging.getLogger(__name__)


def object_snapshot(folder, identity=136039, total=20):
    (folder / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}), encoding='utf-8')


def mobile_report(folder, identity=136039, run_id='20261127T083202-567534', all_count=99,
                  confirmed=False):
    check = {'scope': 'current_run_fingerprints_vs_app_filter_label', 'filter': None,
             'current_run_unique': 2, 'display_count_labels': [], 'display_total': None,
             'state': 'selection_unconfirmed', 'difference': None}
    if confirmed:
        check.update({'filter': '全部', 'state': 'compared_display_only', 'display_total': all_count})
    data = {'run_id': run_id, 'kind': 'sight', 'object_id': identity, 'place': f'景点{identity}',
            'reason': 'screen_budget_reached', 'count': 2, 'review_count_check': check,
            'sampling_context': {'observations': [{
                'visible_filters': [{'label': '全部', 'display_count': all_count},
                                    {'label': '好评', 'display_count': 90}]}]}}
    (folder / f'report-{run_id}.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def row_for(root, key='sight-136039'):
    return [row for row in write_coverage_audit(root)['objects'] if row['object_key'] == key][0]


class PhoneSideAuditTests(unittest.TestCase):
    def test_all_label_count_becomes_phone_target(self):
        # /* ===== 步骤1：只看屏幕计数；数据源：手机报告；操作：1) 目标 2) 来源标注 ===== */
        logger.info('开始手机侧目标（all_label）测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root)
            mobile_report(root, all_count=99)
            row = row_for(root)
            self.assertEqual(row['phone_target'], 99)
            self.assertEqual(row['phone_target_source'], 'all_label')
            self.assertEqual(row['phone_collected'], 0)
            self.assertEqual(row['phone_missing'], 99)
        logger.info('手机侧目标（all_label）测试完成')

    def test_report_claiming_confirmed_without_stored_run_is_not_trusted(self):
        # /* ===== 步骤2：没入库的运行不算已核对；数据源：报告自称 compared_display_only；
        #         操作：1) app_all_run 仍为 None 2) 退回屏幕计数 ===== */
        logger.info('开始「自称已核对」防伪测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root)
            mobile_report(root, all_count=99, confirmed=True)
            row = row_for(root)
            # 2.1 手机库是空的，报告自称「已核对」也不能升级为 confirmed_run。
            self.assertIsNone(row['app_all_run'])
            self.assertEqual(row['phone_target_source'], 'all_label')
            self.assertEqual(row['phone_target'], 99)
        logger.info('「自称已核对」防伪测试完成')

    def test_no_label_means_unknown_target_not_guess(self):
        # /* ===== 步骤3：没有计数就不猜；数据源：报告没有可见筛选；操作：1) None 2) 不冒充 ===== */
        logger.info('开始手机侧目标缺省测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root)
            data = {'run_id': '20261127T090000-abcdef', 'kind': 'sight', 'object_id': 136039,
                    'place': '景点136039', 'reason': 'screen_budget_reached', 'count': 1,
                    'review_count_check': {'filter': None, 'state': 'selection_unconfirmed',
                                           'display_total': None, 'scope': 'x'},
                    'sampling_context': {'observations': [{'visible_filters': []}]}}
            (root / 'report-20261127T090000-abcdef.json').write_text(
                json.dumps(data, ensure_ascii=False), encoding='utf-8')
            row = row_for(root)
            self.assertIsNone(row['phone_target'])
            self.assertIsNone(row['phone_target_source'])
            self.assertIsNone(row['phone_missing'])
        logger.info('手机侧目标缺省测试完成')

    def test_multiple_different_labels_are_not_trusted(self):
        # /* ===== 步骤4：两次看到不同计数；数据源：多个观察；操作：1) 不猜 2) 返回 None ===== */
        logger.info('开始手机侧目标冲突测试')
        data = {'sampling_context': {'observations': [
            {'visible_filters': [{'label': '全部', 'display_count': 99}]},
            {'visible_filters': [{'label': '全部', 'display_count': 120}]}]}}
        self.assertIsNone(phone_all_label(data))
        logger.info('手机侧目标冲突测试完成')


if __name__ == '__main__':
    unittest.main()