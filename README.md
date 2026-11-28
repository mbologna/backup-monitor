# backup-monitor

[![CI](https://github.com/mbologna/backup-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/mbologna/backup-monitor/actions/workflows/ci.yml)

backup-monitor checks once a day that a [k3s](https://k3s.io/) cluster on
[Oracle Cloud Infrastructure](https://www.oracle.com/cloud/) (OCI) is being
backed up, and reports problems through an
[Apprise API](https://github.com/caronc/apprise-api) server. It runs inside
the cluster as a Kubernetes
[CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

## What it checks

- [Longhorn](https://longhorn.io/) backups: the `default` BackupTarget must
  be available, and every volume older than `MAX_BACKUP_AGE_HOURS` must have
  a completed backup within that window. Younger volumes are skipped.
  Problems name the volume by its PVC (`namespace/pvc`).
- etcd snapshots: the newest object under `ETCD_PREFIX` in the
  [OCI Object Storage](https://www.oracle.com/cloud/storage/object-storage/)
  bucket `ETCD_BUCKET` must have been created within `MAX_ETCD_AGE_HOURS`.
  No objects at all is a problem too.

Each check that finds a problem sends one notification of type `failure`,
titled `<CLUSTER_NAME>: Longhorn backups stale` or
`<CLUSTER_NAME>: etcd snapshots stale`, with one problem per line. A check
that cannot run (an API error, missing permissions) also counts as a
problem, and its notification carries the traceback. The run exits 1 if
either check reported anything, so the failed Job shows in the cluster as
well; an undelivered notification is logged to stderr.

## Configuration

All settings are environment variables.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `APPRISE_URL` | yes | | Apprise API notify URL, such as `http://apprise:8000/notify/<key>`. Several space-separated URLs are tried in order, each retried once, until one delivers |
| `MAX_BACKUP_AGE_HOURS` | yes | | Oldest acceptable Longhorn backup per volume, in hours |
| `MAX_ETCD_AGE_HOURS` | yes | | Oldest acceptable etcd snapshot, in hours |
| `ETCD_BUCKET` | yes | | OCI Object Storage bucket holding the etcd snapshots (k3s, or talos-backup on Talos) |
| `ETCD_PREFIX` | yes | | Object name prefix of the snapshots |
| `AWS_ACCESS_KEY_ID` | no | | Access key of an OCI Customer Secret Key: when set, the bucket is listed through OCI's S3-compatible API instead of with instance principals |
| `AWS_SECRET_ACCESS_KEY` | with the key | | Its secret |
| `S3_ENDPOINT` | with the key | | `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com` |
| `S3_REGION` | with the key | | OCI region, such as `us-phoenix-1` |
| `CLUSTER_NAME` | no | `k3s` | Prefix of the notification titles |

The hour limits accept decimals (`36`, `0.5`).

## Running it

The image is `ghcr.io/mbologna/backup-monitor`, built for linux/amd64 and
linux/arm64 and tagged `X.Y.Z` and `X.Y` for each release and `latest` for
the newest. Its entry point is the
`backup-monitor` console script, which takes no arguments.

It needs the cluster around it:

- A ServiceAccount allowed to `get` and `list` `backups`, `backuptargets`
  and `volumes` (API group `longhorn.io`, version `v1beta2`) in
  `longhorn-system`. The check reads them through the in-cluster Kubernetes
  API with the pod's token.
- Read access to the bucket, one of:
  - a Customer Secret Key of a user limited to the bucket (the four
    `AWS_*`/`S3_*` variables). The pod then needs no path to the instance
    metadata service, which on most clusters it should never have;
  - an OCI dynamic group for the cluster's instances, with a policy allowing
    it to list objects in the bucket. The check signs in with instance
    principals through the
    [OCI Python SDK](https://docs.oracle.com/en-us/iaas/tools/python/latest/),
    so no OCI credentials are stored in the cluster, but the pod must reach
    `169.254.169.254`.
- Egress to the Kubernetes API, OCI and the Apprise server.

The image runs as uid 65534 and works with a read-only root filesystem.
Mount a writable `/tmp` (an `emptyDir`): the OCI SDK writes there, and
`HOME` points to it.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.15, the
version in the image.

```sh
uv sync
uv run pytest --cov
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install        # once
uv run pre-commit run --all-files
```

`pytest --cov` fails below the coverage floor, `fail_under` in
`pyproject.toml`. The [pre-commit](https://pre-commit.com/) hooks add
[ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org/),
[bandit](https://bandit.readthedocs.io/) and
[hadolint](https://github.com/hadolint/hadolint) to the usual file checks.

Dependencies resolve only to releases at least 7 days old (`exclude-newer`
in `pyproject.toml`), and the image installs exactly what `uv.lock` pins.
To take a CVE fix released less than 7 days ago, add the package to
`exclude-newer-package` as `<name> = false`, run `uv lock`, and remove the
entry once the release is a week old.

CI runs the reusable `python-ci` and `docker-ci` workflows from
[mbologna/.github](https://github.com/mbologna/.github): ruff, the tests
with the coverage gate, pip-audit, then the image build and a smoke test.
Pull requests build the image without pushing it.

The project skeleton comes from
[python-app-template](https://github.com/mbologna/python-app-template)
through [Copier](https://copier.readthedocs.io/) (`.copier-answers.yml`).

## Why not an existing tool

- [Longhorn](https://longhorn.io/) exposes `longhorn_volume_last_backup_at`
  (since v1.11), so a Prometheus alert rule could check each volume's backup
  age, but that needs a Prometheus and Alertmanager to run the rule, which
  this setup doesn't have. A volume left out of the backup jobs on purpose
  would also alert forever (longhorn/longhorn#13700).
- [Gatus](https://github.com/TwiN/gatus) conditions have no date arithmetic,
  so Gatus can't check an age either.
- [Healthchecks](https://healthchecks.io/) and
  [Uptime Kuma](https://github.com/louislam/uptime-kuma), which wait for a
  ping, need a push after each backup, and Longhorn can't send one.
- None of them reads the age of the newest etcd snapshot in OCI Object
  Storage.

Checked 2026-11-23. A planned upstream pull request would add a last-backup
timestamp metric to Longhorn:
[#52](https://github.com/mbologna/backup-monitor/issues/52).

## Versioning

Releases follow SemVer: [auto-release](https://github.com/mbologna/auto-release)
tags `vX.Y.Z` from the [Conventional Commit](https://www.conventionalcommits.org/)
types (`feat` minor; `fix`, `perf`, `revert` patch; a breaking change
major). It cuts them on Mondays when `main` has gained such a commit since
the last tag, and runtime dependency updates ship in the same weekly
release; a CVE fix releases on the push that brings it. Only a release tag
publishes the image, as `X.Y.Z`, `X.Y` and `latest`. The `version` in
`pyproject.toml` stays `0.0.0`. See the
[versioning standard](https://github.com/mbologna/.github/blob/main/VERSIONING.md).

## Licence

[MIT](LICENSE)
