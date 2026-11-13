"""
需求：集中管理 SmartVoyage 项目的所有配置项，包括大模型、数据库、日志、意图路由、天气源等。
      所有模块通过 Config() 实例获取统一配置，避免硬编码分散在各处。

配置优先级：环境变量 > config.yaml > 代码中的兜底默认值

思路步骤：
1. 加载项目根目录下的 config.yaml 配置文件
2. 提供 _get() 方法实现三级优先级：环境变量 → YAML 值 → 兜底默认值
3. 在 __init__ 中初始化所有配置项为属性
4. 大模型配置：API 地址、密钥、模型名称（含分级：premium / flash）
5. 数据库配置：host / user / password / database
6. 第三方 API 密钥和 URL（和风天气、高德地图、博查搜索、飞常准）
7. 业务配置：意图映射、温度参数、天气数据源、复杂任务开关等
8. 加载 .env 文件：本地开发的密钥等敏感配置从 .env 读取（环境变量优先级最高）
"""

# os 模块：用于路径拼接和环境变量读取
import os

# yaml 模块：用于加载配置文件
import yaml

# dotenv 模块：加载项目根目录的 .env 文件到环境变量（可选依赖）
# 未安装 python-dotenv 时静默降级 —— Docker 场景由 docker-compose 注入环境变量，无需 .env 文件
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# SmartVoyage 项目根目录 —— 本文件（config.py）所在目录即为项目根
# 后续用 os.path.join(project_root, ...) 拼接日志、SQL 等文件路径
project_root = os.path.dirname(os.path.abspath(__file__))

# 加载 .env 文件（若存在）—— 本地开发时从 .env 读取密钥等敏感配置
# override=False：不覆盖已存在的环境变量，保证 Docker 注入的变量优先级更高
if load_dotenv is not None:
    load_dotenv(os.path.join(project_root, '.env'))


