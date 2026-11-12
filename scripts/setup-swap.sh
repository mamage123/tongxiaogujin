#!/usr/bin/env bash
# 通晓古今 · 低内存机型 swap 脚本
#
# 用途：给 2 核 2G 这类便宜服务器开 swap，给 Chromium 打印 PDF 兜底。
#       Docker 的 mem_limit 是硬顶，容器内不会用宿主 swap，所以光靠 compose
#       压内存不够——宿主本身也要有 swap，否则 worker 峰值时会被 OOM killer 干掉。
#
# 用法：
#   sudo bash scripts/setup-swap.sh 2G        # 开 2G swap（推荐）
#   sudo bash scripts/setup-swap.sh 4G        # 开 4G swap
#   sudo bash scripts/setup-swap.sh status    # 只看当前 swap 情况，不动
#   sudo bash scripts/setup-swap.sh off       # 关闭并移除（可选）
#
# 注意：
#   - 只影响当前主机，重启后是否保留取决于下面的 fstab 步骤（脚本会做）；
#   - 用 swapfile（不是 swap 分区），云服务器系统盘都有空间，最省事；
#   - 脚本幂等：重复执行同名 size 会先清掉旧文件再建。

set -euo pipefail

SIZE="${1:-2G}"
SWAPFILE="/swapfile"

# macOS 没有 swapon，直接拦截（本脚本只给 Linux 服务器用）
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "本脚本只用于 Linux 服务器；macOS/NAS 请用各自方式（群晖在 DSM 面板开 swap）。" >&2
  exit 1
fi

if [[ "${SIZE}" == "status" ]]; then
  echo "== 当前 swap =="
  free -h || true
  swapon --show 2>/dev/null || echo "（无活动 swap）"
  exit 0
fi

if [[ "${SIZE}" == "off" ]]; then
  echo "== 关闭并移除 swapfile =="
  swapoff "${SWAPFILE}" 2>/dev/null || echo "（当前未启用，跳过 swapoff）"
  rm -f "${SWAPFILE}"
  # 从 fstab 里去掉这一行，避免重启后报「找不到 /swapfile」
  if grep -q "^${SWAPFILE} " /etc/fstab 2>/dev/null; then
    sed -i "\#^${SWAPFILE} #d" /etc/fstab
  fi
  echo "完成。"
  exit 0
fi

if [[ ! "${SIZE}" =~ ^[0-9]+G$ ]]; then
  echo "size 参数格式不对，应为 2G / 4G 这类。例如：sudo bash $0 2G" >&2
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "需要 root 权限，请用 sudo 运行。" >&2
  exit 1
fi

echo "== 准备创建 ${SIZE} swapfile 到 ${SWAPFILE} =="

# 幂等：先关掉旧的再重建
swapoff "${SWAPFILE}" 2>/dev/null || true
rm -f "${SWAPFILE}"

# fallocate 秒建；老内核/某些文件系统不支持时回退 dd
if ! fallocate -l "${SIZE}" "${SWAPFILE}"; then
  echo "fallocate 不可用，改用 dd（较慢）..."
  dd if=/dev/zero of="${SWAPFILE}" bs=1M count="$(( ${SIZE%G} * 1024 ))" status=progress
fi

chmod 600 "${SWAPFILE}"
mkswap "${SWAPFILE}" >/dev/null
swapon "${SWAPFILE}"

# 写进 fstab 以便重启后自动挂载（幂等：先删旧行再加）
if grep -q "^${SWAPFILE} " /etc/fstab 2>/dev/null; then
  sed -i "\#^${SWAPFILE} #d" /etc/fstab
fi
echo "${SWAPFILE} none swap sw 0 0" >> /etc/fstab

echo ""
echo "== 完成，当前 swap 情况 =="
free -h
swapon --show
echo ""
echo "如需验证可跑：free -h 看 Swap 一行不再是 0。"
