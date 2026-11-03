import json
import io
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from coverage_report import write_coverage_report

logger = logging.getLogger(__name__)


class CoverageReportTests(unittest.TestCase):
    def test_cli_generates_report_without_network_collection(self):
        # /* ===== 步骤1：核对命令入口；数据源：本地数据库；操作：1) 输出覆盖报告 2) 不启动网络采集 ===== */
        logger.info('开始覆盖报告命令测试')
        import app
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder)/'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)',('sight-1','1','{"content":"PRIVATE_TEXT"}','t'))
                db.commit()
            # 1.1 只输出统计结果，不能把评论正文复制到覆盖报告。
            with patch('sys.argv',['app.py','--coverage','--output',folder]), patch('sys.stdout',new_callable=io.StringIO) as stdout, patch('app.collect_batch',side_effect=AssertionError('network')):
                app.main()
                report=json.loads(stdout.getvalue())
                self.assertEqual(report['objects'][0]['review_count'],1)
                self.assertNotIn('PRIVATE_TEXT',stdout.getvalue())
        logger.info('覆盖报告命令测试完成')

    def test_distinguishes_missing_null_and_populated_fields(self):
        # /* ===== 步骤1：核对字段覆盖；数据源：本地评论库；操作：1) 分开缺失与空值 2) 不推断全量 ===== */
        logger.info('开始字段覆盖统计测试')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'sight-1-object.json').write_text(json.dumps({'kind':'sight','resource_id':1,'name':'景区',
                'display_total':100,'details':{},'ai_summary':{'status':'not_captured','content':None}}),encoding='utf-8')
            with closing(sqlite3.connect(root/'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                rows=[{'content':'文字','images':[{'url':'public'}],'videos':None}, {'content':'','images':[]}, {'score':0}]
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)', [('sight-1',str(i),json.dumps(row),'t') for i,row in enumerate(rows)])
                db.commit()
            # 1.1 三条评论中的缺失字段不能写成平台没有该字段。
            report=write_coverage_report(root)
            obj=report['objects'][0]
            self.assertEqual(obj['review_count'],3)
            self.assertEqual(obj['fields']['images'],{'present':2,'nonempty':1,'missing':1})
            self.assertEqual(obj['fields']['videos'],{'present':1,'nonempty':0,'missing':2})
            self.assertEqual(obj['fields']['score']['nonempty'],1)
            self.assertFalse(obj['complete'])
            self.assertEqual(obj['sources'][0]['ai_summary_status'],'not_captured')
            self.assertEqual(json.loads(Path(report['report_file']).read_text(encoding='utf-8'))['objects'],report['objects'])
        logger.info('字段覆盖统计测试完成')

    def test_invalid_rows_are_reported_not_silently_discarded(self):
        # /* ===== 步骤2：核对坏数据；数据源：无法解码的历史行；操作：1) 记录异常数量 2) 保留其他对象 ===== */
        logger.info('开始坏评论统计测试')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with closing(sqlite3.connect(root/'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)',('sight-2','1','not json','t'))
                db.commit()
            result=write_coverage_report(root)
            self.assertEqual(result['objects'][0]['invalid_rows'],1)
            self.assertEqual(result['objects'][0]['review_count'],1)
        logger.info('坏评论统计测试完成')
