import copy
import unittest
from pathlib import Path
from threading import Event
from xml.etree import ElementTree as ET

from runner import AndroidReadinessError, sight_screen_title
from sight_snapshot import parse_sight_snapshot
from sight_review_navigation import open_sight_reviews


FIXTURES = Path(__file__).parent / 'fixtures'
NAME = '长白山西景区'
ADDRESS = '白山市抚松县松江河镇松天线'


class FastStop(Event):
    def wait(self, timeout=None):
        return self.is_set()


class NavigationDevice:
    def __init__(self, screen='home'):
        self.screen = screen
        self.frames = {name: (FIXTURES / f'sight-review-nav-{name}.xml').read_text('utf-8')
                       for name in ('home', 'search', 'filled', 'detail', 'reviews')}
        self.actions = []
        self.focus_checks = 0
        self.temporary_no_focus = False
        self.cancel_on_check = None

    def ensure_foreground(self):
        self.focus_checks += 1
        if self.cancel_on_check:
            self.cancel_on_check(self.focus_checks)
        if self.temporary_no_focus and self.screen == 'detail':
            self.temporary_no_focus = False
            raise AndroidReadinessError('focus_unknown', '页面正在切换')

    def snapshot(self):
        return self.frames[self.screen]

    def run(self, *args):
        assert args[:3] == ('shell', 'input', 'tap')
        point = tuple(int(value) for value in args[3:])
        states = {'home': ('search', (453, 799)),
                  'filled': ('detail', (472, 317)),
                  'detail': ('reviews', (236, 1107))}
        assert self.screen in states, self.screen
        target, expected = states[self.screen]
        assert point == expected, (self.screen, point, expected)
        self.actions.append((self.screen, point))
        self.screen = target

    def enter_text(self, text):
        assert self.screen == 'search', self.screen
        self.actions.append((self.screen, text))
        self.screen = 'filled'


