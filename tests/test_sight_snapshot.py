import logging
import copy
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from sight_snapshot import parse_sight_snapshot

logger = logging.getLogger(__name__)
TICKET_FIXTURE = Path(__file__).parent / 'fixtures' / 'sight-detail-ticket.xml'


class SightSnapshotTests(unittest.TestCase):
    def test_historical_detail_binds_ticket_fields_to_the_visible_product(self):
        logger.info('开始历史票品卡片核对')
        xml = TICKET_FIXTURE.read_text('utf-8')
        snapshot = parse_sight_snapshot(xml, '长白山西景区')
        self.assertEqual(snapshot['review_display'], {'score_text': '4.4', 'scale_text': '/5', 'count_text': '1097条点评'})
        self.assertEqual(snapshot['parent_sight_name'], '长白山景区')
        self.assertEqual(snapshot['ranking_labels'], ['长白山必打卡景点榜 No.1'])
        self.assertEqual(snapshot['address_text'], '白山市抚松县松江河镇松天线')
        self.assertEqual(len(snapshot['ticket_cards']), 1)
        ticket = snapshot['ticket_cards'][0]
        self.assertEqual(ticket['title'], '西景区组合票(7天内班期)(成人票)')
        self.assertEqual(ticket['display_price'], '¥190起')
        self.assertEqual(ticket['booking_conditions'], ['可订明日', '有条件退', '无需取票'])
        self.assertEqual(ticket['sold_label'], '已售2000+')
        self.assertFalse(snapshot['complete'])
        with self.assertRaises(ValueError):
            parse_sight_snapshot(xml, '长白山景区')
        logger.info('历史票品卡片核对完成')

    def test_foreign_or_missing_price_cannot_be_borrowed_from_page(self):
        logger.info('开始核对票品价格归属')
        root = ET.parse(TICKET_FIXTURE).getroot()
        price = next(node for node in root.iter('node') if node.get('resource-id') == 'undefined-price')
        parents = {child: parent for parent in root.iter() for child in parent}
        first_card = parents[parents[price]]
        second_card = copy.deepcopy(first_card)
        next(node for node in second_card.iter('node') if node.get('text') == '西景区组合票(7天内班期)(成人票)').set('text', '另一张票')
        next(node for node in second_card.iter('node') if node.get('resource-id') == 'undefined-price').set('text', '88')
        parents[first_card].append(second_card)
        price.set('package', 'other.app')
        price.set('text', '1')
        snapshot = parse_sight_snapshot(ET.tostring(root, encoding='unicode'), '长白山西景区')
        self.assertEqual(snapshot['price_texts'], ['门票¥190起'])
        self.assertIsNone(snapshot['ticket_cards'][0]['display_price'])
        self.assertEqual(snapshot['ticket_cards'][1]['display_price'], '¥88起')
        logger.info('票品价格归属核对完成')

    def test_capture_checks_foreground_and_records_time(self):
        # /* ===== 步骤1：核验保存边界；数据源：模拟设备；操作：1) 前后核验 2) 写结构化结果 ===== */
        logger.info('开始实时采集测试')
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch
        from sight_snapshot import capture_sight_snapshot
        device = Mock()
        device.snapshot.return_value = '<hierarchy/>'
        with tempfile.TemporaryDirectory() as folder, patch('sight_snapshot.parse_sight_snapshot', return_value={'complete': False}):
            # 1.1 前台切出时禁止保存结果。
            device.ensure_foreground.side_effect = [None, RuntimeError('changed')]
            with self.assertRaises(RuntimeError):
                capture_sight_snapshot(device, folder, '景点')
            self.assertEqual(list(Path(folder).iterdir()), [])
            device.ensure_foreground.side_effect = None
            result = capture_sight_snapshot(device, folder, '景点')
            self.assertIn('+08:00', result['captured_at'])
            self.assertTrue(Path(result['file']).is_file())
        logger.info('实时采集测试完成')

    def test_visible_fields_and_identity(self):
        # /* ===== 步骤1：核验详情；数据源：模拟界面；操作：1) 提取原文 2) 排除其他应用 ===== */
        logger.info('开始景点字段测试')
        texts = ['测试景区', '点评/笔记', '须知', '预订', '今日已闭园', '明日07:30-17:00开放', '门票¥190起', '优待政策', '儿童等']
        xml = '<hierarchy>' + ''.join('<node package="ctrip.android.view" text="'+t+'"/>' for t in texts) + '<node package="other" text="门票¥1起"/></hierarchy>'
        # 1.1 名称不匹配必须拒绝，政策入口不等于政策全文。
        result = parse_sight_snapshot(xml, '测试景区')
        self.assertEqual(result['opening_texts'], ['今日已闭园', '明日07:30-17:00开放'])
        self.assertEqual(result['price_texts'], ['门票¥190起'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['policy_status'], 'entry_visible_content_not_captured')
        with self.assertRaises(ValueError):
            parse_sight_snapshot(xml, '其他景区')
        with self.assertRaises(ValueError):
            parse_sight_snapshot('<hierarchy/>', '测试景区')
        logger.info('景点字段测试完成')
