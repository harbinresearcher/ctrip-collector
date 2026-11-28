# 目标与验收标准（Goal-Driven）

> 本文件用于多智能体按同一套标准协作：master 按这里的标准判定是否达标，
> subagent 只做这里列出的、可验证的工作。改动前先读项目 `AGENTS.md`。

## Goal

把「手机携程 App 的所有数据」变成**可判定**的事：每采一次，都能回答
「平台有多少 / 我采到多少 / 还差多少 / 差在哪一类」。

现有覆盖表按网页和 App 分别记录可核验目标、已采、缺口和未知项。各业务通过实测逐项验收，最新可用功能见 [路线图](../ROADMAP.md)。

## 最终交付验收

- **部署**。老师可在 Windows 或 Android 上安装并直接运行对应的交付版本。Windows 优先交付，包含工作台和可随包分发的运行依赖，网页和已接入的公开来源可直接采集。需从携程 App 读取的字段可用手机上的独立采集版本覆盖，该版本需单独实现和验收。模拟器只用于开发与回归验证。按 [部署手册](DEPLOYMENT.md) 分别留存依赖清单、包校验与实测字段。
- **App 更新**。用已脱敏的真实页面样本覆盖关键布局，再用当前 App 和候选版本跑同一批代表对象。记录 App 版本、布局变化、字段缺口和备用来源。受影响的通道可以暂停，其余可用通道按各自的证据报告覆盖。
- **高频异常与风控**。故障测试分别覆盖暂时失败后的有限重试、持续失败的熔断、平台拒绝的停止和已写入记录的恢复。保留便于对账的任务计数、来源、重试原因与停止原因。容量按实测和允许配额评估。
- **协作与发布**。在 GitHub 记录每个里程碑的负责方、覆盖范围、证据和未验证场景。大版本发布保留可复现的提交和标签，教师交付另外验证可执行包和迁移流程。
- **交互**。在教师交付阶段集中完成界面设计，按首次配置、创建任务、查看进度、处理暂停、续跑和下载结果的顺序验收。

## Criteria for success

四项全部满足才算达标；每项都必须有命令输出或数据库计数作为证据。

### C1c 无人值守驱动循环（下一步）

目标：把「发现 → 采集 → 对账 → 续采」串成一条能自己跑的命令，直到预算用完
或确实推不动为止。

```powershell
python app.py --drive --output <目录> [--city-list <入口链接> | --region-seeds <类型>] `
  --rounds <轮数> --discovery-pages <页> --max-objects <对象数> `
  --limit <每对象再补条数> --gap-limit <每轮处理对象数>
```

验收：

- 每轮结束重新对账并记录 `missing`；`missing` 必须**严格下降**才继续下一轮，
  否则停止并给出 `no_progress`；
- 停止原因必须是以下之一且写进报告：`round_budget_reached` / `no_progress` /
  `all_targets_met` / `cancelled`；**不允许无限循环**；
- 单对象的失败（`reason=error`、`export_error`）不能中断整轮，但要原样记进
  `rounds[]`，不许写成成功；
- 只在第一轮做目录发现；后续轮次只续采，避免每轮重复打目录接口；
- 报告里 `complete` 仍固定 `false`（清单内目标达成 ≠ 平台全量）。

验收证据：真实目录连跑 ≥2 轮，贴出 `rounds[]` 的 missing 变化；单测 5 项
（有进展→继续 / 无进展→no_progress / 目标全达成→all_targets_met /
轮数预算→round_budget_reached / 单对象失败不影响整轮）。

### C1e 长跑保护：每日配额 + 熔断 + 降速（下一步）

目标：`--drive` 能连续跑几小时而不失控——配额到点自己停，连续失败自己熔断，
速率掉到阈值以下自动降速并在报告里说明。

```powershell
python app.py --drive --output <目录> --rounds <N> `
  --daily-quota <每天最多新增条数> --breaker-failures <连续失败阈值> `
  --min-per-minute <低于该速率视为降级>
```

验收：

- 配额：同一天累计新增条数达到 `--daily-quota` 后停止，原因 `daily_quota_reached`；
  状态按日期落在输出目录（例如 `drive-state.json`），跨进程重启仍然生效，隔天自动归零；
- 熔断：一轮里连续 `--breaker-failures` 个对象失败（`reason=error`）→ 立即停止，
  原因 `circuit_open`，并把触发对象与错误原文记进报告；
- 降速：本轮 `reviews_per_minute` 低于 `--min-per-minute` → 报告标 `degraded`，
  下一轮把对象处理数减半（不清零、不放弃），并写明降速原因；
- 三种保护都要出现在报告字段里（`quota` / `breaker` / `pacing`），且**不得**
  把被保护停下的运行写成成功；
- 单测 5 项（配额到点 / 隔天归零 / 熔断 / 降速减半 / 保护字段随报告输出），不触网。

#### C1f 验收记录：已达标（2026-11-26）

真实网络对比（各自全新目录，抚松 2 个对象，每对象 5 条，只改 `--workers`）：

| 方式 | 耗时 | 处理对象 | 结果 |
| --- | --- | --- | --- |
| `--workers 1` | 13.8s | sight-136039、sight-1407863 | 各 `target_reached`，汇总 collected 10 |
| `--workers 2` | **7.6s** | 同上 | 各 `target_reached`，汇总 collected 10 |

