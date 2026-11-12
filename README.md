# 通晓古今 · 古籍 OCR 转换站

> 上传古籍扫描件 → 自动识别为可读文本 → 在线预览 → 按字数付费下载多种排版。

「通晓古今」是一个面向古籍影印文献的文字识别与排版转换网站。用户上传古籍扫描图或 PDF，
系统调用大模型 OCR 引擎（默认星河 PaddleOCR-VL）逐页识别，经归一化、行序自检后，
可导出为横排 Word、竖排 PDF、横排 PDF、校对版 PDF、Markdown 及打包 ZIP。

**核心特点**

- 🀄 **专为古籍优化**：竖排行序自检、繁简保留原字、双页合拍自动拆分、黑边裁切
- 💰 **按字数计费**：¥1 / 万字（最低按 1000 字），新账号赠 5 页免费额度，文件保留 72 小时
- 🔐 **极简注册**：用户名 + 密码，无手机验证；人工充值四档 ¥10 / ¥30 / ¥100 / ¥300
- 🎨 **可搜索可复制的 PDF**：内置字体加工链路，导出 PDF 文本层零康熙部首、零 Type3 碎片
- 🚀 **无构建步骤**：FastAPI + Jinja2 + Alpine.js，不引 React/Vite

---

## 功能特性

| 模块 | 说明 |
|---|---|
| 识别 | 星河 PaddleOCR-VL 异步 Job（主通道），GLM 备用，`OCR_MOCK=1` 离线跑 mock |
| 归一化 | 去前言 → 结构清洗 → 繁简转换 → 字数只统计一次（`plain_text`） |
| 行序自检 | 竖排三信号（行首重复 / 句读断裂 / 栏连续性），真实古籍通过率 8.5% → 100% |
| 双页拆分 | 一张图摊开拍两页自动按中缝拆分（右半在前，古籍右起），支持纸弯实心缝 |
| 计费账本 | 金额只用整数「厘」，结算幂等，余额不足锁下载、充值解锁、全失败退款 |
| 导出 | 横排 Word / 竖排 PDF（简繁）/ 横排 PDF / 校对版 PDF / Markdown / ZIP |
| 后台 | 看板、充值确认、账实监控、日对账、OCR Key 在线配置、收款码上传 |
| 清理 | 72 小时过期清理、卡死任务回收（按最后活动时间 + 页数放宽） |

**导出格式的字体承诺**：白名单仅京华老宋体 / 思源宋体简繁三件；系统宋体、微软雅黑、楷体、
苹方一律禁用（分发即侵权）。官方思源宋体需经 `tools/build_fonts.py` 加工成 TTF 才能正确嵌入
PDF，否则会退化成「每个字一个 Type3」，复制搜索失效。

---

## 技术栈

- **后端**：Python 3.11+ · FastAPI · Uvicorn · SQLAlchemy Core（原生 SQL，无 ORM 声明式）
- **数据库**：SQLite（WAL 模式），已预留 PostgreSQL 切换（改 `DATABASE_URL` 即可）
- **前端**：Jinja2 + Alpine.js（无构建步骤）
- **认证**：argon2 口令 + 签名 Cookie 会话 + 设备指纹
- **OCR**：星河 PaddleOCR-VL（异步 Job）/ GLM / 离线 mock
- **文档**：python-docx（Word）、PyMuPDF（PDF 渲染）、Pillow + OpenCV（图像处理）、fonttools（字体子集化）
- **PDF 引擎**：Playwright + Chromium 打印（预览与 PDF 同源：同一份 HTML + CSS）

---

## 快速开始（离线可跑，不需要任何 API Key）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 0) 装 PDF 引擎（竖排/横排/校对版 PDF 都靠它；不装则只有 Word/Markdown）
.venv/bin/pip install playwright && .venv/bin/playwright install chromium

