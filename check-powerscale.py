#!/usr/bin/env python3
"""Pre-flight check for the Dell CSI PowerScale driver.

Reads the OneFS credentials (default: secrets/isilon-creds.yaml), talks to the OneFS
Platform API and reports everything the driver needs, then prints the values to use for
my-isilon-settings.yaml / the secret / the StorageClass.

Only the Python standard library is used (PyYAML is optional).

Examples:
  ./check-powerscale.py                                   # use secrets/isilon-creds.yaml
  ./check-powerscale.py --endpoint 10.0.0.5 --user csi    # prompt for password
  ./check-powerscale.py --zone zone1 --path /ifs/k8s      # check a different zone/path
  ./check-powerscale.py --from-node                       # also test reachability from a cluster node
  ./check-powerscale.py --create-path                     # create isiPath if it does not exist
"""
import argparse
import base64
import getpass
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Dell CSM docs, "PowerScale" helm installation page: privileges the API user must hold.
REQUIRED_PRIVS = {
    "ISI_PRIV_LOGIN_PAPI": "r",
    "ISI_PRIV_NFS": "rw",
    "ISI_PRIV_QUOTA": "rw",
    "ISI_PRIV_SNAPSHOT": "rw",
    "ISI_PRIV_IFS_RESTORE": "r",
    "ISI_PRIV_NS_IFS_ACCESS": "r",
    "ISI_PRIV_IFS_BACKUP": "r",
    "ISI_PRIV_AUTH_ZONES": "r",
    "ISI_PRIV_STATISTICS": "r",
}
OPTIONAL_PRIVS = {"ISI_PRIV_SYNCIQ": "rw (replication only)"}

C = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m", "off": "\033[0m", "dim": "\033[2m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}

results = []          # (level, check, detail)
suggest = {}          # values discovered for the final YAML block


def report(level, check, detail=""):
    results.append((level, check, detail))
    tag = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}[level]
    col = C.get(level, "")
    print(f"  {col}{tag:4}{C['off']}  {check:<44} {C['dim']}{detail}{C['off']}")


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


# ----------------------------------------------------------------------------- credentials
def load_creds(path):
    """Return dict for the first (or default) cluster in a Dell isilon-creds file."""
    with open(path) as f:
        text = f.read()
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(text)
        clusters = doc.get("isilonClusters", [])
    except ImportError:
        # minimal parser: flat "key: value" lines under the first "- clusterName:" item
        clusters, cur = [], None
        for line in text.splitlines():
            line = line.split("#", 1)[0].rstrip()
            m = re.match(r"^\s*-?\s*([A-Za-z]+):\s*(.*)$", line)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
            if line.lstrip().startswith("-"):
                cur = {}
                clusters.append(cur)
            if cur is not None:
                cur[k] = v
    if not clusters:
        sys.exit(f"no isilonClusters found in {path}")
    for c in clusters:
        if str(c.get("isDefault", "")).lower() == "true":
            return c
    return clusters[0]


