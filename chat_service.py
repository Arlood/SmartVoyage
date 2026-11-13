"""
需求：SmartVoyage核心对话服务，供CLI和API共同调用。
思路步骤：
1. 意图识别（intent_agent）：用 flash 模型分析用户输入，识别出用户想做什么（查天气/查机票/查景点等）
2. 启发式路由（_should_skip_planning）：快速判断任务复杂度
   - 单意图：跳过规划，直接执行（省去 1 次 LLM 调用）
   - 多意图：调用 planning_agent 判断意图间依赖（如条件依赖）并生成执行计划
3. ReAct循环（react_loop）：针对复杂任务，逐步执行每个步骤
   - Action（行动）：直接调用对应的 agent 执行查询（跳过 Thought 推理，省去每步 1 次 LLM 调用）
   - Observation（观察）：记录执行结果，供后续步骤参考
4. 结果汇总：所有步骤完成后，用 flash 模型生成一条连贯的最终回复

架构说明：
    本模块实现了智能旅游助手的核心对话流程，采用优化的 Planning + ReAct 架构。
    双模型策略：self.llm（flash，负责意图识别/结果总结等轻量任务）
               self.llm_premium（premium，负责规划等复杂任务）
    所有意图（含景点推荐）均通过 A2A 子代理路由执行，不直接调用 LLM 生成。

整体流程：
    用户输入 → 意图识别 → [跳过规划 / 调用planning_agent] → [直接执行 / ReAct循环] → 返回回复

A2A架构说明：
    Agent2Agent (A2A) 是一种代理间通信协议。本系统通过 AgentNetwork 管理多个子代理：
    - WeatherQueryAssistant（天气查询代理）：运行在 localhost:5005
    - TicketAssistant（火车票代理）：运行在 localhost:5006
    - TripAssistant（行程管家代理）：运行在 localhost:5007
    主助手通过 A2A 协议向子代理发送任务请求，获取查询结果后整合回复用户。
"""

# ==================== 导入依赖 ====================
import asyncio  # 异步IO库，用于并发调用多个 agent（异步网络请求）
import json  # JSON处理，用于解析大模型的JSON输出和序列化数据
import os  # 环境变量读取
import uuid  # 唯一标识符生成，用于给每个任务分配唯一ID
from datetime import datetime  # 时间处理，用于获取当前日期和时间戳
import pytz  # 时区库，用于将时间转换到 Asia/Shanghai 时区
import re  # 正则表达式，用于清理大模型返回的JSON（去掉代码块标记）
import mysql.connector  # MySQL 数据库驱动，用于记忆持久化

# python_a2a 是 A2A 协议的 Python 客户端库
from python_a2a import AgentNetwork, TextContent, Message, MessageRole, Task
from python_a2a.client import A2AClient
# AgentNetwork: 代理网络管理器，负责注册和获取子代理
# A2AClient: A2A 客户端，支持自定义超时
# TextContent: 消息文本内容包装
# Message: A2A 消息对象，包含角色和内容
# MessageRole: 消息角色枚举（USER/ASSISTANT）
# Task: A2A 任务对象，用于向子代理发送请求

from langchain_openai import ChatOpenAI  # LangChain 的大模型接口，兼容 OpenAI API 格式

# 导入项目内部模块
from SmartVoyage.config import Config  # 配置模块（模型地址、API Key、意图映射等）
from SmartVoyage.create_logger import logger  # 日志模块，用于记录运行日志
from SmartVoyage.main_prompts import SmartVoyagePrompts  # Prompt 模板管理
from SmartVoyage.memory import ConversationMemory, TICKET_INTENTS  # 记忆管理（短期记忆、用户偏好、任务上下文）+ 票务类意图白名单

conf = Config()  # 全局配置实例，包含模型参数、意图映射等


