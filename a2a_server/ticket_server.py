"""
需求：实现基于A2A的票务查询服务器，同时支持火车票和机票查询。

思路步骤：
    1. 启动 A2A 服务器（端口 5006），注册 Agent Card 暴露自身能力（名称、描述、技能）。
    2. 主助手通过 A2A 协议发送任务（Task），服务器在 handle_task 中接收。
    3. 从任务消息中提取用户的自然语言查询文本。
    4. 通过 langchain-mcp-adapters 的 StreamableHttpConnection 同时连接两个 MCP 服务器：
       - 统一外部 MCP 服务器（8002 端口）：提供 query_train_tickets 火车票工具（py12306 直连 12306）
       - 飞常准 Aviation MCP（远程）：提供 searchFlightsByDepArr / getFlightPriceByCities 等 9 个航班工具
    5. 创建 LangChain Tool Calling Agent：由 LLM 从自然语言中提取参数并自动选择火车票或机票工具。
    6. LLM 将工具返回的数据格式化为友好的中文回复。
    7. 根据结果设置任务状态（COMPLETED / INPUT_REQUIRED / FAILED）并返回给主助手。

    本 Agent 与 weather_server.py 保持完全相同的架构模式：
    - 连接 MCP → 加载工具 → LangChain Agent → 自然语言提取参数 → 调用工具

    涉及的关键技术：
    - LangChain Agent: 让 LLM 自主选择和使用工具的框架
    - Tool Calling Agent: LLM 以结构化格式（函数调用）来使用工具
    - MCP Tools: 统一外部 MCP 提供火车票（py12306）和航班/机票（飞常准）工具
    - create_agent: 创建 Agent 并自动构建工具调用循环（langchain 1.3.x 新 API）

    飞常准 Aviation MCP 已通过 VariFlightClient 集成到统一外部 MCP 服务器（8002 端口），
    本 Agent 只需连接本地 MCP 即可获得全部工具，保持架构一致性。
"""

# ==================== 导入依赖 ====================
import json  # JSON 处理
import asyncio  # 异步 IO 库，用于在同步方法中调用异步 MCP 客户端
import os  # 环境变量读取
import sys  # 系统路径操作，用于把项目上级目录加入 sys.path
import time  # 计时用

from python_a2a import A2AServer, run_server, AgentCard, AgentSkill, TaskStatus, TaskState
# A2AServer: A2A 服务器基类
# run_server: 启动服务器的函数
# AgentCard: 代理卡片，描述本代理的能力、技能等信息
# AgentSkill: 代理技能，描述本代理能做什么
# TaskStatus: 任务状态，标记任务是完成、失败还是需要输入
# TaskState: 任务状态枚举（COMPLETED / FAILED / INPUT_REQUIRED）

from langchain_openai import ChatOpenAI  # LangChain 的大模型接口
from langchain_mcp_adapters.tools import load_mcp_tools  # 从 MCP 连接加载工具
from langchain_mcp_adapters.sessions import StreamableHttpConnection  # Streamable HTTP 连接配置
from langchain.agents import create_agent
# create_agent: 创建基于工具调用的 Agent（langchain 1.3.x 新 API，替代旧版 create_tool_calling_agent + AgentExecutor）

# 把项目代码的上两级目录（04-代码）加入模块搜索路径，保证 SmartVoyage 包可被正确导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from SmartVoyage.config import Config  # 项目配置（模型地址、API Key 等）
from datetime import datetime  # 时间处理，用于获取当前日期
import pytz  # 时区库，用于转换到 Asia/Shanghai 时区

from SmartVoyage.create_logger import logger  # 日志模块

conf = Config()  # 全局配置实例

# ==================== 初始化大模型 ====================
# 创建一个 LLM 实例，供 Agent 使用
# 火车票查询使用 flash 模型：参数提取和结果格式化属于简单任务，追求响应速度
llm = ChatOpenAI(
    model=conf.model_flash,    # 快速模型：意图识别、参数提取、结果总结
    base_url=conf.base_url,    # API 基础地址
    api_key=conf.api_key,      # API 密钥
    temperature=0.1,           # 温度参数：0.1 表示输出比较确定、稳定，适合参数提取
    extra_body={"enable_thinking": False}  # 禁用思考模式
)

# ==================== MCP 客户端配置 ====================
# 统一外部 MCP 服务器运行在 8002 端口，提供全部工具：
# - 火车票：query_train_tickets（py12306 直连 12306）
# - 航班/机票：search_flights、get_flight_price 等（飞常准 Aviation MCP）
# 本 Agent 只需连接这一个本地 MCP 即可获得火车票 + 机票全部能力
# 地址从 Config 读取，Docker 环境中通过环境变量覆盖
MCP_URL = conf.external_mcp_url

