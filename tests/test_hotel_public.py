import json
import logging
import tempfile
import unittest
from pathlib import Path

from hotel_public import parse_public_hotel
from network import canonical_url, collect_network

logger = logging.getLogger(__name__)


def fixture(identity=427100):
    props = {'hotelDetailResponse': {'hotelBaseInfo': {'masterHotelId': identity, 'nameInfo': {'name': '酒店'}},
                                    'hotelPositionInfo': {'address': '地址'}, 'hotelPolicyInfo': {'checkIn': '14:00'},
                                    'authInfo': {'token': 'never_export'}},
             'hotelCommentResponse': {'totalCount': 5000, 'commentRating': {'ratingAll': 4.7},
                                     'groupList': [{'commentList': [{'id': 123, 'hotelId': identity, 'content': '正文',
                                                                    'rating': 5, 'checkinDate': '2026-09',
                                                                    'imageList': ['/photo.jpg'], 'videoList': [],
                                                                    'userInfo': {'nickName': '昵称', 'userId': 'private'}}]}]}}
    record = 'Ja:' + json.dumps(['$', '@c', None, props]) + '\n'
    return ''.join('<script>self.__next_f.push(' + json.dumps([1, chunk]) + ')</script>'
                   for chunk in [record[:80], record[80:]])


class PublicHotelTests(unittest.TestCase):
    def test_literal_flight_identity_fields_and_no_auth(self):
        # /* ===== 步骤1：核对酒店公开状态；数据源：分块 Flight；操作：1) 纯 JSON 解析 2) 身份核对 3) 凭据排除 ===== */
        logger.info('开始酒店状态检查')
        target = parse_public_hotel('https://hotels.ctrip.com/hotels/427100.html', fixture())
        # 1.1 不丢政策、入住时间和相对图片地址，不携带认证状态。
        self.assertEqual(target['resource_id'], 427100)
        self.assertEqual(target['details']['hotelPolicyInfo']['checkIn'], '14:00')
        self.assertEqual(target['snapshot_comments'][0]['commentId'], 123)
        self.assertEqual(target['snapshot_comments'][0]['images'], ['/photo.jpg'])
        self.assertNotIn('never_export', json.dumps(target))
        self.assertNotIn('private', json.dumps(target))
        self.assertEqual(canonical_url('https://m.ctrip.com/html5/hotel/hoteldetail/427100.html'), target['url'])
        with self.assertRaises(ValueError):
            parse_public_hotel('https://hotels.ctrip.com/hotels/427100.html', fixture(999))
        with self.assertRaises(ValueError):
            parse_public_hotel('https://hotels.ctrip.com/hotels/427100.html', '<script>alert(1)</script>')
        logger.info('酒店状态检查完成')

    def test_sample_is_not_fabricated_pagination(self):
        # /* ===== 步骤2：核对样本边界；数据源：公开页面附带点评；操作：1) 只读一批 2) 不假造接口末页 ===== */
        logger.info('开始样本边界检查')
        target = parse_public_hotel('https://hotels.ctrip.com/hotels/427100.html', fixture())
        with tempfile.TemporaryDirectory() as folder:
            result = collect_network(target, Path(folder), 500, delay=0)
            self.assertEqual(result['count'], 1)
            self.assertEqual(result['reason'], 'public_snapshot_only')
            self.assertEqual(result['source_method'], 'hotel_public_html_snapshot')
            self.assertFalse(result['complete'])
            self.assertEqual(result['display_total'], 5000)
            self.assertIsNone(result['sort_type'])
            self.assertIsNone(result['page_size'])
        logger.info('样本边界检查完成')
