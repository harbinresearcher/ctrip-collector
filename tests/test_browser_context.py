import logging
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error
from browser_context import open_hotel_context

logger = logging.getLogger(__name__)


class BrowserContextTests(unittest.TestCase):
    def test_missing_edge_uses_separate_chrome_profile(self):
        # /* ===== 步骤1：验证浏览器回退；数据源：Edge不可用；操作：1) 选择Chrome 2) 独立保存登录态 ===== */
        logger.info('开始浏览器回退测试')
        playwright=Mock()
        context=Mock()
        playwright.chromium.launch_persistent_context.side_effect=[Error("Chromium distribution 'msedge' is not found"),context]
        # 1.1 Chrome不复用Edge资料，也不访问用户日常浏览器目录。
        with patch.dict('os.environ',{'LOCALAPPDATA':'C:/collector-test'}):
            self.assertIs(open_hotel_context(playwright),context)
        calls=playwright.chromium.launch_persistent_context.call_args_list
        self.assertEqual([call.kwargs['channel'] for call in calls],['msedge','chrome'])
        self.assertTrue(calls[0].args[0].endswith('browser-profile'))
        self.assertTrue(calls[1].args[0].endswith('browser-profile-chrome'))
        logger.info('浏览器回退测试完成')

    def test_non_missing_edge_error_does_not_switch_browser(self):
        # /* ===== 步骤2：验证异常边界；数据源：资料占用等错误；操作：1) 保留异常 2) 不切换身份 ===== */
        logger.info('开始浏览器异常测试')
        playwright=Mock()
        playwright.chromium.launch_persistent_context.side_effect=Error('profile locked')
        # 2.1 资料目录占用等错误不应被伪装成缺少Edge。
        with self.assertRaises(Error): open_hotel_context(playwright)
        self.assertEqual(playwright.chromium.launch_persistent_context.call_count,1)
        logger.info('浏览器异常测试完成')
