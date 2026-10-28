# 通晓古今 · 开发规格（AI 编码手册 v1.0）

> 配套 `PRD-通晓古今-v2.0（完整产品需求书）.md`（下称 PRD）。
> **本文档只解决一个问题：把 PRD 翻译成 AI 能一次写对的契约** —— 技术栈、建表、接口、算法、任务卡、验收用例。
> 分工：**PRD 说"做什么/怎么验收"，本文档说"怎么写/写在哪"**。冲突时以 PRD 为准，并把冲突记进 §12。

---

## 1. 技术栈（已定，不要另行选型）

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.13 | 与 OCR/字体/PDF 生态一致 |
| Web | **FastAPI + Uvicorn** | 异步、自带 OpenAPI、单人可维护 |
| 模板 | **Jinja2 + Alpine.js + 原生 JS** | **无构建步骤**，AI 改完即跑；不引入 React/Vite |
| 数据库 | **SQLite（WAL）→ PostgreSQL** | M1a 用 SQLite 零运维；触发条件见 §2.2 |
| ORM | SQLAlchemy 2.0 + Alembic | 迁移可追溯 |
| 队列 | **自建 DB 队列 + 独立 worker 进程** | 不引 Celery/Redis（单人运维成本 > 收益） |
| 图片 | Pillow + opencv-python-headless | 双页检测与裁边 |
| Word | python-docx | 横排 Word 直接生成，不走 Chromium |
| PDF | **Playwright（Chromium）** | 与预览同源渲染（PRD FR-EXP-06） |
| 字体子集 | fonttools / pyftsubset | M-13 体积红线 |
| 存储 | 本地磁盘 + Nginx X-Accel（或 S3 兼容） | 私有、签名 URL |
| 密码 | argon2-cffi（或 bcrypt） | 加盐哈希 |
| 测试 | pytest + httpx | 契约与资金用例 |

### 1.1 目录结构

```
app/
  main.py                 # FastAPI 装配
  config.py               # 环境变量（见 §11）
  db.py                   # engine / session / 迁移入口
  models.py               # SQLAlchemy 模型（与 §3 DDL 一一对应）
  enums.py                # 全部枚举（§4）
  deps.py                 # 鉴权、当前用户、管理员
  routers/
    auth.py  upload.py  task.py  result.py  export.py
    billing.py  recharge.py  admin.py  meta.py
  services/
    ocr/
      base.py             # OcrProvider 接口 + OcrPageResult（§5）
      xinghe.py           # 星河 PaddleOCR 实现
      glm.py              # GLM 降级实现
      normalize.py        # 归一化 + 去前言 + 字数口径
      ordercheck.py       # 行序自检（FR-OCR-06 ④）
    preprocess.py         # 双页检测/拆分/裁边（M1b）
    billing.py            # 计费算法（§7）
    payment/
      base.py             # PaymentChannel 接口（§8）
      manual.py           # 人工通道
    export/
      html_render.py      # OcrPageResult → HTML（预览与 PDF 同源）
      pdf.py              # Playwright 打印
      docx.py             # python-docx
      fonts.py            # 白名单 + 子集化 + 缺字回退
    worker.py             # 队列消费
    cleanup.py            # 72h 清理
  templates/              # Jinja2
  static/
fonts/                    # 3 个白名单字体 + font_assets 记录
tests/
  test_billing.py  test_idempotency.py  test_ordercheck.py  test_export.py
```

---

## 2. 运行与部署约束

### 2.1 进程模型

```
uvicorn (web)  ──┬─→ SQLite/Postgres
worker (1 个)  ──┘        ↑
cron: cleanup.py（每小时）/ daily_reconcile.py（每日 00:10）
```

- **worker 必须单实例**（M1a）。多实例需改用 `SELECT ... FOR UPDATE SKIP LOCKED`（Postgres）。
- 单用户并发 1 个任务（PRD FR-OCR-08），在**创建任务时**用唯一约束 + 计数校验双重保证。

### 2.2 何时换 PostgreSQL

出现任一即迁移：① 日均任务 >1,000；② 同时在线写 >5；③ 需要多 worker。
迁移只改连接串 + 自增主键类型，业务代码不动。

---

## 3. 数据表（可直接执行）

> 约定：金额字段一律 `*_mill`，整数，单位**厘**；时间一律 ISO-8601 文本（UTC）；主键用 UUID4 字符串。

