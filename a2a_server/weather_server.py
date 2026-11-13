"""
需求：实现基于A2A的天气查询服务器，处理用户的天气查询请求并返回结果。

思路步骤：
    1. 启动 A2A 服务器（端口 5005），注册 Agent Card 暴露自身能力（名称、描述、技能）。
    2. 主助手通过 A2A 协议发送任务（Task），服务器在 handle_task 中接收。
    3. 从任务消息中提取用户的自然语言查询文本。
    4. 通过 langchain-mcp-adapters 的 StreamableHttpConnection 连接统一外部 MCP 服务器（8002 端口），加载 query_weather 等工具。
    5. 创建 LangChain Tool Calling Agent：由 LLM 从自然语言中提取参数（城市、日期），调用 MCP 工具。
    6. MCP 工具内部执行查询（支持数据库 / 和风天气 API 双数据源），返回结构化数据。
    7. LLM 将工具返回数据格式化为友好的中文回复。
    8. 根据结果设置任务状态（COMPLETED / INPUT_REQUIRED / FAILED）并返回给主助手。

    涉及的关键技术：
    - LangChain Agent: 让 LLM 自主选择和使用工具的框架
    - Tool Calling Agent: LLM 以结构化格式（函数调用）来使用工具
    - MCP Tools: 统一外部 MCP 服务器提供的参数化工具（city、start_date、end_date）
    - create_agent: 创建 Agent 并自动构建工具调用循环（langchain 1.3.x 新 API）
"""

# ==================== 导入依赖 ====================
import json  # JSON 处理
import asyncio  # 异步 IO 库，用于在同步方法中调用异步 MCP 客户端
import os  # 环境变量读取
import sys  # 系统路径操作，用于把项目上级目录加入 sys.path
import time  # 计时用

from python_a2a import A2AServer, run_server, AgentCard, AgentSkill, TaskStatus, TaskState
# A2AServer: A2A 服务器基类，我们需要继承它来实现自己的天气查询服务器
# run_server: 启动 A2A 服务器的函数
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

from SmartVoyage.config import Config  # 项目配置（模型地址、API Key等）
from datetime import datetime  # 时间处理，用于获取当前日期
import pytz  # 时区库，用于转换到 Asia/Shanghai 时区

from SmartVoyage.create_logger import logger  # 日志模块

conf = Config()  # 全局配置实例

# ==================== 初始化大模型 ====================
# 创建一个 LLM 实例，供 Agent 使用
# 天气查询使用 flash 模型：参数提取和结果格式化属于简单任务，追求响应速度
llm = ChatOpenAI(
    model=conf.model_flash,    # 快速模型：意图识别、参数提取、结果总结
    base_url=conf.base_url,    # API 基础地址
    api_key=conf.api_key,      # API 密钥
    temperature=0.1,           # 温度参数：0.1 表示输出比较确定、稳定，适合参数提取
    extra_body={"enable_thinking": False}  # 禁用思考模式
)

# ==================== MCP 客户端配置 ====================
# MCP Server 运行在 8002 端口，提供天气查询的参数化工具
# 本服务器通过连接 MCP Server，让 Agent 自主调用其工具
# 地址从 Config 读取，Docker 环境中通过环境变量覆盖
MCP_URL = conf.external_mcp_url

# 构建 StreamableHttpConnection 连接配置
# langchain-mcp-adapters 0.3.2 的 connection 模式：
# load_mcp_tools 内部自动管理连接生命周期，无需手动创建 ClientSession
mcp_connection = StreamableHttpConnection(
    transport="streamable_http",
    url=MCP_URL,
)

# 天气 Agent 只需要的工具白名单
_WEATHER_TOOL_NAMES = {"query_weather"}

# 模块级工具缓存
_cached_tools = None

async def _get_weather_tools():
    """获取天气 Agent 所需工具（带缓存 + 白名单过滤）。"""
    global _cached_tools
    if _cached_tools is None:
        t0 = time.time()
        all_tools = await load_mcp_tools(session=None, connection=mcp_connection)
        _cached_tools = [t for t in all_tools if t.name in _WEATHER_TOOL_NAMES]
        logger.info(f"[耗时] 天气MCP工具加载(首次): {time.time() - t0:.2f}s, "
                     f"全部 {len(all_tools)} 个 → 过滤后 {len(_cached_tools)} 个")
    return _cached_tools


