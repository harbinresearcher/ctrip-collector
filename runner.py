import json
import logging
import re
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from core import Store, bounds, parse_reviews
from mobile_scoped_store import IDENTITY_SOURCES, MOBILE_REVIEW_RUN_ID, OPERATOR_DECLARED, MobileScopedStore
from mobile_expansion import expand_card, expansion_target
from mobile_review_context import read_review_context, summarize_review_count
from mobile_review_fields import SHOPPING_DIMENSION, SHOPPING_RATING_FIELDS
from ai_capture import parse_ai, persist_app_ai_summary
from hotel_review_thumbnails import ReviewThumbnails
from layout_fingerprint import attach_layout, mobile_family, structure_fingerprint
from review_screen import review_screen_state

logger = logging.getLogger(__name__)

# 到达判据的提示文案：只说清楚缺哪一条，不猜「大概到了」。
ARRIVAL_HINT = {
    'no_review_anchor': '当前屏幕不是点评列表（没找到点评列表锚点），请先在手机打开目标对象的全部评论。',
    'name_mismatch': '当前点评页的对象名与目标不一致，请确认手机停在正确的对象上。',
    'no_all_count': '点评页没有显示「全部」计数，无法确认总数；请切到「全部」页签后重试。',
    'name_partial_match': '屏幕标题只是包含目标名（可能是另一个对象），请确认打开的是目标对象。',
    'invalid_xml': '读取到的界面不是有效的 XML，无法判断是否到达点评页。',
}



class AndroidReadinessError(RuntimeError):
    def __init__(self, state, message):
        super().__init__(message)
        self.state = state


class Adb:
    def __init__(self, executable, serial=None, ui_backend='adb', *, command_timeout=35):
        self.executable = str(executable)
        self.serial = serial
        self.ui_backend = ui_backend
        self.command_timeout = command_timeout
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

    def run(self, *args, timeout=None):
        selector = ['-s', self.serial] if self.serial and args != ('devices',) else []
        command = [self.executable, *selector, *args]
        result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                timeout=self.command_timeout if timeout is None else timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        return result.stdout

    def connect(self):
        # /* ============================================================
        # 步骤1：选择已授权设备
        # 数据源：ADB；操作：1) 检查唯一设备 2) 校验携程安装
        # ============================================================ */
        logger.info('开始检查设备')
        devices = {}
        listing = self.run('devices').splitlines()
        if 'List of devices attached' not in listing:
            raise AndroidReadinessError('check_failed', '未读到 Android 设备列表，请重试。')
        for line in listing:
            if '\t' in line and len(parts := line.split()) >= 2:
                devices[parts[0]] = parts[1]
        if self.serial is None and len(devices) > 1:
            raise AndroidReadinessError('ambiguous', '请只连接一台 Android 设备，或在工作台启动时指定设备。')
        selected = self.serial if self.serial is not None else next(iter(devices), None)
        status = devices.get(selected)
        if status != 'device':
            label = f'指定的 Android 设备 {self.serial}' if self.serial is not None else 'Android 设备'
            if status == 'unauthorized':
                raise AndroidReadinessError('unauthorized', f'{label}未授权，请在设备上允许 USB 调试。')
            if status == 'no':
                raise AndroidReadinessError('unauthorized', f'{label}未授权，请检查设备连接权限。')
            if status is not None:
                raise AndroidReadinessError('offline', f'{label}暂不可用，请等待设备启动或重新连接。')
            raise AndroidReadinessError('disconnected', f'{label}未连接。')
        self.serial = selected
        packages = self.run('shell', 'pm', 'list', 'packages', 'ctrip.android.view')
        if 'package:ctrip.android.view' not in {line.strip() for line in packages.splitlines()}:
            raise AndroidReadinessError('not_installed', '选定的 Android 设备未安装携程。')
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
            # 2.0.2 快速读取也会偶发读不到携程窗口（转场动画、页面正在重建）：
            #       重试两次再报错，避免把「正在切页」当成「App 没了」。
            parsed = None
            for attempt in range(3):
                try:
                    parsed = ET.fromstring(self._ui.dump_hierarchy(compressed=False))
                except Exception as exc:
                    if attempt >= 2:
                        raise RuntimeError('快速读取页面失败：' + str(exc)[:80] + '；请确认携程停目标页面后重试。') from exc
                    time.sleep(0.6)
                    continue
                windows = [node for node in parsed if node.get('package') == 'ctrip.android.view']
                if windows:
                    break
                if attempt >= 2:
                    raise RuntimeError('快速读取没有携程窗口，已停止')
                time.sleep(0.6)
            windows = [node for node in parsed if node.get('package') == 'ctrip.android.view']
            if not windows:
                raise RuntimeError('快速读取没有携程窗口，已停止')
            windows.sort(key=lambda n: (bounds(n)[2] - bounds(n)[0]) * (bounds(n)[3] - bounds(n)[1]), reverse=True)
            filtered = ET.Element('hierarchy')
            filtered.extend(windows)
            logger.info('快速页面读取完成')
            return ET.tostring(filtered, encoding='unicode')
        # 2.0.1 真机会偶发 'null root node returned by UiTestAutomationBridge'（屏幕刚亮起、
        #       转场动画未结束、或 uiautomator 服务抖动）。重试一次，仍失败再报错，并说清可能原因。
        attempt = 0
        while True:
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
            except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
                attempt += 1
                if attempt >= 2:
                    raise RuntimeError(
                        '页面读取失败：' + str(exc).strip()
                        + '；若手机锁屏或应用无响应，请唤醒屏幕并确认携程停目标页面后重试。') from exc
                logger.warning('页面读取失败，重试一次：%s', exc)
                self.run('shell', 'input', 'keyevent', 'KEYCODE_WAKEUP', timeout=10)
                time.sleep(1.5)
                continue
            finally:
                # 2.2 只删除本次创建的唯一远端文件。
                try:
                    self.run('shell', 'rm', '-f', remote, timeout=5)
                except (RuntimeError, subprocess.TimeoutExpired):
                    logger.warning('临时页面文件未清理')

    def enter_text(self, text):
        if self._ui is None:
            raise RuntimeError('当前连接无法向 App 搜索框输入文本。')
        self.ensure_foreground()
        self._ui.send_keys(text, clear=True)

    def ensure_foreground(self):
        # 3.1.0.1 模拟器在页面切换瞬间会读不到焦点窗口（dumpsys 返回空）：
        #         重试两次再判失败，避免把「正在切页」当成「用户把 App 切走了」。
        state = ''
        for attempt in range(3):
            state = self.run('shell', 'dumpsys', 'window')
            focused = next((line.partition('=')[2].strip() for line in state.splitlines()
                            if line.strip().startswith('mCurrentFocus=')), '')
            if focused and focused != 'null':
                break
            if attempt < 2:
                # 等待要短：工作台的前台检查有 2 秒超时，重试不能把请求拖过超时。
                time.sleep(0.5)
        if not focused or focused == 'null':
            raise AndroidReadinessError('focus_unknown', '无法确认携程在前台，已停止操作 Android 设备。')
        if not re.search(r'(?:^|\s)ctrip\.android\.view/', focused):
            raise AndroidReadinessError('background', '携程不在前台，已停止操作 Android 设备。')


