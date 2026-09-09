#!/bin/bash
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
export KUBECONFIG=${KUBECONFIG:-/opt/ocpdeploy/clusters/homeshift/install/auth/kubeconfig}
export PATH="$HERE/bin:$PATH"
oc delete -f "$HERE/test/pvc-pod.yaml" --ignore-not-found
oc delete -f "$HERE/storageclass.yaml" --ignore-not-found
cd "$HERE/dell-csi-helm-installer" && ./csi-uninstall.sh --namespace isilon
oc -n isilon delete secret isilon-creds isilon-certs-0 --ignore-not-found
