-- 通晓古今 · 数据库结构（唯一事实源）
-- 依据《开发规格 §3》DDL，按 PRD 补齐 FR 所需的功能表。
-- 约定：
--   * 金额字段一律 *_mill，整数，单位「厘」；禁止浮点。
--   * 时间一律 ISO-8601 文本（UTC）。
--   * 主键用 UUID4 字符串。
-- 修改此文件等价于修改生产表结构：必须同步 alembic 版本，禁止直接改线上库。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================ 账号
CREATE TABLE IF NOT EXISTS users (
  id                TEXT PRIMARY KEY,
  username          TEXT NOT NULL UNIQUE,
  pwd_hash          TEXT NOT NULL,
  email             TEXT NULL UNIQUE,
  role              TEXT NOT NULL DEFAULT 'user',      -- user / admin（FR-ADM-01）
  balance_mill      INTEGER NOT NULL DEFAULT 0 CHECK (balance_mill >= 0),
  free_pages_total  INTEGER NOT NULL DEFAULT 5,
  free_pages_used   INTEGER NOT NULL DEFAULT 0
                    CHECK (free_pages_used >= 0 AND free_pages_used <= free_pages_total),
  status            TEXT NOT NULL DEFAULT 'active',    -- active / disabled
  risk_flag         INTEGER NOT NULL DEFAULT 0,
  device_hash       TEXT NULL,
  reg_ip            TEXT NULL,
  created_at        TEXT NOT NULL,
  last_recharge_at  TEXT NULL,
  -- 余额有效期（FR-RCG-08）：自最后一次充值起 12 个月，NULL 表示从未充值
  balance_expire_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at);
CREATE INDEX IF NOT EXISTS idx_users_device ON users(device_hash);

-- 登录失败计数与锁定（FR-ACC-03：用户名 + IP 双维度，5 次锁定 15 分钟）
CREATE TABLE IF NOT EXISTS login_attempts (
  scope        TEXT NOT NULL,          -- 'user:alice' / 'ip:1.2.3.4'
  fail_count   INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (scope)
);

