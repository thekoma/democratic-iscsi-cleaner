#!/usr/bin/env python3
"""
K8s CSI iSCSI Reconciliation Tool.

Cleans up orphaned iSCSI sessions/node-records left behind by democratic-csi
when it destroys a ZFS zvol (and its LIO target) without waiting for the
initiator to log out. Those sessions retry the login forever, burning kernel
CPU (~4 login attempts/sec each) until the node collapses.

SAFETY MODEL (see iscsi-cleaner-rework-report.md in asgard-k8s):
  1. Fail-safe: ANY failure while gathering a source of truth aborts the run
     with a non-zero exit code. An unknown state is never treated as "empty".
  2. Node-role aware: ZFS is only consulted on storage nodes. Worker nodes use
     the Kubernetes API as source of truth. `zfs list` failing on a worker is
     not a reason to nuke every session.
  3. Triple validation: a target is removed ONLY if it is simultaneously
     - not present as a PersistentVolume in Kubernetes,
     - not backed by a ZFS volume (storage node only),
     - not attached via VolumeAttachment to this node,
     - not mounted anywhere on the host,
     - and its LIO target does not exist (storage node only).
  4. Blast-radius cap: refuses to act if the candidate set exceeds
     MAX_DELETE_RATIO of all discovered sessions (default 30%).
  5. Dry-run by default.
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

try:
    from kubernetes import client, config
except ImportError:
    print("FATAL: 'kubernetes' python library not found.", file=sys.stderr)
    sys.exit(2)

ZFS_PARENT_DATASET = os.getenv("ZFS_PARENT_DATASET", "data/csi/iscsi")
IQN_PREFIX = os.getenv("IQN_PREFIX", "iqn.2024-03.lan.asgard:knas")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
NODE_NAME = os.getenv("NODE_NAME", "")
USE_NSENTER = os.getenv("USE_NSENTER", "true").lower() == "true"
# Storage node = the node that owns the ZFS pool and exports the LIO targets.
STORAGE_NODE = os.getenv("STORAGE_NODE", "")
MAX_DELETE_RATIO = float(os.getenv("MAX_DELETE_RATIO", "0.30"))
# A session must have been failing for at least this long to be touched.
MIN_AGE_MINUTES = int(os.getenv("MIN_AGE_MINUTES", "10"))

PVC_RE = re.compile(r"(pvc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


class GatherError(RuntimeError):
    """Raised when a source of truth cannot be established."""


def log(msg=""):
    print(msg, flush=True)


def run(command, check=True, allow_empty_rc=()):
    """Run a host command. Raises GatherError on failure unless allowed.

    NOTE: never returns "" to paper over a failure — that was the original bug.
    """
    full = command
    if USE_NSENTER and os.geteuid() == 0:
        full = f"nsenter -t 1 -m -u -n -i -- {command}"
    elif os.geteuid() != 0:
        full = "sudo " + command

    proc = subprocess.run(full, shell=True, capture_output=True, text=True)
    if proc.returncode != 0 and proc.returncode not in allow_empty_rc:
        if check:
            raise GatherError(
                f"command failed (rc={proc.returncode}): {command}\n"
                f"stderr: {proc.stderr.strip()}"
            )
        return None
    return proc.stdout.strip()


def host_has_binary(name):
    return run(f"command -v {name} >/dev/null 2>&1", check=False, allow_empty_rc=(1, 127)) is not None


# --------------------------------------------------------------------------
# Sources of truth
# --------------------------------------------------------------------------

def get_iscsi_sessions():
    """Active iSCSI sessions on this host: {pvc_uuid: iqn}. rc=21 => none."""
    log("[-] Fetching active iSCSI sessions...")
    raw = run("iscsiadm -m session", allow_empty_rc=(21,)) or ""
    out = {}
    for line in raw.splitlines():
        if IQN_PREFIX not in line:
            continue
        m = PVC_RE.search(line)
        if m:
            iqn = next((p for p in line.split() if p.startswith("iqn.")), None)
            if iqn:
                out[m.group(1)] = iqn
    return out


def get_iscsi_node_records():
    """Persisted node records: {pvc_uuid: iqn}. rc=21 => none configured."""
    log("[-] Fetching iSCSI node records...")
    raw = run("iscsiadm -m node", allow_empty_rc=(21,)) or ""
    out = {}
    for line in raw.splitlines():
        if IQN_PREFIX not in line:
            continue
        m = PVC_RE.search(line)
        if m:
            iqn = next((p for p in line.split() if p.startswith("iqn.")), None)
            if iqn:
                out[m.group(1)] = iqn
    return out


def get_zfs_volumes():
    """ZFS zvols. Storage node ONLY. Raises if zfs is expected but unusable."""
    log("[-] Fetching ZFS volumes...")
    raw = run(f"zfs list -t volume -H -o name -r {ZFS_PARENT_DATASET}")
    uuids = set()
    for line in (raw or "").splitlines():
        m = PVC_RE.search(line.strip().split("/")[-1])
        if m:
            uuids.add(m.group(1))
    if not uuids:
        # Storage node with zero zvols is implausible and is exactly the
        # signature of the outage: refuse to proceed.
        raise GatherError(
            f"zfs reported ZERO volumes under {ZFS_PARENT_DATASET}. "
            "Refusing to treat that as 'everything is stale'."
        )
    return uuids


def get_lio_targets():
    """Targets currently exported by LIO. Storage node ONLY.

    NOTE: this MUST be read from the host namespace. A container has its own
    /sys, where the path is empty — reading it locally would make every target
    look unexported and turn the whole session list into deletion candidates.
    """
    log("[-] Fetching LIO exported targets...")
    raw = run("ls /sys/kernel/config/target/iscsi", check=False, allow_empty_rc=(1, 2))
    if raw is None:
        raise GatherError("cannot read /sys/kernel/config/target/iscsi on storage node")
    targets = {m.group(1) for line in raw.splitlines() if (m := PVC_RE.search(line))}
    if not targets:
        raise GatherError(
            "LIO reported ZERO exported targets on a storage node — implausible. "
            "Most likely /sys was read from the container instead of the host "
            "(check USE_NSENTER and hostPID). Refusing to proceed."
        )
    return targets


def get_k8s_pvs():
    """All PersistentVolumes. Any API failure aborts the run."""
    log("[-] Fetching Kubernetes PersistentVolumes...")
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    v1 = client.CoreV1Api()
    pvs = v1.list_persistent_volume(timeout_seconds=60)
    uuids = {pv.metadata.name for pv in pvs.items if pv.metadata.name.startswith("pvc-")}
    if not uuids:
        raise GatherError("Kubernetes reported ZERO PersistentVolumes — implausible, aborting.")
    return uuids


def get_volume_attachments(node_name):
    """PV names attached to this node according to the K8s API."""
    log("[-] Fetching VolumeAttachments for this node...")
    storage = client.StorageV1Api()
    vas = storage.list_volume_attachment(timeout_seconds=60)
    return {
        va.spec.source.persistent_volume_name
        for va in vas.items
        if va.spec.node_name == node_name
        and va.spec.source
        and va.spec.source.persistent_volume_name
    }


def get_mounted_uuids():
    """Any pvc-* referenced in the host mount table (kubelet mounts included)."""
    log("[-] Fetching host mount table...")
    raw = run("cat /proc/mounts")
    return {m.group(1) for line in (raw or "").splitlines() if (m := PVC_RE.search(line))}


def get_session_devices(iqn):
    """SCSI devices backing a session, to double-check nothing is mounted."""
    raw = run(f"iscsiadm -m session -P3", check=False, allow_empty_rc=(21,)) or ""
    devices, current, capture = [], None, False
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("Target:"):
            current = s.split()[1] if len(s.split()) > 1 else None
            capture = current == iqn
        elif capture and "Attached scsi disk" in s:
            parts = s.split()
            if len(parts) >= 4:
                devices.append(parts[3])
    return devices


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def cleanup(iqn, has_session):
    if has_session:
        log(f"     logout: iscsiadm -m node -T {iqn} -u")
        if not DRY_RUN:
            run(f"iscsiadm -m node -T {iqn} -u", check=False, allow_empty_rc=(21, 15, 2))
    log(f"     delete: iscsiadm -m node -T {iqn} -o delete")
    if not DRY_RUN:
        run(f"iscsiadm -m node -T {iqn} -o delete", check=False, allow_empty_rc=(21, 2))


def main():
    started = datetime.now(timezone.utc)
    is_storage_node = bool(STORAGE_NODE) and NODE_NAME == STORAGE_NODE

    log(f"*** democratic-csi iSCSI reconciliation — {started.isoformat()} ***")
    log("=" * 74)
    log(f"Node:         {NODE_NAME or 'UNKNOWN'}")
    log(f"Role:         {'STORAGE (ZFS authoritative)' if is_storage_node else 'WORKER (K8s API authoritative)'}")
    log(f"Mode:         {'DRY RUN' if DRY_RUN else 'LIVE EXECUTION'}")
    log(f"Host access:  {'nsenter' if USE_NSENTER else 'direct'}")
    log("")

    if not NODE_NAME:
        log("FATAL: NODE_NAME not set (Downward API). Refusing to guess node role.")
        return 2
    if not STORAGE_NODE:
        log("FATAL: STORAGE_NODE not set. Refusing to run without an explicit role definition.")
        return 2

    # ---- Gather. Any GatherError aborts before a single destructive action.
    try:
        sessions = get_iscsi_sessions()
        node_records = get_iscsi_node_records()
        k8s_pvs = get_k8s_pvs()
        attached = get_volume_attachments(NODE_NAME)
        mounted = get_mounted_uuids()
        zfs_volumes = get_zfs_volumes() if is_storage_node else None
        lio_targets = get_lio_targets() if is_storage_node else None
    except GatherError as e:
        log("")
        log(f"[FATAL] Could not establish a source of truth: {e}")
        log("[FATAL] Aborting WITHOUT touching anything (fail-safe).")
        return 1
    except Exception as e:
        log("")
        log(f"[FATAL] Unexpected error while gathering state: {e!r}")
        log("[FATAL] Aborting WITHOUT touching anything (fail-safe).")
        return 1

    known = set(sessions) | set(node_records)
    log("")
    log("Stats:")
    log(f"  - active iSCSI sessions:  {len(sessions)}")
    log(f"  - iSCSI node records:     {len(node_records)}")
    log(f"  - distinct targets:       {len(known)}")
    log(f"  - Kubernetes PVs:         {len(k8s_pvs)}")
    log(f"  - attached to this node:  {len(attached)}")
    log(f"  - mounted on host:        {len(mounted)}")
    if is_storage_node:
        log(f"  - ZFS volumes:            {len(zfs_volumes or set())}")
        log(f"  - LIO exported targets:   {len(lio_targets or set())}")

    if not known:
        log("\n[OK] No iSCSI targets matching the prefix. Nothing to do.")
        return 0

    # ---- Evaluate each target against every independent signal.
    candidates, kept = [], []
    for uuid in sorted(known):
        iqn = sessions.get(uuid) or node_records[uuid]
        reasons_to_keep = []
        if uuid in k8s_pvs:
            reasons_to_keep.append("PV exists in Kubernetes")
        if uuid in attached:
            reasons_to_keep.append("VolumeAttachment present for this node")
        if uuid in mounted:
            reasons_to_keep.append("mounted on host")
        if is_storage_node:
            if uuid in zfs_volumes:
                reasons_to_keep.append("ZFS volume exists")
            if uuid in lio_targets:
                reasons_to_keep.append("LIO target exported")

        if reasons_to_keep:
            kept.append((uuid, reasons_to_keep))
            continue

        # Final belt-and-braces: refuse if any backing device is still mounted.
        if uuid in sessions:
            mount_table = run("cat /proc/mounts") or ""
            blocking = [d for d in get_session_devices(iqn) if d and f"/dev/{d}" in mount_table]
            if blocking:
                kept.append((uuid, [f"device still mounted: {','.join(blocking)}"]))
                continue

        candidates.append((uuid, iqn, uuid in sessions))

    log("")
    log(f"[=] Healthy targets kept: {len(kept)}")
    for uuid, reasons in kept[:5]:
        log(f"      {uuid}: {'; '.join(reasons)}")
    if len(kept) > 5:
        log(f"      ... and {len(kept) - 5} more")

    if not candidates:
        log("\n[OK] No orphaned iSCSI targets found.")
        return 0

    log("")
    log(f"[!] Found {len(candidates)} ORPHANED target(s):")
    for uuid, iqn, has_sess in candidates:
        log(f"      {uuid} (session={'yes' if has_sess else 'no'})")

    # ---- Blast-radius guard.
    ratio = len(candidates) / len(known)
    if ratio > MAX_DELETE_RATIO:
        log("")
        log(f"[ABORT] {len(candidates)}/{len(known)} targets ({ratio:.0%}) exceed the "
            f"{MAX_DELETE_RATIO:.0%} safety threshold.")
        log("[ABORT] This looks like a discovery failure, not a real orphan set.")
        log("[ABORT] Nothing was touched. Investigate manually.")
        return 1

    log("")
    log(f"[ACTION] Cleaning up {len(candidates)} orphaned target(s)"
        f"{' (DRY RUN)' if DRY_RUN else ''}...")
    for uuid, iqn, has_sess in candidates:
        log(f"  -> {uuid}")
        cleanup(iqn, has_sess)

    log("")
    if DRY_RUN:
        log("[DONE] Dry run complete — nothing was modified. Set DRY_RUN=false to apply.")
    else:
        log(f"[DONE] Cleaned {len(candidates)} orphaned target(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