```sql
CREATE TABLE users (
  id                TEXT PRIMARY KEY,
  username          TEXT NOT NULL UNIQUE,
  pwd_hash          TEXT NOT NULL,
  email             TEXT NULL UNIQUE,
  balance_mill      INTEGER NOT NULL DEFAULT 0 CHECK (balance_mill >= 0),
  free_pages_total  INTEGER NOT NULL DEFAULT 5,
  free_pages_used   INTEGER NOT NULL DEFAULT 0
                    CHECK (free_pages_used >= 0 AND free_pages_used <= free_pages_total),
  status            TEXT NOT NULL DEFAULT 'active',   -- active / disabled
  risk_flag         INTEGER NOT NULL DEFAULT 0,
  device_hash       TEXT NULL,
  created_at        TEXT NOT NULL,
  last_recharge_at  TEXT NULL
);
CREATE INDEX idx_users_created ON users(created_at);

CREATE TABLE tasks (
  id                    TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL REFERENCES users(id),
  filename              TEXT NOT NULL,
  file_hash             TEXT NOT NULL,          -- sha256(文件内容)，过期重跑幂等的依据
  file_type             TEXT NOT NULL,          -- image / pdf
  pages_total           INTEGER NOT NULL,
  page_range            TEXT NULL,              -- 'all' 或 '1-50'
  mode                  TEXT NOT NULL,          -- ancient / formula / modern
  optimize              INTEGER NOT NULL DEFAULT 1,
  status                TEXT NOT NULL,          -- §4.1
  chars                 INTEGER NOT NULL DEFAULT 0,
  estimated_chars       INTEGER NOT NULL DEFAULT 0,
  amount_mill           INTEGER NOT NULL DEFAULT 0,
  pending_mill          INTEGER NOT NULL DEFAULT 0,   -- 待补金额
  free_pages_applied    INTEGER NOT NULL DEFAULT 0,
  split_pages           INTEGER NOT NULL DEFAULT 0,
  split_reverted        INTEGER NOT NULL DEFAULT 0,
  crop_applied          INTEGER NOT NULL DEFAULT 0,
  is_rerun              INTEGER NOT NULL DEFAULT 0,
  rerun_free            INTEGER NOT NULL DEFAULT 0,
  order_check_pass_rate REAL NULL,
  error_code            TEXT NULL,
  created_at            TEXT NOT NULL,
  expire_at             TEXT NOT NULL            -- created_at + 72h
);
CREATE INDEX idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX idx_tasks_expire ON tasks(expire_at);
-- 60 秒内同用户同文件只建一个（PRD EX-14 / 资金幂等）
CREATE UNIQUE INDEX uq_tasks_dedup ON tasks(user_id, file_hash, created_at);

CREATE TABLE task_pages (
  task_id            TEXT NOT NULL REFERENCES tasks(id),
  page_no            INTEGER NOT NULL,        -- 任务内页序号（拆分后连续）
  source_page_no     INTEGER NOT NULL,        -- 原文件页号（双页两张共享同一值）
  is_split_half      INTEGER NOT NULL DEFAULT 0,  -- 0 整页 / 1 右半 / 2 左半
  status             TEXT NOT NULL,           -- pending / processing / done / empty / failed / skipped
  chars              INTEGER NOT NULL DEFAULT 0,
  retry_count        INTEGER NOT NULL DEFAULT 0,
  order_check_passed INTEGER NULL,            -- NULL=未校验，1 通过，0 重排后仍可疑
  img_width          INTEGER NOT NULL,        -- 坐标缩放唯一基准（必填）
  img_height         INTEGER NOT NULL,
  error_msg          TEXT NULL,
  PRIMARY KEY (task_id, page_no)
);

CREATE TABLE page_blocks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      TEXT NOT NULL,
  page_no      INTEGER NOT NULL,
  block_id     TEXT NOT NULL,
  block_label  TEXT NOT NULL,                 -- text / title / table / formula / figure / header
  block_order  INTEGER NOT NULL,              -- 阅读顺序，从 0 起
  text         TEXT NOT NULL DEFAULT '',
  bbox_x1      INTEGER NOT NULL, bbox_y1 INTEGER NOT NULL,
  bbox_x2      INTEGER NOT NULL, bbox_y2 INTEGER NOT NULL,
  polygon_json TEXT NULL,                     -- [[x,y],...]
  group_id     TEXT NULL,                      -- 夹注/表格分组
  FOREIGN KEY (task_id, page_no) REFERENCES task_pages(task_id, page_no)
);
CREATE INDEX idx_blocks_task_page ON page_blocks(task_id, page_no, block_order);

CREATE TABLE preprocess_records (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        TEXT NOT NULL,
  source_page_no INTEGER NOT NULL,
  action         TEXT NOT NULL,               -- split / crop
  confidence     REAL NULL,
  bbox           TEXT NULL,                   -- JSON
  reverted       INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);

CREATE TABLE ledger (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        TEXT NOT NULL REFERENCES users(id),
  type           TEXT NOT NULL,               -- recharge / consume / refund / adjust / expire
  amount_mill    INTEGER NOT NULL,            -- 正=入账，负=出账
  balance_before INTEGER NOT NULL,
  balance_after  INTEGER NOT NULL,
  ref_type       TEXT NULL,                   -- task / recharge_order / admin
  ref_id         TEXT NULL,
  remark         TEXT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX idx_ledger_user_time ON ledger(user_id, created_at);
CREATE UNIQUE INDEX uq_ledger_consume ON ledger(type, ref_type, ref_id) WHERE type IN ('consume','refund');

CREATE TABLE recharge_orders (
  id              TEXT PRIMARY KEY,
  order_no        TEXT NOT NULL UNIQUE,        -- RC202610260001
  user_id         TEXT NOT NULL REFERENCES users(id),
  amount_mill     INTEGER NOT NULL CHECK (amount_mill > 0),
  source          TEXT NOT NULL DEFAULT 'manual',   -- manual / wechat / alipay（D30 预留）
  pay_method      TEXT NOT NULL,
  payer_note      TEXT NULL,                   -- 付款人昵称
  paid_at         TEXT NULL,                   -- 转账时间（用户填）
  proof_url       TEXT NULL,
  status          TEXT NOT NULL,               -- §4.2
  reject_reason   TEXT NULL,
  admin_id        TEXT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,        -- manual: admin|proof_hash|amount
  confirmed_at    TEXT NULL,
  created_at      TEXT NOT NULL
);
CREATE INDEX idx_rcg_status ON recharge_orders(status, created_at);

CREATE TABLE files (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  kind       TEXT NOT NULL,  -- vert_pdf / vert_pdf_trad / horiz_docx / horiz_pdf / proof_pdf / markdown / zip
  lang       TEXT NOT NULL DEFAULT 'simp',    -- simp / trad（仅竖排与校对版有意义）
  path       TEXT NOT NULL,
  size       INTEGER NOT NULL DEFAULT 0,
  expire_at  TEXT NOT NULL,
  cleaned    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
-- 导出幂等：同任务同类型同语种只生成一次
CREATE UNIQUE INDEX uq_files_kind ON files(task_id, kind, lang);

CREATE TABLE export_logs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        TEXT NOT NULL,
  user_id        TEXT NOT NULL,
  kind           TEXT NOT NULL,
  success        INTEGER NOT NULL,
  duration_ms    INTEGER NULL,
  size_bytes     INTEGER NULL,
  error_code     TEXT NULL,
  missing_glyphs TEXT NULL,                   -- JSON: {"font":"SourceHanSC","chars":["甯","峯"]}
  cache_hit      INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);

CREATE TABLE font_assets (
  name          TEXT PRIMARY KEY,             -- KingHwaOldSong / SourceHanSerifSC / SourceHanSerifTC
  filename      TEXT NOT NULL,
  role          TEXT NOT NULL,                -- body / fallback / title
  license_grade TEXT NOT NULL,                -- A / B
  sha256        TEXT NOT NULL,
  license_url   TEXT NULL,
  archive_url   TEXT NULL,
  captured_at   TEXT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE rerun_records (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         TEXT NOT NULL,
  file_hash       TEXT NOT NULL,
  free_rerun_used INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_rerun ON rerun_records(user_id, file_hash);

CREATE TABLE admin_logs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id     TEXT NOT NULL,
  action       TEXT NOT NULL,
  target_type  TEXT NOT NULL,
  target_id    TEXT NULL,
  before_value TEXT NULL,
  after_value  TEXT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE quality_samples (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  page_no    INTEGER NOT NULL,
  checker    TEXT NOT NULL,
  order_ok   INTEGER NULL,
  split_ok   INTEGER NULL,
  bbox_ok    INTEGER NULL,
  remark     TEXT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 初始值
-- bbox_global_enabled = '1'
-- bbox_gray_ratio     = '1.0'
-- price_per_10k_mill  = '1000'   -- ¥1/万字 = 1000 厘/万字
-- min_bill_chars      = '1000'
-- free_pages_default  = '5'
-- max_pages_normal    = '200'
-- max_pages_low_balance = '10'
-- low_balance_mill    = '1000'
-- circuit_break_ratio = '1.5'
```