def sight_screen_title(root):
    # /* ================================================================
    # 步骤1：识别景点评论页标题
    # 数据源：本屏携程 XML；操作：1) 核对页面标签 2) 按已验证的导航结构取唯一标题
    # ================================================================ */
    logger.info('开始核验景点页面标题')
    # 1.1 只识别实测页面，其他页面结构不猜测对象名称。
    texts = {node.get('text', '') for node in root.iter('node') if node.get('package') == 'ctrip.android.view'}
    if not {'用户点评', '景点详情'}.issubset(texts):
        logger.info('景点页面标题核验结束：页面标签不匹配')
        return None
    screen = next(root.iter('node'), None)
    if screen is None:
        logger.info('景点页面标题核验结束：缺少页面范围')
        return None
    x1, y1, x2, y2 = bounds(screen)
    height = y2 - y1
    if x2 <= x1 or height <= 0:
        logger.info('景点页面标题核验结束：页面范围无效')
        return None
    # 1.2 限定顶部导航的直接子节点，避免把评论正文当成对象名称。
    titles = []
    for node in root.iter('node'):
        left, top, right, bottom = bounds(node)
        children = list(node)
        if (node.get('package') != 'ctrip.android.view' or node.get('class') != 'android.view.ViewGroup'
                or left != x1 or right != x2 or top != y1 or not top < bottom <= y1 + height * .2
                or len(children) not in (3, 4)):
            continue
        expected_controls = ['\ue943', '\ue957', '\ue939'] if len(children) == 4 else ['\ue943', '\ue939']
        controls = children[:1] + children[2:]
        if (any(child.get('package') != 'ctrip.android.view' for child in children)
                or [child.get('index') for child in children] != [str(i) for i in range(len(children))]
                or children[1].get('class') != 'android.widget.TextView'
                or [child.get('content-desc') for child in controls] != expected_controls):
            continue
        title = children[1].get('text', '').strip()
        if title:
            titles.append(title)
    result = titles[0] if len(titles) == 1 else None
    logger.info('景点页面标题核验结束：%s', '唯一标题' if result else '标题不明确')
    return result


