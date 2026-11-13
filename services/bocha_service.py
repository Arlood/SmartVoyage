"""
需求：博查搜索 API 实现逻辑，包含联网搜索调用、结果格式化和教学样例数据。

本模块从 mcp_external_server.py 中提取，包含：
- 博查搜索 API 配置（KEY、URL）
- _call_bocha_api：调用博查 Web Search API 获取真实搜索结果
- _format_search_results：将搜索结果格式化为 Markdown 文本
- _get_sample_data：教学用样例数据，API 不可用时的降级兜底
"""

# ==================== 导入依赖 ====================
import os                       # 操作系统接口，用于读取环境变量
import requests                 # HTTP 客户端，用于调用博查 REST API

from SmartVoyage.config import Config  # 项目配置类
from SmartVoyage.create_logger import logger  # 日志记录器

conf = Config()  # 全局配置实例

# 博查搜索 API 配置 —— 统一从 Config 读取（config.yaml / 环境变量 / 兜底值）
# 博查 AI Web Search API —— 用于旅游攻略联网搜索
# 注册地址：https://open.bochaai.com，注册后在「API KEY 管理」处创建 Key
BOCHA_API_KEY = conf.bocha_api_key
BOCHA_API_URL = conf.bocha_api_url


def _call_bocha_api(query: str, count: int = 5) -> list:
    """
    需求：调用博查搜索 Web Search API，获取网页搜索结果列表。
    思路步骤：
    1. 构造请求头，包含 Bearer token 认证和 Content-Type
    2. 构造请求体（query、count、summary=True）
    3. 发送 POST 请求（超时 15 秒）
    4. 解析 JSON 响应，检查 code 是否为 200
    5. 从 data.webPages.values 中提取结果列表并返回

    参数：
        query (str): 搜索关键词
        count (int): 返回结果数量，默认 5

    返回值：
        list: 搜索结果列表，每项包含 name、url、snippet 等字段
    """
    # 第一步：构造请求头
    headers = {
        "Authorization": f"Bearer {BOCHA_API_KEY}",  # Bearer token 认证
        "Content-Type": "application/json",           # 请求体为 JSON 格式
    }

    # 第二步：构造请求体
    payload = {
        "query": query,              # 搜索关键词
        "count": count,              # 返回结果数量
        "summary": True,             # 要求返回详细摘要
    }

    # 第三步：发送 POST 请求
    response = requests.post(BOCHA_API_URL, headers=headers, json=payload, timeout=15)
    # 检查 HTTP 状态码，非 200 会抛出异常
    response.raise_for_status()

    # 第四步：解析 JSON 响应
    result = response.json()

    # 博查 API 返回格式：{"code": 200, "data": {"webPages": {"value": [...]}}}
    # 注意：结果数组字段是 value（单数），不是 values（复数）
    # 检查业务状态码
    if result.get("code") != 200:
        logger.warning(f"博查 API 返回异常：code={result.get('code')}")
        return []                    # 返回空列表

    # 第五步：从嵌套结构中提取结果列表
    web_pages = result.get("data", {}).get("webPages", {})
    values = web_pages.get("value", [])   # value（单数）—— 官方返回的字段名
    return values                    # 返回搜索结果列表


def _format_search_results(query: str, results: list, source: str) -> str:
    """
    需求：将旅游攻略搜索结果格式化为可读的 Markdown 文本。
    思路步骤：
    1. 遍历搜索结果列表，为每条结果生成标题、来源、摘要、链接
    2. 截断超过 300 字符的摘要
    3. 用分隔线组合所有结果，加上头部说明信息
    4. 返回格式化后的完整文本

    参数：
        query (str): 搜索关键词
        results (list): 搜索结果列表
        source (str): 数据来源说明（"博查联网搜索" / "教学样例数据"）

    返回值：
        str: 格式化后的搜索结果文本
    """
    # 第一步：遍历结果，逐条格式化
    output = []
    for i, item in enumerate(results, 1):           # 从 1 开始编号
        title = item.get("name", "无标题")          # 标题
        url = item.get("url", "")                   # 链接
        site_name = item.get("siteName", "")        # 来源网站名
        snippet = item.get("summary") or item.get("snippet", "")  # 摘要（优先 summary）

        # 第二步：截断过长的摘要
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."         # 超过 300 字符截断并加省略号

        # 第三步：构造每条结果的 Markdown 文本
        lines = [f"### {i}. {title}"]               # 标题行
        if site_name:
            lines.append(f"**来源**：{site_name}")   # 来源
        if snippet:
            lines.append(f"**摘要**：{snippet}")     # 摘要
        if url:
            lines.append(f"**链接**：{url}")         # 链接

        # 将每条结果的各行用换行拼接
        output.append("\n".join(lines))

    # 第四步：组合完整的搜索结果文本
    result_text = (
        f"旅游攻略搜索结果（关键词：{query}，共找到 {len(results)} 条结果）\n"
        f"（数据来源：{source}）\n"
        + "\n" + "-" * 50 + "\n"                   # 分隔线
        + "\n\n".join(output)                        # 各结果之间空一行
        + "\n" + "-" * 50                           # 尾部分隔线
    )
    return result_text


