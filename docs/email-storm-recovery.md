# Repeated dashboard URL emails

The October 27 incident was a delivery/persistence failure. The backend pane
showed a successful SMTP send followed by `tunnel-change check failed: ... disk
I/O error`. The stored URL stayed stale, so the once-per-minute scheduler sent
the same new URL again. Later, database reads also reported `database disk image
is malformed`. A copied snapshot passed `PRAGMA integrity_check` on local disk.

The fix commits a URL reservation before delivery in a serialized SQLite
transaction. If the reservation cannot be saved, no email is sent. A reservation
survives process restarts and failures updating the delivery status. An ambiguous
SMTP outcome is not automatically retried for the same URL; a recorded failure
retries after 15 minutes. Genuine rotations are coalesced to the latest URL with
a one-hour cooldown. Configured stable dashboard domains take precedence, and
unreadable email-pause settings suppress delivery.

## Link recovery after paused notifications

On October 29, cloudflared's automatic update restarted the quick tunnel and
assigned a new hostname while email was paused. The deployed application was
healthy, but the old bookmark was no longer usable. The notification state also
treated a URL observed during a pause as already handled, so resuming email
would not deliver that current link.

Paused URLs now remain eligible for one notification after email resumes,
subject to the same durable reservation and cooldown rules. Repeated scheduler
ticks and concurrent workers cannot resend a successfully claimed URL.

Both initial setup and watchdog recovery launch cloudflared with
`--no-autoupdate --protocol http2` and an explicit `127.0.0.1` origin. Upgrade the
tunnel binary during planned maintenance; its automatic updater previously
replaced the process outside the tmux supervisor and rotated the public URL.
[Cloudflare documents the update restart behavior and opt-out flag](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/#no-autoupdate).
Quick-tunnel URLs can still change when the tunnel is relaunched. Use the
authenticated `/api/url` endpoint on the server to retrieve the current URL.
Watchdog checks are serialized, and failure counts belong to the URL actually
probed. A new hostname cannot inherit failures from its predecessor while DNS
is still settling.

## Runtime storage

Keep the active SQLite and DuckDB databases on a local filesystem. The affected
deployment used a network-backed FUSE mount. SQLite explicitly documents that
[WAL does not work over a network filesystem](https://sqlite.org/wal.html).
Changing journal mode alone is not a reliable repair for filesystem I/O errors.

Before recovery, stop the backend and its watchdog and preserve the database,
WAL, and shared-memory files together. Validate a copy on local disk before
using it. Do not copy an actively changing SQLite database with `cp`.

Set the recovered data path in the deployment's gitignored `data/arui.env`:

```sh
export ARUI_DATA_DIR=/var/lib/autoresearcherui/data
```

The watchdog loads this file on restart. Retain the existing workspace, run
logs, artifacts, and tunnel log paths when migrating the runtime data directory.
Pause email in the recovered settings before restarting; resume research through
the authenticated API and verify real training progress independently of the UI.

Local container storage may be lost if the container is replaced. Publish
consistent snapshots to persistent storage periodically, for example:

```sh
python3 bin/arui_backup_db.py /var/lib/autoresearcherui/data/autoresearch.db \
  data/backups/autoresearch.latest.sqlite
```

The backup command includes committed WAL data, validates the snapshot locally,
and atomically replaces the destination with a private, closed database file.
Keep experiment logs/checkpoints on persistent storage too. This command backs
up SQLite metadata only; it does not back up DuckDB metrics or checkpoints.
