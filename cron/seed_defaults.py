"""Seed default Hermes cron jobs on gateway startup.

Reads cron/default_jobs.json and adds any jobs not already present in
the live jobs.json on the Railway volume. Idempotent — existing jobs
(matched by name) are never overwritten.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_JOBS_FILE = Path(__file__).parent / "default_jobs.json"


def seed_default_jobs() -> None:
    """Seed default cron jobs if not already present. Safe to call on every boot."""
    if not _DEFAULT_JOBS_FILE.exists():
        logger.debug("seed_default_jobs: no default_jobs.json found — skipping")
        return

    try:
        defaults = json.loads(_DEFAULT_JOBS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("seed_default_jobs: failed to read default_jobs.json: %s", exc)
        return

    specs = defaults.get("jobs", [])
    if not specs:
        return

    from cron.jobs import load_jobs, create_job, save_jobs

    existing = load_jobs()
    existing_names = {j.get("name") for j in existing}

    added = 0
    updated = 0
    for spec in specs:
        name = spec.get("name")
        if not name:
            logger.warning("seed_default_jobs: skipping unnamed job spec: %s", spec)
            continue

        default_version = spec.get("version", 1)
        existing_job = next((j for j in existing if j.get("name") == name), None)

        if existing_job is not None:
            existing_version = existing_job.get("version", 0)
            if existing_version >= default_version:
                logger.debug(
                    "seed_default_jobs: job '%s' already at version %d — skipping",
                    name, existing_version,
                )
                continue
            # Existing job is older — replace it with the new spec.
            logger.info(
                "seed_default_jobs: updating job '%s' from version %d to %d",
                name, existing_version, default_version,
            )
            existing = [j for j in existing if j.get("name") != name]
            existing_names.discard(name)

        try:
            job = create_job(
                prompt=spec.get("prompt"),
                schedule=spec.get("schedule", "0 0 * * *"),
                name=name,
                deliver=spec.get("deliver", "local"),
                model=spec.get("model") or None,
            )
            job["version"] = default_version
            existing.append(job)
            existing_names.add(name)
            if existing_job is not None:
                updated += 1
                logger.info("seed_default_jobs: updated job '%s' (schedule: %s)", name, spec.get("schedule"))
            else:
                added += 1
                logger.info("seed_default_jobs: seeded job '%s' (schedule: %s)", name, spec.get("schedule"))
        except Exception as exc:
            logger.warning("seed_default_jobs: failed to create/update job '%s': %s", name, exc)

    if added or updated:
        save_jobs(existing)
        logger.info("seed_default_jobs: %d job(s) seeded, %d job(s) updated", added, updated)
