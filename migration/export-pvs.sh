#!/usr/bin/env bash
set -euo pipefail

# Exports all truenas CSI PV manifests for backup/restore after cluster rebuild.
# The exported YAML can be applied to a fresh cluster to rebind existing zvols/datasets.
#
# Usage:
#   export KUBECONFIG=~/homelab-deployments/production/kubeconfig
#   ./migration/export-pvs.sh > ~/homelab-deployments/production/pvs.yaml
#
# To restore after cluster rebuild:
#   kubectl apply -f ~/homelab-deployments/production/pvs.yaml

if [[ -z "${KUBECONFIG:-}" ]]; then
  echo "ERROR: KUBECONFIG not set" >&2
  exit 1
fi

echo "# Exported PV manifests - $(date -Iseconds)" >&2
echo "# Cluster: $(kubectl config current-context)" >&2

# Export both iSCSI and NFS PVs provisioned by truenas-csi
pvs=$(kubectl get pv -o jsonpath='{range .items[?(@.spec.csi.driver=="csi.truenas.io")]}{.metadata.name}{"\n"}{end}' | grep -v '^$' || true)

if [[ -z "$pvs" ]]; then
  echo "# No truenas CSI PVs found" >&2
  exit 0
fi

count=0
while IFS= read -r pv; do
  if [[ $count -gt 0 ]]; then
    echo "---"
  fi
  # Strip runtime metadata and claimRef.uid/resourceVersion.
  # Keeping claimRef.name + namespace allows pre-binding to new PVCs without UID match.
  # IfNotPresent ensures Flux creates PVs on a fresh cluster but never updates live ones
  # (which would strip claimRef.uid and unbind the volume).
  kubectl get pv "$pv" -o yaml | \
    yq 'del(.metadata.creationTimestamp, .metadata.resourceVersion, .metadata.uid, .metadata.managedFields, .metadata.finalizers, .metadata.annotations["pv.kubernetes.io/provisioned-by"], .status, .spec.claimRef.uid, .spec.claimRef.resourceVersion) | .metadata.annotations["kustomize.toolkit.fluxcd.io/ssa"] = "IfNotPresent"'
  count=$((count + 1))
done <<< "$pvs"

echo "# Exported $count PV(s)" >&2
