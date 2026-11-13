"""
需求：实现SmartVoyage智能旅游助手的命令行交互入口，支持多轮对话、追问机制、代理卡片查看和记忆管理。
思路步骤：
1. 导入必要的模块和库（异步IO、JSON、时间、A2A协议、LangChain等）
2. 初始化全局变量（对话历史、代理网络、LLM实例、记忆管理器等）
3. 实现系统初始化函数（创建代理网络、配置LLM、初始化记忆管理器）
4. 实现意图识别函数（使用LLM识别用户意图，返回意图列表和改写查询）
5. 实现用户输入处理函数（根据意图路由到相应代理或直接生成内容，汇总回复）
6. 实现代理卡片显示函数（展示各代理的能力、地址和在线状态）
7. 实现记忆状态显示函数（展示短期对话、用户偏好、任务上下文等）
8. 实现主函数（初始化系统 → 显示代理卡片 → 进入交互循环 → 处理用户命令）
9. 处理异常情况（JSON解析错误、网络错误、服务不可用等）
"""
import asyncio  # 异步IO库，用于调用子代理的异步接口
import os  # 操作系统接口，用于拼接文件路径
import sys  # 系统路径操作，用于把项目上级目录加入 sys.path
import json  # JSON处理，用于解析大模型返回的意图识别结果
import uuid  # 唯一标识符生成，用于给每个 A2A 任务分配唯一ID
from datetime import datetime  # 时间处理，用于获取当前日期（注入到意图识别 prompt 中）
import pytz  # 时区库，用于将时间转换到 Asia/Shanghai 时区
import re  # 正则表达式，用于清理大模型返回的 JSON（去掉代码块标记）
from python_a2a import AgentNetwork, TextContent, Message, MessageRole, Task  # A2A 协议核心类
from langchain_openai import ChatOpenAI  # LangChain 的大模型接口，兼容 OpenAI API 格式

# 把项目代码的上一级目录（04-代码）加入模块搜索路径，保证 SmartVoyage 包可被正确导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from SmartVoyage.config import Config  # 配置模块（模型地址、API Key、意图映射等）
from SmartVoyage.create_logger import logger  # 日志模块，用于记录运行日志
from SmartVoyage.main_prompts import SmartVoyagePrompts  # Prompt 模板管理
from SmartVoyage.memory import ConversationMemory, TICKET_INTENTS  # 记忆管理（短期记忆、用户偏好、任务上下文）+ 票务类意图白名单

conf = Config()  # 全局配置实例，包含模型参数、意图映射等

# 初始化全局变量，用于模拟会话状态
messages = []  # 存储对话历史消息列表，每个元素为字典{"role": "user/assistant", "content": "消息内容"}
agent_network = None  # 代理网络实例，管理所有子代理的注册和通信
llm = None  # 大语言模型实例，用于意图识别和景点生成
agent_urls = {}  # 存储代理的URL信息字典，格式：{"代理名": "http://地址:端口"}
conversation_history = ""  # 存储整个对话历史字符串，用于调试和展示
memory = None  # 对话记忆管理器实例，管理短期记忆、用户偏好、任务上下文等


# 初始化代理网络和相关组件
def initialize_system():
    """
    需求：初始化系统所有核心组件，包括代理网络、大模型连接和记忆管理器。
    思路步骤：
    1. 声明全局变量，确保修改对全局生效
    2. 配置三个子代理的 URL 地址（天气5005、票务5006、行程5007）
    3. 创建 AgentNetwork 实例，向其中注册三个子代理
    4. 初始化 LangChain ChatOpenAI 大模型连接（使用配置中的模型参数）
    5. 初始化对话历史字符串和记忆管理器（短期记忆上限 10 条）
    """
    global agent_network, llm, agent_urls, conversation_history, memory
    agent_urls = {
        "WeatherQueryAssistant": "http://localhost:5005",  # 天气代理URL
        "TicketAssistant": "http://localhost:5006",        # 火车票代理URL
        "TripAssistant": "http://localhost:5007"           # 行程管家代理URL
    }
    network = AgentNetwork(name="旅行助手网络")
    network.add("WeatherQueryAssistant", "http://localhost:5005")
    network.add("TicketAssistant", "http://localhost:5006")
    network.add("TripAssistant", "http://localhost:5007")
    agent_network = network

    llm = ChatOpenAI(
        model=conf.model_name,
        api_key=conf.api_key,
        base_url=conf.base_url,
        temperature=0.1,
        extra_body={"enable_thinking": False}
    )

    conversation_history = ""
    memory = ConversationMemory(username="cli_user", short_term_limit=10)  # CLI 单用户场景，使用固定用户名

