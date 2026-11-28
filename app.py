import argparse
import json
import logging
import queue
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from runner import Adb, collect, scan_phone_target
from network import BlockedError, canonical_url, collect_network, load_target
from hotel_browser import collect_hotel
from capture_io import import_capture
from ai_capture import capture_ai
from gallery_entry import capture_ai_gallery
from discovery import discover_objects
from batch import collect_batch
from media import download_media
from shopping_discovery import discover_shopping
from multi_discovery import discover_many, resume_details
from region_seeds import discover_regions
from coverage_report import write_coverage_report
from coverage_audit import write_coverage_audit
from phone_queue import write_phone_queue
from gap_collect import MAX_WORKERS, collect_gaps
from drive import drive_rounds
from object_identity import resolve_object_ref
from hotel_public_batch import collect_public_hotels

logger = logging.getLogger(__name__)
HOME = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent


def gui(default_adb, smoke=False):
    # /* ================================================================
    # 步骤1：创建操作窗口
    # 数据源：用户填写的对象名称；操作：1) 选择类型 2) 后台采集并显示结果
    # ================================================================ */
    logger.info('开始创建窗口')
    root = tk.Tk()
    root.title('携程点评采集 · 测试版 0.21')
    root.geometry('900x850')
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='网络采集：酒店、景点、餐饮、购物。', font=('Microsoft YaHei UI', 13)).pack(anchor='w')
    ttk.Label(frame, text='酒店会打开专用浏览器（Edge 或 Chrome），首次需自行登录。').pack(anchor='w', pady=(4, 10))
    mode = tk.StringVar(value='network')
    fast_ui = tk.BooleanVar(value=True)
    modes = ttk.Frame(frame)
    modes.pack(fill='x')
    ttk.Radiobutton(modes, text='网络采集（不需要手机）', value='network', variable=mode).pack(side='left')
    ttk.Radiobutton(modes, text='手机评论补采（先打开全部评论）', value='phone', variable=mode).pack(side='left', padx=12)
    ttk.Radiobutton(modes, text='酒店 AI 总结', value='ai', variable=mode).pack(side='left')
    ttk.Radiobutton(frame, text='城市酒店/景点/餐饮目录：每行填一个城市链接，生成后转网络模式', value='discover', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='继续地区目录：链接框填写上次地区报告 JSON 文件路径', value='discover_resume', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='购物目录：每行填写一个携程地区数字 ID（上海为 2）', value='shopping_discover', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='自动发现地区：酒店目录', value='seeds_hotel', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='自动发现地区：景点目录', value='seeds_sight', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='自动发现地区：餐饮目录', value='seeds_restaurant', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='自动发现地区：购物目录', value='seeds_shopping', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='导入 App 响应：在下框填写受限抓包 JSONL 文件路径', value='capture', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='下载评论图片：在下框填写已导出评论 JSONL 路径；数量框为图片上限', value='media', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='酒店 AI 图集（试验）：手机先打开住客晒图网格顶部，填写酒店 ID', value='gallery', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='统计本地数据覆盖：选择已有采集输出目录', value='coverage', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='酒店公开样本：填酒店详情链接，不需要登录', value='hotel_public', variable=mode).pack(anchor='w')
    ttk.Checkbutton(frame, text='手机使用快速页面读取（uiautomator2；不要同时运行 Mobile MCP）', variable=fast_ui).pack(anchor='w')
    ttk.Label(frame, text='对象详情链接，每行一个（酒店 hotels.ctrip.com；景点 / 餐厅 / 购物 you.ctrip.com）').pack(anchor='w', pady=(8, 2))
    urls_box = tk.Text(frame, height=3, wrap='word')
    urls_box.pack(fill='x')
    values = {}
    # 1.1 明确对象标签，避免把不同对象写入同一数据集。
    for label, key, default in [('对象名称（手机模式填写）', 'place', ''), ('酒店 ID（AI 模式填写）', 'resource_id', ''), ('每对象目标累计条数', 'limit', '500'),
                                ('目录 / 图集页数上限', 'pages', '5'), ('目录对象 / 地区数量上限', 'objects', '50'),
                                ('输出目录', 'folder', str(HOME / 'data')), ('ADB 路径', 'adb', default_adb)]:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        ttk.Label(row, text=label, width=25).pack(side='left')
        values[key] = tk.StringVar(value=default)
        ttk.Entry(row, textvariable=values[key]).pack(side='left', fill='x', expand=True)
        if key == 'folder':
            ttk.Button(row, text='选择', command=lambda: values['folder'].set(filedialog.askdirectory() or values['folder'].get())).pack(side='right')
    kind = tk.StringVar(value='hotel')
    options = ttk.Frame(frame)
    options.pack(fill='x', pady=8)
    ttk.Radiobutton(options, text='酒店', value='hotel', variable=kind).pack(side='left')
    ttk.Radiobutton(options, text='景点', value='sight', variable=kind).pack(side='left', padx=16)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=8)
    display = tk.Text(frame, height=15, wrap='word', state='disabled')
    display.pack(fill='both', expand=True)
    events = queue.Queue()
    stop = threading.Event()
    active = False

    def start():
        # /* ============================================================
        # 步骤2：启动采集线程
        # 数据源：表单；操作：1) 校验名称与数量 2) 禁止重复启动
        # ============================================================ */
        nonlocal active
        logger.info('开始校验采集参数')
        # 2.1 Tk 状态只在主线程读取和更新。
        if active:
            return
        try:
            target = int(values['limit'].get())
            selected_mode = mode.get()
            urls = [line.strip() for line in urls_box.get('1.0', 'end').splitlines() if line.strip()]
            if target < 1 or (selected_mode in ('phone', 'ai') and not values['place'].get().strip()) or (selected_mode in ('network', 'discover', 'capture', 'media', 'hotel_public') and not urls):
                raise ValueError()
            if selected_mode in ('capture', 'media') and (len(urls) != 1 or not Path(urls[0]).is_file()):
                raise ValueError()
            if selected_mode == 'discover_resume' and (len(urls) != 1 or not Path(urls[0]).is_file()):
                raise ValueError()
            if selected_mode == 'discover' and (not urls or int(values['pages'].get()) < 1 or int(values['objects'].get()) < 1):
                raise ValueError()
            if selected_mode == 'shopping_discover' and (not urls or not all(url.isdigit() for url in urls) or int(values['pages'].get()) < 1 or int(values['objects'].get()) < 1):
                raise ValueError()
            if selected_mode == 'ai' and (kind.get() != 'hotel' or not values['resource_id'].get().strip().isdigit()):
                raise ValueError()
            if selected_mode == 'gallery' and (kind.get() != 'hotel' or not values['resource_id'].get().strip().isdigit()
                                              or int(values['resource_id'].get()) <= 0 or not 1 <= int(values['pages'].get()) <= 500):
                raise ValueError()
            if selected_mode.startswith('seeds_') and int(values['objects'].get()) < 1:
                raise ValueError()
        except ValueError:
            messagebox.showerror('参数错误', '网络模式填写详情链接；手机评论填写对象名称；AI 模式选择酒店并填写正整数酒店 ID。图集页数为1至500。目标条数必须为正整数。')
            return
        selected = {key: value.get().strip() for key, value in values.items()}
        selected_kind = kind.get()
        selected_backend = 'uiautomator2' if fast_ui.get() else 'adb'
        stop.clear()
        active = True
        start_button.configure(state='disabled')

        def worker():
            logger.info('开始后台采集')
            adb = None
            # 2.2 异常和完成消息均送回主线程。
            try:
                # 2.2.1 网络任务串行读取，不与手机争抢页面。
                if selected_mode == 'hotel_public':
                    # 2.2.5 公开页仅有评论样本，不等同登录浏览器的完整分页。
                    result = collect_public_hotels(urls, Path(selected['folder']), target, stop, lambda msg: events.put(('log',msg)))
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
                elif selected_mode == 'coverage':
                    # 2.2.4 覆盖统计只读取本地数据库，不启动手机或浏览器。
                    result = write_coverage_report(Path(selected['folder']))
                    events.put(('log', f"已统计 {len(result['objects'])} 个对象；报告：{result['report_file']}"))
                    events.put(('log', '覆盖报告只反映已有数据，不代表平台全部数据。'))
                elif selected_mode.startswith('seeds_'):
                    # 2.2.3 地区入口来自首页候选；生成后再由用户启动业务目录核验。
                    seed_kind = selected_mode.removeprefix('seeds_')
                    result = discover_regions(Path(selected['folder']), seed_kind, int(selected['objects']))
                    events.put(('log', f"首页找到 {result['total_visible']} 个地区，已生成 {len(result['entries'])} 个待核验入口。"))
                    events.put(('region_' + seed_kind, '\n'.join(result['entries'])))
                elif selected_mode == 'media':
                    result = download_media(Path(urls[0]), Path(selected['folder']), target, stop)
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
                elif selected_mode == 'capture':
                    result = import_capture(Path(urls[0]), Path(selected['folder']), target, stop,
                                            lambda msg: events.put(('log', msg)))
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
                elif selected_mode == 'discover_resume':
                    # 2.2.2 按报告原预算恢复地区任务，校验成功后才发起新请求。
                    entries,pages,objects = resume_details(urls[0])
                    finder = discover_shopping if all(entry.isdigit() for entry in entries) else discover_objects
                    result = discover_many(entries, Path(selected['folder']), pages, objects, stop, finder, resume_report=urls[0],
                                           emit=lambda msg: events.put(('log',msg)))
                    events.put(('log', json.dumps({k: v for k, v in result.items() if k != 'objects'}, ensure_ascii=False, indent=2)))
                    if result['objects']:
                        events.put(('urls', '\n'.join(obj['url'] for obj in result['objects'])))
                elif selected_mode in ('discover', 'shopping_discover'):
                    finder = discover_shopping if selected_mode == 'shopping_discover' else discover_objects
                    # 2.2.2 多个地区依次发现；预算分别作用于每个地区。
                    result = (finder(urls[0], Path(selected['folder']), int(selected['pages']), int(selected['objects']), stop)
                              if len(urls) == 1 else discover_many(urls, Path(selected['folder']), int(selected['pages']), int(selected['objects']), stop, finder,
                                                                  emit=lambda msg: events.put(('log',msg))))
                    events.put(('log', json.dumps({k: v for k, v in result.items() if k != 'objects'}, ensure_ascii=False, indent=2)))
                    if result['objects']:
                        events.put(('urls', '\n'.join(obj['url'] for obj in result['objects'])))
                elif selected_mode == 'network':
                    result = collect_batch(urls, Path(selected['folder']), target, collect_url, stop,
                                           lambda msg: events.put(('log', msg)))
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
                elif selected_mode == 'gallery':
                    # 2.2.2 图集入口自行建立缓存基线，与评论采集共用停止标记和设备清理。
                    adb = Adb(selected['adb'], ui_backend=selected_backend)
                    adb.connect()
                    result = capture_ai_gallery(adb, selected['resource_id'], Path(selected['folder']), stop,
                                                max_pages=int(selected['pages']))
                    counts = {status: sum(page['result']['status'] == status for page in result['pages'])
                              for status in ('saved', 'ambiguous', 'no_match')}
                    events.put(('log', f"图集结束：{result['status']}；已确认 {counts['saved']} 页，待核对 {counts['ambiguous']} 页，未匹配 {counts['no_match']} 页。"))
                    events.put(('log', '待核对版本保存在 review 文件夹；本次结果不代表图集已采全。'))
                    if result.get('error'):
                        events.put(('log', '错误：' + result['error']))
                elif selected_mode == 'ai':
                    adb = Adb(selected['adb'], ui_backend=selected_backend)
                    adb.connect()
                    result = capture_ai(adb, selected_kind, selected['place'], selected['resource_id'], Path(selected['folder']), stop)
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
                else:
                    adb = Adb(selected['adb'], ui_backend=selected_backend)
                    adb.connect()
                    result = collect(adb, selected_kind, selected['place'], Path(selected['folder']), target,
                                     max(1500, target * 8), stop, lambda msg: events.put(('log', msg)))
                    events.put(('log', f"停止原因：{result['reason']}\n累计：{result['count']} 条\nCSV：{result['csv']}\n{result['error']}\n导出错误：{result['export_error']}"))
            except Exception as exc:
                events.put(('log', '错误：' + str(exc)))
            finally:
                if adb is not None:
                    adb.close()
                events.put(('done', '本次运行已结束；不代表数据已全部采完。'))
                logger.info('后台采集结束')
        threading.Thread(target=worker, daemon=False).start()
        logger.info('采集参数校验完成')

    def pump():
        nonlocal active
        # 2.3 主线程处理日志和按钮状态。
        while not events.empty():
            event, text = events.get_nowait()
            if event == 'urls':
                urls_box.delete('1.0', 'end')
                urls_box.insert('1.0', text)
                mode.set('network')
                text = '对象清单已填入链接框。点开始即可依次采集；目录预算不代表全站覆盖。'
            elif event.startswith('region_'):
                urls_box.delete('1.0', 'end')
                urls_box.insert('1.0', text)
                mode.set('shopping_discover' if event == 'region_shopping' else 'discover')
                text = '地区入口已填入链接框。点开始后逐地区发现对象；首页地区不是完整城市清单。'
            display.configure(state='normal')
            display.insert('end', text + '\n')
            display.see('end')
            display.configure(state='disabled')
            if event == 'done':
                active = False
                start_button.configure(state='normal')
        root.after(200, pump)

    def close():
        # 2.4 先停止线程，让最后一批数据完成落盘。
        if active:
            stop.set()
            root.after(500, close)
        else:
            root.destroy()

    start_button = ttk.Button(buttons, text='开始 / 继续保存', command=start)
    start_button.pack(side='left')
    ttk.Button(buttons, text='停止并保存', command=stop.set).pack(side='left', padx=12)
    root.protocol('WM_DELETE_WINDOW', close)
    pump()
    logger.info('窗口创建完成')
    if smoke:
        root.withdraw()
        root.after(400, root.destroy)
    root.mainloop()


