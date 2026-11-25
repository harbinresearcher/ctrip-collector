import logging
from pathlib import Path
import unittest
from sight_policy import parse_policy

logger = logging.getLogger(__name__)


class PolicyTests(unittest.TestCase):
    def test_merge_headerless_screen_and_reject_other_sight(self):
        # /* ===== 步骤1：核对跨屏；数据源：两屏真机样本；操作：1) 合并去重 2) 拒绝身份改变 ===== */
        logger.info('开始政策跨屏测试')
        from sight_policy import merge_policy_screens
        first, second = [(Path(__file__).parent / 'fixtures' / f).read_text(encoding='utf-8') for f in ('sight-policy.xml', 'sight-policy-second.xml')]
        # 1.1 重复屏不能重复计数，标题改变不能沿用列映射。
        result = merge_policy_screens([first, second, second], '长白山西景区')
        self.assertGreater(len(result['rows']), 7)
        self.assertEqual(sum(r['group'] == '老人' for r in result['rows']), 2)
        self.assertTrue(any(r['group'] == '在职人民警察配偶' for r in result['rows']))
        with self.assertRaises(ValueError):
            merge_policy_screens([first, second.replace('长白山西景区', '另一景区')], '长白山西景区')
        logger.info('政策跨屏测试完成')

    def test_merged_age_rows_and_clipped_bottom(self):
        # /* ===== 步骤1：核验真实表格；数据源：真机样本；操作：1) 关联跨行 2) 排除裁切 ===== */
        logger.info('开始政策表测试')
        xml = (Path(__file__).parent / 'fixtures/sight-policy.xml').read_text(encoding='utf-8')
        # 1.1 老人包含两种条件，不能按文本排列错配给学生。
        rows = parse_policy(xml)['rows']
        elderly = [r for r in rows if r['group'] == '老人']
        self.assertEqual(len(elderly), 2)
        self.assertEqual([r['policy'] for r in elderly], ['半价', '优惠'])
        self.assertTrue(elderly[0]['condition'].startswith('年龄60'))
        self.assertFalse(any(r['group'] in {'在职人民警察配偶', '现役军人'} for r in rows))
        self.assertEqual(rows[0]['group'], '儿童')
        logger.info('政策表测试完成')