# 意图识别agent
def intent_agent(user_input):
    """
    需求：分析用户输入，识别用户意图、改写查询内容、判断是否需要追问。
    思路步骤：
    1. 声明使用全局的 llm 和 memory 实例
    2. 用 intent_prompt 模板 + llm 组装处理链
    3. 获取当前日期（用于解析"明天""后天"等相对时间）
    4. 将对话历史、用户输入、日期、用户偏好、任务上下文注入模板，调用大模型
    5. 清理大模型返回的 JSON（去掉 ```json 代码块标记）
    6. 解析 JSON，提取意图列表、改写查询字典、追问消息
    7. 返回三元组 (intents, user_queries, follow_up_message)
    """
    global llm, memory

    chain = SmartVoyagePrompts.intent_prompt() | llm

    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    intent_response = chain.invoke(
        {"conversation_history": memory.get_short_term_text(),
         "query": user_input,
         "current_date": current_date,
         "user_profile": memory.get_profile_text(),
         "long_term_memory": memory.get_long_term_text(user_input),
         "task_context": json.dumps(memory.current_task, ensure_ascii=False)}
    ).content.strip()
    logger.info(f"意图识别原始响应: {intent_response}")

    intent_response = re.sub(r'^```json\s*|\s*```$', '', intent_response).strip()
    logger.info(f"清理后响应: {intent_response}")
    intent_output = json.loads(intent_response)
    intents = intent_output.get("intents", [])
    user_queries = intent_output.get("user_queries", {})
    follow_up_message = intent_output.get("follow_up_message", "")
    logger.info(f"intents: {intents}||user_queries: {user_queries}||follow_up_message: {follow_up_message} ")

    return intents, user_queries, follow_up_message

# 处理用户输入的核心函数
def process_user_input(prompt):
    """
    需求：处理用户输入的核心函数，完成意图识别、代理路由、结果汇总和回复输出。
    思路步骤：
    1. 将用户消息保存到记忆管理器和对话历史中
    2. 调用 intent_agent 进行意图识别
    3. 处理特殊情况：超出范围（out_of_scope）或需要追问时直接使用追问消息
    4. 对每个识别到的意图，路由到对应处理逻辑：
       - 景点推荐：直接用大模型生成
       - 其他意图：通过 A2A 协议调用子代理，获取结果后用对应 prompt 总结
    5. 将多个意图的回复拼接为最终回复
    6. 将助手回复保存到记忆中
    7. 打印回复到控制台
    8. 异常处理：JSON 解析失败或其他错误时，输出友好错误提示
    """
    global messages, conversation_history, llm, memory
    # 添加用户消息到记忆
    memory.add_message("user", prompt)
    # 同步到完整历史（用于调试和展示）
    messages.append({"role": "user", "content": prompt})
    conversation_history += f"\nUser: {prompt}"

    print("正在分析您的意图...")
    try:
        # 意图识别过程
        intents, user_queries, follow_up_message = intent_agent(prompt)

        # 根据意图输出生成响应
        if "out_of_scope" in intents:
            response = follow_up_message
        elif follow_up_message != "":
            response = follow_up_message
        else:
            responses = []
            routed_agents = []
            for intent in intents:
                logger.info(f"处理意图：{intent}")
                agent_name = conf.intent.get(intent)

                if agent_name:
                    # 所有意图（含 attraction 景点推荐）统一走 A2A 子代理
                    # attraction → TripAssistant → MCP search_travel_guides（博查联网搜索）
                    query_str = user_queries.get(intent, prompt)  # 改写缺失/非法时回退用户原话，恒为 str
                    logger.info(f"{agent_name} 查询：{query_str}")

                    # 提取关键实体到记忆
                    if intent in TICKET_INTENTS:  # 票务/行程类意图白名单（与 memory.extract_long_term_from_query 共用常量）
                        memory.extract_entities(intent, query_str)
                        # 更新任务上下文
                        memory.update_task_context({"type": intent, "query": query_str})

                    agent = agent_network.get_agent(agent_name)
                    # 构建上下文：短期记忆 + 新查询
                    chat_history = memory.get_short_term_text() + f'\nUser: {query_str}'
                    message = Message(content=TextContent(text=chat_history), role=MessageRole.USER)
                    task = Task(id="task-" + str(uuid.uuid4()), message=message.to_dict())
                    raw_response = asyncio.run(agent.send_task_async(task))
                    logger.info(f"{agent_name} 原始响应: {raw_response}")

                    if raw_response.status.state == 'completed':
                        agent_result = raw_response.artifacts[0]['parts'][0]['text']
                    else:
                        agent_result = raw_response.status.message['content']['text']

                    # 根据代理类型总结响应
                    if agent_name == "WeatherQueryAssistant":
                        chain = SmartVoyagePrompts.summarize_weather_prompt() | llm
                        final_response = chain.invoke({"query": query_str, "raw_response": agent_result}).content.strip()
                    elif agent_name == "TicketAssistant":
                        chain = SmartVoyagePrompts.summarize_ticket_prompt() | llm
                        final_response = chain.invoke({"query": query_str, "raw_response": agent_result}).content.strip()
                    else:
                        final_response = agent_result

                    responses.append(final_response)
                    routed_agents.append(agent_name)
                else:
                    responses.append("暂不支持此意图。")

            response = "\n\n".join(responses)
            if routed_agents:
                logger.info(f"路由到代理：{routed_agents}")

        # 添加响应到记忆
        memory.add_message("assistant", response)
        conversation_history += f"\nAssistant: {response}"
        messages.append({"role": "assistant", "content": response})

        # 输出助手响应
        print(f"\n助手回复：\n{response}\n")

    except json.JSONDecodeError as json_err:
        logger.error(f"意图识别JSON解析失败")
        error_message = f"意图识别JSON解析失败：{str(json_err)}。请重试。"
        print(f"\n助手回复：\n{error_message}\n")
        memory.add_message("assistant", error_message)
        messages.append({"role": "assistant", "content": error_message})
    except Exception as e:
        logger.error(f"处理异常: {str(e)}")
        error_message = f"处理失败：{str(e)}。请重试。"
        print(f"\n助手回复：\n{error_message}\n")
        memory.add_message("assistant", error_message)
        messages.append({"role": "assistant", "content": error_message})


