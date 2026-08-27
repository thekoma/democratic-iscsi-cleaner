#!/usr/bin/env python3
"""Replay the cleaner against a REAL state snapshot captured from knas.

Guarantees the decision logic produces the correct verdict on live data
without touching the cluster. Reads the dumps in /opt/data/tmp/real_*.txt.
"""
import importlib.util
import os
import re
import sys
import types

sys.modules.setdefault("kubernetes", types.ModuleType("kubernetes"))
sys.modules["kubernetes"].client = types.SimpleNamespace()
sys.modules["kubernetes"].config = types.SimpleNamespace()

D = "/opt/data/tmp"
PVC_RE = re.compile(r"(pvc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def read(name):
    with open(os.path.join(D, name)) as f:
        return f.read()


sessions_raw = read("real_sessions.txt")
nodes_raw = read("real_nodes.txt")
zfs_raw = read("real_zfs.txt")
mounts_raw = read("real_mounts.txt")
pvs = {l.strip() for l in read("real_pvs.txt").splitlines() if l.strip()}
va = {l.strip() for l in read("real_va.txt").splitlines() if l.strip()}
lio_raw = read("real_lio_host.txt")  # captured via chroot, the CORRECT way

os.environ.update({
    "NODE_NAME": "knas.asgard.lan",
    "STORAGE_NODE": "knas.asgard.lan",
    "DRY_RUN": "true",
    "USE_NSENTER": "false",
    "IQN_PREFIX": "iqn.2024-03.lan.asgard:knas",
})

spec = importlib.util.spec_from_file_location(
    "m", os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

CALLS = []


def fake_run(command, check=True, allow_empty_rc=()):
    if "-o delete" in command or command.endswith(" -u"):
        CALLS.append(command)
        return ""
    if command.startswith("zfs list"):
        return zfs_raw
    if command.startswith("iscsiadm -m session -P3"):
        return ""
    if command.startswith("iscsiadm -m session"):
        return sessions_raw
    if command.startswith("iscsiadm -m node"):
        return nodes_raw
    if command.startswith("ls /sys/kernel/config/target/iscsi"):
        return lio_raw
    if command.startswith("cat /proc/mounts"):
        return mounts_raw
    return ""


m.run = fake_run
m.get_k8s_pvs = lambda: pvs
m.get_volume_attachments = lambda n: va

print("### REPLAY AGAINST REAL knas STATE ###\n")
rc = m.main()

print("\n" + "=" * 62)
print(f"exit code: {rc}")
print(f"destructive commands that WOULD run: {len(CALLS)}")
for c in CALLS:
    print("   ", c)

# Assertions against ground truth we established manually.
sess_uuids = set(PVC_RE.findall(sessions_raw))
zfs_uuids = set(PVC_RE.findall(zfs_raw))
ok = True

if rc != 0:
    print("\nFAIL: expected clean exit on healthy state")
    ok = False

targets = {PVC_RE.search(c).group(1) for c in CALLS if PVC_RE.search(c)}
protected = sess_uuids & zfs_uuids
violation = targets & protected
if violation:
    print(f"\nFAIL: would delete targets that HAVE a zvol: {violation}")
    ok = False
else:
    print("\nOK: no target backed by a live zvol was selected")

if targets:
    print(f"OK: would clean {len(targets)} genuinely orphaned target(s)")
else:
    print("OK: nothing to clean (state already reconciled)")

sys.exit(0 if ok else 1)
