"""景点关联对象队列发现和断点续跑。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event

from related_graph import discover_sight_graph, digest_report

logger = logging.getLogger(__name__)
A = 'https://you.ctrip.com/sight/city1/1.html'
B = 'https://you.ctrip.com/sight/city1/2.html'
C = 'https://you.ctrip.com/sight/city1/3.html'
B_ALT = 'https://you.ctrip.com/sight/city2/2.html'
FOOD = 'https://you.ctrip.com/food/city1/4.html'
SHOP = 'https://you.ctrip.com/shopping/city1/5.html'


def item(kind, identity, url):
    return {'kind':kind, 'resource_id':identity, 'poi_id':identity + 100, 'url':url,
            'identity_status':'page_link_only', 'observations':[{'relation':'recommended'}]}


class GraphTests(unittest.TestCase):
    def test_resume_honors_interval_between_requests(self):
        # /* ===== 步骤1：核验续跑间隔；数据源：相邻队列任务；操作：1) 首次访问 2) 续跑限速 ===== */
        logger.info('开始关联队列续跑限速测试')
        waits = []
        class Stop:
            def is_set(self): return False
            def wait(self, seconds):
                waits.append(seconds)
                return False
        def fetch(url, folder, stop=None, emit=None):
            resource = int(url.rsplit('/', 1)[-1].split('.')[0])
            return {'reason':'recommendations_read',
                    'source':{'url':url,'kind':'sight','resource_id':resource,'poi_id':resource+100},
                    'objects':[item('sight',2,B)] if url == A else []}
        with tempfile.TemporaryDirectory() as folder:
            first = discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=1.5)
            discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=1.5, stop=Stop(),
                                 resume_file=Path(first['report_file']).name)
            self.assertEqual(len(waits), 1)
            self.assertGreater(waits[0], 0)
            self.assertLessEqual(waits[0], 1.5)
        logger.info('关联队列续跑限速测试完成')

    def test_bad_candidate_url_is_skipped_without_discarding_valid_candidates(self):
        # /* ===== 步骤0：核对候选异常；数据源：单页推荐；操作：1) 跳过无效地址 2) 保留有效对象 ===== */
        logger.info('开始景点无效候选测试')
        rows = [dict(item('sight', 2, B), url=None), dict(item('sight', 2, B), url=123), item('sight', 3, C)]
        def fetch(url, folder, stop=None, emit=None):
            return {'reason':'recommendations_read','source':{'kind':'sight','url':A,'resource_id':1,'poi_id':101},
                    'objects':rows}
        with tempfile.TemporaryDirectory() as folder:
            result = discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=0)
            self.assertEqual(result['reason'], 'visit_budget_reached')
            self.assertEqual(result['skipped_invalid'], 2)
            self.assertEqual(result['pending_sights'], [C])
        logger.info('景点无效候选测试完成')

    def test_same_resource_different_urls_not_revisited_and_edges_survive(self):
        # /* ===== 步骤1：核对对象去重；数据源：不同页面推荐；操作：1) 合并同资源 2) 保存各来源边 ===== */
        logger.info('开始相同景点跨地址去重测试')
        pages = {A:[item('sight',2,B),item('sight',3,C)],
                 B:[], C:[item('sight',2,B_ALT)]}
        calls = []
        def fetch(url, folder, stop=None, emit=None):
            calls.append(url)
            identity = int(url.rsplit('/',1)[1].split('.')[0])
            return {'reason':'recommendations_read','source':{'kind':'sight','url':url,'resource_id':identity,'poi_id':identity+100},'objects':pages[url]}
        with tempfile.TemporaryDirectory() as folder:
            report = discover_sight_graph(A, folder, max_visits=5, fetch=fetch, delay=0)
            self.assertEqual(calls, [A,B,C])
            self.assertEqual([x['resource_id'] for x in report['objects']].count(2), 1)
            self.assertEqual({(edge['source'],edge['target']) for edge in report['edges']}, {(A,B),(A,C),(C,B)})
        logger.info('相同景点跨地址去重测试完成')

    def test_mismatch_between_recommendation_and_open_page_stops_before_expansion(self):
        # /* ===== 步骤2：核对来源身份；数据源：推荐和详情页；操作：1) 首次登记 2) 冲突保留待处理 ===== */
        logger.info('开始景点推荐身份冲突测试')
        bad = True
        def fetch(url, folder, stop=None, emit=None):
            if url == A:
                return {'reason':'recommendations_read','source':{'kind':'sight','url':A,'resource_id':1,'poi_id':101},'objects':[item('sight',2,B)]}
            return {'reason':'recommendations_read','source':{'kind':'sight','url':B,'resource_id':2,'poi_id':999 if bad else 102},'objects':[item('sight',3,C)]}
        with tempfile.TemporaryDirectory() as folder:
            report = discover_sight_graph(A, folder, max_visits=2, fetch=fetch, delay=0)
            self.assertEqual(report['reason'], 'error')
            self.assertEqual(report['expanded_sights'], [A])
            self.assertEqual(report['pending_sights'], [B])
            self.assertNotIn(C, {x['url'] for x in report['objects']})
            root = next(x for x in report['objects'] if x['url'] == A)
            self.assertEqual(root['poi_id'],101)
            self.assertEqual(root['identity_status'],'page_verified')
            bad = False
            resumed = discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=0,
                                           resume_file=Path(report['report_file']).name)
            self.assertIn(B, resumed['expanded_sights'])
            self.assertIn(C, resumed['pending_sights'])
        logger.info('景点推荐身份冲突测试完成')

    def test_resume_merges_legacy_aliases_without_losing_relations(self):
        # /* ===== 步骤3：兼容历史断点；数据源：旧版重复地址；操作：1) 合并资源 2) 保留不同观察 ===== */
        logger.info('开始历史景点别名断点测试')
        calls = []
        def fetch(url, folder, stop=None, emit=None):
            calls.append(url)
            identity = int(url.rsplit('/',1)[1].split('.')[0])
            return {'reason':'recommendations_read','source':{'kind':'sight','url':url,
                    'resource_id':identity,'poi_id':identity+100},
                    'objects':[item('sight',2,B)] if url == A else []}
        with tempfile.TemporaryDirectory() as folder:
            first = discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=0)
            path = Path(first['report_file'])
            # 3.1 模拟旧版同资源以不同地址写入对象、队列和边。
            first['objects'].append(item('sight',2,B_ALT))
            first['pending_sights'].append(B_ALT)
            first['edges'].append({'source':A,'target':B_ALT,'kind':'sight',
                                   'observations':[{'relation':'other'}]})
            first['state_sha256'] = digest_report(first)
            path.write_text(json.dumps(first, ensure_ascii=False), encoding='utf-8')
            result = discover_sight_graph(A, folder, max_visits=2, fetch=fetch, delay=0,
                                          resume_file=path.name)
            self.assertEqual(calls, [A,B])
            self.assertEqual([x['resource_id'] for x in result['objects']].count(2), 1)
            self.assertEqual(result['pending_sights'], [])
            self.assertEqual({x['relation'] for x in result['edges'][0]['observations']},
                             {'recommended','other'})
        logger.info('历史景点别名断点测试完成')

    def test_resume_discovers_neighbors_without_repeating_visited_pages(self):
        # /* ===== 步骤1：验证队列续跑；数据源：三页合成推荐；操作：1) 限制访问 2) 保留关系和断点 ===== */
        logger.info('开始景点关联队列续跑测试')
        pages = {A:[item('sight',2,B),item('restaurant',4,FOOD)],
                 B:[item('sight',1,A),item('sight',3,C),item('shopping',5,SHOP)], C:[]}
        calls = []
        def fetch(url, folder, stop=None, emit=None):
            calls.append(url)
            identity = int(url.rsplit('/',1)[1].split('.')[0])
            return {'reason':'recommendations_read','source':{'kind':'sight','url':url,
                    'resource_id':identity,'poi_id':identity+100},'objects':pages[url],
                    'recommendations_status':'available','list_status':{}}
        with tempfile.TemporaryDirectory() as folder:
            first = discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=0)
            self.assertEqual(first['reason'], 'visit_budget_reached')
            self.assertEqual(first['expanded_sights'], [A])
            self.assertEqual(first['pending_sights'], [B])
            self.assertEqual({x['url'] for x in first['objects']}, {A, B, FOOD})
            second = discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=0,
                                          resume_file=Path(first['report_file']).name)
            self.assertEqual(second['expanded_sights'], [A, B])
            self.assertEqual(second['pending_sights'], [C])
            self.assertIn(SHOP, {x['url'] for x in second['objects']})
            self.assertFalse(second['complete'])
            third = discover_sight_graph(A, folder, max_visits=1, fetch=fetch, delay=0,
                                         resume_file=Path(second['report_file']).name)
            self.assertEqual(third['reason'], 'queue_exhausted')
            self.assertFalse(third['complete'])
            self.assertEqual(calls, [A, B, C])
            self.assertEqual(len(third['edges']), 5)
            self.assertEqual(json.loads(Path(third['report_file']).read_text('utf-8')), third)
        logger.info('景点关联队列续跑测试完成')

    def test_cancel_and_error_keep_pending_page_for_retry(self):
        # /* ===== 步骤1：核验中断；数据源：有界队列；操作：1) 取消 2) 错误后保留待访问页 ===== */
        logger.info('开始景点关联队列中断测试')
        with tempfile.TemporaryDirectory() as folder:
            stop = Event()
            stop.set()
            def fail(url, folder, stop=None, emit=None):
                return {'reason':'error','error':'页面错误','objects':[]}
            cancelled = discover_sight_graph(A, folder, max_visits=1, stop=stop, fetch=fail, delay=0)
            self.assertEqual(cancelled['reason'], 'cancelled')
            self.assertEqual(cancelled['pending_sights'], [A])
            error = discover_sight_graph(A, folder, max_visits=1, fetch=fail, delay=0,
                                         resume_file=Path(cancelled['report_file']).name)
            self.assertEqual(error['reason'], 'error')
            self.assertEqual(error['pending_sights'], [A])
            self.assertIn('页面错误', error['error'])
        logger.info('景点关联队列中断测试完成')

    def test_resume_rejects_changed_or_wrong_source(self):
        # /* ===== 步骤1：核验断点；数据源：本地队列报告；操作：1) 检查摘要 2) 限定根对象 ===== */
        logger.info('开始景点断点核验测试')
        with tempfile.TemporaryDirectory() as folder:
            stop = Event()
            stop.set()
            first = discover_sight_graph(A, folder, stop=stop, delay=0)
            name = Path(first['report_file']).name
            with self.assertRaises(ValueError):
                discover_sight_graph(B, folder, resume_file=name, delay=0)
            path = Path(first['report_file'])
            data = json.loads(path.read_text('utf-8'))
            data['pending_sights'] = []
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ValueError):
                discover_sight_graph(A, folder, resume_file=name, delay=0)
            with self.assertRaises(ValueError):
                discover_sight_graph(A, folder, resume_file='../' + name, delay=0)
        logger.info('景点断点核验测试完成')
