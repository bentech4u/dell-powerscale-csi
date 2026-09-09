#!/bin/bash
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
export KUBECONFIG=${KUBECONFIG:-/opt/ocpdeploy/clusters/homeshift/install/auth/kubeconfig}
export PATH=/usr/local/bin:$PATH
kubectl delete -f "$HERE/test/pvc-pod.yaml" --ignore-not-found
kubectl delete -f "$HERE/storageclass.yaml" --ignore-not-found
cd "$HERE/csi-powerscale/dell-csi-helm-installer" && ./csi-uninstall.sh --namespace isilon
kubectl -n isilon delete secret isilon-creds isilon-certs-0 --ignore-not-found