**不可违反的建表纪律**

1. `ledger` **不得**加 `channel`/`source` 字段（FR-BIL-14）。
2. `task_pages.img_width/height` **必填**，写入 `page_blocks` 必须同事务。
3. `files(task_id, kind, lang)` 唯一 → 导出天然幂等。
4. `ledger(type='consume', ref_id=task_id)` 唯一 → 结算天然幂等。

---

## 4. 枚举（`app/enums.py`，前后端共用字符串）

### 4.1 任务状态

```python
TASK_STATUS = ["created","preprocessing","queued","processing",
               "settling","settled","partial","failed","cancelled","locked","expired"]
PAGE_STATUS = ["pending","processing","done","empty","failed","skipped"]
MODE        = ["ancient","formula","modern"]
```

**合法迁移表（其余一律抛 `IllegalTransition`）**

```python
ALLOWED = {
 "created":       {"preprocessing"},
 "preprocessing": {"queued","created"},
 "queued":        {"processing"},
 "processing":    {"settling","partial","failed","cancelled"},
 "settling":      {"settled","locked"},
 "locked":        {"settled"},
 "settled":       {"expired"},
 "partial":       {"expired"},
 "failed":        {"expired"},          # 退款完成后进入终态
 "cancelled":     {"expired"},
 "expired":       set(),
}
```
> 禁止回跳。唯一"重启"是新建任务（新 `task_id`）。

