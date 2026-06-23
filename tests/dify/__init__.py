"""ragspine.dify 编译器测试套件（P0-P6）：parse / IR / codegen / optimize / 门面 + CLI。

全程离线确定性：LLM 节点用 MockProvider，零真实 API、零网络。fixtures/ 下是手写的
小样例 Dify 工作流 YAML（seq / parallel / branch / iteration）。
"""
