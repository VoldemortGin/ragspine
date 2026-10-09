"""对象库后端缝:Protocol、数据类与异常(sqlite 对象库设计稿 §8.1)。

一个 ``ObjectBackend`` 承载一个 store 根目录的全部持久化原语:内容寻址对象、
stage-cache 条目、``current-*`` 指针、可变 records(document-tree)、漂移 pin 与事务作用域。
一个 ``ModelCacheBackend`` 承载模型缓存三件套(requests / responses / contexts)与跨进程 claim。

两个实现:``files.FileBackend``(今天的文件布局,字节不变)与 ``sqlite.SqliteBackend``。
本包只定义机制;store 层的校验 / 自愈 / 先到先得语义仍由各 store 自己绑定(PR-2/3 接线)。
"""

from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

BackendKind = Literal["files", "sqlite", "staged"]


@dataclass(frozen=True, slots=True)
class StageEntry:
    """一条 stage-cache 条目:信封摘要、信封字节,以及(预留的)内联产物。

    ``envelope`` 必须 sha256 到 ``envelope_digest``(ADR 0029 Amendment 1 的指针不变量)。
    ``product`` 是 ADR 0029 Amendment 2 的内联小产物(即 outcome.artifact 的字节,
    必须 sha256 到信封里 artifact 的 digest);FileBackend 的第三段指针格式与
    ``document_store.split_stage_pointer`` 逐字节一致。
    """

    envelope_digest: str
    envelope: bytes
    product: bytes | None = None


@dataclass(frozen=True, slots=True)
class PinToken:
    """一次 ``pin`` 的漂移凭据:只含摘要与整数标记,从不含内容。

    ``marks`` 的含义由产生它的后端决定(files: 布局 + size + mtime_ns;
    sqlite: data_version + db size + db mtime_ns),调用方只原样传回 ``pin_unchanged``。
    """

    digest: str
    backend: BackendKind
    marks: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    """一次 claim 的持有者(ADR 0023 的持有者 JSON 字段;从不含 prompt / key / 正文)。"""

    host: str
    pid: int
    process: str
    created_at: float
    lease_seconds: int


class StoreBusy(RuntimeError):
    """写者租约被一个可能还活着的持有者占用;本次写入不进行(原因码 ``store_busy``)。"""


class BackendUnavailable(RuntimeError):
    """显式选择的后端在该目录上不可用(探测失败);绝不静默回退。"""


class BackendSchemaError(BackendUnavailable):
    """db 的 ``application_id`` / ``user_version`` 不是本代码可写的版本(版本门)。"""


class StoreConflict(ValueError):
    """不可变条目首写胜出后的冲突:已有条目完好且是另一份字节。"""


class DamagedEntry(ValueError):
    """条目存在但已损坏(缺失字节、截断或摘要不符);读路径拒绝,写路径修复。"""