### 4.2 充值单状态

```python
RCG_STATUS = ["submitted","confirmed","rejected","closed"]
ALLOWED_RCG = {"submitted":{"confirmed","rejected","closed"},
               "confirmed":set(), "rejected":set(), "closed":set()}
```

### 4.3 导出类型

```python
EXPORT_KIND = ["vert_pdf","vert_pdf_trad","horiz_docx","horiz_pdf","proof_pdf","markdown","zip"]
```

---

## 5. Provider 接口与归一化（最容易写歪的一段）

### 5.1 统一返回结构

```python
from dataclasses import dataclass, field

@dataclass
class OcrBlock:
    block_id: str
    label: str            # text/title/table/formula/figure/header
    order: int            # 阅读顺序
    text: str
    bbox: tuple[int,int,int,int]
    polygon: list[tuple[int,int]] | None = None
    group_id: str | None = None

@dataclass
class OcrPageResult:
    page_no: int
    width: int            # 原图像素宽（坐标基准，必填）
    height: int
    blocks: list[OcrBlock]
    plain_text: str       # 归一化纯文本，长度即计费字数依据
    raw_text: str         # 供应商原始文本（保留换行）
    provider: str         # xinghe / glm
    model: str
    latency_ms: int
    cost_mill: int = 0
    order_ok: bool | None = None
```

### 5.2 接口

```python
class OcrProvider(Protocol):
    name: str
    def recognize(self, image_path: str, mode: str) -> OcrPageResult: ...
```

**禁止**：业务层出现 `if provider == "xinghe"`。要看供应商，读 `OcrPageResult.provider` 字段或日志，不分支。

### 5.3 两家字段映射

| 统一字段 | 星河（PaddleOCR-VL） | GLM-OCR |
|---|---|---|
| `width/height` | `dataInfo.pages[0].width/height`（**缺失则报错，不许猜**） | 由 Pillow 读原图 |
| `blocks[]` | `prunedResult.parsing_res_list[]`：`block_label / block_content / block_bbox[x1,y1,x2,y2] / block_polygon_points / block_order / group_id` | 解析 Markdown 段落，坐标缺失 → `bbox=(0,0,0,0)` 并触发 EX-27 |
| `plain_text` | 各块 `block_content` 按 `block_order` 拼接 + 清洗 | Markdown 去标记后清洗 |

### 5.4 归一化四步（顺序不能变）

```python
def normalize(result: OcrPageResult) -> OcrPageResult:
    # 1) 去前言：剥离「以下是识别结果」「图片中文字如下」等
    # 2) 结构清洗：合并段内换行、去多余空行、全半角统一
    # 3) 繁简：仅 ancient 模式且 optimize=1 时转简；trad 导出用 raw
    # 4) 字数：len(re.sub(r'\s', '', plain_text))  ← 只在这里统计一次
    ...
```

**铁律**：字数**只在归一化后的 `plain_text` 上统计一次**，下游任何模块（计费、熔断、导出）都读这个值，不许各自再算（FR-BIL-12）。

### 5.5 星河调用要点