# ----------------------------------------------------------------------------- HTTP client
class OneFS:
    def __init__(self, host, port, user, password, verify=False, timeout=15):
        self.base = f"https://{host}:{port}"
        self.user, self.password, self.timeout = user, password, timeout
        self.ctx = ssl.create_default_context()
        if not verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.cookie = None
        self.csrf = None

    def _req(self, method, path, body=None, auth="basic", raw=False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth == "basic":
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        elif auth == "session" and self.cookie:
            req.add_header("Cookie", self.cookie)
            req.add_header("X-CSRF-Token", self.csrf or "")
            req.add_header("Referer", self.base)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                payload = r.read()
                return r.status, dict(r.headers), (payload if raw else _json(payload))
        except urllib.error.HTTPError as e:
            payload = e.read()
            return e.code, dict(e.headers), (payload if raw else _json(payload))

    def get(self, path, auth="basic"):
        return self._req("GET", path, auth=auth)

    def login_session(self):
        body = {"username": self.user, "password": self.password, "services": ["platform", "namespace"]}
        status, headers, data = self._req("POST", "/session/1/session", body, auth=None)
        if status in (200, 201):
            cookies = headers.get("Set-Cookie", "")
            sess = re.search(r"isisessid=([^;]+)", cookies)
            csrf = re.search(r"isicsrf=([^;]+)", cookies)
            if sess:
                self.cookie = f"isisessid={sess.group(1)}" + (f"; isicsrf={csrf.group(1)}" if csrf else "")
                self.csrf = csrf.group(1) if csrf else None
        return status, data


def _json(payload):
    try:
        return json.loads(payload.decode() or "null")
    except Exception:
        return payload.decode(errors="replace")


def err_text(data):
    if isinstance(data, dict) and "errors" in data:
        return "; ".join(e.get("message", str(e)) for e in data["errors"])
    return str(data)[:200]


# ----------------------------------------------------------------------------- checks
def tcp_check(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except Exception as e:
        return False, str(e)


def tls_info(host, port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=8) as s:
        with ctx.wrap_socket(s, server_hostname=host) as t:
            der = t.getpeercert(binary_form=True)
            # a second, verifying handshake tells us if the system trust store accepts it
    verified = True
    try:
        vctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as s:
            with vctx.wrap_socket(s, server_hostname=host):
                pass
    except Exception:
        verified = False
    subject = issuer = "?"
    not_after = None
    try:
        # openssl gives us readable subject/issuer without extra modules
        out = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-subject", "-issuer", "-enddate"],
                             input=der, capture_output=True, timeout=10).stdout.decode()
        subject = re.search(r"subject=(.*)", out).group(1).strip()
        issuer = re.search(r"issuer=(.*)", out).group(1).strip()
        end = re.search(r"notAfter=(.*)", out).group(1).strip()
        not_after = datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return verified, subject, issuer, not_after


def run_from_node(host, api_port, nfs_host):
    """Test TCP reachability from a cluster worker via `oc debug node`."""
    oc = os.environ.copy()
    try:
        nodes = subprocess.run(["oc", "get", "nodes", "-l", "node-role.kubernetes.io/worker", "-o",
                                "jsonpath={.items[0].metadata.name}"], capture_output=True, text=True,
                               timeout=30, env=oc).stdout.strip()
    except Exception as e:
        report("warn", "cluster node reachability", f"oc not usable: {e}")
        return
    if not nodes:
        report("warn", "cluster node reachability", "no worker node found")
        return
    script = (f"for t in {host}:{api_port} {nfs_host}:2049 {nfs_host}:111; do "
              f"h=${{t%%:*}}; p=${{t##*:}}; "
              f"if timeout 5 bash -c \"</dev/tcp/$h/$p\" 2>/dev/null; then echo \"$t open\"; else echo \"$t CLOSED\"; fi; done")
    try:
        out = subprocess.run(["oc", "debug", f"node/{nodes}", "-q", "--", "chroot", "/host", "bash", "-c", script],
                             capture_output=True, text=True, timeout=120, env=oc).stdout
    except Exception as e:
        report("warn", "cluster node reachability", f"oc debug failed: {e}")
        return
    for line in out.splitlines():
        if "open" in line:
            report("ok", f"from {nodes.split('-')[-1]}: {line.split()[0]}", "reachable")
        elif "CLOSED" in line:
            report("fail", f"from node: {line.split()[0]}", "not reachable from the cluster node")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--creds", default=os.path.join(here, "secrets", "isilon-creds.yaml"))
    ap.add_argument("--endpoint", help="OneFS API host/IP (overrides creds file)")
    ap.add_argument("--port", type=int, help="OneFS API port (default from creds, else 8080)")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--zone", help="access zone to validate (default from creds/values, else System)")
    ap.add_argument("--path", help="isiPath to validate (default from creds, else /ifs/data/csi)")
    ap.add_argument("--nfs-host", help="SmartConnect name/IP used for NFS traffic (default: endpoint)")
    ap.add_argument("--from-node", action="store_true", help="also test reachability from a cluster worker (needs oc + KUBECONFIG)")
    ap.add_argument("--create-path", action="store_true", help="create isiPath on the array if missing")
    ap.add_argument("--verify-tls", action="store_true", help="fail instead of warn on untrusted certificate")
    ap.add_argument("--json", action="store_true", help="also write results to check-powerscale.json")
    a = ap.parse_args()

    creds = {}
    if os.path.exists(a.creds) and not (a.endpoint and a.user and a.password):
        creds = load_creds(a.creds)
    host = (a.endpoint or creds.get("endpoint") or "").replace("https://", "").replace("http://", "").strip("/")
    port = a.port or int(creds.get("endpointPort") or 8080)
    user = a.user or creds.get("username") or ""
    password = a.password or creds.get("password") or ""
    zone = a.zone or creds.get("isiAccessZone") or "System"
    ipath = (a.path or creds.get("isiPath") or "/ifs/data/csi").rstrip("/")
    nfs_host = a.nfs_host or host
    if not host or "CHANGE_ME" in (host + user + password):
        sys.exit("endpoint/user/password not set; edit the creds file or pass --endpoint --user --password")
    if not password:
        password = getpass.getpass(f"OneFS password for {user}@{host}: ")

    print(f"PowerScale pre-flight  endpoint={host}:{port}  user={user}  zone={zone}  isiPath={ipath}")
    suggest.update(endpoint=host, endpointPort=port, username=user, isiPathRequested=ipath)

    # --- network ---------------------------------------------------------------
    section("Network")
    ip = host
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        try:
            ip = socket.gethostbyname(host)
            report("ok", f"DNS {host}", ip)
        except Exception as e:
            report("fail", f"DNS {host}", str(e))
            finish(a)
            return
    ok, msg = tcp_check(ip, port)
    report("ok" if ok else "fail", f"TCP {host}:{port} (OneFS API)", "" if ok else msg)
    if not ok:
        finish(a)
        return
    nfs_ip = nfs_host
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", nfs_host):
        try:
            nfs_ip = socket.gethostbyname(nfs_host)
        except Exception as e:
            report("fail", f"DNS {nfs_host} (NFS host)", str(e))
    for p, what in ((2049, "NFS"), (111, "rpcbind/portmapper")):
        ok, msg = tcp_check(nfs_ip, p)
        report("ok" if ok else ("fail" if p == 2049 else "warn"), f"TCP {nfs_host}:{p} ({what})", "" if ok else msg)

    # --- TLS -------------------------------------------------------------------
    section("TLS")
    try:
        verified, subject, issuer, not_after = tls_info(ip, port)
        report("info", "certificate subject", subject)
        report("info", "certificate issuer", issuer)
        if not_after:
            days = (not_after - datetime.now(timezone.utc)).days
            report("ok" if days > 30 else "warn", "certificate expiry", f"{not_after:%Y-%m-%d} ({days} days)")
        if verified:
            report("ok", "certificate trusted by this host", "skipCertificateValidation: false is possible")
            suggest["skipCertificateValidation"] = False
        else:
            report("fail" if a.verify_tls else "warn", "certificate not trusted (self-signed?)",
                   "use skipCertificateValidation: true, or put the CA in isilon-certs-0")
            suggest["skipCertificateValidation"] = True
    except Exception as e:
        report("warn", "TLS handshake", str(e))

    api = OneFS(ip, port, user, password)

    # --- API & auth ------------------------------------------------------------
    section("OneFS API and authentication")
    st, _, d = api._req("GET", "/platform/latest", auth=None)
    if st == 200 and isinstance(d, dict):
        report("ok", "GET /platform/latest", f"API version {d.get('latest')}")
    elif st == 401:
        report("ok", "GET /platform/latest", "reachable (auth required)")
    else:
        report("fail", "GET /platform/latest", f"HTTP {st} {err_text(d)}")

    st, _, d = api.get("/platform/1/cluster/identity")
    basic_ok = st == 200
    if basic_ok:
        report("ok", "basic auth (isiAuthType 0)", f"cluster '{d.get('name')}'")
        suggest["clusterName"] = d.get("name")
    else:
        report("fail", "basic auth (isiAuthType 0)", f"HTTP {st} {err_text(d)}")
        finish(a)
        return

    st, d = api.login_session()
    if st in (200, 201) and api.cookie:
        report("ok", "session auth (isiAuthType 1)", "session cookie issued")
        suggest["isiAuthType"] = 1
        st2, _, d2 = api.get("/platform/1/cluster/identity", auth="session")
        if st2 != 200:
            report("warn", "session reuse", f"HTTP {st2} {err_text(d2)}; falling back to isiAuthType 0")
            suggest["isiAuthType"] = 0
    else:
        report("warn", "session auth (isiAuthType 1)", f"HTTP {st} {err_text(d)}; use isiAuthType: 0")
        suggest["isiAuthType"] = 0

    st, _, d = api.get("/platform/1/cluster/config")
    if st == 200:
        rel = d.get("onefs_version", {}).get("release", "?")
        report("info", "OneFS version", f"{rel}  (driver 2.17 supports 9.5 - 9.10)")
        suggest["onefs"] = rel
        try:
            major, minor = (int(x) for x in rel.lstrip("v").split(".")[:2])
            if (major, minor) < (9, 5):
                report("warn", "OneFS version support", "older than 9.5; check Dell's support matrix")
        except Exception:
            pass

    # --- access zones ----------------------------------------------------------
    section("Access zones")
    st, _, d = api.get("/platform/1/zones")
    zones = {}
    if st == 200:
        for z in d.get("zones", []):
            zones[z["name"]] = z.get("path", "")
        report("info", "zones on cluster", ", ".join(f"{n} ({p})" for n, p in zones.items()))
        if zone in zones:
            report("ok", f"zone '{zone}' exists", f"root path {zones[zone]}")
            suggest["isiAccessZone"] = zone
            if not (ipath + "/").startswith(zones[zone].rstrip("/") + "/") and zones[zone] != "/ifs":
                report("fail", f"isiPath under zone '{zone}' root", f"{ipath} is outside {zones[zone]}")
        else:
            report("fail", f"zone '{zone}' exists", "not found; pick one of the zones above")
    else:
        report("warn", "list access zones", f"HTTP {st} {err_text(d)} (needs ISI_PRIV_AUTH_ZONES)")

    # --- isiPath ---------------------------------------------------------------
    section("Base path (isiPath)")
    ns = "/namespace" + ipath
    st, h, d = api.get(ns + "?metadata")
    if st == 200:
        attrs = {x.get("name"): x.get("value") for x in (d.get("attrs", []) if isinstance(d, dict) else [])}
        report("ok", f"{ipath} exists", f"type={attrs.get('type', '?')} mode={attrs.get('mode', '?')} owner={attrs.get('owner', '?')}")
        suggest["isiPath"] = ipath
    elif st == 404:
        if a.create_path:
            req_ok = _mkdir(api, ns)
            report("ok" if req_ok else "fail", f"create {ipath}", "" if req_ok else "PUT failed; create it on the array")
            if req_ok:
                suggest["isiPath"] = ipath
        else:
            report("fail", f"{ipath} exists", "missing; create it on the array (or rerun with --create-path)")
    else:
        report("warn", f"{ipath} exists", f"HTTP {st} {err_text(d)}")

    # --- NFS & licenses --------------------------------------------------------
    section("NFS and licenses")
    st, _, d = api.get("/platform/3/protocols/nfs/settings/global")
    if st == 200:
        s = d.get("settings", d)
        report("ok" if s.get("service") else "fail", "NFS service enabled", "" if s.get("service") else "enable: isi services nfs enable")
        report("info", "NFS protocol versions", f"v3={'on' if s.get('nfsv3_enabled') else 'off'}  v4={'on' if s.get('nfsv4_enabled') else 'off'}")
        suggest["nfsv4"] = bool(s.get("nfsv4_enabled"))
    else:
        report("warn", "NFS global settings", f"HTTP {st} {err_text(d)} (needs ISI_PRIV_NFS)")
    st, _, d = api.get(f"/platform/2/protocols/nfs/exports?zone={urllib.parse.quote(zone)}&limit=1000")
    if st == 200:
        under = [e for e in d.get("exports", []) if any(p.startswith(ipath) for p in e.get("paths", []))]
        report("info", f"existing NFS exports under {ipath} in '{zone}'", str(len(under)))
    st, _, d = api.get("/platform/1/license/licenses")
    if st == 200:
        lic = {l.get("name"): l.get("status") for l in d.get("licenses", [])}
        for name, need in (("SmartQuotas", "enableQuota: true needs it"), ("SnapshotIQ", "volume snapshots need it"),
                           ("SyncIQ", "replication only")):
            status = lic.get(name, "not listed")
            good = status in ("Licensed", "Evaluation", "Activated")
            level = "ok" if good else ("warn" if name != "SmartQuotas" else "fail")
            report(level, f"license {name}", f"{status}  ({need})")
            if name == "SmartQuotas":
                suggest["enableQuota"] = good
    else:
        report("warn", "licenses", f"HTTP {st} {err_text(d)}")

    # --- privileges ------------------------------------------------------------
    section(f"Privileges of '{user}' (roles in zone System)")
    st, _, d = api.get("/platform/1/auth/roles?zone=System")
    have = {}
    if st == 200:
        my_roles = []
        for r in d.get("roles", []):
            names = {m.get("name", "").lower() for m in r.get("members", [])}
            if user.lower() in names:
                my_roles.append(r["name"])
                for p in r.get("privileges", []):
                    pid = p.get("id")
                    rw = not p.get("read_only", True)
                    have[pid] = have.get(pid, False) or rw
        report("info", "roles", ", ".join(my_roles) or "none found by member name")
        missing = []
        for pid, need in REQUIRED_PRIVS.items():
            if pid not in have:
                missing.append(pid)
                report("fail", pid, f"missing (needs {need})")
            elif need == "rw" and not have[pid]:
                missing.append(pid)
                report("fail", pid, "read-only, needs read/write")
            else:
                report("ok", pid, "rw" if have[pid] else "r")
        for pid, note in OPTIONAL_PRIVS.items():
            report("info", pid, ("present" if pid in have else "absent") + f"  ({note})")
        if missing and my_roles:
            role = my_roles[0]
            rd = " ".join(f"--add-priv-read {p}" for p in missing if REQUIRED_PRIVS[p] == "r")
            wr = " ".join(f"--add-priv-write {p}" for p in missing if REQUIRED_PRIVS[p] == "rw")
            print(f"\n  fix on the array:  isi auth roles modify {role} --zone System {rd} {wr}".rstrip())
        elif missing:
            print("\n  fix on the array (example):\n"
                  f"    isi auth roles create CSIRole --zone System\n"
                  f"    isi auth roles modify CSIRole --zone System --add-user {user} "
                  + " ".join(f"--add-priv-read {p}" for p, n in REQUIRED_PRIVS.items() if n == "r") + " "
                  + " ".join(f"--add-priv-write {p}" for p, n in REQUIRED_PRIVS.items() if n == "rw"))
    else:
        report("warn", "list roles", f"HTTP {st} {err_text(d)} (user cannot read roles; verify privileges manually)")

    # --- SmartConnect / network pools -----------------------------------------
    section("Network pools (SmartConnect)")
    st, _, d = api.get("/platform/3/network/pools")
    if st == 200:
        pools = d.get("pools", [])
        for p in pools:
            rng = ", ".join(f"{r.get('low')}-{r.get('high')}" for r in p.get("ranges", []))
            sc = p.get("sc_dns_zone") or "-"
            report("info", f"pool {p.get('groupnet')}.{p.get('subnet')}.{p.get('name')}",
                   f"zone={p.get('access_zone')}  smartconnect={sc}  ips={rng}")
        mine = [p for p in pools if p.get("access_zone") == zone]
        if mine:
            p = mine[0]
            suggest["AzServiceIP"] = p.get("sc_dns_zone") or (p.get("ranges") or [{}])[0].get("low")
            if p.get("sc_dns_zone"):
                try:
                    socket.gethostbyname(p["sc_dns_zone"])
                    report("ok", f"SmartConnect name resolves", p["sc_dns_zone"])
                except Exception:
                    report("warn", "SmartConnect name does not resolve from here", p["sc_dns_zone"] +
                           " (nodes need a DNS delegation to the SmartConnect service IP)")
        else:
            report("warn", f"no network pool bound to zone '{zone}'", "NFS clients would use the API address")
    else:
        report("warn", "network pools", f"HTTP {st} {err_text(d)}")

    if a.from_node:
        section("Reachability from a cluster node")
        run_from_node(host, port, suggest.get("AzServiceIP") or nfs_host)

    finish(a)


def _mkdir(api, ns_path):
    req = urllib.request.Request(api.base + ns_path, method="PUT")
    token = base64.b64encode(f"{api.user}:{api.password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("x-isi-ifs-target-type", "container")
    req.add_header("x-isi-ifs-access-control", "0777")
    try:
        with urllib.request.urlopen(req, timeout=api.timeout, context=api.ctx) as r:
            return r.status in (200, 201)
    except urllib.error.HTTPError:
        return False


def finish(a):
    fails = [r for r in results if r[0] == "fail"]
    warns = [r for r in results if r[0] == "warn"]
    section("Summary")
    print(f"  {len(fails)} failed, {len(warns)} warnings")
    if suggest.get("clusterName") or suggest.get("isiAccessZone"):
        q = json.dumps
        cluster = suggest.get("clusterName") or "powerscale1"
        zone = suggest.get("isiAccessZone", "System")
        ipath = suggest.get("isiPath") or suggest.get("isiPathRequested", "/ifs/data/csi")
        skip = suggest.get("skipCertificateValidation", True)
        print("\n  secrets/isilon-creds.yaml (secret isilon-creds):")
        print(f"    isilonClusters:\n"
              f"      - clusterName: {q(cluster)}\n"
              f"        username: {q(suggest.get('username', ''))}\n"
              f"        password: \"<password>\"\n"
              f"        endpoint: {q(suggest.get('endpoint', ''))}\n"
              f"        endpointPort: {suggest.get('endpointPort', 8080)}\n"
              f"        isDefault: true\n"
              f"        skipCertificateValidation: {q(skip)}\n"
              f"        isiPath: {q(ipath)}\n"
              f"        isiVolumePathPermissions: \"0777\"")
        print("\n  my-isilon-settings.yaml (driver defaults):")
        print(f"    endpointPort: {suggest.get('endpointPort', 8080)}\n"
              f"    skipCertificateValidation: {q(skip)}\n"
              f"    isiAuthType: {suggest.get('isiAuthType', 1)}\n"
              f"    isiAccessZone: {q(zone)}\n"
              f"    enableQuota: {q(suggest.get('enableQuota', True))}\n"
              f"    isiPath: {q(ipath)}")
        print("\n  storageclass.yaml (parameters):")
        print(f"    ClusterName: {q(cluster)}\n"
              f"    AccessZone: {q(zone)}\n"
              f"    IsiPath: {q(ipath)}\n"
              f"    AzServiceIP: {q(suggest.get('AzServiceIP') or suggest.get('endpoint', ''))}"
              + ("" if suggest.get("AzServiceIP") else "   # no SmartConnect pool found for the zone; API address used")
              + "\n    RootClientEnabled: \"false\"")
    if a.json:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check-powerscale.json")
        with open(out, "w") as f:
            json.dump({"results": [dict(level=l, check=c, detail=d) for l, c, d in results], "suggest": suggest}, f, indent=2)
        print(f"\n  written {out}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
