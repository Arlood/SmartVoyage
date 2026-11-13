#!/bin/bash
# SmartVoyage 统一入口脚本
# 根据 command 参数决定启动哪个服务
# 用法：command: ["mcp"] / ["weather"] / ["ticket"] / ["trip"] / ["api"]

set -e

case "$1" in
  mcp)
    echo "[entrypoint] 启动统一外部 MCP 服务器 (port 8002)..."
    exec python -m SmartVoyage.mcp_server.mcp_external_server
    ;;
  weather)
    echo "[entrypoint] 启动天气 A2A 服务器 (port 5005)..."
    exec python -m SmartVoyage.a2a_server.weather_server
    ;;
  ticket)
    echo "[entrypoint] 启动票务 A2A 服务器 (port 5006)..."
    exec python -m SmartVoyage.a2a_server.ticket_server
    ;;
  trip)
    echo "[entrypoint] 启动行程 A2A 服务器 (port 5007)..."
    exec python -m SmartVoyage.a2a_server.trip_server
    ;;
  api)
    echo "[entrypoint] 启动 API 服务器 (port 8080)..."
    exec python -m uvicorn SmartVoyage.api_server:app --host 0.0.0.0 --port 8080
    ;;
  *)
    echo "[entrypoint] 未知命令: $1"
    echo "可用命令: mcp | weather | ticket | trip | api"
    exit 1
    ;;
esac
