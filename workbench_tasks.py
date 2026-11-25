"""Single-worker task state shared by the local workbench."""
import copy
import logging
from threading import Event, Lock, Thread

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self):
        self.lock = Lock()
        self.stop = Event()
        self.worker = None
        self.closed = False
        self.state = {'status': 'idle', 'messages': [], 'result': None, 'error': '', 'stop_requested': False}

    def snapshot(self):
        # /* ===== 步骤1：读取状态；数据源：后台任务；操作：1) 加锁 2) 返回独立快照 ===== */
        logger.info('开始读取任务状态')
        with self.lock:
            result = copy.deepcopy(self.state)
        logger.info('任务状态读取完成')
        return result

    def start(self, job):
        # /* ===== 步骤1：启动后台任务；数据源：采集函数；操作：1) 限制单任务 2) 初始化状态 ===== */
        logger.info('开始创建工作台任务')
        with self.lock:
            if self.closed:
                raise RuntimeError('工作台正在关闭，无法启动任务')
            if self.worker is not None and self.worker.is_alive():
                raise RuntimeError('已有任务运行，请先停止并等待保存完成')
            self.stop = Event()
            self.state = {'status': 'running', 'messages': [], 'result': None, 'error': '', 'stop_requested': False}
            # 1.1 采集函数接收现有的取消事件与进度回调。
            self.worker = Thread(target=self._run, args=(job,), daemon=True)
            self.worker.start()
        logger.info('工作台任务创建完成')

    def _run(self, job):
        # /* ===== 步骤1：采集并记录结果；数据源：实际采集函数；操作：1) 保留异常 2) 不推断全量完成 ===== */
        logger.info('开始运行工作台任务')
        try:
            result = job(self.stop, self._emit)
            reason = result.get('reason', result.get('status', '')) if isinstance(result, dict) else ''
            # 1.0 导出失败不覆盖原始报告，但必须在工作台显式显示错误。
            error = (result.get('error') or result.get('export_error') or '') if isinstance(result, dict) else ''
            # 1.1 身份未核验始终显示停止原因，缺少详情时保留导出错误。
            if reason == 'identity_unverified' and not result.get('error'):
                error = '景点页面身份未核验，采集已停止。' + (f' 导出失败：{error}' if error else '')
            with self.lock:
                # 1.2 手机采集器使用 stopped_by_user；原始报告仍完整保留。
                if self.state['stop_requested'] and not error and reason not in {'error', 'cancelled', 'stopped_by_user'}:
                    # 1.3 不可中断步骤在停止请求后完成，明确显示时序而不虚报已取消。
                    message = '收到停止请求后，当前步骤已完成并保存结果。'
                    self.state['messages'] = (self.state['messages'] + [message])[-200:]
                self.state.update(result=result, error=error,
                                  status='error' if error or reason == 'error' else 'cancelled' if reason in {'cancelled', 'stopped_by_user'} else 'finished')
        except Exception as exc:
            with self.lock:
                self.state.update(status='error', error=str(exc))
        logger.info('工作台任务运行结束')

    def _emit(self, message):
        # /* ===== 步骤1：保存进度；数据源：采集回调；操作：1) 有界缓冲 2) 保留最近消息 ===== */
        logger.info('开始保存任务进度')
        with self.lock:
            self.state['messages'] = (self.state['messages'] + [str(message)])[-200:]
        logger.info('任务进度保存完成')

    def cancel(self):
        # /* ===== 步骤1：请求取消；数据源：用户操作；操作：1) 发出事件 2) 等待采集器保存 ===== */
        logger.info('开始请求停止任务')
        with self.lock:
            if self.state['status'] == 'running':
                self.state['status'] = 'stopping'
                self.state['stop_requested'] = True
                self.stop.set()
        logger.info('停止请求已发出')

    def close(self):
        # /* ===== 步骤1：关闭任务管理；数据源：服务退出；操作：1) 禁止新任务 2) 请求取消并等待保存 ===== */
        logger.info('开始关闭任务管理，等待当前任务保存')
        with self.lock:
            # 1.1 加锁标记关闭，防止等待期间有新请求启动任务。
            self.closed = True
            if self.state['status'] == 'running':
                self.state['status'] = 'stopping'
                self.state['stop_requested'] = True
                self.stop.set()
            worker = self.worker
        # 1.2 在锁外等待采集器结束，允许采集器提交结果和状态；不静默超时退出。
        if worker is not None:
            worker.join()
        logger.info('任务管理关闭完成')
