"""Daily freshness check for a k3s cluster's backups on Oracle Cloud.

Checks that every Longhorn volume has a completed backup newer than
MAX_BACKUP_AGE_HOURS and that the BackupTarget is available, and that the
newest k3s etcd snapshot in an OCI Object Storage bucket is newer than
MAX_ETCD_AGE_HOURS. Each check that finds a problem sends one Apprise
notification; the run exits non-zero if anything was stale, failed or could
not be reported.

Runs in-cluster: Longhorn is read through the Kubernetes API with the pod's
service account, OCI with instance-principal auth.
"""

import datetime as dt
import json
import os
import ssl
import sys
import traceback
import urllib.request

from apprise_notify import notify

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
LONGHORN = "apis/longhorn.io/v1beta2/namespaces/longhorn-system"


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
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)


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
    """Creation times of the objects under prefix, via OCI instance principals."""
    import oci

    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.object_storage.ObjectStorageClient({}, signer=signer)
    ns = client.get_namespace().data
    objs = oci.pagination.list_call_get_all_results(
        client.list_objects, ns, bucket, prefix=prefix, fields="timeCreated"
    ).data.objects
    return [o.time_created for o in objs]


def check_etcd(times, now, max_age, prefix):
    if not times:
        return [f"no etcd snapshots under {prefix}"]
    newest = max(times)
    if now - newest > max_age:
        return [f"newest etcd snapshot upload is {hours(now - newest)} old ({len(times)} kept)"]
    return []


def main():
    env = os.environ
    apprise = env["APPRISE_URL"]
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
            if not notify(apprise, f"{cluster}: {name} stale", "\n".join(problems), "failure"):
                print(f"{name}: the Apprise notification was not delivered", file=sys.stderr)
        else:
            print(f"{name}: OK")
    sys.exit(1 if failed else 0)
