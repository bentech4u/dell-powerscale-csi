#!/bin/bash
# Installs the Dell CSI PowerScale driver on the homeshift cluster using Dell's helm installer.
# Usage: ./install.sh [--upgrade]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
export KUBECONFIG=${KUBECONFIG:-/opt/ocpdeploy/clusters/homeshift/install/auth/kubeconfig}
export PATH=/usr/local/bin:$PATH
NS=isilon
VALUES="$HERE/my-isilon-settings.yaml"
CREDS="$HERE/secrets/isilon-creds.yaml"

if grep -q CHANGE_ME "$CREDS"; then
  echo "Edit $CREDS first (endpoint / username / password still say CHANGE_ME)." >&2; exit 1
fi

echo "==> Namespace $NS"
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

echo "==> Secrets"
kubectl -n "$NS" create secret generic isilon-creds --from-file=config="$CREDS" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$HERE/secrets/isilon-certs-0.yaml"

echo "==> Checking OneFS API reachability"
EP=$(grep -E '^\s*endpoint:' "$CREDS" | head -1 | sed -E 's/.*:\s*"?([^"]*)"?.*/\1/')
PORT=$(grep -E '^\s*endpointPort:' "$CREDS" | head -1 | sed -E 's/.*:\s*"?([^"]*)"?.*/\1/'); PORT=${PORT:-8080}
EP=${EP#https://}; EP=${EP#http://}
if ! curl -sk -m 10 -o /dev/null "https://$EP:$PORT/platform/latest"; then
  echo "WARNING: https://$EP:$PORT not reachable from this host; continuing anyway." >&2
fi

echo "==> Running Dell helm installer (node SSH checks skipped: RHCOS has no root SSH)"
cd "$HERE/dell-csi-helm-installer"
./csi-install.sh --namespace "$NS" --values "$VALUES" --skip-verify-node "$@"

echo "==> StorageClass"
kubectl apply -f "$HERE/storageclass.yaml"
kubectl -n "$NS" get pods -o wide
kubectl get sc
