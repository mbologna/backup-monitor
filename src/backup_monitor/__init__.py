"""Daily freshness check for a k3s cluster's backups on Oracle Cloud.

Checks that every Longhorn volume has a completed backup newer than
MAX_BACKUP_AGE_HOURS and that the BackupTarget is available, and that the
newest k3s etcd snapshot in an OCI Object Storage bucket is newer than
MAX_ETCD_AGE_HOURS. Each check that finds a problem sends one Apprise
notification; the run exits non-zero if anything was stale, failed or could
not be reported.

Runs in-cluster: Longhorn is read through the Kubernetes API with the pod's
service account. The bucket is listed through OCI's S3-compatible API with a
Customer Secret Key when AWS_ACCESS_KEY_ID is set, else with the nodes'
instance principal.
"""

import datetime as dt
import hashlib
import hmac
import json
import os
import ssl
import sys
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # nosec B405: parses only the S3 endpoint's TLS-authenticated replies

import apprise

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
LONGHORN = "apis/longhorn.io/v1beta2/namespaces/longhorn-system"
# Seconds an Apprise API request may take.
APPRISE_TIMEOUT_SECONDS = 15
# Seconds before the one retry of an Apprise URL.
APPRISE_RETRY_WAIT_SECONDS = 1


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours(delta):
    return f"{delta.total_seconds() / 3600:.0f}h"


