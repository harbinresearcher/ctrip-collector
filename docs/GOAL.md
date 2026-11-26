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

### C2 手机侧对象 ID 证据核验

- 手机采集时若屏幕证据里能读到对象 ID（详情页 URL、deeplink、评论页可见 ID），
  必须与声明值比对：一致 → `screen_verified_id`；不一致 → 停止并报
  `identity mismatch`；读不到 → 保持 `operator_declared_unverified`。
- 验收：`tests/fixtures/` 中至少 1 个带 ID 证据的真实页面样本；单测 3 项
  （一致 / 不一致 / 无证据），报告字段能区分三者。

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