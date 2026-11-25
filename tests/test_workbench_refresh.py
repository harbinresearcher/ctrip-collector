"""工作台显式刷新已达目标对象。"""
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from workbench_jobs import make_job

logger = logging.getLogger(__name__)


class RefreshWorkbenchTests(unittest.TestCase):
    def test_hotel_refresh_flows_through_batch_and_collector(self):
        # /* ===== 步骤1：核验刷新传递；数据源：酒店任务；操作：1) 刷新批任务 2) 传递到酒店采集 ===== */
        logger.info('开始酒店刷新入口测试')
        url = 'https://hotels.ctrip.com/hotels/123.html'
        with tempfile.TemporaryDirectory() as folder:
            stop, emit = Event(), Mock()
            def batch(urls, output, limit, collector, stop, emit, *, refresh):
                self.assertTrue(refresh)
                return collector(urls[0], output, limit, stop, emit, refresh=True)
            with patch('workbench_jobs.collect_batch', side_effect=batch), \
                 patch('workbench_jobs.collect_hotel', return_value={'reason':'target_reached'}) as hotel:
                report = make_job({'mode':'reviews','urls':[url],'refresh':True}, folder)(stop, emit)
            self.assertEqual(report['reason'], 'target_reached')
            self.assertTrue(hotel.call_args.kwargs['refresh'])
            with self.assertRaises(ValueError):
                make_job({'mode':'reviews','urls':[url],'refresh':'true'}, folder)
        logger.info('酒店刷新入口测试完成')

    def test_network_refresh_flows_to_source_collector(self):
        # /* ===== 步骤1：核验网络刷新；数据源：景点任务；操作：1) 读取对象 2) 传递刷新状态 ===== */
        logger.info('开始网络刷新入口测试')
        url = 'https://you.ctrip.com/sight/city1/123.html'
        with tempfile.TemporaryDirectory() as folder:
            def batch(urls, output, limit, collector, stop, emit, *, refresh):
                return collector(urls[0], output, limit, stop, emit, refresh=refresh)
            with patch('workbench_jobs.collect_batch', side_effect=batch), \
                 patch('workbench_jobs.load_target', return_value={'kind':'sight'}), \
                 patch('workbench_jobs.collect_network', return_value={'reason':'target_reached'}) as network:
                make_job({'mode':'reviews','urls':[url],'refresh':True}, folder)(Event(), Mock())
            self.assertTrue(network.call_args.kwargs['refresh'])
        logger.info('网络刷新入口测试完成')
