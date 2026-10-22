# 携程点评采集器（CtripCollector）

![tests](https://github.com/harbinresearcher/ctrip-collector/actions/workflows/tests.yml/badge.svg)

> ⚠️ **开发中，非成品**。当前 `0.8`，只对下面「当前状态」表里标为完成的能力负责。
> 未完成的能力在 [ROADMAP.md](ROADMAP.md) 里列着，不要当成已有功能使用或宣传。

从携程 App / 网页采集**公开可见**的点评类数据，落地 SQLite，导出 CSV / JSONL，带 Tkinter 操作窗口和 CLI。

面向学术研究：**有界、可复现、不抽样**，数量以数据库为准，不以"长时间没有新增"当作全量完成。

## 当前状态（0.8）

| 能力 | 状态 | 备注 |
| --- | --- | --- |
| 网络点评采集（景点 / 餐饮 / 购物） | ✅ 可用 | 真实页面验证过，分页到接口返回空为止 |
| 酒店浏览器通道 + AI 总结 | ✅ 可用 | 依赖本机 Edge，首次需自行登录 |
| 城市景点 / 餐饮目录发现 | ✅ 可用 | 只做了这两类，酒店 / 购物目录未适配 |
| 持久批处理与断点续跑 | ✅ 可用 | 中断恢复做过模拟测试，未做真实强杀测试 |
| 抓包 JSONL 导入 | ✅ 可用 | 不含原始流量，字段按白名单脱敏 |
| 手机 UI 补采 | ⚠️ 受限 | 机型 / 版本强相关，只作兜底 |
| App 独有字段、AI 原图、全站目录 | ❌ 未开始 | 见 [ROADMAP.md](ROADMAP.md) |

## 功能

| 通道 | 说明 |
| --- | --- |
| 网络响应 | 酒店、景点、餐饮、购物的点评分页；酒店用本机 Edge 配置目录打开页面，监听响应并顺带取 AI 总结 |
| 手机 UI | `adb` / `uiautomator2` 读 Android UI XML，按卡片解析，适合网络接口拿不到时的补采 |
| App 响应导入 | `capture_addon.py`（mitmproxy addon）写 JSONL，`--import-capture` 导入，不保存原始流量 |
| 城市目录 | 景点/餐饮列表页翻页生成对象清单，可直接串接采集 |
| 持久批处理 | `batch.sqlite3` 记录进度，重启跳过已达标对象，中断任务重试 |
| 媒体 | 下载评论里明确返回的图片版本，不猜测原图地址 |
| 打包 | PyInstaller（`CtripCollector.spec` / `package_release.py`） |

## 快速开始

```powershell
uv venv
uv pip install -r requirements.txt
playwright install msedge        # 酒店通道依赖本机 Edge

python app.py                   # 图形界面
python app.py --check           # 环境自检
```

CLI 示例：

```powershell
# 网络采集单个酒店
python app.py --collect --url "https://hotels.ctrip.com/hotels/detail/xxxx.html" --output data

# 城市景点目录（第一页链接）→ 生成清单 → 串接采集
python app.py --city-list "https://you.ctrip.com/sight/..." --discovery-pages 5 --collect-discovered

# 导入受限 App 抓包结果
python app.py --import-capture capture/flows.jsonl

# 手机 UI 补采（设备用 USB 连接，先打开"全部评论"页）
python app.py --collect --url "https://..." --ui-backend uiautomator2
```

## 测试

```powershell
python -m unittest discover -s tests     # 53 项，覆盖解析、去重、批处理、抓包、媒体
```

回归用例基于真实页面导出的 UI XML / 响应样本，位于 `tests/fixtures/`。

## 目录

```
app.py              GUI + CLI 入口
core.py             UI XML 解析、SQLite 存储、CSV 导出
runner.py           adb / uiautomator2 采集循环
network.py          网页响应通道（点评分页、字段清洗）
hotel_browser.py    酒店专用 Edge 通道
hotel_public.py     公开详情补充（地址、设施、政策）
ai_capture.py       酒店 AI 总结采集
capture_io.py       抓包 JSONL 导入与脱敏
capture_addon.py    mitmproxy addon
discovery.py        城市目录发现
batch.py            持久批处理
media.py            评论图片下载
tests/              回归测试 + fixtures
docs/               计划与路线图
```

## 边界

- 只采公开可见数据；**不绕过验证码、不破解签名、不保存登录凭据**，抓包文件不进仓库。
- 每页请求走固定间隔，失败退避；触发风控即停止，不换号、不换 IP 硬刷。
- 未完成项（全站目录、App 独有字段、AI 原图、跨屏 AI 全文等）见 [ROADMAP.md](ROADMAP.md)，不要把这些当成已实现。

## 合规

个人学术研究用途。使用时请自行确认符合目标站点的用户协议与 robots 规则，控制请求频率，不对外分发原始数据。

## License

[MIT](LICENSE)
