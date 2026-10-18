import datetime as dt

import pytest

import backup_monitor as bm

NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(hours=36)


def iso(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def volume(name, created_hours_ago=500, ns="apps", pvc=None):
    return {
        "metadata": {"name": name, "creationTimestamp": iso(created_hours_ago)},
        "status": {"kubernetesStatus": {"namespace": ns, "pvcName": pvc or name}},
    }


def backup(vol, hours_ago, state="Completed"):
    return {"status": {"volumeName": vol, "state": state, "backupCreatedAt": iso(hours_ago)}}


def fake_get(target_available=True, backups=(), volumes=()):
    def get(path):
        if path.endswith("backuptargets/default"):
            return {"status": {"available": target_available, "conditions": ["c"]}}
        if path.endswith("/backups"):
            return {"items": list(backups)}
        if path.endswith("/volumes"):
            return {"items": list(volumes)}
        raise AssertionError(path)

    return get


def test_longhorn_all_fresh():
    get = fake_get(backups=[backup("v1", 30), backup("v1", 60)], volumes=[volume("v1")])
    assert bm.check_longhorn(get, NOW, DAY) == []


def test_longhorn_stale_missing_and_new():
    get = fake_get(
        backups=[backup("old", 40), backup("old", 50), backup("failed", 1, state="Error")],
        volumes=[volume("old", pvc="db"), volume("failed"), volume("fresh-vol", created_hours_ago=2)],
    )
    assert bm.check_longhorn(get, NOW, DAY) == [
        "apps/db: last backup 40h ago",
        "apps/failed: no completed backup",
    ]


def test_longhorn_target_unavailable():
    problems = bm.check_longhorn(fake_get(target_available=False), NOW, DAY)
    assert problems == ["BackupTarget default not available: ['c']"]


def test_etcd():
    max_age = dt.timedelta(hours=13)
    assert bm.check_etcd([], NOW, max_age, "p/") == ["no etcd snapshots under p/"]
    times = [NOW - dt.timedelta(hours=20), NOW - dt.timedelta(hours=14)]
    assert bm.check_etcd(times, NOW, max_age, "p/") == ["newest etcd snapshot upload is 14h old (2 kept)"]
    assert bm.check_etcd(times + [NOW - dt.timedelta(hours=1)], NOW, max_age, "p/") == []


@pytest.fixture
def env(monkeypatch):
    for k, v in {
        "APPRISE_URL": "http://a/notify/x",
        "CLUSTER_NAME": "hotel",
        "MAX_BACKUP_AGE_HOURS": "36",
        "MAX_ETCD_AGE_HOURS": "13",
        "ETCD_BUCKET": "b",
        "ETCD_PREFIX": "p/",
    }.items():
        monkeypatch.setenv(k, v)
    sent = []
    monkeypatch.setattr(bm, "notify", lambda url, title, body, type_: sent.append(title) or True)
    return sent


def test_main_reports_each_failing_check(env, monkeypatch):
    monkeypatch.setattr(bm, "kube_get", fake_get(target_available=False))
    monkeypatch.setattr(bm, "list_etcd_snapshots", lambda bucket, prefix: [])
    with pytest.raises(SystemExit) as e:
        bm.main()
    assert e.value.code == 1
    assert env == ["hotel: Longhorn backups stale", "hotel: etcd snapshots stale"]


def test_main_turns_exceptions_into_reports(env, monkeypatch):
    def boom(path):
        raise OSError("api down")

    monkeypatch.setattr(bm, "kube_get", boom)
    monkeypatch.setattr(bm, "list_etcd_snapshots", lambda b, p: [dt.datetime.now(dt.UTC)])
    with pytest.raises(SystemExit) as e:
        bm.main()
    assert e.value.code == 1
    assert env == ["hotel: Longhorn backups stale"]


def test_main_ok(env, monkeypatch):
    monkeypatch.setattr(bm, "kube_get", fake_get())
    monkeypatch.setattr(bm, "list_etcd_snapshots", lambda b, p: [dt.datetime.now(dt.UTC)])
    with pytest.raises(SystemExit) as e:
        bm.main()
    assert e.value.code == 0
    assert env == []
