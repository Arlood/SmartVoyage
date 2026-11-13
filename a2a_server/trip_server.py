"""
需求：实现基于A2A的行程管家服务器，处理用户的租车、旅游团、保险、旅游攻略、本地生活、路径规划查询请求。

思路步骤：
    1. 启动 A2A 服务器（端口 5007），注册 Agent Card 暴露自身能力（名称、描述、8 项技能）。
    2. 主助手通过 A2A 协议发送任务（Task），服务器在 handle_task 中接收。
    3. 从任务消息中提取用户的自然语言查询文本。
    4. 通过 langchain-mcp-adapters 的 StreamableHttpConnection 连接统一外部 MCP 服务器（8002 端口），加载本地生活 + 旅游攻略联网搜索工具。
    5. 通过 langchain-mcp-adapters 的 StreamableHttpConnection 连接商业 MCP 服务（8005 端口），加载租车/保险/旅游团工具。
    6. 合并两组工具（去重），创建 LangChain Tool Calling Agent。
    7. LLM 分析用户输入，从所有工具中自主选择合适的工具并提取参数。
    8. 工具返回结果后，LLM 将结果格式化为友好的中文回复（保险产品等还会输出结构化 <products> 标签）。
    9. 根据结果设置任务状态（COMPLETED / INPUT_REQUIRED / FAILED）并返回给主助手。

    本 Agent 采用双 MCP 架构：
    1. 统一外部 MCP（8002 端口）：
       - 提供 geocode、regeocode、search_around、direction_* 等本地生活工具
       - 提供 search_travel_guides（旅游攻略联网搜索，基于博查 AI Web Search API）
    2. 商业 MCP 服务（8005 端口，由 config.business_mcp_url 指定）：
       - 提供 search_car_rental（租车查询）、query_insurance（保险查询）、query_tour_group（旅游团查询）
       - 业务团队独立开发部署，通过 Docker 容器运行
"""

# ==================== 导入依赖 ====================
import json  # JSON 处理
import asyncio  # 异步 IO 库
import os  # 环境变量读取
import sys  # 系统路径操作，用于把项目上级目录加入 sys.path
import time  # 计时用

from python_a2a import A2AServer, run_server, AgentCard, AgentSkill, TaskStatus, TaskState
# A2AServer: A2A 服务器基类
# run_server: 启动服务器的函数
# AgentCard: 代理卡片
# AgentSkill: 代理技能
# TaskStatus: 任务状态对象
# TaskState: 任务状态枚举（COMPLETED / FAILED / INPUT_REQUIRED）

from langchain_openai import ChatOpenAI  # LangChain 的大模型接口
from langchain_mcp_adapters.tools import load_mcp_tools  # 从 MCP 连接加载工具
from langchain_mcp_adapters.sessions import StreamableHttpConnection  # Streamable HTTP 连接配置
from langchain.agents import create_agent
# create_agent: 创建基于工具调用的 Agent（langchain 1.3.x 新 API，替代旧版 create_tool_calling_agent + AgentExecutor）

from datetime import datetime  # 时间处理
import pytz  # 时区库

# 把项目代码的上两级目录（04-代码）加入模块搜索路径，保证 SmartVoyage 包可被正确导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from SmartVoyage.config import Config  # 项目配置
from SmartVoyage.create_logger import logger  # 日志模块

conf = Config()  # 全局配置实例

# ==================== 初始化大模型 ====================
# 创建一个 LLM 实例，供 Agent 使用（行程管家使用 flash 模型，参数提取和结果格式化属于简单任务）
llm = ChatOpenAI(
    model=conf.model_flash,
    base_url=conf.base_url,
    api_key=conf.api_key,
    temperature=conf.temperature,
    extra_body={"enable_thinking": False}
)

# ==================== MCP 客户端配置 ====================
# 本 Agent 同时连接两个 MCP 服务器（双 MCP 架构）：

# MCP1: 统一外部 MCP 服务器（8002 端口）
# 提供旅游攻略联网搜索（search_travel_guides）
# 地址从 Config 读取，Docker 环境中通过环境变量覆盖
EXTERNAL_MCP_URL = conf.external_mcp_url

