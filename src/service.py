from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        resource_id = payload.get("resource_id")
        if resource_id is not None:
            resource_id = require_text(resource_id, "resource_id", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, resource_id)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
            "resource_id": resource_id,
        })
        return record

    def merge_records(self, item_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        """队员回营后批量补录离线记录：client_ref 幂等去重，资源跨火线占用时整批退回。"""
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValidationError("records必须是数组")
        if not records:
            raise ValidationError("records不能为空")
        if len(records) > 500:
            raise ValidationError("每批最多500条记录")

        normalized = []
        seen_refs = set()
        for index, entry in enumerate(records):
            location = f"records[{index}]"
            if not isinstance(entry, dict):
                raise ValidationError(f"{location}必须是对象")
            client_ref = require_text(entry.get("client_ref"),
                                      f"{location}.client_ref", 100)
            if client_ref in seen_refs:
                raise ValidationError(
                    f"{location}.client_ref在批次内重复：{client_ref}")
            seen_refs.add(client_ref)
            kind = require_text(entry.get("kind"), f"{location}.kind", 100)
            detail = require_text(entry.get("detail"), f"{location}.detail")
            status = entry.get("status", "open")
            if status not in ("open", "closed"):
                raise ValidationError(f"{location}.status必须是open或closed")
            resource_id = entry.get("resource_id")
            if resource_id is not None:
                resource_id = require_text(
                    resource_id, f"{location}.resource_id", 100)
            normalized.append({
                "client_ref": client_ref, "kind": kind, "detail": detail,
                "status": status, "resource_id": resource_id,
            })

        result = self.repository.merge_records(item_id, normalized, actor)
        created, duplicates, conflicts = (result["created"],
                                          result["duplicates"],
                                          result["conflicts"])
        if conflicts:
            summary = "；".join(
                f"队员{c['resource_id']}仍分配在事件{c['item_id']}（{c['item_title']}）"
                for c in conflicts)
            message = f"资源仍占用在其他未关闭事件，整批退回：{summary}" if summary \
                else "资源仍占用在其他未关闭事件，整批退回"
            raise ConflictError(message, {
                "created_count": 0,
                "duplicate_count": len(duplicates),
                "rejected_count": len(normalized) - len(duplicates),
                "created": [],
                "duplicates": duplicates,
                "conflicts": conflicts,
            })
        return {
            "created_count": len(created),
            "duplicate_count": len(duplicates),
            "rejected_count": 0,
            "created": created,
            "duplicates": duplicates,
        }

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
