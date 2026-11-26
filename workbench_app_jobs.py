"""Adapt existing Android collectors to the workbench task contract."""
import logging
import re
from pathlib import Path

from ai_capture import capture_ai
from gallery_entry import capture_ai_gallery
from object_identity import resolve_object_ref
from runner import Adb, collect

logger = logging.getLogger(__name__)


def resolve_app_job_object(request, output, *, expected_kind=None):
    object_ref = request.get('object_ref', '')
    if object_ref == '':
        return None
    if not isinstance(object_ref, str):
        raise ValueError('对象引用必须为文本')
    if request.get('name', '') != '' or request.get('resource_id', '') != '':
        raise ValueError('从本地目录选择对象时，请清空手填名称和对象 ID')
    evidence = resolve_object_ref(output, object_ref)
    kind = evidence['kind']
    if (expected_kind is not None and kind != expected_kind) or request.get('kind', kind) != kind:
        raise ValueError('所选对象与任务类型不一致')
    return evidence


def prepare_app_job(request, output, adb_path, *, adb_serial=None):
    # /* ===== 步骤1：核对手机任务；数据源：工作台表单；操作：1) 验证模式和身份 2) 固定采集预算 ===== */
    logger.info('开始核对手机采集参数')
    mode = request.get('mode')
    kind = request.get('kind', 'hotel')
    name = request.get('name', '')
    resource_id = request.get('resource_id', '')
    evidence = resolve_app_job_object(request, output) if mode == 'app_reviews' else None
    if evidence is not None:
        kind, name, resource_id = evidence['kind'], evidence['name'], evidence['object_id']
    limit = request.get('limit', 500)
    pages = request.get('max_pages', 10)
    display_fallback = request.get('gallery_display_fallback', False)
    gallery_start_page = request.get('gallery_start_page')
    expand_reviews = request.get('expand_reviews', False)
    refresh = request.get('refresh', False)
    save_visible_ai = request.get('save_visible_ai', False)
    save_review_thumbnails = request.get('save_review_thumbnails', False)
    swipes = request.get('max_swipes', max(1500, limit * 8) if type(limit) is int else 1500)
    if mode not in {'app_ai', 'app_gallery', 'app_reviews'} or kind not in {'hotel', 'sight', 'restaurant', 'shopping'}:
        raise ValueError('请选择支持的手机采集类型')
    if mode in {'app_ai', 'app_gallery'} and kind != 'hotel':
        raise ValueError('AI总结和图集当前仅支持酒店')
    if not isinstance(name, str) or not name.strip() or not adb_path:
        raise ValueError('手机采集需要对象名称和ADB路径')
    if not isinstance(resource_id, str) or not re.fullmatch(r'[0-9]{1,20}', resource_id) or int(resource_id) <= 0:
        raise ValueError('请填写正整数对象 ID')
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('目标评论条数须为1至100000')
    if type(pages) is not int or not 1 <= pages <= 500:
        raise ValueError('图集页数须为1至500')
    if mode == 'app_gallery' and type(display_fallback) is not bool:
        raise ValueError('图集画面补录选项必须为开或关')
    if mode == 'app_gallery' and gallery_start_page is not None and (
            type(gallery_start_page) is not int or not 1 <= gallery_start_page <= 1000000):
        raise ValueError('图集起始页码须为正整数')
    if mode == 'app_reviews' and (type(expand_reviews) is not bool or (expand_reviews and kind != 'hotel')):
        raise ValueError('尝试展开正文目前只支持酒店点评')
    if mode == 'app_reviews' and type(refresh) is not bool:
        raise ValueError('重新读取选项无效')
    if type(save_visible_ai) is not bool or (save_visible_ai and (mode != 'app_reviews' or kind != 'hotel')):
        raise ValueError('首屏 AI 总结仅用于酒店点评任务')
    if type(save_review_thumbnails) is not bool or (save_review_thumbnails and
                                                    (mode != 'app_reviews' or kind not in {'hotel', 'restaurant'})):
        raise ValueError('点评图片仅用于酒店或餐厅点评任务')
    if type(swipes) is not int or not 1 <= swipes <= 800000:
        raise ValueError('滑动次数须为1至800000')
    name, folder = name.strip(), Path(output).resolve()
    logger.info('手机采集参数核对完成')

    def job(stop, emit):
        # /* ===== 步骤2：运行手机采集；数据源：已校验参数；操作：1) 连接设备 2) 传递停止事件并释放 ===== */
        logger.info('开始工作台手机采集')
        if stop.is_set():
            logger.info('手机采集启动前已取消')
            return {'reason': 'cancelled', 'complete': False}
        emit('请保持手机解锁，并停留在指定对象页面')
        try:
            # 2.1 上下文管理器确保连接失败和异常路径也清理设备会话。
            with Adb(adb_path, serial=adb_serial, ui_backend='uiautomator2') as device:
                device.connect()
                if mode == 'app_ai':
                    result = capture_ai(device, kind, name, resource_id, folder, stop)
                elif mode == 'app_gallery':
                    options = {}
                    if display_fallback:
                        options['save_display_fallback'] = True
                    if gallery_start_page is not None:
                        options['start_page'] = gallery_start_page
                    result = capture_ai_gallery(device, resource_id, folder, stop, max_pages=pages,
                                                **options)
                else:
                    options = {}
                    if expand_reviews:
                        options['expand_reviews'] = True
                    if refresh:
                        options['refresh'] = True
                    if save_visible_ai:
                        options.update(save_visible_ai=True, refresh=True)
                    if save_review_thumbnails:
                        options.update(save_review_thumbnails=True, refresh=True)
                    if evidence is not None:
                        options.update(identity_source=evidence['identity_source'],
                                       identity_evidence=evidence['identity_evidence'])
                    result = collect(device, kind, name, folder, limit, swipes, stop, emit, object_id=resource_id,
                                     **options)
        except InterruptedError:
            logger.info('手机采集已取消')
            return {'reason': 'cancelled', 'complete': False}
        emit('本次手机任务结束，请查看报告中的覆盖范围')
        logger.info('工作台手机采集结束')
        return result
    return job
