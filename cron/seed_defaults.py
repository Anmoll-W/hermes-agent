"""Seed default Hermes cron jobs on gateway startup.

Reads cron/default_jobs.json and adds any jobs not already present in
the live jobs.json on the Railway volume. Idempotent — existing jobs
(matched by name) are never overwritten.

Only runs on the primary gateway service. Services that route output
through the gateway API (e.g. vault-brain) skip seeding — they have no
persistent volume and their jobs would fail at every vault file read.
Set HERMES_SEED_JOBS=true to force seeding on any service.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_JOBS_FILE = Path(__file__).parent / "default_jobs.json"

# Scripts bundled in the image (baked at build time via `COPY . .`). On every
# boot these are mirrored into HERMES_HOME/scripts/ on the persistent volume,
# because cron `--script` jobs resolve their path under HERMES_HOME/scripts/
# (the volume) — NOT the image. Without this mirror, a volume reset silently
# drops the script and the job fails every tick with no script to run. (This is
# exactly what took down vault-git-sync on 2026-06-22.)
_BUNDLED_SCRIPTS_DIR = Path(__file__).parent / "scripts"

# Services known to route through the gateway API server for delivery.
# They have no vault volume and should not run the default job set.
_PROXY_SERVICES = {"vault-brain"}


def _hermes_scripts_dir() -> Path:
    """HERMES_HOME/scripts on the persistent volume (cron --script resolution root)."""
    home = os.getenv("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "scripts"


def seed_default_scripts() -> None:
    """Mirror image-bundled cron scripts onto the volume's scripts dir.

    Idempotent and self-healing: copies a bundled script when it is missing OR
    when its content differs from the image copy (so script fixes ship on the
    next deploy without a manual volume edit). Safe to call on every boot.
    """
    if not _BUNDLED_SCRIPTS_DIR.is_dir():
        logger.debug("seed_default_scripts: no bundled scripts dir — skipping")
        return

    dest_dir = _hermes_scripts_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("seed_default_scripts: cannot create %s: %s", dest_dir, exc)
        return

    copied = 0
    for src in sorted(_BUNDLED_SCRIPTS_DIR.iterdir()):
        if src.is_dir():
            logger.warning(
                "seed_default_scripts: skipping subdirectory %s — only top-level "
                "scripts are mirrored to the volume", src.name,
            )
            continue
        if not src.is_file():
            continue
        dest = dest_dir / src.name
        try:
            src_bytes = src.read_bytes()
            if dest.exists() and dest.read_bytes() == src_bytes:
                continue
            # Atomic write (tmp → replace) so a crashed boot never leaves a
            # half-written executable that the cron tick would try to run.
            # Bundled cron scripts are always executables → 0o755.
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(src_bytes)
            tmp.chmod(0o755)
            tmp.replace(dest)
            copied += 1
            logger.info("seed_default_scripts: installed %s", dest)
        except Exception as exc:
            logger.warning("seed_default_scripts: failed to install %s: %s", src.name, exc)

    if copied:
        logger.info("seed_default_scripts: %d script(s) installed/updated", copied)


def seed_default_jobs() -> None:
    """Seed default cron jobs if not already present. Safe to call on every boot."""
    service_name = os.getenv("RAILWAY_SERVICE_NAME", "")
    force = os.getenv("HERMES_SEED_JOBS", "").lower() in {"1", "true", "yes"}

    if service_name in _PROXY_SERVICES and not force:
        logger.info(
            "seed_default_jobs: skipping — '%s' routes through gateway API "
            "(no vault volume). Set HERMES_SEED_JOBS=true to override.",
            service_name,
        )
        return

    # On Railway, HERMES_HOME must point at the persistent volume. If it is
    # unset, every path below (scripts mirror, jobs.json, the vault clone) falls
    # back to ~/.hermes on EPHEMERAL container storage — the job would clone,
    # heartbeat, and ping green, then lose everything on the next redeploy. That
    # silently recreates the 2026-06-22 outage with a green dead-man's switch.
    # Refuse loudly instead of degrading silently.
    if service_name and not os.getenv("HERMES_HOME"):
        logger.error(
            "seed_default_jobs: RAILWAY_SERVICE_NAME=%s but HERMES_HOME is unset — "
            "refusing to seed onto ephemeral storage. Set HERMES_HOME to the volume "
            "mount (e.g. /opt/data) in the Railway service variables.",
            service_name,
        )
        return

    # Mirror image-bundled scripts onto the volume first — script jobs below
    # reference these by path and would fail every tick if the file is missing.
    seed_default_scripts()

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
                script=spec.get("script") or None,
                no_agent=bool(spec.get("no_agent", False)),
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