# 1) 一键初始化 + 起服务（Web 与 Worker 一起，Ctrl-C 一起退）
ADMIN_INIT_PWD='换成你的强密码' OCR_MOCK=1 ./run.sh
```

打开 http://127.0.0.1:8000 ，页脚与 `/api/meta/health` 会显示各组件状态。

> **只起 Web 是不够的。** Web 只接单与展示，识别是 Worker 干的；只跑 uvicorn 时任务会
> 永远停在「排队中」。`./run.sh` 默认把两个进程一起拉起来，这是本地最常见的一个坑。

其它子命令：

```bash
./run.sh status      # 体检：库 / 字体 / OCR / PDF 引擎 / 队列 / 账实 / 磁盘
./run.sh web         # 只起 Web
./run.sh worker      # 只起 Worker
./run.sh init        # 只做初始化（幂等）
```

命令行工具（`python -m app.cli`）：

```bash
init             建库 + 管理员 + 字体登记（幂等）
status           一条命令看清现在能不能用（比翻日志快）
mock             离线全链路：造图 → 识别 → 结算 → 导出 Word/Markdown
integrity        账实核对（必须输出「账实相符」）
reconcile        日对账（充值/消费/退款/调整是否自洽）
cleanup          72 小时过期清理
fonts            字体准入体检
resetpwd <用户名> [新密码]   重置密码并清登录锁定（忘记管理员密码时唯一的救急方式）
v0 <file.pdf>     验证供应商是否把 30 页 PDF 静默截断成 10 页
```

**生产**把 worker 拆成独立进程（单 worker 轮询 DB 队列，不引 Redis）：

```bash
.venv/bin/python -m app.worker                                       # 终端 A
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000            # 终端 B
```

开发期想单进程跑完队列，加 `RUN_WORKER_INPROC=1` 即可在 Web 进程内起 worker 线程
（**仅限开发**：多副本部署会变成多个 worker 抢队列）。

### 容器化部署（Docker）

标准配置（`docker-compose.yml`）按 2 核 4G 以上设计：worker `mem_limit: 2g`、web `1g`。

```bash
docker compose up -d                      # web + worker
docker compose --profile tunnel up -d     # 再加 Cloudflare 隧道
```

**低端服务器（2 核 2G / 便宜的轻量机型）**：用低配覆盖文件，把 worker 压到 1.4g、web 到 512m，
并先给宿主开 swap 兜底（Chromium 打印大 PDF 峰值 300~500MB，2G 整机不 swap 会被 OOM 杀掉）：

```bash
sudo bash scripts/setup-swap.sh 2G        # 开 2G swap（开机自启，幂等）
docker compose -f docker-compose.yml -f docker-compose.lowmem.yml up -d
```

> 内存是这套站唯一不能省的指标：OCR 本身在云端跑，本机 CPU/带宽都过剩，但导出 PDF
> 时的 Chromium 峰值内存会突增。低于 2G 的整机只建议跑「低配 + swap」组合，且要接受
> 100+ 页大书导出变慢；再低（1 核 1G）不建议跑本套站。

### 环境变量

完整清单见 `.env.example`（`APP_SECRET`、`DATABASE_URL`、`STORAGE_DIR`、`OCR_MOCK`、
`OCR_PRIMARY`、`AISTUDIO_TOKEN`、`AISTUDIO_ENDPOINT`、`AISTUDIO_MODEL` 等）。
其中星河 Token 也支持在后台「开关与运维 → OCR 识别服务」在线配置（存 settings 表，
审计日志只记尾 4 位脱敏），改完立即生效、无需重启。

### 升级与备份（重要）

**全站数据 = `data/app.db` 一个 SQLite 文件 + `data/files` 里的用户文件。** 代码随时能从
GitHub 重新拉，但这个文件不会自己飞到别处 —— 删了、坏了、换机器忘了带走，数据就没了。

升级代码**不会清库**：建表用 `CREATE TABLE ... IF NOT EXISTS`，已存在的表直接跳过，
用户、余额、账本、任务记录原样保留。升级只要三步：

```bash
git pull                                # 拉新代码
bash scripts/backup-db.sh               # 升级前先备份（强烈建议）
docker compose up -d --build            # 重建镜像并重启
```

真正会清库的只有两种情况，务必避开：

| 操作 | 结果 |
|------|------|
| 删除 `data/` 目录或 `app.db` 文件 | ❌ 数据全丢 |
| 换服务器时忘了带走 `data/app.db` 和 `fonts/` | ❌ 数据不跟随代码 |

备份脚本 `scripts/backup-db.sh`（用 SQLite `backup` API 拿一致性快照，比直接 `cp` 安全；
默认保留最近 30 份，传数字可改保留数）：

```bash
bash scripts/backup-db.sh               # 备份到 data/backups/
bash scripts/backup-db.sh 10            # 只保留最近 10 份
```

> 给改代码的人：若升级时给表**加新列**，只写进 `schema.sql` 不够（`IF NOT EXISTS` 不会
> 给已存在的表补列，老库会报 `no such column`）。必须同时登记到 `app/db.py` 的
> `_ADDED_COLUMNS`，启动时才会 `ALTER TABLE` 补上。

---

## 目录结构

```
schema.sql                 表结构与索引的唯一事实源（20 张表）
app/
  config.py                唯一读环境变量的地方；含零依赖 .env 加载
  enums.py                 11 态任务状态机 + ALLOWED 迁移表 + 权限判定
  db.py                    SQLAlchemy Core + 原生 SQL；tx() 显式事务边界
  security.py              argon2 口令、签名 Cookie 会话、设备指纹
  routers/                 meta/auth/upload/task/export/billing/recharge/admin
  services/
    billing.py             计费与账本（金额只用整数「厘」）
    worker.py              队列抢占、逐页识别、熔断、结算
    ocr/                   供应商门面 + 归一化 + 行序自检 + xinghe/glm/mock
    export/                docx / markdown / html_render / pdf / fonts
    payment/manual.py      人工充值通道（管理员确认到账）
    cleanup.py compliance.py audit.py preprocess.py
  data/radical_map.json    CJK 部首补充区 → 等价汉字（PDF 文本层正确的前提）
  templates/ static/       宣纸+墨色设计系统，Alpine.js，无构建步骤
