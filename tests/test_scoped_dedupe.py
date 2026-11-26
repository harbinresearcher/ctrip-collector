"""作用域去重：同一条点评重复读到、展开后重读、显示名变化都不能算成两条。"""
import logging
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from mobile_scoped_store import MobileScopedStore

logger = logging.getLogger(__name__)
RUN_A = '20261126T101010-aaaaaa'
RUN_B = '20261126T111111-bbbbbb'
COLLAPSED = {'author': '张三', 'date': '2024年10月', 'date_kind': 'stay_month', 'rating': '5.0',
             'content': '位置很好，服务…', 'has_expand': True, 'raw_texts': ['位置很好，服务…', '展开'],
             'card_fields': {'media_tiles_visible': 2}}
EXPANDED = dict(COLLAPSED, content='位置很好，服务热情，早餐丰富，会再来', has_expand=False)


class ScopedDedupeTests(unittest.TestCase):
    @staticmethod
    def store(folder):
        # 1.0 连接必须在临时目录清理前关闭，否则 Windows 删不掉库文件。
        return closing(MobileScopedStore(folder, 'hotel', '123456'))

    def test_repeat_sighting_counts_once_and_records_each_run(self):
        # /* ===== 步骤1：重复读到；数据源：同一条点评；操作：1) 两次入库 2) 计数不变 ===== */
        logger.info('开始重复读到检查')
        with tempfile.TemporaryDirectory() as folder:
            with self.store(folder) as store:
                for run in (RUN_A, RUN_B):
                    store.start_run(run, '测试酒店')
                    observed = {}
                    store.save('测试酒店', [COLLAPSED], 'evidence/00001.xml', observed_keys=observed, run_id=run)
                    store.finish_run(run, 'target_reached')
                    self.assertEqual(list(observed), [store.review_key(COLLAPSED)])
                # 1.1 同一条点评仍是一条；但两次任务都记下了「这次看到了它」。
                self.assertEqual(store.count(), 1)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM mobile_review_run_items').fetchone()[0], 2)
                self.assertEqual(store.db.execute(
                    'SELECT COUNT(*) FROM reviews_scoped_observations').fetchone()[0], 1)
        logger.info('重复读到检查完成')

    def test_expanded_reread_maps_back_to_the_original_key(self):
        # /* ===== 步骤2：展开后重读；数据源：已展开正文；操作：1) 别名回原键 2) 不新增行 ===== */
        logger.info('开始展开重读检查')
        with tempfile.TemporaryDirectory() as folder:
            with self.store(folder) as store:
                store.start_run(RUN_A, '测试酒店')
                observed = {}
                store.save('测试酒店', [COLLAPSED], 'evidence/00001.xml', observed_keys=observed, run_id=RUN_A)
                original = store.review_key(COLLAPSED)
                self.assertTrue(store.save_expansion(COLLAPSED, EXPANDED, 'evidence/00001-expanded.xml'))
                store.finish_run(RUN_A, 'target_reached')
                # 2.1 下一轮 App 已经显示展开后的正文，指纹不同；仍要认回原来的那条。
                store.start_run(RUN_B, '测试酒店')
                observed = {}
                store.save('测试酒店', [EXPANDED], 'evidence/00001.xml', observed_keys=observed, run_id=RUN_B)
                self.assertNotEqual(store.review_key(EXPANDED), original)
                self.assertEqual(list(observed), [original])
                self.assertEqual(store.count(), 1)
                self.assertEqual(store.db.execute(
                    'SELECT expanded_content FROM reviews_scoped_expansions').fetchone()[0], EXPANDED['content'])
        logger.info('展开重读检查完成')

    def test_display_name_change_keeps_one_row(self):
        # /* ===== 步骤3：显示名变化；数据源：同一对象改名；操作：1) 仍按ID去重 2) 计数不变 ===== */
        logger.info('开始显示名变化检查')
        with tempfile.TemporaryDirectory() as folder:
            with self.store(folder) as store:
                for run, place in ((RUN_A, '测试酒店'), (RUN_B, '测试酒店（新名）')):
                    store.start_run(run, place)
                    store.save(place, [COLLAPSED], 'evidence/00001.xml', observed_keys={}, run_id=run)
                    store.finish_run(run, 'target_reached')
                # 3.1 作用域跟着对象 ID 走，名字只是展示字段。
                self.assertEqual(store.count(), 1)
        logger.info('显示名变化检查完成')


if __name__ == '__main__':
    unittest.main()