- 鉴权：`Authorization: token {AISTUDIO_TOKEN}`
- 请求：POST JSON，`file` 传 Base64，`fileType`：0=PDF，1=图像
- 返回：`ocrResults[]`，每页一项，含 `prunedResult`（文本 + 坐标 + 置信度）
- **V0 必验**：30 页 PDF → 数 `ocrResults` 长度。返回 10 条即说明被静默截断，**必须自己按页拆 PDF 逐页调用**（这会让 D3 上传模块 +1 天，先验再写代码）
- 错误码：429（额度尽）→ 切 GLM 并降级提示；5xx → 退避重试 2 次后切 GLM
- 重试只在这三种情况：**空输出 / 乱码率 >30% / 5xx**。每次重试都是双倍成本

---

## 6. 行序自检（FR-OCR-06 ④，卖点 1 的兜底）

```python
def check_order(blocks: list[OcrBlock], mode: str) -> tuple[bool, float]:
    """返回 (是否通过, 连续性得分 0~1)。仅 ancient 模式启用。"""
    # 信号一：行首重复率（同一行首字符在相邻块重复出现 → 疑似跨栏串行）
    # 信号二：句读断裂率（块尾无标点却接下一块的中段）
    # 信号三：block_order 的栏连续性（同一 group_id / 同列 x 中心的块必须连续）
    # 综合：得分 = 1 - 加权异常率；< 0.98 判不通过 → 按 x 中心降序（右起）+ y 升序重排
```

- 重排后**必须写回 `page_blocks`** 并记 `order_check_passed=0`，同时埋点 `order_check_fail`。
- 得分写入 `tasks.order_check_pass_rate`，作为 M-10 的机器初筛。
- **禁止**在没有 `block_order` 的情况下自己按 y 坐标瞎排（竖排双栏准确率难超 90%，达不到红线）。

---

## 7. 计费算法（资金核心，写错就是资损）

```python
def bill_mill(chars: int, price_per_10k_mill: int = 1000, min_chars: int = 1000) -> int:
    """金额(厘) = ceil(max(chars, min_chars) * price / 10000)，向上取整到厘。"""
    c = max(chars, min_chars)
    return -(-c * price_per_10k_mill // 10000)   # 整数向上取整，禁止 float
```

**结算流程（必须在一个事务内）**

```python
def settle(task, user):
    free_left = user.free_pages_total - user.free_pages_used
    paid_pages = [p for p in task.pages if p.status == "done"]      # 空白/失败已排除
    free_applied = min(free_left, len(paid_pages))
    paid_chars = sum(p.chars for p in paid_pages[free_applied:])
    amount = bill_mill(paid_chars) if paid_chars else 0

    if amount == 0:
        return transition(task, "settled", amount_mill=0, free_pages_applied=free_applied)

    if user.balance_mill >= amount:
        debit(user, amount, task.id)                 # 写 ledger，type=consume
        return transition(task, "settled", amount_mill=amount,
                          free_pages_applied=free_applied)
    # 余额不足：扣至 0，记待补，锁下载
    debit(user, user.balance_mill, task.id)
    transition(task, "locked", amount_mill=user.balance_mill,
               pending_mill=amount - user.balance_mill,
               free_pages_applied=free_applied)
```

**边界（每条都要有单测）**

| 场景 | 期望 |
|---|---|
| 300 字 | 扣 100 厘（¥0.10） |
| 12,340 字 | 扣 1,234 厘（¥1.234） |
| 全部页在额度内 | 扣 0，`free_pages_applied` = 页数，可下载全部格式 |
| 余额 500 厘、应扣 1234 | 余额 0，`pending_mill` = 734，状态 locked |
| 重复调用 settle | 第二次返回上次结果，不重复记账（`uq_ledger_consume` 兜底） |
| 任务失败 | `refund` 一条，金额取该任务 consume 之和的相反数 |

---

## 8. 支付渠道抽象（D30，为迁移预留）

```python
class PaymentChannel(Protocol):
    name: str                                    # manual / wechat / alipay
    def create_order(self, user_id: str, amount_mill: int) -> dict: ...
    def handle_callback(self, payload: dict) -> dict: ...   # 幂等，按 idempotency_key
    def query_order(self, order_no: str) -> dict: ...
```

人工通道 `handle_callback` 即为「管理员确认」：
`idempotency_key = f"{admin_id}|{sha256(proof_bytes)}|{amount_mill}"`，
写入时靠唯一索引保证只入账一次。

---

## 9. 导出管线（三条路径，不许混）

```
OcrPageResult
 ├─→ html_render.build_html(task, lang, bbox)          # 唯一 HTML 源
 │      ├─ 预览：浏览器直接渲染该模板
 │      ├─ 竖排 PDF：Chromium page.pdf()（同一 HTML + 同一 CSS）
 │      └─ 校对版 PDF：同一模板换左图右文布局
 ├─→ docx.build(task)                                  # 横排 Word，直接映射段落
 └─→ markdown.build(task)                              # 纯派生文本
```

