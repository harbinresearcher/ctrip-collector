"""使用独立资料启动酒店浏览器，缺少Edge时回退到系统Chrome。"""
import logging
import os
from pathlib import Path

from playwright.sync_api import Error

logger = logging.getLogger(__name__)


def open_hotel_context(playwright):
    # /* ===== 步骤1：启动酒店浏览器；数据源：系统浏览器；操作：1) 优先Edge 2) 缺失时使用独立Chrome资料 ===== */
    logger.info('开始启动酒店浏览器')
    base=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'CtripCollector'
    try:
        # 1.1 仅缺少Edge时切换，避免把登录或资料占用错误当作需要切换浏览器。
        context=playwright.chromium.launch_persistent_context(str(base/'browser-profile'),channel='msedge',headless=False)
    except Error as exc:
        if "distribution 'msedge' is not found" not in str(exc):
            raise
        logger.info('未找到Edge，使用独立Chrome资料目录')
        context=playwright.chromium.launch_persistent_context(str(base/'browser-profile-chrome'),channel='chrome',headless=False)
    logger.info('酒店浏览器启动完成')
    return context
