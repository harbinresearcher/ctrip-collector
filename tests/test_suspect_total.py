"""目录总数可疑时不拿它当分母：发现率必须是 null 而不是假的小数。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def directory_report(folder, listing_total, objects, suspect=None, pages=2):
    rows = [{'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}'} for identity in objects]
    report = {'kind': 'sight', 'scope_id': 2479, 'listing_total': listing_total, 'objects': rows,
              'reason': 'page_budget_reached', 'pages': pages, 'cards_seen': pages * 10,
              'own_cards': len(objects)}
    if suspect is not None:
        report['listing_total_suspect'] = suspect
    (folder / 'discovery-sight-2479-20261128T030000000000Z.json').write_text(
        json.dumps(report, ensure_ascii=False), encoding='utf-8')


class SuspectTotalTests(unittest.TestCase):
    def test_round_thousand_total_is_not_used_as_denominator(self):
        # /* ===== 步骤1：整千总数；数据源：真机见过的 3000；操作：1) 标可疑 2) 不给比值 ===== */
        logger.info('开始整千总数测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            directory_report(root, 3000, [1, 2, 3])
            discovery = write_coverage_audit(root)['summary']['coverage']['discovery']
            # 1.1 用 3000 当分母会算出 0.1% 这种假数字；这里必须给 null。
            self.assertTrue(discovery['listing_total_suspect'])
            self.assertIsNone(discovery['listing_total'])
            self.assertIsNone(discovery['ratio'])
            self.assertEqual(discovery['discovered'], 3)
            self.assertEqual(discovery['pages_read'], 2)
            self.assertIn('截断', discovery['note'])
        logger.info('整千总数测试完成')

    def test_plain_total_still_gives_ratio(self):
        # /* ===== 步骤2：正常总数；数据源：非整千值；操作：1) 仍给比值 ===== */
        logger.info('开始正常总数测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            directory_report(root, 250, [1, 2, 3, 4, 5])
            discovery = write_coverage_audit(root)['summary']['coverage']['discovery']
            self.assertFalse(discovery['listing_total_suspect'])
            self.assertEqual(discovery['listing_total'], 250)
            self.assertEqual(discovery['ratio'], 0.02)
        logger.info('正常总数测试完成')

    def test_explicit_suspect_flag_from_report_is_trusted(self):
        # /* ===== 步骤3：报告显式标可疑；数据源：listing_total_suspect=true；操作：1) 生效 ===== */
        logger.info('开始显式标记测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            directory_report(root, 250, [1, 2], suspect=True)
            discovery = write_coverage_audit(root)['summary']['coverage']['discovery']
            self.assertTrue(discovery['listing_total_suspect'])
            self.assertIsNone(discovery['ratio'])
        logger.info('显式标记测试完成')


if __name__ == '__main__':
    unittest.main()