# 构建 StreamableHttpConnection 连接配置
mcp_connection = StreamableHttpConnection(
    transport="streamable_http",
    url=MCP_URL,
)

# 模块级工具缓存：首次调用时加载，后续复用，避免每次查询都重新连接 MCP
_cached_tools = None
# 票务 Agent 的工具白名单
_TICKET_TOOL_NAMES = {
    "query_train_tickets",
    "search_flights", "get_flight_price", "get_flight_comfort", "search_flight_itineraries", "search_flights_transfer"
}

async def _get_ticket_tools():
    """获取票务 Agent 所需工具（带缓存）。"""
    global _cached_tools
    if _cached_tools is None:
        t0 = time.time()
        all_tools = await load_mcp_tools(session=None, connection=mcp_connection)
        _cached_tools = [t for t in all_tools if t.name in _TICKET_TOOL_NAMES]
        logger.info(f"[耗时] MCP 工具加载(首次): {time.time() - t0:.2f}s, 共 {len(_cached_tools)} 个工具")
    return _cached_tools


# ==================== 票务查询函数（火车票 + 机票） ====================
async def query_train(conversation: str) -> dict:
    """
    需求：通过 LangChain Agent + 双 MCP 工具执行票务查询（火车票 + 机票），返回格式化结果。

    思路步骤：
        1. 加载全部工具（火车票 + 航班/机票）。
        2. 提前计算当前日期，构建系统 Prompt 并嵌入日期变量。
        3. Agent 自动循环：LLM 判断是火车票还是机票 → 选择对应工具 → 提取参数 → 调用。
        4. 通过 ainvoke 异步执行 Agent，从 messages 列表末尾提取 LLM 生成的最终回复。

    参数：
        conversation (str): 用户的查询内容，例如：
            "北京到上海明天的高铁"        → 调用 query_train_tickets
            "明天上海到北京的直飞航班"    → 调用 search_flights
            "北京到上海的机票最低价"      → 调用 get_flight_price

    返回值：
        dict: 查询结果，格式为：
            - {"status": "success", "message": "格式化后的查询结果"}
            - {"status": "error", "message": "错误信息"}
    """
    try:
        t_total = time.time()  # 总计时开始

        # 第一步：获取票务工具（首次从 MCP 加载后缓存，后续直接复用）
        t0 = time.time()
        tools = await _get_ticket_tools()
        logger.info(f"[耗时] 获取工具(命中缓存): {time.time() - t0:.2f}s, {len(tools)} 个工具")

        # 获取当前日期，提前计算并嵌入系统 Prompt（用户可能说"明天"、"后天"等相对时间）
        current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

        # 定义 Agent 的系统 Prompt，指引 LLM 根据用户意图选择火车票或机票工具
        system_prompt = f"""你是一个票务查询助手，能够调用工具来查询火车票和机票信息。
        你需要仔细分析用户的问题，判断用户要查询的是火车票还是机票，然后选择对应的工具。
        
        ## 火车票查询
        - 使用工具：query_train_tickets
        - 适用场景：用户提到"火车"、"高铁"、"动车"、"火车票价"、"二等座"、"一等座"等关键词
        - 参数：departure_city（出发城市中文）、arrival_city（到达城市中文）、date（YYYY-MM-DD）、seat_type（可选）
        
        ## 机票/航班查询（飞常准）
        - 使用工具：search_flights（按出发/到达城市查直飞航班）、get_flight_price（查票价）、search_flight_itineraries（查行程推荐）、get_flight_comfort（查舒适度）、search_flights_transfer（查中转方案）
        - 适用场景：用户提到"航班"、"飞机"、"机票"、"直飞"、"飞行"等关键词
        - 注意：城市/机场代码使用 IATA 三字码（城市如 BJS、SHA，机场如 PEK、PVG）
        - 日期格式：YYYY-MM-DD
        
        ## 通用规则
        - 用户可能使用相对时间，如"明天"、"后天"、"今天"等，请转换为具体日期（YYYY-MM-DD 格式），当前日期是{current_date}。
        - 如果用户只给了出发城市没给到达城市，需要追问。
        - 如果用户只给了日期没给城市，需要追问。
        - 不能自己编撰参数。
        - **【重要】职责边界**：你只负责票务类请求（火车票/机票）。对话历史中若出现天气查询、景点推荐、旅游攻略等其他类型请求，由其他助手负责，不要代答或代为处理；只围绕最后一条 User 消息中的票务内容作答。
        - **【重要】结合对话上下文理解意图**：你会收到完整的对话历史。如果对话历史中已经包含了出发城市、日期、目的地等信息，请直接从上下文中提取，**不要再追问已知信息**。例如：用户之前说了"Day3在曼谷"，你就知道曼谷是目的地之一；用户说了"国内机票已买"，就不要再问"国内出发城市是哪里"。
        - **【重要】智能规划航班查询**：如果用户给了一个行程（如"Day3在曼谷，Day4在清迈"），你应该根据行程中相邻天的城市变化，推断出需要查询的航段（如曼谷→清迈），**只查询必要的航段**，而不是逐天查询所有日期。
        - **【重要】当你需要向用户追问时，回复必须以"[追问]"开头**，例如："[追问]请问您想查询哪一天的火车票？"
        
        ## 何时追问、何时直接返回
        - 用户要求"列出全部"、"都有哪些"、"所有车次"、"所有航班"等查询类请求时：参数齐全则直接返回完整列表，【不要追问】。
        - 用户明确表示要"预订"、"预定"、"买票"等行动类请求时：如果缺少预订必要参数才追问。
        - 只要查询工具已经返回了结果，就必须先展示结果，【永远不要在有结果时仍然追问】。
        
        查询到结果后，请用清晰的中文格式化输出票务信息。
        如果未查到数据，请回复"未找到相关票务数据，请确认或修改查询条件。"
        """

        # 使用 create_agent 创建 Agent（langchain 1.3.x 新 API）
        t1 = time.time()
        agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
        logger.info(f"[耗时] Agent 创建: {time.time() - t1:.2f}s")

        # 执行 Agent，传入消息列表
        t2 = time.time()
        result = await agent.ainvoke({"messages": [{"role": "user", "content": conversation}]})
        logger.info(f"[耗时] Agent 执行(LLM+工具调用): {time.time() - t2:.2f}s")
        logger.info(f"[耗时] 总计: {time.time() - t_total:.2f}s")

        # 提取最后一条消息的内容作为最终回复
        return {"status": "success", "message": result["messages"][-1].content}

    except Exception as e:
        # MCP 连接失败、工具调用异常等情况的捕获
        logger.error(f"票务 MCP 查询出错：{str(e)}")
        return {"status": "error", "message": f"票务 MCP 查询出错：{str(e)}"}


