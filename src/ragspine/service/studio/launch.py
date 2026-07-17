"""launch-session 注册表：CLI serve 登记工作流 → Studio 前端凭不透明 token 自动加载。

隐私不变量：session id 由 secrets.token_urlsafe 生成，不含任何工作流内容、文件路径或
凭据，故进入 URL/query string 的只有不透明 token；session 内容（name/yaml）绝不进
observability trace/log。注册表为进程内内存态且有界（FIFO 最多 8 个），不是持久化
存储，也不是执行入口——消费端点只读返回 YAML，绝不执行工作流。
"""

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass

# PRD 要求 identifier 与 session 都有界：最多同时保留 8 个 launch session，FIFO 淘汰最旧。
_MAX_SESSIONS = 8


@dataclass(frozen=True)
class LaunchSession:
    """一次 serve 启动会话：不透明 id + 展示名 + Dify DSL YAML 文本。"""

    session_id: str
    name: str
    yaml: str


class LaunchSessionRegistry:
    """内存态、线程安全、FIFO 有界的 launch-session 注册表。

    FastAPI sync route 跑在线程池，CLI 主线程注册、请求线程读取，故用 threading.Lock
    保护；token 由 secrets.token_urlsafe(16) 生成（22 字符 URL-safe，碰撞概率可忽略，
    仍显式查重以保证唯一）。
    """

    def __init__(self, *, max_sessions: int = _MAX_SESSIONS) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, LaunchSession] = OrderedDict()
        self._max_sessions = max_sessions

    def register(self, *, name: str, yaml: str) -> LaunchSession:
        """生成不透明 token 并登记 session；超出容量时 FIFO 淘汰最旧。"""
        with self._lock:
            session_id = secrets.token_urlsafe(16)
            while session_id in self._sessions:
                session_id = secrets.token_urlsafe(16)
            session = LaunchSession(session_id=session_id, name=name, yaml=yaml)
            self._sessions[session_id] = session
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
            return session

    def get(self, session_id: str) -> LaunchSession | None:
        """按 token 取回 session；未知 token 返回 None（由调用侧整形成 404）。"""
        with self._lock:
            return self._sessions.get(session_id)
