"""Send-file progress jobs: the records the WebUI's file modal polls.

The browser mints a job id and hands it to POST /comm-send; the send itself
runs in this process (the file is on this disk, or the phone just uploaded it
here), so the bytes moving to the hub are observable from nowhere else. GET
/transfer-progress reads a record back — room.py serves it, server.py routes it.

One registry for the whole room: transfers are rare, and a job is a handful of
ints.
"""

import threading
import time

JOB_TTL_S = 300.0  # settled jobs stay readable this long, for a last poll


class TransferJobs:
    """Thread-safe registry of file-send jobs (id -> {name, total, done, state})."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, job_id: str, name: str, total: int) -> None:
        with self._lock:
            self._sweep()
            self._jobs[str(job_id)] = {
                "id": str(job_id),
                "name": str(name),
                "total": max(0, int(total)),
                "done": 0,
                "state": "running",
                "error": "",
                "ts": time.time(),
            }

    def progress(self, job_id: str, done: int) -> None:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is not None and job["state"] == "running":
                job["done"] = max(0, int(done))

    def finish(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is not None:
                job["state"] = "done"
                job["done"] = job["total"]
                job["ts"] = time.time()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is not None:
                job["state"] = "error"
                job["error"] = str(error)
                job["ts"] = time.time()

    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(str(job_id))
        return dict(job) if job is not None else {"error": "unknown transfer job"}

    def _sweep(self) -> None:
        """Drop settled jobs nobody can be polling any more (lock held).

        Called from start(), the only moment the registry grows — an in-flight
        job is never swept, however long a slow transfer takes.
        """
        now = time.time()
        dead = [
            key
            for key, job in self._jobs.items()
            if job["state"] != "running" and now - float(job.get("ts") or 0) > JOB_TTL_S
        ]
        for key in dead:
            del self._jobs[key]
