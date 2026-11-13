"""
需求：实现SmartVoyage的记忆管理模块，包括短期对话记忆、用户偏好记忆和当前任务上下文
支持 MySQL 数据库持久化，服务重启后自动恢复记忆数据
"""
import re  # 正则表达式，用于从查询中抽取长期偏好线索
from datetime import datetime  # 导入日期时间模块，用于生成时间戳
import json  # JSON 模块，用于序列化/反序列化任务上下文和查询数据
import pytz  # 时区库，用于生成带时区的时间戳（统一用上海时区）

# 票务/行程类意图白名单：把守 extract_entities / update_task_context / extract_long_term_from_query
# 三个记忆写入动作，由 memory.py、chat_service.py、main.py 共用（单一定义源，避免多处字面量口径漂移）
TICKET_INTENTS = ("flight", "flight_ticket", "train", "car_rental", "tour_group", "insurance")


class ConversationMemory:
    """
    需求：管理对话记忆的类，包括短期对话、长期记忆、用户偏好和任务上下文。
    思路步骤：
    1. 维护短期对话列表（最近几轮）、用户偏好字典、当前任务上下文
    2. 支持数据库持久化，服务重启后可自动恢复
    3. 提供分层上下文组装方法，供意图识别等环节注入 prompt
    """

    # ==================== 初始化与数据库基础 ====================

    def __init__(self, username: str = "default", short_term_limit: int = 10):
        """
        需求：初始化记忆对象，设置短期记忆上限，并清空各层记忆容器。
        思路步骤：
        1. 保存用户名，用于数据库隔离
        2. 创建短期对话消息列表
        3. 创建用户偏好字典（如座位、舱位）
        4. 创建当前任务上下文字典
        5. 设置短期记忆最大条数
        6. 创建实体历史记录列表
        7. 创建长期记忆字典（key->记忆内容）
        8. 初始化数据库连接为 None
        """
        self.username = username  # 当前用户名，所有 SQL 都按此字段隔离

        self.short_term_messages = []  # 短期对话消息列表，最多保留short_term_limit条
        self.short_term_limit = short_term_limit  # 短期记忆最大长度

        self.user_profile = {}  # 用户偏好，如 {"seat_type": "二等座", "cabin_type": "经济舱"}

        self.current_task = {}  # 当前任务上下文，如 {"type": "train", "query": "北京到上海的车票"}
        self.entity_history = [] # 历史查询记录，如[{"type": "train", "query": "北京到上海的车票", "timestamp"："2026-10-01 00:00:00"}]

        self.long_term_memories = {}  # 长期记忆（语义记忆）：跨会话沉淀的用户偏好/事实，key->dict
        self._db_conn = None  # 数据库连接

    def set_db_connection(self, db_conn):
        """
        需求：设置数据库连接（由 ChatService 注入）。
        思路步骤：
        1. 将外部传入的数据库连接对象赋值给内部属性
        """
        self._db_conn = db_conn  # 保存数据库连接引用

    def _ensure_db(self):
        """
        需求：确保数据库连接有效，断开则自动重连。
        思路步骤：
        1. 检查连接是否为 None，若是则抛出异常
        2. 尝试 ping 数据库并自动重连
        3. 若 ping 失败则抛出连接断开异常
        """
        if self._db_conn is None:  # 若连接未初始化，抛出运行时错误
            raise RuntimeError("数据库连接未初始化")
        try:
            self._db_conn.ping(reconnect=True)  # ping 数据库，断开时自动重连
        except Exception:
            raise RuntimeError("数据库连接已断开")  # ping 失败说明连接已断开

    def close(self):
        """
        需求：关闭数据库连接，释放资源（由 api_server 在用户会话缓存过期时调用）。
        思路步骤：
        1. 若连接存在则调用 close() 释放
        2. 置为 None，后续持久化方法会因连接为 None 而安全跳过
        """
        if self._db_conn is not None:  # 若持有数据库连接
            self._db_conn.close()  # 关闭连接，释放数据库资源
            self._db_conn = None  # 置为 None，后续 DB 读写方法将安全跳过

    # ==================== 短期对话（short_term_messages / 工作记忆） ====================

    def add_message(self, role: str, content: str):
        """
        需求：添加消息到短期记忆，并持久化到数据库。
        思路步骤：
        1. 将消息（角色、内容、时间戳）追加到短期消息列表
        2. 若超过短期记忆上限，则截断只保留最新的 N 条
        3. 调用持久化方法将短期记忆写入数据库
        """
        self.short_term_messages.append({  # 追加一条新消息到短期对话列表
            "role": role,  # 消息角色（user 或 assistant）
            "content": content,  # 消息文本内容
            "timestamp": datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')  # 当前上海时间的完整时间戳（日期+时分秒，与 entity_history 口径一致）
        })
        # 超过限制则移除最旧的消息
        if len(self.short_term_messages) > self.short_term_limit:  # 若消息数超出上限
            self.short_term_messages = self.short_term_messages[-self.short_term_limit:]  # 截断只保留最新的 N 条
        # 持久化到数据库
        self.save_messages_to_db()  # 将截断后的短期对话写入数据库

    def save_messages_to_db(self):
        """
        需求：将短期对话覆盖写入数据库（先删除旧数据，再写入当前列表）。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 先删除 short_term_messages 表的所有旧数据
        4. 遍历当前短期对话列表，逐条插入（带序号保持顺序）
        5. 提交事务并关闭游标
        6. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor()  # 创建数据库游标
            cursor.execute("DELETE FROM short_term_messages WHERE username = %s", (self.username,))  # 先清空当前用户的旧数据
            for i, msg in enumerate(self.short_term_messages):  # 遍历每条短期消息（带序号）
                cursor.execute(  # 插入一条短期消息记录
                    "INSERT INTO short_term_messages (username, role, content, message_time, message_order) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (self.username, msg["role"], msg["content"], msg["timestamp"], i)  # 用户名、角色、内容、时间、序号
                )
            self._db_conn.commit()  # 提交事务
            cursor.close()  # 关闭游标
        except Exception as e:  # 捕获所有异常
            print(f"保存短期对话到数据库失败: {e}")  # 打印错误信息

    def load_messages_from_db(self):
        """
        需求：从数据库加载短期对话。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 按 message_order 正序查询 short_term_messages 表
        4. 将结果组装为短期对话列表（message_time 为 DATETIME 列，格式化为完整时间戳字符串）
        5. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor(dictionary=True)  # 创建游标，返回字典格式结果
            cursor.execute(  # 按消息序号正序查询当前用户的短期对话
                "SELECT role, content, message_time FROM short_term_messages "
                "WHERE username = %s ORDER BY message_order ASC",
                (self.username,)  # 用户名
            )
            rows = cursor.fetchall()  # 获取所有查询结果
            cursor.close()  # 关闭游标
            self.short_term_messages = [  # 组装短期对话列表
                {"role": row["role"], "content": row["content"],
                 "timestamp": row["message_time"].strftime('%Y-%m-%d %H:%M:%S') if row["message_time"] else ""}  # DATETIME 列格式化为完整时间戳字符串
                for row in rows  # 遍历每条记录
            ]
        except Exception as e:  # 捕获所有异常
            print(f"从数据库加载短期对话失败: {e}")  # 打印错误信息


    def get_short_term_text(self) -> str:
        """
        需求：获取短期对话的文本格式，用于意图识别和代理调用。
        思路步骤：
        1. 遍历短期消息列表
        2. 将每条消息的角色（user/assistant）转为显示标签
        3. 拼接为多行字符串返回
        """
        lines = []  # 初始化空列表，用于收集每行文本
        for msg in self.short_term_messages:  # 遍历每条短期消息
            role_label = "User" if msg["role"] == "user" else "Assistant"  # 根据角色选择显示标签
            lines.append(f"{role_label}: {msg['content']}")  # 格式化为 "User: 内容" 或 "Assistant: 内容"
        return '\n'.join(lines)  # 用换行符拼接所有行并返回

    # ==================== 用户偏好（user_profile） ====================

    def update_profile(self, profile_update: dict):
        """
        需求：更新用户偏好，并持久化到数据库。
        思路步骤：
        1. 将传入的偏好字典合并到当前用户偏好
        2. 调用持久化方法将更新后的偏好写入数据库
        """
        self.user_profile.update(profile_update)  # 合并新的偏好键值对到用户偏好字典
        self.save_profile_to_db()  # 持久化到数据库

    def save_profile_to_db(self):
        """
        需求：将用户偏好持久化到数据库（UPSERT）。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 遍历用户偏好的每个键值对，执行 INSERT ... ON DUPLICATE KEY UPDATE
        4. 提交事务并关闭游标
        5. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回，不做任何操作
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor()  # 创建数据库游标
            for key, value in self.user_profile.items():  # 遍历每个偏好键值对
                cursor.execute(  # 执行 UPSERT 语句：存在则更新，不存在则插入
                    "INSERT INTO user_profiles (username, profile_key, profile_value) "
                    "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE profile_value = %s",
                    (self.username, key, str(value), str(value))  # 参数：用户名、键、值（转为字符串）
                )
            self._db_conn.commit()  # 提交事务
            cursor.close()  # 关闭游标
        except Exception as e:  # 捕获所有异常
            print(f"保存用户偏好到数据库失败: {e}")  # 打印错误信息

    def load_profile_from_db(self):
        """
        需求：从数据库加载用户偏好。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 查询 user_profiles 表的所有记录
        4. 将结果组装为字典赋值给 user_profile
        5. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor(dictionary=True)  # 创建游标，返回字典格式结果
            cursor.execute("SELECT profile_key, profile_value FROM user_profiles WHERE username = %s", (self.username,))  # 查询当前用户的偏好记录
            rows = cursor.fetchall()  # 获取所有查询结果
            cursor.close()  # 关闭游标
            self.user_profile = {row["profile_key"]: row["profile_value"] for row in rows}  # 组装为偏好字典
        except Exception as e:  # 捕获所有异常
            print(f"从数据库加载用户偏好失败: {e}")  # 打印错误信息

    def get_profile_text(self) -> str:
        """
        需求：获取用户偏好的文本描述，用于注入到prompt中。
        思路步骤：
        1. 若用户偏好为空，返回"无已知的用户偏好"
        2. 否则将每个偏好键值对格式化为 "key: value"
        3. 用中文逗号拼接所有偏好项并返回
        """
        if not self.user_profile:  # 若用户偏好字典为空
            return "无已知的用户偏好"  # 返回默认提示文本
        items = [f"{k}: {v}" for k, v in self.user_profile.items()]  # 将每个偏好项格式化为 "key: value"
        return "，".join(items)  # 用中文逗号拼接所有偏好项

    # ==================== 长期记忆（long_term_memories / 语义记忆层） ====================

    def extract_long_term_from_query(self, intent: str, query: str):
        """
        需求：从用户查询中自动抽取「可长期沉淀」的偏好线索，写入长期记忆。
        思路步骤：
        1. 用正则匹配城市偏好（如「喜欢去北京」「常去上海」）
        2. 用关键词匹配座位偏好（二等座/一等座/商务座）
        3. 用关键词匹配舱位偏好（经济舱/公务舱/头等舱）
        4. 用关键词匹配车型偏好（经济型/SUV/豪华型/MPV）
        5. 若当前意图属于票务/行程类（TICKET_INTENTS，与 chat_service/main 共用），且抽取到新线索，则写入长期记忆
        6. 仅当内容变化时才更新，避免每轮重复写入相同内容

        采用轻量规则匹配（而非每轮调用 LLM），兼顾运行效率与教学可读性。

        参数：
            intent (str): 当前意图类型
            query (str): 用户查询文本
        """
        candidates = []  # 初始化候选偏好列表

        # 城市偏好：匹配「喜欢去/常去/经常去/习惯去 + 城市名」
        city_pattern = re.compile(  # 编译正则表达式，匹配城市偏好模式
            r'(?:喜欢去|常去|经常去|习惯去|一般去|爱去)\s*([一-龥]{2,6}?市?)'
        )
        m = city_pattern.search(query)  # 在查询文本中搜索城市偏好
        if m:  # 若匹配到城市偏好
            city = m.group(1)  # 提取城市名
            # 排除掉"去各地""去那里"这类无意义匹配
            if city not in ("各地", "那里", "哪里"):  # 过滤无意义的城市名
                candidates.append(("pref_city", "preference", f"用户常去的城市：{city}", query))  # 加入候选列表

        # 座位偏好
        for seat in ("二等座", "一等座", "商务座"):  # 遍历所有座位类型
            if seat in query:  # 若查询文本中包含该座位类型
                candidates.append(("pref_seat", "preference", f"用户偏好座位类型：{seat}", query))  # 加入候选列表
                break  # 匹配到一个即停止

        # 舱位偏好
        for cabin in ("经济舱", "公务舱", "头等舱"):  # 遍历所有舱位类型
            if cabin in query:  # 若查询文本中包含该舱位类型
                candidates.append(("pref_cabin", "preference", f"用户偏好舱位类型：{cabin}", query))  # 加入候选列表
                break  # 匹配到一个即停止

        # 车型偏好
        for car in ("经济型", "SUV", "豪华型", "MPV"):  # 遍历所有车型类型
            if car in query:  # 若查询文本中包含该车型
                candidates.append(("pref_car_type", "preference", f"用户偏好车型：{car}", query))  # 加入候选列表
                break  # 匹配到一个即停止

        # 若本次查询属于票务/行程类意图，且抽取到新线索，则写入长期记忆
        if candidates and intent in TICKET_INTENTS:  # 仅当意图属于票务/行程类时
            for key, mtype, content, source in candidates:  # 遍历每个候选偏好
                # 仅当内容变化时才更新，避免每轮重复写入相同内容
                old = self.long_term_memories.get(key)  # 获取已有的同 key 记忆
                if old is None or old["content"] != content:  # 若不存在或内容已变化
                    self.add_long_term_memory(key, mtype, content, source)  # 写入长期记忆

    def add_long_term_memory(self, key: str, memory_type: str, content: str,
                             source_query: str = None, importance: int = 5):
        """
        需求：写入一条长期记忆（UPSERT 语义：同 key 已存在则覆盖更新）。
        思路步骤：
        1. 获取当前上海时间作为更新时间
        2. 将记忆内容存入 long_term_memories 字典（以 key 为索引）
        3. 保留已有的访问次数（若 key 已存在）
        4. 调用持久化方法将长期记忆写入数据库

        长期记忆是跨会话沉淀的「语义记忆」，与短期对话（工作记忆）相对，
        用于记住用户可长期复用的偏好与事实，服务重启后仍能恢复。

        参数：
            key (str): 记忆唯一键，如 "pref_city"、"pref_seat"
            memory_type (str): 记忆类型，如 "preference"(偏好) / "fact"(事实)
            content (str): 记忆内容（注入 prompt 时展示的文本）
            source_query (str): 来源查询，便于追溯这条记忆是怎么沉淀的
            importance (int): 重要性 1~10，越大越重要
        """
        now = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')  # 获取当前上海时间
        self.long_term_memories[key] = {  # 将记忆内容存入字典
            "key": key,  # 记忆唯一键
            "type": memory_type,  # 记忆类型（preference/fact）
            "content": content,  # 记忆内容文本
            "source": source_query,  # 来源查询
            "importance": importance,  # 重要性评分
            "access_count": self.long_term_memories.get(key, {}).get("access_count", 0),  # 保留已有访问次数
            "updated_at": now  # 更新时间（落库前的占位值；真相源是 DB 的 updated_at 列，load 时读回）
        }
        self.save_long_term_to_db()  # 持久化到数据库

    def save_long_term_to_db(self):
        """
        需求：将长期记忆持久化到数据库（UPSERT），服务重启后自动恢复。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 遍历长期记忆字典的每条记录
        4. 执行 INSERT ... ON DUPLICATE KEY UPDATE 语句
        5. 提交事务并关闭游标
        6. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor()  # 创建数据库游标
            for key, mem in self.long_term_memories.items():  # 遍历每条长期记忆
                cursor.execute(  # 执行 UPSERT 语句：存在则更新，不存在则插入
                    "INSERT INTO long_term_memories "
                    "(username, memory_key, memory_type, memory_content, source_query, importance, access_count) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                    "memory_type=%s, memory_content=%s, source_query=%s, importance=%s, access_count=%s",
                    (self.username, key, mem["type"], mem["content"], mem.get("source"),  # 插入参数
                     mem.get("importance", 5), mem.get("access_count", 0),
                     mem["type"], mem["content"], mem.get("source"),  # 更新参数
                     mem.get("importance", 5), mem.get("access_count", 0))
                )
            self._db_conn.commit()  # 提交事务
            cursor.close()  # 关闭游标
        except Exception as e:  # 捕获所有异常
            print(f"保存长期记忆到数据库失败: {e}")  # 打印错误信息

    def load_long_term_from_db(self):
        """
        需求：从数据库加载长期记忆。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 查询 long_term_memories 表的所有记录（含 DB 自动维护的 updated_at 列）
        4. 将结果组装为长期记忆字典（updated_at 一并读回，保证重启后新鲜度信息不丢失）
        5. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor(dictionary=True)  # 创建游标，返回字典格式结果
            cursor.execute(  # 查询当前用户的所有长期记忆记录（updated_at 由 DB 自动维护）
                "SELECT memory_key, memory_type, memory_content, source_query, importance, access_count, updated_at "
                "FROM long_term_memories WHERE username = %s",
                (self.username,)  # 用户名
            )
            rows = cursor.fetchall()  # 获取所有查询结果
            cursor.close()  # 关闭游标
            self.long_term_memories = {}  # 重置长期记忆字典
            for row in rows:  # 遍历每条记录
                self.long_term_memories[row["memory_key"]] = {  # 以 memory_key 为键组装记忆字典
                    "key": row["memory_key"],  # 记忆唯一键
                    "type": row["memory_type"],  # 记忆类型
                    "content": row["memory_content"],  # 记忆内容
                    "source": row["source_query"],  # 来源查询
                    "importance": row["importance"],  # 重要性评分
                    "access_count": row["access_count"],  # 访问次数
                    "updated_at": row["updated_at"].strftime('%Y-%m-%d %H:%M:%S') if row["updated_at"] else ""  # 更新时间（以 DB 列为准读回）
                }
        except Exception as e:  # 捕获所有异常
            print(f"从数据库加载长期记忆失败: {e}")  # 打印错误信息

    def get_long_term_text(self, query: str = None) -> str:
        """
        需求：将长期记忆格式化为可注入 prompt 的文本。
        思路步骤：
        1. 若长期记忆为空，返回"无长期记忆"
        2. 若传入 query，则只返回与该查询相关的长期记忆
        3. 否则返回全部长期记忆
        4. 用中文分号拼接所有记忆内容

        参数：
            query (str): 若传入，则只返回与该查询相关的长期记忆；否则返回全部

        返回值：
            str: 长期记忆文本（用作 prompt 中的「长期记忆」字段）
        """
        if not self.long_term_memories:  # 若长期记忆字典为空
            return "无长期记忆"  # 返回默认提示文本
        mems = self.get_relevant_long_term(query) if query else list(self.long_term_memories.values())  # 按查询检索或取全部
        if not mems:  # 若匹配结果为空
            return "无相关长期记忆"  # 返回无相关记忆提示
        return "；".join([m["content"] for m in mems])  # 用中文分号拼接所有记忆内容

    def get_relevant_long_term(self, query: str, limit: int = 5) -> list:
        """
        需求：按相关性检索长期记忆：先做城市/偏好词匹配，命中后更新访问次数。
        思路步骤：
        1. 若长期记忆为空，直接返回空列表
        2. 提取查询中的城市名（"去/到/在 + 城市"）
        3. 提取查询中的偏好关键词（座位/舱位/车型）
        4. 遍历长期记忆，匹配城市名或偏好词
        5. 命中后更新访问次数并持久化
        6. 返回匹配结果（最多 limit 条）

        这是「按需注入」的关键：只把与当前查询相关的长期记忆喂给大模型，
        避免无关记忆干扰判断，也降低一次性注入的 token 开销。

        参数：
            query (str): 当前查询文本
            limit (int): 最多返回条数

        返回值：
            list: 命中的长期记忆 dict 列表
        """
        if not self.long_term_memories:  # 若长期记忆字典为空
            return []  # 返回空列表

        # 提取查询中的城市名（简化：取"去/到/在 + 城市"）
        city_match = re.search(r'(?:去|到|在)\s*([一-龥]{2,6}?市?)', query)  # 正则匹配城市名
        query_city = city_match.group(1) if city_match else None  # 提取城市名，未匹配到则为 None
        # 提取查询中的偏好词
        pref_terms = [t for t in ("二等座", "一等座", "商务座", "经济舱",  # 过滤出查询中包含的偏好词
                                  "公务舱", "头等舱", "经济型", "SUV", "豪华型", "MPV") if t in query]

        matched = []  # 初始化匹配结果列表
        for key, mem in self.long_term_memories.items():  # 遍历每条长期记忆
            content = mem["content"]  # 获取记忆内容文本
            if query_city and query_city in content:  # 若查询城市存在于记忆内容中
                matched.append(mem)  # 加入匹配结果
            elif any(t in content for t in pref_terms):  # 若偏好词存在于记忆内容中
                matched.append(mem)  # 加入匹配结果

        # 命中后更新访问次数（用于后续判断记忆活跃度）
        for mem in matched:  # 遍历匹配到的记忆
            self.long_term_memories[mem["key"]]["access_count"] += 1  # 访问次数加1
        # 持久化访问次数变化
        if matched:  # 若有匹配结果
            self.save_long_term_to_db()  # 将更新后的访问次数写入数据库

        return matched[:limit]  # 返回最多 limit 条匹配结果

    # ==================== 当前任务（current_task / 任务上下文） ====================

    def update_task_context(self, task_update: dict):
        """
        需求：更新当前任务上下文。
        思路步骤：
        1. 将传入的任务更新字典合并到当前任务上下文
        """
        self.current_task.update(task_update)  # 合并新的任务上下文字段到当前任务字典

    # ==================== 实体历史（entity_history / 情景记忆） ====================

    def extract_entities(self, intent_type: str, query: str):
        """
        需求：从查询中提取关键实体到历史，并持久化到数据库。
        思路步骤：
        1. 将意图类型、查询文本、时间戳组成实体记录，追加到实体历史列表
        2. 若实体历史超过20条，截断只保留最新的20条
        3. 调用持久化方法将单条实体写入数据库
        """
        self.entity_history.append({  # 追加一条实体记录到历史列表
            "type": intent_type,  # 意图类型（如 weather、train 等）
            "query": query,  # 用户的查询文本
            "timestamp": datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')  # 当前上海时间的完整时间戳
        })
        # 只保留最近20条实体记录
        if len(self.entity_history) > 20:  # 若实体历史超过20条
            self.entity_history = self.entity_history[-20:]  # 截断只保留最新的20条
        # 持久化单条到数据库
        self.save_entity_to_db(intent_type, query)  # 将这条实体记录写入数据库


    def save_entity_to_db(self, intent_type: str, query: str):
        """
        需求：将单条查询实体持久化到数据库。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 获取当前上海时间作为时间戳
        4. 将意图类型、查询内容（JSON 格式）、时间戳插入 query_history 表
        5. 提交事务并关闭游标
        6. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor()  # 创建数据库游标
            now = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')  # 获取当前上海时间
            cursor.execute(  # 插入一条查询历史记录
                "INSERT INTO query_history (username, intent_type, query_content, query_time) "
                "VALUES (%s, %s, %s, %s)",
                (self.username, intent_type, json.dumps({"query": query}, ensure_ascii=False), now)  # 用户名、意图、查询内容JSON、时间
            )
            self._db_conn.commit()  # 提交事务
            cursor.close()  # 关闭游标
        except Exception as e:  # 捕获所有异常
            print(f"保存查询实体到数据库失败: {e}")  # 打印错误信息

    def load_entities_from_db(self, limit: int = 20):
        """
        需求：从数据库加载查询历史，按时间倒序取最近N条。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 按时间倒序查询 query_history 表，限制条数
        4. 将结果翻转为正序
        5. 解析每条记录的 JSON 查询内容，组装为实体历史列表
        6. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor(dictionary=True)  # 创建游标，返回字典格式结果
            cursor.execute(  # 按时间倒序查询当前用户的查询历史记录
                "SELECT intent_type, query_content, query_time FROM query_history "
                "WHERE username = %s ORDER BY query_time DESC LIMIT %s",
                (self.username, limit)  # 用户名、限制返回条数
            )
            rows = cursor.fetchall()  # 获取所有查询结果
            cursor.close()  # 关闭游标
            # 按时间正序排列（倒序取出来后翻转）
            rows.reverse()  # 翻转列表，使其按时间正序排列
            self.entity_history = []  # 重置实体历史列表
            for row in rows:  # 遍历每条记录
                try:
                    query_data = json.loads(row["query_content"])  # 解析 JSON 格式的查询内容
                    raw_query = query_data.get("query", "")  # 取出查询文本
                    query_text = raw_query if isinstance(raw_query, str) else ""  # 非 str 的历史脏数据（如旧版类型漂移写入的 {}）归一为空串
                except Exception:
                    query_text = ""  # 解析失败则置为空字符串
                self.entity_history.append({  # 组装实体记录并追加到列表
                    "type": row["intent_type"],  # 意图类型
                    "query": query_text,  # 查询文本
                    "timestamp": row["query_time"].strftime('%Y-%m-%d %H:%M:%S') if row["query_time"] else ""  # 时间戳
                })
        except Exception as e:  # 捕获所有异常
            print(f"从数据库加载查询历史失败: {e}")  # 打印错误信息

    # ==================== 全局方法（跨模块） ====================

    def get_memory_context(self, query: str = None) -> str:
        """
        需求：分层组装完整记忆上下文，供意图识别等环节一次性注入。
        思路步骤：
        1. 组装工作记忆层：短期对话（最近几轮）
        2. 组装任务层：当前任务上下文（JSON 格式）
        3. 组装语义记忆层：用户偏好 + 长期记忆（按需检索）
        4. 用换行符拼接所有层并返回

        分层结构：
            - 工作记忆层：短期对话（最近几轮）
            - 任务层：当前任务上下文
            - 语义记忆层：用户偏好 + 长期记忆（按需检索）

        对比旧实现（只注入短期对话），新增的语义记忆层让大模型能利用
        跨会话沉淀的用户偏好，实现「记得用户」的长期记忆能力。
        """
        parts = [  # 初始化各层文本列表
            f"【短期对话】\n{self.get_short_term_text()}",  # 工作记忆层：短期对话
            f"【当前任务】\n{json.dumps(self.current_task, ensure_ascii=False) if self.current_task else '无'}",  # 任务层：当前任务上下文
            f"【用户偏好】\n{self.get_profile_text()}",  # 语义记忆层：用户偏好
            f"【长期记忆】\n{self.get_long_term_text(query)}",  # 语义记忆层：长期记忆（按需检索）
        ]
        return "\n".join(parts)  # 用换行符拼接所有层

    def clear(self):
        """
        需求：清空所有记忆，同时清除数据库数据。
        思路步骤：
        1. 重置短期对话列表为空
        2. 重置用户偏好字典为空
        3. 重置当前任务上下文为空
        4. 重置实体历史列表为空
        5. 重置长期记忆字典为空
        6. 调用数据库清除方法清空所有记忆表
        """
        self.short_term_messages = []  # 清空短期对话列表
        self.user_profile = {}  # 清空用户偏好字典
        self.current_task = {}  # 清空当前任务上下文
        self.entity_history = []  # 清空实体历史列表
        self.long_term_memories = {}  # 清空长期记忆字典
        self.clear_all_from_db()  # 清空数据库中所有记忆数据

    def clear_all_from_db(self):
        """
        需求：清空数据库中所有记忆数据。
        思路步骤：
        1. 若数据库连接为 None，直接返回
        2. 确保数据库连接有效
        3. 依次删除 short_term_messages、query_history、user_profiles、long_term_memories 四张表的所有数据
        4. 提交事务并关闭游标
        5. 异常时打印错误信息
        """
        if self._db_conn is None:  # 若未设置数据库连接
            return  # 直接返回
        try:
            self._ensure_db()  # 确保数据库连接有效
            cursor = self._db_conn.cursor()  # 创建数据库游标
            cursor.execute("DELETE FROM short_term_messages WHERE username = %s", (self.username,))  # 清空当前用户的短期对话
            cursor.execute("DELETE FROM query_history WHERE username = %s", (self.username,))  # 清空当前用户的查询历史
            cursor.execute("DELETE FROM user_profiles WHERE username = %s", (self.username,))  # 清空当前用户的偏好
            cursor.execute("DELETE FROM long_term_memories WHERE username = %s", (self.username,))  # 清空当前用户的长期记忆
            self._db_conn.commit()  # 提交事务
            cursor.close()  # 关闭游标
        except Exception as e:  # 捕获所有异常
            print(f"清空数据库记忆失败: {e}")  # 打印错误信息