fonts/                     白名单字体（体积大、受授权约束，不入库；放本机即可）
tests/                     254 条用例（计费/状态机/行序/导出/HTTP/字体/清理）
tools/check_fonts.py       字体准入体检（SHA256 / 竖排特性 / 繁简覆盖）
tools/build_fonts.py       把官方思源宋体加工成能正确嵌入 PDF 的 TTF
tools/audit_pdf_text.py    PDF 文本层体检（康熙部首 / Type3 碎片）
tools/accept.py            端到端验收（真 HTTP + 七种导出 + magic 校验）
tools/batch_run.py         真样张批量跑分（test/ 目录 → 报告）
tools/probe_all.py         全站广度巡检（3 种身份 × 全部 GET，专抓 5xx 与越权）
tools/probe_edge.py        边界探测（畸形输入：负页号/长文件名/SQL 注入式用户名）
tools/probe_segment.py     大文件分段建任务（1296 页取 1-200 页，建完即取消）
tools/shot_pages.py        页面截图 + 控制台报错收集（样式塌/白屏靠它）
tools/reexport.py          对已完成任务重跑七种导出并校验交付件（不重新识别）
tools/diagnose_order.py    行序分数诊断：把三个信号分量单独打出来，看分为什么掉
tools/recheck_order.py     整本按**当前**判定逻辑重算行序通过率（新旧对比）
run.sh                     一键启动（Web + Worker 一起起，Ctrl-C 一起退）
```

---

## 不可违反的几条

1. **金额只有整数「厘」**（`*_mill`）。任何 float 参与金额计算都算缺陷。
2. **字数只统计一次**：`normalize()` 写进 `plain_text`，计费/预览/导出共用，别处不许再 `len()`。
3. **结算幂等**：状态判断 + `uq_ledger_consume` 部分唯一索引双保险，重复调用只扣一次。
4. **账本不认渠道**：`ledger` 只记事件（recharge/consume/refund/adjust/expire），渠道在 `recharge_orders`。
5. **状态禁回跳**：只走 `ALLOWED`；`expired` 是终态。
6. **字体白名单**：仅京华老宋体 / 思源宋体简繁三件；系统宋体、微软雅黑、楷体、苹方等一律禁用（分发即侵权）。
7. **横排 Word 不许竖排**：改了会整列重排，用户会骂。
8. **PDF 逐页渲染**：自己渲染再逐页调供应商，规避「超 10 页只处理前 10 页」的静默截断。
9. **预览与 PDF 同源**：只允许一套 HTML + 一套 CSS。「下载的和看到的不一样」是这类产品最难解释的客诉。
10. **交付文件必须真的能打开**：导出后按 magic bytes 校验（`%PDF-` / `PK\x03\x04`），不看文件大小猜。
11. **一个源页 = 一张纸**：打印 CSS 里 `.page` 必须 `break-after: page` + `break-inside: avoid`，
    否则第二页会从第一张纸底部接着排、被腰斩成「上半页 + 下半页」两张纸。
12. **改了渲染就升 `RENDER_VERSION`**（`app/services/export/__init__.py`）：导出走
    `files(task_id,kind,lang)` 幂等缓存，不升版本的话，修好的排版用户永远拿不到，下载到的还是旧坏文件。
13. **字体必须是「加工版」TTF**：官方思源宋体是 CID-keyed CFF，Chromium 打印时
    **无法子集化**，会退化成「每个字一个 Type3 字体」。加工（去 glyph 名 + 删部首码位）
    见 `tools/build_fonts.py`，验收见 `tools/audit_pdf_text.py`：
    PDF 文本层必须 **康熙部首 0 个、Type3 0 个**，否则「可复制可搜索」这条承诺就是假的。
14. **标题不许用粗体**：白名单只放 Regular，`font-weight:bold` 会让浏览器做伪粗体，
    而伪粗体在 PDF 里必然退化成 Type3 图片字（复制不到、放大发糊）。层级靠字号与字距。
15. **入库保留原字**：OCR 输出可能带康熙部首（⻩/⼆），入库前必须 `fix_radicals()`
    还原成汉字；繁简只在输出层按用户点的 `lang` 转，与 `mode` 无关。
16. **一张图摊开拍两页就必须拆**：古籍影印大量是「左右页合拍一张」，不拆时整幅图
    塞进模型输入，每半页有效分辨率只剩一半，两页的列序还会互相穿插。拆在
    `init_pages()`（建页时定 2 条记录，`is_split_half` 1=右半 / 2=左半，
    右半在前因为古籍右起），`render_pages()` 落盘。检测见 `detect_spread()`，
    **中缝只认「正中附近的一对紧邻竖线」**——旧版按「中间区最暗列」判，
    方向是反的（单页竖排的文字列对比度 0.65，比真双页 0.29 还高）。
    纸弯阴影把两条框线糊成**一条**实心暗带时走判据 C：列暗占比 ≥0.5 的窄带
    + 两侧 12~40px 是白纸边。**别只看「中间有没有暗线」**——单页正中也有界行。
17. **页码范围存「压缩表达式」**：`tasks.page_range` 只有 200 字符可用，
    113 页展开成 `1,2,3,…` 要 380 字符，截断后 worker 只读到 70 页 ——
    后半本静默消失，还按 70 页计费。写入用 `compact_page_range()`（`1-113`），
    读取用 `parse_page_range()`。
18. **worker 重启必须能续跑**：`claim_next()` 只认 `queued`，进程被杀时
    被抢占的任务会永远停在 `processing`（用户看到「识别中」到 72h 过期）。
    `recover_stale_tasks()` 在拿到单实例锁后把它放回队列，`run_task()` 跳过
    已 `done` 的页 —— 两条缺一不可，否则要么卡死、要么整本重跑重复花钱。
19. **导出链路里不许有「删文件」**：`html_to_pdf()` 曾 `unlink()` 临时 HTML，
    在限制删除次数的环境里直接抛 `SystemExit`，整次导出 500。`*.render.html`
    现在保留（同名覆盖、随任务清理、还能拿浏览器打开排查版式）。
20. **清理到期任务前先排除「还在跑」的**：`cleanup_expired()` 的查询必须带
    `status NOT IN ('queued','processing','preprocessing','settling')`。
    只排 `expired` 时，一个到期那刻正在识别的任务会被删掉目录（worker 随后
    一路 OCR 失败），还会被直接改成 `expired` 绕过状态机。
21. **收尸看「最后活动时间」，不看创建时间**：`expire_stale_running()` 用
    `MAX(task_pages.finished_at/started_at)` 判，预算按页数放宽
    （`max(6h, 页数×4min)`）。实测 1.5 分钟/页，200 页要 5 小时，
    按固定 6 小时判会把「一直在推进」的大任务当僵尸杀掉。
    这个函数必须在 worker 空闲循环里**真的被调用** —— 定义了不调等于没兜底。
22. **导出并发撞唯一索引不是错误**：`files(task_id,kind,lang)` 有 `uq_files_kind`，
    双击下载 / 浏览器重试 / ZIP 顺手生成，两个请求都会走到 INSERT。
    `_save_file()` 捕获 `IntegrityError` 后**复用先写入的那条**并返回 `cache_hit`，
    不许抛 500 —— 那只是「别人已经生成好了」。
23. **只读接口给 admin 豁免，写操作不给**：后台任务列表里每个任务号都是
    `/task/{id}` 链接，管理员点进去必须能看到别人任务的详情、原图、块坐标
    （客服排查的唯一入口）。但 `cancel` / `retry` 显式 `admin=False` ——
    误取消用户任务、替用户重试导致重复扣费，都不可撤销。
24. **行序自检只认「正文块」，竖排不看句读**：古籍影印页里的书名、撰者、卷次、
    页码是 2~8 字的短块，贴在边缘窄列。让它们参与栏连续性判据，x 中心会来回跳，
    **正确顺序被误判成串栏**（实测 test6.pdf 第 10 页 0.5437）。
    同理竖排的「块」就是一整列，列间是同一段的续排、本就不加标点，
    拿横排的「块尾有没有标点」套竖排，会让整页无标点的古籍天天判不通过。
    现在的规则：`≥6 字且非 number/image/header 类`才参与栏连续性；
    竖排 `break` 权重归零；标点密度 <0.5% 时横排也归零；回目与其副标题
    （前缀关系、≥4 字）不算行首重复；连续两段都以「卻說/話說/且說…」起头
    也不算（章回体叙述习惯，实测第 6 页因此 dup=0.2）。
    **改之前 113 页样张的通过率是 8.5%（等于每页都提示「行序存疑」）。**

---

## 测试

```bash
.venv/bin/python -m pytest          # 254 passed
```

覆盖：计费取整与最低字数、免费额度、余额不足锁下载与充值解锁、失败退款、账实核对、
11 态迁移与非法回跳、竖排三信号（行首重复/句读断裂/栏连续性）与重排、Word 版式与 AI 标识、
导出幂等与越权下载、校对版 PDF 不得懒加载图片、校对版图片必须 `file://` 绝对路径、
打印样式「一源页一纸」、渲染器换版后缓存必须重出、竖排 LaTeX 降级、
部首还原与全角标点不被折半、PDF 文本层无康熙部首/无 Type3、字体文件是否已加工、
上传文件头校验、页数上限、并发限制、60 秒去重、充值确认幂等与驳回、
双页合拍检测与拆分（单页不误拆、近方图不被宽高比挡掉、实心缝不漏判、
密排文字单页不误拆、拆后页序与尺寸、预估字数按拆后页数重算）、
页码范围压缩与 113 页大 PDF 不被截断、worker 崩溃恢复与断点续跑、
清理不删运行中任务、收尸按最后活动时间且大任务不误杀、收尸函数必须被 worker 真正调用、
上传文件名与充值 paid_at/proof_url 长度上限、收款码默认地址指向真实路由、
导出并发撞唯一索引时复用而非 500、管理员可读用户任务但不能取消/重试、
双页拆分决策留痕（preprocess_records）、双页拆分后建任务预估按拆后页数报、
行序自检的版心豁免（真串栏仍要被抓）、无标点古籍不扣分、回目与副标题不算行首重复、
章回体发语词不算行首重复、
后台/账户/任务页未登录必须跳登录。

