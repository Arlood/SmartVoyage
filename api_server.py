"""
需求：SmartVoyage FastAPI 后端服务器，提供 REST API 接口

思路步骤：
    1. 导入 FastAPI、Pydantic、uvicorn 等依赖，以及项目内部的 ChatService、Config
    2. 创建 FastAPI 应用实例和全局 ChatService 实例
    3. 定义请求体模型（ChatRequest / ProfileRequest / LoginRequest）
    4. 实现数据库辅助函数：获取 MySQL 连接、校验 token、提取 token
    5. 实现认证端点：登录（/api/login）、获取用户信息（/api/user/info）、登出（/api/logout）
    6. 实现对话端点：普通对话（/api/chat）、流式对话（/api/chat/stream，SSE）
    7. 实现记忆管理端点：获取记忆状态、清空记忆、更新用户偏好
    8. 实现代理信息端点：获取所有代理卡片
    9. 实现静态文件端点：返回 index.html / order.html / tour_order.html
    10. 通过 uvicorn 启动服务，监听 8080 端口

架构说明：
    本模块是 SmartVoyage 系统的 HTTP API 层，基于 FastAPI 构建，运行在 8080 端口。
    它提供以下功能：
    1. 用户认证：登录（POST /api/login）、获取当前用户信息（GET /api/user/info）
    2. 对话接口：普通对话（POST /api/chat）、流式对话（POST /api/chat/stream，SSE）
    3. 记忆管理：获取记忆状态（GET /api/memory）、清空记忆（POST /api/memory/clear）
    4. 代理信息：获取所有代理卡片（GET /api/agents）
    5. 静态文件：返回前端 HTML 页面（GET /）

    认证机制（JWT）：
        登录时从 MySQL users 表验证用户名密码，通过后生成 JWT token（含用户名 + 过期时间）。
        后续请求通过 Authorization: Bearer <token> 头携带 token。
        服务端通过 JWT 签名验证 token 有效性，无需查数据库。
        JWT 配置从 config.yaml 的 jwt 节读取（secret / algorithm / expiration_days）。
        注意：实际项目中登录通常由 SSO / 统一账号服务处理，本模块仅作教学演示。

    前端页面：
        static/index.html —— 聊天界面 + 登录界面
        static/order.html —— 保险/旅游团下单页面
"""
# ==================== 导入依赖 ====================
import json                      # JSON 序列化/反序列化，用于 SSE 事件格式化
import os                        # 操作系统接口，用于拼接文件路径
import sys                       # 系统路径操作，用于把上级目录加入 sys.path
import threading                 # 线程库，用于后台定时清理过期的 ChatService 实例
import uuid                      # UUID 生成器，用于生成唯一的登录 token
from datetime import datetime, timedelta  # 日期时间类，用于 token 过期时间计算
import mysql.connector           # MySQL 驱动，用于直接查 users 表
import jwt                       # PyJWT 库，用于生成和验证 JWT token
from cachetools import TTLCache  # 带 TTL 和 LRU 淘汰的缓存，用于管理 ChatService 实例生命周期

# 把当前文件的上级目录（项目根目录）加入模块搜索路径，保证 SmartVoyage 包可以被正确导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 从 FastAPI 框架导入核心类：FastAPI 应用、HTTPException 异常、Header 依赖、Request 对象
from fastapi import FastAPI, HTTPException, Header, Request
# StaticFiles 用于挂载静态资源目录（本项目未使用，仅导入备用）
from fastapi.staticfiles import StaticFiles
# FileResponse 用于返回文件内容（HTML 页面）
from fastapi.responses import FileResponse
# StreamingResponse 用于返回 SSE 流式响应
from fastapi.responses import StreamingResponse
# BaseModel 用于定义请求体的数据模型（自动校验类型）
from pydantic import BaseModel
# uvicorn 是 ASGI 服务器，负责运行 FastAPI 应用
import uvicorn

# 导入项目内部的 ChatService，封装了与 A2A 代理网络的对话逻辑
from SmartVoyage.chat_service import ChatService
# 导入项目配置类，包含数据库、大模型等所有配置项
from SmartVoyage.config import Config

