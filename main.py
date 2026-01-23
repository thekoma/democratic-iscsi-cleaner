#!/usr/bin/env python3
import subprocess
import os
import sys
import re
from datetime import datetime
try:
    from kubernetes import client, config
except ImportError:
    print("Error: 'kubernetes' python library not found. Install it with 'pip install kubernetes'")
    sys.exit(1)

# Configuration via ENV VARS (Best practice for K8s Jobs)
# ZFS configuration
ZFS_PARENT_DATASET = os.getenv("ZFS_PARENT_DATASET", "data/csi/iscsi")
# iSCSI configuration
IQN_PREFIX = os.getenv("IQN_PREFIX", "iqn.2024-03.lan.asgard:knas")
# Execution Mode
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
# Node Name (Downward API)
NODE_NAME = os.getenv("NODE_NAME")
# Use nsenter to run on host
USE_NSENTER = os.getenv("USE_NSENTER", "true").lower() == "true"

def run_command(command):
    """Run a shell command and return stdout decoded."""
    # Prefix with nsenter if configured and running as root
    if USE_NSENTER and os.geteuid() == 0:
        # Check if we are already in a hostPID env (pid 1 is systemd/init of host)
        # Usually checking /proc/1/comm being systemd is a hint, but we just assume true via config
        command = f"nsenter -t 1 -m -u -n -i -- {command}"
    elif os.geteuid() != 0:
        command = "sudo " + command
            
    try:
        result = subprocess.check_output(command, shell=True, stderr=subprocess.PIPE)
        return result.decode('utf-8').strip()
    except subprocess.CalledProcessError as e:
        # Ignore grep errors (exit code 1 means not found)
        if e.returncode != 1: 
            print(f"Error running command: {command}", file=sys.stderr)
            print(e.stderr.decode('utf-8'), file=sys.stderr)
        return ""

def get_iscsi_nodes():
    """Returns a set of UUIDs found in iscsiadm configurations."""
    print("[-] Fetching iSCSI nodes...")
    raw = run_command("iscsiadm -m node")
    uuids = set()
    full_targets = []
    
    for line in raw.splitlines():
        if IQN_PREFIX in line:
            parts = line.split()
            if len(parts) >= 2:
                iqn = parts[1]
                match = re.search(r'(pvc-[a-f0-9-]+)', iqn)
                if match:
                    uuids.add(match.group(1))
                    full_targets.append(iqn)
    return uuids, full_targets

def get_zfs_volumes():
    """Returns a set of UUIDs found in ZFS volumes."""
    print("[-] Fetching ZFS volumes...")
    # Assuming we are running on the hyperconverged node where ZFS is local
    raw = run_command(f"zfs list -t volume -H -o name -r {ZFS_PARENT_DATASET}")
    uuids = set()
    
    for line in raw.splitlines():
        clean_name = line.strip()
        if clean_name.startswith(ZFS_PARENT_DATASET):
            dataset_name = clean_name.split('/')[-1]
            if dataset_name.startswith('pvc-'):
                uuids.add(dataset_name)
    return uuids

def get_k8s_pvs():
    """Returns a set of UUIDs found in Kubernetes PersistentVolumes."""
    print("[-] Fetching Kubernetes PVs...")
    try:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
            
        v1 = client.CoreV1Api()
        # We only care about PVs that are actually claimed and related to our storage class ideally
        # But keeping it simple: list all PVs starting with pvc-
        pvs = v1.list_persistent_volume()
        uuids = set()
        
        for pv in pvs.items:
            name = pv.metadata.name
            if name.startswith('pvc-'):
                uuids.add(name)
        return uuids
    except Exception as e:
        print(f"[!] Failed to talk to K8s API: {e}")
        print("    Ensure ServiceAccount has permissions to list PVs.")
        return set() # Fail safe

def cleanup_iscsi(targets_to_remove, full_targets_list):
    """Executes the cleanup."""
    if not targets_to_remove:
        return

    print(f"\n[ACTION] Cleaning up {len(targets_to_remove)} stale targets...")
    
    for uuid in targets_to_remove:
        # Find full IQN
        target = next((t for t in full_targets_list if uuid in t), None)
        if target:
            logout_cmd = f"iscsiadm -m node -T {target} -u"
            delete_cmd = f"iscsiadm -m node -T {target} -o delete"
            
            # Note: run_command automatically adds nsenter prefix if enabled
            print(f"  -> {logout_cmd}")
            print(f"  -> {delete_cmd}")
            
            if not DRY_RUN:
                # Try to logout first. If it fails (e.g. already logged out), we proceed to delete anyway
                run_command(logout_cmd)
                run_command(delete_cmd)
            else:
                print("     (Dry Run: skipped)")

def main():
    print(f"*** K8s CSI Reconciliation Tool (Container Mode) - {datetime.now()} ***")
    print("=========================================================================")
    print(f"Node: {NODE_NAME if NODE_NAME else 'Unknown (Not set via Downward API)'}")
    print(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE EXECUTION'}")
    print(f"Host Access: {'ENABLED (nsenter)' if USE_NSENTER else 'DISABLED'}")
    
    # 1. Gather Facts
    iscsi_uuids, iscsi_targets = get_iscsi_nodes()
    zfs_uuids = get_zfs_volumes()
    k8s_uuids = get_k8s_pvs()

    print(f"\nStats:")
    print(f"  - iSCSI Configurations found: {len(iscsi_uuids)}")
    print(f"  - ZFS Volumes found:          {len(zfs_uuids)}")
    print(f"  - Kubernetes PVs found:       {len(k8s_uuids)}")

    # 2. Reconcile
    stale_iscsi = iscsi_uuids - zfs_uuids

    # 3. Action
    if stale_iscsi:
        print(f"\n[!] FOUND {len(stale_iscsi)} STALE iSCSI CONFIGURATIONS")
        cleanup_iscsi(stale_iscsi, iscsi_targets)
    else:
        print("\n[OK] No stale iSCSI configurations found.")

    if not DRY_RUN and stale_iscsi:
        # Force a rescan/restart of iscsid might be too aggressive, 
        # usually deleting the node record is enough to stop the spam.
        pass

if __name__ == "__main__":
    main()
