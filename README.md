# Dell CSI PowerScale (Isilon) driver on homeshift

Installs csi-powerscale **v2.17.1** on the `homeshift` OpenShift 4.22 cluster using Dell's
`dell-csi-helm-installer` scripts (https://github.com/dell/csi-powerscale/tree/main/dell-csi-helm-installer), copied into this repo unchanged.
The scripts clone https://github.com/dell/helm-charts at tag csi-isilon-2.17.1 on each run.

## Layout

```
dell-csi-helm-installer/     Dell's installer scripts, copied from csi-powerscale (CSM 1.17.1, Apache-2.0)
my-isilon-settings.yaml      helm values (from helm-charts tag csi-isilon-2.17.1)
secrets/isilon-creds.yaml    OneFS API credentials -> secret isilon-creds (git-ignored, fill in!)
secrets/isilon-certs-0.yaml  empty CA secret isilon-certs-0 (required even when skipping TLS checks)
storageclass.yaml            StorageClass "isilon" (NFS, RWX)
test/pvc-pod.yaml            smoke test: 1Gi RWX PVC + pod writing a file
install.sh / uninstall.sh    wrappers around csi-install.sh / csi-uninstall.sh
```

## Steps

1. Edit `secrets/isilon-creds.yaml`: endpoint, username, password, isiPath. On the array make sure
   `isiPath` (default `/ifs/data/csi`) exists in the chosen access zone and the user has the
   OneFS API privileges Dell documents (ISI_PRIV_LOGIN_PAPI, NFS, QUOTA, SNAPSHOT, ...).
2. `./install.sh` (creates namespace `isilon`, both secrets, runs the Dell installer, applies the StorageClass).
   Add `--upgrade` to re-apply changed values later.
3. `oc apply -f test/pvc-pod.yaml` and check `oc -n isilon get pvc,pod`.

## Using Dell's scripts directly

`install.sh` is only a convenience wrapper. The vendored Dell scripts can be run on their own from
the `dell-csi-helm-installer/` directory. They need `KUBECONFIG` exported, and the namespace and
secrets already created (steps below), which is what the wrapper does for you.

```bash
export KUBECONFIG=/opt/ocpdeploy/clusters/homeshift/install/auth/kubeconfig
cd /root/isilon/dell-csi-helm-installer

# one-time prerequisites (namespace + secrets)
oc create ns isilon
oc -n isilon create secret generic isilon-creds --from-file=config=../secrets/isilon-creds.yaml
oc apply -f ../secrets/isilon-certs-0.yaml

# verify only (no changes to the cluster)
./verify.sh --namespace isilon --values ../my-isilon-settings.yaml --driver-version v2.17.1 --skip-verify-node

# install (runs verify first, then helm install)
./csi-install.sh --namespace isilon --values ../my-isilon-settings.yaml --skip-verify-node

# upgrade / re-apply changed values
./csi-install.sh --namespace isilon --values ../my-isilon-settings.yaml --skip-verify-node --upgrade

# uninstall (removes the helm release; secrets and namespace stay)
./csi-uninstall.sh --namespace isilon
```

Notes on the flags:

* `--driver-version v2.17.1` is required when running `verify.sh` by hand. `csi-install.sh` passes it
  automatically; without it verify reports `Incompatible helm values file specified - expected: , found: v2.17.1`.
* `--skip-verify-node` skips the SSH checks against worker nodes. RHCOS does not allow root SSH, so
  without this flag the node checks fail. The NFS client is already present on RHCOS.
* `--skip-verify` skips all verification, not recommended.
* `-h` on any script prints its full usage.

## Notes

* Tools installed on this host: helm 3.19 in /usr/local/bin, `kubectl` symlinked to the cluster's `oc`.
* KUBECONFIG defaults to /opt/ocpdeploy/clusters/homeshift/install/auth/kubeconfig.
* Node SSH verification is skipped (RHCOS has no root SSH). NFS client is present on the nodes (nfs-utils).
* Dell's verify script flags OpenShift 4.22 as "newer than tested (4.21)"; that is a warning only.
* The driver needs the nodes to reach the array's NFS ports and the controller to reach the OneFS API (default 8080/tcp).