**加速 1.82 倍**，两次采到的条数一致、无 `error`、无库忙错误。另用假 fetch 让
两个线程并发写同一个 `network.sqlite3`（各 200 条），验证 `timeout=30` 能吸收
写冲突：库内计数正确、无丢条。真实网络下 2 路并发的风控表现**未验证**，
默认仍为 1。

#### C1e 验收记录：已达标（2026-11-26）

真实配额验收（全新目录，抚松景点 2 个对象，`--limit 4 --gap-limit 2 --daily-quota 5`）：

| 次 | reason | rounds_used | used_today | 说明 |
| --- | --- | --- | --- | --- |
| 第 1 次 | `daily_quota_reached` | 1 | 8 | 一轮补了 8 条后停（配额 5，一轮跨过边界，已写入 limitations） |
| 第 2 次（新进程） | `daily_quota_reached` | 0 | 8 | 配额已用完，直接 0 轮停下 |

`drive-state.json` 按日期持久（`{"date":"2026-11-26","collected_today":8}`），跨进程生效。

真实降速验收（新建目录，`--min-per-minute 10000` 人为抬高阈值）：

| 轮 | delta | reviews_per_minute | degraded | 下一轮 gap_limit |
| --- | --- | --- | --- | --- |
| 1 | 4 | 32.0 | true | 2 → 1 |
| 2 | 2 | 36.2 | true | 保持 1 |

熔断只有单测覆盖，真实场景未触发（如实记为未验证）。533 项测试全绿。

#### C1c 验收记录：已达标（2026-11-26）

真实网络验收（全新目录，`--rounds 2 --discovery-pages 1 --max-objects 2
--limit 2 --gap-limit 2`，抚松景点目录）：

| 轮 | missing | collected | delta | 本轮 reason |
| --- | --- | --- | --- | --- |
| 1 | 1510 → **1506** | 8 → 12 | 4 | continued |
| 2 | 1506 → **1502** | 12 → 16 | 4 | round_budget_reached |

目录发现只执行 1 次（`object_budget_reached`，`listing_total=3000`，发现 2 个对象）；
`complete=false`；`limitations` 明确写出「手机与酒店对象只报不采」「清单内清零 ≠ 平台全量」
「运行期间新增对象不会被发现」。517 项测试全绿，CI success。

### C1f 对象级并发（下一步）

目标：在**不改每请求间隔**的前提下，把「一个对象接一个对象」变成可选的并行处理，
让长跑吞吐上去；默认仍是串行，不改变现有行为。

```powershell
python app.py --drive --output <目录> --workers 2 --rounds 3 --limit 200 --gap-limit 4
```

验收：

- `--workers` 默认 1（串行，行为与今天完全一致），允许 1–3，越界 `parser.error`；
- 并发只并行「不同对象」，**同一对象内部仍串行且保持原有间隔**；不允许并发翻页同一对象；
- 共享保护：配额计数、熔断阈值、`stop` 信号在线程间共享——任一工作线程触发熔断或
  人工取消，其余线程必须在当前请求结束后停下，不得继续开新对象；
- SQLite 并发写不能变成随机失败：同目录多线程写入时必须给出明确错误（或已被 busy
  timeout 吸收），不能表现为「采集随机丢条」；
- 报告里新增 `workers`，并保留每对象 `processed[]` 的 reason/error，顺序按对象键稳定；
- 单测 5 项（默认串行不变 / 2 线程都跑到 / 配额在并发下不超发 / 一个线程失败触发整体停止 /
  取消信号能终止其余线程），不触网。

### C1g 页面结构指纹与布局版本（下一步）

目标：App 或网页改版后，报告能直接说出「**哪一类页面的结构变了**」，而不是只留下 0 条。

验收：

- 新增只读的指纹函数（例如 `layout_fingerprint.py`）：对一份 UI XML 只取**结构**
  （层级、class、resource-id 尾段、是否有文字），不算正文，得到稳定短哈希；
  同一页面重复计算必须一致，只改文字不改结构时指纹不变；
- 基线文件 `tests/layout_baselines.json` 由真实 fixture 生成并纳入版本管理；
  新增一项测试：从 fixture 重算的指纹必须等于基线（页面改版会强制更新基线）；
- 采集报告新增 `layout: {family, fingerprint, expected, version, changed}`；
  `changed=true` 时报告 `layout_changed: true` 并写明期望/实际指纹，
  但**不因此中断采集**（数据仍可能解析成功）；
- 至少覆盖两类采集：手机点评（`runner.collect`）与网页点评（`network.collect_network`）；
- 单测 4 项（结构相同指纹相同 / 只改文字指纹不变 / 改结构 → `changed=true` /
  基线由 fixture 重算一致），不触网不连设备。

### C1h 风控信号识别与整体熔断（下一步）

目标：被平台限流/拦截时**立刻整轮停下并说清楚**，而不是当成普通错误继续跑下一个对象。

验收：

- 识别信号：HTTP 403 / 429 / 432 / 511，或响应体出现验证码/人机校验特征；
- 命中后：该对象结果标 `blocked=True` 且带原始状态码，**整轮立即停止**
  （`reason='blocked'`），不再对后续对象发请求；`--collect-gaps` 与 `--drive` 同规则；