**CSS 关键点（竖排，M1b）**

```css
.vert { writing-mode: vertical-rl; text-orientation: upright;
        font-family: "KingHwaOldSong"; font-size: 12pt;
        line-height: 1.9; }
.vert .num { text-combine-upright: all; }   /* 阿拉伯数字整体旋转，禁止逐个正立 */
```

**导出五条硬约束**

1. 预览与 PDF **必须同一份 HTML + CSS**（FR-EXP-06）。
2. 字体只用白名单 3 文件；渲染前 `pyftsubset` 子集化；缺字按 京华 → 思源 SC → 思源 TC 逐字回退并记 `missing_glyphs`。
3. `files(task_id, kind, lang)` 唯一 → 重复点击返回已有文件（`cache_hit=1`）。
4. 导出**不得**改任务状态、不得扣费（FR-EXP-08）。
5. 文件 ≤1MB/1.5MB/250KB-per-page（M-13），超限视为实现缺陷。

---

## 10. API 契约

统一响应：成功 `{ "ok": true, "data": ... }`；失败 `{ "ok": false, "code": "E_XXX", "message": "人话", "detail": {...} }`。

| 方法 | 路径 | 说明 | 关键错误码 |
|---|---|---|---|
| POST | `/api/auth/register` | 用户名+密码（≥8 位） | E_USERNAME_TAKEN / E_WEAK_PWD |
| POST | `/api/auth/login` | 登录，写 7 天会话 | E_LOCKED / E_BAD_CRED |
| POST | `/api/auth/logout` | 登出 | — |
| POST | `/api/auth/password/reset-request` | 有邮箱发链接（30 分钟） | E_NO_EMAIL |
| POST | `/api/upload/init` | 分片初始化，返回 upload_id | E_TOO_LARGE / E_UNSUPPORTED |
| POST | `/api/upload/chunk` | 分片上传（断点续传） | — |
| POST | `/api/upload/complete` | 合并 + 校验文件头 + 算 `file_hash` | E_ENCRYPTED_PDF / E_CORRUPT |
| POST | `/api/tasks` | 创建任务（file_id, page_range, mode, optimize） | E_PAGE_LIMIT / E_CONCURRENT / E_DUP / E_NO_BALANCE |
| GET | `/api/tasks/{id}` | 任务详情（含页级状态） | E_FORBIDDEN |
| GET | `/api/tasks/{id}/stream` | SSE 流式文本与进度 | — |
| POST | `/api/tasks/{id}/cancel` | 取消（已识别页仍结算） | E_BAD_STATE |
| POST | `/api/tasks/{id}/pages/{no}/retry` | 重试失败页 | E_BAD_STATE |
| POST | `/api/tasks/{id}/preprocess/revert` | 撤销拆分/裁边（M1b） | E_BAD_STATE |
| GET | `/api/tasks/{id}/pages/{no}/image` | 签名 URL（≤10 分钟） | E_EXPIRED |
| GET | `/api/tasks/{id}/pages/{no}/blocks` | 块结构（框层数据） | — |
| POST | `/api/tasks/{id}/exports` | body: `{kind, lang}` → 返回 file 或 job | E_LOCKED / E_GATE_CLOSED |
| GET | `/api/exports/{file_id}/download` | 签名下载，校验归属 | E_EXPIRED / E_FORBIDDEN |
| GET | `/api/billing/estimate?task_id=` | 预估上限与额度抵扣说明 | — |
| GET | `/api/billing/ledger` | 分页流水 | — |
| POST | `/api/recharge/orders` | 提交申请单 | E_PENDING_EXISTS |
| GET | `/api/recharge/orders` | 我的申请单 | — |
| POST | `/admin/api/recharge/{id}/confirm` | 确认到账（幂等） | E_ALREADY_DONE |
| POST | `/admin/api/recharge/{id}/reject` | 驳回（必填原因） | E_NO_REASON |
| POST | `/admin/api/users/{id}/balance` | 手工调整（必填原因+凭证号） | E_NO_REASON |
| GET | `/admin/api/ledger/daily?date=` | 对账视图 | — |
| POST | `/admin/api/quality/sample` | 提交抽检标记 | — |
| POST | `/admin/api/settings/bbox` | 开关与灰度 | — |
| GET | `/api/meta/health` | 健康检查（含 DB、字体、Playwright） | — |

**鉴权**：Cookie session（HttpOnly + SameSite=Lax）。管理端 `/admin/api/*` 强制校验 `role=admin`。

---

## 11. 环境变量（`.env.example` 直接照抄）

