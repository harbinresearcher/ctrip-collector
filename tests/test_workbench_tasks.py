import logging
import unittest
from threading import Event, Thread
from workbench_tasks import TaskManager

logger = logging.getLogger(__name__)


class TaskTests(unittest.TestCase):
    def test_unverified_identity_is_error_and_keeps_report(self):
        # /* ===== 步骤1：核验身份异常；数据源：手机采集报告；操作：1) 显示原因 2) 保留已采结果 ===== */
        logger.info('开始身份异常状态测试')
        for detail in ('景点名称不一致', ''):
            # 1.1 没有错误说明时也不能显示正常结束。
            with self.subTest(detail=detail):
                report = {'reason': 'identity_unverified', 'count': 3, 'error': detail}
                manager = TaskManager()
                manager.start(lambda stop, emit: report)
                manager.worker.join(2)
                state = manager.snapshot()
                self.assertEqual(state['status'], 'error')
                self.assertTrue(state['error'])
                if detail:
                    self.assertEqual(state['error'], detail)
                self.assertEqual(state['result'], report)
        # 1.2 身份错误和导出错误同时出现时，两项都要展示。
        report = {'reason': 'identity_unverified', 'error': '', 'export_error': '文件被占用'}
        manager = TaskManager()
        manager.start(lambda stop, emit: report)
        manager.worker.join(2)
        state = manager.snapshot()
        self.assertEqual(state['status'], 'error')
        self.assertIn('身份', state['error'])
        self.assertIn('文件被占用', state['error'])
        self.assertEqual(state['result'], report)
        logger.info('身份异常状态测试完成')

    def test_export_error_is_visible_and_original_result_is_preserved(self):
        # /* ===== 步骤1：核验导出异常；数据源：手机采集报告；操作：1) 展示错误 2) 保留已有数据报告 ===== */
        logger.info('开始导出异常状态测试')
        # 1.1 数据已采集，但CSV被占用时不能向界面报告正常完成。
        for reason in ('target_reached', 'stopped_by_user'):
            with self.subTest(reason=reason):
                report = {'reason': reason, 'count': 3, 'csv': None,
                          'error': '', 'export_error': 'CSV文件被占用'}
                manager = TaskManager()
                manager.start(lambda stop, emit: report)
                manager.worker.join(2)
                self.assertFalse(manager.worker.is_alive())
                state = manager.snapshot()
                self.assertEqual(state['status'], 'error')
                self.assertEqual(state['error'], 'CSV文件被占用')
                self.assertEqual(state['result'], report)
        logger.info('导出异常状态测试完成')

    def test_non_interruptible_step_finished_after_stop_preserves_timing(self):
        # /* ===== 步骤1：核对停止时序；数据源：不可中断步骤；操作：1) 请求停止 2) 保留实际完成结果 ===== */
        logger.info('开始不可中断步骤停止测试')
        manager = TaskManager()
        entered, release = Event(), Event()
        report = {'name': '景点', 'file': 'result.json', 'complete': False}
        def job(stop, emit):
            entered.set()
            release.wait(2)
            return report
        # 1.1 停止发生在读取期间，之后结果正常完成不能虚报为已取消。
        manager.start(job)
        try:
            self.assertTrue(entered.wait(2))
            manager.cancel()
            self.assertEqual(manager.snapshot()['status'], 'stopping')
        finally:
            release.set()
            manager.worker.join(2)
        state = manager.snapshot()
        self.assertEqual(state['status'], 'finished')
        self.assertTrue(state['stop_requested'])
        self.assertEqual(state['result'], report)
        self.assertTrue(any('停止' in message and '完成' in message for message in state['messages']))
        logger.info('不可中断步骤停止测试完成')

    def test_phone_user_stop_keeps_original_report(self):
        # /* ===== 步骤1：核验手机采集停止状态；数据源：手机采集报告；操作：1) 保留原报告 2) 显示已停止 ===== */
        logger.info('开始手机采集停止状态测试')
        manager = TaskManager()
        report = {'reason': 'stopped_by_user', 'count': 3, 'complete': False}
        # 1.1 保留实际报告中的原因和数量，不把用户停止误记成任务结束。
        manager.start(lambda stop, emit: report)
        manager.worker.join(2)
        self.assertFalse(manager.worker.is_alive())
        state = manager.snapshot()
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(state['result'], report)
        logger.info('手机采集停止状态测试完成')

    def test_close_waits_and_rejects_new_tasks(self):
        # /* ===== 步骤1：核对关闭竞争；数据源：受控后台任务；操作：1) 等待保存 2) 拒绝新任务 ===== */
        logger.info('开始关闭竞争测试')
        manager = TaskManager()
        entered, saving, release = Event(), Event(), Event()
        def job(stop, emit):
            entered.set()
            stop.wait(2)
            saving.set()
            release.wait(2)
            return {'reason': 'cancelled'}
        # 1.1 在任务保存期间尝试提交另一任务，不能破坏原任务状态。
        manager.start(job)
        self.assertTrue(entered.wait(2))
        closer = Thread(target=manager.close)
        closer.start()
        try:
            self.assertTrue(saving.wait(2))
            self.assertTrue(closer.is_alive())
            with self.assertRaises(RuntimeError):
                manager.start(job)
        finally:
            release.set()
            closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(manager.snapshot()['status'], 'cancelled')
        logger.info('关闭竞争测试完成')

    def test_real_progress_cancel_and_single_worker(self):
        # /* ===== 步骤1：核验工作台任务；数据源：受控后台函数；操作：1) 进度 2) 排他 3) 取消 ===== */
        logger.info('开始任务管理测试')
        entered = Event()
        manager = TaskManager()
        def job(stop, emit):
            emit('读取第1页')
            entered.set()
            stop.wait(3)
            return {'reason': 'cancelled', 'count': 3}
        # 1.1 任务未结束时拒绝启动第二任务，取消后保留已取得结果。
        manager.start(job)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(RuntimeError):
            manager.start(job)
        self.assertIn('读取第1页', manager.snapshot()['messages'])
        manager.cancel()
        manager.worker.join(2)
        self.assertEqual(manager.snapshot()['status'], 'cancelled')
        self.assertEqual(manager.snapshot()['result']['count'], 3)
        logger.info('任务管理测试完成')