- 命令行输出一句人能照做的提示（例如「平台限流（HTTP 432），请停止今天的采集，
  记录现象后明天再试；不要换 IP 或换账号重试」），退出码 1，不打印堆栈；
- 报告与 `--audit` 能看出「这次是被拦了」而不是「没数据」；
- 单测 4 项（432 识别 / 其他 HTTP 错误不算 blocked / blocked 后不再请求下一个对象 /
  命令行提示与退出码），不触网。

### C1h 验收记录：已达标（2026-11-26）

- `network.blocked_status` / `network.is_blocked`：HTTP 403 / 429 / 432 / 511，或异常原文与响应提示里的
  验证码、人机校验、请稍后再试特征判为被拦；500、超时、连接重置不算。
- `collect_network` 报告写 `blocked` / `blocked_status`（`reason` 仍为 `error`）；
  `load_target` 阶段的被拦由 `batch.collect_batch` 把异常状态码写进对象结果，不会被降级成普通错误。
- `gap_collect.collect_gaps` 与 `drive.drive_rounds` 看到 `blocked=True` 立即整轮停止（`reason='blocked'`），
  串行与并发都不再对后续对象发请求，报告记录被拦对象与状态。
- `--audit` 对象行新增 `blocked` / `blocked_status`；被拦对象 `gap_reason='blocked'`，
  不再进入续采队列，与「没数据」的 `missing` 分开。
- 命令行捕获被拦时只打印一句「平台限流（HTTP 432），请停止今天的采集……不要换 IP 或换账号重试。」并退出 1，无堆栈。
- 单测 4 项（`tests/test_platform_block.py`），不触网；582 项测试全绿（skipped=2）。

模拟验证（构造 432 响应，不触网）：对象报告 `blocked=True, blocked_status='432', reason='error'`；
`--audit` 同一对象为 `gap_reason='blocked'`、`blocked=true`、`missing` 仍如实保留 1095；
`--collect-gaps` 命令行退出码 1，只多一行当天停跑提示，`reason='blocked'`，后续对象零请求。

### C1g 验收记录：已达标（2026-11-26）

- `layout_fingerprint.py`：UI XML 走「节点序列」指纹，网页走「页面状态 JSON」指纹，
  两套明确不可互换（网页不保留原始 HTML，用臆造 HTML 建基线违反「不臆造样本」）；
- 基线 `tests/layout_baselines.json` 由真实 fixture 生成，测试断言重算一致；
- 报告新增 `layout {family, fingerprint, expected, version, changed}`，
  `changed=true` 只标记不中断（`layout_changed` + limitation 说明）。

真机验证（2026-11-27，`emulator-5556`）：

- 云冈石窟与长白山西景区两屏真实点评页 → 同一指纹 `cf768820b13d178f`；
- 年度榜单页（没有点评锚点）→ 指纹为 `None`，报告不报警；
- 指纹只算锚点（`o_gs_comments*` / `htl_x_htlcmtlist*`）子树内深度 ≤3 的去重结构，
  因此滚动位置、卡片数量、卡片内部差异都不影响它；卡片内部的变化交给 C3 未知布局上报。

真实采集验证（`--url .../136039.html --limit 8` 的真实报告）：

```json
{"family": "web_reviews", "fingerprint": "697dd017b08b88e3",
 "expected": "697dd017b08b88e3", "version": 1, "changed": false}
```

模拟验证：同一 fixture 改一个 TextView 的 class → `changed=true`、`count` 仍为 1
（改版不会把采集判成失败）。562 项测试全绿。

### C1i 手机侧缺口队列（下一步）

目标：把「下一步该在手机上采哪个对象」变成可判定输出——不用凭记忆翻目录，
也不会因为忘了哪个对象没采而漏。

```powershell
python app.py --phone-queue --output <目录> --max-objects 5
```

验收：

- 只读：不连设备、不发网络请求，读对账与手机库后输出工作清单；
- 每个条目给：`object_key`、`place`、`kind`、对象链接（来自证据）、
  `target`（手机侧「全部」计数；拿不到就 `target_unknown`）、
  `phone_collected`、`missing`、`reason`（`never_collected` / `partial` /
  `channel_capped` / `blocked` / `target_unknown`）、以及可直接复制的
  `command`（`--collect --object-ref <key> --limit <n>`）；
- 排序可判定：已知缺口按 missing 降序；`never_collected` 排在「已采过但还缺」前面；
  `channel_capped` 与 `blocked` 单独列在 `skipped[]` 并说明原因，不混进待办；
- 输出同时给 `summary{objects, with_phone_gap, never_collected, target_unknown}`；
- 单测 5 项（从未采过 / 部分已采 / 目标未知 / 采尽与拦截进 skipped /
  排序与命令字段），不触网不连设备。

#### C1i 验收记录：已达标（2026-11-27）

真机数据（`95e007f0` 真机 + `.test-tmp\real` 目录）上的实际输出：

```
summary: {objects: 3, with_phone_gap: 1, never_collected: 2, target_unknown: 2, skipped: 0}
sight-1407863  万达长白山国际滑雪场  target=None phone=0    missing=None  never_collected
sight-5706212  长白山万达国际度假区  target=None phone=0    missing=None  never_collected
sight-136039   长白山西景区        target=1099 phone=13   missing=1086  partial
              -> --object-ref sight-136039 --limit 213（已采 13 + 建议 200）
```