def kube_get(path):
    """GET a path on the in-cluster Kubernetes API with the pod's token."""
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with open(f"{SA}/token") as f:
        token = f.read()
    req = urllib.request.Request(
        f"https://kubernetes.default.svc/{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:  # nosec B310: fixed https URL of the in-cluster API
        return json.load(r)


def apprise_api_url(url, timeout=APPRISE_TIMEOUT_SECONDS):
    """The apprise library's URL for an Apprise API endpoint, http(s)://host:port/notify/<key>.

    JSON to /notify/<key> (version=1), as Apprise API expects, with one retry.
    The timeout bounds the connect (cto) as well as each read (rto), as
    urllib's did: apprise's own connect timeout is 4 s. Any query on the URL
    is dropped."""
    parsed = urllib.parse.urlsplit(url)
    scheme = "apprises" if parsed.scheme == "https" else "apprise"
    prefix, _, key = parsed.path.rpartition("/notify/")
    path = f"{prefix.strip('/')}/{key}".lstrip("/")
    return (
        f"{scheme}://{parsed.netloc}/{path}?method=json&version=1"
        f"&retry=1&wait={APPRISE_RETRY_WAIT_SECONDS}&cto={timeout}&rto={timeout}"
    )


def notify(urls, title, body, type_):
    """Send one notification and return whether it was delivered.

    urls holds one Apprise API URL or several separated by whitespace. Each is
    its own apprise priority group, so they are tried in order: the first that
    delivers wins, and each is retried once before the next. No URL returns
    False. apprise logs failures; it never raises."""
    notifier = apprise.Apprise()
    for priority, url in enumerate(urls.split(), start=1):
        notifier.add(apprise_api_url(url), tag=f"{priority}:backup-monitor")
    if not len(notifier):
        return False
    return bool(notifier.notify(title=title, body=body, notify_type=type_, tag="backup-monitor"))


def check_longhorn(get, now, max_age):
    """Problems with Longhorn backups. get(path) returns a Longhorn resource."""
    problems = []
    target = get(f"{LONGHORN}/backuptargets/default")
    if not target.get("status", {}).get("available"):
        problems.append(f"BackupTarget default not available: {target.get('status', {}).get('conditions')}")
    newest = {}
    for b in get(f"{LONGHORN}/backups")["items"]:
        st = b.get("status", {})
        if st.get("state") == "Completed" and st.get("backupCreatedAt"):
            v, t = st["volumeName"], ts(st["backupCreatedAt"])
            newest[v] = max(newest.get(v, t), t)
    for vol in get(f"{LONGHORN}/volumes")["items"]:
        name = vol["metadata"]["name"]
        pvc = vol.get("status", {}).get("kubernetesStatus", {})
        label = f"{pvc.get('namespace', '?')}/{pvc.get('pvcName', name)}"
        if now - ts(vol["metadata"]["creationTimestamp"]) < max_age:
            continue  # too new to have been backed up yet
        if name not in newest:
            problems.append(f"{label}: no completed backup")
        elif now - newest[name] > max_age:
            problems.append(f"{label}: last backup {hours(now - newest[name])} ago")
    return problems


def list_etcd_snapshots(bucket, prefix):
    """Creation times of the objects under prefix: through the S3 API when
    AWS_ACCESS_KEY_ID is set, else via OCI instance principals."""
    env = os.environ
    if env.get("AWS_ACCESS_KEY_ID"):
        return list_s3_objects(
            env["S3_ENDPOINT"],
            env["S3_REGION"],
            bucket,
            prefix,
            env["AWS_ACCESS_KEY_ID"],
            env["AWS_SECRET_ACCESS_KEY"],
        )
    import oci

    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.object_storage.ObjectStorageClient({}, signer=signer)
    ns = client.get_namespace().data
    objs = oci.pagination.list_call_get_all_results(
        client.list_objects, ns, bucket, prefix=prefix, fields="timeCreated"
    ).data.objects
    return [o.time_created for o in objs]


def sigv4_headers(method, url, region, access_key, secret_key, now):
    """Headers that sign a body-less request with AWS Signature Version 4 (service s3)."""
    parts = urllib.parse.urlsplit(url)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload = hashlib.sha256(b"").hexdigest()
    query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    headers = {"host": parts.netloc, "x-amz-content-sha256": payload, "x-amz-date": amz_date}
    signed = ";".join(headers)
    canonical = "\n".join(
        [method, parts.path or "/", query, *(f"{k}:{v}" for k, v in headers.items()), "", signed, payload]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = f"AWS4{secret_key}".encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return headers


def list_s3_objects(endpoint, region, bucket, prefix, access_key, secret_key):
    """LastModified of every object under prefix (ListObjectsV2, path-style)."""
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    times, token = [], None
    while True:
        query = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        url = f"{endpoint.rstrip('/')}/{urllib.parse.quote(bucket)}?{urllib.parse.urlencode(query)}"
        headers = sigv4_headers("GET", url, region, access_key, secret_key, dt.datetime.now(dt.UTC))
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310: the configured https S3 endpoint
            root = ET.fromstring(r.read())  # nosec B314: TLS-authenticated reply of the S3 endpoint
        times += [ts(e.text) for e in root.iter(f"{ns}LastModified")]
        token = root.findtext(f"{ns}NextContinuationToken")
        if root.findtext(f"{ns}IsTruncated") != "true" or not token:
            return times


def check_etcd(times, now, max_age, prefix):
    if not times:
        return [f"no etcd snapshots under {prefix}"]
    newest = max(times)
    if now - newest > max_age:
        return [f"newest etcd snapshot upload is {hours(now - newest)} old ({len(times)} kept)"]
    return []


def main():
    env = os.environ
    apprise_url = env["APPRISE_URL"]
    cluster = env.get("CLUSTER_NAME", "k3s")
    now = dt.datetime.now(dt.UTC)
    max_backup = dt.timedelta(hours=float(env["MAX_BACKUP_AGE_HOURS"]))
    max_etcd = dt.timedelta(hours=float(env["MAX_ETCD_AGE_HOURS"]))
    bucket, prefix = env["ETCD_BUCKET"], env["ETCD_PREFIX"]

    checks = (
        ("Longhorn backups", lambda: check_longhorn(kube_get, now, max_backup)),
        ("etcd snapshots", lambda: check_etcd(list_etcd_snapshots(bucket, prefix), now, max_etcd, prefix)),
    )
    failed = False
    for name, check in checks:
        try:
            problems = check()
        except Exception:
            problems = ["check failed:\n" + traceback.format_exc(limit=3)]
        if problems:
            failed = True
            print(f"{name}: " + "; ".join(problems))
            if not notify(apprise_url, f"{cluster}: {name} stale", "\n".join(problems), "failure"):
                print(f"{name}: the Apprise notification was not delivered", file=sys.stderr)
        else:
            print(f"{name}: OK")
    sys.exit(1 if failed else 0)
