"""ragspine.n8n 转换器测试套件：parse / 变量表达式 / n8n→dify / dify→n8n / round-trip。

全程离线确定性：零真实 API、零网络、不依赖 [llm]。fixtures/ 下是手写的真实感 n8n
workflow JSON（linear / branch / unknown）；dify 侧复用 tests/dify/fixtures 的 YAML。
"""