**三层巡检**（对着正在跑的服务，各有分工，别混用）：

```bash
ADMIN_PWD='管理员密码' .venv/bin/python tools/probe_all.py    # 广度：37 端点 × 3 身份
ADMIN_PWD='管理员密码' .venv/bin/python tools/probe_edge.py   # 深度：畸形输入找 5xx
ADMIN_PWD='管理员密码' .venv/bin/python tools/shot_pages.py   # 视觉：8 页截图 + JS 报错
```

`probe_all` 判断的是「有没有 5xx / 普通用户能不能摸到后台数据」；
`probe_edge` 打的是负数页号、500 字符文件名、`admin' OR 1=1 --` 这类输入；
`shot_pages` 抓的是 pytest 和接口测试都看不见的东西 —— 页面白屏、样式塌、Alpine 报错。

**端到端验收**（对着正在跑的服务走真实 HTTP，不看单测）：

```bash
BASE=http://127.0.0.1:8000 ADMIN_PWD='管理员密码' .venv/bin/python tools/accept.py
```

依次验：5 个页面 200 → 健康检查 → 注册/上传/建任务/识别完成 → 三种排版预览 →
七种导出下载并**按 magic bytes 校验**（`%PDF-` / `PK\x03\x04`）→ 12 个后台接口。
退出码 0 才算过。字体未装、OCR 走 mock 这类环境依赖只标 `!` 不算失败。

