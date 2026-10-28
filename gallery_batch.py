"""当前已确认图集的有限翻页采集，逐页保留成功与缺失记录。"""
import json
import logging
import uuid
from pathlib import Path

from gallery_cache import GalleryCandidateSession
from gallery_capture import capture_current_gallery_frame, gallery_position
from gallery_layout import viewer_region

logger = logging.getLogger(__name__)


def collect_gallery_pages(adb, baseline, region, output, object_key, confirmed_category, stop, *, max_pages=30):
    # /* ===== 步骤1：建立任务记录；数据源：当前已确认图集；操作：1) 限定页数 2) 创建独立进度文件 ===== */
    logger.info('开始图集分页任务')
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 500:
        raise ValueError('单次图集页数须在1至500之间')
    if region is not None:
        left, top, right, bottom = region
        if not 0 <= left < right or not 0 <= top < bottom:
            raise ValueError('主图区边界无效')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f'gallery-run-{uuid.uuid4().hex}.json'
    record = {'status': 'running', 'object_key': object_key, 'category': confirmed_category,
              'pages': [], 'complete': False, 'category_verification': 'operator_confirmed'}
    # 1.1 原子替换进度；不覆盖之前任务，不将显示总数当作已采数量。
    def checkpoint():
        temporary = path.with_suffix('.part')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    checkpoint()
    logger.info('图集任务记录建立完成')
    # /* ===== 步骤2：采集并翻页；数据源：当前页码和缓存会话；操作：1) 逐页记录 2) 不前进即停止 ===== */
    logger.info('开始连续读取图集')
    try:
        with GalleryCandidateSession(adb, baseline) as session:
            previous = None
            for _ in range(max_pages):
                if stop.is_set():
                    raise InterruptedError('图集采集已取消')
                adb.ensure_foreground()
                xml = adb.snapshot()
                position = gallery_position(xml, confirmed_category)
                current_region = viewer_region(xml, confirmed_category) if region is None else region
                left, top, right, bottom = current_region
                if previous is not None and position != (previous[0] + 1, previous[1]):
                    record['status'] = 'page_not_advanced' if position == previous else 'page_changed_unexpectedly'
                    break
                # 2.1 无匹配与歧义保留为缺失，不阻止采集后续页。
                result = capture_current_gallery_frame(adb, baseline, current_region, output, object_key, confirmed_category, stop, candidate_session=session)
                adb.ensure_foreground()
                if gallery_position(adb.snapshot(), confirmed_category) != position:
                    raise ValueError('采集期间页码被外部操作改变')
                if result['status'] == 'saved' and result['gallery_index'] != position[0]:
                    raise ValueError('保存图片与预期页码不一致')
                record['pages'].append({'index': position[0], 'total': position[1], 'result': result})
                checkpoint()
                if position[0] == position[1]:
                    record['status'] = 'end_reached'
                    break
                if len(record['pages']) == max_pages:
                    record['status'] = 'page_limit'
                    break
                # 2.2 只在当前已核验主图区内横向滑动，取消或切出携程即停止。
                if stop.is_set():
                    raise InterruptedError('图集采集已取消')
                adb.ensure_foreground()
                adb.run('shell', 'input', 'swipe', str(round(left + (right - left) * .8)), str((top + bottom) // 2),
                        str(round(left + (right - left) * .2)), str((top + bottom) // 2), '350')
                if stop.wait(2):
                    raise InterruptedError('图集采集已取消')
                previous = position
    except InterruptedError:
        record['status'] = 'cancelled'
    except Exception as exc:
        record.update(status='error', error_type=type(exc).__name__, error=str(exc))
    finally:
        checkpoint()
        logger.info('图集连续读取结束：%s', record['status'])
    return record