# ==================== Agent Card（代理卡片） ====================
# Agent Card 是 A2A 协议中描述一个代理能力的元数据
# 它告诉主助手：我是谁、我能做什么、我运行在哪里、我有哪些技能
# 主助手通过这个卡片来决定是否将任务路由到这个代理
agent_card = AgentCard(
    name="TicketAssistant",  # 代理名称
    description="基于 LangChain 提供火车票和机票查询服务的助手。火车票通过 py12306 直连 12306 真实接口；机票通过飞常准 Aviation MCP 查询实时航班和票价",  # 代理描述
    url="http://localhost:5006",  # 代理的访问地址
    version="4.0.0",  # 版本号（4.0.0：新增飞常准 MCP 机票查询能力）
    capabilities={"streaming": True, "memory": True},  # 支持的能力：流式输出、记忆
    skills=[  # 技能列表：描述本代理具体能做什么
        AgentSkill(
            name="query train tickets",  # 技能名称
            description="查询火车票余票信息，支持自然语言输入，从 12306 实时查询",  # 技能描述
            examples=["北京到上海明天的高铁", "成都到重庆 2025-07-30 二等座", "北京西到上海虹桥 G101"]  # 使用示例
        ),
        AgentSkill(
            name="query flight tickets",  # 技能名称
            description="查询航班和机票信息，支持直飞/中转航班查询、票价查询、舒适度分析，数据来自飞常准",  # 技能描述
            examples=["明天上海到北京有哪些直飞航班", "北京到上海的机票最低价", "对比明天京沪航班的准点率和舒适度"]  # 使用示例
        )
    ]
)