```ini
APP_ENV=dev
APP_SECRET=                       # session 签名
BASE_URL=http://localhost:8000
DATABASE_URL=sqlite:///./data/app.db
STORAGE_DIR=./data/files
AISTUDIO_TOKEN=                   # 星河 Access Token
AISTUDIO_ENDPOINT=                # 从「API 调用示例」获取
OCR_PRIMARY=xinghe
OCR_FALLBACK=glm
GLM_API_KEY=
GLM_MODEL=glm-ocr
MAX_UPLOAD_MB=500
MAX_PAGES=200
PRICE_PER_10K_MILL=1000
MIN_BILL_CHARS=1000
FREE_PAGES_DEFAULT=5
CIRCUIT_BREAK_RATIO=1.5
FILE_TTL_HOURS=72
SESSION_DAYS=7
FONT_DIR=./fonts
PLAYWRIGHT_CHROMIUM_PATH=
ADMIN_INIT_USER=admin
ADMIN_INIT_PWD=                   # 首次启动后立即改
```

---

## 12. 任务卡（AI 逐卡实现，每卡独立可验收）

> 顺序即依赖顺序。**每卡做完跑一次 `pytest -q` + 卡内验收项**，全绿才进下一卡。

| 卡 | 目标 | 产出 | 验收 | 禁止 |
|---|---|---|---|---|
| **T01** | 骨架与配置 | FastAPI 装配、`config.py`、`enums.py`、`db.py`、健康检查 | `/api/meta/health` 返回 DB/字体/Playwright 三项状态 | 不引 React/Vite；不加 Redis |
| **T02** | 建表与迁移 | §3 全部 DDL + Alembic 首版 + `settings` 初始值 | `alembic upgrade head` 通过；表数 = 14 | 不擅自加字段；`ledger` 不许有 channel |
| **T03** | 账号 | 注册/登录/登出/找回；argon2；防刷指纹 | 单测覆盖：弱密码拒绝、重复用户名、5 次锁定、超限不发额度 | 不采集手机号；不做验证码 |
| **T04** | 上传 | 分片、断点续传、文件头校验、`file_hash`、加密 PDF 拦截 | 200MB 断网续传成功；伪装扩展名被拒 | 不把文件读进内存整块处理 |
| **T05** | 供应商接口 | `OcrProvider` / `OcrPageResult` / `normalize.py` / 星河实现 | **V0 脚本跑通 30 页 PDF 并打印 `ocrResults` 长度** | 业务层禁止 `if provider==` |
| **T06** | 队列与状态机 | DB 队列、worker、`ALLOWED` 迁移校验、页级重试 | 非法迁移抛异常；20 页任务可跑完；重试 2 次后标 failed | 禁止状态回跳 |
| **T07** | 计费 | `bill_mill`、`settle`、`debit`、5 页额度、熔断 | 边界表 6 条全绿；重复 settle 不重复记账 | 禁止 float；禁止在别处再数字数 |
| **T08** | 横排 Word | python-docx，12pt/缩进 2 字/保层级/AI 标识/免责脚注 | 800 字文件 ≤1MB，Word 可编辑无空行 | 不做竖排 Word |
| **T09** | 充值 L-min | 申请单、幂等确认、加余额、流水；`PaymentChannel` 接口 | 连点 2 次只入账 1 次；台账前后值正确 | `ledger` 不许写渠道；不许做自定义金额 |
| **T10** | 后台与对账 | 用户/任务/台账/对账视图/加额度/操作日志 | 对账三项自洽；每笔可追溯操作人 | 后台不许自助注册 |
| **T11** | 清理与合规 | 72h 清理（存储 + DB）、AI 标识、协议勾选、关键词拦截、日志 | 满 72h 物理文件不存在；日志可查 3 个月前 | 不许留永久直链 |
| **T12** | 内测与埋点 | §10.3 事件、看板、错误告警 | 关键 8 个事件落库；失败率突增能告警 | 不埋 PII 原文 |
| **T13** | 竖排 PDF（M1b） | HTML 模板、Chromium 打印、字体子集、标点实测、繁简切换 | 标点 11 项逐一截图确认；800 字 ≤1.5MB；繁简切换不扣费 | 不许两套渲染 |
| **T14** | 框层（M1b） | 坐标缩放、双向联动、类型着色、异常兜底、灰度开关 | 与文字偏差 ≤2px；缩放后仍重合；坐标异常静默隐藏 | 不许用 CSS 像素套原图坐标 |
| **T15** | 双页拆分（M1b） | 中缝检测、拆分、裁边、撤销、漏拆提示 | 100 页样本 M-11 ≥95%；撤销重跑不计费 | 不许裁掉页眉页脚 |
| **T16** | 校对版与 ZIP（M1b） | 按需生成、缓存键四要素、低峰预生成、ZIP | 换繁简不命中旧缓存；缓存与清理同步销毁 | 不许预生成占存储 |

