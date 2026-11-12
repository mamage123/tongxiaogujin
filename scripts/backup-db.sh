#!/usr/bin/env bash
# 通晓古今 · 数据库备份
#
# 用法：
#   bash scripts/backup-db.sh           备份 data/app.db 到 data/backups/
#   bash scripts/backup-db.sh 10        保留最近 10 份，更早的自动删除
#
# 为什么必须备份：全站数据（用户、余额、账本、任务、充值记录）都在
# data/app.db 这一个 SQLite 文件里。代码可以随时从 GitHub 重新拉，
# 但这个文件不会自己飞到别处 —— 删了、坏了、换机器忘了带走，数据就没了。
# 所以：升级前 / 做任何改库操作前，先跑一次本脚本。
set -euo pipefail
cd "$(dirname "$0")/.."

DB="data/app.db"
BACKUP_DIR="data/backups"
KEEP="${1:-30}"   # 默认保留最近 30 份

if [ ! -f "$DB" ]; then
  echo "✗ 找不到 ${DB}（还没有数据库，无需备份）"
  exit 1
fi

mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/app.db-$STAMP"

# 用 SQLite 自带的 backup API：即使有连接在写（WAL 模式）也能拿到一致快照，
# 比直接 cp 更安全（cp 可能拷到写了一半的库）。
PY=".venv/bin/python"
if [ -x "$PY" ]; then
  "$PY" -c "
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
print('OK')
" "$DB" "$DEST"
else
  cp "$DB" "$DEST"
fi

SIZE="$(du -h "$DEST" | cut -f1)"
echo "✓ 已备份：${DEST}（${SIZE}）"

# 滚动清理：只保留最近 KEEP 份，更早的自动删（不会误删正在写的主库）
ls -1t "$BACKUP_DIR"/app.db-* 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r f; do
  rm -f "$f"
  echo "  已清理旧备份：$(basename "$f")"
done

echo "  当前备份份数：$(ls -1 "$BACKUP_DIR"/app.db-* 2>/dev/null | wc -l | tr -d ' ')"