def _get_sample_data(query: str, count: int = 5) -> list:
    """
    需求：返回教学用样例数据，模拟旅游攻略搜索的结果格式。
    思路步骤：
    1. 预定义多个热门旅游城市（丽江/成都/三亚）的样例攻略数据
    2. 遍历 query 中是否包含这些城市关键词
    3. 如果匹配到则返回对应城市的数据
    4. 如果没有匹配到，默认返回丽江的样例数据
    5. 按 count 参数截断返回

    当博查 API 不可用（API Key 未配置、调用失败等）时，使用此函数返回样例数据，
    保证教学演示可以正常进行。

    参数：
        query (str): 搜索关键词
        count (int): 返回结果数量

    返回值：
        list: 样例搜索结果列表
    """
    # 第一步：定义各城市的样例数据
    samples = {
        "丽江": [
            {"name": "丽江3天2夜超全攻略｜避坑指南+美食推荐",
             "url": "https://www.mafengwo.cn/gonglve/lijiang-3days.html",
             "siteName": "马蜂窝",
             "snippet": "Day1：抵达丽江→入住古城民宿→晚上逛四方街、大水车。Day2：玉龙雪山一日游（建议提前订大索道票）→蓝月谷拍照。Day3：束河古镇悠闲逛→拉市海骑马体验。避坑提示：古城内银饰谨慎购买，玉龙雪山注意防寒。"},
            {"name": "玉龙雪山保姆级攻略｜如何抢到索道票？",
             "url": "https://www.ctrip.com/guide/yulong-mountain.html",
             "siteName": "携程攻略",
             "snippet": "索道票提前7天在'丽江旅游集团'公众号预约，早上7点放票。建议穿冲锋衣或羽绒服，山上温度低。蓝月谷最佳拍照时间是上午10点前，光线柔和。氧气瓶山下买更便宜，山上贵一倍。"},
            {"name": "丽江必吃美食清单｜本地人推荐",
             "url": "https://you.qunar.com/lijiang-food.html",
             "siteName": "去哪儿攻略",
             "snippet": "必吃：腊排骨火锅、黑山羊火锅、鸡豆凉粉、纳西烤鱼。推荐店铺：阿婆情腊排骨、云雪丽餐厅、88号小吃店。古城内价格偏高，建议去新城或束河古镇吃更实惠。"},
            {"name": "丽江住宿推荐｜古城内外怎么选？",
             "url": "https://www.mafengwo.cn/hotel/lijiang-guide.html",
             "siteName": "马蜂窝",
             "snippet": "古城内：氛围好，但价格偏高，夜晚较吵。古城南门附近：性价比高，步行可达古城。束河古镇：安静悠闲，适合度假。大研古镇周边：交通便利，适合短期游。"},
            {"name": "云南旅游避坑指南｜丽江篇",
             "url": "https://you.qunar.com/yunnan-tips.html",
             "siteName": "去哪儿攻略",
             "snippet": "不要跟路边的拉客走，容易被宰。出租车打表，不打表就换车。玉龙雪山不用买氧气瓶，山下药店几块钱一瓶。古城酒吧一条街体验即可，消费较高。"},
        ],
        "成都": [
            {"name": "成都5天4夜深度游攻略｜吃喝玩乐全覆盖",
             "url": "https://www.mafengwo.cn/gonglve/chengdu-5days.html",
             "siteName": "马蜂窝",
             "snippet": "Day1：春熙路→太古里→IFS爬墙熊猫打卡。Day2：大熊猫繁育研究基地（建议早上7点入园）→宽窄巷子。Day3：武侯祠→锦里→人民公园喝盖碗茶。Day4：都江堰→青城山一日游。Day5：建设路小吃街扫荡→返程。"},
            {"name": "成都火锅避坑指南｜本地人私藏店铺",
             "url": "https://www.ctrip.com/guide/chengdu-hotpot.html",
             "siteName": "携程攻略",
             "snippet": "必吃：大龙燚、小龙坎、蜀大侠、电台巷。避坑：景区附近的火锅店大多又贵又不正宗。微辣=中辣，不吃辣的朋友记得点鸳鸯锅。推荐配菜：毛肚、鸭肠、黄喉、红糖糍粑。"},
            {"name": "成都亲子游推荐｜带娃必去的地方",
             "url": "https://you.qunar.com/chengdu-family.html",
             "siteName": "去哪儿攻略",
             "snippet": "大熊猫基地是必去景点，小朋友最爱。成都海昌极地海洋公园适合半日游。欢乐谷适合大一点的孩子。宽窄巷子可以体验掏耳朵、糖画等传统手艺。"},
            {"name": "成都网红打卡地TOP10",
             "url": "https://www.mafengwo.cn/chengdu-checkin.html",
             "siteName": "马蜂窝",
             "snippet": "IFS爬墙熊猫、太古里、东郊记忆、建设路小吃街、九眼桥酒吧街、春熙路、宽窄巷子、锦里、武侯祠、人民公园。"},
            {"name": "成都周边一日游｜都江堰+青城山",
             "url": "https://www.ctrip.com/guide/dujiangyan.html",
             "siteName": "携程攻略",
             "snippet": "从成都坐高铁30分钟到都江堰。上午游览都江堰水利工程，下午登青城山。建议穿舒适的运动鞋。山上的道观可以休息喝茶。"},
        ],
        "三亚": [
            {"name": "三亚亲子游攻略｜带娃必看",
             "url": "https://www.mafengwo.cn/gonglve/sanya-family.html",
             "siteName": "马蜂窝",
             "snippet": "推荐酒店：亚特兰蒂斯（水世界适合小朋友）。必玩：蜈支洲岛（海水清澈，适合浮潜）。避坑：天涯海角性价比低，可以跳过。海鲜去第一市场买，找附近加工店处理。"},
            {"name": "三亚5天自由行攻略｜酒店+景点全推荐",
             "url": "https://www.ctrip.com/guide/sanya-5days.html",
             "siteName": "携程攻略",
             "snippet": "Day1：亚龙湾海滩+酒店泳池。Day2：蜈支洲岛一日游。Day3：南山寺+天涯海角。Day4：呀诺达热带雨林。Day5：免税店购物+返程。"},
            {"name": "三亚美食攻略｜海鲜怎么吃不踩坑",
             "url": "https://you.qunar.com/sanya-food.html",
             "siteName": "去哪儿攻略",
             "snippet": "第一市场买海鲜+附近加工店是最实惠的吃法。推荐：和乐蟹、基围虾、芒果螺。清补凉是海南特色甜品，必尝。椰子鸡火锅也是本地特色。"},
            {"name": "三亚免税店购物攻略",
             "url": "https://www.mafengwo.cn/sanya-duty-free.html",
             "siteName": "马蜂窝",
             "snippet": "海棠湾免税城是最大的免税购物中心。每人每年10万元免税额度。建议提前下载cdf海南免税APP查看价格。热门品牌经常断货，建议早去。"},
            {"name": "三亚潜水攻略｜新手怎么选",
             "url": "https://www.ctrip.com/guide/sanya-diving.html",
             "siteName": "携程攻略",
             "snippet": "蜈支洲岛潜水体验最好，水质清澈。亚龙湾也有潜水项目，价格稍便宜。新手建议体验潜（有教练一对一）。注意：耳压平衡很重要，下水前认真听讲。"},
        ],
    }

    # 第二步：遍历样例数据，匹配查询关键词
    matched_samples = []
    for keyword, items in samples.items():
        # 如果关键词（如“三亚”）出现在查询文本中
        if keyword in query:
            # 使用该城市的样例数据
            matched_samples = items
            # 找到第一个匹配就停止
            break

    # 第三步：如果没有匹配到任何城市，默认使用丽江的数据
    if not matched_samples:
        matched_samples = samples["丽江"]

    # 第四步：按 count 截断并返回
    return matched_samples[:count]

# rs = _call_bocha_api("丽江旅游")
# print(rs)
#
# md = _format_search_results("丽江旅游", rs, source="博查联网搜索")
# print(md)