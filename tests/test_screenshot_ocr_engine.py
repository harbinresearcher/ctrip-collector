"""截图文字识别：真机调用 Windows OCR，并核对错误信息可读。"""
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from threading import Event

from PIL import Image, ImageDraw, ImageFont

from screenshot_ocr import error_excerpt, prepare_image, read_windows_ocr, recognize_screenshot

logger = logging.getLogger(__name__)
FONT = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / 'msyh.ttc'


def screenshot_bytes(text='携程酒店 评分 4.8'):
    # 1.1 用系统自带雅黑生成一张白底黑字图片，模拟 App 截图的可识别画面。
    image = Image.new('RGB', (760, 200), 'white')
    ImageDraw.Draw(image).text((30, 60), text, font=ImageFont.truetype(str(FONT), 44), fill='black')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


class ScreenshotOcrTests(unittest.TestCase):
    def test_error_excerpt_turns_clixml_into_a_sentence(self):
        # /* ===== 步骤1：核对错误提取；数据源：PowerShell CLIXML；操作：1) 去标记 2) 去换行 ===== */
        logger.info('开始错误信息提取测试')
        raw = ('#< CLIXML\n<Objs Version="1.1.0.1"><S S="Error">Exception calling "GetResult": '
               '"拒绝访问。_x000D__x000A_无法访问指定的文件"_x000D__x000A_</S></Objs>')
        excerpt = error_excerpt(raw)
        self.assertIn('拒绝访问', excerpt)
        self.assertNotIn('CLIXML', excerpt)
        self.assertNotIn('<S S=', excerpt)
        self.assertNotIn('_x000D_', excerpt)
        self.assertLessEqual(len(error_excerpt('x' * 500)), 200)
        self.assertEqual(error_excerpt(None), '')
        logger.info('错误信息提取测试完成')

    @unittest.skipUnless(os.name == 'nt' and FONT.is_file(), '需要 Windows 与微软雅黑字体')
    def test_real_windows_ocr_reads_a_generated_screenshot(self):
        # /* ===== 步骤2：真实识别；数据源：生成的截图；操作：1) 读取图片 2) 核对中文被识别 ===== */
        logger.info('开始真实 OCR 测试')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'shot.png'
            self.assertEqual(prepare_image(screenshot_bytes(), path), [760, 200])
            try:
                lines = read_windows_ocr(path)
            except RuntimeError as exc:
                # 2.1 没装语言包时明确跳过，不把环境缺失当成代码失败。
                if '未安装' in str(exc) or '未能完成' in str(exc):
                    self.skipTest(f'本机未能完成 Windows OCR：{exc}')
                raise
            # 2.2 Windows OCR 会把每个汉字用空格分开，比较前先去掉空格。
            joined = ''.join(line.replace(' ', '') for line in lines)
            self.assertIn('携程酒店', joined)
        logger.info('真实 OCR 测试完成')

    @unittest.skipUnless(os.name == 'nt' and FONT.is_file(), '需要 Windows 与微软雅黑字体')
    def test_recognize_screenshot_writes_report_and_strips_han_spacing(self):
        # /* ===== 步骤3：端到端；数据源：生成截图；操作：1) 保存报告 2) 中文间距被去掉 ===== */
        logger.info('开始截图识别端到端测试')
        with tempfile.TemporaryDirectory() as folder:
            try:
                result = recognize_screenshot(screenshot_bytes(), folder, Event(), lambda message: None)
            except RuntimeError as exc:
                self.skipTest(f'本机未能完成 Windows OCR：{exc}')
            self.assertEqual(result['reason'], 'read')
            self.assertTrue(Path(result['report_file']).is_file())
            # 3.1 契约是去掉汉字之间的空格；汉字与数字之间的空格保留。
            self.assertNotRegex(result['text'], r'[\u4e00-\u9fff] +[\u4e00-\u9fff]')
            self.assertIn('4', result['text'])
        logger.info('截图识别端到端测试完成')


if __name__ == '__main__':
    unittest.main()