# 实例化配置对象，读取数据库连接参数等
conf = Config()

# JWT 配置从 Config 读取（优先级：环境变量 > config.yaml > 默认值）
JWT_SECRET = conf.jwt_secret
JWT_ALGORITHM = conf.jwt_algorithm
JWT_EXPIRATION_DAYS = conf.jwt_expiration_days
# 创建 FastAPI 应用实例，设置标题和描述（用于自动生成的 Swagger 文档）
app = FastAPI(title="SmartVoyage API", description="基于 A2A 的旅行智能助手")

# ---------- 用户 ChatService 缓存（LRU + TTL） ----------
# 为什么用缓存而不是每次从数据库重建？
#   ChatService 持有 LLM 连接池、A2A 客户端连接和短期记忆热数据——
#   每次请求都重建代价过高。但如果不设过期，登录过的用户会永久占用内存。
# 设计要点：
#   - maxsize：缓存上限，超过时按 LRU（最近最少使用）淘汰
#   - ttl：单条记录过期秒数，超过后自动失效
#   - 后台清理线程：每 60 秒扫一次，把过期实例的 DB 连接优雅关闭后再移除，
#     避免连接泄漏
_CACHE_MAXSIZE = int(os.getenv("CHAT_CACHE_MAXSIZE", "100"))   # 最多缓存 100 个用户
_CACHE_TTL = int(os.getenv("CHAT_CACHE_TTL", "1800"))          # 单用户 30 分钟不活跃即过期
_user_chat_services = TTLCache(maxsize=_CACHE_MAXSIZE, ttl=_CACHE_TTL)
_cache_lock = threading.Lock()


def _cleanup_loop():
    """后台清理：每 60 秒扫描一次缓存，优雅关闭已过期的 ChatService 的 DB 连接。

    TTLCache 在 get/set 时会自动剔除过期条目，但剔除时不会触发连接关闭——
    如果不主动关闭，被踢出的 ChatService 持有的 MySQL 连接就会泄漏。
    这个线程每 60 秒扫一次，找到"刚才还在、现在已经不在"的条目，
    说明它们已被 TTLCache 自动剔除——然后关闭其 DB 连接避免泄漏。
    """
    while True:
        threading.Event().wait(60)  # 每 60 秒扫一次（比 time.sleep 更易被中断）
        with _cache_lock:
            # 拍快照：此刻缓存里有哪些 (username, svc)
            snapshot = list(_user_chat_services.items())
        for username, svc in snapshot:
            # 用 .get() 触发 TTLCache 的过期检查
            # 如果已过期，TTLCache 会自动剔除并返回 None
            _ = _user_chat_services.get(username)
            # 再次检查：如果 username 不在缓存里了，说明刚被剔除
            if username not in _user_chat_services:
                # 优雅关闭 DB 连接
                try:
                    if getattr(svc, "memory", None) is not None:
                        svc.memory.close()
                except Exception:
                    pass


def get_chat_service(username: str) -> ChatService:
    """获取或创建指定用户的 ChatService 实例。

    多用户记忆隔离的关键：每个 username 拥有独立的 ChatService 实例，
    其内部的 ConversationMemory 按 username 过滤所有 SQL 查询。
    缓存采用 LRU + TTL 策略：
      - 最多缓存 _CACHE_MAXSIZE 个用户（默认 100），超过时踢出最久未用的
      - 单用户 _CACHE_TTL 秒（默认 1800 = 30 分钟）内未被访问则过期
      - 过期后下次访问同一 username 会重建实例，并从 DB 恢复记忆
    """
    with _cache_lock:
        svc = _user_chat_services.get(username)
        if svc is None:
            svc = ChatService(username=username)
            _user_chat_services[username] = svc
        return svc


# 启动后台清理线程（daemon=True 保证主进程退出时自动结束）
threading.Thread(target=_cleanup_loop, daemon=True, name="chat-service-cleanup").start()

