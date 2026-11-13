"""
需求：飞常准 Aviation MCP 客户端实现逻辑。

本模块从 mcp_external_server.py 中提取，包含：
- 飞常准 MCP 配置（URL、API Key）
- VariFlightClient 类：通过 JSON-RPC 2.0 over HTTP 调用飞常准 MCP 端点
"""

# ==================== 导入依赖 ====================
import os                       # 操作系统接口，用于读取环境变量
import json                     # JSON 序列化/反序列化
import requests                 # HTTP 客户端，用于调用飞常准 MCP 端点

from SmartVoyage.config import Config  # 项目配置类
from SmartVoyage.create_logger import logger  # 日志记录器

conf = Config()  # 全局配置实例

# 飞常准 Aviation MCP 配置 —— 统一从 Config 读取（config.yaml / 环境变量 / 兜底值）
# 飞友科技 Aviation MCP —— 提供航班动态、机票价格、舒适度分析等 9 个航旅工具
# 控制台：https://mcp.variflight.com/（注册即送 50 元体验额度）
# API Key 创建：https://mcp.variflight.com/keys
VARIFLIGHT_API_KEY = conf.variflight_api_key
VARIFLIGHT_MCP_URL = conf.variflight_mcp_url


# ==================== 飞常准 Aviation MCP 客户端 ====================

class VariFlightClient:
    """
    需求：飞常准 Aviation MCP 的 JSON-RPC 客户端，通过 HTTP POST 调用远程 MCP 端点。
    思路步骤：
        1. 初始化时保存 MCP 端点 URL 和 API Key。
        2. call_tool 方法构造 JSON-RPC 2.0 请求（method="tools/call"），发送 HTTP POST。
        3. 解析响应中的 result.content[0].text 字段，返回工具执行结果。
        4. 异常时返回错误 JSON 字符串。

    飞常准 MCP 协议说明：
    - 传输方式：MCP streamable HTTP（JSON-RPC 2.0 over HTTP POST）
    - 鉴权：请求头 X-API-Key
    - 请求体：{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "...", "arguments": {...}}}
    - 响应体：{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "..."}]}}

    端点地址：https://ai.variflight.com/servers/aviation/mcp
    控制台：https://mcp.variflight.com/
    """

    def __init__(self):
        """
        需求：初始化 VariFlightClient，读取配置中的 URL 和 API Key。
        """
        self.url = VARIFLIGHT_MCP_URL      # MCP 端点地址
        self.api_key = VARIFLIGHT_API_KEY  # API 密钥

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """
        需求：调用飞常准 MCP 的指定工具，返回工具执行结果文本。
        思路步骤：
            1. 构造 JSON-RPC 2.0 请求体（method="tools/call"）。
            2. 设置请求头：Content-Type + X-API-Key + Accept。
            3. 发送 HTTP POST 请求（超时 30 秒，航班查询数据量大）。
            4. 解析 JSON 响应，从 result.content[0].text 提取结果。
            5. 异常时返回错误 JSON 字符串。

        参数：
            tool_name (str): 工具名称，如 "searchFlightsByDepArr"
            arguments (dict): 工具参数，如 {"date": "2026-08-28", "depcity": "SHA", "arrcity": "BJS"}

        返回值：
            str: 工具执行结果文本（通常是 JSON 格式的航班数据）
        """
        # 第一步：构造 JSON-RPC 2.0 请求体
        payload = {
            "jsonrpc": "2.0",                    # JSON-RPC 协议版本
            "id": 1,                             # 请求 ID（任意整数）
            "method": "tools/call",              # MCP 方法：调用工具
            "params": {
                "name": tool_name,               # 工具名称
                "arguments": arguments,           # 工具参数
            }
        }

        # 第二步：设置请求头
        headers = {
            "Content-Type": "application/json",  # 请求体为 JSON
            "Accept": "application/json, text/event-stream",  # 接受 JSON 或 SSE 响应
            "X-API-Key": self.api_key,           # 飞常准鉴权
        }

        try:
            # 第三步：发送 HTTP POST 请求
            resp = requests.post(self.url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()              # HTTP 错误码会抛异常

            # 第四步：解析 JSON 响应
            data = resp.json()

            # 检查 JSON-RPC 错误
            if "error" in data:
                error_msg = data["error"].get("message", "未知错误")
                return json.dumps({"status": "error", "message": f"飞常准 MCP 错误：{error_msg}"}, ensure_ascii=False)

            # 第五步：从 result.content[0].text 提取结果
            result = data.get("result", {})
            content_list = result.get("content", [])
            if content_list and isinstance(content_list, list):
                return content_list[0].get("text", "{}")
            return json.dumps({"status": "no_data", "message": "飞常准未返回数据"}, ensure_ascii=False)

        except requests.exceptions.Timeout:
            logger.error(f"飞常准 MCP 超时：{tool_name}")
            return json.dumps({"status": "error", "message": "飞常准查询超时，请稍后重试"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"飞常准 MCP 调用失败：{str(e)}")
            return json.dumps({"status": "error", "message": f"飞常准调用失败：{str(e)}"}, ensure_ascii=False)


# 创建全局飞常准客户端实例（API Key 已配置时可用）
_variflight_client = VariFlightClient() if VARIFLIGHT_API_KEY else None
