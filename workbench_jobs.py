"""Translate workbench inputs into existing collector calls."""
import logging
import re
from pathlib import Path
from batch import collect_batch
from network import canonical_url, collect_network, load_target
from hotel_browser import collect_hotel
from hotel_public_batch import collect_public_hotels
from runner import Adb
from sight_snapshot import capture_sight_snapshot
from sight_guide import capture_sight_guide
from sight_ai import capture_sight_ai
from sight_photo_gallery import capture_sight_photo_gallery
from sight_review_replay import replay_sight_reviews
from app_city_discovery import capture_city_recommendations
from app_hotel_snapshot import capture_hotel_snapshot
from app_hotel_list import capture_hotel_list
from app_page_fields import collect_page_fields, validate_labels
from mobile_scoped_store import MOBILE_REVIEW_RUN_ID
from ai_replay import replay_ai_summaries
from policy_capture import capture_policy
from region_seeds import discover_regions
from multi_discovery import discover_many, resume_details
from discovery import discover_objects
from shopping_discovery import discover_shopping
from workbench_app_jobs import prepare_app_job
from workbench_media_jobs import prepare_media_job
from workbench_capture_jobs import prepare_capture_job
from coverage_report import write_coverage_report
from related_discovery import discover_related
from related_resolver import resolve_related_restaurants
from sight_related import discover_sight_related
from related_graph import discover_sight_graph
from public_poi_profile import collect_public_profiles

logger = logging.getLogger(__name__)


