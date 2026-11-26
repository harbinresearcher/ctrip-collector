# 携程点评采集器

![tests](https://github.com/harbinresearcher/ctrip-collector/actions/workflows/tests.yml/badge.svg)

当前版本 **0.24.0**。Windows 电脑上的本地采集工作台，读取携程网页和 Android 模拟器内可见的资料，保存到本地目录。工作台可以查看采集范围、预览点评并导出 CSV、JSON 或适用的 ZIP。

## 在 Windows 启动

使用 Python 3.12，在仓库目录运行。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py --workbench --output data
```

电脑浏览器会打开工作台。网页采集和检查已有数据可以直接使用。需要读取 App 时，在 Android 模拟器里打开携程，再把工作台绑定到它。

```powershell
adb devices
.\.venv\Scripts\python.exe app.py --workbench --output data --adb adb --adb-serial emulator-5554
```

按 `adb devices` 的结果替换设备编号。酒店的部分网页任务会在独立的浏览器资料目录中打开 Edge 或 Chrome，并提示手动登录。详细操作见 [工作台使用指南](docs/WORKBENCH_USAGE.md)。

## 已接入工作台

| 入口 | 可读取和导出的内容 |
| --- | --- |
| 网页点评与公开资料 | 酒店、景点、餐饮和购物的资料或点评，按入口和页面实际返回保存 |
| 对象发现 | 城市与多地区目录、周边推荐、逐层发现景点、按已保存的多地区队列继续 |
| Android 列表与详情 | 酒店搜索及房型、景点详情与指南、城市美食与购物列表、餐厅与购物地点详情、餐厅推荐菜清单 |
| Android 点评 | 酒店、景点、餐厅和购物地点，支持本次样本、累计 CSV 和已保存任务恢复，酒店和餐厅可导出点评图片裁图 |
| 餐厅点评图集 | 下载 App 保存版图片，ZIP 清单按页码和点评 CSV 关联 |
| App AI 总结和图集 | 酒店与景点的可见总结，景点尝试展开总结；景点用户实拍图集可裁图、导出和续采 |
| 自选字段与截图 | 读取当前 App 页面或已保存页面中的指定字段；在 Windows 识别截图所选区域的文字 |
| 本地覆盖概览 | 按采集来源、对象和字段查看已有记录，工具会报告停止原因及显示数 |

实测范围包括东方明珠的展开总结与图集续采、上海美食与购物列表、餐厅和购物点评样本，以及 Mercato 和 PHENIX 的推荐菜清单、Mercato 两条点评的 2 页和 15 页图集。App 任务显示的总数、页面核对结果和实际读到的记录分别写入报告。平台全量、所有 App 字段和媒体原文件仍待验证，详见 [路线图](ROADMAP.md)。

长期运行、App 更新韧性、风控处理与日志监控的约定见 [部署与运维方案](docs/DEPLOYMENT.md)；验收标准见 [docs/GOAL.md](docs/GOAL.md)。

## 覆盖台账与缺口续采

三条只读/自动命令回答「还差多少」并自己补采。验收标准见 [docs/GOAL.md](docs/GOAL.md)。

```powershell
# 1. 对账：平台总数 vs 已发现对象 vs 每个对象已采数
.\.venv\Scripts\python.exe app.py --audit --output data

# 2. 缺口续采：按缺口从大到小，每个对象本次再补 10 条（最多处理 5 个对象）
.\.venv\Scripts\python.exe app.py --collect-gaps --output data --limit 10 --gap-limit 5

# 3. 无人值守循环：首轮发现 → 逐轮续采 → 每轮重新对账，直到轮数或缺口不再下降
.\.venv\Scripts\python.exe app.py --drive --output data `
  --city-list https://you.ctrip.com/sight/fusong2479/s0-p1.html `
  --rounds 5 --discovery-pages 2 --max-objects 20 --limit 20 --gap-limit 5
```

`--limit` 表示「每个对象本次最多再补多少条」，实际目标 = `min(已有 + N, 平台总数)`。
目标数只取报告里平台回显的总数，取不到就标 `target_unknown`，**不会用已采数冒充目标数**。
`--drive` 只在首轮执行目录发现；酒店（需人工登录）与手机对象只报缺口、不自动采。
`complete` 始终为 `false`：清单内目标达成不等于平台全量。

## 开发和测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

`app.py` 保留原图形窗口和 CLI。`workbench_server.py` 提供本机服务，`workbench.html` 提供浏览器界面。`runner.py` 读取模拟器页面。`network.py` 和 `hotel_browser.py` 提供网页采集。`CtripCollector.spec` 用于构建 Windows 程序。

[MIT 许可证](LICENSE)
