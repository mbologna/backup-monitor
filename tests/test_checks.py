import datetime as dt
import http.server
import json
import threading
import time

import pytest

import backup_monitor as bm

# The real notify, before the env fixture replaces it.
NOTIFY = bm.notify

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


class StubApprise:
    """A local Apprise API stand-in answering every POST with one status (after
    `delay` seconds); records the requests."""

    def __init__(self, status, delay=0.0):
        calls = self.calls = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                calls.append((self.path, self.headers.get("content-type"), json.loads(self.rfile.read(length))))
                time.sleep(delay)
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/notify/backup-key"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(bm, "APPRISE_RETRY_WAIT_SECONDS", 0)
    servers = []

    def start(status, delay=0.0):
        servers.append(StubApprise(status, delay))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def test_notify_sends_the_notification_as_json_to_the_key(stub):
    """No format: Apprise API treats the body as plain text and passes it on unchanged."""
    ok = stub(200)
    assert bm.notify(ok.url, "t", "b", "failure")
    assert ok.calls == [("/notify/backup-key", "application/json", {"title": "t", "body": "b", "type": "failure"})]


def test_notify_retries_a_failing_url_once_then_the_next_delivers(stub):
    down, ok = stub(500), stub(200)
    assert bm.notify(f"{down.url} {ok.url}", "t", "b", "failure")
    assert (len(down.calls), len(ok.calls)) == (2, 1)


def test_notify_counts_no_configuration_204_as_not_delivered(stub):
    empty, ok = stub(204), stub(200)
    assert bm.notify(f"{empty.url} {ok.url}", "t", "b", "failure")
    assert len(ok.calls) == 1


def test_notify_first_delivery_wins(stub):
    ok, other = stub(200), stub(200)
    assert bm.notify(f"{ok.url} {other.url}", "t", "b", "failure")
    assert (len(ok.calls), len(other.calls)) == (1, 0)


def test_notify_every_url_failing_returns_false(stub):
    a, b = stub(500), stub(500)
    assert not bm.notify(f"{a.url} {b.url}", "t", "b", "failure")
    assert (len(a.calls), len(b.calls)) == (2, 2)


def test_notify_without_a_url_returns_false():
    assert not bm.notify("", "t", "b", "failure")


def test_notify_a_reply_slower_than_the_timeout_is_not_delivered(stub, monkeypatch):
    real = bm.apprise_api_url
    monkeypatch.setattr(bm, "apprise_api_url", lambda url: real(url, timeout=0.2))
    slow = stub(200, delay=1.0)
    assert not bm.notify(slow.url, "t", "b", "failure")


def test_apprise_api_url():
    assert (
        bm.apprise_api_url("http://apprise:8000/notify/backups", timeout=15)
        == "apprise://apprise:8000/backups?method=json&version=1&retry=1&wait=1&cto=15&rto=15"
    )
    assert (
        bm.apprise_api_url("https://example.org/apprise/notify/k?x=y", timeout=5)
        == "apprises://example.org/apprise/k?method=json&version=1&retry=1&wait=1&cto=5&rto=5"
    )


def test_main_fails_over_to_the_next_apprise_url(env, stub, monkeypatch):
    """APPRISE_URL may list several URLs: the first is retried once, then the next delivers."""
    down, ok = stub(500), stub(200)
    monkeypatch.setenv("APPRISE_URL", f"{down.url} {ok.url}")
    monkeypatch.setattr(bm, "notify", NOTIFY)
    monkeypatch.setattr(bm, "kube_get", fake_get())
    monkeypatch.setattr(bm, "list_etcd_snapshots", lambda b, p: [])
    with pytest.raises(SystemExit) as e:
        bm.main()
    assert e.value.code == 1
    assert (len(down.calls), len(ok.calls)) == (2, 1)
    assert ok.calls[0][2]["title"] == "hotel: etcd snapshots stale"


def test_sigv4_matches_the_aws_example():
    """AWS's published GET Bucket (list objects) example for Signature Version 4."""
    headers = bm.sigv4_headers(
        "GET",
        "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J",
        "us-east-1",
        "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        dt.datetime(2013, 5, 24, tzinfo=dt.UTC),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, "
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


def s3_page(times, token=None):
    ns = "http://s3.amazonaws.com/doc/2006-03-01/"
    contents = "".join(f"<Contents><Key>k</Key><LastModified>{t}</LastModified></Contents>" for t in times)
    more = f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return f'<ListBucketResult xmlns="{ns}">{more}{contents}</ListBucketResult>'.encode()


def test_list_s3_objects_follows_continuation_tokens():
    pages = {None: s3_page([iso(30)], token="t2"), "t2": s3_page([iso(2)])}
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = dict(bm.urllib.parse.parse_qsl(bm.urllib.parse.urlsplit(self.path).query))
            seen.append((bm.urllib.parse.urlsplit(self.path).path, query, self.headers["Authorization"][:16]))
            body = pages[query.get("continuation-token")]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        times = bm.list_s3_objects(f"http://127.0.0.1:{server.server_port}", "r", "bkt", "etcd/", "ak", "sk")
    finally:
        server.shutdown()
        server.server_close()
    assert times == [NOW - dt.timedelta(hours=30), NOW - dt.timedelta(hours=2)]
    assert [s[0] for s in seen] == ["/bkt", "/bkt"]
    assert seen[0][1] == {"list-type": "2", "prefix": "etcd/"}
    assert seen[1][1]["continuation-token"] == "t2"
    assert seen[0][2] == "AWS4-HMAC-SHA256"


def test_list_etcd_snapshots_uses_s3_credentials_when_set(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("S3_ENDPOINT", "https://ns.compat.objectstorage.r.oraclecloud.com")
    monkeypatch.setenv("S3_REGION", "r")
    calls = []
    monkeypatch.setattr(bm, "list_s3_objects", lambda *a: calls.append(a) or [NOW])
    assert bm.list_etcd_snapshots("bkt", "etcd/") == [NOW]
    assert calls == [("https://ns.compat.objectstorage.r.oraclecloud.com", "r", "bkt", "etcd/", "ak", "sk")]
