# AI-KM 知识管理平台 · 生产镜像
# 基于官方 Python 3.13 精简镜像，依赖单一，零额外运维负担。
FROM python:3.13-slim

# 环境变量（容器内运行）。PYTHONDONTWRITEBYTECODE 避免写 .pyc，减小镜像层。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIKM_HOST=0.0.0.0 \
    AIKM_PORT=5200

# 工作目录
WORKDIR /app

# 先装依赖（利用 Docker 层缓存，依赖不变则不重装）
# PIP_INDEX 可在构建时覆盖，例如国内网络用清华镜像：
#   docker build --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple -t aikm .
ARG PIP_INDEX=https://pypi.org/simple
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -i "${PIP_INDEX}"

# 拷贝应用代码（.dockerignore 已排除 .env / data / __pycache__ 等敏感与可变内容）
COPY . /app

# 创建专用非 root 用户 aikm（uid 10001）备用：如需进一步加固可在 entrypoint 里用 gosu/su 降权运行。
# 注意：命名卷默认以 root 属主初始化，若直接以 aikm 运行会因无写权限而启动失败；
# 因此当前容器以 root 运行（可信局域网内，宿主机即安全边界），保证数据卷可写"开箱即用"。
# 后续若要求非 root，可在 Dockerfile 增加 entrypoint 脚本：先 `chown -R aikm:aikm /app/data` 再降权启动。
RUN useradd --create-home --uid 10001 aikm \
    && chown -R aikm:aikm /app

# 数据目录：运行时由 docker-compose 的命名卷挂载持久化（容器重建不丢数据）
VOLUME ["/app/data"]

# 暴露端口
EXPOSE 5200

# 健康检查：用 Python 内置 urllib 探活 /login（精简镜像无 curl）。
# 仅在 gunicorn 真正起来后 /login 返回 200 才判健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5200/login').status==200 else 1)" || exit 1

# 生产启动命令：gunicorn 取代 Flask 开发服务器。
# -k gthread：多线程worker，适配 SQLite（避免多写进程互相锁表）
# -w 1 --threads 8：单进程多线程，既并发又能安全写入 SQLite；如需更高并发可评估迁移 Postgres
# --timeout 120：大模型问答可能耗时较长，放宽超时避免被误杀
CMD ["gunicorn", "-k", "gthread", "-w", "1", "--threads", "8", "--timeout", "120", "-b", "0.0.0.0:5200", "wsgi:app"]
