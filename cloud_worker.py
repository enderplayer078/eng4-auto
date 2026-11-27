"""GitHub Actions Cloud Chrome Worker for ENGLISH 4 (Cambridge One).

Reads encrypted job tickets from Zero-Write Firebase RTDB using a least-privilege
GCP service account, deletes tickets immediately upon claim, decrypts credentials
in memory (RSA-OAEP / SHA-256), runs the 100%-guarded solver, and publishes
anonymized progress updates to the public queue node.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from account_session import LoginError
from answer_guard import AnswerGuardError
from run_account import run_for_account

RTDB_SCOPES = (
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/firebase.database",
)

ASSIGNMENT_TOTAL = 40
MAX_JOBS_PER_RUN = 15
STALE_JOB_MS = 240 * 60 * 1000
MAX_HISTORY_JOBS = 100


class RTDBClient:
    """Minimal authenticated client for Firebase Realtime Database REST API."""

    def __init__(self, base_url: str, sa_info: dict[str, Any]) -> None:
        self.base_url = base_url.rstrip("/")
        self._creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=RTDB_SCOPES
        )
        self._session = requests.Session()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if not self._creds.valid or self._creds.expired:
            self._creds.refresh(GoogleAuthRequest())
        headers = {
            "Authorization": f"Bearer {self._creds.token}",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        clean = path.strip("/")
        return f"{self.base_url}/{clean}.json"

    def get(self, path: str) -> Any:
        resp = self._session.get(self._url(path), headers=self._headers(), timeout=20)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, payload: Any) -> Any:
        resp = self._session.put(
            self._url(path),
            headers=self._headers(),
            data=json.dumps(payload),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        resp = self._session.patch(
            self._url(path),
            headers=self._headers(),
            data=json.dumps(payload),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str) -> None:
        resp = self._session.delete(self._url(path), headers=self._headers(), timeout=20)
        resp.raise_for_status()

    def atomic_pop(self, path: str) -> Any | None:
        """Atomically read and delete a node using Firebase RTDB ETag CAS.

        Returns the node value if this worker won exclusive claim (HTTP 200),
        or None if another parallel runner already claimed it (HTTP 412).
        """
        url = self._url(path)
        r = self._session.get(
            url, headers=self._headers({"X-Firebase-ETag": "true"}), timeout=20
        )
        r.raise_for_status()
        etag = r.headers.get("ETag")
        val = r.json()
        if val is None or not etag or etag == "null_etag":
            return None
        d = self._session.delete(
            url, headers=self._headers({"If-Match": etag}), timeout=20
        )
        if d.status_code == 200:
            return val
        if d.status_code == 412:
            return None
        d.raise_for_status()
        return None

    def increment_completed_count(self) -> None:
        """Atomically increment public/stats/completedCount using ETag CAS."""
        url = self._url("public/stats/completedCount")
        for _ in range(5):
            r = self._session.get(
                url, headers=self._headers({"X-Firebase-ETag": "true"}), timeout=15
            )
            r.raise_for_status()
            etag = r.headers.get("ETag")
            current = r.json()
            new_val = (int(current) if isinstance(current, (int, float)) else 0) + 1
            put_headers = self._headers({"If-Match": etag} if etag else None)
            w = self._session.put(
                url, headers=put_headers, data=json.dumps(new_val), timeout=15
            )
            if w.status_code == 200:
                return
            if w.status_code == 412:
                time.sleep(0.2)
                continue
            w.raise_for_status()


def _load_private_key():
    pem_raw = os.environ.get("RSA_PRIVATE_KEY", "").strip()
    if not pem_raw:
        raise RuntimeError("Missing RSA_PRIVATE_KEY environment variable")
    pem_normalized = pem_raw.replace("\\n", "\n").encode("utf-8")
    return serialization.load_pem_private_key(pem_normalized, password=None)


def _decrypt_payload(private_key: Any, b64_ciphertext: str) -> dict[str, str]:
    raw_cipher = base64.b64decode(b64_ciphertext)
    plaintext = private_key.decrypt(
        raw_cipher,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("username") or not data.get("password"):
        raise ValueError("Decrypted payload missing required credentials")
    return {
        "username": str(data["username"]).strip(),
        "password": str(data["password"]),
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _claim_next_job(db: RTDBClient) -> tuple[str, dict[str, Any]] | None:
    """Atomically claim one FIFO verified ticket from private/inbox (parallel-safe)."""
    inbox = db.get("private/inbox")
    if not isinstance(inbox, dict) or not inbox:
        return None

    ordered = sorted(
        inbox.items(),
        key=lambda kv: int((kv[1] or {}).get("createdAt", 0)) if isinstance(kv[1], dict) else 0,
    )
    for job_id, _preview in ordered:
        ticket = db.atomic_pop(f"private/inbox/{job_id}")
        if ticket is None:
            # Already claimed by a parallel runner
            continue
        if not isinstance(ticket, dict):
            continue
        if ticket.get("verified") is not True:
            continue
        if not ticket.get("encryptedPayload") or not ticket.get("userKey"):
            continue
        return job_id, ticket
    return None


def _cleanup_stale_public_jobs(db: RTDBClient) -> None:
    """Mark abandoned queued/running public jobs as failed and bound history size."""
    try:
        queue = db.get("public/queue")
    except Exception:
        return
    if not isinstance(queue, dict) or not queue:
        return

    now = _now_ms()
    history_items: list[tuple[str, int]] = []
    for job_id, item in queue.items():
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        updated_at = int(item.get("updatedAt") or item.get("createdAt") or 0)
        if status in ("queued", "running") and updated_at > 0 and (now - updated_at) > STALE_JOB_MS:
            try:
                db.patch(
                    f"public/queue/{job_id}",
                    {
                        "status": "failed",
                        "error": "Timed out",
                        "updatedAt": now,
                    },
                )
            except Exception:
                pass
        elif status in ("completed", "failed"):
            history_items.append((job_id, updated_at))

    if len(history_items) > MAX_HISTORY_JOBS:
        history_items.sort(key=lambda kv: kv[1], reverse=True)
        for old_id, _ts in history_items[MAX_HISTORY_JOBS:]:
            try:
                db.delete(f"public/queue/{old_id}")
            except Exception:
                pass


def _process_job(db: RTDBClient, private_key: Any, job_id: str, ticket: dict[str, Any]) -> None:
    user_key = str(ticket["userKey"])
    scope = str(ticket.get("scope") or "assignments")
    if scope not in ("assignments", "workbook"):
        scope = "assignments"

    display_total = ASSIGNMENT_TOTAL if scope == "assignments" else 130

    db.patch(
        f"public/queue/{job_id}",
        {
            "status": "running",
            "total": display_total,
            "updatedAt": _now_ms(),
        },
    )

    try:
        creds = _decrypt_payload(private_key, str(ticket["encryptedPayload"]))
        username = creds["username"]
        password = creds["password"]
    except Exception:
        db.delete(f"private/active/{user_key}")
        db.patch(
            f"public/queue/{job_id}",
            {
                "status": "failed",
                "error": "Invalid encrypted payload",
                "updatedAt": _now_ms(),
            },
        )
        print("[worker] Job failed: payload decryption error")
        return

    def _on_progress(overall_done: int, overall_total: int) -> None:
        safe_total = max(1, int(overall_total or display_total))
        safe_done = min(safe_total, max(0, int(overall_done or 0)))
        try:
            db.patch(
                f"public/queue/{job_id}",
                {
                    "status": "running",
                    "done": safe_done,
                    "total": safe_total,
                    "updatedAt": _now_ms(),
                },
            )
        except Exception:
            pass

    raw_min = ticket.get("minMinutes", 20.0)
    raw_max = ticket.get("maxMinutes", 40.0)
    try:
        min_minutes = max(0.0, min(135.0, float(raw_min)))
        max_minutes = max(min_minutes, min(135.0, float(raw_max)))
    except (TypeError, ValueError):
        min_minutes, max_minutes = 20.0, 40.0

    try:
        max_units = max(0, min(10, int(ticket.get("maxUnits") or 0)))
    except (TypeError, ValueError):
        max_units = 0

    try:
        max_activities = max(0, min(200, int(ticket.get("maxActivities") or 0)))
    except (TypeError, ValueError):
        max_activities = 0

    try:
        summary = run_for_account(
            username=username,
            password=password,
            scope=scope,
            max_units=max_units,
            max_activities=max_activities,
            headless=True,
            quiet_logs=True,
            on_progress=_on_progress,
            min_total_minutes=min_minutes,
            max_total_minutes=max_minutes,
        )
        final_total = max(int(summary.get("total", display_total) or display_total), 1)
        is_all_done = bool(summary.get("all_completed", False))
        final_done = min(
            final_total,
            max(0, int(summary.get("completed", final_total if is_all_done else 0))),
        )
        now = _now_ms()
        db.delete(f"private/active/{user_key}")

        if is_all_done:
            already_recorded = False
            try:
                already_recorded = db.get(f"private/completed/{user_key}") is not None
            except Exception:
                already_recorded = False
            db.put(
                f"private/completed/{user_key}",
                {
                    "completedAt": now,
                    "total": final_total,
                },
            )
            db.patch(
                f"public/queue/{job_id}",
                {
                    "status": "completed",
                    "done": final_total,
                    "total": final_total,
                    "partial": False,
                    "updatedAt": now,
                },
            )
            if not already_recorded:
                db.increment_completed_count()
            print("[worker] Job completed at 100%")
        else:
            db.patch(
                f"public/queue/{job_id}",
                {
                    "status": "completed",
                    "done": final_done,
                    "total": final_total,
                    "partial": True,
                    "updatedAt": now,
                },
            )
            print(f"[worker] Partial run finished ({final_done}/{final_total})")
    except LoginError:
        db.delete(f"private/active/{user_key}")
        db.patch(
            f"public/queue/{job_id}",
            {
                "status": "failed",
                "error": "Authentication failed",
                "updatedAt": _now_ms(),
            },
        )
        print("[worker] Job failed: authentication error")
    except AnswerGuardError:
        db.delete(f"private/active/{user_key}")
        db.patch(
            f"public/queue/{job_id}",
            {
                "status": "failed",
                "error": "Verification guard stopped before submission",
                "updatedAt": _now_ms(),
            },
        )
        print("[worker] Job stopped by 100% answer guard")
    except Exception:
        db.delete(f"private/active/{user_key}")
        db.patch(
            f"public/queue/{job_id}",
            {
                "status": "failed",
                "error": "Worker execution error",
                "updatedAt": _now_ms(),
            },
        )
        print("[worker] Job failed: unexpected runtime error")


def main() -> int:
    rtdb_base = os.environ.get("FIREBASE_RTDB_BASE", "").strip()
    sa_raw = os.environ.get("GCP_SA_JSON", "").strip()
    if not rtdb_base or not sa_raw:
        print("[worker] Missing required FIREBASE_RTDB_BASE or GCP_SA_JSON")
        return 1

    sa_info = json.loads(sa_raw)
    private_key = _load_private_key()
    db = RTDBClient(rtdb_base, sa_info)
    _cleanup_stale_public_jobs(db)

    processed = 0
    run_start = time.monotonic()
    while processed < MAX_JOBS_PER_RUN:
        claimed = _claim_next_job(db)
        if claimed is None:
            print("[worker] Queue empty; exiting cleanly")
            break
        job_id, ticket = claimed
        processed += 1
        print(f"[worker] Processing queued job #{processed}")
        _process_job(db, private_key, job_id, ticket)
        # In parallel mode each dispatch runs its own runner; only drain additional
        # waiting tickets if this runner has been alive for < 15 minutes.
        if (time.monotonic() - run_start) >= 900.0:
            break

    print(f"[worker] Worker run finished (processed={processed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