照着队列命令在真机上开工：`--limit 60 --max-swipes 12 --adb-serial 95e007f0`
→ 13 屏、13 条、新增 9 条、37.3 秒；`layout.changed=false`；
再对账 `reviews_scoped` 4 → 13，队列里 `missing` 由 1095 降到 1086。
手机侧「全部」计数来自手机自己的页面（1099），不与网页目标混用。

顺带修掉一个真机阻塞：两台设备同时在线时命令行采集直接拒绝，
`--adb-serial` 原本只允许配合 `--workbench`；现在命令行也能指定设备。

**三对象补齐（同一真机，2026-11-27）**：把另外两个「从未采过」的对象也打开采了，

| 对象 | 手机侧目标（页面「全部」） | 手机已采 | 网页已采 |
| --- | --- | --- | --- |
| sight-136039 长白山西景区 | 1099 | 13 | 577 |
| sight-1407863 万达长白山国际滑雪场 | 357 | 10 | 61 |
| sight-5706212 长白山万达国际度假区 | 62 | 7 | 56 |

队列从 `never_collected:2, target_unknown:2` 变成 `never_collected:0, target_unknown:0`，
三个对象都给出真实 `missing`（1086 / 347 / 55）。**手机侧目标与网页目标完全一致**
（1099 / 357 / 62），是两个独立来源互相印证，不是把网页数字挪过来用。

导航过程可复用：`ctrip://wireless/search?keyword=<urlencode(对象名)>` 深链打开搜索结果，
再点第一条结果与「N条点评」即可；中文对象名用 uiautomator2 的 `set_text` 输入。

### C1j 四类手机通道逐个验证（进行中）

目标：景点之外的三类（餐饮 / 酒店 / 购物）都要在真机上验证「能打开、能解析、能落库、身份可核」，
不能停在「同一套解析应该通用」的假设上。

| 类型 | 状态 | 证据 |
| --- | --- | --- |
| 景点 | ✅ 已验证 | 3 个对象：1099 / 357 / 62（见 C1i） |
| 餐饮 | ✅ 已验证（1 个对象） | `restaurant-135978570`：真机 4 屏 4 条，`identity_source=evidence_manifest`，`layout.family=mobile_reviews_restaurant`（无基线 → `expected=null, changed=false`，不误报） |
| 酒店 | ⛔ 不动（需要登录） | 2026-11-27 在模拟器上实测：点酒店搜索结果卡片后直接跳到 `CtripLoginActivity`（手机验证码登录）。按项目规则「不保存登录凭据」，不代为登录，因此酒店手机点评不在本项目范围内 |
| 购物 | ⏳ 未验证（App 侧加载失败） | 购物地点详情页能打开（`cdf三亚国际免税城`，`4974条点评`），但点进点评列表稳定返回「加载未成功(-503)」，重试两次都是同一结果；不是本仓库的解析问题 |

验收口径：每类都要有一份真机报告 + 一次对账，并如实标注 `expected=null`（无基线时不声称「结构没变」）。

餐饮顺带发现并修掉：真机页签写的是「视频/图片」（与景点页「图片/视频」词序相反），
原来的筛选集合不收这个写法，计数会漏一个页签。

### C1l 统一覆盖率总表（下一步）

目标：把「离全量还有多远」变成一行数字——同一批对象、两条通道（网页 / 手机）各自的
目标与已采，以及整体状态分布；**不许把两条通道的已采数相加冒充覆盖**。

验收：

- --audit 的 summary 增加 coverage 块（additive，不改现有字段）：
  - web {target, collected, missing}、phone {target, collected, missing}；
  - objects {total, complete, partial, unknown_target, no_phone_data}；
  - est_effort {covered, target, ratio}：每个对象取「两条通道里已采更多的那个」，
    按对象汇总；ratio 为 covered/target，target 未知的对象不计入分母；
  - 
ote：一句话写明「网页与手机没有共同主键，不能相加去重」；
- 目标未知（	arget_unknown）的对象单独计数，不冒充 0；
- 页面/手机数据缺失时字段为 
ull，不写 0；
- 单测 4 项（两通道都有数据 / 只有网页 / 只有手机 / 目标未知不计入分母），不触网；
- 真实目录上跑一次，把数字写进验收记录。

#### C1l 验收记录：已达标（2026-11-27）

真实目录 `.test-tmp\real`（3 个对象，网页目标 1099/357/62，手机目标同为 1099/357/62）：

```json
{"web":   {"target": 1518, "collected": 694, "missing": 824},
 "phone": {"target": 1518, "collected": 30,  "missing": 1488},
 "objects": {"total": 3, "complete": 0, "partial": 3, "unknown_target": 0, "no_phone_data": 0},
 "best_effort": {"covered": 694, "target": 1518, "ratio": 0.457},
 "note": "网页与手机没有共同主键，不能相加去重；best_effort 表示每个对象取已采更多的那条通道"}
```

#### C1l 城市级复核（2026-11-28，抚松景点目录）

`--city-list .../sight/fusong2479/s0-p1.html --collect-discovered --discovery-pages 2
--max-objects 30 --limit 30`（90.6 秒，19 个对象、479 条），审计输出：

