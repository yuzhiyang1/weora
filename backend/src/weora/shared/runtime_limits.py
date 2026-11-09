"""工具网关与 Harness 传输共用的有界执行契约。"""

MAX_TOOL_CALLS = 64
# 一步可包含请求前、工具结算后及收尾快照，重复日志不计入新增输出预算。
MAX_CHECKPOINT_FRAMES = MAX_TOOL_CALLS * 4
