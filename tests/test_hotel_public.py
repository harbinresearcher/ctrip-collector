import json
import logging
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    def test_public_mode_cli_routes_to_snapshot_collector(self):
        # /* ===== 步骤1：核对命令入口；数据源：酒店链接；操作：1) 选择公开样本 2) 不启动浏览器 ===== */
        logger.info('开始公开样本命令测试')
        import app
        url='https://hotels.ctrip.com/hotels/427100.html'
        # 1.1 明确指定公开模式时不能落入登录浏览器通道。
        with tempfile.TemporaryDirectory() as folder, patch('app.collect_public_hotels',return_value={'reason':'queue_exhausted','results':[]}) as collect, patch('app.collect_batch',side_effect=AssertionError('browser')), patch('sys.stdout',new_callable=io.StringIO):
            with patch('sys.argv',['app.py','--hotel-public','--url',url,'--output',folder]):app.main()
            self.assertEqual(collect.call_args.args[0],[url])
        logger.info('公开样本命令测试完成')

    def test_public_snapshot_does_not_overwrite_browser_object(self):
        # /* ===== 步骤1：保护酒店详情；数据源：既有浏览器结果及公开快照；操作：1) 分开保存 2) 留存评论 ===== */
        logger.info('开始酒店来源隔离测试')
        target=parse_public_hotel('https://hotels.ctrip.com/hotels/427100.html',fixture())
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            original=root/'hotel-427100-object.json'
            original.write_text('{"ai_summary":"browser data"}',encoding='utf-8')
            # 1.1 公开快照不能覆盖此前取得的完整浏览器AI数据。
            result=collect_network(target,root,500,delay=0)
            self.assertEqual(result['reason'],'public_snapshot_only')
            self.assertEqual(original.read_text(encoding='utf-8'),'{"ai_summary":"browser data"}')
            public=root/'hotel-427100-public-object.json'
            self.assertEqual(json.loads(public.read_text(encoding='utf-8'))['source_method'],'hotel_public_html_snapshot')
            # 1.2 已有评论达到目标时，依旧明确这是公开快照。
            again=collect_network(target,root,1,delay=0)
            self.assertEqual(again['reason'],'public_snapshot_only')
        logger.info('酒店来源隔离测试完成')

    def test_public_hotel_batch_never_opens_browser(self):
        # /* ===== 步骤2：核对无需登录入口；数据源：公开酒店样本；操作：1) 处理清单 2) 明确样本状态 ===== */
        logger.info('开始公开酒店批量测试')
        from hotel_public_batch import collect_public_hotels
        target=parse_public_hotel('https://hotels.ctrip.com/hotels/427100.html',fixture())
        with tempfile.TemporaryDirectory() as folder, patch('hotel_public_batch.load_target',return_value=target):
            # 2.1 评论最多来自公开页样本，不把5000条平台总数当作采集数量。
            result=collect_public_hotels([target['url']],folder,500)
            self.assertEqual(result['reason'],'queue_exhausted')
            self.assertEqual(result['results'][0]['reason'],'public_snapshot_only')
            self.assertEqual(result['results'][0]['count'],1)
            self.assertFalse(result['complete'])
        logger.info('公开酒店批量测试完成')
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