# 定义配置类 —— 集中管理所有配置项
class Config:
    """
    需求：将项目所有配置项集中在一个类中管理，方便统一修改和环境切换。
    配置优先级：环境变量 > config.yaml > 代码中的兜底默认值

    思路步骤：
    1. 加载 config.yaml 配置文件到 self._yaml 字典
    2. _get(key, env_var, default) 实现三级优先级
    3. 大模型配置：API 地址、密钥、模型名称（model_premium / model_flash 分级）
    4. 数据库配置：host / user / password / database
    5. 第三方 API 密钥和 URL（hefeng / amap / bocha / variflight）
    6. 日志配置：log_file 路径
    7. 意图映射：intent 字典，将用户意图关键词映射到 A2A 代理名称
    8. 其他配置：temperature、enable_complex_task、business_mcp_url
    9. 数据库连接参数统一由 _get() 读取（host / user / password / database），供各模块直接使用
    """

    def __init__(self):
        # ========== 加载 YAML 配置文件 ==========
        yaml_path = os.path.join(project_root, 'config.yaml')
        if os.path.exists(yaml_path):
            with open(yaml_path, 'r', encoding='utf-8') as f:
                self._yaml = yaml.safe_load(f) or {}
        else:
            self._yaml = {}

        # ========== 大模型配置 ==========
        # 支持任意「OpenAI 兼容」服务，切换供应商只需改 base_url + api_key + model 三项
        # （改 config.yaml 的 llm 节，或设置对应环境变量）。常见供应商 base_url 示例：
        #   阿里云百炼 DashScope：https://dashscope.aliyuncs.com/compatible-mode/v1
        #   DeepSeek            ：https://api.deepseek.com/v1
        #   OpenAI              ：https://api.openai.com/v1
        #   硅基流动 SiliconFlow ：https://api.siliconflow.cn/v1
        #   本地 Ollama         ：http://localhost:11434/v1
        self.base_url = self._get(
            'llm.base_url', env_var='LLM_BASE_URL',
            default='https://maas.whu.edu.cn/v1'
        )
        # API 密钥 —— 从对应供应商平台获取（格式通常为 sk-xxx）
        self.api_key = self._get(
            'llm.api_key', env_var='LLM_API_KEY',
            default=''  # 请在 .env 中设置 LLM_API_KEY
        )
        # 模型分级配置 —— 不同任务使用不同模型，兼顾效果和速度
        # premium：复杂推理任务（规划、景点生成），需要较强的推理能力和创造力
        # flash：简单任务（意图识别、参数提取、结果总结），追求响应速度
        self.model_name = self._get('llm.model_name', env_var='LLM_MODEL', default='deepseek-flash')
        self.model_premium = self._get('llm.model_premium', env_var='LLM_MODEL_PREMIUM', default='deepseek-flash')
        self.model_flash = self._get('llm.model_flash', env_var='LLM_MODEL_FLASH', default='deepseek-flash')

        # ========== 数据库配置 ==========
        # MySQL 连接参数 —— 用于天气数据、租车数据等本地存储
        self.host = self._get('database.host', env_var='DB_HOST', default='localhost')
        self.user = self._get('database.user', env_var='DB_USER', default='smart_voyage')
        self.password = self._get('database.password', env_var='DB_PASSWORD', default='')  # 请在 .env 中设置 DB_PASSWORD
        self.database = self._get('database.name', env_var='DB_NAME', default='travel_rag')

        # ========== 第三方 API 密钥 ==========
        # 和风天气（30 天预报）
        self.hefeng_api_key = self._get(
            'api_keys.hefeng_weather', env_var='HEFENG_API_KEY',
            default=''  # 请在 .env 中设置 HEFENG_API_KEY
        )
        # 高德地图（本地生活 8 个工具）
        self.amap_api_key = self._get(
            'api_keys.amap', env_var='AMAP_API_KEY',
            default=''  # 请在 .env 中设置 AMAP_API_KEY
        )
        # 博查搜索（旅游攻略联网搜索，未配置时降级为教学样例数据）
        self.bocha_api_key = self._get(
            'api_keys.bocha_search', env_var='BOCHA_API_KEY',
            default=''  # 请在 .env 中设置 BOCHA_API_KEY
        )
        # 飞常准（航班查询 MCP，未配置时航班工具不可用）
        self.variflight_api_key = self._get(
            'api_keys.variflight', env_var='VARIFLIGHT_API_KEY',
            default=''  # 请在 .env 中设置 VARIFLIGHT_API_KEY
        )

        # ========== 第三方 API 地址 ==========
        self.hefeng_base_url = self._get(
            'api_urls.hefeng_weather', default='https://m7487r6ych.re.qweatherapi.com/v7/weather/30d'
        )
        self.amap_base_url = self._get(
            'api_urls.amap', default='https://restapi.amap.com'
        )
        self.bocha_api_url = self._get(
            'api_urls.bocha_search', default='https://api.bochaai.com/v1/web-search'
        )
        self.variflight_mcp_url = self._get(
            'api_urls.variflight_mcp', default='https://ai.variflight.com/servers/aviation/mcp'
        )

        # ========== 日志配置 ==========
        # 日志文件路径 —— 项目根目录/logs/app.log
        self.log_file = os.path.join(project_root, 'logs', 'app.log')

        # ========== 意图映射字典 ==========
        # 将用户意图关键词映射到对应的 A2A 代理名称
        # ChatService 根据此字典决定把任务分发给哪个代理
        self.intent = {
            "weather": "WeatherQueryAssistant",      # 天气查询 → 天气代理
            "train": "TicketAssistant",              # 火车票查询 → 票务代理
            "flight": "TicketAssistant",             # 机票查询 → 票务代理（飞常准 MCP）
            "flight_ticket": "TicketAssistant",      # 航班查询 → 票务代理
            "car_rental": "TripAssistant",           # 租车 → 行程代理
            "tour_group": "TripAssistant",           # 旅游团 → 行程代理
            "insurance": "TripAssistant",            # 保险 → 行程代理
            "trip_order": "TripAssistant",           # 行程单 → 行程代理
            "attraction": "TripAssistant",           # 景点 → 行程代理
            "local_life": "TripAssistant",           # 本地生活 → 行程代理
            "travel_guide": "TripAssistant",         # 旅游攻略 → 行程代理
        }

        # ========== LLM 温度参数 ==========
        # temperature 控制输出随机性：0.0 = 确定性最高，1.0 = 最随机
        # 0.1 保证输出稳定可控，适合信息查询场景
        self.temperature = float(self._get('business.temperature', default=0.1))

        # ========== 复杂任务开关 ==========
        # True  ：启用"规划 + ReAct 拆解"流程（多意图有依赖时自动拆解步骤，更智能但更慢）
        # False ：关闭复杂任务逻辑（所有意图直接串行执行，更快，适合简单场景）
        val = self._get('business.enable_complex_task', default=True)
        self.enable_complex_task = val if isinstance(val, bool) else str(val).lower() == 'true'

        # ========== 商业 MCP 服务地址 ==========
        # 租车 + 保险 + 旅游团查询的统一入口 —— 由业务团队通过 Docker 独立部署
        # 学生只需修改此处即可连接到商业 MCP 服务
        self.business_mcp_url = self._get(
            'business.business_mcp_url', env_var='BUSINESS_MCP_URL',
            default='http://localhost:8005/mcp'
        )

        # ========== JWT 认证配置 ==========
        # JWT 密钥：生产环境必须修改为随机字符串（建议 32 字节以上）
        self.jwt_secret = self._get('jwt.secret', env_var='JWT_SECRET', default='smartvoyage-dev-secret-key-change-in-production')
        self.jwt_algorithm = self._get('jwt.algorithm', default='HS256')
        self.jwt_expiration_days = int(self._get('jwt.expiration_days', default=7))

        # ========== 服务端口配置 ==========
        self.api_host = self._get('ports.api_host', env_var='API_HOST', default='0.0.0.0')
        self.api_port = int(self._get('ports.api_port', env_var='API_PORT', default=8080))
        self.mcp_host = self._get('ports.mcp_host', env_var='MCP_HOST', default='0.0.0.0')
        self.mcp_port = int(self._get('ports.mcp_port', env_var='MCP_PORT', default=8002))
        self.a2a_host = self._get('ports.a2a_host', env_var='A2A_HOST', default='0.0.0.0')
        self.weather_port = int(self._get('ports.weather_port', env_var='WEATHER_PORT', default=5005))
        self.ticket_port = int(self._get('ports.ticket_port', env_var='TICKET_PORT', default=5006))
        self.trip_port = int(self._get('ports.trip_port', env_var='TRIP_PORT', default=5007))

        # ========== 服务地址配置（A2A 子代理）==========
        self.weather_a2a_url = self._get('a2a_urls.weather', env_var='WEATHER_A2A_URL', default='http://localhost:5005')
        self.ticket_a2a_url = self._get('a2a_urls.ticket', env_var='TICKET_A2A_URL', default='http://localhost:5006')
        self.trip_a2a_url = self._get('a2a_urls.trip', env_var='TRIP_A2A_URL', default='http://localhost:5007')

        # ========== 外部 MCP 服务地址 ==========
        self.external_mcp_url = self._get('external_mcp_url', env_var='EXTERNAL_MCP_URL', default='http://localhost:8002/mcp')
        self.amap_mcp_url = self._get('amap_mcp_url', env_var='AMAP_MCP_URL')

    def _get(self, key, env_var=None, default=None):
        """
        三级优先级读取配置：环境变量 > config.yaml > 兜底默认值

        参数：
            key (str): YAML 中的键路径，支持点号分隔（如 'llm.api_key'）
            env_var (str, optional): 对应的环境变量名
            default: 兜底默认值

        返回：
            配置值
        """
        # 第一优先级：环境变量
        if env_var and (val := os.environ.get(env_var)) is not None:
            return val

        # 第二优先级：config.yaml
        val = self._yaml
        for part in key.split('.'):
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
                break
        if val is not None:
            return val

        # 第三优先级：兜底默认值
        return default


# 模块独立运行时的测试代码 —— 打印配置概览（数据库密码脱敏）
if __name__ == '__main__':
    c = Config()
    print("日志路径:", c.log_file)
    print("数据库配置:", (c.host, c.user, '******' if c.password else '(未配置)', c.database))
    print("大模型 API:", c.base_url)
    print("和风 API Key:", c.hefeng_api_key[:6] + "****" if c.hefeng_api_key else "(未配置)")
    # 输出示例：('localhost', 'smart_voyage', '******', 'travel_rag')