# ==================== 请求模型 ====================
# 以下三个类继承自 Pydantic BaseModel，用于自动校验 HTTP 请求体的字段类型

class ChatRequest(BaseModel):
    """
    需求：定义用户发送聊天消息的请求体结构。
    思路步骤：
    1. 继承 BaseModel，声明 message 字段为字符串类型
    2. FastAPI 会自动把请求体 JSON 解析为该对象，并校验 message 必须存在
    """
    # 用户发送的消息文本
    message: str


class ProfileRequest(BaseModel):
    """
    需求：定义更新用户偏好的请求体结构。
    思路步骤：
    1. 继承 BaseModel，声明 profile 字段为字典类型
    2. profile 字典的 key/value 由前端自由定义（如饮食偏好、预算等）
    """
    # 用户偏好信息，以键值对形式传入
    profile: dict


class LoginRequest(BaseModel):
    """
    需求：定义用户登录的请求体结构。
    思路步骤：
    1. 继承 BaseModel，声明 username 和 password 两个字符串字段
    2. FastAPI 自动校验请求体必须包含这两个字段
    """
    # 用户名
    username: str
    # 密码（教学用，明文存储，实际项目应加密）
    password: str


# ==================== 数据库辅助函数 ====================

def get_db_connection():
    """
    需求：创建并返回一个到 MySQL 数据库的新连接。
    思路步骤：
    1. 从 Config 对象中读取 host、user、password、database 等连接参数
    2. 调用 mysql.connector.connect 建立连接并返回
    3. 调用方负责在使用完毕后关闭连接
    """
    # 使用配置中的数据库参数创建 MySQL 连接
    return mysql.connector.connect(
        host=conf.host,                # 数据库主机地址
        user=conf.user,                # 数据库用户名
        password=conf.password,        # 数据库密码
        database=conf.database,        # 要连接的数据库名
        charset='utf8mb4'              # Windows 默认编码可能是 GBK，显式指定 UTF-8 防止中文乱码
    )


