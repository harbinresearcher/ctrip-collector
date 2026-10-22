import csv
import logging
import tempfile
import unittest
from pathlib import Path

from core import parse_reviews, Store

logger = logging.getLogger(__name__)
FIXTURES = Path(__file__).parent / 'fixtures'


class CoreTests(unittest.TestCase):
    def test_missing_author_is_not_invented_from_date(self):
        logger.info('开始检查半张评论卡片')
        # 1.0 作者已滑出结构时不能把入住月份当作作者。
        rows = parse_reviews((FIXTURES / 'hotel-clipped.xml').read_text('utf-8'), 'hotel')
        self.assertTrue(all(row['author'] != row['date'] for row in rows))
        logger.info('半张评论卡片检查完成')

    def test_footer_does_not_change_review_identity(self):
        logger.info('开始检查页尾宣传文字隔离')
        # 1.0 同一评论在不同滚动位置出现，不得混入平台文案。
        original = parse_reviews((FIXTURES / 'hotel-comments.xml').read_text('utf-8'), 'hotel')[0]
        scrolled = parse_reviews((FIXTURES / 'hotel-footer.xml').read_text('utf-8'), 'hotel')[0]
        self.assertEqual(original['content'], scrolled['content'])
        logger.info('页尾宣传文字隔离完成')

    # /* ================================================================
    # 步骤1：检查真实页面解析
    # 目标：酒店与景点样本；操作：1) 读取原始 XML 2) 核对正文与日期
    # ================================================================ */
    def test_real_hotel(self):
        logger.info('开始检查酒店样本')
        # 1.1 长评论保留原文，入住月份不冒充发布日期。
        rows = parse_reviews((FIXTURES / 'hotel-comments2.xml').read_text('utf-8'), 'hotel')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['date_kind'], 'stay_month')
        self.assertEqual(rows[0]['date'], '2026年09月入住')
        self.assertIn('希望有机会还能再去入住', rows[0]['content'])
        self.assertNotIn('酒店回复', rows[0]['content'])
        self.assertTrue(rows[0]['has_expand'])
        logger.info('酒店样本检查完成')

    def test_real_sight(self):
        logger.info('开始检查景点样本')
        # 1.2 两张卡片各自保留作者、日期和完整正文。
        rows = parse_reviews((FIXTURES / 'ctrip-comments3.xml').read_text('utf-8'), 'sight')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['author'], 'YoYo_8F6N6N3Y')
        self.assertEqual(rows[0]['date'], '2026-02-23')
        self.assertEqual(rows[0]['rating'], '5.0')
        self.assertIn('值得一游', rows[1]['content'])
        logger.info('景点样本检查完成')

    def test_short_comment_is_not_dropped(self):
        logger.info('开始检查短评论')
        # 1.3 用真实卡片结构替换正文，禁止按长度过滤。
        xml = (FIXTURES / 'hotel-comments.xml').read_text('utf-8')
        xml = xml.replace('房间干净整洁，用餐环境舒适，园区景色不错，度假区内项目丰富，健身房和泳池体验不错，客房管家嘟嘟全程贴心保障', '好')
        self.assertEqual(parse_reviews(xml, 'hotel')[0]['content'], '好')
        logger.info('短评论检查完成')

    def test_invalid_or_foreign_screen(self):
        logger.info('开始检查错误页面')
        # 1.4 非携程界面和损坏 XML 必须拒绝。
        for xml in ('broken', '<hierarchy><node package="com.other" /></hierarchy>'):
            with self.assertRaises(ValueError):
                parse_reviews(xml, 'hotel')
        logger.info('错误页面检查完成')

    # /* ================================================================
    # 步骤2：检查存储与导出
    # 数据源：真实样本和临时数据库；操作：1) 重复写入 2) 重开数据库核对
    # ================================================================ */
    def test_resume_and_object_isolation(self):
        logger.info('开始检查持久去重')
        # 2.1 同一对象重复写入不增长，不同对象独立保存。
        rows = parse_reviews((FIXTURES / 'hotel-comments.xml').read_text('utf-8'), 'hotel')
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            self.assertEqual(store.save('hotel', 'A', rows, 'one.xml'), 1)
            self.assertEqual(store.save('hotel', 'A', rows, 'two.xml'), 0)
            store.close()
            store = Store(Path(folder))
            self.assertEqual(store.count('hotel', 'A'), 1)
            self.assertEqual(store.save('hotel', 'B', rows, 'three.xml'), 1)
            store.close()
        logger.info('持久去重检查完成')

    def test_csv_formula_and_newlines(self):
        logger.info('开始检查 CSV 导出')
        # 2.2 评论作为文本导出，保留换行并阻止表格公式执行。
        rows = parse_reviews((FIXTURES / 'hotel-comments.xml').read_text('utf-8'), 'hotel')
        rows[0]['content'] = '=1+1\n第二行'
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            store.save('hotel', 'A', rows, 'one.xml')
            path = store.export('hotel', 'A')
            with path.open(encoding='utf-8-sig', newline='') as source:
                result = list(csv.DictReader(source))
            self.assertEqual(result[0]['content'], "'=1+1\n第二行")
            store.close()
        logger.info('CSV 导出检查完成')


if __name__ == '__main__':
    unittest.main()