# 显示代理卡片信息
def display_agent_cards():
    """
    需求：在控制台打印所有子代理的卡片信息，包括技能、描述、地址和在线状态。
    思路步骤：
    1. 打印标题行
    2. 遍历代理网络中注册的所有 agent 名称
    3. 获取每个 agent 的卡片信息（技能列表、描述）
    4. 从 agent_urls 字典中获取代理地址
    5. 格式化打印每个代理的名称、技能、描述、地址和状态
    """
    print("\n🛠️ Agent Cards:")
    for agent_name in agent_network.agents.keys():
        agent_card = agent_network.get_agent_card(agent_name)
        agent_url = agent_urls.get(agent_name, "未知地址")
        print(f"\n--- Agent: {agent_name} ---")
        print(f"技能: {agent_card.skills}")
        print(f"描述: {agent_card.description}")
        print(f"地址: {agent_url}")
        print(f"状态: 在线")


def display_memory():
    """
    需求：在控制台打印当前记忆状态的详细信息，用于调试和查看系统状态。
    思路步骤：
    1. 打印标题行和短期对话统计（当前条数/上限）
    2. 显示最近 5 条短期对话消息，角色标签翻译为中文
    3. 显示用户偏好字典，为空时显示"无"
    4. 显示当前任务上下文，为空时显示"无"
    5. 显示最近 5 条实体查询历史，为空时显示"无"
    """
    print("\n🧠 Memory State:")
    print(f"--- 短期对话 ({len(memory.short_term_messages)}/{memory.short_term_limit}) ---")
    for msg in memory.short_term_messages[-5:]:
        role_label = "用户" if msg["role"] == "user" else "助手"
        print(f"  [{msg['timestamp']}] {role_label}: {msg['content']}")
    print(f"\n--- 用户偏好 ---")
    if memory.user_profile:
        for k, v in memory.user_profile.items():
            print(f"  {k}: {v}")
    else:
        print("  无")
    print(f"\n--- 当前任务上下文 ---")
    if memory.current_task:
        for k, v in memory.current_task.items():
            print(f"  {k}: {v}")
    else:
        print("  无")
    print(f"\n--- 查询历史 (最近5条) ---")
    for entity in memory.entity_history[-5:]:
        print(f"  [{entity['timestamp']}] {entity['type']}: {entity['query']}")
    if not memory.entity_history:
        print("  无")

# 主函数：脚本入口
# 初始化系统并进入交互循环
if __name__ == "__main__":
    # 初始化系统
    initialize_system()
    print("🤖 基于A2A的SmartVoyage旅行智能助手")
    print("欢迎体验智能对话！输入问题，按回车提交；")
    print("输入'quit'退出；输入'cards'查看代理卡片；输入'memory'查看记忆状态；输入'clear'清空记忆。")

    # 显示初始代理卡片
    display_agent_cards()

    # 交互循环
    while True:
        user_input = input("\n请输入您的问题: ").strip()
        if user_input.lower() == 'quit':
            print("感谢使用SmartVoyage！再见！")
            break
        elif user_input.lower() == 'cards':
            display_agent_cards()
            continue
        elif user_input.lower() == 'memory':
            display_memory()
            continue
        elif user_input.lower() == 'clear':
            memory.clear()
            conversation_history = ""
            messages.clear()
            print("记忆已清空。")
            continue
        elif not user_input:
            continue
        else:
            process_user_input(user_input)

    # 脚本结束时打印页脚信息
    print("\n---")
    print("Powered by 黑马程序员 | 基于Agent2Agent的旅行助手系统 v2.0")
