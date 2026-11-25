import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import MagicMock, patch
from workbench_app_jobs import prepare_app_job

logger = logging.getLogger(__name__)


class AppJobsTests(unittest.TestCase):
    def test_validated_modes_forward_stop_and_release_device(self):
        # /* ===== 步骤1：验证手机任务；数据源：模拟设备；操作：1) 传递参数 2) 释放连接 ===== */
        logger.info('开始手机任务适配测试')
        stop = Event()
        emit = MagicMock()
        device = MagicMock()
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as factory, \
             patch('workbench_app_jobs.capture_ai', return_value={'status': 'partial'}) as ai, \
             patch('workbench_app_jobs.capture_ai_gallery', return_value={'status': 'page_limit', 'pages': []}) as gallery, \
             patch('workbench_app_jobs.collect', return_value={'reason': 'target_reached', 'complete': False}) as reviews:
            factory.return_value.__enter__.return_value = device
            # 1.1 汇总、图集和评论各使用各自原始采集器和同一停止事件。
            common = {'kind': 'hotel', 'name': '测试酒店', 'resource_id': '123', 'limit': 7, 'max_pages': 3, 'max_swipes': 20}
            for mode in ('app_ai', 'app_gallery', 'app_reviews'):
                result = prepare_app_job(dict(common, mode=mode), folder, 'adb.exe')(stop, emit)
                self.assertNotEqual(result.get('status'), 'error')
            ai.assert_called_once_with(device, 'hotel', '测试酒店', '123', Path(folder).resolve(), stop)
            gallery.assert_called_once_with(device, '123', Path(folder).resolve(), stop, max_pages=3)
            reviews.assert_called_once_with(device, 'hotel', '测试酒店', Path(folder).resolve(), 7, 20, stop, emit, object_id='123')
            self.assertEqual(device.connect.call_count, 3)
            self.assertEqual(factory.return_value.__exit__.call_count, 3)
        logger.info('手机任务适配测试完成')

    def test_invalid_inputs_and_cancelled_before_connection(self):
        # /* ===== 步骤1：核对输入边界；数据源：用户表单；操作：1) 拒绝无效值 2) 提前取消 ===== */
        logger.info('开始手机任务输入测试')
        valid = {'mode': 'app_ai', 'kind': 'hotel', 'name': '酒店', 'resource_id': '123'}
        for updates in ({'mode': 'other'}, {'kind': 'sight'}, {'name': '  '}, {'resource_id': '0'},
                        {'resource_id': '12x'}, {'max_pages': 501}, {'max_pages': True}, {'limit': False}, {'max_swipes': 0}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                prepare_app_job(dict(valid, **updates), '.', 'adb.exe')
        with self.assertRaises(ValueError):
            prepare_app_job(valid, '.', None)
        # 1.1 预取消不访问设备，图集打开期间取消归一化为已取消。
        stop = Event()
        stop.set()
        with patch('workbench_app_jobs.Adb') as factory:
            result = prepare_app_job(valid, '.', 'adb.exe')(stop, lambda _: None)
            self.assertEqual(result['reason'], 'cancelled')
            factory.assert_not_called()
        logger.info('手机任务输入测试完成')

    def test_app_reviews_requires_object_id_before_device_connection(self):
        # /* ===== 步骤1：核对评论对象 ID；数据源：工作台请求；操作：1) 拒绝无效 ID 2) 不连接设备 ===== */
        logger.info('开始评论对象 ID 检查')
        # 1.1 酒店和景点均须填写正整数 ID，手填 ID 并不证明页面归属。
        with patch('workbench_app_jobs.Adb') as factory:
            for kind in ('hotel', 'sight'):
                for bad in ('', '0', '-1', 'abc', '１２３', '9' * 21, 123, True):
                    with self.subTest(kind=kind, bad=bad), self.assertRaises(ValueError):
                        prepare_app_job({'mode': 'app_reviews', 'kind': kind, 'name': '对象', 'resource_id': bad}, '.', 'adb.exe')
            factory.assert_not_called()
        logger.info('评论对象 ID 检查完成')

    def test_error_and_interrupted_state_are_not_marked_success(self):
        # /* ===== 步骤1：核验异常；数据源：采集器失败；操作：1) 保存错误 2) 取消正确传递 ===== */
        logger.info('开始异常状态测试')
        request = {'mode': 'app_gallery', 'kind': 'hotel', 'name': '酒店', 'resource_id': '123'}
        with patch('workbench_app_jobs.Adb') as factory, patch('workbench_app_jobs.capture_ai_gallery') as gallery:
            gallery.side_effect = InterruptedError('取消')
            job = prepare_app_job(request, '.', 'adb.exe')
            self.assertEqual(job(Event(), lambda _: None)['reason'], 'cancelled')
            gallery.side_effect = RuntimeError('设备断开')
            with self.assertRaisesRegex(RuntimeError, '设备断开'):
                job(Event(), lambda _: None)
            self.assertEqual(factory.return_value.__exit__.call_count, 2)
            # 1.1 底层返回错误报告时，任务管理器也要显示错误而不是完成。
            from workbench_tasks import TaskManager
            gallery.side_effect = None
            gallery.return_value = {'status': 'error', 'error': '页面不匹配', 'complete': False}
            manager = TaskManager()
            manager.start(job)
            manager.worker.join(2)
            self.assertEqual(manager.snapshot()['status'], 'error')
            self.assertEqual(manager.snapshot()['error'], '页面不匹配')
        logger.info('异常状态测试完成')
