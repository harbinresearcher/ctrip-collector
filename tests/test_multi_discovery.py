import json
import io
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from multi_discovery import discover_many

logger = logging.getLogger(__name__)


class MultiDiscoveryTests(unittest.TestCase):
    def test_cli_accepts_repeated_city_list_without_changing_single_entry(self):
        # /* ===== 步骤1：核对命令入口；数据源：重复地区参数；操作：1) 分别请求 2) 合并报告 ===== */
        logger.info('开始多地区命令入口测试')
        import app
        one='https://you.ctrip.com/sight/a1/100.html'
        calls=[]
        def find(entry,folder,pages,objects,stop=None):
            calls.append(entry)
            return {'reason':'page_budget_reached','objects':[{'url':one}],'complete':False}
        # 1.1 两个命令行参数依次进入适配器，相同链接只导出一次。
        with tempfile.TemporaryDirectory() as folder, patch('app.discover_objects',side_effect=find), patch('sys.stdout',new_callable=io.StringIO) as stdout:
            args=['app.py','--city-list','city-a','--city-list','city-b','--output',folder]
            with patch('sys.argv',args): app.main()
            result=json.loads(stdout.getvalue())
            self.assertEqual(calls,['city-a','city-b'])
            self.assertEqual(len(result['objects']),1)
        logger.info('多地区命令入口测试完成')

    def test_merges_cities_and_deduplicates_object_links(self):
        # /* ===== 步骤1：核对多地区目录；数据源：两个地区结果；操作：1) 合并去重 2) 保存各地区状态 ===== */
        logger.info('开始多地区目录测试')
        one='https://you.ctrip.com/sight/a1/100.html'
        two='https://you.ctrip.com/sight/b2/200.html'
        def find(entry, folder, pages, objects, stop):
            urls=[one] if entry=='first' else [one,two]
            return {'reason':'page_budget_reached','objects':[{'url':url} for url in urls], 'complete':False}
        # 1.1 各地区预算单独执行，合并清单不能重复对象。
        with tempfile.TemporaryDirectory() as folder:
            result=discover_many(['first','second'],folder,2,5,Event(),find)
            self.assertEqual(result['reason'],'queue_exhausted')
            self.assertEqual([x['url'] for x in result['objects']],[one,two])
            self.assertEqual(len(result['regions']),2)
            self.assertFalse(result['complete'])
            self.assertEqual(Path(result['links_file']).read_text(encoding='utf-8').splitlines(),[one,two])
            self.assertEqual(len(json.loads(Path(result['report_file']).read_text(encoding='utf-8'))['regions']),2)
        logger.info('多地区目录测试完成')

    def test_failure_and_cancel_preserve_prior_objects(self):
        # /* ===== 步骤2：核对中断；数据源：前一地区成功、后一地区失败；操作：1) 停止 2) 保留已发现对象 ===== */
        logger.info('开始多地区中断测试')
        one='https://you.ctrip.com/sight/a1/100.html'
        called=[]
        def find(entry,folder,pages,objects,stop):
            called.append(entry)
            return {'reason':'error' if entry=='bad' else 'page_budget_reached',
                    'error':'failure' if entry=='bad' else '', 'objects':[] if entry=='bad' else [{'url':one}]}
        # 2.1 错误不能覆盖前一区的已发现对象。
        with tempfile.TemporaryDirectory() as folder:
            result=discover_many(['good','bad','later'],folder,2,5,Event(),find)
            self.assertEqual(result['reason'],'error')
            self.assertEqual(called,['good','bad'])
            self.assertEqual(Path(result['links_file']).read_text(encoding='utf-8').strip(),one)
            stop=Event(); stop.set(); called.clear()
            result=discover_many(['good'],folder,2,5,stop,find)
            self.assertEqual(result['reason'],'cancelled')
            self.assertFalse(called)
        logger.info('多地区中断测试完成')
