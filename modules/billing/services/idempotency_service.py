"""
DB-backed Idempotency Service for financial transactions and sensitive clinical mutations.

Enforces:
1. Transactional deduplication: checks and records within the same database transaction.
2. Parameter integrity: raises ConflictError if an existing key is submitted with mismatched payload hashes.
3. Zero external dependencies: no Redis, Memcached, or distributed locks needed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable
from uuid import UUID

from sqlalchemy.orm import Session

from modules.billing.entities.idempotency import IdempotencyRecord
from shared.exceptions.base import ConflictError


class IdempotencyService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def compute_request_hash(self, data: Any) -> str:
        """Deterministically hashes JSON-serializable request payload."""
        if data is None:
            return hashlib.sha256(b"").hexdigest()
        if hasattr(data, "model_dump"):
            dumped = data.model_dump(mode="json")
        elif isinstance(data, dict):
            dumped = data
        else:
            dumped = str(data)
        encoded = json.dumps(dumped, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def get_recorded_response(
        self,
        hospital_id: UUID,
        scope: str,
        idempotency_key: str,
        request_hash: str | None = None,
    ) -> tuple[int, dict[str, Any]] | None:
        """
        Retrieves previously saved response.
        If request_hash is provided, verifies that the payload has not changed.
        """
        rec = (
            self.db.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.hospital_id == hospital_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .first()
        )
        if not rec:
            return None

        if request_hash and rec.request_hash != request_hash:
            raise ConflictError(
                "Idempotency conflict: key was previously used with different request parameters.",
                code="IDEMPOTENCY_CONFLICT",
            )

        return rec.response_code, rec.response_payload

    def claim_or_replay(
        self,
        hospital_id: UUID,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[bool, tuple[int, dict[str, Any]] | None]:
        """
        Atomically claims an idempotency key before mutations begin, or retrieves
        an existing completed response (replay).

        Returns:
            (True, None) if this call claimed the key and owns execution.
            (False, (response_code, response_payload)) if the key was already completed.
        Raises:
            ConflictError if the key was previously used with a different request_hash,
            or if a concurrent request holds the in-flight claim.
        """
        # 1. Check existing record
        rec = (
            self.db.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.hospital_id == hospital_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .first()
        )
        if rec:
            if rec.request_hash != request_hash:
                raise ConflictError(
                    "Idempotency conflict: key was previously used with different request parameters.",
                    code="IDEMPOTENCY_CONFLICT",
                )
            if rec.response_code != 0:
                return False, (rec.response_code, rec.response_payload)
            raise ConflictError(
                "A request with this idempotency key is already in progress.",
                code="IDEMPOTENCY_IN_PROGRESS",
            )

        # 2. Insert claim record within a SAVEPOINT to protect the outer transaction on duplicate key collision
        claim_rec = IdempotencyRecord(
            hospital_id=hospital_id,
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_code=0,
            response_payload={},
        )
        try:
            with self.db.begin_nested():
                self.db.add(claim_rec)
                self.db.flush()
            return True, None
        except Exception:
            # Collision with concurrent transaction inserting same key
            try:
                self.db.expunge(claim_rec)
            except Exception:
                pass
            import time
            for attempt in range(120):
                time.sleep(0.1)
                try:
                    self.db.expire_all()
                except Exception:
                    pass
                existing = (
                    self.db.query(IdempotencyRecord)
                    .filter(
                        IdempotencyRecord.hospital_id == hospital_id,
                        IdempotencyRecord.scope == scope,
                        IdempotencyRecord.idempotency_key == idempotency_key,
                    )
                    .first()
                )
                if existing:
                    if existing.request_hash != request_hash:
                        raise ConflictError(
                            "Idempotency conflict: key was previously used with different request parameters.",
                            code="IDEMPOTENCY_CONFLICT",
                        )
                    if existing.response_code != 0:
                        return False, (existing.response_code, existing.response_payload)
                elif attempt > 5:
                    # The competing transaction aborted/rolled back; attempt to claim the key
                    try:
                        fresh_claim = IdempotencyRecord(
                            hospital_id=hospital_id,
                            scope=scope,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            response_code=0,
                            response_payload={},
                        )
                        with self.db.begin_nested():
                            self.db.add(fresh_claim)
                            self.db.flush()
                        return True, None
                    except Exception:
                        try:
                            self.db.expunge(fresh_claim)
                        except Exception:
                            pass

            raise ConflictError(
                "A request with this idempotency key is already in progress.",
                code="IDEMPOTENCY_IN_PROGRESS",
            )

    def finalize_response(
        self,
        hospital_id: UUID,
        scope: str,
        idempotency_key: str,
        response_code: int,
        response_payload: dict[str, Any],
    ) -> IdempotencyRecord:
        """Updates the claimed idempotency record with final response status and payload."""
        rec = (
            self.db.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.hospital_id == hospital_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .first()
        )
        if rec:
            rec.response_code = response_code
            rec.response_payload = response_payload
            self.db.flush()
            return rec

        rec = IdempotencyRecord(
            hospital_id=hospital_id,
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash="",
            response_code=response_code,
            response_payload=response_payload,
        )
        self.db.add(rec)
        self.db.flush()
        return rec

    def record_response(
        self,
        hospital_id: UUID,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        response_code: int,
        response_payload: dict[str, Any],
    ) -> IdempotencyRecord:
        """Persists or finalizes the response alongside the business operation."""
        rec = (
            self.db.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.hospital_id == hospital_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .first()
        )
        if rec:
            rec.request_hash = request_hash
            rec.response_code = response_code
            rec.response_payload = response_payload
            self.db.flush()
            return rec

        rec = IdempotencyRecord(
            hospital_id=hospital_id,
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_code=response_code,
            response_payload=response_payload,
        )
        self.db.add(rec)
        self.db.flush()
        return rec

    def execute_idempotent(
        self,
        hospital_id: UUID,
        scope: str,
        idempotency_key: str | None,
        request_payload: Any,
        operation: Callable[[], tuple[int, dict[str, Any]]],
    ) -> tuple[int, dict[str, Any]]:
        """
        Executes operation with guaranteed idempotency.
        If idempotency_key is None or empty, executes directly without recording.
        """
        if not idempotency_key or not idempotency_key.strip():
            return operation()

        key = idempotency_key.strip()
        req_hash = self.compute_request_hash(request_payload)

        claimed, cached = self.claim_or_replay(hospital_id, scope, key, req_hash)
        if not claimed and cached is not None:
            return cached

        # Execute business logic
        code, payload = operation()

        # Store result atomically
        self.finalize_response(
            hospital_id=hospital_id,
            scope=scope,
            idempotency_key=key,
            response_code=code,
            response_payload=payload,
        )
        return code, payload
