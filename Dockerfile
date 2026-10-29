# 通晓古今 · 容器镜像
#
# 基础镜像选 Playwright 官方版，而不是 python:3.11-slim 自己装：
#   1. Chromium 及其全部系统依赖（libnss3/libfontconfig/中文字体渲染库等）已预装，
#      省掉几十个 apt 包和多轮踩坑；
#   2. 官方镜像同时提供 amd64 与 arm64 构建（多架构 manifest），
#      群晖/威联通这类 ARM NAS 也能原生跑，不会被 qemu 模拟拖死；
#   3. 浏览器固定落在 /ms-playwright，PLAYWRIGHT_BROWSERS_PATH 已设好，不要覆盖它。
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先单独装依赖：requirements.txt 不变就能命中构建缓存，改代码不用重装依赖
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 再拷代码。.dockerignore 已排除 .env / data / tmp / 字体 / 样张，
# 所以这一步不会把密钥和几百 MB 数据打进镜像层。
COPY . . .

# data 与 fonts 由 compose 挂载宿主目录，这里只保证目录存在且可写。
# 以 root 运行（见 compose 的 user 说明）：家庭 NAS 上目录属主千差万别，
# 用固定 uid 的普通用户极易撞上 "attempt to write a readonly database"。
RUN mkdir -p /app/data/files /app/data/uploads /app/data/tasks /app/fonts

EXPOSE 8000

# --proxy-headers + --forwarded-allow-ips：
# 请求经过 cloudflared（或 Nginx）转发后，socket 对端都是内网 IP。
# 不读 X-Forwarded-For 的话，app/deps.py::client_ip() 认为所有用户来自同一个 IP，
# 会直接撞上 IP_ACCOUNT_LIMIT（默认 5 个账号/ IP）→ 第 6 个用户注册就被判超限、不发免费额度。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
