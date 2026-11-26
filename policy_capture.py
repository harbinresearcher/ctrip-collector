"""Bounded policy screen collection; no claim of whole-table completeness."""
import hashlib
import json
import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from core import bounds
from sight_policy import parse_policy_screen

logger = logging.getLogger(__name__)


def capture_policy(device, output, name, stop, max_pages=10):
    # /* ===== 步骤1：建立记录；数据源：当前政策表；操作：1) 校验预算 2) 独立原子保存 ===== */
    logger.info('开始政策表任务')
    if type(max_pages) is not int or not 1 <= max_pages <= 50:
        raise ValueError('政策采集页数须为1至50')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / ('sight-policy-' + uuid.uuid4().hex + '.json')
    report = {'name': name, 'rows': [], 'pages': [], 'supplementary_notes': [], 'complete': False,
              'status': 'running', 'file': str(path.resolve()), 'source_channel': 'android_ui',
              'source_method': 'android_sight_policy'}
    # 1.1 只保存结构化结果与屏幕哈希，不把完整设备界面写入交付数据。
    def save():
        temp = path.with_suffix('.part')
        try:
            temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(path)
        finally:
            # 写入或替换失败都不能在输出目录留下半个文件。
            temp.unlink(missing_ok=True)
    save()
    logger.info('政策表任务记录建立完成')
    # /* ===== 步骤2：读取并翻页；数据源：当前屏幕；操作：1) 验证身份 2) 合并 3) 有界滑动 ===== */
    logger.info('开始逐屏读取政策')
    layout, hashes, seen_rows = None, set(), set()
    try:
        for index in range(max_pages):
            if stop.is_set():
                report['status'] = 'cancelled'
                break
            # 2.1 读取前后核验前台，名称由合并器逐屏核验。
            device.ensure_foreground()
            stamp = datetime.now(timezone(timedelta(hours=8))).isoformat()
            xml = device.snapshot()
            device.ensure_foreground()
            digest = hashlib.sha256(xml.encode()).hexdigest()
            if digest in hashes:
                report['status'] = 'repeated_screen'
                break
            result = parse_policy_screen(xml, name, layout)
            layout = result['layout']
            hashes.add(digest)
            new_rows = 0
            for row in result['rows']:
                key = (row['group'], row['condition'], row['policy'])
                if key not in seen_rows:
                    seen_rows.add(key)
                    report['rows'].append(row)
                    new_rows += 1
            report['supplementary_notes'] = list(dict.fromkeys(report['supplementary_notes'] + result['supplementary_notes']))
            report['pages'].append({'captured_at': stamp, 'raw_hash': digest, 'new_row_count': new_rows,
                                    'unresolved_condition_count': result['unresolved_condition_count']})
            save()
            if index + 1 == max_pages:
                report['status'] = 'page_limit'
                break
            # 2.2 从可滚动容器计算坐标，不能使用固定手机尺寸。
            candidates = [n for n in ET.fromstring(xml).iter('node') if n.get('package') == 'ctrip.android.view' and n.get('scrollable') == 'true' and n.get('class') == 'android.widget.ScrollView']
            if len(candidates) != 1:
                raise ValueError('无法唯一定位政策滚动容器')
            left, top, right, bottom = bounds(candidates[0])
            if right <= left or bottom <= top:
                raise ValueError('政策滚动区域无效')
            if stop.is_set():
                report['status'] = 'cancelled'
                break
            device.ensure_foreground()
            x = str((left + right) // 2)
            device.run('shell', 'input', 'swipe', x, str(int(top + .8*(bottom-top))), x, str(int(top + .3*(bottom-top))), '400')
            if stop.wait(1.5):
                report['status'] = 'cancelled'
                break
    except Exception as exc:
        report.update(status='error', error=str(exc))
    finally:
        # 末次写入失败时保留已读政策行并把失败写进记录。
        try:
            save()
        except OSError as exc:
            report['export_error'] = str(exc)
            logger.warning('政策读取进度写入失败，已读行仍返回：%s', exc)
        logger.info('逐屏读取政策结束：%s', report['status'])
    return report
