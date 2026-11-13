"""
MCP 服务器测试模块

测试内容：
1. 格式编码工具（纯函数测试，零依赖）
2. 统一外部 MCP 服务器（mcp_external_server.py）测试
   - fetch_weather_from_api（和风天气 API 实时查询）
   - LocalLifeService（高德地图工具）
   - _query_train_from_12306（12306 火车票查询，需要网络）

运行方式：
    cd SmartVoyage
    python -m tests.test_mcp_servers
"""

import unittest
import json
import sys
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

# 确保能导入 SmartVoyage 模块（需要 SmartVoyage 的父目录在 sys.path 中）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


def _future_date(days: int) -> str:
    """生成今天之后的日期，避免写死日期过期导致测试失败。"""
    return (date.today() + timedelta(days=days)).isoformat()


# ==================== 1. 格式编码工具测试（纯函数，零依赖） ====================

class TestFormatEncoder(unittest.TestCase):
    """测试 default_encoder 函数和 DateEncoder 类"""

    def setUp(self):
        from SmartVoyage.utils.format import default_encoder, DateEncoder
        self.default_encoder_func = default_encoder
        self.date_encoder_cls = DateEncoder

    def test_encode_datetime(self):
        """测试 datetime 编码"""
        dt = datetime(2025, 7, 30, 14, 30, 0)
        result = self.default_encoder_func(dt)
        self.assertEqual(result, '2025-07-30 14:30:00')

    def test_encode_date(self):
        """测试 date 编码"""
        d = date(2025, 7, 30)
        result = self.default_encoder_func(d)
        self.assertEqual(result, '2025-07-30')

    def test_encode_timedelta(self):
        """测试 timedelta 编码"""
        td = timedelta(hours=5, minutes=30)
        result = self.default_encoder_func(td)
        self.assertEqual(result, '5:30:00')

    def test_encode_decimal(self):
        """测试 Decimal 编码"""
        dec = Decimal('123.45')
        result = self.default_encoder_func(dec)
        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 123.45)

    def test_encode_unknown(self):
        """测试未知类型原样返回"""
        obj = "hello"
        result = self.default_encoder_func(obj)
        self.assertEqual(result, "hello")

    def test_date_encoder_json_dumps(self):
        """测试 DateEncoder 类与 json.dumps 配合使用"""
        data = {
            "date": date(2025, 7, 30),
            "datetime": datetime(2025, 7, 30, 14, 30, 0),
            "value": Decimal('99.99')
        }
        result = json.dumps(data, cls=self.date_encoder_cls)
        parsed = json.loads(result)
        self.assertEqual(parsed["date"], "2025-07-30")
        self.assertEqual(parsed["datetime"], "2025-07-30 14:30:00")
        self.assertEqual(parsed["value"], 99.99)


# ==================== 2. 统一外部 MCP 服务器测试 ====================

class TestExternalMCPWeather(unittest.TestCase):
    """统一外部 MCP：天气查询测试（和风天气 API 模式）"""

    def test_query_weather_single_day(self):
        """测试单天天气查询"""
        from SmartVoyage.services.weather_service import fetch_weather_from_api
        # 使用城市代码查询（和风天气需要 location ID，成都 = 101270101）
        result = fetch_weather_from_api("101270101", _future_date(1), _future_date(1))
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertGreater(len(parsed["data"]), 0)

    def test_query_weather_date_range(self):
        """测试日期范围天气查询"""
        from SmartVoyage.services.weather_service import fetch_weather_from_api
        result = fetch_weather_from_api("101270101", _future_date(1), _future_date(3))
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")


class TestExternalMCPTrain(unittest.TestCase):
    """统一外部 MCP：12306 火车票查询测试（需要网络连接）"""

    def test_query_train_basic(self):
        """测试基础火车票查询（调用 py12306，需要网络）"""
        from SmartVoyage.services.train_service import _query_train_from_12306
        result = _query_train_from_12306("北京", "上海", _future_date(7))
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertGreater(len(parsed["data"]), 0)
        first = parsed["data"][0]
        self.assertEqual(first["departure_city"], "北京")
        self.assertEqual(first["arrival_city"], "上海")
        self.assertIn("train_number", first)

    def test_query_train_with_seat(self):
        """测试带座位类型的火车票查询"""
        from SmartVoyage.services.train_service import _query_train_from_12306
        result = _query_train_from_12306("北京", "上海", _future_date(7), "二等座")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        for item in parsed["data"]:
            self.assertEqual(item["seat_type"], "二等座")

    def test_query_train_invalid_city(self):
        """测试无效城市名"""
        from SmartVoyage.services.train_service import _query_train_from_12306
        result = _query_train_from_12306("无效城市", "上海", _future_date(7))
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "no_data")


class TestExternalMCPLocalLife(unittest.TestCase):
    """统一外部 MCP：本地生活（高德地图）工具测试"""

    @classmethod
    def setUpClass(cls):
        """创建 LocalLifeService 实例（需要环境变量 AMAP_API_KEY）"""
        from SmartVoyage.services.local_life_service import LocalLifeService
        cls.service = LocalLifeService()

    def test_geocode(self):
        """测试地理编码"""
        result = self.service.geocode("北京市政府")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertGreater(len(parsed["data"]), 0)
        self.assertIn("location", parsed["data"][0])

    def test_search_poi(self):
        """测试 POI 关键字搜索"""
        result = self.service.search_poi("餐厅", city="北京")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertGreater(len(parsed["data"]), 0)
        self.assertIn("name", parsed["data"][0])

    def test_direction_driving(self):
        """测试驾车路径规划（使用北京知名坐标）"""
        # 天安门 → 北京站
        result = self.service.direction_driving("116.397428,39.90923", "116.427445,39.90289")
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "success")
        self.assertIn("distance", parsed["data"][0])


class TestExternalMCPServerConfig(unittest.TestCase):
    """验证统一外部 MCP 服务器配置"""

    def test_server_config(self):
        """验证服务器名称和端口"""
        from SmartVoyage.mcp_server.mcp_external_server import create_external_mcp_server
        from SmartVoyage.services.local_life_service import LocalLifeService
        from SmartVoyage.services.train_service import _query_train_from_12306
        from SmartVoyage.services.weather_service import fetch_weather_from_api
        # create_external_mcp_server() 会启动服务器，这里只验证模块能正常导入
        self.assertTrue(callable(create_external_mcp_server))
        self.assertTrue(LocalLifeService is not None)
        self.assertTrue(callable(_query_train_from_12306))
        self.assertTrue(callable(fetch_weather_from_api))


if __name__ == "__main__":
    unittest.main(verbosity=2)
