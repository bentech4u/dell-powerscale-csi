# Dell CSI PowerScale (Isilon) driver on homeshift

Installs csi-powerscale **v2.17.1** on the `homeshift` OpenShift 4.22 cluster using Dell's
`dell-csi-helm-installer` scripts (https://github.com/dell/csi-powerscale/tree/main/dell-csi-helm-installer), copied into this repo unchanged.
The scripts clone https://github.com/dell/helm-charts at tag csi-isilon-2.17.1 on each run.

## Layout

```
dell-csi-helm-installer/     Dell's installer scripts, copied from csi-powerscale (CSM 1.17.1, Apache-2.0)
helm-charts/                 Dell helm-charts checkout at tag csi-isilon-2.17.1 (vendored, no clone at run time)
bin/helm                     helm v3.19.0 linux/amd64 (+ .sha256); bin/kubectl is a shim that execs oc
images.txt                   container images the driver pulls (for mirroring in disconnected clusters)
my-isilon-settings.yaml      helm values (from helm-charts tag csi-isilon-2.17.1)
secrets/isilon-creds.yaml    OneFS API credentials -> secret isilon-creds (git-ignored, fill in!)
secrets/isilon-certs-0.yaml  empty CA secret isilon-certs-0 (required even when skipping TLS checks)
storageclass.yaml            StorageClass "isilon" (NFS, RWX)
test/pvc-pod.yaml            smoke test: 1Gi RWX PVC + pod writing a file
install.sh / uninstall.sh    wrappers around csi-install.sh / csi-uninstall.sh
check-powerscale.py          pre-flight against the OneFS API: connectivity, TLS, auth type, zones, isiPath,
                             NFS, licenses, API-user privileges, SmartConnect pools -> suggested values
```

## Steps

1. Edit `secrets/isilon-creds.yaml`: endpoint, username, password, isiPath. On the array make sure
   `isiPath` (default `/ifs/data/csi`) exists in the chosen access zone and the user has the
   OneFS API privileges Dell documents (ISI_PRIV_LOGIN_PAPI, NFS, QUOTA, SNAPSHOT, ...).
2. `./check-powerscale.py` to validate the array side and get the values for `isiAccessZone`,
   `isiPath`, `isiAuthType`, `skipCertificateValidation`, `enableQuota` and the StorageClass
   `AzServiceIP`. Fix anything marked FAIL (it prints the `isi auth roles modify` command for missing
   privileges; `--create-path` creates `isiPath`; `--from-node` also tests reachability from a worker).
3. `./install.sh` (creates namespace `isilon`, both secrets, runs the Dell installer, applies the StorageClass).
   Add `--upgrade` to re-apply changed values later.
4. `oc apply -f test/pvc-pod.yaml` and check `oc -n isilon get pvc,pod`.

## Pre-flight: check-powerscale.py

Python 3 standard library only. It reads `secrets/isilon-creds.yaml` by default, or takes
`--endpoint/--port/--user/--password` (password is prompted if omitted). Exit code 1 if anything failed.

```bash
./check-powerscale.py                          # from the creds file
./check-powerscale.py --zone k8s --path /ifs/k8s/csi --create-path
./check-powerscale.py --from-node              # add TCP tests from a worker node (uses oc debug)
./check-powerscale.py --json                   # also write check-powerscale.json
```

What it checks and where the driver uses it:

| Section | Checks | Drives |
|---|---|---|
| Network | DNS, TCP to API port, TCP 2049/111 to the NFS host | `endpoint`, `endpointPort`, `AzServiceIP` |
| TLS | subject/issuer/expiry, trusted by this host? | `skipCertificateValidation` / `isilon-certs-0` |
| API and auth | `/platform/latest`, basic auth, session auth, OneFS version | `isiAuthType` (1 if sessions work) |
| Access zones | zone exists, isiPath is under the zone root | `isiAccessZone` |
| Base path | `isiPath` exists (mode/owner), optional create | `isiPath` |
| NFS and licenses | NFS service, v3/v4, exports under the path, SmartQuotas/SnapshotIQ/SyncIQ | `enableQuota`, snapshots, replication |
| Privileges | roles of the API user vs Dell's required list, prints the fix command | secret user |
| Network pools | SmartConnect zone names and IP ranges per access zone | StorageClass `AzServiceIP` |

The summary ends with three ready-to-paste blocks: the `isilonClusters` entry for
`secrets/isilon-creds.yaml` (clusterName, username, endpoint, endpointPort, skipCertificateValidation,
isiPath), the matching defaults for `my-isilon-settings.yaml` (isiAuthType, isiAccessZone, isiPath,
enableQuota, ...) and the `parameters` for `storageclass.yaml` (ClusterName, AccessZone, IsiPath,
AzServiceIP).

Required privileges (Dell CSM docs): LOGIN_PAPI r, NFS rw, QUOTA rw, SNAPSHOT rw, IFS_RESTORE r,
NS_IFS_ACCESS r, IFS_BACKUP r, AUTH_ZONES r, STATISTICS r; SYNCIQ rw only for replication.

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

## OpenShift

There is no separate OpenShift install. Dell's `csi-install.sh` checks for the
`securitycontextconstraints.security.openshift.io` CRD and, when found, runs helm with
`--set openshift=true`, which makes the chart add the privileged SCC bindings and the OpenShift CSI
annotations. The scripts only ever call `kubectl`; on OpenShift `oc` is a superset, so `bin/kubectl`
is a one-line shim that runs `oc`. `install.sh` uses `oc` directly for everything it does itself.

## Offline install

What is in the repo and needs no internet on the installer host:

* `bin/helm` (v3.19.0). Verify with `sha256sum -c bin/helm.sha256`.
* `helm-charts/` at tag csi-isilon-2.17.1. `csi-install.sh` skips its `git clone` when this directory
  exists next to `dell-csi-helm-installer/`.
* `dell-csi-helm-installer/` scripts and `my-isilon-settings.yaml`.

What is **not** in the repo: `oc` (ship it with your cluster tooling) and the container images. The
cluster nodes pull the images in `images.txt` from quay.io and registry.k8s.io. For a disconnected
cluster, mirror them to your registry and point the cluster at it, for example:

```bash
# on a connected host with podman/oc logged in to your registry
while read img; do oc image mirror "$img" "myregistry.bentech.work:5000/${img#*/}"; done < images.txt
```

then either edit the `images:` section of `my-isilon-settings.yaml` to the mirrored names, or create an
`ImageDigestMirrorSet`/`ImageTagMirrorSet` mapping `quay.io/dell` and `registry.k8s.io/sig-storage` to
your registry. Only the seven images referenced by the default values (driver + six sidecars) are needed
unless you enable replication, authorization or podmon.

## Notes

* `install.sh` prepends `bin/` to PATH, so the vendored helm is used and Dell's `kubectl` calls go to `oc`.
  Only `oc` has to be present on the host.
* KUBECONFIG defaults to /opt/ocpdeploy/clusters/homeshift/install/auth/kubeconfig.
* Node SSH verification is skipped (RHCOS has no root SSH). NFS client is present on the nodes (nfs-utils).
* Dell's verify script flags OpenShift 4.22 as "newer than tested (4.21)"; that is a warning only.
  `csi-install.sh` then prompts "Press y to continue" (its `-Y` flag is printed in a message but never
  parsed), so `install.sh` feeds the answers on stdin to stay unattended.
* The helm release is named `isilon` (Dell strips the `csi-` prefix from the driver name).
* Proven on homeshift 2026-09-09 with the mock array (test/mock-onefs): helm install/upgrade on 4.22.12,
  SCC bindings, image pulls, controller 6/6 and node 2/2 Running on all three workers, CSIDriver and
  CSINode registrations, StorageClass `isilon` created. PV provisioning needs a real OneFS.
* Driver start-up probe calls, in order: `GET /platform/latest/`, `POST /session/1/session/`
  (isiAuthType 1), `GET /platform/3/cluster/config/`. All with trailing slashes.
* On OpenShift the node plugin derives the node FQDN by reverse DNS of the node IP, which resolves to
  `<ip>.kube-rbac-proxy-crio.openshift-machine-config-operator.svc.cluster.local`. The driver puts both
  that name and the IP in the NFS export client list, so the IP is what matters on the array.
* The driver needs the nodes to reach the array's NFS ports and the controller to reach the OneFS API (default 8080/tcp).

## Links

* Dell CSI PowerScale driver: https://github.com/dell/csi-powerscale
* Helm installer scripts (vendored here): https://github.com/dell/csi-powerscale/tree/main/dell-csi-helm-installer
* Helm charts: https://github.com/dell/helm-charts (tag `csi-isilon-2.17.1`, `charts/csi-isilon`)
* CSM docs, PowerScale helm install (prerequisites, values reference, privileges): https://dell.github.io/csm-docs/docs/getting-started/installation/kubernetes/powerscale/helm/
* CSM docs, PowerScale troubleshooting: https://dell.github.io/csm-docs/docs/concepts/csidriver/troubleshooting/powerscale/
* CSM support matrix (OneFS / OpenShift versions): https://dell.github.io/csm-docs/docs/getting-started/supportmatrix/
* cert-csi (Dell's CSI conformance tool, suggested for untested OpenShift versions): https://dell.github.io/csm-docs/docs/support/cert-csi/
* CSM operator alternative in OperatorHub: `dell-csm-operator-certified` (Certified Operators catalog)
* OneFS Simulator (free VMware OVA, real OneFS for lab testing; Dell support login needed):
  https://www.dell.com/support/kbdoc/en-us/000021453/how-to-download-the-onefs-simulator
  (also listed under Downloads for PowerScale OneFS on https://www.dell.com/support/home/en-us/product-support/product/isilon-onefs/drivers)
* OneFS API reference (Platform API used by check-powerscale.py): https://www.dell.com/support/home/en-us/product-support/product/isilon-onefs/docs
* Helm: https://helm.sh/docs/intro/install/  (vendored: v3.19.0, https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz)