# MCP2: 商业 MCP 服务（8005 端口，由 config.business_mcp_url 指定）
# 提供 search_car_rental（租车）、query_insurance（保险）、query_tour_group（旅游团）
# 业务团队通过 Docker 独立部署，学生只需在 config.yaml 中配置 URL 即可
BUSINESS_MCP_URL = conf.business_mcp_url

# MCP3: 高德地图MCP
# 提供本地生活工具（search_poi、geocode 等)
# 地址从 Config 读取，Docker 环境中通过环境变量覆盖
AMAP_MCP_URL = conf.amap_mcp_url


# 构建两个 StreamableHttpConnection 连接配置（双 MCP 架构）
# langchain-mcp-adapters 0.3.2 的 connection 模式：
# load_mcp_tools 内部自动管理连接生命周期，无需手动创建 ClientSession
external_mcp_connection = StreamableHttpConnection(
    transport="streamable_http",
    url=EXTERNAL_MCP_URL
)

business_mcp_connection = StreamableHttpConnection(
    transport="streamable_http",
    url=BUSINESS_MCP_URL
)


# 高德 MCP 为可选连接：未配置 AMAP_MCP_URL 时跳过该连接，
# 本地生活工具改由统一外部 MCP（8002）提供，避免 url=None 触发 httpx 报错
amap_mcp_connection = (
    StreamableHttpConnection(
        transport="streamable_http",
        url=AMAP_MCP_URL
    )
    if AMAP_MCP_URL else None
)

# 模块级工具缓存：首次调用时从两个 MCP 加载，后续复用
_cached_trip_tools = None

# 本地生活工具（高德）：统一外部 MCP 与高德 MCP 都提供
_LOCAL_LIFE_TOOL_NAMES = {
    "geocode", "regeocode",
    "search_poi", "search_around",
    "direction_driving", "direction_transit", "direction_walking", "direction_bicycling"
}

# 外部 MCP 工具白名单：
# - 始终取旅游攻略搜索
# - 未单独配置高德 MCP 时，本地生活工具也从外部 MCP 取，避免功能缺失
_TRIP_TOOL_NAMES = {"search_travel_guides"} | (
    set() if amap_mcp_connection else _LOCAL_LIFE_TOOL_NAMES
)

async def _get_trip_tools():
    """获取行程 Agent 所需的全部工具（双 MCP，带缓存）。"""
    global _cached_trip_tools
    if _cached_trip_tools is None:
        t0 = time.time()
        external_tools = await load_mcp_tools(session=None, connection=external_mcp_connection)
        trip_tools = [t for t in external_tools if t.name in _TRIP_TOOL_NAMES]

        business_tools = await load_mcp_tools(session=None, connection=business_mcp_connection)

        amap_tools = []
        if amap_mcp_connection is not None:
            amap_tools = await load_mcp_tools(session=None, connection=amap_mcp_connection)

        _cached_trip_tools = trip_tools + business_tools + amap_tools

        logger.info(f"[耗时] 行程MCP工具加载(首次): {time.time() - t0:.2f}s, "
                     f"外部 {len(external_tools)} 个 + 商业 {len(business_tools)} 个 + "
                     f"高德 {len(amap_tools)} 个 = 共 {len(_cached_trip_tools)} 个")
    return _cached_trip_tools