def verify_token(token: str) -> str:
    """
    需求：校验前端传来的 JWT token 是否有效，返回对应的用户名。
    思路步骤：
    1. 使用 JWT 密钥解码 token，提取 payload 中的用户名
    2. 如果 token 无效或签名错误，抛出 401 异常
    3. 如果 token 已过期，抛出 401 异常
    4. 校验通过，返回用户名

    参数：
        token (str): 用户请求中携带的 Bearer token（JWT 字符串）

    返回值：
        str: 对应的用户名

    异常：
        HTTPException: token 无效或已过期时抛出 401
    """
    try:
        # 第一步：解码 JWT token，提取 payload
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

        # 第二步：从 payload 中获取用户名
        username = payload.get("username")
        if not username:
            raise HTTPException(status_code=401, detail="token 中缺少用户名")

        # 第三步：校验通过，返回用户名
        return username

    except jwt.ExpiredSignatureError:
        # token 已过期
        raise HTTPException(status_code=401, detail="token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        # token 无效（签名错误、格式错误等）
        raise HTTPException(status_code=401, detail="无效的 token，请重新登录")
    except HTTPException:
        # HTTPException 直接向上抛出，不被吞掉
        raise
    except Exception as e:
        # 其他未知异常统一返回 500
        raise HTTPException(status_code=500, detail=f"校验 token 失败：{str(e)}")


def extract_token(authorization: str = None) -> str:
    """
    需求：从 HTTP 请求的 Authorization 头中解析出 token 字符串。
    思路步骤：
    1. 检查 authorization 参数是否为空，为空则抛出 401
    2. 按空格分割字符串，期望得到 ["Bearer", "<token>"] 两部分
    3. 校验第一部分是否为 "Bearer"（不区分大小写）
    4. 返回第二部分的 token 字符串

    参数：
        authorization (str): HTTP 头，格式为 "Bearer <token>"

    返回值：
        str: token 字符串
    """
    # 第一步：检查是否提供了 Authorization 头
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供 Authorization 头")
    # 第二步：按空格分割，期望格式为 "Bearer xxxxx"
    parts = authorization.split()
    # 第三步：校验格式必须恰好是两部分，且第一部分为 "Bearer"
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authorization 格式错误，应为 'Bearer <token>'")
    # 第四步：返回实际的 token 值
    return parts[1]


# ==================== 认证端点 ====================

@app.post("/api/login")
async def login(request: LoginRequest):
    """
    需求：用户登录接口，验证用户名密码，成功后生成 JWT token。
    思路步骤：
    1. 从请求体中获取用户名和密码
    2. 查询 MySQL users 表，验证用户名和密码是否匹配
    3. 如果不匹配，返回 401 错误
    4. 如果匹配，生成 JWT token（包含用户名和过期时间），有效期 7 天
    5. 返回 token 和用户名给前端

    返回值：
        {"status": "success", "token": "...", "username": "..."}
    """
    try:
        # 第一步：建立数据库连接
        conn = get_db_connection()
        # 创建字典游标
        cursor = conn.cursor(dictionary=True)

        # 第二步：查询 users 表，验证用户名和密码
        cursor.execute(
            "SELECT id, username FROM users WHERE username = %s AND password = %s",
            (request.username, request.password)  # 参数化查询
        )
        # 获取匹配的用户记录
        user = cursor.fetchone()

        # 第三步：如果没有找到用户，说明用户名或密码错误
        if not user:
            cursor.close()       # 关闭游标
            conn.close()         # 关闭连接
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        # 第四步：生成 JWT token
        # payload 中包含用户名和过期时间
        # 注意：PyJWT 要求 iat/exp 使用 UTC 时间，不能用 datetime.now()（本地时间）
        # 否则在东八区会导致 iat 比当前 UTC 时间快 8 小时，触发 ImmatureSignatureError
        now = datetime.utcnow()
        expires_at = now + timedelta(days=JWT_EXPIRATION_DAYS)
        payload = {
            "username": request.username,           # 用户名（用于后续请求识别用户）
            "iat": now,                             # 签发时间
            "exp": expires_at,                      # 过期时间
            "sub": str(user["id"])                  # subject：用户 ID
        }
        # 使用 HMAC-SHA256 签名生成 token
        token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

        # 关闭游标和连接
        cursor.close()
        conn.close()

        # 第五步：返回成功响应，包含 token 和用户名
        return {
            "status": "success",                              # 状态标记
            "token": token,                                    # JWT token
            "username": request.username,                      # 用户名
            "expires_at": expires_at.isoformat()               # 过期时间（ISO 格式字符串）
        }

    except HTTPException:
        # HTTP 异常直接抛出
        raise
    except Exception as e:
        # 其他异常返回 500
        raise HTTPException(status_code=500, detail=f"登录失败：{str(e)}")


@app.get("/api/user/info")
async def get_user_info(authorization: str = Header(None)):
    """
    需求：获取当前登录用户的信息（通过 token 识别身份）。
    思路步骤：
    1. 从请求头中提取 Authorization 字段
    2. 调用 extract_token 解析出 token
    3. 调用 verify_token 校验 token 并获取用户名
    4. 返回用户名信息

    返回值：
        {"status": "success", "data": {"username": "..."}}
    """
    # 第一步：从 Authorization 头中提取 token
    token = extract_token(authorization)
    # 第二步：校验 token，获取对应的用户名
    username = verify_token(token)
    # 第三步：返回用户名
    return {"status": "success", "data": {"username": username}}


@app.post("/api/logout")
async def logout(authorization: str = Header(None)):
    """
    需求：用户登出接口。JWT 是无状态的，服务端无需删除 token。
    思路步骤：
    1. 从请求头中提取并校验 token（确认请求合法）
    2. 返回成功消息（前端收到后清除本地 token 即可）

    注意：JWT 无法在服务端主动失效，过期时间到期后自动失效。
    如需强制失效，需配合 Redis 黑名单机制（本项目未实现）。
    """
    # 第一步：提取 token（不校验，仅确认请求格式合法）
    token = extract_token(authorization)
    # 第二步：返回成功消息（前端应清除本地存储的 token）
    return {"status": "success", "message": "已登出，请清除本地 token"}


# ==================== 对话端点 ====================

@app.post("/api/chat")
async def chat(request: ChatRequest, authorization: str = Header(None)):
    """
    需求：发送用户消息，获取 AI 助手的完整回复（同步接口，需登录）。
    思路步骤：
    1. 提取并校验 token，确认用户身份
    2. 调用 chat_service.chat 发送消息并等待完整回复
    3. 将回复结果返回给前端
    """
    # 第一步：从请求头提取 token
    token = extract_token(authorization)
    # 第二步：校验 token 有效性，获取用户名
    username = verify_token(token)
    # 第三步：获取该用户的 ChatService 实例
    chat_service = get_chat_service(username)
    # 第四步：调用 ChatService 的同步对话方法，获取完整回复
    response = await chat_service.chat(request.message)
    # 第五步：返回成功响应和回复内容
    return {"status": "success", "message": response}


async def sse_generator(message: str, chat_service: ChatService):
    """
    需求：SSE（Server-Sent Events）生成器，逐块流式返回 AI 回复内容。
    思路步骤：
    1. 异步遍历 chat_service.chat_stream 返回的内容块
    2. 如果内容块是字典且 type 为 "card"，按卡片事件格式发送
    3. 否则按普通文本块格式发送
    4. 所有内容块发送完毕后，发送 [DONE] 结束标记

    SSE 事件格式：
    - 文本块：data: {"type": "text", "chunk": "你好"}\\n\\n
    - 卡片推送：data: {"type": "card", "card_type": "insurance", "items": [...]}\\n\\n
    - 结束标记：data: [DONE]\\n\\n
    """
    # 第一步：异步遍历 ChatService 产出的每一个内容块
    async for chunk in chat_service.chat_stream(message):
        # 第二步：判断内容块的类型
        if isinstance(chunk, dict) and chunk.get("type") == "card":
            # 如果是卡片类型（如保险卡片、订单卡片），直接序列化发送
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        else:
            # 如果是普通文本，包装成 {type: "text", chunk: "..."} 格式发送
            text = chunk if isinstance(chunk, str) else str(chunk)  # 确保是字符串
            yield f"data: {json.dumps({'type': 'text', 'chunk': text}, ensure_ascii=False)}\n\n"
    # 第三步：所有内容块发送完毕，发送结束标记
    yield "data: [DONE]\n\n"


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, authorization: str = Header(None)):
    """
    需求：发送用户消息，以 SSE 流式方式逐步返回 AI 回复（需登录）。
    思路步骤：
    1. 提取并校验 token，确认用户身份
    2. 创建 StreamingResponse，使用 sse_generator 作为响应体生成器
    3. 设置 media_type 为 "text/event-stream"，让浏览器识别为 SSE 流
    """
    # 第一步：从请求头提取 token
    token = extract_token(authorization)
    # 第二步：校验 token，获取用户名
    username = verify_token(token)
    # 第三步：获取该用户的 ChatService 实例
    chat_service = get_chat_service(username)
    # 第四步：返回流式响应，使用 sse_generator 逐块产出数据
    return StreamingResponse(sse_generator(request.message, chat_service), media_type="text/event-stream")


