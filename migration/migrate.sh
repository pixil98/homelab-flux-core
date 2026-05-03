#!/usr/bin/env bash
set -euo pipefail

# Production storage migration script.
# Migrates data from nfs-subdir volumes to truenas-iscsi and truenas-nfs PVCs.
#
# Prerequisites:
#   - Branch merged and Flux has reconciled (truenas-csi driver + storage classes exist)
#   - KUBECONFIG set to production cluster
#   - Static NFS media already rsynced on TrueNAS (audible, audiobooks, ebooks)
#
# Usage:
#   export KUBECONFIG=~/homelab-deployments/production/kubeconfig
#   ./migration/migrate.sh <cluster-name>
#
# To find your cluster name: ls /mnt/main/kubernetes/ on TrueNAS

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <cluster-name>"
  echo "  cluster-name: the nfs-subdir directory name (check: ls /mnt/main/kubernetes/)"
  exit 1
fi

CLUSTER_NAME="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISCSI_JOBS="${SCRIPT_DIR}/rsync-to-iscsi.yaml"
NFS_JOBS="${SCRIPT_DIR}/rsync-to-nfs.yaml"

if [[ -z "${KUBECONFIG:-}" ]]; then
  echo "ERROR: KUBECONFIG not set"
  exit 1
fi

echo "=== Production Storage Migration ==="
echo "Cluster context: $(kubectl config current-context)"
echo "NFS source dir: CLUSTER_NAME=${CLUSTER_NAME}"
echo ""
read -p "Continue? (yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
  echo "Aborted."
  exit 1
fi

# ============================================================
# Helpers
# ============================================================

wait_for_no_pods() {
  local ns=$1
  local label=$2
  echo "  Waiting for pods with $label in $ns to terminate..."
  kubectl wait --for=delete pod -l "$label" -n "$ns" --timeout=120s 2>/dev/null || true
}

