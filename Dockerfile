# SmartVoyage 智慧旅游 — 统一镜像
# 所有 Python 服务（MCP / A2A / API）共用此镜像，通过 command 区分启动哪个服务
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（MySQL 客户端库）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc default-libmysqlclient-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装（requirements.txt 已放入 SmartVoyage/ 目录）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码（build context 就是 SmartVoyage/ 目录本身）
COPY . ./SmartVoyage/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["./entrypoint.sh"]