# ==================== 天气查询函数 ====================
async def query_weather(conversation: str) -> dict:
    """
    需求：通过 LangChain Agent + 统一外部 MCP 工具执行天气查询，返回格式化结果。

    思路步骤：
        1. 加载天气工具。
        2. 提前计算当前日期，构建系统 Prompt 并嵌入日期变量。
        3. 使用 create_agent 创建基于工具调用的 Agent。
        4. Agent 自动循环：LLM 提取参数 → 调用 query_weather 工具 → 处理结果。
        5. 通过 ainvoke 异步执行 Agent，从 messages 列表末尾提取 LLM 生成的最终回复。

    参数：
        conversation (str): 用户的查询内容，例如：
            "北京明天天气怎么样？"
            "上海未来3天的天气"
            "北京2025-07-30的天气"

    返回值：
        dict: 查询结果，格式为：
            - {"status": "success", "message": "格式化后的查询结果"}  # 查询成功
            - {"status": "error", "message": "错误信息"}  # 查询失败
    """
    try:
        # 获取天气工具（首次从 MCP 加载后缓存，后续直接复用）
        tools = await _get_weather_tools()

        # 获取当前日期，提前计算并嵌入系统 Prompt（用户可能说"明天"、"后天"等相对时间）
        current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

        # 定义 Agent 的系统 Prompt，指引 LLM 提取参数并调用天气工具
        system_prompt = f"""
        你是一个天气查询助手，能够调用工具来查询天气信息。
        你需要仔细分析用户的问题，从问题中提取工具需要的参数（城市、日期等），然后调用对应的查询工具。
        如果用户提供的信息不足以提取到调用工具的所有必要参数，则向用户追问，以获取该信息。不能自己编撰参数。
        注意：
        - 用户可能使用相对时间，如"明天"、"后天"、"今天"、"未来3天"等，请根据当前日期{current_date} 转换为具体日期（YYYY-MM-DD格式）。
        - 如果用户只给了城市没给日期，需要追问日期。
        - 如果用户只给了日期没给城市，需要追问城市。
        - **【重要】职责边界**：你只负责天气查询类请求。
          - 对话历史中若出现票务、景点推荐、租车等其他类型请求，由其他助手负责，不要代答或代为处理；
          - 只围绕最后一条 User 消息中的天气内容作答。
        - 【重要】当你需要向用户追问时，回复必须以"[追问]"开头，例如："[追问]请问您想查询哪一天的天气？"
        
        查询到结果后，请用清晰的中文格式化输出天气信息，包括城市、日期、天气状况、温度、湿度、风向、降水量等。
        如果未查到数据，请回复"未找到相关天气数据，请确认或修改查询条件。
        """

        # create_agent 内部自动构建 LangGraph 状态图，包含"调用 LLM → 检查工具调用 → 执行工具 → 循环"的完整流程
        agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)

        # 执行 Agent，传入消息列表
        # create_agent 返回的 CompiledStateGraph 使用 messages 列表作为输入输出格式
        result = await agent.ainvoke({"messages": [{"role": "user", "content": conversation}]})

        # 提取最后一条消息的内容作为最终回复
        return {"status": "success", "message": result["messages"][-1].content}

    except Exception as e:
        # MCP 连接失败、工具调用异常等情况的捕获
        logger.error(f"天气 MCP 查询出错：{str(e)}")  # 记录错误日志
        return {"status": "error", "message": f"天气 MCP 查询出错：{str(e)}"}  # 返回错误结果


# ==================== Agent Card（代理卡片） ====================
# Agent Card 是 A2A 协议中描述一个代理能力的元数据
# 它告诉主助手：我是谁、我能做什么、我运行在哪里、我有哪些技能
# 主助手通过这个卡片来决定是否将任务路由到这个代理
agent_card = AgentCard(
    name="WeatherQueryAssistant",  # 代理名称
    description="基于LangChain提供天气查询服务的助手",  # 代理描述
    url="http://localhost:5005",  # 代理的访问地址
    version="2.0.0",  # 版本号（升级为2.0.0，表示架构变更）
    capabilities={"streaming": True, "memory": True},  # 支持的能力：流式输出、记忆
    skills=[  # 技能列表：描述本代理具体能做什么
        AgentSkill(
            name="execute weather query",  # 技能名称
            description="执行天气查询，返回天气数据库结果，支持自然语言输入，支持单天和范围查询",  # 技能描述
            examples=["北京 2025-07-30 天气", "上海未来5天", "今天天气如何"]  # 使用示例
        )
    ]
)