# ==================== 行程查询函数 ====================
async def query_trip(conversation: str) -> dict:
    """
    需求：通过 LangChain Agent + 双 MCP 工具执行行程查询，返回格式化结果。

    思路步骤：
        1. 加载全部工具（本地生活 + 旅游攻略 + 租车/旅游团/保险）。
        2. 提前计算当前日期，构建系统 Prompt 并嵌入日期变量。
        3. 使用 create_agent 创建基于工具调用的 Agent。
        4. Agent 自动循环：LLM 提取参数 → 调用 query_weather 工具 → 处理结果。
        5. 通过 ainvoke 异步执行 Agent，从 messages 列表末尾提取 LLM 生成的最终回复。
        6. 若发生 ExceptionGroup（MCP 内部 anyio 抛出的嵌套异常），递归展开并记录每个子异常。
        7. 若发生其他 BaseException，记录堆栈信息并返回错误结果。

    参数：
        conversation (str): 用户的查询内容，例如：
            "成都租车"
            "有没有去丽江的旅游团"
            "旅行保险推荐"
            "帮我规划五一旅行，顺便推荐保险和旅游团"

    返回值：
        dict: 查询结果，格式为：
            - {"status": "success", "message": "格式化后的查询结果"}
            - {"status": "error", "message": "错误信息"}
    """
    try:
        # 获取行程工具（首次从双 MCP 加载后缓存，后续直接复用）
        t_tools = time.time()
        tools = await _get_trip_tools()
        logger.info(f"[耗时] 获取行程工具(命中缓存): {time.time() - t_tools:.2f}s, {len(tools)} 个")

        # 获取当前日期和数据来源说明，提前计算并嵌入系统 Prompt
        current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
        search_data_source = "博查联网搜索（真实互联网搜索结果，已集成在统一外部 MCP 服务中）"

        # 定义 Agent 的系统 Prompt（使用 f-string 直接嵌入变量，JSON 中的花括号需用 {{}} 转义）
        system_prompt = f"""你是一个行程管家助手，能够调用工具来完成以下任务：

        【租车查询（使用商业 MCP 服务的工具）】
        - search_car_rental: 查询指定城市的租车门店，返回门店名称、地址、电话、价格等信息，如 search_car_rental(city="北京")

        【保险查询（使用商业 MCP 服务的工具）】
        - query_insurance: 查询旅行保险产品，传入用户查询描述

        【旅游团查询（使用商业 MCP 服务的工具）】
        - query_tour_group: 查询旅游团信息，支持自然语言描述需求

        【本地生活搜索与路径规划（使用统一外部 MCP 的工具）】
        - search_poi:根据关键词搜索酒店、餐厅、景点等
        - search_around: 搜索指定坐标附近的餐厅、酒店、景点等（需先用 geocode 获取坐标）
        - geocode: 将地址转为经纬度坐标
        - direction_driving/transit/walking/bicycling: 路径规划

        【旅游攻略联网搜索】
        - search_travel_guides: 联网搜索旅游攻略，返回真实的网页搜索结果（标题、来源、摘要、链接）
          1. 数据来源：{search_data_source}
          2. 搜索规则（强制）：
            - 当用户提到「攻略」「旅游」「避坑」「打卡」「美食推荐」等关键词，或明确要求搜索某个目的地的旅行经验/建议时，**必须**调用 search_travel_guides 工具获取真实数据
            - **严禁自己编造旅游攻略内容**——你必须通过工具获取真实的互联网搜索结果
            - 调用示例：search_travel_guides(query="丽江")

          3.推荐规则
            - 当用户咨询旅行规划、目的地推荐、节假日出行等话题时，主动推荐保险和旅游团
            - 先调用 query_insurance 和 query_tour_group 获取产品列表，然后在回复中推荐
            - 租车查询使用 search_car_rental 查询商业 MCP 服务中的租车门店数据

        【重要：结构化产品数据输出】
            当查询到保险或旅游团产品时，请在自然语言描述之后，额外附加一段结构化产品数据，
            用 <products>...</products> 标签包裹 JSON 数组。

            保险产品字段：name, price, price_num（数字）, coverage, suitable_for, highlight
            旅游团字段：name, price, price_num（数字）, destination, days, highlights, suitable_for, schedule

            示例：
            <products>[{{"name": "境内旅行意外险", "price": "30元/份", "price_num": 30, "coverage": "意外身故/残疾最高80万，意外医疗5万", "suitable_for": "国内自由行旅客", "highlight": "价格实惠，保障全面"}}, {{"name": "丽江大理双飞6日游", "price": "3580元/人", "price_num": 3580, "destination": "云南丽江+大理", "days": 6, "highlights": "含往返机票、五星酒店、玉龙雪山大索道", "suitable_for": "情侣/朋友出游", "schedule": "D1：抵达丽江→古城自由逛 D2：玉龙雪山→蓝月谷"}}]</products>

            注意：price_num 必须是数字（用于前端价格计算）。

        注意：
        - 用户可能使用相对时间，如"明天"、"后天"等，请根据当前日期 {current_date} 转换为具体日期。
        - 如果用户提供的信息不足以提取到调用工具所有必要参数，则向用户追问，以获取该信息。不能自己编撰参数。
        - **【重要】结合对话上下文理解意图**：你会收到完整的对话历史。如果对话历史中已经包含了目的地、日期、出行人数等信息，请直接从上下文中提取，**不要再追问已知信息**。例如：用户说了"国庆去泰国9月26-10月6号"，你就知道目的地和日期；用户说了"海岛去普吉岛"，你就知道海岛选普吉；用户说了"国内机票已买"，就不要再问"国内出发城市是哪里"。
        - **【重要】职责边界**：你只负责行程管家类请求（租车、本地生活与路径规划、保险、旅游团、旅游攻略与景点推荐）。对话历史中若出现火车票/机票、天气查询等其他类型请求，由其他助手负责，不要代答或代为处理；只围绕最后一条 User 消息中的行程相关内容作答。
        - **【重要】当你需要向用户追问时，回复必须以"[追问]"开头**，例如："[追问]请问您想在哪一天取车？"。如果查询工具已经返回了结果，就必须先展示结果，永远不要在有结果时仍然追问。

        查询到结果后，请用清晰的中文格式化输出。
        """

        # 使用 create_agent 创建 Agent
        agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)

        # 执行 Agent，传入消息列表
        result = await agent.ainvoke({"messages": [{"role": "user", "content": conversation}]})

        # 提取最后一条消息的内容作为最终回复
        return {"status": "success", "message": result["messages"][-1].content}

    except ExceptionGroup as eg:
        # MCP 工具内部使用 anyio.create_task_group()，失败时抛出 ExceptionGroup
        # 递归展开所有子异常（ExceptionGroup 可嵌套），打印完整栈
        import traceback  # 导入 traceback 模块，用于格式化异常堆栈

        def _print_eg(eg, depth=0):  # 递归打印 ExceptionGroup 中的所有子异常
            prefix = "  " * depth  # 根据嵌套深度生成缩进前缀
            logger.error(f"{prefix}ExceptionGroup（{len(eg.exceptions)} 个子异常）：")  # 记录异常组信息
            for i, exc in enumerate(eg.exceptions):  # 遍历每个子异常
                if isinstance(exc, ExceptionGroup):  # 如果子异常本身也是 ExceptionGroup（嵌套）
                    _print_eg(exc, depth + 1)  # 递归处理嵌套的 ExceptionGroup
                else:
                    logger.error(f"{prefix}  子异常 {i}: {type(exc).__name__}: {exc}")  # 记录子异常类型和信息
                    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)  # 格式化完整堆栈
                    logger.error("".join(tb_lines))  # 输出堆栈信息

        _print_eg(eg)  # 开始递归打印异常组
        first_exc = eg.exceptions[0] if eg.exceptions else eg  # 取第一个子异常作为主要错误信息
        return {"status": "error", "message": f"行程 MCP 查询出错：{first_exc}"}  # 返回错误结果
    except BaseException as e:
        # 兜底捕获（包括 ExceptionGroup 等 BaseException 子类）
        import traceback  # 导入 traceback 模块
        logger.error(f"行程 MCP 查询出错：{type(e).__name__}: {e}")  # 记录异常类型和信息
        logger.error("".join(traceback.format_exception(type(e), e, e.__traceback__)[-5:]))  # 记录最后 5 行堆栈
        return {"status": "error", "message": f"行程 MCP 查询出错：{e}"}  # 返回错误结果


