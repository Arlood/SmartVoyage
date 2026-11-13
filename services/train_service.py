"""
需求：12306 火车票查询服务的实现逻辑。

本模块从 mcp_external_server.py 中提取，包含：
- _query_train_from_12306：使用 py12306 库直连 12306 查询火车票余票
"""

# ==================== 导入依赖 ====================
import json                       # JSON 序列化/反序列化
from py12306 import get_tickets, resolve_station  # py12306 火车票查询和站点解析函数

from SmartVoyage.create_logger import logger     # 日志记录器
from SmartVoyage.utils.format import DateEncoder  # JSON 日期编码器


def _query_train_from_12306(departure_city: str, arrival_city: str, query_date: str,
                            seat_type_filter: str = None) -> str:
    """
    需求：使用 py12306 库直接查询 12306 真实火车票余票信息，返回 JSON 字符串。
    思路步骤：
    1. 使用 resolve_station 将出发/到达城市名转换为站点代码
    2. 调用 get_tickets 查询指定日期的所有车次
    3. 遍历每个车次的每个座位类型，按需过滤座位
    4. 将余票数量统一转换为整数（"有"/"充足" → -1 表示充足，"无"/"0" → 0）
    5. 构造结果字典列表，序列化为 JSON 返回

    参数：
        departure_city: 出发城市（中文，如 "上海"）
        arrival_city: 到达城市（中文，如 "北京"）
        query_date: 查询日期，格式 "YYYY-MM-DD"
        seat_type_filter: 座位类型过滤（可选，如 "二等座"）

    返回：
        JSON 格式字符串
    """
    # 记录查询日志
    logger.info(f"12306 查询: {departure_city} -> {arrival_city}, 日期: {query_date}")

    try:
        # 第一步：将出发城市名解析为站点代码
        src_code = resolve_station(departure_city)
        # 将到达城市名解析为站点代码
        dst_code = resolve_station(arrival_city)

        # 第二步：检查站点代码是否解析成功
        if not src_code or not dst_code:
            logger.warning(f"无法解析站点代码: {departure_city} -> {src_code}, {arrival_city} -> {dst_code}")
            return json.dumps(
                {"status": "no_data", "message": f"无法识别城市名称：{departure_city} 或 {arrival_city}，请确认城市名是否正确。"},
                ensure_ascii=False
            )

        # 第三步：调用 py12306 查询车票信息
        tickets = get_tickets(query_date, src_code, dst_code)

        # 第四步：检查是否查到车次
        if not tickets:
            return json.dumps(
                {"status": "no_data", "message": "未找到符合条件的火车票，请确认查询条件。"},
                ensure_ascii=False
            )

        # 第五步：遍历所有车次和座位类型，构造结果列表
        data_list = []
        for ticket in tickets:
            # 遍历当前车次的每种座位类型
            for seat in ticket.seats:
                # 如果指定了座位类型过滤，且不匹配则跳过
                if seat_type_filter and seat.name != seat_type_filter:
                    continue

                # 第六步：解析余票数量，统一转为整数
                available = seat.available         # 获取余票字段值
                if isinstance(available, int):
                    # 已经是整数，直接使用
                    remaining = available
                elif isinstance(available, str):
                    # 字符串类型，需要判断含义
                    if available in ("有", "充足"):
                        remaining = -1             # -1 表示票量充足
                    elif available in ("无", "0"):
                        remaining = 0              # 0 表示无票
                    else:
                        try:
                            remaining = int(available)  # 尝试转为整数
                        except ValueError:
                            remaining = -1         # 无法解析时默认为充足
                else:
                    remaining = -1                 # 其他类型默认为充足

                # 将每条记录构造为字典
                data_list.append({
                    "departure_city": departure_city,                                      # 出发城市
                    "arrival_city": arrival_city,                                          # 到达城市
                    "departure_time": f"{query_date} {ticket.depart}",                     # 出发时间
                    "arrival_time": f"{query_date} {ticket.arrive}",                       # 到达时间
                    "train_number": ticket.train,                                          # 车次编号
                    "seat_type": seat.name,                                                # 座位类型名称
                    "price": seat.price,                                                   # 票价
                    "remaining_seats": remaining                                           # 余票数量
                })

        # 第七步：检查过滤后是否有数据
        if not data_list:
            return json.dumps(
                {"status": "no_data", "message": "未找到符合条件的火车票，请确认查询条件。"},
                ensure_ascii=False
            )

        # 第八步：记录成功日志，返回结果
        logger.info(f"12306 查询成功，返回 {len(data_list)} 条记录")
        return json.dumps(
            {"status": "success", "data": data_list},
            cls=DateEncoder, ensure_ascii=False  # 使用 DateEncoder 处理可能的日期类型
        )

    except Exception as e:
        # 异常处理：记录日志并返回错误信息
        logger.error(f"12306 查询异常: {str(e)}")
        return json.dumps(
            {"status": "error", "message": f"12306查询失败: {str(e)}"},
            ensure_ascii=False
        )
#
# result =_query_train_from_12306("北京", "上海", "2026-09-13")
# print(result)