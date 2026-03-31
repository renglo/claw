from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

from .class_prototypes import JournalEntry


RING_CLAW_JOURNAL = "claw_journal"


def _safe_doc_id(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", raw)[:200]


class Journal:
    """
    Episodic journal stored in ring ``claw_journal``.

    Documents are keyed by entity, optional task bucket, and calendar date.
    Each document holds an ``entries`` list; new notes are appended with put.
    """

    def __init__(
        self,
        data_controller: Any,
        portfolio: str,
        org: str,
        entity_type: str,
        entity_id: str,
    ) -> None:
        self._dc = data_controller
        self._portfolio = portfolio
        self._org = org
        self._entity_type = entity_type
        self._entity_id = entity_id

    def _document_id(self, task_key: str, journal_date: str) -> str:
        return _safe_doc_id(f"{self._entity_type}:{self._entity_id}:{task_key}:{journal_date}")

    def _row_to_entry(self, row: dict[str, Any]) -> JournalEntry:
        created = row.get("created_at")
        if isinstance(created, str):
            try:
                created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                created_at = None
        else:
            created_at = created if isinstance(created, datetime) else None
        src = row.get("source_event_ids") or []
        if not isinstance(src, list):
            src = [str(src)]
        tags = row.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        return JournalEntry(
            entry_id=str(row.get("entry_id", "")),
            journal_date=str(row.get("journal_date", "")),
            session_id=str(row.get("session_id", "")),
            summary=str(row.get("summary", "")),
            source_event_ids=src,
            tags=tags,
            created_at=created_at,
        )

    def append_entry(
        self,
        journal_date: str,
        summary: str,
        session_id: str,
        source_event_ids: list[str],
        tags: Optional[list[str]] = None,
    ) -> JournalEntry:
        task_key = "default"
        entry_id = str(uuid.uuid4())
        row = {
            "entry_id": entry_id,
            "journal_date": journal_date,
            "session_id": session_id,
            "summary": summary,
            "source_event_ids": source_event_ids,
            "tags": tags or [],
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        doc_id = self._document_id(task_key, journal_date)
        existing = self._dc.get_a_b_c(self._portfolio, self._org, RING_CLAW_JOURNAL, doc_id)
        if existing.get("success") is False or "_id" not in existing:
            payload = {
                "_id": doc_id,
                "entity_type": self._entity_type,
                "entity_id": self._entity_id,
                "task_key": task_key,
                "journal_date": journal_date,
                "entries": [row],
            }
            result, _st = self._dc.post_a_b(self._portfolio, self._org, RING_CLAW_JOURNAL, payload)
            if not result.get("success"):
                raise RuntimeError(result.get("message", "journal post_a_b failed"))
        else:
            entries = list(existing.get("entries") or [])
            entries.append(row)
            result, _st = self._dc.put_a_b_c(
                self._portfolio,
                self._org,
                RING_CLAW_JOURNAL,
                doc_id,
                {"entries": entries},
            )
            if not result.get("success"):
                raise RuntimeError(result.get("message", "journal put failed"))
        return self._row_to_entry({**row, "journal_date": journal_date})

    def get_entries_for_date(self, journal_date: str, task_key: str = "default") -> list[JournalEntry]:
        doc_id = self._document_id(task_key, journal_date)
        doc = self._dc.get_a_b_c(self._portfolio, self._org, RING_CLAW_JOURNAL, doc_id)
        if doc.get("success") is False or "_id" not in doc:
            return []
        out: list[JournalEntry] = []
        for row in doc.get("entries") or []:
            if isinstance(row, dict):
                out.append(self._row_to_entry(row))
        return out

    def list_recent_entries(self, limit: int = 50) -> list[JournalEntry]:
        """All journal rows from a ring scan (best-effort, recency not guaranteed)."""
        res = self._dc.get_a_b(self._portfolio, self._org, RING_CLAW_JOURNAL, limit=500)
        if not res.get("success"):
            return []
        flat: list[JournalEntry] = []
        for doc in res.get("items", []):
            for row in doc.get("entries") or []:
                if isinstance(row, dict):
                    flat.append(self._row_to_entry(row))
                if len(flat) >= limit:
                    return flat[:limit]
        return flat[:limit]

    def search_entries(
        self,
        query: str,
        tags: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        limit: int = 10,
    ) -> list[JournalEntry]:
        q = (query or "").lower()
        tag_set = set(tags or [])
        found: list[JournalEntry] = []
        res = self._dc.get_a_b(self._portfolio, self._org, RING_CLAW_JOURNAL, limit=1000)
        if not res.get("success"):
            return []
        for doc in res.get("items", []):
            for row in doc.get("entries") or []:
                if not isinstance(row, dict):
                    continue
                je = self._row_to_entry(row)
                if session_id and je.session_id != session_id:
                    continue
                if tag_set and not tag_set.intersection(set(je.tags)):
                    continue
                if q and q not in je.summary.lower():
                    continue
                found.append(je)
                if len(found) >= limit:
                    return found
        return found

    def summarize_day(self, journal_date: str, task_key: str = "default") -> str:
        entries = self.get_entries_for_date(journal_date, task_key=task_key)
        if not entries:
            return ""
        lines = [e.summary for e in entries]
        return "\n".join(lines)