def collect_url(url, output, limit, stop=None, emit=None):
    # /* ===== 步骤3：按对象分派；数据源：已验证详情链接；操作：1) 酒店打开专用浏览器 2) 其他对象读取公开接口 ===== */
    logger.info('开始选择对象采集通道')
    # 3.1 酒店登录状态由浏览器管理，其他类型不需要启动浏览器。
    url = canonical_url(url)
    if url.startswith('https://hotels.ctrip.com/'):
        result = collect_hotel(url, output, limit, stop, emit)
    else:
        result = collect_network(load_target(url), output, limit, stop, emit)
    logger.info('对象采集通道结束')
    return result


def main():
    # /* ================================================================
    # 步骤3：选择运行入口
    # 数据源：命令行；操作：1) 自检或批量验证 2) 默认显示操作窗口
    # ================================================================ */
    logger.info('开始启动程序')
    parser = argparse.ArgumentParser(description='携程公开点评网络采集与 Android 补采测试版')
    bundled_adb = HOME / 'platform-tools' / 'adb.exe'
    parser.add_argument('--adb', default=str(bundled_adb) if bundled_adb.is_file() else shutil.which('adb'))
    parser.add_argument('--workbench', action='store_true', help='启动本机采集工作台并打开浏览器')
    parser.add_argument('--port', type=int, help='工作台端口，默认 8765，设为 0 时自动选择')
    parser.add_argument('--no-browser', action='store_true', help='运行工作台时只显示本地访问地址')
    parser.add_argument('--adb-serial', help='工作台使用的 Android 设备编号，例如 emulator-5554')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--gui-smoke', action='store_true')
    parser.add_argument('--coverage', action='store_true', help='统计输出目录内已有网络评论及字段覆盖')
    parser.add_argument('--audit', action='store_true', help='对账输出目录内已有证据：平台目标数、已采数与缺口原因')
    parser.add_argument('--phone-queue', action='store_true',
                        help='只读：读对账与手机库，列出下一步该在手机上采哪个对象及可直接复制的命令')
    parser.add_argument('--collect-gaps', action='store_true', help='按对账缺口续采：只处理目标已知、缺口最大且有链接的对象')
    parser.add_argument('--gap-limit', type=int, default=10, help='--collect-gaps / --drive 每轮最多处理几个对象，默认 10')
    parser.add_argument('--drive', action='store_true', help='无人值守驱动循环：只在首轮发现目录，之后按缺口续采直到轮数用完或没有进展')
    parser.add_argument('--rounds', type=int, default=1, help='--drive 最多运行几轮，默认 1；不会无限循环')
    parser.add_argument('--daily-quota', type=int, default=0,
                        help='--drive 同一天累计新增多少条就停，0 表示不限制；状态存输出目录的 drive-state.json')
    parser.add_argument('--breaker-failures', type=int, default=0,
                        help='--drive 一轮里连续多少个对象采集失败就熔断停止，0 表示不启用')
    parser.add_argument('--min-per-minute', type=float, default=0,
                        help='--drive 本轮采集速率低于该值（条/分钟）就标 degraded 并把下一轮每轮对象数减半，0 表示不启用')
    parser.add_argument('--workers', type=int, default=1,
                        help='--drive 对象级并发数，1–3，默认 1（串行，行为与以前一致）；同一对象内部仍串行翻页')
    parser.add_argument('--hotel-public', action='store_true', help='采酒店公开详情及评论样本，不登录浏览器')
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--object-ref', default='', help='手机采集的对象引用，如 sight-136039；身份取自 --output 里的落盘证据')
    parser.add_argument('--url', action='append', default=[], help='公开点评对象详情链接，可重复指定')
    parser.add_argument('--import-capture', type=Path, help='导入受限 App 点评响应 JSONL')
    parser.add_argument('--download-media', type=Path, help='从已导出评论 JSONL 下载返回的图片版本')
    parser.add_argument('--media-limit', type=int, default=20, help='本次处理的不同图片URL上限')
    parser.add_argument('--capture-ai', action='store_true')
    parser.add_argument('--resource-id', default='')
    parser.add_argument('--city-list', action='append', help='城市目录链接，可重复指定；酒店示例：https://hotels.ctrip.com/hotel/shanghai2')
    parser.add_argument('--region-seeds', choices=['hotel', 'sight', 'restaurant', 'shopping'], help='从公开首页生成地区入口并发现对象')
    parser.add_argument('--resume-regions', type=Path, help='从多地区报告恢复，使用原地区清单和预算')
    parser.add_argument('--max-regions', type=int, default=50, help='本次最多处理的首页地区数')
    parser.add_argument('--shopping-district', type=int, help='发现购物目录，携程地区ID，上海为2；0表示全球')
    parser.add_argument('--discovery-pages', type=int, default=5)
    parser.add_argument('--max-objects', type=int, default=None,
                        help='目录发现 / --drive 的对象上限，默认 50；--phone-queue 默认 10')
    parser.add_argument('--collect-discovered', action='store_true')
    parser.add_argument('--url-file', type=Path, help='每行一个对象详情链接的 UTF-8 文件')
    parser.add_argument('--kind', choices=['hotel', 'sight'], default='hotel')
    parser.add_argument('--place', default='')
    parser.add_argument('--output', type=Path, default=HOME / 'data')
    parser.add_argument('--limit', type=int, default=None,
                        help='每个对象本次最多再补这么多条；--drive 默认 200（每个对象每轮最多再补这么多条，实测约 230 条/分钟），其他模式默认 500')
    parser.add_argument('--max-swipes', type=int, default=1500)
    parser.add_argument('--ui-backend', choices=['adb', 'uiautomator2'], default='uiautomator2')
    parser.add_argument('--phone-target', action='store_true',
                        help='只读手机当前页面的「全部」点评数，不采点评；配合 --object-ref 使用')
    args = parser.parse_args()
    # 3.0 --limit 的默认值跟模式走：--drive 每个对象每轮补 200 条（实测约 230 条/分钟），
    #     其他模式（含 --collect-gaps）保持原来的 500，不改既有命令的口径。
    if args.limit is None:
        args.limit = 200 if args.drive else 500
    # 3.0.0 --max-objects 的默认值也跟模式走：--phone-queue 默认 10，其他模式保持 50。
    if args.max_objects is None:
        args.max_objects = 10 if args.phone_queue else 50
    if args.workbench:
        if any((args.check, args.gui_smoke, args.coverage, args.audit, args.phone_queue, args.collect_gaps,
                args.drive, args.hotel_public, args.collect,
                args.object_ref, args.url,
                args.url_file, args.import_capture, args.download_media, args.capture_ai, args.city_list,
                args.region_seeds, args.resume_regions, args.shopping_district is not None, args.collect_discovered)):
            parser.error('--workbench 不能与其他运行模式同时使用')
        if args.adb_serial and not args.adb:
            parser.error('请使用 --adb 指定 ADB 路径，或将 ADB 加入 PATH')
        port = 8765 if args.port is None else args.port
        if not 0 <= port <= 65535:
            parser.error('--port 须在 0 至 65535 之间')
        from workbench_server import serve_workbench

        try:
            serve_workbench(args.output, args.adb, port, adb_serial=args.adb_serial,
                            open_browser=not args.no_browser)
        except OSError as exc:
            parser.exit(1, f'工作台启动失败：{exc}\n')
        return
    # 3.1 只把工作台专属的参数挡在门外；--adb-serial 对命令行采集同样有用：
    #     同时连着模拟器和真机时，串行采集也要能指定其中一台。
    if args.port is not None or args.no_browser:
        parser.error('--port 和 --no-browser 需要使用 --workbench')
    args.adb = args.adb or 'adb'
    # 3.0 采集模式互斥：同时给了多个入口时明确拒绝，而不是悄悄只跑其中一个。
    modes = []
    if args.url or args.url_file or args.hotel_public:
        modes.append('对象点评（--url / --url-file / --hotel-public）')
    if args.capture_ai:
        modes.append('AI 总结（--capture-ai）')
    if args.check:
        modes.append('设备自检（--check）')
    if args.collect:
        modes.append('手机采集（--collect）')
    if args.drive:
        modes.append('驱动循环（--drive）')
    if args.city_list or args.region_seeds or args.resume_regions or args.shopping_district is not None:
        if not args.drive:
            modes.append('目录发现（--city-list / --region-seeds / --resume-regions / --shopping-district）')
    if len(modes) > 1:
        parser.error('一次只能运行一种模式，当前同时给了：' + '、'.join(modes))
    # 3.0.1 对象身份只取一个来源：证据引用与手填名称/编号同时给出时明确拒绝，不悄悄选一个。
    if args.object_ref:
        if not args.collect:
            parser.error('--object-ref 只能与 --collect 一起使用')
        if args.place.strip() or args.resource_id.strip():
            parser.error('--object-ref 与 --place/--resource-id 互斥：编号和名称都从证据取，请去掉手填值')
    # 3.0.1.5 手机侧缺口队列：只读对账与手机库，不连设备、不触网；与采集、统计、对账互斥。
    if args.phone_queue:
        if any((args.url, args.url_file, args.city_list, args.region_seeds, args.resume_regions, args.hotel_public,
                args.shopping_district is not None, args.collect, args.capture_ai, args.import_capture,
                args.download_media, args.coverage, args.collect_gaps, args.drive, args.audit, args.check)):
            parser.error('--phone-queue 不能与采集任务、覆盖统计、对账或设备自检同时使用')
        if args.max_objects < 1:
            parser.error('max-objects 必须大于零')
        print(json.dumps(write_phone_queue(args.output, args.max_objects), ensure_ascii=False, indent=2))
        return
    # 3.0.2 覆盖对账只读取落盘证据；与采集任务、覆盖统计互斥，避免参数混用。
    if args.audit:
        if any((args.url, args.url_file, args.city_list, args.region_seeds, args.resume_regions, args.hotel_public,
                args.shopping_district is not None, args.collect, args.capture_ai, args.import_capture,
                args.download_media, args.coverage, args.collect_gaps, args.drive)):
            parser.error('--audit 不能与采集任务或覆盖统计同时使用')
        print(json.dumps(write_coverage_audit(args.output), ensure_ascii=False, indent=2))
        return
    # 3.0.3 缺口续采先读对账再采；与对账、统计和其他采集入口互斥，避免参数混用。
    if args.collect_gaps:
        if any((args.url, args.url_file, args.city_list, args.region_seeds, args.resume_regions, args.hotel_public,
                args.shopping_district is not None, args.collect, args.capture_ai, args.import_capture,
                args.download_media, args.coverage, args.audit, args.drive)):
            parser.error('--collect-gaps 不能与采集任务、覆盖统计或对账同时使用')
        if args.limit < 1 or args.gap_limit < 1:
            parser.error('limit 和 gap-limit 必须大于零')
        result = collect_gaps(args.output, args.limit, args.gap_limit, collector=collect_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # 3.0.3.1 被平台拦住不是普通的「这轮没采到」：命令行只留一句可照做的提示，退出 1。
        if result['reason'] == 'blocked':
            raise BlockedError((result.get('blocked') or {}).get('status'))
        return
    # 3.0.4 无人值守驱动循环：目录发现只交给第一轮，之后每轮按缺口续采；是否继续由缺口是否下降决定。
    if args.drive:
        if any((args.url, args.url_file, args.hotel_public, args.collect, args.capture_ai, args.import_capture,
                args.download_media, args.coverage, args.audit, args.collect_gaps, args.resume_regions,
                args.collect_discovered, args.shopping_district is not None)):
            parser.error('--drive 不能与对象采集、统计、对账、导入或其他续采入口同时使用')
        if args.rounds < 1 or args.discovery_pages < 1 or args.max_objects < 1 or args.limit < 1 or args.gap_limit < 1:
            parser.error('rounds、discovery-pages、max-objects、limit 和 gap-limit 必须大于零')
        if args.daily_quota < 0 or args.breaker_failures < 0 or args.min_per_minute < 0:
            parser.error('daily-quota、breaker-failures 和 min-per-minute 不能为负数')
        # 3.0.4.2 并发只在不同对象之间：上限 3，越界直接拒绝，不悄悄夹到范围内。
        if not 1 <= args.workers <= MAX_WORKERS:
            parser.error(f'--workers 只能是 1 到 {MAX_WORKERS} 的整数')
        if args.city_list and args.region_seeds:
            parser.error('--city-list 与 --region-seeds 一次只能用一个')

        def discover(output, pages, objects):
            # 3.0.4.1 目录入口沿用既有函数；地区种子与购物目录的参数口径与独立发现模式一致。
            finder = discover_shopping if args.region_seeds == 'shopping' else discover_objects
            seeds = discover_regions(output, args.region_seeds, args.max_regions) if args.region_seeds else None
            entries = seeds['entries'] if seeds else args.city_list
            result = (discover_many(entries, output, pages, objects, threading.Event(), finder)
                      if len(entries) > 1 else finder(entries[0], output, pages, objects))
            if seeds:
                result['region_seed_report'] = seeds['report_file']
            return result
        discover_entry = discover if args.city_list or args.region_seeds else None
        drive_report = drive_rounds(args.output, args.rounds, args.discovery_pages, args.max_objects,
                                    args.limit, args.gap_limit, discover=discover_entry, collector=collect_url,
                                    daily_quota=args.daily_quota, breaker_failures=args.breaker_failures,
                                    min_per_minute=args.min_per_minute, workers=args.workers)
        print(json.dumps(drive_report, ensure_ascii=False, indent=2))
        # 3.0.4.3 被拦时整轮已停，命令行再给一句当天停跑的提示，退出 1。
        if drive_report['reason'] == 'blocked':
            raise BlockedError((drive_report.get('blocked') or {}).get('status'))
        return
    if args.coverage:
        if args.url or args.url_file or args.city_list or args.region_seeds or args.resume_regions or args.hotel_public or args.shopping_district is not None or args.collect or args.capture_ai or args.import_capture or args.download_media:
            parser.error('--coverage 不能与采集任务同时使用')
        print(json.dumps(write_coverage_report(args.output), ensure_ascii=False, indent=2))
        return
    # 3.0.0 图片任务与评论采集分开，避免参数混用触发额外请求。
    if args.download_media:
        if args.url or args.url_file or args.city_list or args.region_seeds or args.resume_regions or args.hotel_public or args.shopping_district is not None or args.collect or args.capture_ai or args.import_capture:
            parser.error('--download-media 不能与其他采集任务同时使用')
        result = download_media(args.download_media, args.output, args.media_limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'error':
            raise SystemExit(1)
        return
    # 3.0.1 离线导入不启动浏览器、不连接手机。
    if args.import_capture:
        if args.url or args.url_file or args.city_list or args.region_seeds or args.resume_regions or args.hotel_public or args.shopping_district is not None or args.collect or args.capture_ai:
            parser.error('--import-capture 不能与其他采集任务同时使用')
        result = import_capture(args.import_capture, args.output, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(item['reason'] == 'error' or item['export_error'] for item in result):
            raise SystemExit(1)
        return
    # 3.0 目录先生成可核对的对象清单；命令行可明确选择接着采集。
    if (args.city_list and args.shopping_district is not None) or (args.region_seeds and (args.city_list or args.shopping_district is not None)) or (args.resume_regions and (args.city_list or args.region_seeds or args.shopping_district is not None or args.url or args.url_file)):
        parser.error('酒店/餐饮/景点目录与购物目录不能同时运行')
    if args.collect_discovered and not args.city_list and not args.region_seeds and not args.resume_regions and args.shopping_district is None:
        parser.error('--collect-discovered 必须与 --city-list 或 --shopping-district 一起使用')
    if args.city_list or args.region_seeds or args.resume_regions or args.shopping_district is not None:
        resume_config = resume_details(args.resume_regions) if args.resume_regions else None
        finder = discover_shopping if args.shopping_district is not None or args.region_seeds == 'shopping' or (resume_config and all(x.isdigit() for x in resume_config[0])) else discover_objects
        seed_report = discover_regions(args.output, args.region_seeds, args.max_regions) if args.region_seeds else None
        entries = resume_config[0] if resume_config else (seed_report['entries'] if seed_report else ([args.shopping_district] if args.shopping_district is not None else args.city_list))
        pages,objects = (resume_config[1],resume_config[2]) if resume_config else (args.discovery_pages,args.max_objects)
        result = (discover_many(entries,args.output,pages,objects,threading.Event(),finder,resume_report=args.resume_regions)
                  if args.resume_regions else (finder(entries[0], args.output, pages, objects)
                  if len(entries) == 1 else discover_many(entries, args.output, pages, objects, threading.Event(), finder)))
        if seed_report:
            result['region_seed_report'] = seed_report['report_file']
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'error':
            raise SystemExit(1)
        if not args.collect_discovered:
            return
        args.url.extend(obj['url'] for obj in result['objects'])
        if not args.url:
            return
    if args.url_file:
        args.url.extend(line.strip() for line in args.url_file.read_text('utf-8-sig').splitlines() if line.strip())
    if args.hotel_public and not args.url:
        parser.error('--hotel-public 需要酒店详情链接或带 --collect-discovered 的酒店目录')
    # 3.1 自检只读取设备；命令行采集必须明确对象名称。
    if args.url:
        if args.limit < 1:
            parser.error('limit 必须大于零')
        result = collect_public_hotels(args.url, args.output, args.limit) if args.hotel_public else collect_batch(args.url, args.output, args.limit, collect_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'error':
            # 3.1.0 被平台拦住时给一句当天停跑的提示，与普通错误分开；两者都退出 1。
            blocked = next((item for item in result.get('results') or [] if item.get('blocked')), None)
            if blocked is not None:
                raise BlockedError(blocked.get('blocked_status'))
            raise SystemExit(1)
    elif args.capture_ai:
        with Adb(args.adb, serial=args.adb_serial, ui_backend=args.ui_backend) as adb:
            adb.connect()
            print(json.dumps(capture_ai(adb, args.kind, args.place, args.resource_id, args.output), ensure_ascii=False, indent=2))
    elif args.check:
        adb = Adb(args.adb, serial=args.adb_serial)
        adb.connect()
        print(json.dumps({'device': 'authorized', 'ctrip': 'installed'}, ensure_ascii=False))
    elif args.phone_target:
        # 3.1.0 只补分母：打开点评页看一眼「全部」有多少条，不采正文。
        identity = resolve_object_ref(args.output, args.object_ref) if args.object_ref else None
        if identity is None:
            parser.error('--phone-target 需要 --object-ref（身份来自落盘证据）')
        with Adb(args.adb, serial=args.adb_serial, ui_backend=args.ui_backend) as adb:
            adb.connect()
            result = scan_phone_target(adb, identity['kind'], identity['name'], args.output,
                                       object_id=identity['object_id'],
                                       identity_source=identity['identity_source'])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'arrival_failed':
            raise SystemExit(1)
    elif args.collect:
        # 3.1.1 --object-ref 先从落盘证据解析身份，解析成功才允许采集；失败交给 cli() 打成一行原因。
        identity = resolve_object_ref(args.output, args.object_ref) if args.object_ref else None
        if args.limit < 1 or args.max_swipes < 0:
            parser.error('limit > 0，max-swipes >= 0')
        if identity is None and not args.place.strip():
            parser.error('必须指定 --place（或改用 --object-ref）')
        with Adb(args.adb, serial=args.adb_serial, ui_backend=args.ui_backend) as adb:
            adb.connect()
            if identity is None:
                # 3.1.2 旧入口保持原样：手填名称，身份仍标为操作者声明。
                result = collect(adb, args.kind, args.place, args.output, args.limit, args.max_swipes)
            else:
                result = collect(adb, identity['kind'], identity['name'], args.output, args.limit, args.max_swipes,
                                 object_id=identity['object_id'], identity_source=identity['identity_source'],
                                 identity_evidence=identity['identity_evidence'])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'error' or result['export_error']:
            raise SystemExit(1)
    else:
        gui(args.adb, args.gui_smoke)
    logger.info('程序结束')


def cli():
    # 3.2 冻结后的程序也统一使用 UTF-8 输出日志。
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # 3.3 目录不存在、链接不符合入口、设备读取失败、手机没停在目标页面等属于可预期错误：
    # 只打印一行原因并返回 1，不再把 Python 栈丢给命令行用户。其他异常保持原样。
    # 3.3.1 RuntimeError 在本仓库只用于这类可预期状态（设备/页面/OCR/任务占用），
    # 实测「手机停在旅拍页」就是它，原来会打一屏栈。
    try:
        main()
    except BlockedError as exc:
        # 3.3.0 平台已经明确拒绝（限流/验证码）：只给一句可照做的提示，不打印堆栈。
        logger.error('%s', exc)
        raise SystemExit(1)
    except (ValueError, OSError, RuntimeError) as exc:
        logger.error('%s', exc)
        raise SystemExit(1)


if __name__ == '__main__':
    cli()
