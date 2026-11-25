import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from workbench_jobs import make_job

FIXTURE = Path(__file__).parent / 'fixtures' / 'hotel-detail.xml'
SECOND_ROOM = FIXTURE.with_name('hotel-second-room.xml')
NAME = '长白山万达锦华度假酒店'


def add_historical_room(xml):
    from app_hotel_snapshot import nodes
    root = ET.fromstring(xml)
    card = nodes(root, 'htl_x_dtl_rmlist_mbRmCard_exposure')[0]
    parent = next(node for node in root.iter() if card in list(node))
    parent.append(ET.parse(SECOND_ROOM).getroot())
    return ET.tostring(root, encoding='unicode')


class AppHotelSnapshotTests(unittest.TestCase):
    def test_reads_historical_hotel_and_room_without_borrowing_starting_price(self):
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.return_value = FIXTURE.read_text(encoding='utf-8')
            report = make_job({'mode': 'app_hotel', 'name': NAME}, folder, 'fake-adb')(Event(), lambda _: None)
            self.assertEqual(report['review_display'], {'score_text': '4.7', 'label': '超棒', 'count_text': '5722条'})
            self.assertEqual(report['address_text'], '抚松长白山国际度假区白云路455号')
            self.assertEqual(report['distance_display'], '距县中心驾车21.2公里')
            self.assertEqual(report['review_snippet_display'], '“去温泉很方便”')
            self.assertEqual(report['stay_nights_visible'], 1)
            self.assertEqual(report['room_filter_options'], [
                {'label': label, 'selected': None}
                for label in ('双床房', '家庭房', '含早餐', '大床房', '免费取消', '套房')
            ])
            self.assertEqual(report['hotel_starting_price'], '¥489起')
            self.assertEqual(report['facility_highlights'], ['A级亲子酒店', '儿童托管', '免费拼车接机', '室内儿童乐园'])
            self.assertEqual(report['room_cards'], [{'title': '锦华高级房-双床', 'bed_text': '2张1.2米单人床',
                'area_text': '36㎡', 'occupancy_text': '2人入住', 'floor_text': '2-6层',
                'features': ['浴缸', '部分禁烟']}])
            self.assertFalse(report['complete'])
            self.assertNotIn('QIBXCQ', json.dumps(report, ensure_ascii=False))
            self.assertEqual(json.loads(Path(report['report_file']).read_text('utf-8')), report)
            coverage = make_job({'mode': 'coverage'}, folder)(Event(), lambda _: None)
            self.assertEqual(coverage['android']['hotel_details'][0]['room_card_count'], 1)
            device.run.assert_not_called()

    def test_room_filter_selection_requires_an_explicit_state_on_the_option(self):
        from app_hotel_snapshot import parse_hotel_snapshot
        root = ET.parse(FIXTURE).getroot()
        labels = {node.get('text'): node for node in root.iter('node') if node.get('text')}
        labels['含早餐'].set('selected', 'true')
        labels['免费取消'].set('package', 'other.app')
        labels['共1晚'].set('text', '共0晚')
        report = parse_hotel_snapshot(ET.tostring(root, encoding='unicode'), NAME)
        self.assertEqual([row['label'] for row in report['room_filter_options'] if row['selected']], ['含早餐'])
        self.assertNotIn('免费取消', [row['label'] for row in report['room_filter_options']])
        self.assertNotIn('stay_nights_visible', report)

    def test_expected_name_must_be_the_header_title(self):
        from app_hotel_snapshot import parse_hotel_snapshot
        xml = FIXTURE.read_text(encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '酒店名称'):
            parse_hotel_snapshot(xml, '锦华高级房-双床')

    def test_adds_new_rooms_and_keeps_the_room_offer_separate_from_the_hotel_price(self):
        first = FIXTURE.read_text('utf-8')
        second = add_historical_room(first)
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.side_effect = [first, second, second]
            stop = Event()
            with patch.object(stop, 'wait', return_value=False):
                result = make_job({'mode': 'app_hotel', 'name': NAME, 'hotel_max_pages': 4},
                                  folder, 'fake-adb')(stop, lambda _: None)
            rooms = result['room_cards']
            self.assertEqual([r['title'] for r in rooms], ['锦华高级房-双床', '锦华高级房-大床'])
            self.assertNotIn('offer_displays', rooms[0])
            self.assertEqual(rooms[1]['features'], ['浴缸', '部分禁烟'])
            self.assertEqual(rooms[1]['offer_displays'], [
                {'starting_price_display': '¥489起', 'offer_label_display': '特惠一口价', 'discount_display': '优惠37'}
            ])
            self.assertEqual(result['hotel_starting_price'], '¥489起')
            self.assertEqual([page['new_room_count'] for page in result['pages']], [1, 1])
            self.assertEqual(result['reason'], 'repeated_screen')
            self.assertFalse(result['complete'])
            self.assertEqual(device.run.call_count, 2)
            for call in device.run.call_args_list:
                self.assertEqual(call.args[:3], ('shell', 'input', 'swipe'))
            self.assertGreater(int(device.run.call_args_list[0].args[4]), 2649)
            self.assertLess(int(device.run.call_args_list[0].args[4]), 2900)
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)

    def test_changed_hotel_search_context_keeps_only_accepted_screen(self):
        first = FIXTURE.read_text('utf-8')
        second = add_historical_room(first).replace('白云路455号', '白云路456号')
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.side_effect = [first, second]
            stop = Event()
            with patch.object(stop, 'wait', return_value=False):
                result = make_job({'mode': 'app_hotel', 'name': NAME, 'hotel_max_pages': 10},
                                  folder, 'fake-adb')(stop, lambda _: None)
            self.assertEqual(result['reason'], 'hotel_context_changed')
            self.assertEqual(len(result['room_cards']), 1)
            self.assertEqual(len(result['pages']), 1)
            self.assertEqual(device.run.call_count, 1)

    def test_hotel_room_budget_is_validated_before_connecting(self):
        with TemporaryDirectory() as folder:
            for limit in (0, 21, True, '2'):
                with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, '1至20'):
                    make_job({'mode': 'app_hotel', 'name': NAME, 'hotel_max_pages': limit}, folder, 'fake-adb')

    def test_stop_between_screens_still_saves_confirmed_rooms(self):
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.return_value = FIXTURE.read_text('utf-8')
            stop = Event()

            def stop_during_swipe(*args):
                stop.set()

            device.run.side_effect = stop_during_swipe
            result = make_job({'mode': 'app_hotel', 'name': NAME, 'hotel_max_pages': 2},
                              folder, 'fake-adb')(stop, lambda _: None)
            device.snapshot.assert_called_once()
            self.assertEqual(result['reason'], 'cancelled')
            self.assertEqual(len(result['room_cards']), 1)
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)

    def test_verified_room_screen_is_on_disk_before_next_device_operation(self):
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.return_value = FIXTURE.read_text('utf-8')
            files_during_swipe = []

            def disconnect_during_swipe(*args):
                files = list(Path(folder).glob('app-hotel-*.json'))
                files_during_swipe.extend(json.loads(f.read_text('utf-8')) for f in files)
                raise RuntimeError('Android device disconnected')

            device.run.side_effect = disconnect_during_swipe
            result = make_job({'mode': 'app_hotel', 'name': NAME, 'hotel_max_pages': 2},
                              folder, 'fake-adb')(Event(), lambda _: None)
            self.assertEqual(len(files_during_swipe), 1)
            checkpoint = files_during_swipe[0]
            self.assertEqual(checkpoint['status'], 'running')
            self.assertFalse(checkpoint['complete'])
            self.assertEqual(len(checkpoint['room_cards']), 1)
            self.assertEqual(result['reason'], 'error')
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['error'], 'Android device disconnected')
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)
            coverage = make_job({'mode': 'coverage'}, folder)(Event(), lambda _: None)
            self.assertEqual(coverage['android']['hotel_details'][0]['status'], 'error')