def restaurant_review_screen(root):
    app = 'ctrip.android.view'
    scene_matches = []
    for window in root:
        if window.get('package') != app:
            continue
        left, top, right, bottom = bounds(window)
        for scene in window.iter('node'):
            children = list(scene)
            if scene.get('package') != app or len(children) != 4:
                continue
            toolbar, _, scroll, _ = children
            header = list(toolbar)
            x1, y1, x2, y2 = bounds(toolbar)
            scroll_left, scroll_top, scroll_right, scroll_bottom = bounds(scroll)
            if (toolbar.get('class') != 'android.view.ViewGroup'
                    or scroll.get('class') != 'android.widget.ScrollView'
                    or scroll.get('scrollable') != 'true'
                    or any(node.get('package') != app for node in (toolbar, scroll, *header))
                    or len(header) != 3 or x1 != left or x2 != right
                    or not top <= y1 < y2 <= top + (bottom - top) * .2
                    or (scroll_left, scroll_top, scroll_right) != (left, y2, right)
                    or not scroll_top < scroll_bottom <= bottom):
                continue
            back, title, share = header
            if (back.get('content-desc') != '\ue943' or share.get('content-desc') != '\ue939'
                    or title.get('class') != 'android.widget.TextView'):
                continue
            name = title.get('text', '').strip()
            context = read_review_context(scroll, 'restaurant')
            labels = {entry['label'] for entry in context['visible_filters']}
            # The observed food-review filter bar is attached to the scroll view, including after scrolling.
            if name and '全部' in labels and labels & {'好评', '中评', '差评', '有图·视频', '图片/视频',
                                                     '图/视频', '带图'}:
                scene_matches.append((name, scroll, context))
    return scene_matches[0] if len(scene_matches) == 1 else (None, None, None)


def shopping_review_screen(root):
    app = 'ctrip.android.view'
    matches = []
    for window in root:
        if window.get('package') != app:
            continue
        left, top, right, bottom = bounds(window)
        for scene in window.iter('node'):
            if scene.get('package') != app or len(scene) != 5:
                continue
            _, toolbar, _, scroll, footer = scene
            if (any(node.get('package') != app for node in (toolbar, scroll, footer, *toolbar))
                    or toolbar.get('class') != 'android.view.ViewGroup' or len(toolbar) != 4
                    or scroll.get('class') != 'android.widget.ScrollView' or scroll.get('scrollable') != 'true'
                    or len(scroll) != 1 or scroll[0].get('package') != app):
                continue
            x1, y1, x2, y2 = bounds(toolbar)
            sx1, sy1, sx2, sy2 = bounds(scroll)
            fx1, fy1, fx2, fy2 = bounds(footer)
            if ((x1, x2) != (left, right) or not top <= y1 < y2 <= top + (bottom - top) * .2
                    or (sx1, sy1, sx2) != (left, y2, right)
                    or (fx1, fy1, fx2) != (left, sy2, right) or not y2 < sy2 < fy2 <= bottom):
                continue
            back, caption, title, share = toolbar
            if (back.get('content-desc') != '\ue943' or share.get('content-desc') != '\ue939'
                    or any(node.get('class') != 'android.widget.TextView' for node in (caption, title))
                    or caption.get('text') != '点评' or not title.get('text', '').strip()
                    or not any(node.get('package') == app and node.get('text') == '写点评' for node in footer)):
                continue
            context = read_review_context(scroll, 'shopping')
            labels = {entry['label'] for entry in context['visible_filters']}
            visible = {node.get('text', '').strip() for node in scroll.iter('node') if node.get('package') == app}
            dimensions = {match[1] for node in scroll[0] if node.get('package') == app
                          if (match := SHOPPING_DIMENSION.fullmatch(node.get('text', '').strip()))}
            # The filter shelf can scroll away. Per-card dimension labels still distinguish the shopping view.
            if ('总评分' in visible and '全部' in labels) or dimensions == set(SHOPPING_RATING_FIELDS):
                matches.append((title.get('text', '').strip(), scroll, context))
    return matches[0] if len(matches) == 1 else (None, None, None)


def review_date_positions(rows):
    positions = {}
    for row in rows:
        identity = (row['author'], row['date'], row['content'])
        positions[identity] = None if identity in positions else row['review_date_bounds']
    return positions