-- 防刷计数（FR-ACC-06：设备 24h ≤2、IP 24h ≤5）
CREATE TABLE IF NOT EXISTS register_marks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  device_hash TEXT NULL,
  ip          TEXT NULL,
  user_id     TEXT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regmark_device ON register_marks(device_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_regmark_ip ON register_marks(ip, created_at);

-- 密码重置令牌（FR-ACC-04：有邮箱可自助，30 分钟有效）
CREATE TABLE IF NOT EXISTS password_resets (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id),
  expire_at  TEXT NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- ============================================================ 任务
CREATE TABLE IF NOT EXISTS tasks (
  id                    TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL REFERENCES users(id),
  filename              TEXT NOT NULL,
  file_hash             TEXT NOT NULL,          -- sha256(文件内容)，过期重跑幂等的依据
  file_type             TEXT NOT NULL,          -- image / pdf
  pages_total           INTEGER NOT NULL,
  page_range            TEXT NULL,              -- 'all' 或 '1-50'
  mode                  TEXT NOT NULL,          -- ancient / formula / modern
  optimize              INTEGER NOT NULL DEFAULT 1,
  status                TEXT NOT NULL,          -- enums.TASK_STATUS
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
  ocr_provider          TEXT NULL,              -- 实际使用的识别通道（xinghe/glm/mock）
                                                -- 必须落库：否则线上出现识别质量问题时
                                                -- 无法判断这一页到底走了哪条通道
  source_path           TEXT NULL,              -- 上传合并后的原文件（清理时删除）
  error_code            TEXT NULL,
  created_at            TEXT NOT NULL,
  expire_at             TEXT NOT NULL,          -- created_at + 72h
  -- 60 秒幂等桶（EX-14）：同用户同文件同分钟只能建一个任务
  dedup_bucket          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_expire ON tasks(expire_at);
CREATE INDEX IF NOT EXISTS idx_tasks_hash ON tasks(user_id, file_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_dedup ON tasks(user_id, file_hash, dedup_bucket);

CREATE TABLE IF NOT EXISTS task_pages (
  task_id            TEXT NOT NULL REFERENCES tasks(id),
  page_no            INTEGER NOT NULL,        -- 任务内页序号（从 1 起，拆分后连续）
  source_page_no     INTEGER NOT NULL,        -- 原文件页号（双页两张共享同一值）
  is_split_half      INTEGER NOT NULL DEFAULT 0,  -- 0 整页 / 1 右半 / 2 左半
  status             TEXT NOT NULL,           -- pending/processing/done/empty/failed/skipped
  chars              INTEGER NOT NULL DEFAULT 0,
  retry_count        INTEGER NOT NULL DEFAULT 0,
  order_check_passed INTEGER NULL,            -- NULL=未校验，1 通过，0 重排后仍可疑
  img_width          INTEGER NOT NULL,        -- 坐标缩放唯一基准（必填）
  img_height         INTEGER NOT NULL,
  image_path         TEXT NULL,               -- 该页实际送识别的图（必要时是半页）
  error_msg          TEXT NULL,
  started_at         TEXT NULL,
  finished_at        TEXT NULL,
  PRIMARY KEY (task_id, page_no)
);
CREATE INDEX IF NOT EXISTS idx_pages_status ON task_pages(task_id, status);

CREATE TABLE IF NOT EXISTS page_blocks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      TEXT NOT NULL,
  page_no      INTEGER NOT NULL,
  block_id     TEXT NOT NULL,
  block_label  TEXT NOT NULL,                 -- text/title/table/formula/figure/header
  block_order  INTEGER NOT NULL,              -- 阅读顺序，从 0 起
  text         TEXT NOT NULL DEFAULT '',
  bbox_x1      INTEGER NOT NULL, bbox_y1 INTEGER NOT NULL,
  bbox_x2      INTEGER NOT NULL, bbox_y2 INTEGER NOT NULL,
  polygon_json TEXT NULL,                     -- [[x,y],...]
  group_id     TEXT NULL,                     -- 夹注/表格分组
  FOREIGN KEY (task_id, page_no) REFERENCES task_pages(task_id, page_no)
);
CREATE INDEX IF NOT EXISTS idx_blocks_task_page ON page_blocks(task_id, page_no, block_order);

CREATE TABLE IF NOT EXISTS preprocess_records (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        TEXT NOT NULL,
  source_page_no INTEGER NOT NULL,
  action         TEXT NOT NULL,               -- split / crop
  confidence     REAL NULL,
  bbox           TEXT NULL,                   -- JSON
  reverted       INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preprocess_task ON preprocess_records(task_id, source_page_no);

-- ============================================================ 资金
CREATE TABLE IF NOT EXISTS ledger (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        TEXT NOT NULL REFERENCES users(id),
  type           TEXT NOT NULL,               -- recharge/consume/refund/adjust/expire
  amount_mill    INTEGER NOT NULL,            -- 正=入账，负=出账
  balance_before INTEGER NOT NULL,
  balance_after  INTEGER NOT NULL,
  ref_type       TEXT NULL,                   -- task / recharge_order / admin
  ref_id         TEXT NULL,
  remark         TEXT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user_time ON ledger(user_id, created_at);
-- 结算天然幂等：同一任务只能有一条 consume / refund
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_consume
  ON ledger(type, ref_type, ref_id) WHERE type IN ('consume','refund');
-- 注：ledger 禁止出现 channel/source 字段（FR-BIL-14「账本不认渠道」）

CREATE TABLE IF NOT EXISTS recharge_orders (
  id              TEXT PRIMARY KEY,
  order_no        TEXT NOT NULL UNIQUE,        -- RC202610260001
  user_id         TEXT NOT NULL REFERENCES users(id),
  amount_mill     INTEGER NOT NULL CHECK (amount_mill > 0),
  source          TEXT NOT NULL DEFAULT 'manual',   -- manual / wechat / alipay（D30 预留）
  pay_method      TEXT NOT NULL,
  payer_note      TEXT NULL,                   -- 付款人昵称
  paid_at         TEXT NULL,                   -- 转账时间（用户填）
  proof_url       TEXT NULL,
  proof_hash      TEXT NULL,
  status          TEXT NOT NULL,               -- submitted/confirmed/rejected/closed
  reject_reason   TEXT NULL,
  admin_id        TEXT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,        -- manual: admin|proof_hash|amount
  confirmed_at    TEXT NULL,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rcg_status ON recharge_orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_rcg_user ON recharge_orders(user_id, created_at);

-- ============================================================ 文件与导出
CREATE TABLE IF NOT EXISTS files (
  id         TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  kind       TEXT NOT NULL,  -- vert_pdf/vert_pdf_trad/horiz_docx/horiz_pdf/proof_pdf/markdown/zip
  lang       TEXT NOT NULL DEFAULT 'simp',    -- simp / trad
  path       TEXT NOT NULL,
  size       INTEGER NOT NULL DEFAULT 0,
  expire_at  TEXT NOT NULL,
  cleaned    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  render_version INTEGER NOT NULL DEFAULT 0  -- 渲染器版本；低于当前版本视为过期缓存，需重新生成
);
-- 导出幂等：同任务同类型同语种只生成一次
CREATE UNIQUE INDEX IF NOT EXISTS uq_files_kind ON files(task_id, kind, lang);
CREATE INDEX IF NOT EXISTS idx_files_expire ON files(expire_at, cleaned);

CREATE TABLE IF NOT EXISTS export_logs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        TEXT NOT NULL,
  user_id        TEXT NOT NULL,
  kind           TEXT NOT NULL,
  success        INTEGER NOT NULL,
  duration_ms    INTEGER NULL,
  size_bytes     INTEGER NULL,
  error_code     TEXT NULL,
  missing_glyphs TEXT NULL,                   -- JSON: {"font":"SourceHanSerifSC","chars":["甯","峯"]}
  cache_hit      INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_export_logs_kind ON export_logs(kind, created_at);

-- ============================================================ 字体
CREATE TABLE IF NOT EXISTS font_assets (
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

-- ============================================================ 幂等与审计
CREATE TABLE IF NOT EXISTS rerun_records (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         TEXT NOT NULL,
  file_hash       TEXT NOT NULL,
  free_rerun_used INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rerun ON rerun_records(user_id, file_hash);

CREATE TABLE IF NOT EXISTS admin_logs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id     TEXT NOT NULL,
  action       TEXT NOT NULL,
  target_type  TEXT NOT NULL,
  target_id    TEXT NULL,
  before_value TEXT NULL,
  after_value  TEXT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_logs_time ON admin_logs(created_at);

CREATE TABLE IF NOT EXISTS access_logs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    TEXT NULL,
  ip         TEXT NULL,
  action     TEXT NOT NULL,     -- login / upload / recognize / download / recharge / adjust
  target_id  TEXT NULL,
  detail     TEXT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_logs_time ON access_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_access_logs_user ON access_logs(user_id, created_at);

CREATE TABLE IF NOT EXISTS quality_samples (
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

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 站内通知（FR-RCG-05：确认/驳回要通知用户）
CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    TEXT NOT NULL,
  kind       TEXT NOT NULL,     -- recharge_confirmed / recharge_rejected / task_done / export_ready
  title      TEXT NOT NULL,
  body       TEXT NULL,
  link       TEXT NULL,
  read_at    TEXT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, created_at);

-- 分片上传会话（FR-UPL-05 断点续传）
CREATE TABLE IF NOT EXISTS upload_sessions (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  filename    TEXT NOT NULL,
  total_size  INTEGER NOT NULL,
  chunk_size  INTEGER NOT NULL,
  total_chunks INTEGER NOT NULL,
  received    TEXT NOT NULL DEFAULT '[]',   -- JSON: 已收到的分片序号
  dir_path    TEXT NOT NULL,
  done        INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

-- 埋点事件（T12）
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  user_id    TEXT NULL,
  task_id    TEXT NULL,
  props      TEXT NULL,     -- JSON，禁止写入原文内容（不埋 PII）
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_name ON events(name, created_at);