```json
{"web":   {"target": 3999, "collected": 479,  "missing": 3520},
 "phone": {"target": 520,  "collected": 21,   "missing": 499},
 "objects": {"total": 19, "complete": 0, "partial": 19, "unknown_target": 0, "no_phone_data": 18},
 "best_effort": {"covered": 479, "target": 3999, "ratio": 0.1198},
 "discovery": {"listing_total": 3000, "discovered": 19, "ratio": 0.0063}}
```

**两级覆盖率，结论不同**：

- **点评采集**覆盖率 11.98%（479/3999）——19 个对象的点评我们采到了近 12%；
- **对象发现**覆盖率 0.63%（19/3000）——目录里只有 19 个对象被我们发现。

也就是说，城市级的瓶颈在**对象发现**，不在点评采集；而且 `listing_total=3000`
是整千值，很可能是平台截断上限，真实目录总数未知（已在 `discovery.note` 写明）。

手机侧：真机 `95e007f0` 上采了「汉拿山温泉」（`sight-143364`）21 条，
`phone_target=520`（来源 `all_label`）、`phone_missing=499`；
另外 18 个对象还没有手机数据（`no_phone_data=18`）。

读法：单对象目录的覆盖率 **45.7%**（694/1518），三条通道都还是部分完成；`phone.collected=30`
是手机侧真实入库条数，**没有**和网页 694 相加去当「总计」。原有 8 个 summary
字段一字未改。641 项测试全绿。

### C1m 「通道碰到上限」不要说成「采尽」（已达标，2026-11-28）

Codex 在 `docs/DEPLOYMENT.md` 第 4 节把口径写严了：**只有核对了末页或同口径的全部展示数，
才可以确认该范围采完**。此前代码里连续两次空页就标 `source_exhausted` 并把
`reachable_missing` 记 0，这是**过度声称**——真机反证：长白山西景区网页只采到 577，
手机页面显示「全部 1099」，说明是网页通道翻页到上限，不是这个对象采完了。

目标：把「碰到通道上限」和「真的采完」分开，且不让 `--drive` 因此空转。

验收：

- 缺口原因从 `source_exhausted` 改为 `channel_capped`（`capped=true`），
  语义是「这条通道翻到上限/末页，但**没有**同口径全部展示数可以证明采完」；
- `reachable_missing` 只有在 `missing == 0`（真采完）时才为 0；`channel_capped` 的对象
  保留真实缺口，并新增 `needs_other_channel: true`；
- `--drive` / `--collect-gaps` 仍然**跳过** `channel_capped` 的对象（否则会无限空转），
  但报告与对账要让读者看到「是通道到顶，不是这对象没了」；
- 历史记录（`audit-history.jsonl` 里旧的 `source_exhausted_count`）继续可读，
  不因改名报错；
- 单测 4 项（连续空页→`channel_capped` 且缺口保留 / `missing==0` 时才是采完 /
  续采队列仍跳过被限对象 / 旧历史行仍可读），不触网；
- 真实目录 `.test-tmp\real` 上复核：`sight-136039` 应为 `channel_capped`，
  `missing` 仍为 522，且审计输出里能看出「网页 577 / 手机目标 1099」。

#### C1m 验收记录：已达标（2026-11-28）

真实目录 `.test-tmp\real` 上 `--audit` 输出（旧 24 行历史带
`source_exhausted_count`，回读无坏行）：

```json
{"object_key": "sight-136039", "target": 1099, "collected": 577, "missing": 522,
 "gap_reason": "channel_capped", "capped": true, "needs_other_channel": true,
 "reachable_missing": 522, "phone_target": 1099, "phone_collected": 13, "complete": false}
summary: {"target": 1518, "collected": 694, "missing": 824,
          "reachable_missing": 824, "capped_count": 3}
history 最新行: {"missing": 824, "reachable_missing": 824, "capped_count": 3}
```

`limitations` 新增一句：通道到顶不等于采完，只有核对末页或同口径全部展示数才算采完；
`channel_capped` 只表示这条通道翻到上限/末页，缺口仍计入 `reachable_missing`，需换
手机等其它通道。669 项测试全绿（skipped=4）。

### C1o 城市级全对象采集（86 个对象，可信分母）

修正 C1n 之后重跑：读 12 页目录发现 **86 个对象**，每个对象按 `--limit 30` 采一遍。
分母是各对象点评数之和（**不是**平台截断的 3000），因此这一轮的数字可信。

| 指标 | 数值 |
| --- | --- |
| 对象数 | 86 |
| 点评目标合计 | 4451 |
| 已采 | 698 |
| 覆盖率 | **15.68%** |
| 已达目标的object | 52 |
| 仍有缺口 | 34（其中 9 个 `channel_capped`、25 个 `missing`） |
| 手机侧 | 0（城市级还没做手机采集） |

批任务本身：`total=86, processed=74, skipped=12（已核验复用）, failed=0`。

**这一轮同时修掉一个会让整批白跑的缺陷**：第一次跑时第 24 个对象遇到
`http.client.IncompleteRead`，整批直接中断，剩下 62 个对象一个都没跑。
`batch.py` 现在记一笔失败后继续，只有「被平台拦住」或「用户取消」才停整轮，
并对 `IncompleteRead`/连接重置/超时重试一次。