wait_for_pvc_bound() {
  local ns=$1
  local pvc=$2
  echo "  Waiting for PVC $pvc in $ns to be Bound..."
  for i in $(seq 1 60); do
    phase=$(kubectl get pvc "$pvc" -n "$ns" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    if [[ "$phase" == "Bound" ]]; then
      echo "  PVC $pvc is Bound"
      return 0
    fi
    sleep 2
  done
  echo "  ERROR: PVC $pvc did not become Bound within 120s"
  return 1
}

wait_for_job() {
  local ns=$1
  local job=$2
  local timeout=${3:-600}
  echo "  Waiting for job $job in $ns (timeout ${timeout}s)..."
  if ! kubectl wait --for=condition=complete job/"$job" -n "$ns" --timeout="${timeout}s"; then
    echo "  ERROR: Job $job failed or timed out. Logs:"
    kubectl logs job/"$job" -n "$ns" --tail=20
    return 1
  fi
  echo "  Job $job completed"
}

# ============================================================
# Phase 0: Suspend Flux reconciliation
# ============================================================
echo ""
echo "=== Phase 0: Suspending Flux ==="
flux suspend kustomization --all
echo "All Flux kustomizations suspended."

# ============================================================
# Phase 1: Scale down all affected deployments
# ============================================================
echo ""
echo "=== Phase 1: Scaling down deployments ==="

# mail
echo "Scaling down mail..."
kubectl scale deployment dovecot-server postfix-server clamav-server rspamd-server roundcube-server -n mail --replicas=0

# mapper
echo "Scaling down mapper..."
kubectl scale deployment mapper-server mapper-client -n mapper --replicas=0

# games
echo "Scaling down games..."
kubectl scale deployment mud-server -n games --replicas=0

# paperless
echo "Scaling down paperless..."
kubectl scale deployment paperless-server paperless-gotenberg paperless-tika -n paperless --replicas=0

# media
echo "Scaling down media..."
kubectl scale deployment audiobookshelf-server -n media --replicas=0

# photos
echo "Scaling down photos..."
kubectl scale deployment immich-server immich-machine-learning -n photos --replicas=0

# auth
echo "Scaling down authentik..."
kubectl scale deployment authentik-server authentik-worker authentik-ldap -n auth --replicas=0

# CNPG clusters
echo "Scaling down CNPG clusters..."
kubectl patch cluster roundcube-postgresql -n mail --type merge -p '{"spec":{"instances":0}}'
kubectl patch cluster paperless-postgresql -n paperless --type merge -p '{"spec":{"instances":0}}'
kubectl patch cluster immich-postgresql -n photos --type merge -p '{"spec":{"instances":0}}'
kubectl patch cluster authentik-postgresql -n auth --type merge -p '{"spec":{"instances":0}}'

echo "Waiting for all pods to stop..."
# mail deployments (each has unique app.kubernetes.io/name)
wait_for_no_pods mail "app.kubernetes.io/name=dovecot"
wait_for_no_pods mail "app.kubernetes.io/name=postfix"
wait_for_no_pods mail "app.kubernetes.io/name=clamav"
wait_for_no_pods mail "app.kubernetes.io/name=rspamd"
wait_for_no_pods mail "app.kubernetes.io/name=roundcube"
# mapper
wait_for_no_pods mapper "app.kubernetes.io/instance=mapper"
# games
wait_for_no_pods games "app.kubernetes.io/instance=mud"
# paperless
wait_for_no_pods paperless "app.kubernetes.io/name=paperless"
# media
wait_for_no_pods media "app.kubernetes.io/name=audiobookshelf-server"
# photos
wait_for_no_pods photos "app.kubernetes.io/instance=immich"
# auth
wait_for_no_pods auth "app.kubernetes.io/instance=authentik"
# CNPG
wait_for_no_pods mail "cnpg.io/cluster=roundcube-postgresql"
wait_for_no_pods paperless "cnpg.io/cluster=paperless-postgresql"
wait_for_no_pods photos "cnpg.io/cluster=immich-postgresql"
wait_for_no_pods auth "cnpg.io/cluster=authentik-postgresql"

echo "All deployments scaled to 0."

# ============================================================
# Phase 2: Delete old PVCs
# ============================================================
echo ""
echo "=== Phase 2: Deleting old PVCs ==="

# iSCSI-bound PVCs
kubectl delete pvc dovecot-mailboxes postfix-spool clamav-db rspamd-var -n mail --ignore-not-found
kubectl delete pvc mapper-server-db -n mapper --ignore-not-found
kubectl delete pvc mud-server-characters -n games --ignore-not-found
kubectl delete pvc paperless-server-data -n paperless --ignore-not-found
kubectl delete pvc audiobookshelf-config-pvc audiobookshelf-metadata-pvc -n media --ignore-not-found

# NFS-bound PVCs
kubectl delete pvc immich-library immich-upload immich-encoded-video immich-profile immich-thumbs -n photos --ignore-not-found
kubectl delete pvc paperless-server-media -n paperless --ignore-not-found

# CNPG PVCs
kubectl delete pvc roundcube-postgresql-1 -n mail --ignore-not-found
kubectl delete pvc paperless-postgresql-1 -n paperless --ignore-not-found
kubectl delete pvc immich-postgresql-1 -n photos --ignore-not-found
kubectl delete pvc authentik-postgresql-1 -n auth --ignore-not-found

echo "Old PVCs deleted."

# ============================================================
# Phase 3: Create new PVCs
# ============================================================
echo ""
echo "=== Phase 3: Creating new PVCs ==="

# Apply PVC manifests directly (Flux is suspended).
# iSCSI PVCs
kubectl apply -f "${SCRIPT_DIR}/../30_applications/mail/dovecot_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/mail/postfix_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/mail/clamav_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/mail/rspamd_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/mapper/mapper_server_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/games/mud_server_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/paperless/paperless_server_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/media/audiobookshelf_pvc.yaml"

# NFS PVCs
kubectl apply -f "${SCRIPT_DIR}/../30_applications/photos/immich_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/photos/immich_nfs_pvc.yaml"
kubectl apply -f "${SCRIPT_DIR}/../30_applications/paperless/paperless_nfs_pvc.yaml"

# CNPG: bounce clusters to trigger PVC creation with new storage class.
# The initdb policy will skip initialization if we rsync PGDATA in before final startup.
echo "Scaling CNPG clusters to 1 to trigger PVC creation..."
kubectl patch cluster roundcube-postgresql -n mail --type merge -p '{"spec":{"instances":1}}'
kubectl patch cluster paperless-postgresql -n paperless --type merge -p '{"spec":{"instances":1}}'
kubectl patch cluster immich-postgresql -n photos --type merge -p '{"spec":{"instances":1}}'
kubectl patch cluster authentik-postgresql -n auth --type merge -p '{"spec":{"instances":1}}'

echo "Waiting for new PVCs to be Bound..."
# iSCSI
wait_for_pvc_bound mail dovecot-mailboxes
wait_for_pvc_bound mail postfix-spool
wait_for_pvc_bound mail clamav-db
wait_for_pvc_bound mail rspamd-var
wait_for_pvc_bound mapper mapper-server-db
wait_for_pvc_bound games mud-server-characters
wait_for_pvc_bound paperless paperless-server-data
wait_for_pvc_bound media audiobookshelf-config-pvc
wait_for_pvc_bound media audiobookshelf-metadata-pvc
# NFS
wait_for_pvc_bound photos immich-library
wait_for_pvc_bound photos immich-upload
wait_for_pvc_bound photos immich-encoded-video
wait_for_pvc_bound photos immich-profile
wait_for_pvc_bound photos immich-thumbs
wait_for_pvc_bound paperless paperless-server-media
# CNPG
wait_for_pvc_bound mail roundcube-postgresql-1
wait_for_pvc_bound paperless paperless-postgresql-1
wait_for_pvc_bound photos immich-postgresql-1
wait_for_pvc_bound auth authentik-postgresql-1

# Scale CNPG back down so rsync jobs can mount the iSCSI volumes (RWO)
echo "Scaling CNPG clusters back to 0 for rsync..."
kubectl patch cluster roundcube-postgresql -n mail --type merge -p '{"spec":{"instances":0}}'
kubectl patch cluster paperless-postgresql -n paperless --type merge -p '{"spec":{"instances":0}}'
kubectl patch cluster immich-postgresql -n photos --type merge -p '{"spec":{"instances":0}}'
kubectl patch cluster authentik-postgresql -n auth --type merge -p '{"spec":{"instances":0}}'
wait_for_no_pods mail "cnpg.io/cluster=roundcube-postgresql"
wait_for_no_pods paperless "cnpg.io/cluster=paperless-postgresql"
wait_for_no_pods photos "cnpg.io/cluster=immich-postgresql"
wait_for_no_pods auth "cnpg.io/cluster=authentik-postgresql"

# ============================================================
# Phase 4: Run rsync jobs
# ============================================================
echo ""
echo "=== Phase 4: Running rsync jobs ==="
sed "s/CLUSTER_NAME/${CLUSTER_NAME}/g" "$ISCSI_JOBS" | kubectl apply -f -
sed "s/CLUSTER_NAME/${CLUSTER_NAME}/g" "$NFS_JOBS" | kubectl apply -f -

echo "Waiting for rsync jobs to complete..."
# iSCSI jobs (small, 600s timeout)
wait_for_job mail migrate-dovecot-mailboxes
wait_for_job mail migrate-postfix-spool
wait_for_job mail migrate-clamav-db
wait_for_job mail migrate-rspamd-var
wait_for_job mail migrate-roundcube-postgresql
wait_for_job mapper migrate-mapper-server-db
wait_for_job games migrate-mud-server-characters
wait_for_job paperless migrate-paperless-server-data
wait_for_job paperless migrate-paperless-postgresql
wait_for_job media migrate-audiobookshelf-config
wait_for_job media migrate-audiobookshelf-metadata
wait_for_job photos migrate-immich-postgresql
wait_for_job auth migrate-authentik-postgresql

# NFS jobs (large volumes, longer timeout)
wait_for_job photos migrate-immich-library 3600
wait_for_job photos migrate-immich-upload
wait_for_job photos migrate-immich-encoded-video 3600
wait_for_job photos migrate-immich-profile 3600
wait_for_job photos migrate-immich-thumbs 3600
wait_for_job paperless migrate-paperless-server-media 3600

echo "All rsync jobs completed."

# ============================================================
# Phase 5: Scale everything back up
# ============================================================
echo ""
echo "=== Phase 5: Scaling back up ==="

# CNPG clusters first (apps depend on them).
# initdb policy detects existing PGDATA and skips initialization.
kubectl patch cluster roundcube-postgresql -n mail --type merge -p '{"spec":{"instances":1}}'
kubectl patch cluster paperless-postgresql -n paperless --type merge -p '{"spec":{"instances":1}}'
kubectl patch cluster immich-postgresql -n photos --type merge -p '{"spec":{"instances":1}}'
kubectl patch cluster authentik-postgresql -n auth --type merge -p '{"spec":{"instances":1}}'

echo "Waiting for CNPG clusters to be ready..."
kubectl wait --for=condition=Ready cluster/roundcube-postgresql -n mail --timeout=120s
kubectl wait --for=condition=Ready cluster/paperless-postgresql -n paperless --timeout=120s
kubectl wait --for=condition=Ready cluster/immich-postgresql -n photos --timeout=120s
kubectl wait --for=condition=Ready cluster/authentik-postgresql -n auth --timeout=120s

# Deployments
kubectl scale deployment dovecot-server postfix-server clamav-server rspamd-server roundcube-server -n mail --replicas=1
kubectl scale deployment mapper-server mapper-client -n mapper --replicas=1
kubectl scale deployment mud-server -n games --replicas=1
kubectl scale deployment paperless-server paperless-gotenberg paperless-tika -n paperless --replicas=1
kubectl scale deployment audiobookshelf-server -n media --replicas=1
kubectl scale deployment immich-server immich-machine-learning -n photos --replicas=1
kubectl scale deployment authentik-server authentik-worker authentik-ldap -n auth --replicas=1

# ============================================================
# Phase 6: Resume Flux
# ============================================================
echo ""
echo "=== Phase 6: Resuming Flux ==="
flux resume kustomization --all
echo "Flux resumed."

echo ""
echo "=== Migration complete ==="
echo "Monitor with: kubectl get pods -A | grep -v Running"
echo ""
echo "Next steps:"
echo "  1. Verify all pods are running"
echo "  2. Export PV manifests for disaster recovery:"
echo "       ./migration/export-pvs.sh > ~/homelab-deployments/production/pvs.yaml"
echo "  3. Commit pvs.yaml to homelab-deployments"
echo "  4. Cleanup migration jobs:"
echo "       sed \"s/CLUSTER_NAME/${CLUSTER_NAME}/g\" $ISCSI_JOBS | kubectl delete -f -"
echo "       sed \"s/CLUSTER_NAME/${CLUSTER_NAME}/g\" $NFS_JOBS | kubectl delete -f -"