@runtime_checkable
class ObjectBackend(Protocol):
    """一个 store 根目录的持久化原语;实现必须保持 digest / 指纹 / 发布 id 不变。"""

    kind: BackendKind
    root: Path

    def get_object(self, digest: str) -> bytes | None:
        """按摘要读回并校验;不存在 → ``None``,存在但不是其摘要 → ``DamagedEntry``。"""
        ...

    def get_content(self, digest: str) -> bytes | None:
        """按摘要读回并校验,**含内联 stage 产物**(ADR 0029 Amendment 2 的完整读顺序:
        已知内联位置 → 对象 → 内联产物扫描/索引)。不存在 → ``None``;只找到损坏的
        副本 → ``DamagedEntry``。"""
        ...

    def read_existing(self, digest: str) -> bytes | None:
        """按摘要读回**不校验**(供写路径判断现状);不存在 → ``None``。"""
        ...

    def note_product(self, digest: str, fingerprint: str) -> None:
        """提示:指纹 ``fingerprint`` 的 stage 条目内联携带 ``digest`` 的产物字节。
        只是位置,从不被信任——每次读回都重新 hash(files: 喂进程内索引;sqlite:
        no-op,``stage_cache.artifact_digest`` 索引已覆盖)。"""
        ...

    def object_location(self, digest: str) -> Path | None:
        """``digest`` 的字节当前会从哪个**文件**读出(对象文件,或内联携带它的
        stage-cache 指针文件);存放在 db 行里或不存在 → ``None``。"""
        ...

    def content_path(self, digest: str) -> Path:
        """``digest`` 的字节所在(或将写入)的那个文件,供调用方盯漂移;
        对象住在 db 行里(sqlite 的内联对象与内联产物)→ ``LookupError``。"""
        ...

    def put_object(
        self, digest: str, data: bytes, media_type: str, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        """内容寻址写入:已有完好条目是 no-op,损坏条目被这份字节修复(ADR 0029)。"""
        ...

    def object_names(self) -> list[str]:
        """已存对象的全部摘要(新旧布局并集,排序去重)。"""
        ...

    def stage_entry(self, fingerprint: str) -> StageEntry | None:
        """按指纹读 stage-cache 条目;缺失 → ``None``,损坏 → ``DamagedEntry``。"""
        ...

    def put_stage_entry(
        self, fingerprint: str, entry: StageEntry, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        """首写胜出;同摘要 → ``existing``,异摘要且完好 → ``StoreConflict``,
        ``replace=True``(调用方判定既有条目已损坏)→ 原子替换。"""
        ...

    def pointer(self, name: str) -> str | None:
        """命名指针(``current-manifest`` / ``current-processing``)指向的摘要;
        缺失或不可读 → ``None``(ADR 0029:丢失的指针由下一次发布重写)。"""
        ...

    def set_pointer(self, name: str, digest: str) -> None:
        """原子地改写命名指针(等价 ``os.replace`` / 单事务 ``INSERT OR REPLACE``)。"""
        ...

    def record(self, name: str) -> bytes | None:
        """可变命名记录(如 ``document-tree/<id>.json``)的当前字节;缺失 → ``None``。"""
        ...

    def put_record(self, name: str, data: bytes) -> None:
        """原子替换一条可变命名记录(document-tree 语义:后写覆盖)。"""
        ...

    def pin(self, digest: str) -> PinToken:
        """为挂载漂移守卫钉住一个对象;对象必须存在,否则 ``LookupError``。"""
        ...

    def pin_unchanged(self, token: PinToken) -> bool:
        """自 ``pin`` 以来对象是否仍是那些字节(标记不变即命中;变了重读并比对摘要)。"""
        ...

    def transaction(self) -> AbstractContextManager[None]:
        """可重入的事务作用域;files 后端是 no-op,sqlite 后端聚合为一个 db 事务。"""
        ...

    def verify_many(self, digests: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        """流式批量读回并逐个校验;缺失 → ``LookupError``,损坏 → ``DamagedEntry``。"""
        ...

    def close(self) -> None:
        """释放连接 / 租约(sqlite: ``wal_checkpoint(TRUNCATE)``);files 后端是 no-op。"""
        ...


@runtime_checkable
class ModelCacheBackend(Protocol):
    """模型缓存:record_key 为 ``<fingerprint>`` 或 ``<fingerprint>.retry-1``(ADR 0021)。"""

    kind: BackendKind

    def record(self, key: str) -> bytes | None:
        """一条请求记录的字节;缺失 → ``None``。"""
        ...

    def put_record(self, key: str, data: bytes, *, replace_damaged: bool = False) -> None:
        """首写胜出;同字节 no-op,异字节 → ``StoreConflict``,
        除非 ``replace_damaged``(既有记录已被判定损坏,ADR 0029)。"""
        ...

    def response(self, digest: str) -> bytes | None:
        """一份内容寻址的响应体;缺失 → ``None``。"""
        ...

    def put_response(self, digest: str, data: bytes) -> None:
        """内容寻址写入(同名异字节是损坏,直接替换,从不是对手——ADR 0029)。"""
        ...

    def context(self, fingerprint: str) -> bytes | None:
        """一份请求上下文(发送的完整请求体);缺失 → ``None``。"""
        ...

    def put_context(self, fingerprint: str, data: bytes) -> None:
        """首写胜出;已存在即 no-op,异字节 → ``StoreConflict``。"""
        ...

    def claim(
        self,
        key: str,
        owner: ClaimOwner,
        *,
        expired: Callable[[bytes, float], bool] | None = None,
    ) -> int | None:
        """跨进程认领一次真实调用(ADR 0023):成功返回接管代次(0 = 全新),
        持有者可能还在跑 → ``None``(调用方按 ``request_in_progress_or_uncertain`` 处理)。

        ``expired(持有者字节, mtime 或行的 created_at)`` 给出时由它判定持有者是否已结束
        (json_completion 传自己的 ``_expired``);缺省用 ``lease.lease_expired``。
        判定在比较并交换之外,每个调用方对当前持有者只判一次。"""
        ...

    def renew(self, key: str, owner: ClaimOwner, generation: int) -> int | None:
        """重试前续租(ADR 0035):以下一代次重新持有;返回新代次。
        当前代次已不是 ``generation``(别人判定本次已结束并接管了)→ ``None``。"""
        ...

    def claimed(self, key: str) -> bool:
        """``key`` 是否有 claim 在场——ADR 0021 的跳过记录不写在它下面
        (文件布局看第 0 代 ``.claim`` 文件,与原 ``_save_skip`` 相同)。"""
        ...

    def release(self, key: str, owner: ClaimOwner) -> None:
        """记录写好后释放认领;释放失败无害(记录自此单独作答)。"""
        ...

    def close(self) -> None:
        """释放连接 / 租约;files 后端是 no-op。"""
        ...