### C1p 城市级手机补采（已打通第一个对象，2026-11-28）

队列按网页缺口排出的第一个对象 `sight-136039 长白山西景区（web_missing=1069）`
已在模拟器上采到手机侧数据：

- 到达判据 `arrived=True`（`all_count=1099`、对象名 exact）；
- 两轮采集：6 条（11 屏）→ 累计 16 条（26 屏），`identity_source=evidence_manifest`；
- 对账变成 `phone {target: 1099, collected: 16, missing: 1083}`，
  `no_phone_data` 从 86 降到 85。

**设备侧的坑（下次别再踩）**：之前 `adb shell uiautomator dump` 一直返回
`null root node returned by UiTestAutomationBridge`，原因是 uiautomator2 的
ATX agent 与 adb dump 桥冲突；`adb shell am force-stop com.github.uiautomator*`
之后 adb 后端立刻恢复。**同一台设备不要混用两个 UI 后端**。

另外深链 `ctrip://wireless/search?keyword=` 在这台模拟器上会落到酒店结果页，
景点卡片不在首位；按文字找卡片要认「可订明日 / 门票」这类景点行，别用「名字完全匹配」。

#### C1o 缺口续采实测（2026-11-28）

对 25 个「真缺口」对象（排除通道到顶与被拦）跑 `--collect-gaps --limit 30 --gap-limit 40`：

| 指标 | 续采前 | 续采后 |
| --- | --- | --- |
| 已采 | 698 | **983**（+285） |
| 覆盖率 | 16.36% | **22.08%** |
| 完成对象 | 52 | 53 |
| 通道到顶（capped） | 9 | **21** |
| 缺口对象（missing） | 25 | 0（25 个都被处理过） |

25 个对象的结果：9 个 `target_reached`、16 个 `empty_page`。

**16 个 `empty_page` 变成了 `channel_capped`（capped 9→21）**，这是对的：
网页通道翻到空页，但拿不到同口径的全部展示数，不能声称「这个对象采完了」。
它们要么目标（目录卡片的 comment_count）本身偏大，要么点评分散在手机通道。

连跑三轮续采后的最终状态（2026-11-28）：

| 轮次 | 处理对象 | 已采 | 覆盖率 |
| --- | --- | --- | --- |
| 起始 | — | 698 | 15.68% |
| 第 1 轮（25 个真缺口） | 25 | 983 | 22.08% |
| 第 2 轮（12 个） | 12 | 1235 | 27.75% |
| 第 3 轮（8 个，修好重试后） | 8 | **1379** | **30.98%** |

缺口分布收敛到 `channel_capped 28 / complete 53 / missing 5`——
**5 个对象还能继续补，28 个是网页通道到顶**（换手机才能再涨）。

第 2 轮里有 2 个对象因为 `IncompleteRead` 直接记 failed，第 3 轮验证了修法：
`gap_collect` 现在对瞬时读取失败重试一次，那 2 个对象在第 3 轮正常采到数据。

继续把剩下 13 个对象逐个推到「翻到空页」之后（又跑 3 轮），得到**通道上限的实测值**：

| 指标 | 数值 |
| --- | --- |
| 已采 | **1830** |
| 平台总数（分母） | 4451 → 覆盖率 **41.11%** |
| 网页通道可得量（到顶对象按已采计） | **2409** → 可达覆盖率 **75.97%** |
| 通道到顶对象 | 32 |
| 已达标对象 | 53 |
| 仍可续采 | 1 |

**这一轮的实测反证很重要**：万达长白山国际滑雪场详情页写「357 条点评」，
但接口翻到第 5 页就返回空，一共只给 **61 条**；长白山西景区写 1099，
翻到第 30 页空、只给 577。所以「平台总数」和「网页通道能给的量」是两个不同的分母：

- 相对平台总数 **41.11%**——这才是「离平台全量还有多远」；
- 相对网页通道能给的量 **75.97%**——说明我们的采集实现基本吃满了通道能力，
  剩下的 24% 里有一部分是还没到顶的对象（1 个），大头是接口本身不给。

结论：网页通道在当前 86 个对象上的天花板约 **2409 条（54% of 4451）**，
剩下的 45% 只能靠手机通道补——而手机通道约每屏 0.6 条。

#### C1p 三个对象的手机补采结果（2026-11-28）

| 对象 | 手机目标（页面「全部」） | 手机已采 | 缺口 | 屏数 / 耗时 |
| --- | --- | --- | --- | --- |
| sight-136039 长白山西景区 | 1099 | **50** | 1049 | 121 屏 / 380s |
| sight-143364 汉拿山温泉 | 520 | **40** | 480 | 68 屏 / 193s |
| sight-2506647 讷殷古城 | 450 | **30** | 420 | 67 屏 / 183s |

城市级手机侧：`phone {target: 2069, collected: 120, missing: 1949}`，
`no_phone_data` 86 → 83。三次采集都是 `identity_source=evidence_manifest`、
`reason=target_reached` 或 `screen_budget_reached`、`error` 为空。

一个观察：模拟器上手机侧约 **每屏 0.6 条**（121 屏 50 条），比网页通道慢一个量级，
所以手机侧只适合补「网页到顶」的对象，不适合替代网页通道。

