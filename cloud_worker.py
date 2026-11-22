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
    """Fetch FIFO verified ticket from private/inbox and delete it immediately."""
    inbox = db.get("private/inbox")
    if not isinstance(inbox, dict) or not inbox:
        return None

    ordered = sorted(
        inbox.items(),
        key=lambda kv: int((kv[1] or {}).get("createdAt", 0)) if isinstance(kv[1], dict) else 0,
    )
    for job_id, ticket in ordered:
        # Delete ticket immediately upon inspection/claim so ciphertext never lingers.
        db.delete(f"private/inbox/{job_id}")
        if not isinstance(ticket, dict):
            continue
        if ticket.get("verified") is not True:
            continue
        if not ticket.get("encryptedPayload") or not ticket.get("userKey"):
            continue
        return job_id, ticket
    return None


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
            "done": 0,
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

    def _on_progress(done_in_run: int, total_in_run: int) -> None:
        if scope == "assignments":
            already_done = max(0, ASSIGNMENT_TOTAL - total_in_run)
            overall_done = min(ASSIGNMENT_TOTAL, already_done + done_in_run)
            overall_total = ASSIGNMENT_TOTAL
        else:
            overall_done = done_in_run
            overall_total = total_in_run or display_total
        try:
            db.patch(
                f"public/queue/{job_id}",
                {
                    "status": "running",
                    "done": overall_done,
                    "total": overall_total,
                    "updatedAt": _now_ms(),
                },
            )
        except Exception:
            pass

    try:
        summary = run_for_account(
            username=username,
            password=password,
            scope=scope,
            headless=True,
            quiet_logs=True,
            on_progress=_on_progress,
        )
        final_total = ASSIGNMENT_TOTAL if scope == "assignments" else max(summary.get("total", 0), 1)
        now = _now_ms()
        db.put(
            f"private/completed/{user_key}",
            {
                "completedAt": now,
                "total": final_total,
            },
        )
        db.delete(f"private/active/{user_key}")
        db.patch(
            f"public/queue/{job_id}",
            {
                "status": "completed",
                "done": final_total,
                "total": final_total,
                "updatedAt": now,
            },
        )
        db.increment_completed_count()
        print("[worker] Job completed at 100%")
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

    processed = 0
    while processed < MAX_JOBS_PER_RUN:
        claimed = _claim_next_job(db)
        if claimed is None:
            print("[worker] Queue empty; exiting cleanly")
            break
        job_id, ticket = claimed
        processed += 1
        print(f"[worker] Processing queued job #{processed}")
        _process_job(db, private_key, job_id, ticket)

    print(f"[worker] Worker run finished (processed={processed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
