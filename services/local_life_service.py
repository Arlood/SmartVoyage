"""
需求：本地生活服务实现逻辑 —— 封装高德地图 REST API 调用，提供 8 个地理位置相关工具。

本模块从 mcp_external_server.py 中提取，包含：
- 高德地图 API 配置（KEY、BASE_URL）
- _amap_request：通用高德 API 请求函数
- LocalLifeService 类：8 个本地生活工具方法
"""

# ==================== 导入依赖 ====================
import json                       # JSON 序列化/反序列化
import requests                   # HTTP 客户端，用于调用高德 REST API
import os                         # 操作系统接口，用于读取环境变量

from SmartVoyage.config import Config  # 项目配置类
from SmartVoyage.create_logger import logger  # 日志记录器

conf = Config()  # 全局配置实例

# 高德地图 API 密钥和基础 URL 统一从 Config 读取（config.yaml / 环境变量 / 兜底值）
AMAP_API_KEY = conf.amap_api_key
AMAP_BASE_URL = conf.amap_base_url


def _amap_request(path: str, params: dict) -> dict:
    """
    需求：通用的高德地图 API 请求函数，自动附加 key 和 output 参数，统一处理错误。
    思路步骤：
    1. 向 params 字典中追加 key（API 密钥）和 output=JSON 参数
    2. 拼接完整 URL，发送 GET 请求（超时 10 秒）
    3. 解析 JSON 响应，检查 status 字段是否为 "1"（成功）
    4. 成功则返回完整数据，失败则返回错误信息字典
    5. 捕获所有异常，返回错误信息字典

    参数：
        path (str): API 路径，如 "/v3/geocode/geo"
        params (dict): 请求参数（不含 key）

    返回值：
        dict: API 返回的 JSON 数据
    """
    # 第一步：自动附加 API 密钥和输出格式参数
    params["key"] = AMAP_API_KEY              # 附加 API 密钥
    params["output"] = "JSON"                 # 指定返回 JSON 格式
    # 第二步：拼接完整请求 URL
    url = f"{AMAP_BASE_URL}{path}"
    try:
        # 第三步：发送 GET 请求，超时 10 秒
        resp = requests.get(url, params=params, timeout=10)
        # 第四步：解析 JSON 响应
        data = resp.json()
        # 第五步：检查 API 返回的 status 字段，"1" 表示成功
        if data.get("status") != "1":
            logger.error(f"高德 API 错误: {data.get('info', '未知错误')}")  # 记录错误日志
            return {"status": "error", "message": f"高德 API 返回错误：{data.get('info', '未知错误')}"}
        # 第六步：成功时返回完整数据
        return data
    except Exception as e:
        # 第七步：异常处理，记录日志并返回错误信息
        logger.error(f"高德 API 请求失败: {str(e)}")
        return {"status": "error", "message": f"高德 API 请求失败：{str(e)}"}


# ==================== 本地生活服务类 ====================

