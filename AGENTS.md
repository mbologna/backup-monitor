# AGENTS.md — backup-monitor

Daily check that a k3s cluster on Oracle Cloud is backed up: Longhorn volume backups and etcd snapshots in OCI Object Storage, with problems reported through Apprise. User-facing docs: `README.md`.

## Layout

- `src/backup_monitor/`: the package; its `main()` is the console script
  `backup-monitor`, the image's entry point.
- `tests/`: pytest. Keep I/O out of the logic so it tests without mocks.
- `Dockerfile`: two stages, `uv sync --locked` into `/opt/venv` (the
  build stage adds gcc and libc6-dev, because two of oci's pinned
  dependencies build from source), then a runtime without pip.

## Conventions

- Conventional Commits, header at most 72 characters. The commit subjects
  that reach main are the release notes: `feat`/`fix`/`perf` release, `refactor`,
  `test`, `docs`, `ci`, `chore` don't (a dependency update to runtime files
  ships in the weekly maintenance release). CI lints every commit with
  [commitlint-config-mbologna](https://github.com/mbologna/commitlint-config)
  and rejects `fixup!` commits; pull requests merge with a merge commit.
- Coverage may not drop below `fail_under` in `pyproject.toml`; raise it
  when tests are added.
- Dependencies: `uv add <pkg>`; uv.lock is committed and the image installs
  it with `--locked`. `exclude-newer` keeps releases younger than 7 days out.
- Notifications go through `notify()` (the apprise library) with the
  `APPRISE_URL` environment variable: one URL or several space-separated
  ones, each its own priority group, tried in order and retried once before
  the next. It returns False rather than raising; check it when sending
  matters.
- The image: non-root (uid 65534), no pip, nothing written outside `/tmp`
  (the OCI SDK writes there), so the root filesystem can be read-only.

## Versioning

Plain SemVer: `vX.Y.Z` tags, each publishing the image
`ghcr.io/mbologna/backup-monitor` as `X.Y.Z`, `X.Y` and `latest`, per the
[versioning standard](https://github.com/mbologna/.github/blob/main/VERSIONING.md).

- Never create, move or delete tags, and never edit a version number to cut
  a release: auto-release tags from the commit types (`feat` minor; `fix`,
  `perf`, `revert` patch; `!` or a `BREAKING CHANGE:` footer major).
- Write Conventional Commit subjects; they decide the next version.
- `version` in `pyproject.toml` is a placeholder and stays `0.0.0`; the
  image's `org.opencontainers.image.version` label comes from the tag.

## Template

The skeleton comes from https://github.com/mbologna/python-app-template
(`.copier-answers.yml`). Renovate's copier manager opens a pull request
for each template release; resolve conflicts in favour of this project's
code. By hand: `uvx copier update`, then `uv lock` if dependencies changed.
