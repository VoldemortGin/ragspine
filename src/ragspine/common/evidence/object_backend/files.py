"""文件布局后端:把现有三处写路径的落位逻辑原样搬入,目录与字节逐位不变。

来源(本 PR **不**改动它们;PR-2/3 才把 store / 模型缓存切到这里):

- ``enterprise_pdf_rag.adapters.document_store.LocalDocumentStore.put`` → ``put_object``
  (tempfile + fsync + ``link_new_file``,读回校验,损坏即以这份字节修复 —— ADR 0020 / 0029);
- ``enterprise_pdf_rag.adapters.processing_store.ProcessingStore`` 的
  ``_write_pointer`` / ``_envelope`` / ``save_document_tree`` / ``current_id`` →
  ``put_stage_entry`` / ``stage_entry`` / ``put_record`` / ``pointer``
  (分层 ``stage-cache-sharded/<ab>/``,三代指针格式,``current-*`` 原子替换 —— ADR 0029 及其 Amendment 1);
- ``ragspine.common.evidence.providers.json_completion`` 的 ``_immutable_write`` /
  ``_claim_request`` / ``_release_claims`` → ``FileModelCacheBackend``(requests / responses /
  contexts / ``.claim`` / ``.takeover-N`` —— ADR 0021 / 0023)。

字节级等价由 ``tests/enterprise_pdf_rag/object_backend/test_file_backend_equivalence.py``
对照现有写路径钉死。
"""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Literal

from ragspine.common.evidence.file_placement import (
    link_new_file,
    note_repair,
    read_stored,
    replace_file,
    sharded_path,
    stored_names,
    stored_path,
)
from ragspine.common.evidence.object_backend import lease
from ragspine.common.evidence.object_backend.protocol import (
    ClaimOwner,
    DamagedEntry,
    PinToken,
    StageEntry,
    StoreConflict,
)

_DIGEST = re.compile(r"[0-9a-f]{64}")
# 与 json_completion.CLAIM_FORMAT 同值(等价性由 test_lease.py 钉死);不从那边 import,
# 避免 object_backend → providers 的依赖方向。
CLAIM_FORMAT = "json-completion-claim-v2"


def _require_digest(digest: str) -> None:
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("Invalid content-addressed artifact identifier")


def _write_temporary(directory: Path, data: bytes) -> Path:
    """同目录 tempfile,写满并 fsync;与三处现有写路径的第一步相同。"""
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return temporary


