# backup-monitor

[![CI](https://github.com/mbologna/backup-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/mbologna/backup-monitor/actions/workflows/ci.yml)

A daily check that a [k3s](https://k3s.io/) cluster on
[Oracle Cloud](https://www.oracle.com/cloud/) is actually being backed up.
It runs in the cluster as a Kubernetes CronJob and reports through
[Apprise](https://github.com/caronc/apprise-api).

- **[Longhorn](https://longhorn.io/):** the backup target must be available,
  and every volume older than `MAX_BACKUP_AGE_HOURS` must have a completed
  backup within that window.
- **etcd:** the newest k3s etcd snapshot in an OCI Object Storage bucket must
  be newer than `MAX_ETCD_AGE_HOURS`.

Each check that finds a problem sends one Apprise notification of type
`failure`. A check that can't run (an API error, missing permissions) counts
as a problem too, with the traceback in the notification. The run exits
non-zero whenever anything was reported, so a failed Job is visible in the
cluster as well.

## Configuration

| Variable | Required | Default | |
|---|---|---|---|
| `APPRISE_URL` | yes | | Apprise notify URL |
| `MAX_BACKUP_AGE_HOURS` | yes | | Oldest acceptable Longhorn backup, per volume |
| `MAX_ETCD_AGE_HOURS` | yes | | Oldest acceptable etcd snapshot upload |
| `ETCD_BUCKET` | yes | | OCI Object Storage bucket holding the snapshots |
| `ETCD_PREFIX` | yes | | Object name prefix of the snapshots |
| `CLUSTER_NAME` | no | `k3s` | Prefix of the notification titles |

## Permissions

- A ServiceAccount allowed to `get` and `list` `backups`, `backuptargets` and
  `volumes` (`longhorn.io`) in `longhorn-system`.
- An OCI dynamic group for the cluster's instances, with a policy allowing it
  to list objects in the bucket. The check signs in with instance principals,
  so no OCI credentials are stored in the cluster.
- Egress to the Kubernetes API, OCI and Apprise.

The image runs as uid 65534 and works with a read-only root filesystem. Mount
a writable `/tmp`, which the OCI SDK uses.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
