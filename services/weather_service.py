"""
需求：天气查询服务 —— 直连和风天气 API 获取实时天气数据。

本模块提供 fetch_weather_from_api 函数，每次查询直接调用和风天气 30 天预报接口，
按日期范围筛选后返回 JSON 字符串。配置统一由 Config 类管理。
"""

# ==================== 导入依赖 ====================
import json                       # JSON 序列化/反序列化
import gzip                       # gzip 解压，用于处理和风天气 API 的压缩响应
import requests                   # HTTP 客户端，用于调用和风天气 REST API
from urllib.parse import urlsplit  # 解析 URL，用于从配置推导和风天气 API Host

from SmartVoyage.config import Config           # 项目配置类
from SmartVoyage.create_logger import logger     # 日志记录器
from SmartVoyage.utils.format import DateEncoder  # JSON 日期编码器

conf = Config()  # 全局配置实例


# ==================== 和风天气 API 常量 ====================
# 城市名称到城市代码的映射表，用于和风天气 API 的 location 参数
# 城市代码来自和风天气城市列表：https://github.com/qwd/LocationList
# CITY_CODES = {
#     "北京": "101010100",           # 北京的城市代码
#     "上海": "101020100",           # 上海的城市代码
#     "杭州": "101210101",           # 杭州的城市代码
#     "南京": "101190101",           # 南京的城市代码
#     "广州": "101280101",           # 广州的城市代码（注意：与杭州代码相同，实际应修正）
#     "深圳": "101280601",           # 深圳的城市代码
#     "成都": "101270101",           # 成都的城市代码
# }

# 和风天气 API Host —— 与天气预报接口共用同一配置来源，避免两处硬编码不一致
HEFENG_HOST = urlsplit(conf.hefeng_base_url).netloc

# 基于城市名获取location_id
def get_location_id_by_city(location_name):
    url = f'https://{HEFENG_HOST}/geo/v2/city/lookup'
    result = requests.get(
        url=url
        , headers={'X-QW-Api-Key': conf.hefeng_api_key}
        , params={
            'location': location_name
        }
    )
    if result.status_code == 200:
        # print(json.dumps(result.json(), indent=2))
        return result.json()['location'][0]['id']
    else:
        print(result.content)
        return ''

# ==================== 天气查询（和风天气 API） ====================