**真实样张批量跑分**（手上有一批扫描件时用这个）：

```bash
# 把图片/PDF 丢进 test/，然后：
ADMIN_PWD='管理员密码' .venv/bin/python tools/batch_run.py --dir test --topup 5
BASE=http://127.0.0.1:8000 ADMIN_PWD=... .venv/bin/python tools/batch_run.py --dir test --only test.pdf
```

每份都走真实用户路径（分片上传 → 建任务 → worker 识别 → 七种导出 → 下载），
结果写到 `docs/reports/<日期>-batch.md`（字数/金额/耗时/体积/部首/Type3 一表看完），
下载件落 `.tmp/batch/<任务号>/` 可以人工翻看。`--topup` 走后台「调整余额」接口注入
测试余额并留审计痕迹，正式账号不要用。

---

## 当前状态

**已交付并逐项实测通过**（不只是「代码写了」）：

- 注册登录（极简用户名+密码，设备风控扣发免费额度）、协议确认、找回密码降级通道
- 分片上传与断点续传、文件头伪装校验、页数上限、60 秒去重、并发上限
- DB 队列表 + 11 态状态机 + 单 worker；重试、降级、熔断、卡死任务回收
- 归一化四步（去前言 → 结构清洗 → 繁简 → 字数只统计一次）与竖排行序三信号自检
- 计费与账本（整数厘、结算幂等、余额不足锁下载、充值自动解锁、全失败退款）
- 人工充值：四档固定金额、付款人昵称必填、管理员确认到账、驳回需理由
- 导出**七种格式全部真实出文件**（magic 校验通过）：
  竖排 PDF（简/繁）、横排 PDF、校对版 PDF、横排 Word、Markdown、ZIP（5 成员）
