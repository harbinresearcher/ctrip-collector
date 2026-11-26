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

验收证据（2026-11-26）：`tests/test_object_ref.py` 5 项（证据齐全 / 缺编号被拒 /
与手填互斥 / 旧路径不变 / 目录报告回退），512 项测试全绿。**验证性质：模拟验证**
（Mock adb + 真实 fixture + 真实 CLI 进程，但本机已无模拟器连接，未覆盖真机采集）。

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