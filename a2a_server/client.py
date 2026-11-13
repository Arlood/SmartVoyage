import json

from python_a2a import A2AClient
# todo 放入 服务端的地址即可
client = A2AClient("http://127.0.0.1:5005")

# todo 可以通过A2AClient拿到server端的信息
print(f"[主控客户端日志] 获取服务端信息: {client.agent_card}")

# 预订火车票
query = "查询北京明天的天气"
# query = "后天南京去北京的车票"
# query = "南京3天旅行规划"
print(f"[主控客户端日志]  -> '{query}'")
result = client.ask(query)
print(f"[主控客户端日志] 结果: {result}")
print("类型：", type(result))