def reviews_moved_up(before, after, viewport):
    left, top, right, bottom = viewport
    height = bottom - top
    shifts = []
    for identity in before.keys() & after.keys():
        old, current = before[identity], after[identity]
        if old is None or current is None:
            return False
        x1, y1, x2, y2 = current
        if ((old[0], old[2], old[3] - old[1]) != (x1, x2, y2 - y1)
                or not left <= x1 < x2 <= right
                or not (top <= y1 < y2 <= bottom and top <= old[1] < old[3] <= bottom)):
            return False
        shifts.append(old[1] - y1)
    # The normal swipe covers 20% of the screen. Ignore small reflows and require shared anchors to agree.
    return (bool(shifts) and height * .10 <= min(shifts) <= max(shifts) <= height * .35
            and max(shifts) - min(shifts) <= height * .01)


def new_review_run_id():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]


def scan_phone_target(adb, kind, place, folder, object_id=None, identity_source=None, run_id=None,
                      allow_detail_probe=False):
    # /* ===== 步骤2.9：只读手机侧「全部」计数；数据源：当前屏幕；操作：1) 校验到达 2) 记录可见筛选 3) 落盘报告 ===== */
    # 2.9.1 采点评慢（约每屏 0.6 条），但「这个对象到底有多少条点评」只要看一眼。
    #       先补分母，才能判断缺口是「还没采」还是「平台没有」。
    logger.info('开始读取手机侧全部计数')
    if kind not in {'hotel', 'sight', 'restaurant', 'shopping'}:
        raise ValueError('对象类型无效')
    if identity_source is not None and identity_source not in IDENTITY_SOURCES:
        raise ValueError('对象身份来源无效')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    run_id = run_id or new_review_run_id()
    if not MOBILE_REVIEW_RUN_ID.fullmatch(run_id):
        raise ValueError('任务编号无效')
    adb.ensure_foreground()
    # 2.9.0 页面可能还在转场：锚点缺失或读不出结构时多看两屏再判，
    #       别把「正在加载」记成「未知布局」（批量扫描里这类假失败最多）。
    retryable = {'no_review_anchor', 'invalid_xml'}
    for attempt in range(3):
        xml = adb.snapshot()
        state = review_screen_state(xml, place, allow_missing_name=kind == 'hotel')
        if state['arrived'] or state['reason'] not in retryable or attempt >= 2:
            break
        logger.info('页面还没稳定（%s），等待后重试', state['reason'])
        time.sleep(0.8)
    raw = folder / 'raw' / run_id
    raw.mkdir(parents=True, exist_ok=True)
    (raw / '00000.xml').write_text(xml, encoding='utf-8')
    root = ET.fromstring(xml)
    context = read_review_context(root, kind)
    detail_state = None
    if state['arrived']:
        reason, arrival_reason = 'target_scanned', None
    else:
        # 2.9.2 没认出点评页也照样落报告：这是可数的未知布局，不是「没跑过」。
        reason, arrival_reason = 'arrival_failed', state['reason']
        # 2.9.2.1 停在景点详情页但页面上根本没有点评入口（例如「暂无评分」）时，
        #         这既不是未知布局也不是失败：平台就是没有可采的点评。
        if allow_detail_probe:
            try:
                from sight_snapshot import parse_sight_snapshot, sight_detail_without_reviews
                detail_state = parse_sight_snapshot(xml, place)
            except (ValueError, ET.ParseError):
                # 2.9.2.2 只有标题 + 「暂无评分」的简化详情页解析不了，但那同样说明没有点评。
                try:
                    detail_state = sight_detail_without_reviews(xml, place)
                except (ValueError, ET.ParseError):
                    detail_state = None
            if detail_state is not None and (detail_state.get('status') == 'no_reviews'
                                             or not detail_state.get('review_display')):
                reason, arrival_reason = 'no_review_entry', None
    report = {'kind': kind, 'place': place, 'count': 0, 'screens': 1, 'reason': reason,
              'error': '' if state['arrived'] else ARRIVAL_HINT.get(state['reason'], '未识别为评论页面'),
              'arrival_reason': arrival_reason, 'run_id': run_id, 'target_scope': 'target_only',
              # 2.9.3 详情页读到了但页面没有点评入口：留证据，别和「未知布局」混为一谈。
              'detail_seen': detail_state is not None,
              'review_entry_missing': reason == 'no_review_entry',
              'complete': False,
              # 2.9.3 身份没核对上时绝不记数字：屏幕可能停在别的对象上，
              #       把它的「全部 228」记到本对象头上会直接污染分母。
              'sampling_context': ({'selected_filter': context.get('selected_filter'),
                                    'observations': [context],
                                    'scope': 'current_run_ui_controls_only'} if state['arrived'] else None),
              'limitations': ['只读取屏幕上可见的「全部」计数，不采点评',
                              '页面不会把页签标注为已选中，此处只记录看到的数字']}
    if object_id is not None:
        report.update(object_id=str(object_id), identity_source=identity_source, identity_id_verified=False)
    temporary = folder / f'report-{run_id}.json.{uuid.uuid4().hex}.tmp'
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(folder / f'report-{run_id}.json')
    finally:
        temporary.unlink(missing_ok=True)
    logger.info('手机侧全部计数记录完成：%s', reason)
    return report


