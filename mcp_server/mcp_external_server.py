"""
需求：实现统一的外部 API MCP 服务器，将所有第三方 API 调用封装到一个 MCP 服务中。

思路步骤：
    1. 导入各服务模块（weather / local_life / train / flight / bocha）
    2. 创建 FastMCP 实例，设置名称、描述、日志级别
    3. 实例化 LocalLifeService
    4. 注册 16 个工具（天气×1 + 本地生活×8 + 火车票×1 + 旅游攻略×1 + 航班×5）
    5. 打印服务器状态信息和工具列表
    6. 启动 streamable-http 服务（8002 端口）

架构说明：
    本服务器是 SmartVoyage 系统中唯一的外部 API 入口，运行在 8002 端口。
    它将原来分散在多个 MCP 服务器中的第三方 API 调用统一合并：
    - 天气查询：和风天气 API（实时）—— 见 services/weather_service.py
    - 本地生活：高德地图 REST API（地理编码、POI 搜索、路径规划等 8 个工具）—— 见 services/local_life_service.py
    - 火车票查询：py12306 直连 12306 真实接口 —— 见 services/train_service.py
    - 旅游攻略联网搜索：博查 AI Web Search API —— 见 services/bocha_service.py
    - 航班/机票查询：飞常准 Aviation MCP（JSON-RPC 2.0 over HTTP）—— 见 services/flight_service.py

    优势：
    - 统一入口：所有第三方 API 调用集中在一个服务中，便于管理和监控
    - 减少端口：只需启动一个 MCP 服务（8002），而非原来的多个（8001/8002/8003/8004）
    - 简化架构：A2A Agent 只需连接一个 MCP 服务即可获取所有外部工具
    - 职责分离：实现逻辑放在 utils/ 下的独立模块，本文件只负责 MCP 注册和路由

    高德 API Key 申请：
    - 注册地址：https://lbs.amap.com/
    - 创建应用后获取 Key，设置环境变量 AMAP_API_KEY

    博查 API Key 申请：
    - 注册地址：https://open.bochaai.com
    - 注册后在「API KEY 管理」处创建，设置环境变量 BOCHA_API_KEY
    - 降级策略：API Key 未配置或调用失败时，返回教学样例数据

    飞常准 API Key 申请：
    - 注册地址：https://mcp.variflight.com/
    - 注册即送 50 元体验额度，设置环境变量 VARIFLIGHT_API_KEY
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
from SmartVoyage.services.local_life_service import local_life_service  # 高德本地生活服务类
from SmartVoyage.services.train_service import _query_train_from_12306  # 12306 火车票查询
from SmartVoyage.services.flight_service import _variflight_client  # 飞常准客户端实例
from SmartVoyage.services.bocha_service import (
    BOCHA_API_KEY,                 # 博查 API Key
    _call_bocha_api,               # 博查 API 调用
    _format_search_results,        # 搜索结果格式化
    _get_sample_data,              # 教学样例数据
)

# 实例化配置对象
conf = Config()


# ==================== 创建统一外部 MCP 服务器 ====================

def create_external_mcp_server():
    """
    需求：创建并启动统一外部 MCP 服务器，注册 16 个工具并运行服务。
    思路步骤：
    1. 创建 FastMCP 实例，设置名称、描述
    2. 实例化 LocalLifeService
    3. 注册天气工具 query_weather（直连和风天气 API）
    4. 注册 8 个高德本地生活工具（geocode/regeocode/search_poi/search_around/direction_*）
    5. 注册火车票工具 query_train_tickets
    6. 注册旅游攻略搜索工具 search_travel_guides（含博查 API + 样例数据降级）
    7. 注册 5 个航班/机票查询工具（飞常准，条件注册）
    8. 打印服务器状态信息和工具列表
    9. 调用 external_mcp.run 启动服务（streamable-http 传输模式）

    端口：8002
    工具分类：
    - 天气工具：query_weather（1 个）
    - 本地生活工具：geocode/regeocode/search_poi/search_around/direction_*（8 个）
    - 火车票工具：query_train_tickets（1 个）
    - 旅游攻略联网搜索：search_travel_guides（1 个）
    - 航班/机票查询：search_flights/get_flight_price/search_flight_itineraries/get_flight_comfort/search_flights_transfer（5 个）
    共 16 个工具
    """
    # 第一步：创建 FastMCP 实例
    external_mcp = FastMCP(
        name="ExternalAPITools",                     # 服务器名称
        instructions=(
            "统一外部 API 工具集。包含五类工具：\n"
            "1. 天气查询：query_weather，直连和风天气 API 获取实时天气数据\n"
            "2. 本地生活（高德地图）：地理编码、POI搜索、周边搜索、路径规划（驾车/公交/步行/骑行）\n"
            "3. 火车票查询：query_train_tickets，通过 py12306 直连 12306 真实接口\n"
            "4. 旅游攻略联网搜索：search_travel_guides，基于博查 AI Web Search API，返回真实网页搜索结果\n"
            "5. 航班/机票查询（飞常准 Aviation MCP）：search_flights、get_flight_price 等，查询实时航班和票价"
        ),
    )

    # 第二步：注册工具
    # ========== 天气工具 ==========

    @external_mcp.tool(
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

    # ========== 本地生活工具（高德地图） ==========

    @external_mcp.tool(
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

    @external_mcp.tool(
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

    @external_mcp.tool(
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

    @external_mcp.tool(
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

    @external_mcp.tool(
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

    @external_mcp.tool(
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

    @external_mcp.tool(
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

    @external_mcp.tool(
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

    # ========== 火车票工具（12306） ==========

    @external_mcp.tool(
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

    # ========== 旅游攻略联网搜索工具（博查 AI API） ==========

    @external_mcp.tool(
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

    # ========== 航班/机票查询工具（飞常准 Aviation MCP） ==========

    if _variflight_client:
        @external_mcp.tool(
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

        @external_mcp.tool(
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

        @external_mcp.tool(
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

        @external_mcp.tool(
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

        @external_mcp.tool(
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
    logger.info(f"名称: {external_mcp.name}")                # 服务器名称
    logger.info(f"描述: {external_mcp.instructions}")         # 服务器描述
    logger.info(f"博查 API Key: {'已配置 ✓' if BOCHA_API_KEY else '未配置 ✗（返回教学样例数据）'}")  # API Key 状态
    logger.info(f"飞常准 API Key: {'已配置 ✓' if _variflight_client else '未配置 ✗（航班工具不可用）'}")  # 飞常准 API Key 状态

    try:
        # 第八步：打印启动提示信息 —— 动态获取所有已注册工具名称
        # 从 external_mcp 实例中提取工具列表，确保新增工具后无需手动维护打印内容
        import asyncio
        all_tool_names = [t.name for t in asyncio.run(external_mcp.list_tools())]
        print("统一外部 MCP 服务器已启动，请访问 http://127.0.0.1:8002/mcp")
        print(f"共注册 {len(all_tool_names)} 个工具：")
        # 按每行 5 个工具名格式化输出，便于阅读
        for i in range(0, len(all_tool_names), 5):
            batch = all_tool_names[i:i + 5]
            print("  " + " | ".join(batch))
        # 第九步：启动 MCP 服务器，使用 streamable-http 传输模式
        # 监听地址和端口从 Config 读取，Docker 环境中通过环境变量覆盖
        external_mcp.run(transport="streamable-http", host=conf.mcp_host, port=conf.mcp_port)
    except Exception as e:
        # 启动失败时打印错误信息
        print(f"服务器启动失败: {e}")


# ==================== 程序入口 ====================
if __name__ == '__main__':
    # 直接运行本文件时，创建并启动外部 MCP 服务器
    create_external_mcp_server()
