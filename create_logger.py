"""
需求：为 SmartVoyage 项目创建和配置日志记录器，同时输出到控制台（INFO 级别）和文件（DEBUG 级别）。
      所有模块通过 from SmartVoyage.create_logger import logger 获取全局日志实例。

思路步骤：
1. 导入必要的模块（logging、os、Config）
2. 定义 setup_logger 函数 —— 配置日志记录器（级别、格式、处理器）
3. 创建日志文件夹（如果不存在则自动创建）
4. 获取并配置日志记录器（设置 DEBUG 级别、关闭 propagate 防止重复输出）
5. 定义统一日志格式（名称 + 时间 + 级别 + 消息）
6. 创建控制台处理器（StreamHandler）—— 输出 INFO 及以上级别到终端
7. 创建文件处理器（FileHandler）—— 输出 DEBUG 及以上级别到日志文件
8. 将处理器添加到日志记录器（先检查 handlers 列表，避免重复添加）
9. 初始化并返回配置好的日志记录器
10. 创建项目的全局日志记录器实例 logger，供其他模块导入使用
"""

# logging 模块：Python 标准日志库
import logging
# os 模块：用于创建日志文件夹
import os

# 导入配置类，获取日志文件路径
from SmartVoyage.config import Config


def setup_logger(name, log_file='logs/app.log'):
    """
    需求：创建并配置一个日志记录器，同时输出到控制台和文件。
    思路步骤：
    1. 创建日志所在目录（os.makedirs，exist_ok=True 表示目录已存在时不报错）
    2. 获取指定名称的 Logger 实例（logging.getLogger）
    3. 设置 Logger 级别为 DEBUG（记录所有级别日志）
    4. 设置 propagate=False（阻止日志向上传播到 root logger，避免重复输出）
    5. 定义日志格式：名称 - 时间 - 级别 - 消息
    6. 创建控制台处理器 StreamHandler，级别 INFO（终端只显示 INFO 及以上）
    7. 创建文件处理器 FileHandler，级别 DEBUG（文件记录所有级别）
    8. 检查 logger.handlers 是否为空，避免重复添加处理器
    9. 返回配置好的 Logger 实例

    :param name: 日志记录器名称（通常用模块名，如 'SmartVoyage'）
    :param log_file: 日志文件路径，默认为 'logs/app.log'
    :return: 配置好的 logging.Logger 实例
    """
    # 创建日志文件夹（os.path.dirname 取文件所在目录，exist_ok=True 避免目录已存在时报错）
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # 获取指定名称的日志记录器（同名多次调用返回同一个实例）
    logger = logging.getLogger(name)
    # 设置记录器级别为 DEBUG —— 所有级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）的日志都会处理
    logger.setLevel(logging.DEBUG)
    # 关键！关闭日志传播 —— 防止日志向上传播到 root logger 导致重复输出
    logger.propagate = False

    # 定义日志格式：记录器名称 - 时间 - 日志级别 - 消息内容
    formatter = logging.Formatter('%(name)s - %(asctime)s - %(levelname)s - %(message)s')

    # 创建控制台处理器 —— 将日志输出到终端（标准输出）
    console_handler = logging.StreamHandler()
    # 为控制台处理器设置日志格式
    console_handler.setFormatter(formatter)
    # 控制台只输出 INFO 及以上级别（DEBUG 级别不显示在终端，减少干扰）
    # 注意：处理器的级别必须 ≥ 记录器的级别，否则低级别日志会被处理器过滤掉
    console_handler.setLevel(logging.INFO)

    # 创建文件处理器 —— 将日志写入文件（mode='a' 表示追加模式，不清空旧日志）
    file_handler = logging.FileHandler(filename=log_file, encoding="utf-8", mode="a")
    # 为文件处理器设置日志格式
    file_handler.setFormatter(formatter)
    # 文件记录 DEBUG 及以上级别 —— 方便排查问题时查看完整日志
    file_handler.setLevel(logging.DEBUG)

    # 将处理器添加到日志记录器中
    # 先检查 handlers 列表是否为空 —— 防止同一 logger 被多次 setup 导致处理器重复
    if not logger.handlers:
        logger.addHandler(console_handler)  # 添加控制台处理器
        logger.addHandler(file_handler)  # 添加文件处理器

    # 返回配置好的日志记录器
    return logger


# 创建项目全局日志记录器实例 —— 其他模块通过 from SmartVoyage.create_logger import logger 导入使用
# 日志文件路径从 Config 中读取（默认为 项目根目录/logs/app.log）
logger = setup_logger('SmartVoyage', Config().log_file)