# ==================== 记忆管理端点 ====================

@app.get("/api/memory")
async def get_memory(authorization: str = Header(None)):
    """
    需求：获取当前对话的记忆状态（消息历史条数、是否启用等，需登录）。
    思路步骤：
    1. 提取并校验 token
    2. 调用 chat_service.get_memory_state() 获取记忆状态信息
    3. 返回状态数据
    """
    # 第一步：提取 token
    token = extract_token(authorization)
    # 第二步：校验 token，获取用户名
    username = verify_token(token)
    # 第三步：获取该用户的 ChatService 实例
    chat_service = get_chat_service(username)
    # 第四步：获取并返回记忆状态
    return {"status": "success", "data": chat_service.get_memory_state()}


@app.post("/api/memory/clear")
async def clear_memory(authorization: str = Header(None)):
    """
    需求：清空当前对话的所有记忆（历史消息），需登录。
    思路步骤：
    1. 提取并校验 token
    2. 调用 chat_service.clear_memory() 清空记忆
    3. 返回成功消息
    """
    # 第一步：提取 token
    token = extract_token(authorization)
    # 第二步：校验 token，获取用户名
    username = verify_token(token)
    # 第三步：获取该用户的 ChatService 实例
    chat_service = get_chat_service(username)
    # 第四步：清空记忆
    chat_service.clear_memory()
    # 第五步：返回成功消息
    return {"status": "success", "message": "记忆已清空"}


