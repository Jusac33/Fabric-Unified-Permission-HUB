"""Run policy-weaver sync with captured logs and snapshots — exposed via web UI."""
from __future__ import annotations
import asyncio
import io
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings
from app.connectors import get_connector
from app.services import db


class SyncJob:
    def __init__(self, source_type: str, config_path: str):
        self.id = uuid.uuid4().hex[:12]
        self.source_type = source_type
        self.config_path = config_path
        self.status: str = "pending"   # pending|running|success|failed
        self.dry_run: bool = False
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.log_buffer = io.StringIO()
        self.error: Optional[str] = None
        self.snapshot_dir: Path = settings.snapshots_path / self.id
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "config_path": self.config_path,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "log_tail": self.log_tail(2000),
        }

    def log_tail(self, n: int = 4000) -> str:
        s = self.log_buffer.getvalue()
        return s[-n:]


class _DbJob:
    """Read-only view of a persisted sync job, matching the SyncJob template API."""

    def __init__(self, row: Dict[str, Any]):
        self.id = row["id"]
        self.source_type = row["source_type"]
        self.config_path = row["config_path"]
        self.status = row["status"]
        self.dry_run = bool(row.get("dry_run"))
        self.error = row.get("error")
        self._log_tail = row.get("log_tail") or ""
        self.started_at = (
            datetime.fromisoformat(row["started_at"]) if row.get("started_at") else None
        )
        self.finished_at = (
            datetime.fromisoformat(row["finished_at"]) if row.get("finished_at") else None
        )
        self.snapshot_dir = settings.snapshots_path / self.id

    def log_tail(self, n: int = 4000) -> str:
        return self._log_tail[-n:]


class SyncService:
    """Job registry backed by SQLite.

    Live jobs are kept in ``_jobs`` while running (for the streaming log buffer);
    every state change is also persisted so history survives restarts.
    """
    _jobs: Dict[str, SyncJob] = {}

    @staticmethod
    def _persist(job: "SyncJob") -> None:
        db.init_db()
        db.execute(
            """INSERT INTO sync_jobs
                 (id, source_type, config_path, status, dry_run,
                  started_at, finished_at, error, log_tail, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status,
                 started_at=excluded.started_at,
                 finished_at=excluded.finished_at,
                 error=excluded.error,
                 log_tail=excluded.log_tail""",
            (
                job.id, job.source_type, job.config_path, job.status,
                1 if getattr(job, "dry_run", False) else 0,
                job.started_at.isoformat() if job.started_at else None,
                job.finished_at.isoformat() if job.finished_at else None,
                job.error, job.log_tail(4000), db.utcnow(),
            ),
        )

    @classmethod
    def list_jobs(cls):
        db.init_db()
        rows = db.query(
            "SELECT * FROM sync_jobs ORDER BY COALESCE(started_at, created_at) DESC LIMIT 100"
        )
        # Prefer the live in-memory object (fresher log buffer) when present.
        out = []
        for r in rows:
            live = cls._jobs.get(r["id"])
            out.append(live if live else _DbJob(r))
        return out

    @classmethod
    def get(cls, job_id: str) -> Optional["SyncJob"]:
        live = cls._jobs.get(job_id)
        if live:
            return live
        db.init_db()
        row = db.query_one("SELECT * FROM sync_jobs WHERE id = ?", (job_id,))
        return _DbJob(row) if row else None

    @classmethod
    def run(cls, source_type: str, config_path: str, dry_run: bool = False) -> SyncJob:
        from policyweaver.weaver import WeaverAgent

        job = SyncJob(source_type=source_type, config_path=config_path)
        job.dry_run = dry_run
        cls._jobs[job.id] = job

        # Attach a handler to policy-weaver's logger to capture logs into job buffer
        pw_logger = logging.getLogger("POLICY_WEAVER")
        pw_logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(job.log_buffer)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        pw_logger.addHandler(handler)

        job.status = "running"
        job.started_at = datetime.utcnow()
        cls._persist(job)
        try:
            connector = get_connector(source_type, config_path)
            config = connector._raw_config

            if dry_run:
                # Build the export but do NOT apply
                export = connector.to_policy_export()
                if export is None:
                    job.log_buffer.write("\nNo policies produced by source.\n")
                else:
                    snap = job.snapshot_dir / "source_export.json"
                    snap.write_text(
                        export.model_dump_json(
                            exclude_none=True, exclude_unset=True, indent=2
                        )
                    )
                    job.log_buffer.write(
                        f"\nDry-run snapshot written to {snap}\n"
                    )
            else:
                def _src_handler(snapshot_text):
                    if snapshot_text:
                        (job.snapshot_dir / "source_snapshot.json").write_text(snapshot_text)

                def _fab_handler(access_policy):
                    try:
                        text = access_policy.model_dump_json(
                            exclude_none=True, exclude_unset=True, indent=2
                        )
                        with (job.snapshot_dir / "fabric_policies.jsonl").open("a") as f:
                            f.write(text + "\n")
                    except Exception:
                        pass

                def _unmapped_handler(lookup_id, _policy):
                    with (job.snapshot_dir / "unmapped.txt").open("a") as f:
                        f.write(f"{lookup_id}\n")

                asyncio.run(WeaverAgent.run(
                    config,
                    source_snapshot_hndlr=_src_handler,
                    fabric_snaphot_hndlr=_fab_handler,
                    unmapped_policy_hndlr=_unmapped_handler,
                ))

            job.status = "success"
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.log_buffer.write(f"\nERROR: {e}\n")
        finally:
            job.finished_at = datetime.utcnow()
            pw_logger.removeHandler(handler)
            cls._persist(job)
        return job
