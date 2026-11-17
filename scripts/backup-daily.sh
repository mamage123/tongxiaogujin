#!/usr/bin/env bash
# 通晓古今 · 每日自动备份（宿主机 crontab 调用）
#
# 作用：在宿主机上每天定时调用容器内的 backup-db.sh，把数据库一致性快照
# 备份到 data/backups/（该目录与容器共享，宿主机能直接看到备份文件）。
#
# 为什么走容器而不在宿主机直接 cp：
#   - 容器内才有 python3（Playwright 官方镜像自带），SQLite backup API 依赖它；
#   - data/ 是 bind mount，宿主机与容器看到的是同一份文件，备份结果两边一致。
#
# 挂到 crontab（每天 03:30 备份，保留最近 30 份）：
#   crontab -e
#   30 3 * * * /bin/bash /opt/tongxiaogujin/scripts/backup-daily.sh >> /opt/tongxiaogujin/data/backups/cron.log 2>&1
#
set -euo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="data/backups"
mkdir -p "$LOG_DIR"

# 容器名与 compose 项目目录按实际调整；脚本默认容器名 txgj-web（见 docker-compose.yml）。
CONTAINER="${TXGJ_WEB_CONTAINER:-txgj-web}"

if docker compose exec -T "$CONTAINER" python -c "import sys" >/dev/null 2>&1; then
  docker compose exec -T "$CONTAINER" python -m app.cli cleanup >/dev/null 2>&1 || true
  # 直接用容器内 python 调 SQLite backup API，避免依赖容器里是否装了 bash 脚本
  docker compose exec -T "$CONTAINER" python - <<'PYEOF'
import sqlite3, datetime, glob, os
src = "/app/data/app.db"
backup_dir = "/app/data/backups"
keep = int(os.environ.get("BACKUP_KEEP", "30"))
os.makedirs(backup_dir, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
dst = os.path.join(backup_dir, f"app.db-{stamp}")
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
print(f"OK {dst}")
files = sorted(glob.glob(os.path.join(backup_dir, "app.db-*")))
for f in files[:-keep]:
    os.remove(f)
    print(f"removed {f}")
PYEOF
else
  echo "✗ 找不到容器 ${CONTAINER}，请确认 docker compose 服务名"
  exit 1
fi
