import json
import logging
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from core import Store, bounds, parse_reviews

logger = logging.getLogger(__name__)


class Adb:
    def __init__(self, executable, serial=None, ui_backend='adb'):
        self.executable = str(executable)
        self.serial = serial
        self.ui_backend = ui_backend
        self._ui = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        # /* ===== 步骤5：释放快速读取服务；数据源：本次连接；操作：1) 释放自动化占用 2) 清除引用 ===== */
        logger.info('开始释放页面读取服务')
        if self._ui is not None:
            try:
                self._ui.stop_uiautomator()
            except Exception as exc:
                logger.warning('页面读取服务释放失败：%s', exc)
            finally:
                self._ui = None
        logger.info('页面读取服务释放完成')

    def run(self, *args, timeout=35):
        command = [self.executable] + (['-s', self.serial] if self.serial else []) + list(args)
        result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        return result.stdout

    def connect(self):
        # /* ============================================================
        # 步骤1：选择已授权设备
        # 数据源：ADB；操作：1) 检查唯一设备 2) 校验携程安装
        # ============================================================ */
        logger.info('开始检查设备')
        # 1.1 多设备不猜测目标，未授权设备不继续。
        devices = [line.split() for line in self.run('devices').splitlines()[1:] if line.strip()]
        if len(devices) != 1 or devices[0][1] != 'device':
            raise RuntimeError('请只连接一台手机，开启 USB 调试并允许电脑授权。')
        self.serial = devices[0][0]
        if 'package:ctrip.android.view' not in self.run('shell', 'pm', 'list', 'packages', 'ctrip.android.view'):
            raise RuntimeError('手机未安装携程。')
        logger.info('设备检查完成')
        return self.serial

    def snapshot(self):
        # /* ============================================================
        # 步骤2：读取新页面证据
        # 数据源：UIAutomator；操作：1) 每次使用唯一文件 2) 仅成功后读取
        # ============================================================ */
        logger.info('开始读取页面')
        if self.ui_backend == 'uiautomator2':
            # 2.0 快速服务复用连接；多窗口 XML 只保留携程，避免状态栏或其他 App 混入。
            if self._ui is None:
                import uiautomator2
                if not self.serial:
                    self.connect()
                try:
                    self._ui = uiautomator2.connect(self.serial)
                except Exception as exc:
                    raise RuntimeError('快速读取服务连接失败；请关闭其他手机自动化工具，或改用 ADB 模式。') from exc
            parsed = ET.fromstring(self._ui.dump_hierarchy(compressed=False))
            windows = [node for node in parsed if node.get('package') == 'ctrip.android.view']
            if not windows:
                raise RuntimeError('快速读取没有携程窗口，已停止')
            windows.sort(key=lambda n: (bounds(n)[2] - bounds(n)[0]) * (bounds(n)[3] - bounds(n)[1]), reverse=True)
            filtered = ET.Element('hierarchy')
            filtered.extend(windows)
            logger.info('快速页面读取完成')
            return ET.tostring(filtered, encoding='unicode')
        remote = '/sdcard/Download/ctrip-collector-' + uuid.uuid4().hex + '.xml'
        try:
            # 2.1 不依赖退出码判断 dump 成功，避免复用旧文件。
            message = self.run('shell', 'uiautomator', 'dump', remote)
            if 'dumped to:' not in message:
                raise RuntimeError('页面读取失败：' + message.strip())
            xml = self.run('shell', 'cat', remote)
            ET.fromstring(xml)
            logger.info('页面读取完成')
            return xml
        finally:
            # 2.2 只删除本次创建的唯一远端文件。
            try:
                self.run('shell', 'rm', '-f', remote, timeout=5)
            except (RuntimeError, subprocess.TimeoutExpired):
                logger.warning('临时页面文件未清理')

    def ensure_foreground(self):
        state = self.run('shell', 'dumpsys', 'window')
        focused = [line for line in state.splitlines() if 'mCurrentFocus=' in line]
        if not focused or 'ctrip.android.view/' not in focused[0]:
            raise RuntimeError('携程不在前台，已停止操作手机。')


