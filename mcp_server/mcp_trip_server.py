"""
需求：行程 MCP 服务器。
高德本地生活：8个工具
博查WebSearch旅游攻略：1个工具
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
from SmartVoyage.services.local_life_service import local_life_service  # 高德本地生活服务类

from SmartVoyage.services.bocha_service import (
    BOCHA_API_KEY,                 # 博查 API Key
    _call_bocha_api,               # 博查 API 调用
    _format_search_results,        # 搜索结果格式化
    _get_sample_data,              # 教学样例数据
)

# 实例化配置对象
conf = Config()


# ==================== 创建统一外部 MCP 服务器 ====================

def create_trip_mcp_server():
    """
    需求：创建并启动 MCP 服务器，注册 9 个工具并运行服务。
    思路步骤：
    1. 创建 FastMCP 实例，设置名称、描述
    2. 注册 8 个高德本地生活工具（geocode/regeocode/search_poi/search_around/direction_*）
    3. 注册旅游攻略搜索工具 search_travel_guides（含博查 API + 样例数据降级）
    4. 打印服务器状态信息和工具列表
    5. 调用 mcp.run 启动服务（streamable-http 传输模式）

    端口：8006
    """
    # 第一步：创建 FastMCP 实例
    mcp = FastMCP(
        name="ExternalAPITools",                     # 服务器名称
        instructions=(
            "外部 API 工具集。包含2类工具：\n"
            "1. 本地生活（高德地图）：地理编码、POI搜索、周边搜索、路径规划（驾车/公交/步行/骑行）\n"
            "2. 旅游攻略联网搜索：search_travel_guides，基于博查 AI Web Search API，返回真实网页搜索结果\n"
        ),
    )

    # 第二步：注册工具

    # ========== 本地生活工具（高德地图） ==========
    @mcp.tool(
        name="geocode",
        description="地理编码：将地址转为经纬度坐标。参数：address(地址文本), city(城市，可选)"
    )
    def geocode(address: str, city: str = None) -> str:
        """
        需求：MCP 工具 —— 地理编码，将地址文本转为经纬度坐标。
        思路步骤：
        1. 委托给 local_life_service.geocode 处理
        """
        # 调用本地生活服务的地理编码方法
        return local_life_service.geocode(address, city)

    @mcp.tool(
        name="regeocode",
        description="逆地理编码：将经纬度坐标转为结构化地址。参数：location(坐标，格式'经度,纬度')"
    )
    def regeocode(location: str) -> str:
        """
        需求：MCP 工具 —— 逆地理编码，将经纬度坐标转为结构化地址。
        思路步骤：
        1. 委托给 local_life_service.regeocode 处理
        """
        # 调用本地生活服务的逆地理编码方法
        return local_life_service.regeocode(location)

    @mcp.tool(
        name="search_poi",
        description="POI 关键字搜索：搜索餐厅、酒店、景点、租车门店等。参数：keywords(关键字), city(城市，可选), limit(数量，默认20)"
    )
    def search_poi(keywords: str, city: str = None, limit: int = 20) -> str:
        """
        需求：MCP 工具 —— POI 关键字搜索。
        思路步骤：
        1. 委托给 local_life_service.search_poi 处理
        """
        # 调用 POI 搜索方法
        return local_life_service.search_poi(keywords, city, limit)

    @mcp.tool(
        name="search_around",
        description="POI 周边搜索：搜索附近餐厅、酒店、景点等。参数：location(中心坐标'经度,纬度'), keywords(关键字), radius(半径/米，默认1000)"
    )
    def search_around(location: str, keywords: str, radius: int = 1000) -> str:
        """
        需求：MCP 工具 —— POI 周边搜索。
        思路步骤：
        1. 委托给 local_life_service.search_around 处理
        """
        # 调用周边搜索方法
        return local_life_service.search_around(location, keywords, radius)

    @mcp.tool(
        name="direction_driving",
        description="驾车路径规划。参数：origin(起点'经度,纬度'), destination(终点'经度,纬度')"
    )
    def direction_driving(origin: str, destination: str) -> str:
        """
        需求：MCP 工具 —— 驾车路径规划。
        思路步骤：
        1. 委托给 local_life_service.direction_driving 处理
        """
        # 调用驾车路径规划方法
        return local_life_service.direction_driving(origin, destination)

    @mcp.tool(
        name="direction_transit",
        description="公交路径规划。参数：origin(起点'经度,纬度'), destination(终点'经度,纬度'), city(城市，默认北京)"
    )
    def direction_transit(origin: str, destination: str, city: str = "北京") -> str:
        """
        需求：MCP 工具 —— 公交路径规划。
        思路步骤：
        1. 委托给 local_life_service.direction_transit 处理
        """
        # 调用公交路径规划方法
        return local_life_service.direction_transit(origin, destination, city)

    @mcp.tool(
        name="direction_walking",
        description="步行路径规划。参数：origin(起点'经度,纬度'), destination(终点'经度,纬度')"
    )
    def direction_walking(origin: str, destination: str) -> str:
        """
        需求：MCP 工具 —— 步行路径规划。
        思路步骤：
        1. 委托给 local_life_service.direction_walking 处理
        """
        # 调用步行路径规划方法
        return local_life_service.direction_walking(origin, destination)

    @mcp.tool(
        name="direction_bicycling",
        description="骑行路径规划。参数：origin(起点'经度,纬度'), destination(终点'经度,纬度')"
    )
    def direction_bicycling(origin: str, destination: str) -> str:
        """
        需求：MCP 工具 —— 骑行路径规划。
        思路步骤：
        1. 委托给 local_life_service.direction_bicycling 处理
        """
        # 调用骑行路径规划方法
        return local_life_service.direction_bicycling(origin, destination)


    # ========== 旅游攻略联网搜索工具（博查 AI API） ==========
    @mcp.tool(
        name="search_travel_guides",
        description=(
            "联网搜索旅游攻略，返回真实的网页搜索结果（标题、来源、摘要、链接）。"
            "用于行程规划时参考互联网上的真实旅行经验分享。"
            "参数：query(搜索关键词，如'丽江攻略'、'成都美食'，会自动追加'旅游攻略')，"
            "count(返回结果数量，默认5)"
        ),
    )
    def search_travel_guides(query: str, count: int = 5) -> str:
        """
        需求：MCP 工具 —— 旅游攻略联网搜索（基于博查 AI Web Search API），含降级到样例数据的兜底策略。
        思路步骤：
        1. 自动追加"旅游攻略"关键词，聚焦旅游内容
        2. 检查 BOCHA_API_KEY 是否配置，未配置则直接返回样例数据
        3. 调用 _call_bocha_api 获取真实搜索结果
        4. 如果返回空结果，降级到样例数据
        5. 如果调用异常，也降级到样例数据
        6. 成功则格式化结果并返回
        """
        # 记录日志
        logger.info(f"旅游攻略联网搜索: query={query}, count={count}")

        # 第一步：自动追加"旅游攻略"关键词
        full_query = f"{query} 旅游攻略"

        # 第二步：检查 API Key 是否配置
        if not BOCHA_API_KEY:
            logger.warning("BOCHA_API_KEY 未配置，返回教学样例数据")
            # 获取样例数据
            sample_results = _get_sample_data(query, count)
            return _format_search_results(full_query, sample_results, "教学样例数据 —— 请配置 BOCHA_API_KEY 环境变量以获取真实数据")

        try:
            # 第三步：调用博查 API 获取真实搜索结果
            results = _call_bocha_api(full_query, count)

            # 第四步：检查是否返回空结果
            if not results:
                logger.warning("博查 API 返回空结果，降级到样例数据")
                sample_results = _get_sample_data(query, count)
                return _format_search_results(full_query, sample_results, "教学样例数据（联网搜索未返回结果）")

            # 第五步：成功，格式化并返回
            result_text = _format_search_results(full_query, results, "博查联网搜索")
            logger.info(f"博查搜索成功，返回 {len(results)} 条结果")
            return result_text

        except Exception as e:
            # 第六步：异常处理，降级到样例数据
            error_msg = f"博查搜索失败（{type(e).__name__}: {str(e)}）"
            logger.error(error_msg)                           # 记录错误日志
            logger.info("降级到教学样例数据")                 # 记录降级日志
            sample_results = _get_sample_data(query, count)   # 获取样例数据
            return _format_search_results(full_query, sample_results, f"教学样例数据（联网搜索失败：{type(e).__name__}）")



    # 第七步：打印服务器状态信息
    logger.info("=== 统一外部 MCP 服务器信息 ===")
    logger.info(f"名称: {mcp.name}")                # 服务器名称
    logger.info(f"描述: {mcp.instructions}")         # 服务器描述
    logger.info(f"博查 API Key: {'已配置 ✓' if BOCHA_API_KEY else '未配置 ✗（返回教学样例数据）'}")  # API Key 状态

    try:
        # 第八步：打印启动提示信息 —— 动态获取所有已注册工具名称
        # 从 mcp 实例中提取工具列表，确保新增工具后无需手动维护打印内容
        import asyncio
        all_tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
        print("统一外部 MCP 服务器已启动，请访问 http://127.0.0.1:8006/mcp")
        print(f"共注册 {len(all_tool_names)} 个工具：")
        # 按每行 5 个工具名格式化输出，便于阅读
        for i in range(0, len(all_tool_names), 5):
            batch = all_tool_names[i:i + 5]
            print("  " + " | ".join(batch))
        # 第九步：启动 MCP 服务器，使用 streamable-http 传输模式
        # 监听地址和端口从 Config 读取，Docker 环境中通过环境变量覆盖
        mcp.run(transport="streamable-http", host=conf.mcp_host, port=conf.mcp_trip_port)
    except Exception as e:
        # 启动失败时打印错误信息
        print(f"服务器启动失败: {e}")


# ==================== 程序入口 ====================
if __name__ == '__main__':
    # 直接运行本文件时，创建并启动外部 MCP 服务器
    create_trip_mcp_server()
