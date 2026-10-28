import logging
import unittest

from PIL import Image, ImageDraw, ImageFilter

from gallery_match import match_gallery_image

logger = logging.getLogger(__name__)


def picture():
    image = Image.new('RGB', (240, 160))
    image.putdata([((x * 3 + y) % 256, (y * 7 + x) % 256, (x * 11 + y * 3) % 256)
                   for y in range(160) for x in range(240)])
    return image


class GalleryMatchTests(unittest.TestCase):
    def test_portrait_with_small_bottom_overlay_matches_visible_main_image(self):
        # /* ===== 步骤1：验证竖图遮挡；数据源：底栏覆盖主图；操作：1) 容许小块底部遮挡 2) 不越出主图区 ===== */
        logger.info('开始竖图遮挡验证')
        photo = picture().resize((240, 320))
        screen = Image.new('RGB', (240, 500), 'gray')
        screen.paste(photo, (0, 90))
        # 1.1 图底10像素位于主图区外，只比较可见部分并保留可见比例。
        result = match_gallery_image(screen, {'portrait': photo}, (0, 40, 240, 400))
        self.assertEqual(result['status'], 'matched')
        self.assertLess(result['scores'][0]['visible_fraction'], 1)
        self.assertGreaterEqual(result['scores'][0]['visible_fraction'], 0.95)
        self.assertLessEqual(result['scores'][0]['matched_bounds'][3], 400)
        # 1.2 大面积遮挡不适用本规则，仍须拒绝匹配。
        covered = Image.new('RGB', (240, 500), 'gray')
        covered.paste(photo, (0, 120))
        self.assertEqual(match_gallery_image(covered, {'portrait': photo}, (0, 40, 240, 400))['status'], 'no_match')
        logger.info('竖图遮挡验证完成')

    def test_rejects_blurred_background_and_downsampled_flat_pattern(self):
        # /* ===== 步骤1：排除背景误配；数据源：模糊图和棋盘格；操作：1) 模拟背景 2) 核对拒绝结果 ===== */
        logger.info('开始背景误配回归')
        # 1.1 颜色近似但细节消失，不能认作主图。
        photo = Image.new('RGB', (240, 160))
        photo.putdata([(x, y, 80) for y in range(160) for x in range(240)])
        draw = ImageDraw.Draw(photo)
        for x in range(20, 230, 30):
            draw.line((x, 40, x, 120), fill='black', width=1)
        for radius in (2, 4, 8, 12):
            with self.subTest(radius=radius):
                screen = Image.new('RGB', (240, 500), 'gray')
                screen.paste(photo.filter(ImageFilter.GaussianBlur(radius)), (0, 120))
                self.assertEqual(match_gallery_image(screen, {'photo': photo}, (0, 40, 240, 400))['status'], 'no_match')
        # 1.2 原图有纹理但缩小后信息消失，也不能认作主图。
        photo.putdata([(255, 255, 255) if (x + y) % 2 else (0, 0, 0)
                       for y in range(160) for x in range(240)])
        screen = Image.new('RGB', (240, 500), (128, 128, 128))
        self.assertEqual(match_gallery_image(screen, {'pattern': photo}, (0, 40, 240, 400))['status'], 'no_match')
        logger.info('背景误配回归完成')

    def test_matches_main_photo_but_not_prefetched_thumbnail(self):
        # /* ===== 步骤1：核对主图匹配；数据源：合成图和缩略图反例；操作：1) 匹配主图 2) 排除预加载误配 ===== */
        logger.info('开始图集主图匹配测试')
        photo = picture()
        screen = Image.new('RGB', (240, 500), 'gray')
        screen.paste(photo, (0, 120))
        result = match_gallery_image(screen, {'third': photo}, (0, 40, 240, 400))
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['candidate'], 'third')
        wrong = Image.new('RGB', (240, 500), 'gray')
        wrong.paste(photo.resize((60, 40)), (0, 430))
        self.assertEqual(match_gallery_image(wrong, {'third': photo}, (0, 40, 240, 400))['status'], 'no_match')
        logger.info('图集主图匹配测试完成')

    def test_ambiguous_candidates_and_plain_images_are_not_associated(self):
        # /* ===== 步骤2：核对不确定匹配；数据源：重复版本与纯色图；操作：1) 拒绝歧义 2) 拒绝低信息图 ===== */
        logger.info('开始图集歧义测试')
        photo = picture()
        screen = Image.new('RGB', (240, 500), 'gray')
        screen.paste(photo, (0, 120))
        self.assertEqual(match_gallery_image(screen, {'one': photo, 'two': photo.copy()}, (0, 40, 240, 400))['status'], 'ambiguous')
        flat = Image.new('RGB', (240, 160), 'gray')
        self.assertEqual(match_gallery_image(screen, {'flat': flat}, (0, 40, 240, 400))['status'], 'no_match')
        with self.assertRaises(ValueError):
            match_gallery_image(screen, {'one': photo}, (0, 0, 300, 400))
        logger.info('图集歧义测试完成')