@app.post("/api/memory/profile")
async def update_profile(request: ProfileRequest, authorization: str = Header(None)):
    """
    需求：更新用户偏好信息（如饮食偏好、预算范围等），需登录。
    思路步骤：
    1. 提取并校验 token
    2. 从请求体中获取 profile 字典
    3. 调用 chat_service.update_user_profile 更新偏好
    4. 返回成功消息
    """
    # 第一步：提取 token
    token = extract_token(authorization)
    # 第二步：校验 token，获取用户名
    username = verify_token(token)
    # 第三步：获取该用户的 ChatService 实例
    chat_service = get_chat_service(username)
    # 第四步：更新用户偏好
    chat_service.update_user_profile(request.profile)
    # 第五步：返回成功消息
    return {"status": "success", "message": "用户偏好已更新"}


# ==================== 代理信息端点 ====================

@app.get("/api/agents")
async def get_agents(authorization: str = Header(None)):
    """
    需求：获取系统中所有 A2A 代理的卡片信息（名称、描述、能力等），需登录。
    思路步骤：
    1. 提取并校验 token
    2. 调用 chat_service.get_agent_cards() 获取所有代理卡片
    3. 返回代理信息列表
    """
    # 第一步：提取 token
    token = extract_token(authorization)
    # 第二步：校验 token，获取用户名
    username = verify_token(token)
    # 第三步：获取该用户的 ChatService 实例
    chat_service = get_chat_service(username)
    # 第四步：获取并返回代理卡片信息
    return {"status": "success", "data": chat_service.get_agent_cards()}


# ==================== 静态文件 ====================
@app.get("/")
async def index():
    """
    需求：返回前端聊天页面（index.html）。
    思路步骤：
    1. 计算 static 目录的绝对路径（与当前文件同级的 static 子目录）
    2. 使用 FileResponse 返回 index.html 文件内容
    3. 附加 no-store 缓存头，确保前端改动后浏览器总是拉取最新页面（避免缓存旧 JS）
    """
    # 第一步：拼接 static 目录路径
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    # 第二步：返回 index.html 文件（禁缓存，保证前端更新即时生效）
    return FileResponse(
        os.path.join(static_dir, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )


@app.get("/order")
async def order_page():
    """
    需求：返回保险下单页面（order.html）。
    思路步骤：
    1. 计算 static 目录的绝对路径
    2. 使用 FileResponse 返回 order.html 文件内容
    """
    # 第一步：拼接 static 目录路径
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    # 第二步：返回 order.html 文件（禁缓存）
    return FileResponse(
        os.path.join(static_dir, "order.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )


@app.get("/tour_order")
async def tour_order_page():
    """
    需求：返回旅游团报名页面（tour_order.html）。
    思路步骤：
    1. 计算 static 目录的绝对路径
    2. 使用 FileResponse 返回 tour_order.html 文件内容
    """
    # 第一步：拼接 static 目录路径
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    # 第二步：返回 tour_order.html 文件（禁缓存）
    return FileResponse(
        os.path.join(static_dir, "tour_order.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )


# ==================== 启动服务器 ====================
if __name__ == "__main__":
    # 使用 uvicorn 启动 ASGI 服务器，监听地址和端口从 Config 读取
    uvicorn.run(app, host=conf.api_host, port=conf.api_port, log_level="info")
