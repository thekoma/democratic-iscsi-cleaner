# Democratic CSI iSCSI Cleaner

A specialized tool designed to reconcile and clean up "zombie" iSCSI sessions left behind by `democratic-csi` on TrueNAS/ZFS Linux hosts.

## The Problem

When using `democratic-csi` with the iSCSI driver in a hyperconverged environment (or with specific network disruptions), `NodeUnstageVolume` may fail to correctly logout from the iSCSI session if the underlying block device is missing or degraded.

This results in:
- `iscsid` spamming system logs with connection errors.
- Accumulation of stale sessions in `/var/lib/iscsi/nodes`.
- Potential resource exhaustion on the node.

Related Issue: [democratic-csi/democratic-csi#536](https://github.com/democratic-csi/democratic-csi/issues/536)

## How it Works

This tool runs as a Kubernetes Job/CronJob directly on the storage node. It uses a rigorous reconciliation process:

1.  **Discovery**:
    *   Lists all registered iSCSI nodes via `iscsiadm`.
    *   Lists all actual ZFS volumes via `zfs list`.
    *   Lists all active Kubernetes PersistentVolumes.
2.  **Reconciliation**:
    *   Identifies iSCSI targets that exist in the local database but have NO corresponding ZFS volume.
3.  **Action**:
    *   Executes `iscsiadm ... -o delete` to remove the stale records.

### Technical Implementation

*   **Architecture**: Runs in a lightweight Python container.
*   **Host Access**: Uses `nsenter` (via `hostPID: true`) to execute commands inside the Host's namespaces (Mount, Net, UTS, IPC). This ensures compatibility with the Host's iSCSI version and database path (e.g., ArchLinux `/var/lib/iscsi` vs Debian `/etc/iscsi`).

## Installation

### Prerequisites
*   ServiceAccount with permissions to list PersistentVolumes.
*   SecurityContext `privileged: true`.
*   `hostPID: true`.

### Deployment

Apply the CronJob manifest found in `deploy/`:

```bash
kubectl apply -f deploy/cronjob.yaml
```

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `ZFS_PARENT_DATASET` | `data/csi/iscsi` | The parent ZFS dataset where PVCs are created. |
| `IQN_PREFIX` | `iqn.2024-03.lan.asgard:knas` | The IQN prefix used by your iSCSI target. |
| `DRY_RUN` | `true` | If `true`, only prints commands. If `false`, executes cleanup. |
| `USE_NSENTER` | `true` | Recommended. Uses host binaries to avoid version/path mismatches. |
