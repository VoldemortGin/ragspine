"""n8n workflow / execution 文件存储（纯存储层，无 HTTP 概念）。

每个对象一个 JSON 文件：`root/workflows/{id}.json`（meta 字段 + 原始
nodes/connections/settings/staticData 原文保存）与 `root/executions/{id}.json`。
全部 pathlib 跨平台；写入原子（同目录临时文件 + Path.replace）；目录惰性创建；
读到坏文件（非 JSON / 非 dict）一律跳过不炸。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


class N8nStore:
    """workflow CRUD + execution 追加式存储（execution 递增 int id，超 cap 删最旧）。"""

    EXECUTION_CAP = 200

    def __init__(self, root: Path) -> None:
        self._workflows_dir = root / "workflows"
        self._executions_dir = root / "executions"

    # ------------------------------------------------------------------
    # 底层文件读写
    # ------------------------------------------------------------------
    def _write_json(self, path: Path, record: dict[str, Any]) -> None:
        """原子写：先写同目录 .tmp 临时文件，再 Path.replace 就位。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # 坏文件/读失败：跳过不炸
        return cast("dict[str, Any]", data) if isinstance(data, dict) else None

    def _read_dir(self, directory: Path) -> list[dict[str, Any]]:
        if not directory.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            record = self._read_json(path)
            if record is not None:
                records.append(record)
        return records

    def _workflow_path(self, workflow_id: str) -> Path | None:
        """id 只允许字母数字与连字符（防路径穿越；本服务生成的 id 恒为 hex）。"""
        if not workflow_id or not workflow_id.replace("-", "").isalnum():
            return None
        return self._workflows_dir / f"{workflow_id}.json"

    # ------------------------------------------------------------------
    # workflows
    # ------------------------------------------------------------------
    def save_workflow(self, workflow: dict[str, Any]) -> None:
        path = self._workflow_path(str(workflow["id"]))
        if path is None:
            raise ValueError(f"非法 workflow id: {workflow['id']!r}")
        self._write_json(path, workflow)

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        path = self._workflow_path(workflow_id)
        return self._read_json(path) if path is not None else None

    def delete_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """删除并返回被删对象；不存在给 None。"""
        path = self._workflow_path(workflow_id)
        if path is None:
            return None
        record = self._read_json(path)
        if record is not None:
            path.unlink(missing_ok=True)
        return record

    def list_workflows(
        self, *, active: bool | None = None, name: str | None = None
    ) -> list[dict[str, Any]]:
        """按 createdAt 升序（稳定序：createdAt 相同再按 id）列出，支持 active/name 过滤。"""
        records = self._read_dir(self._workflows_dir)
        if active is not None:
            records = [r for r in records if bool(r.get("active")) == active]
        if name is not None:
            records = [r for r in records if r.get("name") == name]
        records.sort(key=lambda r: (str(r.get("createdAt", "")), str(r.get("id", ""))))
        return records

    # ------------------------------------------------------------------
    # executions
    # ------------------------------------------------------------------
    def _execution_ids(self) -> list[int]:
        if not self._executions_dir.is_dir():
            return []
        ids: list[int] = []
        for path in self._executions_dir.glob("*.json"):
            try:
                ids.append(int(path.stem))
            except ValueError:
                continue  # 非数字文件名：跳过
        return ids

    def create_execution(self, record: dict[str, Any]) -> dict[str, Any]:
        """分配递增 int id（现有最大 id+1）写入；超 EXECUTION_CAP 时删最旧（id 最小）。"""
        existing = self._execution_ids()
        next_id = (max(existing) + 1) if existing else 1
        stored = dict(record)
        stored["id"] = next_id
        self._write_json(self._executions_dir / f"{next_id}.json", stored)
        ids = sorted(existing + [next_id])
        while len(ids) > self.EXECUTION_CAP:
            oldest = ids.pop(0)
            (self._executions_dir / f"{oldest}.json").unlink(missing_ok=True)
        return stored

    def get_execution(self, execution_id: int) -> dict[str, Any] | None:
        return self._read_json(self._executions_dir / f"{execution_id}.json")

    def delete_execution(self, execution_id: int) -> dict[str, Any] | None:
        """删除并返回被删对象；不存在给 None。"""
        path = self._executions_dir / f"{execution_id}.json"
        record = self._read_json(path)
        if record is not None:
            path.unlink(missing_ok=True)
        return record

    def list_executions(
        self, *, workflow_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """按 id 降序（最新在前）列出，支持 workflowId/status 过滤。"""
        records = self._read_dir(self._executions_dir)
        if workflow_id is not None:
            records = [r for r in records if r.get("workflowId") == workflow_id]
        if status is not None:
            records = [r for r in records if r.get("status") == status]
        records.sort(key=lambda r: int(r.get("id", 0)), reverse=True)
        return records
