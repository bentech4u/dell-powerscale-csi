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
    def log_message(self, *a): pass
    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _authed(self):
        a = self.headers.get("Authorization", "")
        if a.startswith("Basic "):
            return base64.b64decode(a[6:]).decode() == f"{USER}:{PASS}"
        return "isisessid=mocksession" in self.headers.get("Cookie", "")
    def do_POST(self):
        if self.path == "/session/1/session":
            n = int(self.headers.get("Content-Length", 0)); b = json.loads(self.rfile.read(n))
            if b.get("username") == USER and b.get("password") == PASS:
                return self._send(201, {"timeout_absolute": 14400}, {"Set-Cookie": "isisessid=mocksession; Path=/; Secure, isicsrf=tok; Path=/"})
            return self._send(401, {"errors": [{"message": "bad login"}]})
        self._send(404, {})
    def do_PUT(self):
        p = urlparse(self.path).path
        if p.startswith("/namespace/") and self._authed() and self.headers.get("x-isi-ifs-target-type") == "container":
            PATHS.add(p[len("/namespace"):]); return self._send(200, {})
        self._send(403, {"errors": [{"message": "denied"}]})
    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        if p == "/platform/latest": return self._send(200, {"latest": "21"})
        if not self._authed(): return self._send(401, {"errors": [{"message": "Unauthorized"}]})
        if p == "/platform/1/cluster/identity": return self._send(200, {"name": "mock-powerscale", "description": ""})
        if p == "/platform/1/cluster/config": return self._send(200, {"onefs_version": {"release": "v9.8.0.0"}})
        if p == "/platform/1/zones": return self._send(200, {"zones": [{"name": "System", "path": "/ifs"}, {"name": "k8s", "path": "/ifs/k8s"}]})
        if p.startswith("/namespace/"):
            path = p[len("/namespace"):]
            if path in PATHS: return self._send(200, {"attrs": [{"name": "type", "value": "container"}, {"name": "mode", "value": "0777"}, {"name": "owner", "value": "root"}]})
            return self._send(404, {"errors": [{"message": "Path not found"}]})
        if p == "/platform/3/protocols/nfs/settings/global": return self._send(200, {"settings": {"service": True, "nfsv3_enabled": True, "nfsv4_enabled": False}})
        if p == "/platform/2/protocols/nfs/exports": return self._send(200, {"exports": [{"id": 1, "paths": ["/ifs/data/csi/old"]}]})
        if p == "/platform/1/license/licenses": return self._send(200, {"licenses": [{"name": "SmartQuotas", "status": "Licensed"}, {"name": "SnapshotIQ", "status": "Evaluation"}, {"name": "SyncIQ", "status": "Unlicensed"}]})
        if p == "/platform/1/auth/roles": return self._send(200, {"roles": [{"name": "CSIRole", "members": [{"name": USER}], "privileges": ROLE_PRIVS}, {"name": "SystemAdmin", "members": [{"name": "admin"}], "privileges": []}]})
        if p == "/platform/3/network/pools": return self._send(200, {"pools": [{"groupnet": "groupnet0", "subnet": "subnet0", "name": "pool0", "access_zone": "System", "sc_dns_zone": "mock-sc.example.invalid", "ranges": [{"low": "127.0.0.1", "high": "127.0.0.1"}]}]})
        self._send(404, {"errors": [{"message": f"no mock for {p}"}]})

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
srv = HTTPServer(("127.0.0.1", port), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("cert.pem", "key.pem")
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print(f"mock OneFS on https://127.0.0.1:{port}", flush=True); srv.serve_forever()