# ==================== Agent Card（代理卡片） ====================
# 描述行程管家代理的能力、技能等信息
agent_card = AgentCard(
    name="TripAssistant",  # 代理名称
    description="基于 LangChain 提供行程管家服务的统一助手，支持租车、旅游团、保险、旅游攻略联网搜索，以及本地生活搜索和路径规划",  # 代理描述
    url="http://localhost:5007",  # 代理的访问地址
    version="3.2.0",  # 版本号（3.2.0：小红书攻略搜索 → 博查旅游攻略联网搜索）
    capabilities={"streaming": True, "memory": True},  # 支持的能力
    skills=[
        # 技能1：租车查询（商业 MCP 服务）
        AgentSkill(
            name="search car rental",
            description="查询指定城市的租车门店，返回门店名称、地址、电话、价格等，数据来自商业 MCP 服务",
            examples=["成都租车", "北京机场附近租车", "租一辆 SUV"]
        ),
        # 技能2：旅游团查询（商业 MCP 服务）
        AgentSkill(
            name="query tour group",
            description="查询旅游团信息，支持自然语言描述需求，数据来自商业 MCP 服务",
            examples=["想看雪山的旅游团", "适合亲子游的短途旅行", "五一去哪玩"]
        ),
        # 技能3：保险查询（商业 MCP 服务）
        AgentSkill(
            name="query insurance",
            description="查询旅行保险产品，数据来自商业 MCP 服务",
            examples=["推荐旅行保险", "买一份综合型旅行保险", "出行保险"]
        ),
        # 技能4：旅游攻略联网搜索（博查联网搜索 MCP 服务）
        AgentSkill(
            name="search travel guides",
            description="联网搜索旅游攻略，返回真实的网页搜索结果（标题、来源、摘要、链接）",
            examples=["成都旅游攻略", "三亚避坑指南", "亲子游推荐", "五一出行攻略"]
        ),
        # 技能5：主动推荐保险和旅游团
        AgentSkill(
            name="recommend trip addons",
            description="当用户咨询旅行规划时，主动推荐保险和旅游团产品",
            examples=["帮我规划五一旅行", "去成都玩有什么推荐"]
        ),
        # 技能6：本地生活搜索
        AgentSkill(
            name="search local life",
            description="搜索附近或指定城市的餐厅、酒店、景点等，支持关键字搜索和周边搜索",
            examples=["附近有什么好吃的", "推荐一家北京烤鸭", "三里屯附近的酒店"]
        ),
        # 技能7：路径规划
        AgentSkill(
            name="plan route",
            description="查询两点之间的路径规划，支持驾车、公交、步行、骑行四种方式",
            examples=["从北京站到三里屯怎么走", "开车去机场要多久", "步行去最近的地铁站"]
        ),
        # 技能8：地理编码
        AgentSkill(
            name="geocode address",
            description="将地址文本转换为经纬度坐标，供周边搜索和路径规划使用",
            examples=["清华大学在哪里", "北京市政府坐标"]
        )
    ]
)