# ==================== 天气查询服务器类 ====================
class WeatherQueryServer(A2AServer):
    """
    需求：实现天气查询 A2A 服务器，接收主助手任务并返回天气查询结果。

    思路步骤：
        1. 继承 A2AServer 基类，通过 Agent Card 注册自身能力（名称、描述、技能、端口）。
        2. 在 handle_task 中接收 A2A 任务，提取用户查询文本。
        3. 调用 query_weather 异步函数执行 MCP 工具查询（内部使用 LangChain Agent）。
        4. 根据返回结果设置任务状态：COMPLETED（成功） / INPUT_REQUIRED（追问） / FAILED（失败）。
        5. 将任务对象返回给主助手，完成任务生命周期。

    任务状态说明：
    - COMPLETED: 任务成功完成，结果放在 task.artifacts 中
    - FAILED: 任务失败，错误信息放在 task.status.message 中
    - INPUT_REQUIRED: 需要用户补充输入，追问信息放在 task.status.message 中
    """

    def __init__(self):
        """
        需求：初始化天气查询服务器，注册 Agent Card 到 A2A 框架。

        思路步骤：
            1. 调用父类 A2AServer 的 __init__，把 agent_card 传入完成注册。
            2. 注册完成后，A2A 框架就能根据 Agent Card 路由任务到本服务器。
        """
        super().__init__(agent_card=agent_card)  # 调用父类初始化，注册 Agent Card

    def handle_task(self, task):
        """
        需求：处理来自 A2A 客户端（主助手）的天气查询任务，是本服务器的核心方法。

        思路步骤：
            1. 从任务消息（task.message）中提取用户查询文本 conversation。
            2. 调用 asyncio.run(query_weather(conversation)) 同步执行异步查询函数。
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
            # ========== 步骤2：调用 Agent 查询 ==========
            weather_result = asyncio.run(query_weather(conversation))
            logger.info(f"MCP 查询返回: {weather_result}")

            # ========== 步骤3：根据结果设置任务状态 ==========
            if weather_result.get("status") == "success":
                result_text = weather_result.get("message", "")  # 取出 LLM 生成的回复文本

                # 检查是否是追问消息（LLM 发现信息不足时会追问）
                # 判断优先级：[追问] 前缀（Prompt 约定）> 关键词兜底
                is_followup = (
                    result_text.strip().startswith("[追问]")  # Prompt 约定的追问标记
                    or "您想查询" in result_text
                    or "哪一天" in result_text
                    or "哪个城市" in result_text
                )
                if is_followup:
                    # 去掉 [追问] 前缀后返回给用户，设为 INPUT_REQUIRED
                    clean_text = result_text.replace("[追问]", "").strip()
                    task.status = TaskStatus(  # 创建追问状态
                        state=TaskState.INPUT_REQUIRED,  # 状态：需要用户输入
                        message={"role": "agent", "content": {"text": clean_text}}  # 追问内容（已去前缀）
                    )
                else:
                    # 查询成功，将结果放入任务产物
                    task.artifacts = [{"parts": [{"type": "text", "text": result_text}]}]  # 成功结果放入任务产物
                    task.status = TaskStatus(state=TaskState.COMPLETED)  # 设为任务完成

            elif weather_result.get("status") == "error":
                # MCP 查询出错：设置为失败状态
                task.status = TaskStatus(  # 创建失败状态
                    state=TaskState.FAILED,  # 状态：失败
                    message={"role": "agent", "content": {"text": weather_result.get("message", "查询失败，请重试。")}}  # 错误信息
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
    # 创建天气查询服务器实例
    weather_server = WeatherQueryServer()

    # 打印服务器信息，方便确认启动状态
    print("\n=== 服务器信息 ===")  # 输出分隔标题
    print(f"名称: {weather_server.agent_card.name}")  # 打印代理名称
    print(f"描述: {weather_server.agent_card.description}")  # 打印代理描述
    print("\n技能:")  # 打印技能标题
    for skill in weather_server.agent_card.skills:  # 遍历所有技能
        print(f"- {skill.name}: {skill.description}")  # 打印每个技能的名称和描述

    # 启动 A2A 服务器，监听地址和端口从 Config 读取
    # 主助手会通过 http://localhost:5005 连接本服务器
    run_server(weather_server, host=conf.a2a_host, port=conf.weather_port)  # 启动服务器（阻塞运行）
