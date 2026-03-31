from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from .class_prototypes import MemoryFact


RING_CLAW_BELIEFS = "claw_beliefs"


class Beliefs:
    """
    Long-term semantic memory backed by DataController ring ``claw_beliefs``.
    Each belief is one document; metadata and tags live on the document.
    """

    def __init__(self, data_controller: Any, portfolio: str, org: str) -> None:
        self._dc = data_controller
        self._portfolio = portfolio
        self._org = org

    def _doc_to_fact(self, doc: dict[str, Any]) -> MemoryFact:
        src = doc.get("source_event_ids") or []
        if isinstance(src, str):
            try:
                src = json.loads(src)
            except json.JSONDecodeError:
                src = [src]
        tags = doc.get("tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except json.JSONDecodeError:
                tags = [tags]
        updated = doc.get("updated_at") or doc.get("_modified") or ""
        if isinstance(updated, datetime):
            updated_at = updated
        else:
            try:
                updated_at = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                updated_at = datetime.utcnow()
        return MemoryFact(
            fact_id=str(doc.get("_id", "")),
            subject=doc.get("subject"),
            predicate=doc.get("predicate"),
            value=doc.get("value"),
            confidence=float(doc.get("confidence", 0.5)),
            source_event_ids=list(src) if isinstance(src, list) else [],
            updated_at=updated_at,
            tags=list(tags) if isinstance(tags, list) else [],
        )

    def write_fact(
        self,
        fact_payload: dict[str, Any],
        source_event_ids: list[str],
    ) -> MemoryFact:
        fact_id = str(fact_payload.get("fact_id") or fact_payload.get("_id") or uuid.uuid4())
        body = {
            "subject": fact_payload.get("subject"),
            "predicate": fact_payload.get("predicate"),
            "value": fact_payload.get("value"),
            "confidence": float(fact_payload.get("confidence", 0.7)),
            "tags": fact_payload.get("tags") or [],
            "source_event_ids": source_event_ids,
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "provenance": fact_payload.get("provenance") or {},
        }
        existing = self._dc.get_a_b_c(self._portfolio, self._org, RING_CLAW_BELIEFS, fact_id)
        if existing.get("success") is False or "_id" not in existing:
            body["_id"] = fact_id
            result, _status = self._dc.post_a_b(self._portfolio, self._org, RING_CLAW_BELIEFS, body)
            if not result.get("success"):
                raise RuntimeError(result.get("message", "post_a_b failed"))
            item = result.get("item") or {}
            item.setdefault("_id", fact_id)
            return self._doc_to_fact(item)
        merged = {**existing, **body, "_id": fact_id}
        result, _status = self._dc.put_a_b_c(
            self._portfolio, self._org, RING_CLAW_BELIEFS, fact_id, merged
        )
        if not result.get("success"):
            raise RuntimeError(result.get("message", "put_a_b_c failed"))
        return self._doc_to_fact(merged)

    def get_fact(self, fact_id: str) -> Optional[MemoryFact]:
        doc = self._dc.get_a_b_c(self._portfolio, self._org, RING_CLAW_BELIEFS, fact_id)
        if doc.get("success") is False or "_id" not in doc:
            return None
        return self._doc_to_fact(doc)

    def list_facts(self, limit: int = 200) -> list[MemoryFact]:
        """Load a page of beliefs for context assembly (``get_a_b``)."""
        out: list[MemoryFact] = []
        res = self._dc.get_a_b(self._portfolio, self._org, RING_CLAW_BELIEFS, limit=limit)
        if not res.get("success"):
            return out
        for row in res.get("items", []):
            out.append(self._doc_to_fact(row))
        return out

    def search_facts(
        self,
        query: str,
        tags: Optional[list[str]] = None,
        subject: Optional[str] = None,
        limit: int = 10,
    ) -> list[MemoryFact]:
        res = self._dc.get_a_b(self._portfolio, self._org, RING_CLAW_BELIEFS, limit=1000)
        if not res.get("success"):
            return []
        q = (query or "").lower()
        found: list[MemoryFact] = []
        tag_set = set(tags or [])
        for row in res.get("items", []):
            fact = self._doc_to_fact(row)
            if subject and (fact.subject or "") != subject:
                continue
            if tag_set and not tag_set.intersection(set(fact.tags)):
                continue
            if q:
                blob = json.dumps(
                    {
                        "s": fact.subject,
                        "p": fact.predicate,
                        "v": fact.value,
                        "tags": fact.tags,
                    },
                    default=str,
                ).lower()
                if q not in blob:
                    continue
            found.append(fact)
            if len(found) >= limit:
                break
        return found

    def delete_fact(self, fact_id: str) -> bool:
        result, status = self._dc.delete_a_b_c(self._portfolio, self._org, RING_CLAW_BELIEFS, fact_id)
        return bool(result.get("success")) and status == 200

    def list_facts_for_subject(self, subject: str) -> list[MemoryFact]:
        return self.search_facts(query="", subject=subject, limit=500)