# ==================== 行程查询服务器类 ====================
class TripQueryServer(A2AServer):
    """
    需求：实现行程管家 A2A 服务器，接收主助手任务并返回行程查询结果。

    思路步骤：
        1. 继承 A2AServer 基类，通过 Agent Card 注册自身能力（名称、描述、8 项技能、端口）。
        2. 在 handle_task 中接收 A2A 任务，提取用户查询文本。
        3. 调用 query_trip 异步函数执行双 MCP 工具查询（内部使用 LangChain Agent）。
        4. 根据返回结果设置任务状态：COMPLETED（成功） / INPUT_REQUIRED（追问） / FAILED（失败）。
        5. 将任务对象返回给主助手，完成任务生命周期。

    本 Agent 采用双 MCP 架构：
    - 连接统一外部 MCP（8002 端口）：获取 geocode、direction_* 等本地生活工具 + search_travel_guides 旅游攻略搜索
    - 连接商业 MCP 服务（8005 端口）：获取 search_car_rental + query_insurance + query_tour_group 工具
    """

    def __init__(self):
        """
        需求：初始化行程查询服务器，注册 Agent Card 到 A2A 框架。

        思路步骤：
            1. 调用父类 A2AServer 的 __init__，把 agent_card 传入完成注册。
            2. 注册完成后，A2A 框架就能根据 Agent Card 路由任务到本服务器。
        """
        super().__init__(agent_card=agent_card)  # 调用父类初始化，注册 Agent Card

    def handle_task(self, task):
        """
        需求：处理来自 A2A 客户端（主助手）的行程查询任务，是本服务器的核心方法。

        思路步骤：
            1. 从任务消息（task.message）中提取用户查询文本 conversation。
            2. 调用 asyncio.run(query_trip(conversation)) 同步执行异步查询函数。
            3. 根据返回字典的 status 字段判断结果：
               - status == "success" 且以"[追问]"开头（或无数据且命中追问关键词）→ 去掉前缀后设为 INPUT_REQUIRED（追问）
               - status == "success" 且为正常结果 → 放入 task.artifacts，设为 COMPLETED
               - status == "error" → 设为 FAILED，写入错误信息
               - 其他未知情况 → 也设为 FAILED
            4. 用 try/except 捕获所有异常，确保即使出错也返回有效的任务状态。

        参数：
            task: A2A 任务对象

        返回值：
            task: 处理后的任务对象（已设置状态和结果）
        """
        # ========== 步骤1：提取输入 ==========
        content = (task.message or {}).get("content", {})
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"用户查询: {conversation}")

        try:
            # ========== 步骤2：调用 Agent 查询 ==========
            trip_result = asyncio.run(query_trip(conversation))
            logger.info(f"MCP 查询返回: {trip_result}")

            # ========== 步骤3：根据结果设置任务状态 ==========
            if trip_result.get("status") == "success":
                result_text = trip_result.get("message", "")  # 取出 LLM 生成的回复文本

                # 检查是否是追问消息（LLM 发现信息不足时会追问）
                # 判断优先级：[追问] 前缀（Prompt 约定）> 关键词兜底
                # 注意：LLM 格式化输出中可能包含"请告诉我"等礼貌用语，不能仅凭关键词判断
                # 如果输出中包含实际数据（价格、车型、团名等），说明是查询结果而非追问
                has_data = (
                        "¥" in result_text or "￥" in result_text
                        or "元/天" in result_text
                        or "元/份" in result_text
                        or "余量" in result_text
                        or "余位" in result_text
                        or "天数" in result_text
                        or "保障" in result_text
                        or "车型" in result_text
                        or "旅行社" in result_text
                        or "评分" in result_text
                        or "亮点" in result_text
                        or "相似度" in result_text
                )
                is_followup = (
                    result_text.strip().startswith("[追问]")  # Prompt 约定的追问标记
                    or (not has_data and
                        (
                        "哪一天" in result_text
                        or "哪个城市" in result_text
                        or "哪种车型" in result_text
                        or "哪种保险" in result_text
                        )
                    )
                )

                # ① 缺少参数，需要追问
                if is_followup:
                    # 去掉 [追问] 前缀后返回给用户，设为 INPUT_REQUIRED
                    clean_text = result_text.replace("[追问]", "").strip()
                    task.status = TaskStatus(
                        state=TaskState.INPUT_REQUIRED,
                        message={"role": "agent", "content": {"text": clean_text}}
                    )
                # ② 查询成功
                else:
                    task.artifacts = [{"parts": [{"type": "text", "text": result_text}]}]  # 成功结果放入任务产物
                    task.status = TaskStatus(state=TaskState.COMPLETED)  # 设为任务完成

            elif trip_result.get("status") == "error":
                task.status = TaskStatus(  # MCP 查询出错，设为失败状态
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": trip_result.get("message", "查询失败，请重试。")}}  # 错误信息
                )
            else:
                task.status = TaskStatus(  # 未知状态码，也视为失败
                    state=TaskState.FAILED,
                    message={"role": "agent", "content": {"text": "查询失败，请重试或提供更多细节。"}}  # 兜底错误信息
                )

            return task

        except Exception as e:
            # 捕获所有异常，确保即使出错也能返回有效的任务状态
            logger.error(f"查询失败: {str(e)}")  # 记录错误日志
            task.status = TaskStatus(  # 创建失败状态
                state=TaskState.FAILED,  # 状态：失败
                message={"role": "agent",  # 角色：agent（服务端回复）
                         "content": {"text": f"查询失败: {str(e)} 请重试或提供更多细节。"}}  # 错误提示文本
            )
            return task  # 返回带失败状态的任务


# ==================== 主函数：启动服务器 ====================
if __name__ == "__main__":
    # 创建行程查询服务器实例
    trip_server = TripQueryServer()

    # 打印服务器信息
    print("\n=== 服务器信息 ===")  # 输出分隔标题
    print(f"名称: {trip_server.agent_card.name}")  # 打印代理名称
    print(f"描述: {trip_server.agent_card.description}")  # 打印代理描述
    print(f"\n统一外部 MCP URL: {EXTERNAL_MCP_URL}")  # 打印统一外部 MCP 地址
    print(f"商业 MCP URL: {BUSINESS_MCP_URL}")  # 打印商业 MCP 地址
    print("\n技能:")  # 打印技能标题
    for skill in trip_server.agent_card.skills:  # 遍历所有技能
        print(f"- {skill.name}: {skill.description}")  # 打印每个技能的名称和描述

    # 启动 A2A 服务器，监听地址和端口从 Config 读取
    run_server(trip_server, host=conf.a2a_host, port=conf.trip_port)  # 启动服务器（阻塞运行）