- 预览与 PDF **同源**：同一份 HTML + 同一份 CSS；预览支持横排/竖排/校对三种排版
- 后台看板、充值单处理、账实监控、日对账、导出健康度、收款码上传
- 72 小时过期清理：物理删除文件、任务转 `expired`、下载返回 410 `E_EXPIRED`

**已有但依赖外部条件才能生效**：

| 能力 | 缺什么 | 缺了会怎样 |
|---|---|---|
| 真实识别 | `AISTUDIO_TOKEN` / `GLM_API_KEY` | 只能 `OCR_MOCK=1` 跑 mock |
| 京华老宋体（繁体竖排首选观感） | `fonts/KingHwa_OldSong.ttf` | 繁体竖排用思源宋体 TC 顶着，能交付，只是不是「老宋」的味道 |
| 邮箱自助找回 | 邮件服务 | 有邮箱时展示 30 分钟令牌，无邮箱走管理员人工核验 |

> PDF 引擎已就绪（Playwright + Chromium 实测出文件）；**思源宋体简繁两件已到位并加工**，
> PDF 文本层实测「康熙部首 0 个、Type3 0 个」。

**字体这件事的完整做法**（换机器/新部署照做）：

```bash
# 1) 下官方思源宋体 2.003R（SIL OFL 1.1，可免费商用）
#    https://github.com/adobe-fonts/source-han-serif/releases/tag/2.003R
#    取 09_SourceHanSerifSC.zip / 10_SourceHanSerifTC.zip 里的 *-Regular.otf

# 2) 转 TTF 并加工（去 glyph 名 + 删康熙部首码位）
.venv/bin/python tools/build_fonts.py --otf .../SourceHanSerifSC-Regular.otf --name SourceHanSerifSC
.venv/bin/python tools/build_fonts.py --otf .../SourceHanSerifTC-Regular.otf --name SourceHanSerifTC

# 3) 准入体检 + 登记 SHA256（授权台账靠它认文件）
.venv/bin/python tools/check_fonts.py fonts/*.ttf
.venv/bin/python -m app.cli fonts --register

# 4) 让旧缓存按新字体重出（RENDER_VERSION 已在代码里随渲染改动升级）
.venv/bin/python tools/audit_pdf_text.py --dir data/files/tasks/<任务>/exports
```

