#!/bin/bash
# vault-git-sync-gateway.sh — GitHub-vault transport for the Hermes gateway on Railway.
# Replaces the VPS-era Syncthing slice (decommissioned 2026-06-11).
#
# Runs INSIDE the gateway container as a Hermes cron job (every 15 min suggested).
# Clone/pull the private vault repo to $VAULT_DIR; commit+push ONLY Hermes write zones.
# Requires: VAULT_DEPLOY_KEY_GW env var (ed25519 private key, repo-scoped deploy key
# with write access to Anmoll-W/aw-vault — SEPARATE key from vault-brain's, so each
# is independently revocable).
set -uo pipefail

# SSH over 443: Railway blocks outbound port 22 (verified 2026-06-11 — port-22
# clone times out; ssh.github.com:443 authenticates).
REPO="ssh://git@ssh.github.com:443/Anmoll-W/aw-vault.git"
VAULT_DIR="${HERMES_HOME:-/opt/data}/vault"
# Per-run temp key: a fixed path breaks when another user (e.g. a root debug
# session) leaves a stale 600 file behind — the next run can neither write nor
# read it and auth fails (2026-06-11 first cron run).
KEY_FILE="$(mktemp /tmp/vault_deploy_key_gw.XXXXXX)"
trap 'rm -f "$KEY_FILE"' EXIT

# Hermes write zones — single-writer-per-file model carried over from the slice era.
WRITE_ZONES=(
  "Knowledge/inbox"
  "Knowledge/learning"
  "Knowledge/hermes-status.md"
  "Knowledge/reminders-done.md"
  "Projects/LinkedIn/Posts/Drafts"
)

# 0600-atomic key write (never world-readable, even briefly)
umask 177
printf '%s\n' "${VAULT_DEPLOY_KEY_GW:?VAULT_DEPLOY_KEY_GW not set}" > "$KEY_FILE"
umask 022
export GIT_SSH_COMMAND="ssh -i $KEY_FILE -o StrictHostKeyChecking=accept-new"

# Healthchecks.io dead-man's switch (optional, non-fatal). Every successful run
# pings the check; any failure pings <url>/fail. If the job stops running at all
# (volume reset, container death, a deploy that drops it), Healthchecks alerts
# after its grace period — closing the 11h-silent-outage gap that hid the
# 2026-06-22 transport failure. Set HC_VAULT_GIT_SYNC_PING_URL in Railway env.
hc_ping() {  # $1: "" on success, "/fail" on failure
  # Tight 5s budget: the cron scheduler kills this job at its timeout; a slow
  # curl here must never eat enough wall-clock to prevent the /fail ping firing.
  [ -n "${HC_VAULT_GIT_SYNC_PING_URL:-}" ] || return 0
  curl -fsS -m 5 "${HC_VAULT_GIT_SYNC_PING_URL}${1:-}" >/dev/null 2>&1 || true
}

if [ ! -d "$VAULT_DIR/.git" ]; then
  git clone --depth 50 "$REPO" "$VAULT_DIR" || { echo "FATAL clone failed"; hc_ping /fail; exit 1; }
fi
# Guard the cd: if $VAULT_DIR vanished between the check and here, every git
# command below would otherwise run against the wrong repo (the image cwd).
cd "$VAULT_DIR" || { echo "FATAL cd $VAULT_DIR failed"; hc_ping /fail; exit 1; }

# Repo-level identity: `git pull` creates a MERGE COMMIT whenever local commits and
# remote commits diverge, and -c flags on the commit command do not cover it —
# the 2026-06-12 11:11 UTC tick fataled "Committer identity unknown" on exactly that
# (first-ever divergence: heartbeat commit + Mac push in the same window).
git config user.name  "Hermes (railway-gateway)"
git config user.email "hermes@railway"
# Recover if a previous tick died mid-merge (MERGE_HEAD left behind blocks every pull)
[ -f .git/MERGE_HEAD ] && git merge --abort 2>/dev/null

# Heartbeat: one-line file (overwritten, never grows) inside the Knowledge/learning
# write zone. It reaches GitHub only when this run's commit+pull+push all succeed —
# which is exactly what it attests. The Mac boot status line reads its age
# (2026-06-12, makes "gateway — no local heartbeat" in VAULT OS STATUS a real check).
mkdir -p Knowledge/learning
printf '| %s | gateway | ok | 15-min cron heartbeat (UTC) |\n' "$(date -u '+%F %H:%M')" \
  > Knowledge/learning/gateway-heartbeat.md

# Stage only write-zone changes BEFORE pulling, so a pull never clobbers them.
# Per-zone add: a single multi-pathspec `git add` is ALL-OR-NOTHING — one missing
# zone (e.g. Knowledge/inbox before anything writes it) fatals the whole add with
# stderr swallowed, staging NOTHING while the run still logs "sync ok". That bug
# produced 66 consecutive "ok" runs with zero commits (found 2026-06-12 when the
# heartbeat file existed in the container but never reached GitHub).
for z in "${WRITE_ZONES[@]}"; do
  git add -- "$z" 2>/dev/null || true
done
if ! git diff --cached --quiet 2>/dev/null; then
  git -c user.name="Hermes (railway-gateway)" -c user.email="hermes@railway" \
    commit -q -m "hermes: gateway sync $(date -u '+%F %T') UTC"
fi

git pull --no-rebase -X theirs -q origin main || { echo "VAULT SYNC: PULL FAILED $(date -u '+%F %T')"; hc_ping /fail; exit 1; }
git push -q origin main || { echo "VAULT SYNC: PUSH FAILED $(date -u '+%F %T')"; hc_ping /fail; exit 1; }

# Discard any non-write-zone drift so the next run starts clean
git checkout -q -- . 2>/dev/null

# Cron runs with --no-agent deliver stdout verbatim: success must be SILENT
# (else Anmoll gets a Telegram ping every 15 min). Log it instead.
LOG_DIR="${HERMES_HOME:-/opt/data}/logs"
mkdir -p "$LOG_DIR"
echo "sync ok $(date -u '+%F %T')" >> "$LOG_DIR/vault-git-sync.log"
tail -n 500 "$LOG_DIR/vault-git-sync.log" > "$LOG_DIR/vault-git-sync.log.tmp" \
  && mv "$LOG_DIR/vault-git-sync.log.tmp" "$LOG_DIR/vault-git-sync.log"

# Success: ping the dead-man's switch last, so it only fires when the entire
# transport (clone/pull/commit/push) actually completed.
hc_ping