class LocalLifeService:
    """
    需求：本地生活服务类 —— 封装高德地图 REST API 调用，提供 8 个地理位置相关工具。
    思路步骤：
    1. geocode：地址文本 → 经纬度坐标（正向地理编码）
    2. regeocode：经纬度坐标 → 结构化地址（逆向地理编码）
    3. search_poi：按关键字搜索 POI（餐厅、酒店、景点等）
    4. search_around：以坐标为中心搜索周边 POI
    5. direction_driving：驾车路径规划
    6. direction_transit：公交路径规划
    7. direction_walking：步行路径规划
    8. direction_bicycling：骑行路径规划
    所有方法返回 JSON 字符串。
    """

    def geocode(self, address: str, city: str = None) -> str:
        """
        需求：地理编码，将地址文本转换为经纬度坐标。
        思路步骤：
        1. 构造请求参数（address + 可选的 city）
        2. 调用 _amap_request 发送请求到 /v3/geocode/geo
        3. 从响应中提取 geocodes 数组，格式化每条结果
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"地理编码: {address}, city={city}")
        # 第一步：构造请求参数
        params = {"address": address}
        if city:
            params["city"] = city                  # 可选的城市参数，提高编码精度

        # 第二步：调用通用请求函数
        data = _amap_request("/v3/geocode/geo", params)
        # 如果请求失败，直接返回错误数据
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取地理编码结果数组
        geocodes = data.get("geocodes", [])
        if not geocodes:
            return json.dumps({"status": "no_data", "message": f"未找到地址的坐标：{address}"}, ensure_ascii=False)

        # 第四步：格式化每条编码结果
        results = []
        for geo in geocodes:
            results.append({
                "formatted_address": geo.get("formatted_address", ""),  # 标准化地址
                "province": geo.get("province", ""),                    # 省份
                "city": geo.get("city", ""),                            # 城市
                "district": geo.get("district", ""),                    # 区县
                "location": geo.get("location", ""),                    # 经纬度坐标
                "level": geo.get("level", "")                           # 匹配级别
            })

        # 返回成功结果
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)

    def regeocode(self, location: str) -> str:
        """
        需求：逆地理编码，将经纬度坐标转换为结构化地址。
        思路步骤：
        1. 构造请求参数（location，格式为 "经度,纬度"）
        2. 调用 _amap_request 发送请求到 /v3/geocode/regeo
        3. 从响应中提取 addressComponent，格式化结果
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"逆地理编码: {location}")
        # 第一步：构造请求参数
        params = {"location": location}

        # 第二步：调用通用请求函数
        data = _amap_request("/v3/geocode/regeo", params)
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取逆地理编码结果
        regeocode = data.get("regeocode", {})
        if not regeocode:
            return json.dumps({"status": "no_data", "message": "逆地理编码失败"}, ensure_ascii=False)

        # 获取地址组件（省/市/区/镇）
        address_component = regeocode.get("addressComponent", {})
        result = {
            "formatted_address": regeocode.get("formatted_address", ""),  # 完整地址
            "province": address_component.get("province", ""),            # 省份
            "city": address_component.get("city", ""),                    # 城市
            "district": address_component.get("district", ""),            # 区县
            "township": address_component.get("township", ""),            # 乡镇/街道
        }

        # 第四步：返回结果
        return json.dumps({"status": "success", "data": result}, ensure_ascii=False)

    def search_poi(self, keywords: str, city: str = None, types: str = None, limit: int = 20) -> str:
        """
        需求：POI 关键字搜索，按关键字搜索餐厅、酒店、景点等兴趣点。
        思路步骤：
        1. 构造请求参数（keywords、city、types、offset）
        2. 调用 _amap_request 发送请求到 /v3/place/text
        3. 从响应中提取 pois 数组，格式化每条 POI
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"POI 搜索: keywords={keywords}, city={city}")
        # 第一步：构造请求参数，offset 最大不超过 25（高德 API 限制）
        params = {"keywords": keywords, "offset": min(limit, 25)}
        if city:
            params["city"] = city                  # 可选的城市限定
        if types:
            params["types"] = types                # 可选的 POI 类型限定

        # 第二步：调用通用请求函数
        data = _amap_request("/v3/place/text", params)
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取 POI 列表
        pois = data.get("pois", [])
        if not pois:
            return json.dumps({"status": "no_data", "message": f"未找到相关地点：{keywords}"}, ensure_ascii=False)

        # 第四步：格式化每条 POI 数据
        results = []
        for poi in pois:
            results.append({
                "name": poi.get("name", ""),                          # POI 名称
                "type": poi.get("type", ""),                          # POI 类型
                "address": poi.get("address", ""),                    # 地址
                "location": poi.get("location", ""),                  # 经纬度
                "province": poi.get("pname", ""),                     # 省份名
                "city": poi.get("cityname", ""),                      # 城市名
                "district": poi.get("adname", ""),                    # 区县名
                "tel": poi.get("tel", ""),                            # 电话
                # 评分在 biz_ext 嵌套字典中，需要安全获取
                "rating": poi.get("biz_ext", {}).get("rating", "") if isinstance(poi.get("biz_ext"), dict) else "",
            })

        # 返回结果
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)

    def search_around(self, location: str, keywords: str, radius: int = 1000, limit: int = 20) -> str:
        """
        需求：POI 周边搜索，以指定坐标为中心，搜索附近餐厅、酒店、景点等。
        思路步骤：
        1. 构造请求参数（location 中心坐标、keywords、radius 搜索半径、offset、按距离排序）
        2. 调用 _amap_request 发送请求到 /v3/place/around
        3. 从响应中提取 pois 数组，格式化每条 POI
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"周边搜索: location={location}, keywords={keywords}, radius={radius}m")
        # 第一步：构造请求参数
        params = {
            "location": location,                 # 中心坐标（格式 "经度,纬度"）
            "keywords": keywords,                 # 搜索关键词
            "radius": radius,                     # 搜索半径（米）
            "offset": min(limit, 25),             # 每页数量，最大 25
            "sortrule": "distance"                # 按距离排序
        }

        # 第二步：调用通用请求函数
        data = _amap_request("/v3/place/around", params)
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取 POI 列表
        pois = data.get("pois", [])
        if not pois:
            return json.dumps({"status": "no_data", "message": f"附近未找到：{keywords}"}, ensure_ascii=False)

        # 第四步：格式化每条 POI 数据
        results = []
        for poi in pois:
            results.append({
                "name": poi.get("name", ""),                          # POI 名称
                "type": poi.get("type", ""),                          # POI 类型
                "address": poi.get("address", ""),                    # 地址
                "location": poi.get("location", ""),                  # 经纬度
                "distance": poi.get("distance", ""),                  # 距中心点距离
                "tel": poi.get("tel", ""),                            # 电话
            })

        # 返回结果
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)

    def direction_driving(self, origin: str, destination: str) -> str:
        """
        需求：驾车路径规划，计算两点之间的驾车路线。
        思路步骤：
        1. 构造请求参数（origin 起点、destination 终点，格式 "经度,纬度"）
        2. 调用 _amap_request 发送请求到 /v3/direction/driving
        3. 从响应中提取 paths 数组，格式化每条路线
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"驾车路线: {origin} → {destination}")
        # 第一步：构造请求参数
        params = {"origin": origin, "destination": destination}
        # 第二步：调用通用请求函数
        data = _amap_request("/v3/direction/driving", params)
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取路线列表
        route = data.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            return json.dumps({"status": "no_data", "message": "未找到驾车路线"}, ensure_ascii=False)

        # 第四步：格式化每条路线
        results = []
        for path in paths:
            results.append({
                "distance": path.get("distance", ""),                 # 距离（米）
                "duration": path.get("duration", ""),                 # 预计耗时（秒）
                "strategy": path.get("strategy", ""),                 # 路线策略
                "steps_count": len(path.get("steps", []))             # 步骤数量
            })

        # 返回结果
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)

    def direction_transit(self, origin: str, destination: str, city: str = "北京") -> str:
        """
        需求：公交路径规划，计算两点之间的公共交通路线。
        思路步骤：
        1. 构造请求参数（origin、destination、city 所在城市）
        2. 调用 _amap_request 发送请求到 /v3/direction/transit/integrated
        3. 从响应中提取 transits 数组，最多取前 5 条方案
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"公交路线: {origin} → {destination}, city={city}")
        # 第一步：构造请求参数
        params = {"origin": origin, "destination": destination, "city": city}
        # 第二步：调用通用请求函数
        data = _amap_request("/v3/direction/transit/integrated", params)
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取公交方案列表
        route = data.get("route", {})
        transits = route.get("transits", [])
        if not transits:
            return json.dumps({"status": "no_data", "message": "未找到公交路线"}, ensure_ascii=False)

        # 第四步：格式化前 5 条公交方案
        results = []
        for transit in transits[:5]:                 # 最多取 5 条方案
            segments = transit.get("segments", [])   # 换乘段列表
            results.append({
                "duration": transit.get("duration", ""),              # 总耗时
                "distance": transit.get("distance", ""),              # 总距离
                "walking_distance": transit.get("walking_distance", ""),  # 步行距离
                "cost": transit.get("cost", ""),                      # 费用
                "nightflag": transit.get("nightflag", ""),            # 是否夜间
                "segments_count": len(segments),                      # 换乘段数
            })

        # 返回结果
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)

    def direction_walking(self, origin: str, destination: str) -> str:
        """
        需求：步行路径规划，计算两点之间的步行路线。
        思路步骤：
        1. 构造请求参数（origin、destination）
        2. 调用 _amap_request 发送请求到 /v3/direction/walking
        3. 从响应中提取 paths 数组，格式化每条路线
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"步行路线: {origin} → {destination}")
        # 第一步：构造请求参数
        params = {"origin": origin, "destination": destination}
        # 第二步：调用通用请求函数
        data = _amap_request("/v3/direction/walking", params)
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取步行路线列表
        route = data.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            return json.dumps({"status": "no_data", "message": "未找到步行路线"}, ensure_ascii=False)

        # 第四步：格式化每条路线
        results = []
        for path in paths:
            results.append({
                "distance": path.get("distance", ""),                 # 距离（米）
                "duration": path.get("duration", ""),                 # 预计耗时（秒）
                "steps_count": len(path.get("steps", []))             # 步骤数量
            })

        # 返回结果
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)

    def direction_bicycling(self, origin: str, destination: str) -> str:
        """
        需求：骑行路径规划，计算两点之间的骑行路线。
        思路步骤：
        1. 构造请求参数（origin、destination）
        2. 调用 _amap_request 发送请求到 /v3/direction/bicycling
        3. 从响应中提取 paths 数组，格式化每条路线
        4. 返回 JSON 字符串
        """
        # 记录日志
        logger.info(f"骑行路线: {origin} → {destination}")
        # 第一步：构造请求参数
        params = {"origin": origin, "destination": destination}
        # 第二步：调用通用请求函数
        data = _amap_request("/v3/direction/bicycling", params)
        if data.get("status") != "1":
            return json.dumps(data, ensure_ascii=False)

        # 第三步：提取骑行路线列表
        route = data.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            return json.dumps({"status": "no_data", "message": "未找到骑行路线"}, ensure_ascii=False)

        # 第四步：格式化每条路线
        results = []
        for path in paths:
            results.append({
                "distance": path.get("distance", ""),                 # 距离（米）
                "duration": path.get("duration", ""),                 # 预计耗时（秒）
            })

        # 返回结果
        return json.dumps({"status": "success", "data": results}, ensure_ascii=False)


# 实例化服务类
local_life_service = LocalLifeService()          # 高德本地生活服务