对账新增 `target_mismatch`：两通道对同一对象的目标数不一致时标出
`{web, phone, delta}`。实测这三个对象两边完全一致（1099/520/450），没有分叉。

### C1p 原始记录（设备侧受阻时的笔记，保留备查）

队列已经能指出该先采哪个对象（按网页缺口降序）：

```
sight-136039 长白山西景区    web_missing=1069  target=None
sight-143364 汉拿山温泉      web_missing=490
sight-2506647 讷殷古城       web_missing=420
```

但这一轮**没有完成手机侧采集**，原因记在这里免得下次重复踩：

- 模拟器 `emulator-5556` 的 `adb shell uiautomator dump` 连续返回
  `ERROR: null root node returned by UiTestAutomationBridge`；
  `Adb.snapshot` 的重试用完后报「页面读取失败」，强制停止 App、重新深链都无效；
- 同一台设备的 `uiautomator2` 后端可以连上（`u2.connect` 正常、能 dump），
  所以下次应直接用 `--ui-backend uiautomator2`，或重启模拟器恢复 adb 后端；
- 深链 `ctrip://wireless/search?keyword=` 在这台模拟器上会落到**酒店**结果页，
  景点卡片不在首位，导致按文字找卡片失败。真机上同一深链正常。

下一步（按优先级）：重启模拟器 → 用 u2 后端打开 `长白山西景区` 点评页 →
`--collect --object-ref sight-136039 --limit 200 --ui-backend uiautomator2`
→ 重新对账，看 `phone_target/phone_collected` 是否进入城市级总表。

### C1n 目录总数可信度：截断值不能当分母（已达标 2026-11-28）

平台在目录接口里回显的 `totalCount` 可能是**截断值**。实测抚松景点目录第 1 页到
第 31 页 `totalCount` 恒为 **3000**，每页仍有 10 张卡片并夹着邻城对象。

后果：上一轮算出的「对象发现率 0.63%（19/3000）」是**用假分母算出来的错误结论**。

验收：

- `discovery` 报告记录 `cards_seen` / `own_cards` / `listing_total_suspect`（整千值标可疑）；
- `coverage.discovery` 在可疑时**不采用分母**：`listing_total=null`、`ratio=null`，
  改为给出 `discovered` 与 `pages_read`，`note` 写明原因；
- 老报告缺这些字段时按整千值兜底判断，历史目录不能继续用假分母；
- 对账的 `directories[]` 带上这几个字段；
- 单测 3 项（整千不给比值 / 正常总数仍给比值 / 显式可疑标记生效），不触网。

#### C1n 实测记录（2026-11-28）

| 读几页 | 发现对象 | 邻城对象 | 平台 totalCount | 是否有发现率 |
| --- | --- | --- | --- | --- |
| 2 | 19 | — | 3000 | ❌ 之前误算成 0.63% |
| 6 | 56 | 4 | 3000 | ❌ 不给（截断值） |
| 12 | 86 | 34 | 3000 | ❌ 不给（截断值） |

真实对象数**至少 86、上界未知**；「19 个对象」只是读了 2 页的产物。
672 项测试全绿。

### C1 覆盖台账与缺口对账

- 新增只读命令 `python app.py --audit --output <输出目录>`，输出 JSON，包含：
  - `objects[]`：每个对象的目标数（平台/接口回显的 `listing_total`、
    点评 `display_total`）、已采数（数据库计数）、缺口、缺口原因分类
    （`missing` / `unadapted` / `failed`）；
  - `directories[]`：每个目录的 `listing_total` 与已发现对象数；
  - `summary{target, collected, missing, unknown_target}`。
- 目标数拿不到时必须标 `target_unknown`，**不得**用已采数冒充目标数。
- 验收：对至少一个真实输出目录跑出真实数字；单测覆盖 4 种情形
  （目标已知且完整 / 目标已知有缺口 / 目标未知 / 数据库不存在）。

来源分开对账后，`target`、`collected`、`missing` 以及对象级 `complete` 使用网页口径。
`app_all_run` 使用单次 App 任务的「全部」精确计数和可核对的本次入库记录。旧报告、子筛选和未经确认的计数保留来源原始条数。
旧历史与网页口径不同，趋势从新口径的连续两次报告开始比较。