def fetch_weather_from_api(city: str, start_date: str, end_date: str = None) -> str:
    """
    需求：直接从和风天气 API 获取 30 天天气预报数据，按日期范围筛选后返回 JSON 字符串。
    思路步骤：
    1. 从 CITY_CODES 映射表中查找城市代码，找不到则返回错误
    2. 构造请求 URL 和请求头（含 API Key 和 gzip 接受编码）
    3. 发送 GET 请求，处理可能的 gzip 压缩响应
    4. 解析 JSON，检查 API 返回码是否为 "200"
    5. 遍历 daily 数组，按 start_date / end_date 过滤日期范围
    6. 将每天的天气数据格式化为统一字典列表
    7. 返回 JSON 字符串（success / no_data / error）

    参数：
        city (str): 城市名称，如 "北京"
        start_date (str): 开始日期，格式 YYYY-MM-DD
        end_date (str, optional): 结束日期，格式 YYYY-MM-DD

    返回值：
        str: JSON 字符串
    """
    # 第一步：查找城市代码
    location = get_location_id_by_city(city)
    # location = CITY_CODES.get(city)
    # # 如果城市不在支持列表中，返回错误信息
    # if not location:
    #     return json.dumps({
    #         "status": "error",
    #         "message": f"不支持的城市：{city}。目前支持的城市有：{', '.join(CITY_CODES.keys())}"
    #     }, ensure_ascii=False)  # ensure_ascii=False 保证中文正常显示

    # 第二步：构造请求头，包含 API Key 和 gzip 接受编码
    headers = {
        "X-QW-Api-Key": conf.hefeng_api_key,    # 和风天气 API 密钥（从 Config 读取）
        "Accept-Encoding": "gzip"               # 接受 gzip 压缩响应
    }
    # 第三步：拼接完整请求 URL，附带城市代码参数
    url = f"{conf.hefeng_base_url}?location={location}"

    try:
        # 第四步：发送 GET 请求
        response = requests.get(url, headers=headers, timeout=10)
        # 检查 HTTP 状态码，非 200 会抛出异常
        response.raise_for_status()

        # 第五步：获取原始响应内容
        raw_content = response.content
        # 第六步：判断响应是否为 JSON 文本（未压缩）
        if raw_content.startswith(b'{') or raw_content.startswith(b'['):
            data = response.text                # 直接取文本
        # 判断是否为 gzip 压缩格式
        elif response.headers.get('Content-Encoding') == 'gzip':
            data = gzip.decompress(raw_content).decode('utf-8')  # 解压并解码
        else:
            data = response.text                # 其他情况直接取文本

        # 第七步：解析 JSON 数据
        api_result = json.loads(data)

        # 第八步：检查和风天气 API 的返回码，"200" 表示成功
        if api_result.get("code") != "200":
            return json.dumps({
                "status": "error",
                "message": f"和风天气 API 返回错误码：{api_result.get('code')}"
            }, ensure_ascii=False)

        # 第九步：获取每日天气预报数组
        daily_data = api_result.get("daily", [])
        # 如果没有预报数据，返回 no_data
        if not daily_data:
            return json.dumps({
                "status": "no_data",
                "message": "和风天气 API 未返回预报数据。"
            }, ensure_ascii=False)

        # 第十步：遍历每日数据，按日期范围过滤并格式化
        formatted_data = []
        for day in daily_data:
            # 获取当前天的日期字符串
            fx_date = day.get("fxDate", "")
            # 如果日期早于 start_date，跳过
            if start_date and fx_date < start_date:
                continue
            # 如果日期晚于 end_date，后面的就不用看了，直接跳出
            if end_date and fx_date > end_date:
                break

            # 将每天的天气数据格式化为统一字典
            formatted_data.append({
                "city": city,                                        # 城市名称
                "fx_date": fx_date,                                  # 预报日期
                "temp_max": int(day.get("tempMax", 0)),              # 最高温度
                "temp_min": int(day.get("tempMin", 0)),              # 最低温度
                "text_day": day.get("textDay", ""),                  # 白天天气文字
                "text_night": day.get("textNight", ""),              # 夜间天气文字
                "humidity": int(day.get("humidity", 0)),             # 相对湿度
                "wind_dir_day": day.get("windDirDay", ""),           # 白天风向
                "wind_scale_day": day.get("windScaleDay", ""),       # 白天风力等级
                "wind_speed_day": int(day.get("windSpeedDay", 0)),   # 白天风速
                "precip": float(day.get("precip", 0.0)),             # 降水量
                "uv_index": int(day.get("uvIndex", 0)),              # 紫外线指数
                "pressure": int(day.get("pressure", 0)),             # 大气压强
                "vis": int(day.get("vis", 0)),                       # 能见度
                "cloud": int(day.get("cloud", 0)),                   # 云量
                "sunrise": day.get("sunrise", ""),                   # 日出时间
                "sunset": day.get("sunset", ""),                     # 日落时间
                "moonrise": day.get("moonrise", ""),                 # 月出时间
                "moonset": day.get("moonset", ""),                   # 月落时间
                "moon_phase": day.get("moonPhase", ""),              # 月相
                "icon_day": day.get("iconDay", ""),                  # 白天天气图标代码
                "icon_night": day.get("iconNight", ""),              # 夜间天气图标代码
                "wind360_day": int(day.get("wind360Day", 0)),        # 白天风向角度
                "wind360_night": int(day.get("wind360Night", 0)),    # 夜间风向角度
                "wind_dir_night": day.get("windDirNight", ""),       # 夜间风向
                "wind_scale_night": day.get("windScaleNight", ""),   # 夜间风力等级
                "wind_speed_night": int(day.get("windSpeedNight", 0)),  # 夜间风速
                "update_time": api_result.get("updateTime", ""),     # 数据更新时间
            })

        # 第十一步：检查过滤后是否有数据
        if not formatted_data:
            return json.dumps({
                "status": "no_data",
                "message": f"未找到 {city} 在 {start_date} 到 {end_date or start_date} 之间的天气数据。"
            }, ensure_ascii=False)

        # 第十二步：返回成功结果，使用 DateEncoder 处理日期类型
        return json.dumps({"status": "success", "data": formatted_data}, cls=DateEncoder, ensure_ascii=False)

    except requests.RequestException as e:
        # HTTP 请求异常（网络错误、超时等）
        logger.error(f"请求 {city} 和风天气 API 失败: {e}")
        return json.dumps({"status": "error", "message": f"和风天气 API 请求失败：{str(e)}"}, ensure_ascii=False)
    except json.JSONDecodeError as e:
        # JSON 解析异常
        logger.error(f"{city} 和风天气 JSON 解析错误: {e}")
        return json.dumps({"status": "error", "message": f"和风天气 JSON 解析失败：{str(e)}"}, ensure_ascii=False)
    except Exception as e:
        # 其他未知异常
        logger.error(f"和风天气 API 查询出错: {str(e)}")
        return json.dumps({"status": "error", "message": f"和风天气 API 查询出错：{str(e)}"}, ensure_ascii=False)


# result= fetch_weather_from_api("北京", "2026-09-12")
# print(result)