def collect(adb, kind, place, folder, limit=500, max_swipes=1500, stop=None, progress=None, object_id=None,
            *, expand_reviews=False, refresh=False, save_visible_ai=False, save_review_thumbnails=False,
            identity_source=OPERATOR_DECLARED, identity_evidence=None, sight_navigation=None, run_id=None):
    # /* ================================================================
    # 步骤3：按屏幕顺序采集并持久化
    # 目标：当前对象；操作：1) 保存 XML 和评论 2) 小幅滑动保留卡片重叠
    # ================================================================ */
    logger.info('开始连续采集：%s', place)
    review_scope_reader = {'restaurant': restaurant_review_screen, 'shopping': shopping_review_screen}.get(kind)
    if review_scope_reader is not None and object_id is None:
        raise ValueError('餐饮和购物点评需要填写对象 ID')
    if type(refresh) is not bool or (refresh and object_id is None):
        raise ValueError('重新读取需要按对象编号保存的手机点评任务')
    if save_visible_ai and (kind != 'hotel' or object_id is None):
        raise ValueError('首屏 AI 总结需要按对象编号保存的酒店点评任务')
    if save_review_thumbnails and (kind not in ('hotel', 'restaurant') or object_id is None):
        raise ValueError('点评缩略图需要按对象编号保存的酒店或餐厅点评任务')
    # 身份来源只认固定三态；证据清单说明编号出处，但不等于与手机屏幕比对过。
    if identity_source not in IDENTITY_SOURCES:
        raise ValueError('身份来源必须是 ' + ' / '.join(IDENTITY_SOURCES))
    if identity_evidence is not None and (not isinstance(identity_evidence, str) or not identity_evidence.strip()):
        raise ValueError('身份证据文件名必须是非空文本')
    if identity_source != OPERATOR_DECLARED and object_id is None:
        raise ValueError('证据身份需要按对象编号保存的手机点评任务')
    if sight_navigation is not None and (kind != 'sight' or sight_navigation not in ('opened', 'already_open')):
        raise ValueError('景点点评导航状态无效')
    stop = stop or threading.Event()
    progress = progress or (lambda message: logger.info('%s', message))
    folder = Path(folder)
    if run_id is None:
        run_id = new_review_run_id()
    elif not isinstance(run_id, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(run_id):
        raise ValueError('点评采集任务编号无效')
    thumbnails = ReviewThumbnails(folder, run_id, kind=kind) if save_review_thumbnails else None
    sample_orders = {}
    raw = folder / 'raw' / run_id
    raw.mkdir(parents=True)
    store = (MobileScopedStore(folder, kind, object_id, identity_source=identity_source)
             if object_id is not None else Store(folder))
    # 3.0 新任务按声明的对象 ID 隔离，旧入口继续使用旧表。
    scoped = object_id is not None
    first_screen_ai = {'status': 'not_read'} if save_visible_ai else None
    expand_reviews = expand_reviews and scoped and kind == 'hotel'
    expansion_stats = {'attempted': 0, 'accepted': 0, 'unverified': 0}
    attempted_cards = set()
    observed_keys = {} if scoped else None
    reason, error, unchanged, empty, screens = 'screen_budget_reached', '', 0, 0, 0
    arrival_state = None
    initial = store.count() if scoped else store.count(kind, place)
    total = initial
    previous = None
    track_review_motion = scoped and kind == 'sight'
    previous_motion_frame = None
    title_verified_screens = 0
    contexts = []
    seen_contexts = set()
    selected_filter = sort_order = rejected_context = None
    selection_confirmed = True
    visible_counts = set()
    first_screen_xml = None
    try:
        if scoped:
            store.start_run(run_id, place)
        for index in range(max_swipes + 1):
            # 3.1 先查停止条件；达到目标不代表全量完成。
            if stop.is_set():
                reason = 'stopped_by_user'
                break
            target_count = len(observed_keys) if refresh else total
            if target_count >= limit:
                reason = 'target_reached'
                break
            adb.ensure_foreground()
            xml = adb.snapshot()
            source = raw / f'{index:05d}.xml'
            if review_scope_reader is None:
                source.write_text(xml, encoding='utf-8')
            screens += 1
            root = ET.fromstring(xml)
            # 3.1.1 验证页标签只来自携程窗口，其他应用的覆盖层不能冒充评论页。
            texts = [n.get('text', '') for n in root.iter('node')
                     if n.get('package') == 'ctrip.android.view' and n.get('text')]
            if any(t in ('安全验证', '请完成验证', '拖动滑块完成拼图', '请先登录') for t in texts):
                reason = 'requires_manual_action'
                break
            # 3.1 景点必须逐屏核对标题；无法确认时不解析或写入该屏评论。
            review_xml = xml
            context = None
            if kind == 'sight' or review_scope_reader is not None:
                if review_scope_reader is not None:
                    title, scroll, context = review_scope_reader(root)
                else:
                    title = sight_screen_title(root)
                if not title or '…' in title or '...' in title or title != place.strip():
                    reason = 'identity_unverified'
                    object_type = {'restaurant': '餐厅', 'shopping': '购物地点'}.get(kind, '景点')
                    error = f'{object_type}评论页标题缺失、模糊或与填写名称不一致，已停止采集。'
                    break
                title_verified_screens += 1
                if review_scope_reader is not None:
                    source.write_text(xml, encoding='utf-8')
                    review_xml = ET.tostring(scroll, encoding='unicode')
            # 3.1.2 第一屏留作结构指纹样本；只用于报告，不影响解析。
            if screens == 1:
                first_screen_xml = review_xml
            if review_scope_reader is None and ('全部' not in texts or not any(
                    t in ('好评', '差评', '有图·视频') for t in texts)):
                # 3.1.3 到没到点评页要能说清楚缺哪一条：锚点 / 对象名 / 全部计数。
                state = review_screen_state(review_xml, place,
                                            allow_missing_name=kind == 'hotel')
                # 3.1.4 带上判据状态抛出：报告里要能写出「缺哪一条」，否则未知布局
                #       只会变成一句 error，对账看不出这是布局问题还是网络问题。
                raise AndroidReadinessError(state, ARRIVAL_HINT.get(
                    state['reason'], '未识别为评论页面，请先在手机打开目标对象的全部评论。'))
            if first_screen_ai is not None and index == 0 and not stop.is_set():
                try:
                    summary = parse_ai(xml)
                except ValueError:
                    summary = None
                if summary is None or not (summary['sections'] or summary['lead']):
                    first_screen_ai = {'status': 'not_visible'}
                else:
                    try:
                        stem = persist_app_ai_summary(xml, summary, place, store.object_id, folder,
                                                      review_run_id=run_id)
                        first_screen_ai = {'status': 'saved', 'summary_status': summary['status'],
                                           'source_file': stem.with_suffix('.json').name,
                                           'section_count': len(summary['sections'])}
                    except OSError:
                        logger.exception('首屏 AI 总结写入失败')
                        first_screen_ai = {'status': 'save_failed'}
            if context is None:
                context = read_review_context(root, kind)
            new_filter, new_sort = context['selected_filter'], context['sort_order']
            if new_filter and selected_filter and new_filter != selected_filter:
                reason = 'review_filter_changed'
                error = '检测到点评筛选发生切换，已停止，避免将不同筛选结果混入同一次采集。'
                rejected_context = context
                break
            selected_filter, sort_order = new_filter or selected_filter, new_sort or sort_order
            if context['visible_filters'] or context['latest_control_visible'] or context.get('shopping_review_overview'):
                signature_context = json.dumps(context, ensure_ascii=False, sort_keys=True)
                if signature_context not in seen_contexts and len(contexts) < 30:
                    contexts.append({'screen': screens, **context})
                    seen_contexts.add(signature_context)
            rows = parse_reviews(review_xml, kind, **({'locate_expansion': True} if expand_reviews else {}),
                                 **({'locate_media': True} if thumbnails else {}),
                                 **({'locate_date': True} if track_review_motion else {}))
            moved_forward = False
            if track_review_motion:
                viewport = tuple(bounds(next(root.iter('node'))))
                if rows and {'正在加载', '加载中', '加载失败', '点击重试'}.isdisjoint(texts):
                    positions = review_date_positions(rows)
                    if previous_motion_frame is not None and previous_motion_frame[:2] == (viewport, context):
                        moved_forward = reviews_moved_up(previous_motion_frame[2], positions, viewport)
                    previous_motion_frame = (viewport, context, positions)
                else:
                    previous_motion_frame = None
            previous_total = total
            previous_read_count = len(observed_keys) if refresh else 0
            expansion_attempted = False
            if scoped:
                screen_keys = {}
                store.save(place, rows, str(source.relative_to(folder)), observed_keys=screen_keys, run_id=run_id)
                for review_key, first_source in screen_keys.items():
                    observed_keys.setdefault(review_key, first_source)
                    if thumbnails and review_key not in sample_orders:
                        sample_orders[review_key] = len(sample_orders) + 1
                if thumbnails:
                    candidates = []
                    for row in rows:
                        if (boxes := row.get('media_tile_bounds')) and (key := store.review_key(row)) in screen_keys:
                            candidates.append((key, sample_orders[key], boxes))
                    window = next((node for node in root if node.get('package') == 'ctrip.android.view'), None)
                    viewport = bounds(window) if window is not None else ()
                    if thumbnails.capture(adb, xml, candidates, screens, stop, viewport) is False:
                        reason = 'review_screen_changed'
                        break
            else:
                store.save(kind, place, rows, str(source.relative_to(folder)))
            if new_filter is None:
                selection_confirmed = False
            else:
                visible_counts.update(entry['display_count'] for entry in context['visible_filters']
                                      if entry['label'] == new_filter)
            if expand_reviews:
                screen_node = next((node for node in root.iter('node') if node.get('package') == 'ctrip.android.view'), None)
                untried = [row for row in rows if 'body_control' in row and store.review_key(row) not in attempted_cards]
                target = expansion_target(untried, bounds(screen_node)) if screen_node is not None else None
                if target is not None:
                    expansion_attempted = True
                    attempted_cards.add(store.review_key(target))
                    expansion_stats['attempted'] += 1
                    status, post_xml, expanded = expand_card(adb, target, stop)
                    if post_xml is not None:
                        evidence = raw / f'{index:05d}-expanded.xml'
                        evidence.write_text(post_xml, encoding='utf-8')
                    if status == 'accepted' and not store.save_expansion(target, expanded, str(evidence.relative_to(folder))):
                        status = 'unverified'
                    expansion_stats[status] += 1
            total = store.count() if scoped else store.count(kind, place)
            read_status = f'；本次读到 {len(observed_keys)} 条不同点评' if refresh else ''
            progress(f'已读取 {screens} 屏；本对象累计 {total} 条；本次新增 {total - initial} 条{read_status}')
            # 3.2 页面不变和无卡片分别计数，不能把无新增当作已抓全。
            signature = tuple(texts)
            unchanged = unchanged + 1 if signature == previous else 0
            previous = signature
            empty = empty + 1 if not rows else 0
            if unchanged >= 5 or empty >= 15:
                reason = 'stalled_unverified'
                break
            if (len(observed_keys) if refresh else total) >= limit:
                reason = 'target_reached'
                break
            if index == max_swipes:
                break
            # 3.3 使用当前屏幕尺寸，小幅滑动；再次检查前台避免误操作其他应用。
            x1, y1, x2, y2 = bounds(next(root.iter('node')))
            width, height = x2 - x1, y2 - y1
            if width <= 0 or height <= 0:
                raise RuntimeError('无法识别屏幕尺寸')
            # New rows show the loop is progressing. Still let Android settle after every swipe.
            gained_reviews = len(observed_keys) > previous_read_count if refresh else total > previous_total
            if stop.is_set() or ((expansion_attempted or not (gained_reviews or moved_forward)) and stop.wait(1.5)):
                reason = 'stopped_by_user'
                break
            adb.ensure_foreground()
            if stop.is_set():
                reason = 'stopped_by_user'
                break
            adb.run('shell', 'input', 'swipe', str(x1 + width // 2), str(y1 + int(height * .78)),
                    str(x1 + width // 2), str(y1 + int(height * .58)), '450')
            if stop.wait(1):
                reason = 'stopped_by_user'
                break
    except InterruptedError:
        reason = 'stopped_by_user'
    except AndroidReadinessError as exc:
        # 3.1.5 到达判据不通过单独记 arrival_failed：这不是网络错误，而是「没认出页面」，
        #       对账要能把它数成未知布局，提示补样本或换入口。
        reason, error = 'arrival_failed', str(exc)
        arrival_state = exc.state if isinstance(exc.state, dict) else None
        logger.warning('未识别为点评页：%s', error)
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
            count = store.count() if scoped else store.count(kind, place)
            exported, export_error = None, ''
            # 4.1 CSV 被表格软件占用时，仍然保存本次采集报告。
            try:
                exported = str(store.export() if scoped else store.export(kind, place))
            except OSError as exc:
                export_error = str(exc)
                logger.warning('CSV 导出失败，数据库已保留：%s', export_error)
            sample_csv = None
            if observed_keys:
                try:
                    sample_csv = str(store.export(run_id=run_id, observed_keys=observed_keys))
                except OSError as exc:
                    export_error = '；'.join(filter(None, (export_error, f'本次点评 CSV 导出失败：{exc}')))
                    logger.warning('本次点评 CSV 导出失败，数据库已保留：%s', exc)
            # 4.2 页面仅核验通用评论标签，对象名称由操作者声明，不能标记为自动核验。
            report = {'kind': kind, 'place': place, 'identity_verification': 'operator_declared_unverified',
                      'count': count, 'new_count': count - initial,
                      'screens': screens, 'reason': reason, 'error': error, 'target': limit,
                      'complete': False, 'csv': exported, 'export_error': export_error, 'run_id': run_id,
                      'arrival_reason': (arrival_state or {}).get('reason'),
                      'limitations': ['仅当前对象；无全站发现', '未验证全量；未下载媒体原文件',
                                      '折叠文本需核对；has_expand=1 不保证完整',
                                      '内容指纹去重，不是服务端评论ID；可能合并同名同日同文评论',
                                      '恢复已保存数据，不自动恢复手机滚动位置',
                                      '对象名称由操作者填写；程序未核验评论页面属于该对象']}
            if reason == 'arrival_failed':
                report['limitations'].append('当前屏幕未被识别为点评页，本次没有采集任何数据；需要补样本或手动打开点评页')
            # 4.3 手填 ID 只隔离存储，不证明该页面属于对应对象；证据清单只说明编号出处。
            if scoped:
                report.update(object_id=store.object_id, identity_source=identity_source,
                              identity_id_verified=False, existing_count=initial,
                              sample_csv=sample_csv, sample_csv_count=len(observed_keys) if sample_csv else 0)
                if identity_evidence is not None:
                    report['identity_evidence'] = identity_evidence
                if sample_csv:
                    report['limitations'].append('本次 CSV 按首次读到顺序选择点评；正文或已核验的展开内容来自导出时的本地记录')
                if identity_source == OPERATOR_DECLARED:
                    report['limitations'].append('对象 ID 由操作者填写，未核验页面对象 ID；旧表数据不计入本作用域')
                else:
                    report['limitations'].append(
                        f'对象 ID 来自落盘证据 {identity_evidence or "（未记录文件名）"}，未与手机屏幕比对；旧表数据不计入本作用域')
            if expand_reviews:
                report['review_expansion'] = expansion_stats
            if refresh:
                report.update(refresh_requested=True, target_scope='current_run', read_unique=len(observed_keys))
            if first_screen_ai is not None:
                report['first_screen_ai'] = first_screen_ai
            if sight_navigation is not None:
                report['sight_navigation'] = sight_navigation
            if contexts:
                report['sampling_context'] = {'selected_filter': selected_filter, 'sort_order': sort_order,
                                              'observations': contexts, 'rejected_context': rejected_context,
                                              'scope': 'current_run_ui_controls_only'}
            report['review_count_check'] = summarize_review_count(
                selected_filter, selection_confirmed, visible_counts,
                len(observed_keys) if observed_keys is not None else None)
            # 4.4 只说明本次已核对的屏幕；旧库数据和对象 ID 均未因此验证。
            if kind == 'sight' or review_scope_reader is not None:
                report['identity_verification'] = 'screen_title_name_only' if title_verified_screens else 'operator_declared_unverified'
                report['identity_id_verified'] = False
                report['identity_verification_scope'] = 'current_run_checked_screens_only'
                report['identity_checked_screens'] = title_verified_screens
                report['existing_count'] = initial
                report['limitations'][5] = '仅按本次屏幕标题核对名称；未核验对象 ID、同名对象或旧库数据'
            # 4.4.1 结构指纹只做改版提示；算不出来也不影响本次已采数据。
            if first_screen_xml is not None:
                try:
                    fingerprint = structure_fingerprint(first_screen_xml)
                except Exception:
                    fingerprint = None
                    logger.warning('第一屏页面结构指纹计算失败，采集结果照常保存', exc_info=True)
                attach_layout(report, mobile_family(kind), fingerprint)
            if thumbnails:
                report['review_thumbnails'] = thumbnails.finish(identity_verification=report['identity_verification'])
                report['limitations'].append('缩略图为点评卡片中的屏幕裁图，不是原图或视频')
            # 4.5 报告原子写入；写不进去时保留已采数据并记录失败，不把结果丢掉。
            report_path = folder / f'report-{run_id}.json'
            temporary_report = report_path.with_name(report_path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary_report.replace(report_path)
            except OSError as exc:
                report['report_error'] = str(exc)
                logger.warning('运行报告写入失败，采集结果仍在数据库：%s', exc)
            finally:
                temporary_report.unlink(missing_ok=True)
            if scoped:
                store.finish_run(run_id, reason)
            logger.info('运行报告保存完成：%s', reason)
        finally:
            store.close()
    logger.info('连续采集结束')
    return report
