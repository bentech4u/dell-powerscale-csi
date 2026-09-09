# Mock OneFS API

A tiny fake of the OneFS Platform API endpoints used by `check-powerscale.py`, for testing the
script without a PowerScale. It deliberately gives the user two privilege problems so the FAIL
output and the suggested `isi auth roles modify` command can be seen.

```bash
cd test/mock-onefs
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 30 -subj "/CN=mock-onefs"
python3 mock_onefs.py 8443 &
../../check-powerscale.py --endpoint 127.0.0.1 --port 8443 --user csiuser --password secret
```
