# Mock OneFS API

A tiny fake of the OneFS Platform API endpoints used by `check-powerscale.py` and by the driver's
start-up probe, for testing without a PowerScale. Good enough to get the driver pods Running and the
StorageClass created; it cannot provision volumes (no filesystem, no NFS). It deliberately gives the user two privilege problems so the FAIL
output and the suggested `isi auth roles modify` command can be seen.

```bash
cd test/mock-onefs
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 30 -subj "/CN=mock-onefs"
python3 mock_onefs.py 8443 &
../../check-powerscale.py --endpoint 127.0.0.1 --port 8443 --user csiuser --password secret
```

To use it as the "array" for a driver install test, run it on the LAN address so the cluster nodes can
reach it, and point `secrets/isilon-creds.yaml` at it (`endpoint: <this host IP>`, `endpointPort: 8443`,
`username: csiuser`, `password: secret`):

```bash
systemd-run --unit=mock-onefs --working-directory=$PWD \
  -p StandardOutput=append:$PWD/mock.log -p StandardError=append:$PWD/mock.log \
  python3 mock_onefs.py 8443 0.0.0.0
tail -f mock.log            # every request is logged; unknown ones are marked UNMOCKED
systemctl stop mock-onefs   # when done
```
