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
    def test_cli_resumes_from_report_without_repeating_finished_regions(self):
        # /* ===== 步骤1：核对续跑入口；数据源：已有地区报告；操作：1) 读取原预算 2) 跳过已完成地区 ===== */
        logger.info('开始续跑命令入口测试')
        import app
        one='https://you.ctrip.com/sight/a1/100.html'
        calls=[]
        def finder(entry,folder,pages,objects,stop):
            calls.append(entry)
            return {'reason':'error' if entry=='b' else 'page_budget_reached','error':'failed' if entry=='b' else '',
                    'objects':[{'url':one}]}
        # 1.1 恢复时使用报告中原始预算，而非命令默认值。
        with tempfile.TemporaryDirectory() as folder:
            first=discover_many(['a','b'],folder,2,5,Event(),finder)
            calls.clear()
            with patch('app.discover_objects',side_effect=lambda entry,folder,pages,objects,stop: calls.append((entry,pages,objects)) or {'reason':'page_budget_reached','objects':[]}), patch('sys.stdout',new_callable=io.StringIO) as stdout:
                with patch('sys.argv',['app.py','--resume-regions',first['report_file'],'--output',folder]): app.main()
                result=json.loads(stdout.getvalue())
            self.assertEqual(calls,[('b',2,5)])
            self.assertEqual(result['resumed_regions'],1)
        logger.info('续跑命令入口测试完成')

    def test_resume_keeps_completed_and_partial_regions_and_retries_failure(self):
        # /* ===== 步骤1：核对地区续跑；数据源：失败前进度；操作：1) 保留已得对象 2) 重试失败地区 ===== */
        logger.info('开始地区续跑测试')
        one='https://you.ctrip.com/sight/a1/100.html'
        two='https://you.ctrip.com/sight/b2/200.html'
        three='https://you.ctrip.com/sight/c3/300.html'
        calls=[]
        def broken(entry,folder,pages,objects,stop):
            calls.append(entry)
            return {'reason':'error' if entry=='b' else 'page_budget_reached','error':'failed' if entry=='b' else '',
                    'objects':[{'url':two if entry=='b' else one}]}
        def recovered(entry,folder,pages,objects,stop):
            calls.append(entry)
            return {'reason':'page_budget_reached','objects':[{'url':two if entry=='b' else three}]}
        # 1.1 不重复请求已完成地区；失败地区已有对象保留且继续去重。
        with tempfile.TemporaryDirectory() as folder:
            first=discover_many(['a','b','c'],folder,2,5,Event(),broken)
            calls.clear()
            result=discover_many(['a','b','c'],folder,2,5,Event(),recovered,resume_report=first['report_file'])
            self.assertEqual(calls,['b','c'])
            self.assertEqual(result['resumed_regions'],1)
            self.assertEqual([x['url'] for x in result['objects']],[one,two,three])
            self.assertFalse(result['complete'])
        logger.info('地区续跑测试完成')

    def test_resume_rejects_changed_budget_and_corrupted_checkpoint(self):
        # /* ===== 步骤2：核对续跑证据；数据源：预算和地区快照；操作：1) 检查参数 2) 拒绝篡改快照 ===== */
        logger.info('开始续跑证据测试')
        url='https://you.ctrip.com/sight/a1/100.html'
        calls=[]
        def find(entry,folder,pages,objects,stop):
            calls.append(entry)
            return {'reason':'page_budget_reached','objects':[{'url':url}]}
        with tempfile.TemporaryDirectory() as folder:
            first=discover_many(['a','b'],folder,2,5,Event(),find)
            calls.clear()
            # 2.1 参数不一致或快照内容改变时，任何新请求都不能发出。
            with self.assertRaises(ValueError): discover_many(['a','b'],folder,3,5,Event(),find,resume_report=first['report_file'])
            path=Path(first['report_file']); saved=json.loads(path.read_text(encoding='utf-8'))
            saved['regions'][0]['objects'][0]['url']='https://you.ctrip.com/sight/a1/999.html'
            path.write_text(json.dumps(saved),encoding='utf-8')
            with self.assertRaises(ValueError): discover_many(['a','b'],folder,2,5,Event(),find,resume_report=path)
            self.assertEqual(calls,[])
        logger.info('续跑证据测试完成')

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
            progress=[]
            result=discover_many(['first','second'],folder,2,5,Event(),find,emit=progress.append)
            self.assertEqual(result['reason'],'queue_exhausted')
            self.assertEqual([x['url'] for x in result['objects']],[one,two])
            self.assertEqual(len(result['regions']),2)
            self.assertFalse(result['complete'])
            self.assertTrue(any('1/2' in line for line in progress))
            self.assertTrue(any('2/2' in line for line in progress))
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
