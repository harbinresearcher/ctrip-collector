"""餐饮详情相关对象发现的工作台入口。"""
import logging
import tempfile
import unittest
from threading import Event
from unittest.mock import Mock, patch

from workbench_jobs import make_job

logger = logging.getLogger(__name__)


class RelatedJobTests(unittest.TestCase):
    def test_graph_entry_passes_budget_and_resume_file(self):
        # /* ===== 步骤1：验证关联队列入口；数据源：景点链接；操作：1) 校验预算 2) 传递断点 ===== */
        logger.info('开始工作台关联队列入口测试')
        url = 'https://you.ctrip.com/sight/tianjin154/136039.html'
        name = 'related-graph-' + 'a' * 32 + '.json'
        with tempfile.TemporaryDirectory() as folder:
            stop, emit = Event(), Mock()
            expected = {'reason':'visit_budget_reached','complete':False}
            request = {'mode':'related_graph','urls':[url],'graph_visits':2,'graph_report':name}
            with patch('workbench_jobs.discover_sight_graph', return_value=expected) as finder:
                self.assertEqual(make_job(request, folder)(stop, emit), expected)
            self.assertEqual(finder.call_args.args[0], url)
            self.assertEqual(finder.call_args.kwargs['max_visits'], 2)
            self.assertEqual(finder.call_args.kwargs['resume_file'], name)
            self.assertIs(finder.call_args.kwargs['stop'], stop)
            for visits in (0, 101, True):
                with self.subTest(visits=visits), self.assertRaises(ValueError):
                    make_job(dict(request, graph_visits=visits), folder)
        logger.info('工作台关联队列入口测试完成')

    def test_sight_related_discovery_requires_one_sight(self):
        # /* ===== 步骤1：校验景点推荐入口；数据源：景点链接；操作：1) 校验类型 2) 传递任务参数 ===== */
        logger.info('开始景点推荐入口测试')
        url = 'https://you.ctrip.com/sight/tianjin154/136039.html'
        with tempfile.TemporaryDirectory() as folder:
            stop, emit = Event(), Mock()
            expected = {'reason':'recommendations_read', 'objects':[], 'complete':False}
            with patch('workbench_jobs.discover_sight_related', return_value=expected) as finder:
                result = make_job({'mode':'related_sight','urls':[url]}, folder)(stop, emit)
            self.assertEqual(result, expected)
            self.assertEqual(finder.call_args.args[0], url)
            self.assertIs(finder.call_args.kwargs['stop'], stop)
            self.assertIs(finder.call_args.kwargs['emit'], emit)
            with self.assertRaises(ValueError):
                make_job({'mode':'related_sight','urls':['https://hotels.ctrip.com/hotels/123.html']}, folder)
        logger.info('景点推荐入口测试完成')

    def test_related_discovery_uses_one_restaurant_url(self):
        # /* ===== 步骤1：检查入口；数据源：餐饮详情链接；操作：1) 启动发现 2) 返回实际报告 ===== */
        logger.info('开始关联对象工作台测试')
        url = 'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html'
        with tempfile.TemporaryDirectory() as folder:
            request = {'mode': 'related_discovery', 'urls': [url]}
            expected = {'reason': 'finished', 'objects': [{'kind': 'hotel'}], 'complete': False}
            stop, emit = Event(), Mock()
            with patch('workbench_jobs.discover_related', return_value=expected) as finder:
                result = make_job(request, folder)(stop, emit)
            self.assertEqual(result, expected)
            self.assertEqual(finder.call_args.args[0], url)
            self.assertIs(finder.call_args.kwargs['stop'], stop)
            self.assertIs(finder.call_args.kwargs['emit'], emit)
            # 1.1 不允许用此入口访问任意地址，或者把多个入口误当一次任务。
            for urls in ([], [url, url], ['https://example.com/'],
                         ['https://hotels.ctrip.com/hotels/123.html']):
                with self.subTest(urls=urls), self.assertRaises(ValueError):
                    make_job({'mode': 'related_discovery', 'urls': urls}, folder)
        logger.info('关联对象工作台测试完成')

    def test_cancelled_before_request_does_not_discover(self):
        # /* ===== 步骤1：检查取消；数据源：停止事件；操作：1) 提前取消 2) 不发送请求 ===== */
        logger.info('开始关联对象取消测试')
        with tempfile.TemporaryDirectory() as folder:
            job = make_job({'mode': 'related_discovery', 'urls': [
                'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html']}, folder)
            stop = Event()
            stop.set()
            with patch('workbench_jobs.discover_related') as finder:
                self.assertEqual(job(stop, Mock())['reason'], 'cancelled')
                finder.assert_not_called()
        logger.info('关联对象取消测试完成')

    def test_resolve_recommendations_from_saved_report(self):
        # /* ===== 步骤1：核验餐厅候选；数据源：已保存发现报告；操作：1) 限定来源 2) 调用核验器 ===== */
        logger.info('开始推荐餐厅核验入口测试')
        with tempfile.TemporaryDirectory() as folder:
            name = 'related-restaurant-' + 'a' * 32 + '.json'
            request = {'mode': 'resolve_related', 'report_file': name}
            expected = {'reason': 'finished', 'objects': [], 'complete': False}
            stop, emit = Event(), Mock()
            with patch('workbench_jobs.resolve_related_restaurants', return_value=expected) as resolver:
                self.assertEqual(make_job(request, folder)(stop, emit), expected)
            self.assertEqual(resolver.call_args.args[0], name)
            self.assertIs(resolver.call_args.kwargs['stop'], stop)
            self.assertIs(resolver.call_args.kwargs['emit'], emit)
            for bad in ('../' + name, 'other.json', '', None):
                with self.subTest(name=bad), self.assertRaises(ValueError):
                    make_job({'mode': 'resolve_related', 'report_file': bad}, folder)
        logger.info('推荐餐厅核验入口测试完成')
