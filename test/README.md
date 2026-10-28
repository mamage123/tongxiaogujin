# test/ —— 真实样张放这里

把要试的扫描件丢进这个目录（`.png .jpg .jpeg .tif .tiff .webp .pdf` 都认），然后：

```bash
# 全部跑一遍（会真调星河 OCR，按字数计费）
ADMIN_PWD='管理员密码' .venv/bin/python tools/batch_run.py --dir test --topup 5

# 只跑某一份
ADMIN_PWD='管理员密码' .venv/bin/python tools/batch_run.py --dir test --only test.pdf

# 换识别模式（默认 ancient 竖排古籍）
ADMIN_PWD='...' .venv/bin/python tools/batch_run.py --dir test --mode modern
```

跑完看两处：

- `docs/reports/<日期>-batch.md` —— 字数、金额、耗时、七种导出体积、PDF 文本层（部首/Type3）
- `.tmp/batch/<任务号>/` —— 下载件本体，可以直接打开翻

## 双页合拍会被自动拆开

古籍影印件常是「一本书摊开拍一张」，一张图里有两个半页。系统会检测中缝并自动拆成两页：

- 一条原图 → 两条页记录（`is_split_half` 1=右半、2=左半），**右半排在前面**（古籍右起）
- 报告里的「页」列会变成 2，PDF 也出 2 页
- 任务页会写明「这张图是双页合拍，已按中缝自动拆成左右两页」，原图卡片会标「（右半 · 原图右页）」
- 拆分不增加费用（按字数计），但每半页各占 1 页识别额度

拆不拆的判断在 `app/services/preprocess.py: detect_spread()`。想确认某张图会不会被拆：

```bash
.venv/bin/python -c "
from app.services.preprocess import detect_spread
print(detect_spread('test/test4.png'))   # (True, 0.745, 507)
"
```

## 注意

- 需要 worker 在跑：`./run.sh status`；没起就 `./run.sh all`
- 单任务最长等 `--wait`（默认 1800 秒），大 PDF 会逐页识别
- `--topup` 只给测试账号注入余额（走后台上调余额接口，留审计痕迹），正式账号别用
- 这个目录里的样张不入库（`.gitignore` 已排除），避免把客户原件提交上去
- `tests/` 才是 pytest 的用例目录，别把样张放进 `tests/`
