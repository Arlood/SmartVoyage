"""
需求：实现 票务 MCP 服务器
火车票：1个工具
飞机票：5个工具
"""

# ==================== 导入依赖 ====================
import os                                      # 环境变量读取
import sys                                     # 系统路径操作，用于把项目上级目录加入 sys.path
from fastmcp import FastMCP                  # FastMCP 框架，用于快速构建 MCP 服务器

# 把项目代码的上两级目录（04-代码）加入模块搜索路径，保证 SmartVoyage 包可被正确导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from SmartVoyage.config import Config         # 项目配置类
from SmartVoyage.create_logger import logger  # 日志记录器

from SmartVoyage.services.train_service import _query_train_from_12306  # 12306 火车票查询
from SmartVoyage.services.flight_service import _variflight_client  # 飞常准客户端实例

# 实例化配置对象
conf = Config()


# ==================== 创建统一外部 MCP 服务器 ====================

def create_ticket_mcp_server():
    """
    需求：创建并启动统一外部 MCP 服务器，注册 16 个工具并运行服务。
    思路步骤：
    1. 创建 FastMCP 实例，设置名称、描述
    2. 注册火车票工具 query_train_tickets
    3. 注册 5 个航班/机票查询工具（飞常准，条件注册）
    4. 打印服务器状态信息和工具列表
    5. 调用 mcp.run 启动服务（streamable-http 传输模式）

    端口：8004
    """
    # 第一步：创建 FastMCP 实例
    mcp = FastMCP(
        name="TicketAPITools",                     # 服务器名称
        instructions=(
            "票务 API 工具集。包含2类工具：\n"
            "1. 火车票查询：query_train_tickets，通过 py12306 直连 12306 真实接口\n"
            "2. 航班/机票查询（飞常准 Aviation MCP）：search_flights、get_flight_price 等，查询实时航班和票价"
        ),
    )

    # 第二步：注册工具
    # ========== 火车票工具（12306） ==========
    @mcp.tool(
        name="query_train_tickets",
        description=(
            "查询火车票（通过py12306查询12306真实接口）。"
            "参数：departure_city(出发城市，如'北京'), arrival_city(到达城市，如'成都'), "
            "date(日期，格式YYYY-MM-DD), seat_type(座位类型，可选，如'二等座'、'一等座'、'商务座')"
        )
    )
    def query_train_tickets(departure_city: str, arrival_city: str, date: str, seat_type: str = None) -> str:
        """
        需求：MCP 工具 —— 12306 火车票查询。
        思路步骤：
        1. 记录查询日志
        2. 委托给 _query_train_from_12306 执行实际查询
        """
        # 记录日志
        logger.info(f"查询火车票(12306): {departure_city} -> {arrival_city}, {date}, {seat_type}")
        # 调用 12306 查询函数
        return _query_train_from_12306(departure_city, arrival_city, date, seat_type)

    # ========== 航班/机票查询工具（飞常准 Aviation MCP） ==========

    if _variflight_client:
        @mcp.tool(
            name="search_flights",
            description=(
                "查询直飞航班信息（来自飞常准）。"
                "参数：date(日期 YYYY-MM-DD)、depcity/dep(出发城市三字码如SHA或机场如PVG)、"
                "arrcity/arr(到达城市三字码如BJS或机场如PEK)。"
                "城市代码用 depcity/arrcity，机场代码用 dep/arr，同侧不要混用。"
                "常见城市代码：北京=BJS, 上海=SHA, 广州=CAN, 深圳=SZX, 成都=CTU, 杭州=HGH"
            ),
        )
        def search_flights(date: str, depcity: str = None, arrcity: str = None,
                           dep: str = None, arr: str = None) -> str:
            """
            需求：MCP 工具 —— 按出发/到达城市和日期查询直飞航班（飞常准）。
            思路步骤：
                1. 构造参数（date + depcity/arrcity 或 dep/arr）。
                2. 委托给 _variflight_client.call_tool("searchFlightsByDepArr") 执行。
            """
            logger.info(f"查询直飞航班: date={date}, depcity={depcity}, arrcity={arrcity}, dep={dep}, arr={arr}")
            arguments = {"date": date}
            if depcity:
                arguments["depcity"] = depcity
            if arrcity:
                arguments["arrcity"] = arrcity
            if dep:
                arguments["dep"] = dep
            if arr:
                arguments["arr"] = arr
            return _variflight_client.call_tool("searchFlightsByDepArr", arguments)

        @mcp.tool(
            name="get_flight_price",
            description=(
                "查询两城之间的机票价格（结构化票价数据，逐航班逐舱位，来自飞常准）。"
                "参数：dep_city(出发城市三字码)、arr_city(到达城市三字码)、dep_date(日期 YYYY-MM-DD)"
            ),
        )
        def get_flight_price(dep_city: str, arr_city: str, dep_date: str) -> str:
            """
            需求：MCP 工具 —— 查询两城之间的机票价格（飞常准）。
            思路步骤：
                1. 构造参数（dep_city、arr_city、dep_date）。
                2. 委托给 _variflight_client.call_tool("getFlightPriceByCities") 执行。
            """
            logger.info(f"查询机票价格: {dep_city} -> {arr_city}, {dep_date}")
            return _variflight_client.call_tool("getFlightPriceByCities", {
                "dep_city": dep_city, "arr_city": arr_city, "dep_date": dep_date
            })

        @mcp.tool(
            name="search_flight_itineraries",
            description=(
                "查询两城之间的航班行程推荐（最低价、最短时长、推荐选项的自然语言摘要，来自飞常准）。"
                "参数：depCityCode(出发城市三字码)、arrCityCode(到达城市三字码)、depDate(日期 YYYY-MM-DD)"
            ),
        )
        def search_flight_itineraries(depCityCode: str, arrCityCode: str, depDate: str) -> str:
            """
            需求：MCP 工具 —— 查询航班行程推荐摘要（飞常准）。
            思路步骤：
                1. 构造参数。
                2. 委托给 _variflight_client.call_tool("searchFlightItineraries") 执行。
            """
            logger.info(f"查询行程推荐: {depCityCode} -> {arrCityCode}, {depDate}")
            return _variflight_client.call_tool("searchFlightItineraries", {
                "depCityCode": depCityCode, "arrCityCode": arrCityCode, "depDate": depDate
            })

        @mcp.tool(
            name="get_flight_comfort",
            description=(
                "查询航班舒适度信息（准点率、机型座椅、餐食娱乐、行李额等，来自飞常准）。"
                "参数：fnum(航班号含航司二字码如MU2157)、date(日期 YYYY-MM-DD)、dep(出发机场三字码)、arr(到达机场三字码)"
            ),
        )
        def get_flight_comfort(fnum: str, date: str, dep: str, arr: str) -> str:
            """
            需求：MCP 工具 —— 查询航班舒适度（飞常准）。
            思路步骤：
                1. 构造参数。
                2. 委托给 _variflight_client.call_tool("flightHappinessIndex") 执行。
            """
            logger.info(f"查询航班舒适度: {fnum}, {date}, {dep}->{arr}")
            return _variflight_client.call_tool("flightHappinessIndex", {
                "fnum": fnum, "date": date, "dep": dep, "arr": arr
            })

        @mcp.tool(
            name="search_flights_transfer",
            description=(
                "查询两城之间的中转航班方案（来自飞常准）。"
                "参数：depdate(日期 YYYY-MM-DD)、depcity(出发城市三字码)、arrcity(到达城市三字码)"
            ),
        )
        def search_flights_transfer(depdate: str, depcity: str, arrcity: str) -> str:
            """
            需求：MCP 工具 —— 查询中转航班方案（飞常准）。
            思路步骤：
                1. 构造参数。
                2. 委托给 _variflight_client.call_tool("getFlightTransferInfo") 执行。
            """
            logger.info(f"查询中转航班: {depcity} -> {arrcity}, {depdate}")
            return _variflight_client.call_tool("getFlightTransferInfo", {
                "depdate": depdate, "depcity": depcity, "arrcity": arrcity
            })

    # 第七步：打印服务器状态信息
    logger.info("=== 统一外部 MCP 服务器信息 ===")
    logger.info(f"名称: {mcp.name}")                # 服务器名称
    logger.info(f"描述: {mcp.instructions}")         # 服务器描述
    logger.info(f"飞常准 API Key: {'已配置 ✓' if _variflight_client else '未配置 ✗（航班工具不可用）'}")  # 飞常准 API Key 状态

    try:
        # 第八步：打印启动提示信息 —— 动态获取所有已注册工具名称
        # 从 _mcp 实例中提取工具列表，确保新增工具后无需手动维护打印内容
        import asyncio
        all_tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
        print("统一外部 MCP 服务器已启动，请访问 http://127.0.0.1:8004/mcp")
        print(f"共注册 {len(all_tool_names)} 个工具：")
        # 按每行 5 个工具名格式化输出，便于阅读
        for i in range(0, len(all_tool_names), 5):
            batch = all_tool_names[i:i + 5]
            print("  " + " | ".join(batch))
        # 第九步：启动 MCP 服务器，使用 streamable-http 传输模式
        # 监听地址和端口从 Config 读取，Docker 环境中通过环境变量覆盖
        mcp.run(transport="streamable-http", host=conf.mcp_host, port=conf.mcp_ticket_port)
    except Exception as e:
        # 启动失败时打印错误信息
        print(f"服务器启动失败: {e}")


# ==================== 程序入口 ====================
if __name__ == '__main__':
    # 直接运行本文件时，创建并启动外部 MCP 服务器
    create_ticket_mcp_server()
