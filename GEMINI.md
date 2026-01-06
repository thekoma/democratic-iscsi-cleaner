# Project Context: Democratic CSI iSCSI Cleaner

## Overview
This project was created to solve a specific issue in a Kubernetes hyperconverged environment (ArchLinux host) using `democratic-csi` with ZFS/iSCSI.

**Symptom:** System logs flooded with `iSCSI Login negotiation failed` for targets that no longer exist.
**Root Cause:** `democratic-csi` fails to perform `iscsiadm logout` if the local block device is missing/degraded during `NodeUnstageVolume`.
**Status:** Bug reported upstream (Issue #536).

## Architecture
- **Language:** Python 3.11
- **Deployment:** Kubernetes CronJob
- **Strategy:** Sidecar/Node-Agent using `nsenter`.
    - Instead of bundling `iscsiadm` and `zfs` binaries in the Docker image, we access the host's binaries and namespaces.
    - This solves path incompatibilities (Arch `/var/lib/iscsi` vs Debian `/etc/iscsi`) and version mismatches.

## Key Components
1.  **`main.py`**: The logic core.
    - Uses `kubernetes` python lib to list PVs.
    - Uses `subprocess` -> `nsenter` to run `iscsiadm` and `zfs` on the host.
    - Reconciles state: `Stale = iSCSI_Config - ZFS_Volume`.
2.  **`deploy/cronjob.yaml`**: The operational manifest.
    - **Critical Settings:**
        - `nodeName: knas.asgard.lan` (Must run on storage node).
        - `hostPID: true` (Required for nsenter).
        - `privileged: true` (Required for root access).

## Maintenance
- **Releases:** Automated via GitHub Actions. Tags follow format `YYYYMMNN` (e.g., `20260100`).
- **Updates:** If `democratic-csi` fixes the bug, this tool might become obsolete, but serves as a safety net.

## Commands Reference
- Run local check (if on host): `./main.py` (requires env vars).
- Build docker: `docker build .`
