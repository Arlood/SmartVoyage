"""
需求：实现数据格式化工具，处理 MySQL 查询结果中非标准类型（date、datetime、timedelta、Decimal）的 JSON 序列化问题。
      MCP 工具返回的结果需要转为 JSON 字符串，但 MySQL 驱动返回的 date/datetime/Decimal 等类型不是 JSON 原生支持的，
      必须先转为字符串或浮点数，否则 json.dumps() 会抛出 TypeError。

思路步骤：
1. 导入必要的模块和库（json、date、datetime、timedelta、Decimal）
2. 实现 default_encoder 函数 —— 将单个非标准对象转为 JSON 兼容类型（字符串 / 浮点数）
3. 创建 DateEncoder 类 —— 继承 json.JSONEncoder，在序列化整个对象/列表时自动调用转换逻辑
4. 重写 DateEncoder.default 方法 —— 逐一判断对象类型并做对应转换
"""

# json 模块：提供 JSON 编码器基类 JSONEncoder
import json
# date / datetime：MySQL 查询返回的日期类型；timedelta：时间差类型
from datetime import date, datetime, timedelta
# Decimal：MySQL 中 DECIMAL 列返回的高精度数值类型，JSON 不支持
from decimal import Decimal


def default_encoder(obj):  # 定义格式化函数，将单个非标准类型对象转为 JSON 兼容类型
    """
    需求：将单个 Python 对象（date / datetime / timedelta / Decimal）转为 JSON 可序列化的值。
    思路步骤：
    1. 判断对象是否为 datetime → 格式化为 'YYYY-MM-DD HH:MM:SS' 字符串
    2. 判断对象是否为 date → 格式化为 'YYYY-MM-DD' 字符串
    3. 判断对象是否为 timedelta → 转为字符串表示（如 '1 day, 2:30:00'）
    4. 判断对象是否为 Decimal → 转为 float，以兼容 JSON 数值
    5. 以上都不是 → 原样返回
    """
    if isinstance(obj, datetime):  # datetime 必须放在 date 前面判断（datetime 是 date 的子类）
        return obj.strftime('%Y-%m-%d %H:%M:%S')  # 返回带时间的格式化字符串
    if isinstance(obj, date):  # 纯日期对象，只返回日期部分
        return obj.strftime('%Y-%m-%d')  # 返回日期格式化字符串
    if isinstance(obj, timedelta):  # 时间差对象，直接转字符串
        return str(obj)  # 例如 '1 day, 2:30:00'
    if isinstance(obj, Decimal):  # 高精度数值，转为浮点数以兼容 JSON
        # TODO 更优的方案 str(obj) —— 保留字符串可避免浮点精度损失
        return float(obj)  # 转为浮点数
    return obj  # 其他类型（str、int、float、None 等）原样返回


# 定义自定义 JSON 编码器类，继承自 json.JSONEncoder
# 用途：在 json.dumps(obj, cls=DateEncoder) 时，遇到非标准类型会自动调用 default() 方法
class DateEncoder(json.JSONEncoder):
    """
    需求：自定义 JSON 编码器，使 json.dumps() 能自动处理 date / datetime / timedelta / Decimal 类型。
    思路步骤：
    1. 继承 json.JSONEncoder 基类
    2. 重写 default() 方法 —— 该方法在遇到 JSON 不能序列化的对象时被调用
    3. 在 default() 中逐一判断类型并做转换
    4. 不能识别的类型交给 super().default() 抛出标准 TypeError
    """
    def default(self, obj):  # 重写 default 方法，处理序列化时的默认对象转换
        """
        需求：将非标准 JSON 类型转为可序列化值。
        思路步骤：
        1. 判断是否为 date / datetime → 格式化为字符串
        2. 判断是否为 timedelta → 转为字符串
        3. 判断是否为 Decimal → 转为浮点数
        4. 以上都不是 → 调用父类默认方法（抛出 TypeError）
        """
        if isinstance(obj, (date, datetime)):  # 同时处理 date 和 datetime
            # datetime 是 date 子类，要先判断 datetime 以返回带时间的字符串
            return obj.strftime('%Y-%m-%d %H:%M:%S') if isinstance(obj, datetime) else obj.strftime('%Y-%m-%d')
        if isinstance(obj, timedelta):  # 时间差对象转为字符串
            return str(obj)  # 例如 '2 days, 3:45:00'
        if isinstance(obj, Decimal):  # 高精度数值转为浮点数
            return float(obj)  # JSON 兼容的数值类型
        return super().default(obj)  # 未识别的类型交给父类处理（抛出 TypeError）