def make_job(request, output, adb_path=None, *, adb_serial=None):
    # /* ===== 步骤1：验证任务输入；数据源：工作台表单；操作：1) 限定模式 2) 核验参数并固定快照 ===== */
    logger.info('开始核验工作台输入')
    mode = request.get('mode')
    if mode == 'public_poi_profile':
        urls = request.get('urls')
        if not isinstance(urls, list) or not 1 <= len(urls) <= 20 or not all(isinstance(url, str) for url in urls):
            raise ValueError('请填写 1 至 20 条景点、餐饮或购物详情链接')
        links = list(dict.fromkeys(canonical_url(url) for url in urls))
        if any(not re.fullmatch(r'https://you\.ctrip\.com/(?:sight|food|yougourmet/restdetail|shopping)/[a-zA-Z0-9]+/[1-9][0-9]*\.html', url)
               for url in links):
            raise ValueError('公开资料入口只接受携程景点、餐饮或购物详情链接')
        return lambda stop, emit: collect_public_profiles(links, output, stop, emit, load_target)
    if mode == 'app_page_fields':
        name = request.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('请填写公开页面显示的名称')
        name = name.strip()
        labels = validate_labels(request.get('field_labels'))
        run = request.get('field_archive_run', '')
        if not isinstance(run, str) or (run and not MOBILE_REVIEW_RUN_ID.fullmatch(run)):
            raise ValueError('原始页面任务编号无效')
        if not run and not adb_path:
            raise ValueError('请选择已保存页面，或配置 Android 设备')

        def page_fields_job(stop, emit):
            if stop.is_set():
                return {'reason': 'cancelled', 'complete': False}
            if run:
                return collect_page_fields(output, name, labels, run, stop, emit)
            with Adb(adb_path, serial=adb_serial, ui_backend='uiautomator2') as device:
                device.connect()
                return collect_page_fields(output, name, labels, run, stop, emit, device=device)

        return page_fields_job
    if mode == 'sight_review_replay':
        return lambda stop, emit: replay_sight_reviews(output, stop, emit)
    if mode == 'ai_replay':
        folder = Path(output).resolve()

        def replay_job(stop, emit):
            if stop.is_set():
                return {'reason': 'cancelled', 'complete': False}
            return replay_ai_summaries(folder, stop, emit)

        return replay_job
    if mode in {'app_city', 'app_hotel', 'app_hotel_list', 'app_restaurant_list',
                'app_restaurant_detail', 'app_shopping_list', 'app_shopping_detail'}:
        name = request.get('name')
        max_name_length = 40 if mode in {'app_city', 'app_restaurant_list', 'app_shopping_list'} else 100
        if not adb_path or not isinstance(name, str) or not 1 <= len(name.strip()) <= max_name_length:
            if mode.startswith('app_restaurant_'):
                raise ValueError('请填写 App 页面显示的城市或餐厅名称，并配置 Android 设备')
            if mode.startswith('app_shopping_'):
                raise ValueError('请填写 App 页面显示的城市或购物地点名称，并配置 Android 设备')
            raise ValueError('请填写手机页面显示的地区或酒店名称，并配置 Android 设备')
        screen_settings = {'app_hotel': ('hotel_max_pages', '酒店'),
                           'app_hotel_list': ('hotel_max_pages', '酒店'),
                           'app_restaurant_list': ('restaurant_max_pages', '餐厅'),
                           'app_shopping_list': ('shopping_max_pages', '购物列表')}
        max_pages = 1
        if mode in screen_settings:
            setting, label = screen_settings[mode]
            max_pages = request.get(setting, 1)
            if type(max_pages) is not int or not 1 <= max_pages <= 20:
                raise ValueError(f'{label}采集屏数须为1至20')
        name, folder = name.strip(), Path(output).resolve()

        def snapshot_job(stop, emit):
            logger.info('开始工作台手机资料读取')
            if stop.is_set():
                return {'reason': 'cancelled', 'complete': False}
            emit({'app_city': '正在读取手机的必看推荐', 'app_hotel_list': '正在读取手机酒店搜索列表',
                  'app_hotel': '正在读取手机酒店资料和可见房型',
                  'app_restaurant_list': '正在读取 App 餐厅列表',
                  'app_restaurant_detail': '正在读取 App 餐厅详情',
                  'app_shopping_list': '正在读取 App 购物列表',
                  'app_shopping_detail': '正在读取 App 购物详情'}[mode])
            try:
                with Adb(adb_path, serial=adb_serial, ui_backend='uiautomator2') as device:
                    device.connect()
                    if mode == 'app_city':
                        result = capture_city_recommendations(device, folder, name, stop)
                    elif mode == 'app_hotel_list':
                        result = capture_hotel_list(device, folder, name, stop, max_pages=max_pages, emit=emit)
                    elif mode == 'app_hotel':
                        result = capture_hotel_snapshot(device, folder, name, stop, max_pages=max_pages, emit=emit)
                    elif mode == 'app_restaurant_list':
                        from app_restaurant_list import capture_restaurant_list
                        result = capture_restaurant_list(device, folder, name, stop, max_pages=max_pages)
                    elif mode == 'app_restaurant_detail':
                        from app_restaurant_detail import capture_restaurant_detail
                        result = capture_restaurant_detail(device, folder, name)
                    elif mode == 'app_shopping_list':
                        from app_shopping_list import capture_shopping_list
                        result = capture_shopping_list(device, folder, name, stop, max_pages=max_pages)
                    else:
                        from app_shopping_detail import capture_shopping_detail
                        result = capture_shopping_detail(device, folder, name)
            except InterruptedError:
                return {'reason': 'cancelled', 'complete': False}
            emit('手机资料读取结束')
            return result

        return snapshot_job
    # 1.0 景点关联队列按本次访问预算推进；续跑报告仍由采集器复核摘要及根对象。
    if mode == 'related_graph':
        raw, visits, previous = request.get('urls'), request.get('graph_visits', 5), request.get('graph_report') or None
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str):
            raise ValueError('请填写一条景点详情链接')
        url = canonical_url(raw[0])
        if not url.startswith('https://you.ctrip.com/sight/'):
            raise ValueError('需要景点详情链接')
        if type(visits) is not int or not 1 <= visits <= 100:
            raise ValueError('每次访问景点数量须为1至100')
        if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r'related-graph-[0-9a-f]{32}\.json', previous)):
            raise ValueError('请选择本目录中的关联发现报告')
        folder = Path(output).resolve()
        logger.info('景点关联队列入口核验完成')

        def graph_job(stop, emit):
            # /* ===== 步骤1：继续关联发现；数据源：已验证的根景点；操作：1) 检查取消 2) 传递访问预算 ===== */
            logger.info('开始工作台景点关联队列')
            if stop.is_set():
                logger.info('景点关联队列启动前已取消')
                return {'reason':'cancelled', 'complete':False}
            report = discover_sight_graph(url, folder, max_visits=visits, stop=stop, emit=emit, resume_file=previous)
            logger.info('工作台景点关联队列结束')
            return report

        return graph_job
    # 1.0.0 仅允许继续核验本目录生成的餐厅推荐报告。
    if mode == 'resolve_related':
        filename = request.get('report_file')
        if not isinstance(filename, str) or not re.fullmatch(r'related-restaurant-[0-9a-f]{32}\.json', filename):
            raise ValueError('请选择本地餐厅推荐报告')
        folder = Path(output).resolve()
        logger.info('推荐餐厅核验入口核验完成')

        def resolve_job(stop, emit):
            # /* ===== 步骤1：核验候选餐厅；数据源：推荐报告；操作：1) 检查取消 2) 逐条核对页面身份 ===== */
            logger.info('开始工作台推荐餐厅核验')
            if stop.is_set():
                logger.info('推荐餐厅核验启动前已取消')
                return {'reason': 'cancelled', 'complete': False}
            result = resolve_related_restaurants(filename, folder, stop=stop, emit=emit)
            logger.info('工作台推荐餐厅核验结束')
            return result

        return resolve_job
    # 1.0.0 餐厅关联对象发现仅接受单条已核验的餐饮 POI 详情地址。
    if mode in {'related_discovery', 'related_sight'}:
        raw = request.get('urls')
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str):
            raise ValueError('请填写一条餐饮详情链接')
        url = canonical_url(raw[0])
        expected = ('https://you.ctrip.com/yougourmet/restdetail/' if mode == 'related_discovery'
                    else 'https://you.ctrip.com/sight/')
        if not url.startswith(expected):
            raise ValueError('需要对应类型的餐饮或景点详情链接')
        folder = Path(output).resolve()
        logger.info('餐饮关联对象入口核验完成')

        def related_job(stop, emit):
            # /* ===== 步骤1：发现关联对象；数据源：餐饮详情页面；操作：1) 检查取消 2) 保留页面推荐 ===== */
            logger.info('开始工作台关联对象发现')
            if stop.is_set():
                logger.info('关联对象发现启动前已取消')
                return {'reason': 'cancelled', 'complete': False}
            finder = discover_related if mode == 'related_discovery' else discover_sight_related
            result = finder(url, folder, stop=stop, emit=emit)
            logger.info('工作台关联对象发现结束')
            return result

        return related_job
    # 1.0 本地覆盖报告不依赖链接、手机或网络采集参数。
    if mode == 'coverage':
        folder = Path(output).resolve()
        logger.info('本地覆盖报告入口核验完成')

        def coverage_job(stop, emit):
            # /* ===== 步骤1：统计本地覆盖；数据源：已保存的网页和手机结果；操作：1) 检查取消 2) 按来源统计 ===== */
            logger.info('开始工作台本地覆盖统计')
            if stop.is_set():
                logger.info('本地覆盖统计启动前已取消')
                return {'reason': 'cancelled', 'complete': False}
            emit('正在统计已保存的网页和手机数据，无需连接手机')
            report = write_coverage_report(folder, include_android=True)
            emit('已统计当前目录；不同来源的评论分开计数')
            logger.info('工作台本地覆盖统计完成')
            return report

        return coverage_job
    # 1.0.0 已筛选抓包响应只从工作目录受限路径导入，不执行抓包。
    if mode == 'capture_import':
        result = prepare_capture_job(request, output)
        logger.info('受限响应导入入口核验完成')
        return result
    # 1.0.1 评论图片只允许读取当前输出目录内的对象文件。
    if mode in {'media', 'video_media'}:
        result = prepare_media_job(request, output)
        logger.info('评论图片任务入口核验完成')
        return result
    # 1.0 手机任务有各自的页数限制和参数校验，交由专用适配器处理。
    if mode in {'app_reviews', 'app_ai', 'app_gallery'}:
        device_options = {'adb_serial': adb_serial} if adb_serial is not None else {}
        result = prepare_app_job(request, output, adb_path, **device_options)
        logger.info('手机采集任务入口核验完成')
        return result
    sight_modes = {'sight_detail', 'sight_policy', 'sight_guide', 'sight_ai', 'sight_gallery'}
    save_sight_thumbnails = request.get('save_sight_gallery_thumbnails', False)
    if mode == 'sight_ai' and type(save_sight_thumbnails) is not bool:
        raise ValueError('景点晒图缩略图选项必须为开或关')
    expand_sight_ai = request.get('expand_sight_ai', False)
    if mode == 'sight_ai' and type(expand_sight_ai) is not bool:
        raise ValueError('景点 AI 总结展开选项必须为开或关')
    resume_sight_gallery = request.get('resume_sight_gallery_run', '')
    if mode == 'sight_gallery' and (not isinstance(resume_sight_gallery, str)
            or resume_sight_gallery and not re.fullmatch(r'[a-f0-9]{32}', resume_sight_gallery)):
        raise ValueError('上次图集任务编号无效')
    if mode not in {'reviews', 'hotel_public', 'auto_discovery', 'resume_discovery'} | sight_modes:
        raise ValueError('请选择支持的采集类型')
    output = Path(output).resolve()
    limit = request.get('limit', 500)
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('目标条数须为1至100000的整数')
    urls, name = [], request.get('name', '')
    refresh = request.get('refresh', False)
    if mode == 'reviews' and type(refresh) is not bool:
        raise ValueError('重新读取选项无效')
    if mode in {'reviews', 'hotel_public'}:
        raw = request.get('urls')
        if not isinstance(raw, list) or not raw or any(not isinstance(u, str) for u in raw):
            raise ValueError('请填写对象详情链接')
        # 1.1 请求校验不访问网络，重复链接只生成一个任务。
        urls = list(dict.fromkeys(canonical_url(u) for u in raw))
        if mode == 'hotel_public' and any(not u.startswith('https://hotels.ctrip.com/') for u in urls):
            raise ValueError('酒店公开样本仅支持酒店链接')
    elif mode in sight_modes:
        if not isinstance(name, str) or not name.strip() or not adb_path:
            raise ValueError('手机采集需要景点名称和ADB路径')
        name = name.strip()
    pages = request.get('max_pages', 10)
    page_ceiling = 500 if mode == 'sight_gallery' else 50
    if type(pages) is not int or not 1 <= pages <= page_ceiling:
        raise ValueError(f'手机采集页数须为1至{page_ceiling}')
    # 1.2 自动地区发现的预算在启动前固定，禁止布尔值冒充整数。
    kind = request.get('kind')
    max_regions = request.get('max_regions', 5)
    discovery_pages = request.get('discovery_pages', 2)
    max_objects = request.get('max_objects', 20)
    collect_discovered = request.get('collect_discovered', False)
    if mode == 'auto_discovery':
        if kind not in {'hotel', 'sight', 'restaurant', 'shopping'}:
            raise ValueError('请选择酒店、景点、餐饮或购物')
        if (type(max_regions) is not int or not 1 <= max_regions <= 1000
                or type(discovery_pages) is not int or not 1 <= discovery_pages <= 100
                or type(max_objects) is not int or not 1 <= max_objects <= 10000
                or type(collect_discovered) is not bool):
            raise ValueError('地区、页数或对象预算无效')
    # 1.3 续跑仅允许选择本输出目录内程序生成的地区报告，不开放任意文件读取。
    resume_path = None
    if mode == 'resume_discovery':
        filename = request.get('report_file')
        if not isinstance(filename, str) or not re.fullmatch(r'discovery-regions-[0-9a-f]{32}\.json', filename):
            raise ValueError('请选择输出目录中的地区续跑报告')
        resume_path = (output / filename).resolve()
        if resume_path.parent != output or not resume_path.is_file():
            raise ValueError('地区续跑报告不存在或不在输出目录')
        entries, discovery_pages, max_objects = resume_details(resume_path)
        if (not 1 <= len(entries) <= 1000 or not 1 <= discovery_pages <= 100
                or not 1 <= max_objects <= 10000 or type(collect_discovered) is not bool):
            raise ValueError('地区续跑报告预算无效')
        # 1.4 入口格式同时决定适配器和酒店采集范围，混合或未知类型一律拒绝。
        patterns = {
            'hotel': r'https://hotels\.ctrip\.com/hotel/[a-zA-Z]+[1-9][0-9]*',
            'sight': r'https://you\.ctrip\.com/sight/[a-zA-Z]*[1-9][0-9]*/s0-p1\.html',
            'restaurant': r'https://you\.ctrip\.com/yougourmet/restaurantlist/[a-zA-Z]*[1-9][0-9]*\.html',
            'shopping': r'[1-9][0-9]*',
        }
        matches = [label for label, pattern in patterns.items() if all(re.fullmatch(pattern, entry) for entry in entries)]
        if len(matches) != 1:
            raise ValueError('地区续跑报告类型混合或入口无效')
        kind = matches[0]
    logger.info('工作台输入核验完成')

    def job(stop, emit):
        # /* ===== 步骤2：调用实际采集器；数据源：已验证参数；操作：1) 传递取消和进度 2) 返回真实结果 ===== */
        logger.info('开始工作台采集')
        if stop.is_set():
            return {'reason': 'cancelled', 'complete': False}
        emit('正在开始采集')
        # 2.1 共用已有通道和持久批任务，不复制采集逻辑。
        def collector(url, folder, count, stop, emit, *, refresh=False):
            if url.startswith('https://hotels.ctrip.com/'):
                return collect_hotel(url, folder, count, stop, emit, **({'refresh':True} if refresh else {}))
            return collect_network(load_target(url), folder, count, stop, emit, **({'refresh':True} if refresh else {}))
        if mode == 'reviews':
            result = collect_batch(urls, output, limit, collector, stop=stop, emit=emit,
                                   **({'refresh':True} if refresh else {}))
        elif mode == 'hotel_public':
            result = collect_public_hotels(urls, output, limit, stop=stop, emit=emit)
        elif mode in {'auto_discovery', 'resume_discovery'}:
            # /* ===== 步骤3：自动发现与可选采集；数据源：公开地区首页；操作：1) 按预算发现 2) 用户选择后采点评 ===== */
            logger.info('开始自动发现地区对象')
            if mode == 'auto_discovery':
                emit('正在读取公开地区入口；首页不代表全部地区')
                seed = discover_regions(output, kind, max_regions)
                run_entries = seed['entries']
            else:
                # 3.0 原清单和预算在启动前固定，discover_many再次校验报告摘要。
                emit('正在恢复地区任务，沿用原地区和预算')
                seed = None
                run_entries = entries
            result = {'reason': 'running', 'error': '', 'complete': False,
                      'seed': seed, 'discovery': None, 'collection': None,
                      'limitations': ['首页地区入口及目录预算不代表全部平台数据。']}
            # 3.1 当前请求结束后立即检查取消，不为取消任务继续请求地区页。
            if stop.is_set():
                result['reason'] = 'cancelled'
            else:
                finder = discover_shopping if kind == 'shopping' else discover_objects
                found = discover_many(run_entries, output, discovery_pages, max_objects, stop, finder,
                                      resume_report=resume_path, emit=emit)
                result['discovery'] = found
                result['reason'] = found['reason']
                result['error'] = found.get('error', '')
                if stop.is_set() and result['reason'] != 'error':
                    result['reason'] = 'cancelled'
                # 3.2 出错、取消或无对象时保留目录报告，不进入评论阶段。
                if result['reason'] == 'queue_exhausted' and not result['error'] and collect_discovered and found['objects']:
                    emit('目录发现结束，开始采集已发现对象')
                    links = [item['url'] for item in found['objects']]
                    collection = (collect_public_hotels(links, output, limit, stop=stop, emit=emit)
                                  if kind == 'hotel' else collect_batch(links, output, limit, collector, stop=stop, emit=emit))
                    result['collection'] = collection
                    result['reason'] = collection.get('reason', 'error')
                    result['error'] = collection.get('error', '')
                    if kind == 'hotel':
                        result['limitations'].append('酒店仅采公开页面样本，不代表完整点评。')
            logger.info('自动地区对象发现结束：%s', result['reason'])
        else:
            with Adb(adb_path, serial=adb_serial, ui_backend='uiautomator2') as device:
                device.connect()
                if mode == 'sight_policy':
                    result = capture_policy(device, output, name, stop, max_pages=pages)
                elif mode == 'sight_guide':
                    result = capture_sight_guide(device, output, name)
                elif mode == 'sight_ai':
                    result = capture_sight_ai(device, output, name, stop,
                                              save_gallery_thumbnails=save_sight_thumbnails,
                                              expand_summary=expand_sight_ai)
                elif mode == 'sight_gallery':
                    result = capture_sight_photo_gallery(device, output, name, stop, max_pages=pages,
                                                         resume_run=resume_sight_gallery or None)
                else:
                    result = capture_sight_snapshot(device, output, name)
        emit('本次任务已结束，完整性以结果报告为准')
        logger.info('工作台采集结束')
        return result
    return job
