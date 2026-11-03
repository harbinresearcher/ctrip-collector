import json
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from region_seeds import discover_regions

logger = logging.getLogger(__name__)


class RegionSeedsTests(unittest.TestCase):
    def test_hotel_seeds_use_hotel_site_city_ids_not_destination_ids(self):
        # /* ===== 步骤1：核对酒店城市入口；数据源：酒店站链接；操作：1) 使用站点原始ID 2) 排除外站 ===== */
        logger.info('开始酒店城市入口测试')
        html='''<a href="/hotel/tianjin3">天津</a><a href="https://hotels.ctrip.com/hotel/tianjin3">重复</a>
        <a href="https://hotels.ctrip.com/hotel/shanghai2">上海</a><a href="https://other.example/hotel/fake5">外站</a>
        <a href="https://you.ctrip.com/place/tianjin154.html">旅游目的地</a>'''
        # 1.1 天津目的地154不能推断成酒店城市154，原站给出的酒店城市是3。
        with tempfile.TemporaryDirectory() as folder:
            result=discover_regions(folder,'hotel',10,html=html)
            self.assertEqual(result['entries'],['https://hotels.ctrip.com/hotel/tianjin3','https://hotels.ctrip.com/hotel/shanghai2'])
            self.assertEqual(result['source_url'],'https://hotels.ctrip.com/jiudian/')
            self.assertFalse(result['complete'])
        logger.info('酒店城市入口测试完成')

    def test_cli_can_chain_seed_regions_into_object_discovery(self):
        # /* ===== 步骤1：核对自动地区入口；数据源：两条目的地；操作：1) 读取种子 2) 传入多地区发现 ===== */
        logger.info('开始自动地区命令测试')
        import app
        entries=['https://you.ctrip.com/sight/tianjin154/s0-p1.html','https://you.ctrip.com/sight/datong275/s0-p1.html']
        # 1.1 自动发现的入口仍须交给业务目录逐地区核验。
        with tempfile.TemporaryDirectory() as folder, patch('app.discover_regions',return_value={'entries':entries,'report_file':'seed.json'}), patch('app.discover_many',return_value={'objects':[],'reason':'queue_exhausted','error':''}) as discover, patch('sys.stdout',new_callable=io.StringIO) as stdout:
            with patch('sys.argv',['app.py','--region-seeds','sight','--max-regions','2','--output',folder]):
                app.main()
            self.assertEqual(discover.call_args.args[0],entries)
            self.assertEqual(json.loads(stdout.getvalue())['region_seed_report'],'seed.json')
        logger.info('自动地区命令测试完成')

    def test_only_valid_destinations_are_deduplicated_and_exported(self):
        # /* ===== 步骤1：核对目的地列表；数据源：主页链接；操作：1) 限定域名 2) 按地区ID去重 ===== */
        logger.info('开始地区入口提取测试')
        html='''<a href="https://you.ctrip.com/place/tianjin154.html">天津</a>
        <a href="/place/tianjin154.html">天津重复</a><a href="https://you.ctrip.com/place/datong275.html">大同</a>
        <a href="https://other.example/place/fake3.html">其他站</a><a href="/place/a0.html">无效地区</a>'''
        # 1.1 首页链接仅生成待核验入口，不能写成已覆盖地区。
        with tempfile.TemporaryDirectory() as folder:
            result=discover_regions(folder,'sight',5,html=html)
            self.assertEqual(result['entries'],['https://you.ctrip.com/sight/tianjin154/s0-p1.html',
                                                'https://you.ctrip.com/sight/datong275/s0-p1.html'])
            self.assertFalse(result['complete'])
            self.assertFalse(result['entries_verified'])
            self.assertEqual(Path(result['links_file']).read_text(encoding='utf-8').splitlines(),result['entries'])
            self.assertEqual(json.loads(Path(result['report_file']).read_text(encoding='utf-8'))['total_visible'],2)
        logger.info('地区入口提取测试完成')

    def test_budget_and_empty_page_are_explicit(self):
        # /* ===== 步骤2：核对预算和异常；数据源：目的地链接；操作：1) 限定数量 2) 无链接不报成功 ===== */
        logger.info('开始地区预算测试')
        html='<a href="/place/a1.html">A</a><a href="/place/b2.html">B</a>'
        with tempfile.TemporaryDirectory() as folder:
            result=discover_regions(folder,'shopping',1,html=html)
            self.assertEqual(result['entries'],['1'])
            self.assertEqual(result['reason'],'region_budget_reached')
            # 2.1 网站结构变化时不能生成空目录冒充采集完成。
            with self.assertRaises(ValueError): discover_regions(folder,'sight',2,html='<html></html>')
        logger.info('地区预算测试完成')