class SightReviewNavigationTests(unittest.TestCase):
    def test_real_anchors_and_reviews_title_are_retained_in_fixtures(self):
        device = NavigationDevice()
        self.assertEqual(parse_sight_snapshot(device.frames['detail'], NAME)['address_text'], ADDRESS)
        self.assertEqual(sight_screen_title(ET.fromstring(device.frames['reviews'])), NAME)

    def test_follows_real_home_search_detail_and_reviews_with_temporary_focus_loss(self):
        device = NavigationDevice()
        device.temporary_no_focus = True
        self.assertEqual(open_sight_reviews(device, NAME, ADDRESS, FastStop()), 'opened')
        self.assertEqual(device.actions, [('home', (453, 799)), ('search', NAME),
                                          ('filled', (472, 317)), ('detail', (236, 1107))])
        self.assertFalse(device.temporary_no_focus)

    def test_home_search_works_with_observed_rotating_suggestion(self):
        device = NavigationDevice()
        device.frames['home'] = device.frames['home'].replace('目的地/酒店/景点/关键字/航班号',
                                                             '上海当地游·景点·酒店')
        self.assertEqual(open_sight_reviews(device, NAME, ADDRESS, FastStop()), 'opened')
        self.assertEqual(device.actions[0], ('home', (453, 799)))

    def test_already_open_does_not_need_search_address_or_tap(self):
        device = NavigationDevice('reviews')
        self.assertEqual(open_sight_reviews(device, NAME, '', FastStop()), 'already_open')
        self.assertEqual(device.actions, [])

    def test_same_title_without_review_controls_is_not_a_review_page(self):
        for screen in ('reviews', 'home'):
            with self.subTest(screen=screen):
                device = NavigationDevice(screen)
                device.frames['reviews'] = device.frames['reviews'].replace('text="全部"', 'text="推荐笔记"')
                self.assertEqual(sight_screen_title(ET.fromstring(device.frames['reviews'])), NAME)
                with self.assertRaisesRegex(RuntimeError, '未确认.*点评'):
                    open_sight_reviews(device, NAME, ADDRESS, FastStop())
                expected_taps = 0 if screen == 'reviews' else 4
                self.assertEqual(len(device.actions), expected_taps)

    def test_other_starting_page_stops_before_any_tap(self):
        for screen, name in (('search', NAME), ('reviews', '其他景点'), ('detail', NAME)):
            with self.subTest(screen=screen, name=name):
                device = NavigationDevice(screen)
                with self.assertRaisesRegex(ValueError, '首页'):
                    open_sight_reviews(device, name, ADDRESS, FastStop())
                self.assertEqual(device.actions, [])

    def test_missing_address_stops_before_search(self):
        device = NavigationDevice()
        with self.assertRaisesRegex(ValueError, '地址'):
            open_sight_reviews(device, NAME, '', FastStop())
        self.assertEqual(device.actions, [])

    def test_wrong_address_stale_input_or_offscreen_result_never_opens_candidate(self):
        for before, after in ((ADDRESS, f'{ADDRESS}支路'), (f'text="{NAME}"', 'text="上一次的查询"'),
                              ('[95,290][850,345]', '[95,2390][850,2445]')):
            with self.subTest(before=before):
                device = NavigationDevice()
                self.assertIn(before, device.frames['filled'])
                device.frames['filled'] = device.frames['filled'].replace(before, after, 1)
                with self.assertRaisesRegex(RuntimeError, '搜索'):
                    open_sight_reviews(device, NAME, ADDRESS, FastStop())
                self.assertEqual(device.actions, [('home', (453, 799)), ('search', NAME)])

    def test_ambiguous_matching_result_never_opens_candidate(self):
        device = NavigationDevice()
        root = ET.fromstring(device.frames['filled'])
        results = next(node for node in root.iter('node') if node.get('class') == 'android.widget.ListView')
        results.append(copy.deepcopy(results[0]))
        device.frames['filled'] = ET.tostring(root, encoding='unicode')
        with self.assertRaisesRegex(ValueError, '多个'):
            open_sight_reviews(device, NAME, ADDRESS, FastStop())
        self.assertEqual(device.actions, [('home', (453, 799)), ('search', NAME)])

    def test_detail_address_mismatch_never_opens_reviews(self):
        device = NavigationDevice()
        device.frames['detail'] = device.frames['detail'].replace(ADDRESS, '同名景点的另一处地址')
        with self.assertRaisesRegex(ValueError, '地址'):
            open_sight_reviews(device, NAME, ADDRESS, FastStop())
        self.assertEqual(device.actions[-1], ('filled', (472, 317)))

    def test_another_reviews_title_after_open_stops_with_mismatch(self):
        device = NavigationDevice()
        device.frames['reviews'] = device.frames['reviews'].replace(NAME, '另一个景点')
        with self.assertRaisesRegex(ValueError, '点评'):
            open_sight_reviews(device, NAME, ADDRESS, FastStop())

    def test_cancel_at_final_foreground_check_prevents_action(self):
        device = NavigationDevice()
        stop = FastStop()

        def cancel_before_tap(focus_checks):
            if focus_checks == 2:
                stop.set()

        device.cancel_on_check = cancel_before_tap
        with self.assertRaises(InterruptedError):
            open_sight_reviews(device, NAME, ADDRESS, stop)
        self.assertEqual(device.actions, [])

    def test_manual_verification_screen_never_taps(self):
        device = NavigationDevice()
        root = ET.fromstring(device.frames['home'])
        window = root.find('node')
        ET.SubElement(window, 'node', {'package': 'ctrip.android.view', 'text': '请完成验证'})
        device.frames['home'] = ET.tostring(root, encoding='unicode')
        with self.assertRaisesRegex(ValueError, '验证'):
            open_sight_reviews(device, NAME, ADDRESS, FastStop())
        self.assertEqual(device.actions, [])
