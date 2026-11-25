import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import ANY, Mock, patch

from capture_io import capture_record
from workbench_capture_jobs import prepare_capture_job

logger = logging.getLogger(__name__)


class CaptureJobTests(unittest.TestCase):
    def test_refresh_is_boolean_and_forwarded_to_capture_import(self):
        # /* ===== 步骤1：校验刷新入口；数据源：工作台表单；操作：1) 拒绝无效类型 2) 传递刷新与停止 ===== */
        logger.info('开始工作台抓包刷新测试')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'captures').mkdir()
            source = root / 'captures' / 'filtered.jsonl'
            source.write_text('{}\n', encoding='utf-8')
            base = {'mode':'capture_import','capture_file':source.name,'refresh':True}
            for invalid in (1, 'true', None):
                with self.subTest(refresh=invalid), self.assertRaises(ValueError):
                    prepare_capture_job(dict(base, refresh=invalid), root)
            stop, emit = Event(), Mock()
            with patch('workbench_capture_jobs.import_capture', return_value=[]) as importer:
                report = prepare_capture_job(base, root)(stop, emit)
            importer.assert_called_once_with(source.resolve(), root.resolve(), 500, stop, emit, on_result=ANY, refresh=True)
            self.assertEqual(report['reason'], 'capture_imported')
            stop.set()
            with patch('workbench_capture_jobs.import_capture') as importer:
                self.assertEqual(prepare_capture_job(base, root)(stop, emit)['reason'], 'cancelled')
                importer.assert_not_called()
        logger.info('工作台抓包刷新测试完成')

    def test_confined_input_and_forwarded_cancellation(self):
        # /* ===== 步骤1：检查导入边界；数据源：工作台输入；操作：1) 路径限定 2) 传递停止事件 ===== */
        logger.info('开始工作台导入边界测试')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'captures').mkdir()
            source = root / 'captures' / 'filtered.jsonl'
            source.write_text('{}\n', encoding='utf-8')
            base = {'mode': 'capture_import', 'capture_file': source.name, 'limit': 20}
            for bad in ('../filtered.jsonl', 'C:/filtered.jsonl', 'filtered.har', 'unknown.jsonl', '', None):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    prepare_capture_job(dict(base, capture_file=bad), folder)
            for bad in (True, 0, 100001, '10'):
                with self.subTest(limit=bad), self.assertRaises(ValueError):
                    prepare_capture_job(dict(base, limit=bad), folder)
            # 1.1 用户取消事件与进度回调保持同一实例。
            stop, emit = Event(), Mock()
            with patch('workbench_capture_jobs.import_capture', return_value=[{'reason':'capture_exhausted','count':1,'complete':False}]) as importer:
                result = prepare_capture_job(base, folder)(stop, emit)
                importer.assert_called_once_with(source.resolve(), root.resolve(), 20, stop, emit, on_result=ANY)
                self.assertEqual(result['reason'], 'capture_imported')
                self.assertFalse(result['complete'])
                self.assertEqual(result['results'][0]['count'], 1)
        logger.info('工作台导入边界测试完成')

    def test_cancellation_error_and_source_revalidation(self):
        # /* ===== 步骤1：保留停止与错误；数据源：导入器结果；操作：1) 取消 2) 错误 3) 复核文件 ===== */
        logger.info('开始工作台导入结果测试')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'captures').mkdir()
            source = root / 'captures' / 'filtered.jsonl'
            source.write_text('{}\n', encoding='utf-8')
            request = {'mode':'capture_import', 'capture_file':source.name}
            job = prepare_capture_job(request, root)
            stop = Event()
            stop.set()
            with patch('workbench_capture_jobs.import_capture') as importer:
                self.assertEqual(job(stop, Mock())['reason'], 'cancelled')
                importer.assert_not_called()
            # 1.1 导出失败不能显示为成功，仍保留已处理对象。
            result = [{'reason':'capture_exhausted','error':'','export_error':'导出失败','count':1}]
            with patch('workbench_capture_jobs.import_capture', return_value=result):
                report = job(Event(), Mock())
                self.assertEqual(report['reason'], 'error')
                self.assertEqual(report['error'], '导出失败')
                self.assertEqual(report['results'], result)
            # 1.2 导入器返回错误时，即使收到取消也不能隐藏失败。
            stop = Event()
            def failed_with_stop(*args, **kwargs):
                stop.set()
                return result
            with patch('workbench_capture_jobs.import_capture', side_effect=failed_with_stop):
                report = job(stop, Mock())
                self.assertEqual(report['reason'], 'error')
                self.assertEqual(report['results'], result)
            source.unlink()
            with patch('workbench_capture_jobs.import_capture') as importer, self.assertRaises(ValueError):
                job(Event(), Mock())
            importer.assert_not_called()
            # 1.3 符号链接一律拒绝，不依赖系统是否允许创建链接。
            source.write_text('{}', encoding='utf-8')
            with patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ValueError):
                prepare_capture_job(request, root)
            with patch.object(Path, 'is_junction', return_value=True), self.assertRaises(ValueError):
                prepare_capture_job(request, root)
            # 1.4 中途取消后保留已导入对象，同时标记停止。
            stop = Event()
            def interrupted(*args, **kwargs):
                stop.set()
                return [{'reason':'capture_exhausted','error':'','count':1}]
            with patch('workbench_capture_jobs.import_capture', side_effect=interrupted):
                report = prepare_capture_job(request, root)(stop, Mock())
                self.assertEqual(report['reason'], 'cancelled')
                self.assertEqual(report['results'][0]['count'], 1)
        logger.info('工作台导入结果测试完成')

    def test_partial_results_are_retained_when_a_later_hotel_fails(self):
        # /* ===== 步骤1：核验部分失败；数据源：模拟多酒店导入；操作：1) 保留首家报告 2) 显示后续异常 ===== */
        logger.info('开始多酒店部分失败测试')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'captures').mkdir()
            (root / 'captures' / 'filtered.jsonl').write_text('{}\n', encoding='utf-8')
            request = {'mode': 'capture_import', 'capture_file': 'filtered.jsonl'}
            saved = {'object_key': 'hotel-123', 'reason': 'capture_exhausted', 'count': 1}
            stop = Event()

            def importer(*args, on_result):
                # 1.1 第一份报告已提交，第二家失败时同时收到停止信号。
                on_result(saved)
                stop.set()
                raise OSError('磁盘故障')

            with patch('workbench_capture_jobs.import_capture', side_effect=importer):
                report = prepare_capture_job(request, root)(stop, Mock())
            self.assertEqual(report['reason'], 'error')
            self.assertIn('磁盘故障', report['error'])
            self.assertEqual(report['results'], [saved])
        logger.info('多酒店部分失败测试完成')

    def test_real_importer_on_synthetic_capture(self):
        # /* ===== 步骤1：验证导入链路；数据源：合成受限响应；操作：1) 解析并落库 2) 保留AI字段 ===== */
        logger.info('开始受限响应导入链路测试')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'captures').mkdir()
            source = root / 'captures' / 'filtered.jsonl'
            # 1.1 不依赖网络或真实个人评论；对象ID与响应需一致。
            record = capture_record('https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo',
                                    {'hotelId':123}, {'data':{'hotelName':'测试酒店','totalCount':1,
                                    'groupList':[{'commentList':[{'id':1,'hotelId':123,'content':'测试'}]}],
                                    'aiSummaryEntities':{'summaryItemList':[{'title':'测试'}]}}}, 200)
            source.write_text(json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8')
            report = prepare_capture_job({'mode':'capture_import','capture_file':source.name}, root)(Event(), Mock())
            self.assertEqual(report['reason'], 'capture_imported')
            self.assertEqual(report['results'][0]['count'], 1)
            self.assertEqual(report['results'][0]['reason'], 'capture_exhausted')
            target = json.loads((root / 'hotel-123-app-capture-object.json').read_text(encoding='utf-8'))
            self.assertEqual(target['ai_summary']['status'], 'response_captured')
            self.assertFalse(report['complete'])
            # 1.2 原始或未知结构不能冒充已筛选的受限响应。
            source.write_text('{"raw":"not_filtered"}\n', encoding='utf-8')
            failed = prepare_capture_job({'mode':'capture_import','capture_file':source.name}, root)(Event(), Mock())
            self.assertEqual(failed['reason'], 'error')
            self.assertIn('受限响应格式', failed['error'])
            self.assertEqual(failed['results'], [])
        logger.info('受限响应导入链路测试完成')
