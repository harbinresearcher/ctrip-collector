"""图集主图与候选缓存的像素比较；结果不证明上传原图或对象身份。"""
import logging
import math

from PIL import Image, ImageChops, ImageFilter, ImageStat

logger = logging.getLogger(__name__)


def match_gallery_image(screen, candidates, region):
    # /* ===== 步骤1：限定主图区域；数据源：截图与查看器布局；操作：1) 排除缩略图 2) 统一比较尺寸 ===== */
    logger.info('开始图集图片匹配')
    left, top, right, bottom = region
    if not (0 <= left < right <= screen.width and 0 <= top < bottom <= screen.height):
        raise ValueError('主图区域超出截图边界')
    # 1.1 仅在调用方确认的主图区域比较，不搜索底部缩略图或其他App界面。
    scale = 96 / (right - left)
    area = screen.crop(region).convert('RGB').resize((96, max(1, round((bottom - top) * scale))), Image.Resampling.LANCZOS)
    scores = []
    for name, original in candidates.items():
        # 1.2 支持宽度或高度受限的居中显示；不搜索任意缩小的缩略图尺寸。
        photo = original.convert('RGB')
        if min(photo.size) < 32 or sum(ImageStat.Stat(photo).stddev) / 3 < 12:
            continue
        ratio = min(area.width / photo.width, area.height / photo.height)
        width, height = max(1, round(photo.width * ratio)), max(1, round(photo.height * ratio))
        thumbnail = photo.resize((width, height), Image.Resampling.LANCZOS)
        # 1.3 检查实际比较尺寸的信息量，排除缩小后变成纯色的纹理。
        if min(width, height) < 8 or sum(ImageStat.Stat(thumbnail).stddev) / 3 < 12:
            continue
        x = (area.width - width) // 2
        best, best_y, best_height = 1.0, 0, height
        # 1.4 底部栏可能遮住少量主图，仅允许最多5%底边缺失，不纳入底部缩略图。
        for y in range(area.height - math.ceil(height * 0.95) + 1):
            visible_height = min(height, area.height - y)
            difference = ImageChops.difference(area.crop((x, y, x + width, y + visible_height)), thumbnail.crop((0, 0, width, visible_height)))
            error = sum(ImageStat.Stat(difference).mean) / (3 * 255)
            if error < best:
                best, best_y, best_height = error, y, visible_height
        # 1.4 主图细节也须保留；剔除滤镜边框，防止同色模糊背景通过颜色比较。
        patch = area.crop((x, best_y, x + width, best_y + best_height))
        inner = (2, 2, width - 2, best_height - 2)
        edges = thumbnail.filter(ImageFilter.FIND_EDGES).crop(inner)
        patch_edges = patch.filter(ImageFilter.FIND_EDGES).crop(inner)
        edge_energy = sum(ImageStat.Stat(edges).mean)
        edge_error = sum(ImageStat.Stat(ImageChops.difference(edges, patch_edges)).mean) / max(edge_energy, 1)
        detail_verified = edge_energy >= 6 and edge_error <= 0.5 and sum(ImageStat.Stat(patch).stddev) / 3 >= 12
        scores.append({'candidate': name, 'error': best, 'edge_error': edge_error,
                       'detail_verified': detail_verified, 'visible_fraction': best_height / height,
                       'matched_bounds': [round(left + x / scale), round(top + best_y / scale),
                                          min(right, round(left + (x + width) / scale)), min(bottom, round(top + (best_y + best_height) / scale))]})
    logger.info('候选图片比较完成，共%s张', len(scores))
    # /* ===== 步骤2：拒绝歧义对应；数据源：候选误差；操作：1) 限定最大误差 2) 要求领先次候选 ===== */
    logger.info('开始核对匹配可信度')
    scores.sort(key=lambda row: row['error'])
    result = {'status': 'no_match', 'candidate': None, 'scores': scores,
              'method': 'bounded_main_image_pixel_similarity', 'original_verified': False}
    # 2.1 阈值仅用于像素近似，不推断图集分类、酒店ID或原图来源。
    if scores and scores[0]['error'] <= 0.025 and scores[0]['detail_verified']:
        if len(scores) > 1 and scores[1]['error'] - scores[0]['error'] < 0.01:
            result['status'] = 'ambiguous'
        else:
            result.update(status='matched', candidate=scores[0]['candidate'])
    logger.info('图集匹配结束：%s', result['status'])
    return result
