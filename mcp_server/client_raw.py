"""
需求：实现基于Streamable的MCP原始客户端，连接服务器并调用远程工具
思路步骤：
1. 配置服务器URL
2. 使用 fastmcp.Client 直接传入 Streamable HTTP URL 建立连接
3. 列出服务器提供的工具列表
4. 调用远程服务器的工具并处理响应
"""

import asyncio
from fastmcp import Client

# 1. 配置服务器地址（Streamable HTTP 端点）
server_url = "http://127.0.0.1:8003/mcp"


async def main():
    # 2. 使用 fastmcp.Client 建立Streamable HTTP连接
    # Client 会自动识别 http:// 结尾为 /mcp 的 URL 并选择 Streamable HTTP 传输协议
    async with Client(server_url) as client:
        # 3. 列出服务器提供的工具列表
        # 向 MCP 服务端发送一个 tools/list 请求（JSON-RPC），服务端返回它注册的所有工具的元数据列表——每个工具的 name、description、inputSchema（参数定义）等
        tools = await client.list_tools()
        print(f"tools-->{tools}")

        # 4. 调用远程工具
        # 向 MCP 服务端发送 tools/call 请求，服务端执行对应的函数并把结果返回
        print("--> 调用工具: query_weather")
        response = await client.call_tool("query_weather", arguments={"city":'南京', "start_date":'2025-09-14'})
        print(f"response-->{response}")


if __name__ == "__main__":
    asyncio.run(main())
