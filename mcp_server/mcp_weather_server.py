"""
需求：天气MCP
1个天气查询工具
"""

# ==================== 导入依赖 ====================
import os                                      # 环境变量读取
import sys                                     # 系统路径操作，用于把项目上级目录加入 sys.path
from fastmcp import FastMCP                  # FastMCP 框架，用于快速构建 MCP 服务器

# 把项目代码的上两级目录（04-代码）加入模块搜索路径，保证 SmartVoyage 包可被正确导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from SmartVoyage.config import Config         # 项目配置类
from SmartVoyage.create_logger import logger  # 日志记录器

# 导入 services 层各模块的实现逻辑
from SmartVoyage.services.weather_service import (
    fetch_weather_from_api,  # 和风天气 API 实时查询
)

# 实例化配置对象
conf = Config()


# ==================== 创建统一外部 MCP 服务器 ====================

def create_weather_mcp_server():

    # 第一步：创建 FastMCP 实例
    mcp = FastMCP(
        name="WEATHERAPITools",                     # 服务器名称
        instructions="天气查询：query_weather，直连和风天气 API 获取实时天气数据"
    )

    # 第二步：注册工具
    # ========== 天气工具 ==========
    @mcp.tool(
        name="query_weather",
        description=(
            "查询天气数据。参数：city(城市名称), start_date(开始日期，格式YYYY-MM-DD), "
            "end_date(结束日期，格式YYYY-MM-DD，可选，不传则只查start_date当天)。"
            "示例：查询北京2025-07-30的天气 --> query_weather(city='北京', start_date='2025-07-30') "
        )
    )
    def query_weather(city: str, start_date: str, end_date: str = None) -> str:
        """
        需求：MCP 工具入口 —— 天气查询，直连和风天气 API。
        思路步骤：
        1. 调用 fetch_weather_from_api 获取实时天气数据
        """
        # 记录执行日志
        logger.info(f"执行天气查询: city={city}, start_date={start_date}, end_date={end_date}")

        return fetch_weather_from_api(city, start_date, end_date)



    # 第七步：打印服务器状态信息
    logger.info("=== 统一外部 MCP 服务器信息 ===")
    logger.info(f"名称: {mcp.name}")                # 服务器名称
    logger.info(f"描述: {mcp.instructions}")         # 服务器描述

    try:
        # 第八步：打印启动提示信息 —— 动态获取所有已注册工具名称
        # 从 external_mcp 实例中提取工具列表，确保新增工具后无需手动维护打印内容
        import asyncio
        all_tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
        print("统一外部 MCP 服务器已启动，请访问 http://127.0.0.1:8003/mcp")
        print(f"共注册 {len(all_tool_names)} 个工具：")

        for i in range(0, len(all_tool_names), 5):
            batch = all_tool_names[i:i + 5]
            print("  " + " | ".join(batch))
        # 第九步：启动 MCP 服务器，使用 streamable-http 传输模式
        # 监听地址和端口从 Config 读取，Docker 环境中通过环境变量覆盖
        mcp.run(transport="streamable-http", host=conf.mcp_host, port=conf.mcp_weather_port)
    except Exception as e:
        # 启动失败时打印错误信息
        print(f"服务器启动失败: {e}")


# ==================== 程序入口 ====================
if __name__ == '__main__':
    # 直接运行本文件时，创建并启动外部 MCP 服务器
    create_weather_mcp_server()
