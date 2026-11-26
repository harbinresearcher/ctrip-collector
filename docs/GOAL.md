# 目标与验收标准（Goal-Driven）

> 本文件用于多智能体按同一套标准协作：master 按这里的标准判定是否达标，
> subagent 只做这里列出的、可验证的工作。改动前先读项目 `AGENTS.md`。

## Goal

把「手机携程 App 的所有数据」变成**可判定**的事：每采一次，都能回答
「平台有多少 / 我采到多少 / 还差多少 / 差在哪一类」。

当前状态（2026-11-26）：采集主干（酒店、景点、餐厅、购物四类的网络与手机通道）
已通，但 `complete` 永远是 `false`，覆盖只有本地计数，没有对账。

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
