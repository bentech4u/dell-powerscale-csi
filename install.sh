#!/bin/bash
# Installs the Dell CSI PowerScale driver on OpenShift using Dell's helm installer scripts.
# Offline-capable: helm binary in bin/, chart in helm-charts/ (no git clone at run time).
# Container images still have to be reachable from the cluster; see README "Offline install".
# Usage: ./install.sh [--upgrade]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
export KUBECONFIG=${KUBECONFIG:-/opt/ocpdeploy/clusters/homeshift/install/auth/kubeconfig}
export PATH="$HERE/bin:$PATH"          # vendored helm + kubectl->oc shim
NS=isilon
VALUES="$HERE/my-isilon-settings.yaml"
CREDS="$HERE/secrets/isilon-creds.yaml"

command -v oc >/dev/null || { echo "oc not found in PATH; install the OpenShift CLI first." >&2; exit 1; }
[ -d "$HERE/helm-charts/charts/csi-isilon" ] || { echo "helm-charts/ missing; chart is expected to be vendored in the repo." >&2; exit 1; }
if grep -q CHANGE_ME "$CREDS"; then
  echo "Edit $CREDS first (endpoint / username / password still say CHANGE_ME)." >&2; exit 1
fi

echo "==> Cluster: $(oc whoami --show-server) as $(oc whoami)"
oc get clusterversion version -o jsonpath='    OpenShift {.status.desired.version}{"\n"}' 2>/dev/null || echo "    (not OpenShift? Dell scripts will run in plain Kubernetes mode)"
echo "    helm $(helm version --short)"

echo "==> Namespace $NS"
oc get ns "$NS" >/dev/null 2>&1 || oc create ns "$NS"

echo "==> Secrets"
oc -n "$NS" create secret generic isilon-creds --from-file=config="$CREDS" \
  --dry-run=client -o yaml | oc apply -f -
oc apply -f "$HERE/secrets/isilon-certs-0.yaml"

echo "==> Checking OneFS API reachability"
EP=$(grep -E '^\s*endpoint:' "$CREDS" | head -1 | sed -E 's/.*:\s*"?([^"]*)"?.*/\1/')
PORT=$(grep -E '^\s*endpointPort:' "$CREDS" | head -1 | sed -E 's/.*:\s*"?([^"]*)"?.*/\1/'); PORT=${PORT:-8080}
EP=${EP#https://}; EP=${EP#http://}
if ! curl -sk -m 10 -o /dev/null "https://$EP:$PORT/platform/latest"; then
  echo "WARNING: https://$EP:$PORT not reachable from this host; continuing anyway." >&2
fi

echo "==> Running Dell helm installer (OpenShift is auto-detected; node SSH checks skipped: RHCOS has no root SSH)"
cd "$HERE/dell-csi-helm-installer"
./csi-install.sh --namespace "$NS" --values "$VALUES" --skip-verify-node "$@"

echo "==> StorageClass"
oc apply -f "$HERE/storageclass.yaml"
oc -n "$NS" get pods -o wide
oc get sc