class ChatService:
    """
    需求：SmartVoyage 核心对话服务类，负责处理用户的所有输入并生成回复。
    思路步骤：
    1. 初始化代理网络、双模型（flash/premium）、记忆管理器等核心组件
    2. 接收用户输入后，先用 flash 模型做意图识别
    3. 通过启发式规则或 premium 模型判断是否需要多步规划
    4. 简单任务直接路由到子代理执行，复杂任务进入 ReAct 循环逐步执行
    5. 最终用 flash 模型汇总各步骤结果，返回连贯回复

    使用示例：
        service = ChatService()          # 创建服务实例
        response = await service.chat("北京明天天气怎么样？")  # 发送用户输入
        print(response)                  # 打印助手回复
    """

    # ==================== ① 初始化 ====================

    def __init__(self, username: str = "default"):
        """
        需求：初始化 ChatService 实例，完成代理网络、大模型、记忆管理器等核心组件的初始化。
        思路步骤：
        1. 保存用户名，用于记忆隔离
        2. 配置 A2A 代理网络，注册天气/票务/行程三个子代理（各 120 秒超时）
        3. 初始化双模型连接：flash 模型（轻量任务）和 premium 模型（复杂任务）
        4. 初始化记忆管理器（短期记忆上限 10 条）和对话历史缓存
        5. 初始化数据库连接，从数据库恢复持久化记忆数据
        """
        self.username = username  # 当前用户名，传递给 memory 实现数据隔离
        # ========== 1. 配置 A2A 代理网络 ==========
        # 记录每个子代理的访问地址（从 Config 读取，Docker 环境中通过环境变量覆盖）
        weather_url = conf.weather_a2a_url
        ticket_url = conf.ticket_a2a_url
        trip_url = conf.trip_a2a_url
        self.agent_urls = {
            "WeatherQueryAssistant": weather_url,  # 天气查询代理的地址
            "TicketAssistant": ticket_url,         # 票务代理的地址
            "TripAssistant": trip_url              # 行程管家代理的地址
        }
        # 创建代理网络实例，给它起个名字
        self.agent_network = AgentNetwork(name="旅行助手网络")
        # 向网络中注册子代理，使用自定义超时（300秒），避免子代理 LLM+MCP 调用链超时
        self.agent_network.add("WeatherQueryAssistant", A2AClient(weather_url, timeout=300))
        self.agent_network.add("TicketAssistant", A2AClient(ticket_url, timeout=300))
        self.agent_network.add("TripAssistant", A2AClient(trip_url, timeout=300))

        # ========== 2. 初始化大模型连接（模型分级） ==========
        # 使用 LangChain 的 ChatOpenAI 接口连接大模型
        # 模型分级策略：
        #   - flash（快速模型）：意图识别、结果总结等简单任务，追求响应速度
        #   - premium（高级模型）：规划等复杂任务，需要较强推理能力
        self.llm = ChatOpenAI(
            model=conf.model_flash,    # flash 模型：意图识别、结果总结（默认实例）
            api_key=conf.api_key,      # API 密钥
            base_url=conf.base_url,    # API 基础地址
            temperature=0.1,           # 温度参数：越小输出越确定、越稳定（0.1表示比较严谨）
            extra_body={"enable_thinking": False}  # 禁用思考模式，避免流式输出空chunk
        )
        self.llm_premium = ChatOpenAI(
            model=conf.model_premium,  # premium 模型：复杂规划
            api_key=conf.api_key,
            base_url=conf.base_url,
            temperature=0.1,
            extra_body={"enable_thinking": False}
        )

        # ========== 3. 初始化记忆和对话状态 ==========
        self.memory = ConversationMemory(username=username, short_term_limit=10)  # 短期记忆最多保留10条对话，按用户名隔离
        self.messages = []           # 对话消息列表，格式：[{"role": "user/assistant", "content": "..."}]

        # ========== 4. 初始化数据库连接，加载持久化记忆 ==========
        self._init_db_and_load_memory()

    def _init_db_and_load_memory(self):
        """
        需求：初始化 MySQL 数据库连接，并从数据库加载持久化记忆数据到内存。
        思路步骤：
        1. 使用配置中的数据库参数创建 MySQL 连接
        2. 将数据库连接注入到 memory 实例，供后续持久化操作使用
        3. 依次从数据库加载用户偏好、查询历史、短期对话、长期记忆
        4. 将加载的短期消息同步到 self.messages 列表，保持对话上下文连续
        5. 如果数据库连接失败，降级为无持久化模式（仅内存记忆）
        """
        try:
            db_conn = mysql.connector.connect(
                host=conf.host,
                user=conf.user,
                password=conf.password,
                database=conf.database,
                charset='utf8mb4'            # Windows 默认编码可能是 GBK，显式指定 UTF-8 防止中文乱码
            )
            self.memory.set_db_connection(db_conn)
            logger.info("数据库连接初始化成功")

            # 从数据库恢复记忆数据
            self.memory.load_profile_from_db()
            self.memory.load_entities_from_db()
            self.memory.load_messages_from_db()
            self.memory.load_long_term_from_db()

            # 将加载的短期消息同步到 messages 列表
            for msg in self.memory.short_term_messages:
                self.messages.append({"role": msg["role"], "content": msg["content"]})

            logger.info(f"记忆加载完成: 偏好={len(self.memory.user_profile)}项, "
                        f"历史={len(self.memory.entity_history)}条, 对话={len(self.memory.short_term_messages)}条, "
                        f"长期记忆={len(self.memory.long_term_memories)}条")
        except Exception as e:
            logger.warning(f"数据库连接初始化失败: {e}")
            self.memory.set_db_connection(None)

    # ==================== ② 对话主入口 ====================

    async def chat(self, user_input: str) -> str:
        """
        需求：处理用户输入的主方法，协调整个对话流程并返回助手回复。
        思路步骤：
        1. 将用户消息保存到记忆和对话历史中
        2. 调用 intent_agent 进行意图识别，同时沉淀长期记忆
        3. 处理特殊情况：超出范围（out_of_scope）或需要追问时直接返回追问消息
        4. 任务规划：先检查开关，再用启发式判断，最后才调用 planning_agent
        5. 执行：复杂任务进入 react_loop，简单任务直接对每个意图调用 _call_agent_intent
        6. 将助手回复保存到记忆中
        7. 返回最终回复文本
        """
        # 将用户消息保存到记忆中（短期记忆、对话历史等）
        self.memory.add_message("user", user_input)
        self.messages.append({"role": "user", "content": user_input})

        try:
            # ========== 步骤1：意图识别 ==========
            intents, user_queries, follow_up_message = self.intent_agent(user_input)

            # 沉淀长期记忆：从用户输入中自动抽取可跨会话复用的偏好线索
            # （如"常去北京"→pref_city，"要二等座"→pref_seat），供后续对话持续使用
            for intent in intents:
                self.memory.extract_long_term_from_query(intent, user_input)

            # ========== 步骤2：处理特殊情况 ==========
            if "out_of_scope" in intents:
                # 用户输入超出系统能力范围，返回追问消息
                response = follow_up_message
            elif follow_up_message != "":
                # 意图不明确，需要追问用户
                response = follow_up_message
            else:
                # ========== 步骤3：任务规划 ==========
                # 复杂任务开关：若关闭，则一律跳过规划、直接串行执行所有意图（更快）
                if not conf.enable_complex_task:
                    plan = {"need_plan": False, "reason": "复杂任务开关已关闭，直接执行", "steps": []}
                    logger.info(f"跳过规划(开关关闭): {plan['reason']}")
                # 启发式判断：单意图跳过 planning_agent，省去1次LLM调用；多意图交给 planning 终判
                elif self._should_skip_planning(intents):
                    plan = {"need_plan": False, "reason": "启发式判断：任务简单，可直接执行", "steps": []}
                    logger.info(f"跳过规划: {plan['reason']}")
                else:
                    plan = self.planning_agent(intents, user_queries)
                need_plan = plan.get("need_plan", False)
                logger.info(f"规划结果: need_plan={need_plan}, reason={plan.get('reason', '')}")

                if need_plan:
                    # ========== 复杂任务：进入 ReAct 循环 ==========
                    # 获取计划步骤，逐步执行
                    steps = plan.get("steps", [])
                    response = await self.react_loop(steps, user_queries)
                else:
                    # ========== 简单任务：直接执行 ==========
                    # 对每个识别到的意图，直接调用对应的处理逻辑
                    responses = []    # 收集每个意图的回复
                    routed_agents = []  # 记录路由到了哪些 agent

                    for intent in intents:
                        logger.info(f"处理意图：{intent}")
                        # 获取改写后的查询内容
                        query_str = user_queries.get(intent, user_input)
                        # 统一调用 agent 处理
                        result = await self._call_agent_intent(intent, query_str)
                        responses.append(result)
                        # 记录路由到的 agent（所有意图均走 agent，含 attraction → TripAssistant）
                        if conf.intent.get(intent):
                            routed_agents.append(conf.intent[intent])

                    # 将多个意图的回复用空行拼接成最终回复
                    response = "\n\n".join(responses)
                    if routed_agents:
                        logger.info(f"路由到代理：{routed_agents}")

            # 将助手回复也保存到记忆中
            self.memory.add_message("assistant", response)
            self.messages.append({"role": "assistant", "content": response})
            return response

        except json.JSONDecodeError as e:
            # 大模型返回的不是有效 JSON（虽然概率低，但偶尔会发生）
            logger.error(f"意图识别JSON解析失败")
            error_message = f"意图识别JSON解析失败：{str(e)}。请重试。"
            self.memory.add_message("assistant", error_message)
            self.messages.append({"role": "assistant", "content": error_message})
            return error_message
        except Exception as e:
            # 捕获其他所有异常（网络错误、服务不可用等）
            logger.error(f"处理异常: {str(e)}")
            error_message = f"处理失败：{str(e)}。请重试。"
            self.memory.add_message("assistant", error_message)
            self.messages.append({"role": "assistant", "content": error_message})
            return error_message

    async def chat_stream(self, user_input: str):
        """
        需求：流式版本的 chat 方法，流程与非流式相同，但最终回复通过 yield 逐字返回给前端。
        思路步骤：
        1. 将用户消息保存到记忆和对话历史中
        2. 调用 intent_agent 进行意图识别，同时沉淀长期记忆
        3. 处理特殊情况：超出范围或需要追问时直接 yield 追问消息
        4. 任务规划：检查开关 → 启发式判断 → 调用 planning_agent
        5. 复杂任务进入 _react_loop_stream 流式执行，简单任务对每个意图流式调用
        6. 收集所有 yield 的片段，拼接为完整回复后保存到记忆中
        """
        self.memory.add_message("user", user_input)
        self.messages.append({"role": "user", "content": user_input})

        try:
            intents, user_queries, follow_up_message = self.intent_agent(user_input)

            # 沉淀长期记忆：从用户输入中自动抽取可跨会话复用的偏好线索
            for intent in intents:
                self.memory.extract_long_term_from_query(intent, user_input)

            if "out_of_scope" in intents:
                response = follow_up_message
                yield response
            elif follow_up_message != "":
                response = follow_up_message
                yield response
            else:
                if not conf.enable_complex_task:
                    plan = {"need_plan": False, "reason": "复杂任务开关已关闭，直接执行", "steps": []}
                    logger.info(f"跳过规划(开关关闭,stream): {plan['reason']}")
                elif self._should_skip_planning(intents):
                    plan = {"need_plan": False, "reason": "启发式判断：任务简单，可直接执行", "steps": []}
                    logger.info(f"跳过规划(stream): {plan['reason']}")
                else:
                    plan = self.planning_agent(intents, user_queries)
                need_plan = plan.get("need_plan", False)
                logger.info(f"规划结果: need_plan={need_plan}, reason={plan.get('reason', '')}")

                if need_plan:
                    steps = plan.get("steps", [])
                    parts = []
                    async for chunk in self._react_loop_stream(steps, user_queries):
                        parts.append(chunk)
                        yield chunk
                    response = "".join(str(p) for p in parts if isinstance(p, str))
                else:
                    responses = []
                    for intent in intents:
                        logger.info(f"处理意图：{intent}")
                        query_str = user_input
                        parts = []
                        async for chunk in self._call_agent_intent_stream(intent, query_str):
                            parts.append(chunk)
                            yield chunk
                        responses.append("".join(str(p) for p in parts if isinstance(p, str)))

                    response = "\n\n".join(responses)

            self.memory.add_message("assistant", response)
            self.messages.append({"role": "assistant", "content": response})

        except json.JSONDecodeError as e:
            logger.error(f"意图识别JSON解析失败")
            error_message = f"意图识别JSON解析失败：{str(e)}。请重试。"
            self.memory.add_message("assistant", error_message)
            self.messages.append({"role": "assistant", "content": error_message})
            yield error_message
        except Exception as e:
            logger.error(f"处理异常: {str(e)}")
            error_message = f"处理失败：{str(e)}。请重试。"
            self.memory.add_message("assistant", error_message)
            self.messages.append({"role": "assistant", "content": error_message})
            yield error_message

    # ==================== ③ 意图识别与任务规划 ====================

    def intent_agent(self, user_input: str):
        """
        需求：用 flash 模型分析用户输入，识别意图、改写查询、判断是否需要追问。
        思路步骤：
        1. 用 intent_prompt 模板 + flash 模型组装处理链
        2. 获取当前日期（用于解析"明天""后天"等相对时间）
        3. 将对话历史、用户输入、日期、偏好、长期记忆、任务上下文注入模板，调用大模型
        4. 清理大模型返回的 JSON（去掉 ```json 代码块标记）
        5. 解析 JSON，提取意图列表、改写后的查询字典、追问消息
        6. 返回三元组 (intents, user_queries, follow_up_message)

        支持的意图类型：
            - weather: 天气查询
            - train: 高铁/火车票查询
            - car_rental: 租车查询
            - tour_group: 旅游团查询
            - insurance: 保险查询
            - trip_order: 行程预订
            - attraction: 景点推荐
            - out_of_scope: 超出系统能力范围
        """
        # 组装 Prompt 模板 + 大模型，形成完整的处理链
        chain = SmartVoyagePrompts.intent_prompt() | self.llm

        # 获取当前日期，注入到 prompt 中（用户可能说"明天"、"后天"等相对时间）
        current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')

        # 调用大模型，传入所有上下文信息
        intent_response = chain.invoke({
            "query": user_input,          # 用户本次输入
            "current_date": current_date, # 当前日期
            "user_profile": self.memory.get_profile_text(),  # 用户偏好（如"二等座"、"经济舱"）
            "conversation_history": self.memory.get_short_term_text(),  # 短期对话历史
            "long_term_memory": self.memory.get_long_term_text(user_input),  # 长期记忆（按需检索注入）
            "task_context": json.dumps(self.memory.current_task, ensure_ascii=False)  # 当前任务上下文
        }).content.strip()  # .content 获取文本内容，.strip() 去掉首尾空白
        logger.info(f"意图识别原始响应: {intent_response}")

        # 大模型有时会返回 ```json ... ``` 格式的代码块，需要用正则去掉
        intent_response = re.sub(r'^```json\s*|\s*```$', '', intent_response).strip()
        logger.info(f"清理后响应: {intent_response}")

        # 将 JSON 字符串解析为 Python 字典
        intent_output = json.loads(intent_response)
        intents = intent_output.get("intents", [])  # 意图列表，如 ["weather", "flight"]
        user_queries = intent_output.get("user_queries", {})  # 改写后的查询字典
        follow_up_message = intent_output.get("follow_up_message", "")  # 追问消息
        logger.info(f"intents: {intents}||user_queries: {user_queries}||follow_up_message: {follow_up_message} ")

        return intents, user_queries, follow_up_message

    def _should_skip_planning(self, intents: list) -> bool:
        """
        需求：启发式判断是否可以跳过 planning_agent，直接执行任务，以省去 1 次 LLM 调用。
        思路步骤：
        1. 如果只有 1 个意图，直接返回 True（无需规划）
        2. 多意图一律返回 False：意图间可能存在条件/依赖关系（如"天气好才推荐景点"），
           启发式只看意图类型看不见依赖，交给 planning_agent 的 premium 模型终判
        """
        # 规则1：单意图直接跳过
        if len(intents) <= 1:
            return True

        # 规则2：多意图进入 planning_agent，由其判断 need_plan 与步骤依赖（depends_on）
        # 若 planning 判定无依赖（need_plan=false），仍回落到简单路径逐意图执行
        return False

    def planning_agent(self, intents: list, user_queries: dict) -> dict:
        """
        需求：用 premium 模型对复杂任务进行多步规划，生成带依赖关系的执行计划。
        思路步骤：
        1. 用 planning_prompt 模板 + premium 模型组装处理链（规划需要较强推理能力）
        2. 将对话历史、用户输入、意图列表、改写查询注入模板，调用大模型
        3. 清理返回的 JSON，解析为字典
        4. 返回规划结果：need_plan=true 时表示需要多步执行，steps 包含各步骤及依赖关系

        注意：在调用此方法前会先通过 _should_skip_planning 做启发式判断，
        只有复杂任务才会进入此方法，省去不必要的 LLM 调用。
        """
        chain = SmartVoyagePrompts.planning_prompt() | self.llm_premium  # 规划需要较强推理能力

        planning_response = chain.invoke({
            "conversation_history": self.memory.get_short_term_text(),
            "query": self.messages[-1]["content"] if self.messages else "",
            "intents": json.dumps(intents, ensure_ascii=False),
            "user_queries": json.dumps(user_queries, ensure_ascii=False)
        }).content.strip()
        logger.info(f"规划响应: {planning_response}")

        planning_response = re.sub(r'^```json\s*|\s*```$', '', planning_response).strip()
        plan = json.loads(planning_response)
        return plan

    # ==================== ④ Agent 执行层（非流式） ====================

    async def _call_agent_intent(self, intent: str, query_str: str) -> str:
        """
        需求：统一调用子代理的底层方法，根据意图类型路由到对应代理执行。
        思路步骤：
        1. 根据意图类型从配置中查找对应的 agent 名称
        2. 如果是票务查询类意图，先提取关键实体保存到记忆（支持多轮对话引用）
        3. 构建包含完整上下文的消息，通过 A2A 协议异步发送给子代理
        4. 解析子代理返回结果，根据代理类型用 flash 模型做适当总结
        5. 天气/火车票需要总结链处理，行程管家（含景点推荐、租车、保险等）直接返回格式化结果

        说明：所有意图（含 attraction 景点推荐）均通过 A2A 子代理路由执行。
        attraction 由 TripAssistant 处理，TripAssistant 内部调用 MCP 的
        search_travel_guides 工具（博查联网搜索），获取真实旅游攻略数据。

        这是一个私有方法（以 _ 开头），供 chat 方法和 execute_step 方法复用，
        避免代码重复，修改 agent 调用逻辑时只需改一处。
        """
        # 根据意图类型，从配置中查找对应的 agent 名称
        agent_name = conf.intent.get(intent)

        # ========== 情况1：需要调用子代理 ===========
        if agent_name:
            # 对于票务查询类意图，提取用户意图和改写后的query保存到记忆中，这样后续对话可以引用（多轮对话场景）
            if intent in TICKET_INTENTS:  # 票务/行程类意图白名单（与 memory.extract_long_term_from_query 共用常量）
                self.memory.extract_entities(intent, query_str)
                self.memory.update_task_context({"type": intent, "query": query_str})

            # 从代理网络中获取对应的 agent 实例
            agent = self.agent_network.get_agent(agent_name)
            if agent is None:
                # agent 未注册或不可用时，返回友好提示
                logger.warning(f"未找到代理：{agent_name}")
                return f"抱歉，{agent_name} 暂时不可用，请稍后重试。"

            # 构建发送给 agent 的消息：包含【短期对话+当前任务+用户偏好+长期记忆】完整上下文 + 本次查询
            # 让子代理理解跨会话的用户偏好与任务背景，提升多轮/复杂任务执行效果
            # 末尾追加范围约束指令：短期对话中可能含用户多意图原话，约束子代理只答当前意图，避免越权代答
            chat_history = self.memory.get_memory_context(query_str) + f'\nUser: {query_str}'

            # 构造task
            msg = Message(content=TextContent(text=chat_history), role=MessageRole.USER)
            task = Task(id="task-" + str(uuid.uuid4()), message=msg.to_dict())

            # 通过 A2A 协议异步发送任务给子代理
            # run_in_executor 将异步操作放到线程池中执行（避免阻塞事件循环）
            #   agent.send_task_async(task) 是 python_a2a 库的异步方法，需要在事件循环里跑。
            #   但当前代码已经处在一个正在运行的事件循环中（整个 chat 流程是 async 的），此时不能再直接 asyncio.run(...)，否则会报 asyncio.run() cannot be called from a running event loop。
            #   解决办法：
            #       asyncio.get_event_loop()：拿到主事件循环。
            #       run_in_executor(None, ...) ：
            #           另开一个后台线程，None 表示用默认线程池；
            #           在线程里临时新建一个事件循环asyncio.run(...)，把子代理请求 send_task_async 完整跑一遍拿到结果（新建一个事件循环 → 运行协程直到完成 → 关闭循环 → 返回结果）。
            #           主线程在等待期间不阻塞，可以去并行处理其他任务。
            #           agent.send_task_async(task)：调用异步方法，只是生成一个「协程对象」，没真正发网络请求。
            # 外层 await：主协程在此期间让出控制权，事件循环可以去推进其他并行协程（比如同组的另一个 step），从而既不阻塞、又能并发。
            try:
                raw_response = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: asyncio.run(agent.send_task_async(task))
                )
                logger.info(f"{agent_name} 原始响应: {raw_response}")

                # 解析 agent 返回结果
                if raw_response.status.state == 'completed' and raw_response.artifacts:
                    # 任务成功完成，从 artifacts 中提取结果文本
                    agent_result = raw_response.artifacts[0]['parts'][0]['text']
                elif raw_response.status.message:
                    # 有消息但可能失败了，尝试从消息中提取文本
                    agent_result = raw_response.status.message.get('content', {}).get('text', str(raw_response.status.message))
                else:
                    agent_result = f"查询失败：{raw_response.status.message or '未知错误'}"

            except Exception as e:
                # agent 服务不可用（如网络不通、服务崩溃），返回友好错误提示
                logger.error(f"{agent_name} 调用异常: {str(e)}")
                return f"{agent_name} 服务暂时不可用：{str(e)}"

            # 根据代理类型，使用对应的总结 prompt 处理结果
            if agent_name == "WeatherQueryAssistant":
                chain = SmartVoyagePrompts.summarize_weather_prompt() | self.llm
                return chain.invoke({"query": query_str, "raw_response": agent_result}).content.strip()
            elif agent_name == "TicketAssistant":
                chain = SmartVoyagePrompts.summarize_ticket_prompt() | self.llm
                return chain.invoke({"query": query_str, "raw_response": agent_result}).content.strip()
            elif agent_name == "TripAssistant":
                # TripAssistant 已返回格式化结果（租车/保险/旅游团等），直接使用
                return agent_result
            else:
                # 其他代理直接返回原始结果
                return agent_result

        # ========== 情况2：未识别的意图 ===========
        else:
            return "暂不支持此意图。"

    async def execute_step(self, step: dict, user_queries: dict) -> str:
        """
        需求：将规划中的单个步骤转化为实际执行，委托给统一的 agent 调用方法。
        思路步骤：
        1. 从步骤字典中提取意图类型（如 "weather"）
        2. 优先从 user_queries 获取改写后的查询，否则使用用户原始输入
        3. 记录日志，然后委托给 _call_agent_intent 执行实际查询
        """

        # 从步骤字典中提取意图类型（如 "weather"）
        intent = step.get("intent", "")
        # 优先从 user_queries 中获取改写后的查询，如果没有则用用户原始输入
        # 优先取改写后的查询；LLM 漏 key 或值非法时回退用户原话
        query_str = user_queries.get(intent, self.messages[-1]["content"] if self.messages else "")
        logger.info(f"执行步骤：{step.get('action', '')}，意图：{intent}")

        # 委托给统一的 agent 调用方法
        return await self._call_agent_intent(intent, query_str)

    async def react_loop(self, steps: list, user_queries: dict) -> str:
        """
        需求：按规划步骤逐步执行 ReAct 循环，直接行动（跳过 Thought），最终汇总为连贯回复。
        思路步骤：
        1. 按 depends_on 值将步骤分组，相同值的步骤可以并行执行
        2. 逐组执行：组内多步骤用 asyncio.gather 并行，单步骤直接执行
        3. 将每步结果存入 observations 和 step_results（作为 Observation）
        4. 所有步骤完成后，如果有多步结果，用 flash 模型做最终汇总
        5. 如果只有一步，直接返回该步结果

        优化：省略 Thought LLM 调用（plan 已确定动作，Thought 无额外决策价值），
        省去每步 1 次 LLM 调用，多步骤场景下显著减少响应时间。
        """
        observations = []     # 存储每个步骤的执行结果（Observation）
        step_results = []     # 存储每个步骤的详细结果（步骤号、描述、结果）

        # 按依赖关系分组：depends_on 值相同的步骤可以并行
        from collections import OrderedDict
        # OrderedDict 保证按第一次出现的 depends_on 顺序执行组。
        # 同一组内的步骤被认为没有相互依赖，可以并行。
        dep_groups = OrderedDict()
        for step in steps:
            dep = step.get("depends_on", 0)
            dep_groups.setdefault(dep, []).append(step)

        # 例如：
        # 用户输入：我下周要去上海出差，帮我查北京飞上海的航班、上海的天气，再推荐几个上海的景点
        # steps = [
        #   {"step": 1, "action": "调用WeatherQueryAssistant查询上海天气", "intent": "weather", "depends_on": 0},
        #   {"step": 2, "action": "调用TripAssistant推荐上海景点",  "intent": "attraction", "depends_on": 0},
        #   {"step": 3, "action": "调用TicketAssistant查询北京到上海航班", "intent": "flight", "depends_on": 1}
        # ]

        # user_queries = {
        #   "flight":     "2026-09-21 北京到上海的航班查询",
        #   "weather":    "2026-09-21 上海天气",
        #   "attraction": "上海景点推荐"
        # }

        # 分组后：
        # {
        #     0: [step1, step2],   # depends_on=0 → 同一组，可并行
        #     1: [step3],          # depends_on=1 → 单独一组，在前一组之后执行
        # }

        # 逐组执行，组内并行
        for dep_key, group_steps in dep_groups.items():
            # 组内步骤并行执行 Action
            if len(group_steps) > 1:
                logger.info(f"并行执行 {len(group_steps)} 个无依赖步骤")

                # execute_step 是 async def，调用它不加 await 时不会真正运行函数体，只会创建并返回一个「协程对象」（coroutine）
                # tasks :两个尚未执行的协程对象  [<coroutine object execute_step at 0x...>, <coroutine object execute_step at 0x...>]
                tasks = [self.execute_step(s, user_queries) for s in group_steps]

                # 并发执行，返回结果
                # *tasks：解包，把列表拆成位置参数，等价于 asyncio.gather(c1, c2)。
                # asyncio.gather(...)：把这些协程一起提交给事件循环并发调度。
                #   并发的本质（单线程也能并发）：事件循环在同一线程内交替推进多个协程——当 step1 的协程执行到 await（发起 A2A 网络请求、等待响应）时会主动让出控制权，事件循环立刻切去推进 step2；谁的 IO 先回来就先继续谁。因此总耗时约等于最慢的那个，而非各步之和：
                #       串行 for+await：  |—step1(2s)—|—step2(3s)—|   合计 5s
                #       gather 并发：     |—step1(2s)—|
                #                         |———step2(3s)———|       合计 ≈ 3s
                # await gather(...)：挂起 react_loop 当前协程，直到组内所有协程都完成，然后一次性返回结果列表。
                #   返回顺序 = 传入顺序（不是完成先后）：results[0] 一定对应 group_steps[0]。这正是后面能安全用 zip(group_steps, results) 一一对应的前提。

                # return_exceptions=True 的作用（容错关键）
                #   默认 False：只要有一个协程抛异常，gather 会立即把该异常向上抛出，导致整个 react_loop 崩掉，其他已经跑好的结果也拿不到。
                #   设为 True：异常不会中断 gather，而是被当作「该位置的结果」返回——即 results 里对应项会是一个 Exception 实例，其余步骤照常返回各自结果。
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # results = [
                #     # step1(weather) → WeatherQueryAssistant，再经 summarize_weather_prompt 总结：
                #     "上海9月21日多云转晴，气温22~29℃，东南风3级，空气质量良，适宜出行。",
                #     # step2(attraction) → TripAssistant，直接返回格式化结果：
                #     "为您推荐上海热门景点：\n1. 外滩——万国建筑群+黄浦江夜景\n2. 东方明珠——登塔俯瞰全城\n3. 豫园——江南古典园林"
                # ]
                for step, result in zip(group_steps, results):
                    step_num = step.get("step", 0)
                    step_desc = step.get("action", "")

                    # 如果返回的是报错对象，转换为文本
                    # 例如：result = "执行失败：TicketAssistant 服务暂时不可用：Connection refused"
                    if isinstance(result, Exception):
                        result = f"执行失败：{result}"

                    # observations = [
                    #     "上海9月21日多云转晴，气温22~29℃…适宜出行。",
                    #     "为您推荐上海热门景点：\n1. 外滩…\n2. 东方明珠…\n3. 豫园…"
                    # ]
                    #
                    # step_results = [
                    #     {"step": 1, "description": "调用WeatherQueryAssistant查询上海天气", "result": "上海9月21日多云转晴…适宜出行。"},
                    #     {"step": 2, "description": "调用TripAssistant推荐上海景点", "result": "为您推荐上海热门景点：\n1. 外滩…"}
                    # ]
                    observations.append(result)
                    step_results.append({"step": step_num, "description": step_desc, "result": result})
                    logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")
            else:
                # 单步骤，直接执行
                # {"step": 3, "action": "调用TicketAssistant查询北京到上海航班", "intent": "flight", "depends_on": 1}
                step = group_steps[0]
                step_num = step.get("step", 0)
                step_desc = step.get("action", "")

                # 返回示例（flight → TicketAssistant，经 summarize_ticket_prompt 总结）：
                # result = "已为您找到9月21日北京→上海航班：\n1. 国航CA1501 07:00起飞 经济舱¥980\n2. 东航MU5101 09:30起飞 经济舱¥1050"
                result = await self.execute_step(step, user_queries)

                # observations = [
                #     "上海9月21日多云转晴…适宜出行。",
                #     "为您推荐上海热门景点：\n1. 外滩…",
                #     "已为您找到9月21日北京→上海航班：\n1. 国航CA1501…\n2. 东航MU5101…"
                # ]
                #
                # step_results = [
                #     {"step": 1, "description": "调用WeatherQueryAssistant查询上海天气", "result": "上海9月21日多云转晴…"},
                #     {"step": 2, "description": "调用TripAssistant推荐上海景点", "result": "为您推荐上海热门景点…"},
                #     {"step": 3, "description": "调用TicketAssistant查询北京到上海航班", "result": "已为您找到9月21日北京→上海航班…"}
                # ]
                observations.append(result)
                step_results.append({"step": step_num, "description": step_desc, "result": result})
                logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")

        # ========== 所有步骤完成后的最终汇总 ==========
        if len(step_results) > 1:
            # 多个步骤：使用专门的汇总 prompt，将各步骤结果整合成一条连贯回复
            summary_chain = SmartVoyagePrompts.react_summary_prompt() | self.llm

            # all_obs = (
            #     "步骤1 (调用WeatherQueryAssistant查询上海天气): 上海9月21日多云转晴…适宜出行。\n"
            #     "步骤2 (调用TripAssistant推荐上海景点): 为您推荐上海热门景点：\n1. 外滩…\n"
            #     "步骤3 (调用TicketAssistant查询北京到上海航班): 已为您找到9月21日北京→上海航班…"
            # )
            all_obs = "\n".join([f"步骤{s['step']} ({s['description']}): {s['result']}" for s in step_results])

            # final_response = (
            #     "您好！已为您备好下周上海出差的全套信息：\n"
            #     "【天气】9月21日上海多云转晴，22~29℃，东南风3级，空气质量良，非常适合出行。\n"
            #     "【航班】当天北京→上海航班充足，推荐国航CA1501（07:00起飞，经济舱¥980），性价比更高。\n"
            #     "【景点】工作之余不妨去外滩看黄浦江夜景、登东方明珠俯瞰全城，或到豫园感受江南园林，祝您出差顺利！"
            # )
            final_response = summary_chain.invoke({
                "query": self.messages[-1]["content"] if self.messages else "",
                "all_observations": all_obs
            }).content.strip()

        else:
            # 只有一个步骤：直接使用该步骤的结果
            final_response = observations[0] if observations else "暂无结果"

        return final_response

    # ==================== ⑤ Agent 执行层（流式） ====================

    async def _stream_text(self, text: str, chunk_size: int = 4, delay: float = 0.02):
        """将一段完整文本切成小片逐片 yield，模拟打字机流式效果。"""
        for i in range(0, len(text), chunk_size):
            yield text[i:i + chunk_size]
            await asyncio.sleep(delay)

    async def _call_agent_intent_stream(self, intent: str, query_str: str):
        """
        需求：流式版本的 _call_agent_intent，仅在生成最终回复时使用流式输出。
        思路步骤：
        1. 根据意图类型查找对应 agent（所有意图含 attraction 均走子代理）
        2. 票务查询类意图先提取实体到记忆，再通过 A2A 协议调用子代理
        3. 解析子代理返回结果，处理完成/追问/失败等不同状态
        4. 保险/旅游团意图额外提取产品数据，以 SSE card 事件推送给前端
        5. 天气用 flash 模型流式总结，行程管家（含景点推荐）直接流式输出格式化结果
        调用方需要自行收集 yield 的片段拼接为完整回复。

        说明：attraction 景点推荐由 TripAssistant 处理，通过 MCP search_travel_guides
        工具（博查联网搜索）获取真实旅游攻略数据，而非直接让 LLM 生成。
        """
        agent_name = conf.intent.get(intent)

        if agent_name:
            if intent in TICKET_INTENTS:  # 票务/行程类意图白名单（与 memory.extract_long_term_from_query 共用常量）
                self.memory.extract_entities(intent, query_str)
                self.memory.update_task_context({"type": intent, "query": query_str})

            agent = self.agent_network.get_agent(agent_name)
            if agent is None:
                logger.warning(f"未找到代理：{agent_name}")
                yield f"抱歉，{agent_name} 暂时不可用，请稍后重试。"
            else:
                # 末尾追加范围约束指令：与 _call_agent_intent 同源，约束子代理只答当前意图，避免越权代答
                chat_history = self.memory.get_memory_context(query_str) + f'\nUser: {query_str}'
                msg = Message(content=TextContent(text=chat_history), role=MessageRole.USER)
                task = Task(id="task-" + str(uuid.uuid4()), message=msg.to_dict())

                try:
                    import time as _time
                    _t_agent = _time.time()
                    raw_response = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, lambda: asyncio.run(agent.send_task_async(task))
                        ),
                        timeout=300  # 代理调用超时：300秒内无响应则返回错误
                    )
                    logger.info(f"[耗时] A2A 调用 {agent_name}: {_time.time() - _t_agent:.2f}s")
                    logger.info(f"{agent_name} 原始响应: {raw_response}")

                    if raw_response.status.state == 'completed' and raw_response.artifacts:
                        agent_result = raw_response.artifacts[0]['parts'][0]['text']
                    elif str(raw_response.status.state) in ('input-required', 'TaskState.INPUT_REQUIRED'):
                        # 子代理需要追问用户，直接透传追问消息，跳过总结链
                        msg = raw_response.status.message
                        if isinstance(msg, dict):
                            follow_up = msg.get('content', {}).get('text', '')
                        else:
                            content = getattr(msg, 'content', None)
                            follow_up = content.text if hasattr(content, 'text') else str(content)
                        if follow_up:
                            # 流式输出追问消息（可能包含查询结果+追问，需分块输出）
                            async for chunk in self._stream_text(follow_up):
                                yield chunk
                        return
                    elif raw_response.status.message:
                        msg = raw_response.status.message
                        if isinstance(msg, dict):
                            agent_result = msg.get('content', {}).get('text', str(msg))
                        else:
                            content = getattr(msg, 'content', None)
                            agent_result = content.text if hasattr(content, 'text') else str(content)
                    else:
                        agent_result = f"查询失败：{raw_response.status.message or '未知错误'}"
                except asyncio.TimeoutError:
                    logger.error(f"{agent_name} 调用超时（300秒）")
                    agent_result = f"{agent_name} 响应超时，查询处理时间过长，请稍后重试。"
                except Exception as e:
                    logger.error(f"{agent_name} 调用异常: {str(e)}")
                    agent_result = f"{agent_name} 服务暂时不可用：{str(e)}"

                # ========== 卡片推送：保险/旅游团推荐 ==========
                # 只要 agent 结果中包含 <products> 标签就提取卡片（不限于 insurance/tour_group 意图）
                # travel_guide 等意图也可能附带保险和旅游团推荐
                for _card_intent in ("insurance", "tour_group"):
                    items = self._extract_card_items(_card_intent, agent_result)
                    if items:
                        yield {"type": "card", "card_type": _card_intent, "items": items}
                # 从文本中移除所有 <products>...</products> 标签，避免裸 JSON 泄漏到聊天
                agent_result = re.sub(
                    r'<products>\s*\[.*?\]\s*</products>',
                    '', agent_result, flags=re.DOTALL
                ).strip()

                if agent_name == "WeatherQueryAssistant":
                    # 天气子代理返回原始数据，需要 LLM 总结为自然语言
                    chain = SmartVoyagePrompts.summarize_weather_prompt() | self.llm
                elif agent_name in ("TicketAssistant", "TripAssistant"):
                    # 票务/行程子代理内部已经用 LLM 格式化过结果，直接流式输出，不再二次总结
                    async for chunk in self._stream_text(agent_result):
                        yield chunk
                    return
                else:
                    async for chunk in self._stream_text(agent_result):
                        yield chunk
                    return

                import time as _time
                _t_sum = _time.time()
                async for chunk in chain.astream({"query": query_str, "raw_response": agent_result}):
                    content = chunk.content if hasattr(chunk, 'content') else str(chunk)
                    if content:
                        yield content
                logger.info(f"[耗时] 总结链 LLM 流式输出: {_time.time() - _t_sum:.2f}s")

        else:
            yield "暂不支持此意图。"

    def _extract_card_items(self, intent: str, agent_result: str) -> list:
        """
        需求：从 TripAgent 的原始结果中提取保险/旅游团产品数据，用于前端卡片推送。
        思路步骤：
        1. 用正则匹配 <products>...</products> 标签中的 JSON 数组内容
        2. 将匹配到的 JSON 字符串解析为 Python 列表
        3. 验证解析结果确实是列表类型，返回产品列表
        4. 提取失败时记录警告日志，返回空列表

        约定格式：TripAgent 的系统 Prompt 要求它在返回结果中附带 <products>...</products>
        标签，内部为 JSON 数组，例如：
            <products>[{"name": "...", "price": ..., "desc": "..."}, ...]</products>
        """
        try:
            # 匹配 <products>...</products> 标签中的 JSON 内容
            match = re.search(r'<products>\s*(\[.*?\])\s*</products>', agent_result, re.DOTALL)
            if match:
                items = json.loads(match.group(1))
                if isinstance(items, list):
                    return items
            return []
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"提取卡片产品数据失败: {e}")
            return []

    async def _react_loop_stream(self, steps: list, user_queries: dict):
        """
        需求：流式版本的 ReAct 循环，按依赖分组执行步骤，最终汇总时流式输出。
        思路步骤：
        1. 按 depends_on 值分组步骤，相同值的步骤可以并行
        2. 逐组执行：组内并行（asyncio.gather），单步直接执行
        3. 收集每步结果到 observations 和 step_results
        4. 多步结果时用 react_summary_prompt 流式汇总（astream）
        5. 单步结果时直接 yield

        优化：与 react_loop 一致，省略 Thought LLM 调用，减少每步 1 次 LLM 请求。
        调用方需要自行收集 yield 的片段拼接为完整回复。
        """
        observations = []
        step_results = []

        from collections import OrderedDict
        dep_groups = OrderedDict()
        for step in steps:
            dep = step.get("depends_on", 0)
            dep_groups.setdefault(dep, []).append(step)

        for dep_key, group_steps in dep_groups.items():
            if len(group_steps) > 1:
                logger.info(f"并行执行 {len(group_steps)} 个无依赖步骤")
                tasks = [self.execute_step(s, user_queries) for s in group_steps]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for step, result in zip(group_steps, results):
                    step_num = step.get("step", 0)
                    step_desc = step.get("description", step.get("action", ""))
                    if isinstance(result, Exception):
                        result = f"执行失败：{result}"
                    observations.append(result)
                    step_results.append({"step": step_num, "description": step_desc, "result": result})
                    logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")
            else:
                step = group_steps[0]
                step_num = step.get("step", 0)
                step_desc = step.get("description", step.get("action", ""))
                result = await self.execute_step(step, user_queries)
                observations.append(result)
                step_results.append({"step": step_num, "description": step_desc, "result": result})
                logger.info(f"ReAct 步骤 {step_num} 结果: {result[:100]}...")

        if len(step_results) > 1:
            summary_chain = SmartVoyagePrompts.react_summary_prompt() | self.llm
            all_obs = "\n".join([f"步骤{s['step']} ({s['description']}): {s['result']}" for s in step_results])
            async for chunk in summary_chain.astream({
                "query": self.messages[-1]["content"] if self.messages else "",
                "all_observations": all_obs
            }):
                content = chunk.content if hasattr(chunk, 'content') else str(chunk)
                if content:
                    yield content
        else:
            yield observations[0] if observations else "暂无结果"
    # ==================== ⑥ 记忆管理接口 ====================

    def update_user_profile(self, profile: dict):
        """
        需求：更新用户偏好设置，供前端 API 直接调用以修改用户个性化配置。
        思路步骤：
        1. 接收前端传入的偏好字典（如 {"seat_type": "二等座"}）
        2. 委托给 memory.update_profile() 合并到现有偏好中（新增/覆盖，不删除已有项）
        3. 记录日志以便追踪偏好变更
        """
        self.memory.update_profile(profile)
        logger.info(f"更新用户偏好: {profile}")

    def get_memory_state(self) -> dict:
        """
        需求：获取当前记忆状态的摘要信息，用于调试展示和前端查看。
        思路步骤：
        1. 取最近 5 条短期对话消息，将角色标签翻译为中文（"用户"/"助手"）
        2. 获取用户偏好字典
        3. 获取当前任务上下文
        4. 取最近 5 条实体查询历史
        5. 取系统自动沉淀的长期记忆（跨会话偏好/事实），按重要性倒序
        6. 组装为字典返回
        """
        # 长期记忆（自动沉淀的用户偏好/事实）：按重要性倒序、再按更新时间倒序，取前 10 条
        long_term = sorted(
            self.memory.long_term_memories.values(),
            key=lambda m: (m.get("importance", 0), m.get("updated_at", "")),
            reverse=True
        )[:10]
        return {
            "short_term_messages": [
                {"role": "用户" if m["role"] == "user" else "助手", "content": m["content"], "timestamp": m["timestamp"]}
                for m in self.memory.short_term_messages[-5:]
            ],
            "user_profile": self.memory.user_profile,
            "current_task": self.memory.current_task,
            "entity_history": self.memory.entity_history[-5:],
            "long_term_memories": long_term
        }

    def get_agent_cards(self) -> list:
        """
        需求：获取所有子代理的卡片信息（Agent Card），用于展示代理能力和动态工具列表。
        思路步骤：
        1. 遍历代理网络中注册的所有 agent 名称
        2. 获取每个 agent 的卡片信息（名称、技能、描述等）
        3. 如果缓存的卡片是启动时拉取失败的 fallback，尝试重新从远程刷新
        4. 将技能对象格式化为 "技能名: 技能描述" 字符串
        5. 组装卡片字典列表返回
        """
        cards = []
        for agent_name in self.agent_network.agents.keys():
            agent_card = self.agent_network.get_agent_card(agent_name)

            # 如果缓存的卡片是启动时拉取失败的 fallback（"Agent card not available"），
            # 尝试重新从远程服务器获取 —— 此时服务器可能已经启动
            if agent_card and agent_card.description == "Agent card not available":
                agent_client = self.agent_network.get_agent(agent_name)
                if agent_client:
                    try:
                        refreshed = agent_client._fetch_agent_card()
                        if refreshed and refreshed.description != "Agent card not available":
                            agent_client.agent_card = refreshed
                            self.agent_network.agent_cards[agent_name] = refreshed
                            agent_card = refreshed
                            logger.info(f"成功刷新 {agent_name} 的 Agent Card")
                    except Exception as e:
                        logger.debug(f"刷新 {agent_name} Agent Card 失败: {e}")

            agent_url = self.agent_urls.get(agent_name, "未知地址")
            cards.append({
                "name": agent_name,
                # 将每个技能对象格式化为 "技能名: 技能描述" 的字符串
                "skills": [s.name + ": " + s.description for s in agent_card.skills],
                "description": agent_card.description,
                "url": agent_url,
                "status": "在线"
            })
        return cards


    def clear_memory(self):
        """
        需求：清空所有记忆数据，重置对话状态，用于用户输入 'clear' 命令时调用。
        思路步骤：
        1. 调用 memory.clear() 清空记忆管理器中的所有数据（内部会自动清理数据库持久化数据）
        2. 清空本实例的 messages 列表
        3. 清空对话历史字符串
        """
        self.memory.clear()
        self.messages.clear()