# ==================== 票务查询服务器类（火车票 + 机票） ====================
class TicketQueryServer(A2AServer):
    """
    需求：实现票务查询 A2A 服务器，同时支持火车票和机票查询。

    思路步骤：
        1. 继承 A2AServer 基类，通过 Agent Card 注册自身能力（名称、描述、技能、端口）。
        2. 在 handle_task 中接收 A2A 任务，提取用户查询文本。
        3. 调用 query_train 异步函数执行 MCP 工具查询（内部连接两个 MCP：py12306 + 飞常准）。
        4. 根据返回结果设置任务状态：COMPLETED（成功） / INPUT_REQUIRED（追问） / FAILED（失败）。
        5. 将任务对象返回给主助手，完成任务生命周期。

    任务状态说明：
    - COMPLETED: 任务成功完成，结果放在 task.artifacts 中
    - FAILED: 任务失败，错误信息放在 task.status.message 中
    - INPUT_REQUIRED: 需要用户补充输入，追问信息放在 task.status.message 中
    """

    def __init__(self):
        """
        需求：初始化火车票查询服务器，注册 Agent Card 到 A2A 框架。

        思路步骤：
            1. 调用父类 A2AServer 的 __init__，把 agent_card 传入完成注册。
            2. 注册完成后，A2A 框架就能根据 Agent Card 路由任务到本服务器。
        """
        super().__init__(agent_card=agent_card)  # 调用父类初始化，注册 Agent Card

    def handle_task(self, task):
        """
        需求：处理来自 A2A 客户端（主助手）的火车票查询任务，是本服务器的核心方法。

        思路步骤：
            1. 从任务消息（task.message）中提取用户查询文本 conversation。
            2. 调用 asyncio.run(query_train(conversation)) 同步执行异步查询函数。
            3. 根据返回字典的 status 字段判断结果：
               - status == "success" 且以"[追问]"开头（或命中追问关键词）→ 去掉前缀后设为 INPUT_REQUIRED（追问）
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
        # 从任务消息中获取内容（A2A 协议中，消息以字典格式存储）
        content = (task.message or {}).get("content", {})
        # 提取对话内容，即客户端发起的任务中的用户输入
        conversation = content.get("text", "") if isinstance(content, dict) else ""
        logger.info(f"对话历史及用户问题: {conversation}")

        try:
            # ========== 步骤2：调用 Agent 查询（火车票 + 机票） ==========
            train_result = asyncio.run(query_train(conversation))
            logger.info(f"MCP 查询返回: {train_result}")

            # ========== 步骤3：根据结果设置任务状态 ==========
            if train_result.get("status") == "success":
                result_text = train_result.get("message", "")  # 取出 LLM 生成的回复文本

                # 检查是否是追问消息（LLM 发现信息不足时会追问）
                # 判断优先级：[追问] 前缀（Prompt 约定）> 关键词兜底
                # 注意：LLM 格式化输出中可能包含"请告诉我"等礼貌用语，不能仅凭关键词判断
                is_followup = (
                    result_text.strip().startswith("[追问]")  # Prompt 约定的追问标记
                    or (
                        "请提供" in result_text
                        or "缺少" in result_text
                        or "哪一天" in result_text
                        or "哪个城市" in result_text
                    )
                )

                # ① 缺少参数，需要追问
                if is_followup:
                    # 去掉 [追问] 前缀后返回给用户，设为 INPUT_REQUIRED
                    clean_text = result_text.replace("[追问]", "").strip()
                    task.status = TaskStatus(  # 创建追问状态
                        state=TaskState.INPUT_REQUIRED,  # 状态：需要用户输入
                        message={"role": "agent", "content": {"text": clean_text}}  # 追问内容（已去前缀）
                    )
                # ② 查询成功
                else:
                    task.artifacts = [{"parts": [{"type": "text", "text": result_text}]}]  # 成功结果放入任务产物
                    task.status = TaskStatus(state=TaskState.COMPLETED)  # 设为任务完成

            elif train_result.get("status") == "error":
                # MCP 查询出错：设置为失败状态
                task.status = TaskStatus(  # 创建失败状态
                    state=TaskState.FAILED,  # 状态：失败
                    message={"role": "agent", "content": {"text": train_result.get("message", "查询失败，请重试。")}}  # 错误信息
                )
            else:
                # 未知的状态码，也视为失败
                task.status = TaskStatus(  # 创建失败状态
                    state=TaskState.FAILED,  # 状态：失败
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
    # 创建票务查询服务器实例（火车票 + 机票）
    ticket_server = TicketQueryServer()

    # 打印服务器信息，方便确认启动状态
    print("\n=== 服务器信息 ===")  # 输出分隔标题
    print(f"名称: {ticket_server.agent_card.name}")  # 打印代理名称
    print(f"描述: {ticket_server.agent_card.description}")  # 打印代理描述
    print(f"\nMCP 连接: {MCP_URL}")  # 统一外部 MCP 地址
    print("\n技能:")  # 打印技能标题
    for skill in ticket_server.agent_card.skills:  # 遍历所有技能
        print(f"- {skill.name}: {skill.description}")  # 打印每个技能的名称和描述

    # 启动 A2A 服务器，监听地址和端口从 Config 读取
    # 主助手会通过 http://localhost:5006 连接本服务器
    run_server(ticket_server, host=conf.a2a_host, port=conf.ticket_port)  # 启动服务器（阻塞运行）
