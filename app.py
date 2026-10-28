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

from runner import Adb, collect
from network import canonical_url, collect_network, load_target
from hotel_browser import collect_hotel
from capture_io import import_capture
from ai_capture import capture_ai
from discovery import discover_objects
from batch import collect_batch
from media import download_media
from shopping_discovery import discover_shopping

logger = logging.getLogger(__name__)
HOME = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent


def gui(default_adb, smoke=False):
    # /* ================================================================
    # 步骤1：创建操作窗口
    # 数据源：用户填写的对象名称；操作：1) 选择类型 2) 后台采集并显示结果
    # ================================================================ */
    logger.info('开始创建窗口')
    root = tk.Tk()
    root.title('携程点评采集 · 测试版 0.11')
    root.geometry('900x850')
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='网络采集：酒店、景点、餐饮、购物。', font=('Microsoft YaHei UI', 13)).pack(anchor='w')
    ttk.Label(frame, text='酒店会打开 Edge，首次需自行登录；采集评论和网页返回的 AI 总结。').pack(anchor='w', pady=(4, 10))
    mode = tk.StringVar(value='network')
    fast_ui = tk.BooleanVar(value=True)
    modes = ttk.Frame(frame)
    modes.pack(fill='x')
    ttk.Radiobutton(modes, text='网络采集（不需要手机）', value='network', variable=mode).pack(side='left')
    ttk.Radiobutton(modes, text='手机评论补采（先打开全部评论）', value='phone', variable=mode).pack(side='left', padx=12)
    ttk.Radiobutton(modes, text='酒店 AI 总结', value='ai', variable=mode).pack(side='left')
    ttk.Radiobutton(frame, text='城市酒店/景点/餐饮目录：填城市链接，生成对象清单后转入网络模式', value='discover', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='购物目录：下框填写携程地区数字 ID（上海为 2），生成后转网络模式', value='shopping_discover', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='导入 App 响应：在下框填写受限抓包 JSONL 文件路径', value='capture', variable=mode).pack(anchor='w')
    ttk.Radiobutton(frame, text='下载评论图片：在下框填写已导出评论 JSONL 路径；数量框为图片上限', value='media', variable=mode).pack(anchor='w')
    ttk.Checkbutton(frame, text='手机使用快速页面读取（uiautomator2；不要同时运行 Mobile MCP）', variable=fast_ui).pack(anchor='w')
    ttk.Label(frame, text='对象详情链接，每行一个（酒店 hotels.ctrip.com；景点 / 餐厅 / 购物 you.ctrip.com）').pack(anchor='w', pady=(8, 2))
    urls_box = tk.Text(frame, height=3, wrap='word')
    urls_box.pack(fill='x')
    values = {}
    # 1.1 明确对象标签，避免把不同对象写入同一数据集。
    for label, key, default in [('对象名称（手机模式填写）', 'place', ''), ('酒店 ID（AI 模式填写）', 'resource_id', ''), ('每对象目标累计条数', 'limit', '500'),
                                ('目录页数上限', 'pages', '5'), ('目录对象上限', 'objects', '50'),
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
            if target < 1 or (selected_mode in ('phone', 'ai') and not values['place'].get().strip()) or (selected_mode in ('network', 'discover', 'capture', 'media') and not urls):
                raise ValueError()
            if selected_mode in ('capture', 'media') and (len(urls) != 1 or not Path(urls[0]).is_file()):
                raise ValueError()
            if selected_mode == 'discover' and (len(urls) != 1 or int(values['pages'].get()) < 1 or int(values['objects'].get()) < 1):
                raise ValueError()
            if selected_mode == 'shopping_discover' and (len(urls) != 1 or not urls[0].isdigit() or int(values['pages'].get()) < 1 or int(values['objects'].get()) < 1):
                raise ValueError()
            if selected_mode == 'ai' and (kind.get() != 'hotel' or not values['resource_id'].get().strip().isdigit()):
                raise ValueError()
        except ValueError:
            messagebox.showerror('参数错误', '网络模式填写详情链接；手机模式填写对象名称；AI 模式选择酒店并填写数字酒店 ID。目标条数必须为正整数。')
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
                if selected_mode == 'media':
                    result = download_media(Path(urls[0]), Path(selected['folder']), target, stop)
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
                elif selected_mode == 'capture':
                    result = import_capture(Path(urls[0]), Path(selected['folder']), target, stop,
                                            lambda msg: events.put(('log', msg)))
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
                elif selected_mode in ('discover', 'shopping_discover'):
                    finder = discover_shopping if selected_mode == 'shopping_discover' else discover_objects
                    result = finder(urls[0], Path(selected['folder']), int(selected['pages']), int(selected['objects']), stop)
                    events.put(('log', json.dumps({k: v for k, v in result.items() if k != 'objects'}, ensure_ascii=False, indent=2)))
                    if result['objects']:
                        events.put(('urls', '\n'.join(obj['url'] for obj in result['objects'])))
                elif selected_mode == 'network':
                    result = collect_batch(urls, Path(selected['folder']), target, collect_url, stop,
                                           lambda msg: events.put(('log', msg)))
                    events.put(('log', json.dumps(result, ensure_ascii=False, indent=2)))
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
    parser.add_argument('--adb', default=str(HOME / 'platform-tools' / 'adb.exe') if (HOME / 'platform-tools' / 'adb.exe').exists() else shutil.which('adb') or 'adb')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--gui-smoke', action='store_true')
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--url', action='append', default=[], help='公开点评对象详情链接，可重复指定')
    parser.add_argument('--import-capture', type=Path, help='导入受限 App 点评响应 JSONL')
    parser.add_argument('--download-media', type=Path, help='从已导出评论 JSONL 下载返回的图片版本')
    parser.add_argument('--media-limit', type=int, default=20, help='本次处理的不同图片URL上限')
    parser.add_argument('--capture-ai', action='store_true')
    parser.add_argument('--resource-id', default='')
    parser.add_argument('--city-list', help='城市酒店、景点或餐饮目录链接，酒店示例：https://hotels.ctrip.com/hotel/shanghai2')
    parser.add_argument('--shopping-district', type=int, help='发现购物目录，携程地区ID，上海为2；0表示全球')
    parser.add_argument('--discovery-pages', type=int, default=5)
    parser.add_argument('--max-objects', type=int, default=50)
    parser.add_argument('--collect-discovered', action='store_true')
    parser.add_argument('--url-file', type=Path, help='每行一个对象详情链接的 UTF-8 文件')
    parser.add_argument('--kind', choices=['hotel', 'sight'], default='hotel')
    parser.add_argument('--place', default='')
    parser.add_argument('--output', type=Path, default=HOME / 'data')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--max-swipes', type=int, default=1500)
    parser.add_argument('--ui-backend', choices=['adb', 'uiautomator2'], default='uiautomator2')
    args = parser.parse_args()
    # 3.0.0 图片任务与评论采集分开，避免参数混用触发额外请求。
    if args.download_media:
        if args.url or args.url_file or args.city_list or args.shopping_district is not None or args.collect or args.capture_ai or args.import_capture:
            parser.error('--download-media 不能与其他采集任务同时使用')
        result = download_media(args.download_media, args.output, args.media_limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'error':
            raise SystemExit(1)
        return
    # 3.0.1 离线导入不启动浏览器、不连接手机。
    if args.import_capture:
        if args.url or args.url_file or args.city_list or args.shopping_district is not None or args.collect or args.capture_ai:
            parser.error('--import-capture 不能与其他采集任务同时使用')
        result = import_capture(args.import_capture, args.output, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(item['reason'] == 'error' or item['export_error'] for item in result):
            raise SystemExit(1)
        return
    # 3.0 目录先生成可核对的对象清单；命令行可明确选择接着采集。
    if args.city_list and args.shopping_district is not None:
        parser.error('酒店/餐饮/景点目录与购物目录不能同时运行')
    if args.collect_discovered and not args.city_list and args.shopping_district is None:
        parser.error('--collect-discovered 必须与 --city-list 或 --shopping-district 一起使用')
    if args.city_list or args.shopping_district is not None:
        finder = discover_shopping if args.shopping_district is not None else discover_objects
        entry = args.shopping_district if args.shopping_district is not None else args.city_list
        result = finder(entry, args.output, args.discovery_pages, args.max_objects)
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
    # 3.1 自检只读取设备；命令行采集必须明确对象名称。
    if args.url:
        if args.limit < 1:
            parser.error('limit 必须大于零')
        result = collect_batch(args.url, args.output, args.limit, collect_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'error':
            raise SystemExit(1)
    elif args.capture_ai:
        with Adb(args.adb, ui_backend=args.ui_backend) as adb:
            adb.connect()
            print(json.dumps(capture_ai(adb, args.kind, args.place, args.resource_id, args.output), ensure_ascii=False, indent=2))
    elif args.check:
        adb = Adb(args.adb)
        adb.connect()
        print(json.dumps({'device': 'authorized', 'ctrip': 'installed'}, ensure_ascii=False))
    elif args.collect:
        if not args.place.strip() or args.limit < 1 or args.max_swipes < 0:
            parser.error('必须指定 --place，limit > 0，max-swipes >= 0')
        with Adb(args.adb, ui_backend=args.ui_backend) as adb:
            adb.connect()
            result = collect(adb, args.kind, args.place, args.output, args.limit, args.max_swipes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['reason'] == 'error' or result['export_error']:
            raise SystemExit(1)
    else:
        gui(args.adb, args.gui_smoke)
    logger.info('程序结束')


if __name__ == '__main__':
    # 3.2 冻结后的程序也统一使用 UTF-8 输出日志。
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    main()