**还没验证到位的**（代码在，缺真实素材）：

- 双页合拍拆分与黑边裁切（`detect_spread` / `split_spread`）—— 需要真实的跨页扫描图
- 框层灰度比、质量抽检流程 —— 需要真实供应商返回的紧框坐标

M1b 现状：**竖排 PDF 已按真实古籍页复核**（右起竖排、页码落版心底部、每页带 AI 标识、
一个源页正好一张纸；竖排里的 LaTeX 会降级成 λ、π、x₁ 这类普通字符 —— 竖排混排公式没有
可用的 CSS 折中，横排/校对版则原样保留 LaTeX 便于复制）；**框层校对版已用真实 bbox 复核**
（左原图带框、右文字带序号、坐标按原图百分比缩放）；ZIP 已通。双页自动拆分仍未验证。

**字体到位后的实测数字**（同一份 1 页现代文本样张，改动前后对比）：

| 导出 | 之前（系统 serif） | 现在（思源宋体 TTF） |
|---|---|---|
| 竖排 PDF | 148.6KB、Type3×44、部首 14 | **30.9KB、Type3×0、部首 0** |
| 横排 PDF | 147.9KB、Type3×44、部首 14 | **30.6KB、Type3×0、部首 0** |
| 校对版 PDF | 264.6KB（超 M-13 红线） | **198.0KB（红线内）** |

---

## 上线前必查

- `APP_SECRET` 换成足够长的随机串（当前默认值仅用于开发）。
- `APP_ENV=prod`（会关闭 `/api/docs`）。
- 反向代理上传体积放行到 `MAX_UPLOAD_MB`，并把 `/api/upload/chunk` 的读超时调大。
- 首次登录后台后立即改管理员密码；密码丢了用 `python -m app.cli resetpwd admin`。
- **确认 worker 真的在跑**：`./run.sh status` 里「待处理任务」不堆积；只起 Web 会让任务永远排队。
- `python -m app.cli integrity` 与后台「账实监控」都应为 0 差异。
- 定期跑 `python -m app.cli cleanup`（或用定时任务调 `/admin/api/cleanup/run`）。
- 上传一张真实古籍页，跑一次 `tools/accept.py`，把七种导出各下一次并**打开**确认
  （PDF 能翻页、Word 能编辑、ZIP 成员齐全）。

---

## License

本项目代码暂未指定开源许可证（保留所有权利）。其中依赖的思源宋体按 **SIL OFL 1.1**
授权（见 `fonts/OFL-SourceHanSerif.txt`）；京华老宋体受其商业授权条款约束，禁止修改字体文件。
如需商用或再分发，请自行确认字体与 OCR 服务的授权范围。