对实际存档重新对账时，长白山西景区的网页目标为 1,095，已采 584，缺口 511。
同目录的 App 点评 1 条单独计数，筛选范围未知。之前的结果合并成已采 585，缺口 510。见 [问题 #5](https://github.com/harbinresearcher/ctrip-collector/issues/5)。

### C2 手机侧对象 ID 证据核验（2026-11-26 修订，已达标）

原写法是「从屏幕证据里读对象 ID 再比对」。**调研后判定不可行**：30 个 fixture
全扫，`text`/`content-desc`/`resource-id` 里没有任何形如 `id=123456` /
`hotelId=` / deeplink 的对象编号（`id=` 的命中全是 `android:id/a` 这类控件 ID），
四个详情页样本（酒店/餐厅/景点/购物）正则命中数均为 0。

改为可落地的两段式：

1. **编号来自我方落盘证据**：`--collect --object-ref <object_key>` 从对象快照 /
   运行报告 / 目录报告解析 `resource_id`，记录 `identity_source='evidence_manifest'`
   与 `identity_evidence`（证据文件名）；解析不到或证据里没有正整数编号 → 一行
   错误退出，**绝不猜编号**；手输 ID 的旧路径仍是 `operator_declared_unverified`。
2. **屏幕证据**（将来若 App 真能回显编号）保留 `screen_verified_id` 作为三态之一，
   实现位置已留好，等真机出现可读编号再接入。

模拟验收包括证据齐全、缺编号被拒、与手填互斥、旧路径和目录报告回退。

实际验收使用 Android 模拟器的长白山西景区「用户点评」页。命令行和 Chrome 工作台各完成一次
1 条点评采集。编号来自 `sight-136039-object.json`，App 页面核对了名称，编号未在屏幕核验。
工作台的 CSV、历史预览和变化摘要保留了编号来源。餐厅点评图集选择入口的测试使用模拟设备。

### C3 未知布局显式上报

- 解析器拒绝未知布局时（例如点评图片 2 列 + 图片数徽标的 4 子节点结构），
  必须计数并写进报告（如 `card_fields.media_layout_unrecognized`），
  不能表现成「这条点评没有图片」。
- 验收：构造或使用真实样本证明该行被记为未适配；单测断言该字段出现。

### C4 交付纪律

- `python -m unittest discover -s tests` 全绿；CI success；一个提交一件事；
  提交消息中文、写清验证方式；`main` 只接收已验证提交。

## 分工

| 角色 | 负责 |
| --- | --- |
| DSH（master） | 定标准、判定达标、修实现缺陷、真机验证、与 Codex 在 GitHub 交流 |
| Codex | 功能与产品决策；按 C1–C3 的验收标准实现或调整方案 |
| subagent | 按本文件逐项实现并自测，证据写在提交消息里 |

## 不做什么

- 不绕过验证码、不破解签名、不保存登录凭据。
- 不把「本次任务采到」说成「平台全量」；`complete` 仍只在有对账证据时为真。

## 验收记录

### C1 覆盖台账：已达标（2026-11-26）

真实采集（抚松景点目录，1 页 / 3 个对象 / 每对象 3 条）后跑只读对账：

```powershell
python app.py --city-list https://you.ctrip.com/sight/fusong2479/s0-p1.html `
  --discovery-pages 1 --max-objects 3 --collect-discovered --limit 3 --output <目录>
python app.py --audit --output <目录>
```

实测输出（真实数字，非模拟）：

| 层级 | 目标 | 已采 | 缺口 |
| --- | --- | --- | --- |
| 目录 sight-2479 | 3000（`listing_total`） | 3 个对象 | 2997 |
| sight-136039 | 1099（`display_total`） | 3 | 1096 |
| sight-1407863 | 357 | 3 | 354 |
| sight-5706212 | 62 | 3 | 59 |

`summary`：`target 1518 / collected 9 / missing 1509 / unknown_target 0`，`complete=false`。
这是项目第一次能用数字回答「还差多少」。

### C3 未知布局上报：已达标（2026-11-26）

两列图片行补上图片数徽标（4 个子节点）时，该点评现在记
`card_fields.media_layout_unrecognized = 1`，不再表现成「没有图片」；
测试 `tests/test_restaurant_review_tiles.py::test_two_columns_with_a_badge_is_reported_as_unadapted`。

### C1 追加验收：缺口会随采集下降（2026-11-26）

在同一个真实目录上，把 `sight-136039` 从 3 条采到 30 条后再对账：

| 对象 | 目标 | 已采 | 缺口 |
| --- | --- | --- | --- |
| sight-136039 | 1099 | 3 → **30** | 1096 → **1069** |
| sight-1407863 | 357 | 3 | 354 |
| sight-5706212 | 62 | 3 | 59 |

`summary`：`collected 9 → 36`、`missing 1509 → 1482`。增量正好等于新增的 27 条，
说明对账读的是最新数据、去重没有重复计数——「报缺口 → 补采 → 缺口变小」这条
闭环在真实数据上成立。

### C1b 缺口驱动续采：已达标（2026-11-26）

`python app.py --collect-gaps --output <目录> --limit N [--gap-limit K]`：
只读对账 → 按 `missing` 降序挑对象 → 复用既有网络采集链路 → 采完重新对账，
输出 `before` / `after` / `processed[]`。

`--limit N` 的含义是「**本次每个对象最多再补 N 条**」，目标 =
`min(已有 + N, 平台总数)`。这一条有真实教训：最初把它当成对象总目标，
对已有 30 条、平台 1099 条的对象传 `--limit 10` 会直接空跑（目标早达成），
缺口一点不降却报 `gap_processed`。

真实连续两轮验收（同一目录，`--limit 5 --gap-limit 1`）：

| 轮次 | 对象缺口 | 整体 collected | 整体 missing |
| --- | --- | --- | --- |
| 第 1 轮 | 1049 → 1044 | 61 | 1457 |
| 第 2 轮 | 1044 → 1039 | 66 | 1452 |

每轮稳定下降 5 条，缺口可以反复驱动。酒店（需人工登录）与手机对象不在自动
续采范围，缺口只报不采，需用 `--url` / 工作台处理。

多对象一轮（`--limit 3 --gap-limit 3`，缺口降序）：三个对象各补 3 条，
`missing 1452 → 1443`，`target_used` 分别是 63 / 6 / 6（= 已有 + 3，均未超平台总数）。