def collect(adb, kind, place, folder, limit=500, max_swipes=1500, stop=None, progress=None):
    # /* ================================================================
    # 步骤3：按屏幕顺序采集并持久化
    # 目标：当前对象；操作：1) 保存 XML 和评论 2) 小幅滑动保留卡片重叠
    # ================================================================ */
    logger.info('开始连续采集：%s', place)
    stop = stop or threading.Event()
    progress = progress or (lambda message: logger.info('%s', message))
    folder = Path(folder)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]
    raw = folder / 'raw' / run_id
    raw.mkdir(parents=True)
    store = Store(folder)
    reason, error, unchanged, empty, screens = 'screen_budget_reached', '', 0, 0, 0
    initial = store.count(kind, place)
    previous = None
    try:
        for index in range(max_swipes + 1):
            # 3.1 先查停止条件；达到目标不代表全量完成。
            if stop.is_set():
                reason = 'stopped_by_user'
                break
            if store.count(kind, place) >= limit:
                reason = 'target_reached'
                break
            adb.ensure_foreground()
            xml = adb.snapshot()
            source = raw / f'{index:05d}.xml'
            source.write_text(xml, encoding='utf-8')
            screens += 1
            root = ET.fromstring(xml)
            texts = [n.get('text', '') for n in root.iter('node') if n.get('text')]
            if any(t in ('安全验证', '请完成验证', '拖动滑块完成拼图', '请先登录') for t in texts):
                reason = 'requires_manual_action'
                break
            if '全部' not in texts or not any(t in ('好评', '差评', '有图·视频') for t in texts):
                raise RuntimeError('未识别为评论页面，请先在手机打开目标对象的全部评论。')
            rows = parse_reviews(xml, kind)
            store.save(kind, place, rows, str(source.relative_to(folder)))
            total = store.count(kind, place)
            progress(f'已读取 {screens} 屏；本对象累计 {total} 条；本次新增 {total - initial} 条')
            # 3.2 页面不变和无卡片分别计数，不能把无新增当作已抓全。
            signature = tuple(texts)
            unchanged = unchanged + 1 if signature == previous else 0
            previous = signature
            empty = empty + 1 if not rows else 0
            if unchanged >= 5 or empty >= 15:
                reason = 'stalled_unverified'
                break
            if total >= limit:
                reason = 'target_reached'
                break
            if index == max_swipes:
                break
            # 3.3 使用当前屏幕尺寸，小幅滑动；再次检查前台避免误操作其他应用。
            x1, y1, x2, y2 = bounds(next(root.iter('node')))
            width, height = x2 - x1, y2 - y1
            if width <= 0 or height <= 0:
                raise RuntimeError('无法识别屏幕尺寸')
            if stop.wait(1.5):
                reason = 'stopped_by_user'
                break
            adb.ensure_foreground()
            adb.run('shell', 'input', 'swipe', str(x1 + width // 2), str(y1 + int(height * .78)),
                    str(x1 + width // 2), str(y1 + int(height * .58)), '450')
            if stop.wait(1):
                reason = 'stopped_by_user'
                break
    except Exception as exc:
        reason, error = 'error', str(exc)
        logger.exception('采集中止')
    finally:
        # /* ============================================================
        # 步骤4：生成本次报告
        # 数据源：已提交数据库；操作：1) 导出 CSV 2) 记录停止原因与限制
        # ============================================================ */
        logger.info('开始保存运行报告')
        try:
            count = store.count(kind, place)
            exported, export_error = None, ''
            # 4.1 CSV 被表格软件占用时，仍然保存本次采集报告。
            try:
                exported = str(store.export(kind, place))
            except OSError as exc:
                export_error = str(exc)
                logger.warning('CSV 导出失败，数据库已保留：%s', export_error)
            report = {'kind': kind, 'place': place, 'count': count, 'new_count': count - initial,
                      'screens': screens, 'reason': reason, 'error': error, 'target': limit,
                      'complete': False, 'csv': exported, 'export_error': export_error, 'run_id': run_id,
                      'limitations': ['仅当前对象；无全站发现', '未验证全量；未下载图片',
                                      '折叠文本需核对；has_expand=1 不保证完整',
                                      '内容指纹去重，不是服务端评论ID；可能合并同名同日同文评论',
                                      '恢复已保存数据，不自动恢复手机滚动位置']}
            (folder / f'report-{run_id}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info('运行报告保存完成：%s', reason)
        finally:
            store.close()
    logger.info('连续采集结束')
    return report
