#!/usr/bin/env python3
"""Tiny fake OneFS Platform API for exercising check-powerscale.py."""
import base64, json, ssl, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

USER, PASS = "csiuser", "secret"
PATHS = {"/ifs", "/ifs/data", "/ifs/data/csi"}
ROLE_PRIVS = [  # deliberately missing ISI_PRIV_IFS_BACKUP and QUOTA read-only, to exercise FAIL output
    {"id": "ISI_PRIV_LOGIN_PAPI", "read_only": True}, {"id": "ISI_PRIV_NFS", "read_only": False},
    {"id": "ISI_PRIV_QUOTA", "read_only": True}, {"id": "ISI_PRIV_SNAPSHOT", "read_only": False},
    {"id": "ISI_PRIV_IFS_RESTORE", "read_only": True}, {"id": "ISI_PRIV_NS_IFS_ACCESS", "read_only": True},
    {"id": "ISI_PRIV_AUTH_ZONES", "read_only": True}, {"id": "ISI_PRIV_STATISTICS", "read_only": True}]

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.command, self.path)); sys.stderr.flush()
    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # real OneFS echoes these; goisilon may look at them
        self.send_header("X-Frame-Options", "sameorigin")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _authed(self):
        a = self.headers.get("Authorization", "")
        if a.startswith("Basic "):
            return base64.b64decode(a[6:]).decode() == f"{USER}:{PASS}"
        return "isisessid=mocksession" in self.headers.get("Cookie", "")
    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") == "/session/1/session":
            n = int(self.headers.get("Content-Length", 0)); b = json.loads(self.rfile.read(n))
            if b.get("username") == USER and b.get("password") == PASS:
                return self._send(201, {"timeout_absolute": 14400}, {"Set-Cookie": "isisessid=mocksession; Path=/; Secure, isicsrf=tok; Path=/"})
            return self._send(401, {"errors": [{"message": "bad login"}]})
        self._send(404, {})
    def do_PUT(self):
        p = urlparse(self.path).path.rstrip("/")
        if p.startswith("/namespace/") and self._authed() and self.headers.get("x-isi-ifs-target-type") == "container":
            PATHS.add(p[len("/namespace"):]); return self._send(200, {})
        self._send(403, {"errors": [{"message": "denied"}]})
    def do_GET(self):
        u = urlparse(self.path); p = u.path.rstrip("/") or "/"; q = parse_qs(u.query)
        if p == "/platform/latest": return self._send(200, {"latest": "21"})
        if not self._authed(): return self._send(401, {"errors": [{"message": "Unauthorized"}]})
        import re
        p = re.sub(r"^/platform/\d+/", "/platform/N/", p)   # version-agnostic
        if p == "/platform/N/cluster/identity": return self._send(200, {"name": "mock-powerscale", "description": "", "logon": {"motd": "", "motd_header": ""}})
        if p == "/platform/N/cluster/config": return self._send(200, {
            "description": "", "devices": [{"devid": 1, "guid": "0050569e2d3c000000000000000000000000000000000001", "is_up": True, "lnn": 1}],
            "encoding": "utf-8", "guid": "0050569e2d3c0000deadbeef0000000000000001", "has_quorum": True, "is_compliance": False,
            "is_virtual": True, "is_vonefs": True, "join_mode": "manual", "local_devid": 1, "local_lnn": 1, "local_serial": "MOCK000001",
            "name": "mock-powerscale", "onefs_version": {"build": "B_9_8_0_0(RELEASE)", "release": "v9.8.0.0", "type": "Internal release", "version": "Isilon OneFS v9.8.0.0"},
            "timezone": {"abbreviation": "UTC", "custom": "", "name": "UTC", "path": "UTC"}, "upgrade_type": ""})
        if p == "/platform/N/zones": return self._send(200, {"zones": [{"name": "System", "path": "/ifs"}, {"name": "k8s", "path": "/ifs/k8s"}]})
        if p == "/platform/N/quota/quotas": return self._send(200, {"quotas": [], "resume": None})
        if p == "/platform/N/snapshot/snapshots": return self._send(200, {"snapshots": [], "resume": None, "total": 0})
        if p.startswith("/platform/N/statistics"): return self._send(200, {"stats": []})
        if p == "/platform/N/protocols/nfs/exports": return self._send(200, {"exports": [{"id": 1, "paths": ["/ifs/data/csi/old"], "zone": "System", "clients": [], "read_only": False}], "resume": None, "total": 1})
        if p.startswith("/namespace/"):
            path = p[len("/namespace"):]
            if path in PATHS: return self._send(200, {"attrs": [{"name": "type", "value": "container"}, {"name": "mode", "value": "0777"}, {"name": "owner", "value": "root"}]})
            return self._send(404, {"errors": [{"message": "Path not found"}]})
        if p == "/platform/N/protocols/nfs/settings/global": return self._send(200, {"settings": {"service": True, "nfsv3_enabled": True, "nfsv4_enabled": False}})
        if p == "/platform/N/license/licenses": return self._send(200, {"licenses": [{"name": "SmartQuotas", "status": "Licensed"}, {"name": "SnapshotIQ", "status": "Evaluation"}, {"name": "SyncIQ", "status": "Unlicensed"}]})
        if p == "/platform/N/auth/roles": return self._send(200, {"roles": [{"name": "CSIRole", "members": [{"name": USER}], "privileges": ROLE_PRIVS}, {"name": "SystemAdmin", "members": [{"name": "admin"}], "privileges": []}]})
        if p == "/platform/N/network/pools": return self._send(200, {"pools": [{"groupnet": "groupnet0", "subnet": "subnet0", "name": "pool0", "access_zone": "System", "sc_dns_zone": "mock-sc.example.invalid", "ranges": [{"low": "127.0.0.1", "high": "127.0.0.1"}]}]})
        sys.stderr.write(f"UNMOCKED GET {p}\n"); sys.stderr.flush()
        self._send(404, {"errors": [{"message": f"no mock for {p}"}]})
    def do_DELETE(self):
        sys.stderr.write(f"UNMOCKED DELETE {self.path}\n"); sys.stderr.flush(); self._send(204, {})

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
bind = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
srv = HTTPServer((bind, port), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("cert.pem", "key.pem")
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print(f"mock OneFS on https://{bind}:{port}", flush=True); srv.serve_forever()