class FileBackend:
    """现有文件布局,原样:``objects/sha256-sharded/``、``stage-cache-sharded/``、
    ``current-*`` 指针与 ``document-tree/`` 记录。"""

    kind: Literal["files", "sqlite"] = "files"

    def __init__(self, root: Path) -> None:
        self.root = root

    # ---- 内容寻址对象(document_store.LocalDocumentStore.put / _read_digest) ----------

    def _flat(self) -> Path:
        return self.root / "objects" / "sha256"

    def get_object(self, digest: str) -> bytes | None:
        _require_digest(digest)
        try:
            _, data = read_stored(self._flat(), digest)
        except FileNotFoundError:
            return None
        if hashlib.sha256(data).hexdigest() != digest:
            raise DamagedEntry("Stored artifact digest mismatch; source review is unavailable")
        return data

    def read_existing(self, digest: str) -> bytes | None:
        _require_digest(digest)
        path = stored_path(self._flat(), digest)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def put_object(
        self, digest: str, data: bytes, media_type: str, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        """与 ``LocalDocumentStore.put`` 字节等价;``media_type`` 不落盘(文件布局
        不记媒体类型),``replace`` 无须显式给出 —— 损坏条目总是被这份字节修复。"""
        del media_type, replace
        _require_digest(digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Object bytes do not hash to their digest")
        target = sharded_path(self._flat(), digest)
        existing = stored_path(self._flat(), digest)
        if existing is not None and self._intact(existing, digest, len(data)):
            return "existing"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = _write_temporary(target.parent, data)
        try:
            try:
                link_new_file(temporary, target)
            except FileExistsError:
                if self._intact(target, digest, len(data)):
                    return "existing"
                replace_file(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        if existing is not None:
            note_repair("object")
        return "placed"

    @staticmethod
    def _intact(path: Path, digest: str, byte_length: int) -> bool:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return False
        return len(data) == byte_length and hashlib.sha256(data).hexdigest() == digest

    def object_names(self) -> list[str]:
        return [name for name in stored_names(self._flat()) if _DIGEST.fullmatch(name)]

    # ---- stage-cache 条目(processing_store._lookup / _envelope / _write_pointer) -----

    def _stage_flat(self) -> Path:
        return self.root / "stage-cache"

    def stage_entry(self, fingerprint: str) -> StageEntry | None:
        """三代指针格式的读取(ADR 0029 及 Amendment 1):

        1. 旧平铺 / digest-only:第一行是信封对象的摘要,信封是 store 里那个对象;
        2. Amendment 1:两行,信封内联在摘要之后,必须 hash 到它;
        3. Amendment 2(预留):信封行之后的第三段是内联产物字节,必须 hash 到信封里
           artifact 的摘要 —— 主分支合入后在 rebase 时与其实际格式对齐。
        """
        _require_digest(fingerprint)
        try:
            _, data = read_stored(self._stage_flat(), fingerprint)
        except FileNotFoundError:
            return None
        except OSError:
            raise DamagedEntry("damaged pointer") from None
        head, _, rest = data.partition(b"\n")
        try:
            digest = head.strip().decode()
        except UnicodeDecodeError:
            raise DamagedEntry("damaged pointer") from None
        if _DIGEST.fullmatch(digest) is None:
            raise DamagedEntry("damaged pointer")
        if not rest.strip():
            envelope = self.get_object(digest)  # 旧格式:信封是 store 对象
            if envelope is None:
                raise DamagedEntry("damaged pointer")
            return StageEntry(digest, envelope)
        envelope_line, _, product = rest.partition(b"\n")
        if hashlib.sha256(envelope_line).hexdigest() != digest:
            raise DamagedEntry("damaged pointer")
        if not product:
            return StageEntry(digest, envelope_line)
        if hashlib.sha256(product).hexdigest() != _artifact_digest(envelope_line):
            raise DamagedEntry("damaged pointer")
        return StageEntry(digest, envelope_line, product)

    def put_stage_entry(
        self, fingerprint: str, entry: StageEntry, *, replace: bool = False
    ) -> Literal["placed", "existing"]:
        """与 ``ProcessingStore._write_pointer(immutable=not damaged, inline=envelope)``
        字节等价;``replace=True`` 即那边的"已损坏,原子替换"分支。"""
        _require_digest(fingerprint)
        if hashlib.sha256(entry.envelope).hexdigest() != entry.envelope_digest:
            raise ValueError("Stage envelope does not hash to its digest line")
        payload = entry.envelope_digest.encode() + b"\n" + entry.envelope + b"\n"
        if entry.product is not None:
            # Amendment 2 预留的第三段(见 stage_entry);rebase 时与主分支格式对齐。
            if hashlib.sha256(entry.product).hexdigest() != _artifact_digest(entry.envelope):
                raise ValueError("Inline stage product does not hash to its artifact digest")
            payload += entry.product
        target = sharded_path(self._stage_flat(), fingerprint)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = _write_temporary(target.parent, payload)
        try:
            if replace:
                os.replace(temporary, target)
                return "placed"
            try:
                link_new_file(temporary, target)
            except FileExistsError:
                head = target.read_bytes().partition(b"\n")[0].strip()
                if head != entry.envelope_digest.encode():
                    raise StoreConflict("Conflicting immutable stage cache entry") from None
                return "existing"
        finally:
            temporary.unlink(missing_ok=True)
        return "placed"

    # ---- 命名指针与可变记录(processing_store / document_store 的发布与 document-tree)

    def pointer(self, name: str) -> str | None:
        try:
            text = (self.root / name).read_text().strip()
        except OSError:
            return None
        return text if _DIGEST.fullmatch(text) else None

    def set_pointer(self, name: str, digest: str) -> None:
        _require_digest(digest)
        target = self.root / name
        temporary = _write_temporary(target.parent, digest.encode() + b"\n")
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def record(self, name: str) -> bytes | None:
        path = self.root / name
        if not path.is_file():
            return None
        return path.read_bytes()

    def put_record(self, name: str, data: bytes) -> None:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = _write_temporary(target.parent, data)
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    # ---- pin / 事务 / 批量校验 --------------------------------------------------------

    def pin(self, digest: str) -> PinToken:
        _require_digest(digest)
        path = stored_path(self._flat(), digest)
        if path is None:
            raise LookupError("Object to pin is absent")
        layout = 0 if path.parent == self._flat() else 1
        status = path.stat()
        return PinToken(digest, "files", (layout, status.st_size, status.st_mtime_ns))

    def pin_unchanged(self, token: PinToken) -> bool:
        path = stored_path(self._flat(), token.digest)
        if path is None:
            return False
        layout = 0 if path.parent == self._flat() else 1
        try:
            status = path.stat()
        except OSError:
            return False
        if (layout, status.st_size, status.st_mtime_ns) == token.marks:
            return True
        try:
            data = path.read_bytes()
        except OSError:
            return False
        return hashlib.sha256(data).hexdigest() == token.digest

    def transaction(self) -> AbstractContextManager[None]:
        return nullcontext()

    def verify_many(self, digests: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        for digest in digests:
            data = self.get_object(digest)
            if data is None:
                raise LookupError("Object to verify is absent")
            yield digest, data

    def close(self) -> None:
        return None


def _artifact_digest(envelope: bytes) -> str:
    """信封 JSON 里 outcome.artifact 的摘要;解析不了 → 空串(当作必然不匹配)。"""
    try:
        document = json.loads(envelope)
        digest = document["outcome"]["artifact"]["sha256"]
    except (ValueError, TypeError, KeyError):
        return ""
    return digest if isinstance(digest, str) else ""


def _immutable_write(path: Path, content: bytes, *, replace_damaged: bool = False) -> None:
    """与 ``json_completion._immutable_write`` 字节等价(冲突抛 ``StoreConflict``,
    那边抛 ``JsonCompletionError("cache_conflict")``,由 PR-3 的调用方映射)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _write_temporary(path.parent, content)
    try:
        try:
            link_new_file(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                if not replace_damaged:
                    raise StoreConflict("cache_conflict") from None
                replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class FileModelCacheBackend:
    """模型缓存的文件布局,原样:``requests/`` / ``responses/`` / ``contexts/`` 平铺
    (ADR 0029 §6:模型缓存不分层),claim 为 ``<record>.claim`` 与 ``.takeover-N``。"""

    kind: Literal["files", "sqlite"] = "files"

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def _record_path(self, key: str) -> Path:
        _require_record_key(key)
        return self.cache_dir / "requests" / f"{key}.json"

    def record(self, key: str) -> bytes | None:
        try:
            return self._record_path(key).read_bytes()
        except FileNotFoundError:
            return None

    def put_record(self, key: str, data: bytes, *, replace_damaged: bool = False) -> None:
        _immutable_write(self._record_path(key), data, replace_damaged=replace_damaged)

    def response(self, digest: str) -> bytes | None:
        _require_digest(digest)
        try:
            return (self.cache_dir / "responses" / f"{digest}.json").read_bytes()
        except FileNotFoundError:
            return None

    def put_response(self, digest: str, data: bytes) -> None:
        _require_digest(digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Response bytes do not hash to their digest")
        # 内容寻址:同名异字节是损坏,从不是对手(json_completion 的 replace_damaged=True)。
        path = self.cache_dir / "responses" / f"{digest}.json"
        _immutable_write(path, data, replace_damaged=True)

    def context(self, fingerprint: str) -> bytes | None:
        _require_digest(fingerprint)
        try:
            return (self.cache_dir / "contexts" / f"{fingerprint}.json").read_bytes()
        except FileNotFoundError:
            return None

    def put_context(self, fingerprint: str, data: bytes) -> None:
        _require_digest(fingerprint)
        path = self.cache_dir / "contexts" / f"{fingerprint}.json"
        if path.exists():  # 首个存进去的上下文胜出(json_completion._store_context)
            return
        _immutable_write(path, data)

    def claim(self, key: str, owner: ClaimOwner) -> int | None:
        base = self._record_path(key).with_suffix(".json.claim")
        content = lease.owner_payload(
            CLAIM_FORMAT, owner, extra={"request_fingerprint": key.partition(".")[0]}
        )
        return lease.acquire_lease(
            base, content, claim_format=CLAIM_FORMAT, process_token=owner.process
        )

    def release(self, key: str, owner: ClaimOwner) -> None:
        del owner  # 文件布局按路径整组释放(ADR 0023 §5),不看持有者
        lease.release_lease(self._record_path(key).with_suffix(".json.claim"))

    def close(self) -> None:
        return None


_RECORD_KEY = re.compile(r"[0-9a-f]{64}(\.retry-1)?")


def _require_record_key(key: str) -> None:
    if _RECORD_KEY.fullmatch(key) is None:
        raise ValueError("A model-cache record key is a fingerprint, optionally '.retry-1'")