---

## 13. P0 验收用例（20 条，可直接写成 pytest）

| 编号 | 场景 | 断言 |
|---|---|---|
| TC-01 | 12,340 字结算 | `amount_mill == 1234`，ledger 一条 consume |
| TC-02 | 300 字结算 | `amount_mill == 100`，界面文案含「按最低 1000 字计费」 |
| TC-03 | 重复 settle 同一 task | 第二次返回同结果，ledger 仍 1 条 |
| TC-04 | 余额 500 应扣 1234 | 余额 0、`pending_mill` 734、状态 locked、下载被拒 |
| TC-05 | 新用户 8 页（前 5 页额度） | `free_pages_applied == 5`，扣费仅按第 6~8 页字数 |
| TC-06 | 空白页 | 不计费、不扣额度、`status='empty'` |
| TC-07 | 单页失败重试 2 次 | 第 3 次后 `failed`，该页 chars 不计 |
| TC-08 | 全部页失败 | 状态 failed，ledger 一条 refund，金额等于 consume 之和的相反数 |
| TC-09 | 同一文件 60 秒内二次提交 | 第二次 `E_DUP`，不建任务 |
| TC-10 | 过期后首次重跑 | `rerun_free=1`，不扣费、不消耗额度；第二次提交提示计费 |
| TC-11 | 充值单连点 2 次确认 | 第二次 `E_ALREADY_DONE`，`balance_mill` 只加一次 |
| TC-12 | 同任务下 5 种格式 + 切繁简 | ledger 仍 1 条，余额不变，`files` 分别落库 |
| TC-13 | 导出重复点击同 kind+lang | 返回同一 file_id，`cache_hit=1` |
| TC-14 | 缺 8 个字形 | 8 字用思源 SC 渲染，`export_logs.missing_glyphs` 有记录，提示条出现 |
| TC-15 | 坐标全 0 的页 | 不渲染框层，上报 `bbox_render_error`，无错位框 |
| TC-16 | 原图 1240×1754 渲染 600×849 | 框与文字偏差 ≤2px；窗口缩放到 900px 后仍 ≤2px |
| TC-17 | 双栏竖排页 | 先右栏完整再左栏完整，`order_check_pass_rate ≥0.98` |
| TC-18 | 加密 PDF | 拒绝、不建任务、提示解除密码 |
| TC-19 | 非法状态迁移（settled→processing） | 抛 `IllegalTransition`，状态不变 |
| TC-20 | 同设备注册第 3 个账号 | 额度 0、`risk_flag=1`、提示可联系管理员 |

---

## 14. AI 编码守则（违反即返工）

1. **不许改 FR 编号的语义**。要改语义先改 PRD 版本号。
2. **钱只用整数厘**。出现 `float` 参与金额计算即错误。
3. **字数只统计一次**（归一化后 `plain_text`）。任何模块自行 `len(text)` 计费都是缺陷。
4. **`ledger` 不许有渠道字段**；渠道只在 `recharge_orders.source`。
5. **`task_pages.img_width/height` 必填**，`page_blocks` 必须同事务写入。
6. **预览与导出同源**，禁止第二套渲染。
7. **字体只用白名单 3 文件**。出现 `SimSun`/`Microsoft YaHei`/`Songti` 即错误。
8. **业务层禁止 provider 分支**。要切换读配置，不写 `if`。
9. **状态只能按 `ALLOWED` 迁移**，禁止回跳。
10. **本文档未定义的行为一律先问，不许猜**。特别是：错误码命名、边界阈值、文案措辞、页面布局。

---

## 15. 交付节奏建议

| 批次 | 任务卡 | 可对外说法 |
|---|---|---|
| 第 1 批（D1-D5） | T01-T06 | 「能上传、能识别、能看文本」（内部） |
| 第 2 批（D6-D11） | T07-T10 | 「能扣费、能充值、能对账」（内部） |
| 第 3 批（D12-D14） | T11-T12 | 可内测（**仍不可对外收费，等备案**） |
| 第 4 批（M1b） | T13-T16 | 可对外宣传「竖排不乱行 · 双页可直传 · 逐字可核对」 |

> **M1a 期间对外只能说「识别 + 横排 Word」**，不得出现「竖排」「仿古」「影印」。用户冲着卖点来、来了没有，是差评第一来源。
