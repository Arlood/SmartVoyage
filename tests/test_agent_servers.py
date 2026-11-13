"""
Agent 服务器测试模块

测试内容：
1. AgentCard 定义验证（纯属性检查，零依赖）
2. Agent 服务器集成测试（需要 MCP 服务器运行 + LLM API 可访问）

运行方式：
    cd SmartVoyage
    python -m tests.test_agent_servers

前置条件（集成测试）：
    1. 统一外部 MCP 服务器已启动（8002 端口）
    2. LLM API 可访问（配置了有效的 LLM_API_KEY）
    3. 商业 MCP 服务可访问（TripAgent 双 MCP 测试时需要）
"""

import unittest
import sys
import os

# 确保能导入 SmartVoyage 模块（需要 SmartVoyage 的父目录在 sys.path 中）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


# ==================== 1. AgentCard 定义测试（纯属性检查，零依赖） ====================

class TestWeatherAgentCard(unittest.TestCase):
    """测试天气 Agent 的代理卡片定义"""

    def test_agent_card_basic(self):
        """验证代理卡片基本信息"""
        from SmartVoyage.a2a_server.weather_server import agent_card

        self.assertEqual(agent_card.name, "WeatherQueryAssistant")
        self.assertIn("天气", agent_card.description)
        self.assertEqual(agent_card.url, "http://localhost:5005")
        self.assertEqual(len(agent_card.skills), 1)

    def test_weather_skill_examples(self):
        """验证天气技能示例"""
        from SmartVoyage.a2a_server.weather_server import agent_card

        skill = agent_card.skills[0]
        self.assertIn("weather", skill.name.lower())
        self.assertIsNotNone(skill.description)
        self.assertGreater(len(skill.examples), 0)


class TestTicketAgentCard(unittest.TestCase):
    """测试火车票 Agent 的代理卡片定义（重构后只保留火车票技能）"""

    def test_agent_card_basic(self):
        """验证代理卡片基本信息"""
        from SmartVoyage.a2a_server.ticket_server import agent_card

        self.assertEqual(agent_card.name, "TicketAssistant")
        self.assertEqual(agent_card.url, "http://localhost:5006")
        # 重构后只有 1 个技能：query train tickets
        self.assertEqual(len(agent_card.skills), 1)

    def test_has_train_query_skill(self):
        """验证包含火车票查询技能"""
        from SmartVoyage.a2a_server.ticket_server import agent_card

        skill_names = [s.name for s in agent_card.skills]
        self.assertTrue(any('train' in n.lower() for n in skill_names),
                        f"应包含火车票技能，实际技能: {skill_names}")

    def test_no_flight_or_concert_skill(self):
        """验证已删除机票和演唱会技能"""
        from SmartVoyage.a2a_server.ticket_server import agent_card

        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertFalse(any('flight' in n for n in skill_names), "不应包含机票技能")
        self.assertFalse(any('concert' in n for n in skill_names), "不应包含演唱会技能")


class TestTripAgentCard(unittest.TestCase):
    """测试行程 Agent 的代理卡片定义（三 MCP 架构下 8 个技能）"""

    def test_agent_card_basic(self):
        """验证代理卡片基本信息"""
        from SmartVoyage.a2a_server.trip_server import agent_card

        self.assertEqual(agent_card.name, "TripAssistant")
        self.assertEqual(agent_card.url, "http://localhost:5007")
        # 三 MCP 架构下有 8 个技能（含旅游攻略联网搜索）
        self.assertEqual(len(agent_card.skills), 8)

    def test_has_travel_guide_skill(self):
        """验证包含旅游攻略联网搜索技能"""
        from SmartVoyage.a2a_server.trip_server import agent_card

        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('travel' in n or 'guide' in n for n in skill_names),
                        f"应包含旅游攻略搜索技能，实际技能: {skill_names}")

    def test_has_car_rental_skill(self):
        """验证包含租车搜索技能"""
        from SmartVoyage.a2a_server.trip_server import agent_card

        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('car' in n for n in skill_names),
                        f"应包含租车技能，实际技能: {skill_names}")

    def test_has_insurance_and_tour_skills(self):
        """验证包含保险和旅游团查询技能"""
        from SmartVoyage.a2a_server.trip_server import agent_card

        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('insurance' in n for n in skill_names), "缺少保险技能")
        self.assertTrue(any('tour' in n for n in skill_names), "缺少旅游团技能")

    def test_has_recommend_skill(self):
        """验证包含主动推荐技能"""
        from SmartVoyage.a2a_server.trip_server import agent_card

        skill_names = [s.name.lower() for s in agent_card.skills]
        self.assertTrue(any('recommend' in n for n in skill_names), "缺少主动推荐技能")


# ==================== 2. Agent 服务器集成测试 ====================

class TestWeatherAgentIntegration(unittest.TestCase):
    """集成测试：天气 Agent 服务器"""

    def test_handle_task_weather_query(self):
        """测试天气 Agent 完整查询流程"""
        import asyncio
        from SmartVoyage.a2a_server.weather_server import query_weather

        result = asyncio.run(query_weather("成都 2026-05-01 天气"))
        self.assertEqual(result["status"], "success")
        self.assertIn("成都", result["message"])

    def test_weather_server_instance(self):
        """验证天气服务器实例化"""
        from SmartVoyage.a2a_server.weather_server import WeatherQueryServer
        server = WeatherQueryServer()
        self.assertEqual(server.agent_card.name, "WeatherQueryAssistant")


class TestTicketAgentIntegration(unittest.TestCase):
    """集成测试：火车票 Agent 服务器"""

    def test_handle_task_ticket_query(self):
        """测试火车票 Agent 完整查询流程"""
        import asyncio
        from SmartVoyage.a2a_server.ticket_server import query_train

        result = asyncio.run(query_train("北京 到 上海 火车票 2026-08-15"))
        self.assertEqual(result["status"], "success")
        self.assertIn("北京", result["message"])

    def test_ticket_server_instance(self):
        """验证火车票服务器实例化"""
        from SmartVoyage.a2a_server.ticket_server import TicketQueryServer
        server = TicketQueryServer()
        self.assertEqual(server.agent_card.name, "TicketAssistant")


class TestTripAgentIntegration(unittest.TestCase):
    """集成测试：行程 Agent 服务器"""

    def test_handle_task_trip_query(self):
        """测试行程 Agent 完整查询流程（需要统一外部 MCP + 商业 MCP）"""
        import asyncio
        from SmartVoyage.a2a_server.trip_server import query_trip

        result = asyncio.run(query_trip("成都租车"))
        self.assertEqual(result["status"], "success")
        self.assertIn("成都", result["message"])

    def test_trip_server_instance(self):
        """验证行程服务器实例化"""
        from SmartVoyage.a2a_server.trip_server import TripQueryServer
        server = TripQueryServer()
        self.assertEqual(server.agent_card.name, "TripAssistant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
