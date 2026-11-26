# 部署与运维方案（含更新韧性、风控处理、监控）

> 这份文件回答「怎么长期跑、App 更新怎么办、遇到风控怎么办、出问题怎么发现」。
> 采集边界的硬性规定见 `AGENTS.md`，本文不重复也不放宽。

## 1. 交付形态

| 形态 | 用途 | 产物 |
| --- | --- | --- |
| 源码运行 | 开发与自用 | `.venv` + `python app.py --workbench --output data` |
| 免安装包 | 给不写命令的使用者 | `package_release.py` 打出的 ZIP（含 `CtripCollector.exe` 与 SHA256SUMS） |
| 输出目录 | 唯一数据落点 | `data/`（数据库、CSV、报告、证据）；可整体搬走或备份 |

- 版本号 `major.minor`，每次交付打 tag；README 状态表与 ROADMAP 同步更新。
- 输出目录结构保持向后兼容：新版本必须能读旧目录（`coverage_audit` 与
  `workbench_preview` 对旧表只读，不改旧字段）。
- 回滚：保留上一个 tag 的 ZIP 与输出目录副本；数据目录不随程序回滚，只读兼容。

## 2. 备份与迁移

- `data/` 里 `*.sqlite3` 用 SQLite 在线备份（`VACUUM INTO`）而不是直接复制，
  避免拷到半个事务。
- CSV/JSONL/报告是可重建派生物：丢了可以重跑 `--audit` 与导出；数据库与
  `*-object.json` 快照才是证据本体，优先备份这两类。
- 迁移到新机器：整目录复制 + 重新 `pip install -r requirements.txt`；
  Playwright 浏览器资料目录（酒店登录）单独迁移，不放进仓库。

## 3. 扛得住 App 更新

App 改版会让布局解析失效。现行对策分三层：

1. **拒绝未知布局**：解析器只认已验证结构；形状像但没验证过的结构必须显式
   计数（如点评图片的 `media_layout_unrecognized`），不能表现成「没有数据」。
2. **样本回归**：`tests/fixtures/` 放真实页面 dump；每次改解析先用样本跑回归，
   新增布局先补样本再改代码。
3. **失效可见**：`--audit` 的 `gap_reason` 会区分 `failed` / `unadapted` /
   `source_exhausted`；App 改版后这些数字会先变，而不是静默少采。

待补（App 更新后仍可能要人介入的部分）：

- 结构指纹：为每类页面记录节点路径/文字模式指纹，改版后在报告里直接指出
  「哪一类页面的结构变了」，而不是只报 0 条。
- 布局版本号：把已验证的布局集合写成显式版本（例如 `restaurant_review_layout=3`），
  样本与代码一起升版本，便于回溯「哪次更新破坏了哪类页面」。

## 4. 风控处理（合规范围内）

硬性边界（来自 `AGENTS.md`，不因任何需求放宽）：

- 只采公开可见数据；不绕过验证码、不破解签名、不保存登录凭据。
- 固定间隔 + 失败退避；**触发风控立即停**，把现象记进 issue。
- **不换号、不换 IP 硬刷**，不做代理轮换来回避限流。

具体做法：

| 场景 | 处理 |
| --- | --- |
| 响应变空 / `empty_page` | 连续两次记 `source_exhausted`，停止续采该对象 |
| 返回错误、429、验证码页 | 立即停止整轮，`reason=error` 原样记录，等人工判断 |
| 长时间大批量 | 每对象每轮限量（`--limit`，默认 200）+ 每轮对账，跑完就停 |
| 触发风控 | 当天停跑；把 URL、时间、响应特征、报告路径记进 issue，不改参数重试 |

`--drive` 现在每轮只在 `missing` 严格下降时继续，`no_progress` 自动停——
这本身就是「不硬刷」的机制。

## 5. 异常与重试

- 重试只针对可恢复的读失败（超时、连接reset），指数退避 + 上限次数；
  一旦出现风控特征（验证码、429、空响应三次以上）立即放弃当次并记录。
- 单对象失败不中断整轮：`rounds[].processed[]` 保留 `reason` / `error` /
  `export_error`，绝不改写成成功。
- 报告写入一律原子替换；写失败记 `export_error` 并保留内存中结果。

## 6. 日志与监控

- 现状：`logging` 输出到控制台；每次运行落 `report-*.json`、`batch-report.json`、
  `audit-*.json`，`--audit` 的 `summary` 就是健康指标。
- 建议的运维节奏：
  1. 每次运行后跑一次 `--audit`，记录 `summary.missing` 与 `reachable_missing`；
  2. 每日比对：`missing` 应单调不增，`reachable_missing` 应为 0 或持续下降；
  3. 出现 `failed` / `unadapted` 数上升 → 视为页面或接口变化信号，先查样本。
- 每次 `--audit` 会把本次 `summary` 追加一行到输出目录的 `audit-history.jsonl`
  （`captured_at`、`missing`、`reachable_missing`、`failed/unadapted/source_exhausted` 计数），
  只追加不覆盖，写失败只记警告、不影响对账结果；输出 JSON 的 `history` 字段回读最近
  20 行并给出 `trend`（与上一次的 `missing` 差值），缺口变大时 `warning` 会写明多出多少条。

## 7. 明确不做

以下请求不在此项目的实现范围内，原因写在后面，避免反复讨论：

| 不做 | 原因 |
| --- | --- |
| 还原 App 的 sign 生成脚本、调用私有接口 | 属于绕过平台访问控制，也违反本仓库「不破解签名」的硬性约定 |
| 给 APK 脱壳、脱 DexGuard、解密 native so | 同上：破解技术保护措施 |
| 代理轮换 / 多账号轮换来绕过限流与风控 | 直接违反「不换号换 IP 硬刷」；且会让数据来源不可信 |
| 采集非公开数据（订单、会员价、个人信息） | 超出「公开可见数据」边界 |

替代路径：优先使用已经验证过的公开网页/H5 通道；确有需要时走官方开放平台
或商务合作渠道，而不是逆向客户端。