#!/usr/bin/env python3
"""Offline safety tests for main.py — no cluster, no host, pure simulation.

These specifically encode the outage described in iscsi-cleaner-rework-report.md:
a worker node where `zfs list` fails must NEVER delete active sessions.
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))

# Stub the kubernetes module before importing main.
k8s = types.ModuleType("kubernetes")
k8s.client = types.SimpleNamespace()
k8s.config = types.SimpleNamespace()
sys.modules.setdefault("kubernetes", k8s)

results = []


def load_main(env):
    for k, v in env.items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location("cleanermod", os.path.join(HERE, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


BASE_ENV = {
    "NODE_NAME": "knas.asgard.lan",
    "STORAGE_NODE": "knas.asgard.lan",
    "DRY_RUN": "true",
    "USE_NSENTER": "false",
    "IQN_PREFIX": "iqn.2024-03.lan.asgard:knas",
}

IQN = "iqn.2024-03.lan.asgard:knas"
ACTIVE = [f"pvc-0000000{i}-1111-2222-3333-44444444444{i}" for i in range(1, 6)]
ORPHAN = "pvc-99999999-1111-2222-3333-999999999999"

DESTRUCTIVE = []


def install_fakes(mod, *, sessions, node_records, zfs, lio, pvs, attached, mounts,
                  zfs_fails=False, k8s_fails=False, age_minutes=600):
    def fake_run(command, check=True, allow_empty_rc=()):
        DESTRUCTIVE.extend([command] if ("-o delete" in command or " -u" in command) else [])
        if command.startswith("zfs list"):
            if zfs_fails:
                if check:
                    raise mod.GatherError("nsenter: failed to execute zfs: No such file or directory")
                return None
            return "\n".join(f"data/csi/iscsi/{u}" for u in zfs)
        if command.startswith("journalctl") or command.startswith("stat -c"):
            if age_minutes is None:
                return ""
            import time
            return str(time.time() - age_minutes * 60)
        if command.startswith("iscsiadm -m session -P3"):
            return ""
        if command.startswith("iscsiadm -m session"):
            return "\n".join(f"tcp: [{i}] 192.168.85.5:3260,1 {IQN}:{u} (non-flash)"
                             for i, u in enumerate(sessions, 1))
        if command.startswith("iscsiadm -m node"):
            if "-o delete" in command or command.endswith(" -u"):
                return ""
            return "\n".join(f"192.168.85.5:3260,1 {IQN}:{u}" for u in node_records)
        if command.startswith("ls /sys/kernel/config/target/iscsi"):
            return "\n".join(f"{IQN}:{u}" for u in lio)
        if command.startswith("cat /proc/mounts"):
            return "\n".join(mounts)
        return ""

    mod.run = fake_run
    mod.get_k8s_pvs = (lambda: (_ for _ in ()).throw(mod.GatherError("api down"))) if k8s_fails \
        else (lambda: set(pvs))
    mod.get_volume_attachments = lambda node: set(attached)


# ---------------------------------------------------------------- TEST 1
# THE OUTAGE: worker node, zfs missing. Must abort, delete nothing.
print("\n=== TEST 1: worker node where `zfs list` fails (the March outage) ===")
DESTRUCTIVE.clear()
env = dict(BASE_ENV, NODE_NAME="nors.asgard.lan", STORAGE_NODE="knas.asgard.lan", DRY_RUN="false")
m = load_main(env)
install_fakes(m, sessions=ACTIVE, node_records=ACTIVE, zfs=[], lio=[],
              pvs=ACTIVE, attached=ACTIVE, mounts=[], zfs_fails=True)
rc = m.main()
check("worker: exits 0 (healthy, K8s is authoritative)", rc == 0, f"rc={rc}")
check("worker: NO destructive command issued", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 2
# Storage node, zfs genuinely broken -> must abort hard.
print("\n=== TEST 2: storage node where `zfs list` fails ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false"))
install_fakes(m, sessions=ACTIVE, node_records=ACTIVE, zfs=[], lio=[],
              pvs=ACTIVE, attached=[], mounts=[], zfs_fails=True)
rc = m.main()
check("storage+zfs broken: aborts non-zero", rc == 1, f"rc={rc}")
check("storage+zfs broken: NO destructive command", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 3
# zfs returns EMPTY (not an error) -> still implausible, must abort.
print("\n=== TEST 3: storage node, zfs returns zero volumes ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false"))
install_fakes(m, sessions=ACTIVE, node_records=ACTIVE, zfs=[], lio=[],
              pvs=ACTIVE, attached=[], mounts=[])
rc = m.main()
check("zfs empty: aborts non-zero", rc == 1, f"rc={rc}")
check("zfs empty: NO destructive command", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 4
# K8s API down -> abort.
print("\n=== TEST 4: Kubernetes API unreachable ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false"))
install_fakes(m, sessions=ACTIVE, node_records=ACTIVE, zfs=ACTIVE, lio=ACTIVE,
              pvs=ACTIVE, attached=[], mounts=[], k8s_fails=True)
rc = m.main()
check("k8s down: aborts non-zero", rc == 1, f"rc={rc}")
check("k8s down: NO destructive command", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 5
# THE REAL CASE: 5 healthy + 1 genuine orphan. Must remove exactly one.
print("\n=== TEST 5: 5 healthy + 1 real orphan (the knas case) ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false"))
install_fakes(m, sessions=ACTIVE + [ORPHAN], node_records=ACTIVE + [ORPHAN],
              zfs=ACTIVE, lio=ACTIVE, pvs=ACTIVE, attached=ACTIVE,
              mounts=[f"/dev/sd{c} /var/lib/kubelet/pods/{u} ext4 rw 0 0"
                      for c, u in zip("abcde", ACTIVE)])
rc = m.main()
check("real orphan: exits 0", rc == 0, f"rc={rc}")
check("real orphan: exactly 1 target touched",
      len({c.split()[4] for c in DESTRUCTIVE}) == 1,
      f"targets={ {c.split()[4] for c in DESTRUCTIVE} }")
check("real orphan: it is the ORPHAN, not a healthy one",
      all(ORPHAN in c for c in DESTRUCTIVE) and DESTRUCTIVE,
      f"{len(DESTRUCTIVE)} cmds")
check("real orphan: no ACTIVE uuid ever appears in a destructive cmd",
      not any(a in c for c in DESTRUCTIVE for a in ACTIVE))

# ---------------------------------------------------------------- TEST 6
# Blast radius: many orphans at once -> refuse.
print("\n=== TEST 6: blast-radius guard (60% of targets look stale) ===")
DESTRUCTIVE.clear()
many = [f"pvc-8888888{i}-1111-2222-3333-88888888888{i}" for i in range(1, 7)]
m = load_main(dict(BASE_ENV, DRY_RUN="false"))
install_fakes(m, sessions=ACTIVE[:4] + many, node_records=ACTIVE[:4] + many,
              zfs=ACTIVE[:4], lio=ACTIVE[:4], pvs=ACTIVE[:4], attached=[], mounts=[])
rc = m.main()
check("blast radius: aborts non-zero", rc == 1, f"rc={rc}")
check("blast radius: NO destructive command", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 7
# Mounted device must be protected even if every other signal says orphan.
print("\n=== TEST 7: orphan-looking target whose device is still mounted ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false"))
install_fakes(m, sessions=ACTIVE, node_records=ACTIVE, zfs=ACTIVE[:4], lio=ACTIVE[:4],
              pvs=ACTIVE[:4], attached=[],
              mounts=[f"/dev/sdz /var/lib/kubelet/pods/{ACTIVE[4]}/mount ext4 rw 0 0"])
rc = m.main()
check("mounted: protected target not deleted",
      not any(ACTIVE[4] in c for c in DESTRUCTIVE), f"cmds={DESTRUCTIVE}")

# ---------------------------------------------------------------- TEST 8
# DRY_RUN default must be true when unset, and must not act.
print("\n=== TEST 8: DRY_RUN defaults to true and performs no action ===")
DESTRUCTIVE.clear()
env = dict(BASE_ENV)
env.pop("DRY_RUN")
os.environ.pop("DRY_RUN", None)
m = load_main(env)
check("DRY_RUN defaults to True", m.DRY_RUN is True)
install_fakes(m, sessions=ACTIVE + [ORPHAN], node_records=ACTIVE + [ORPHAN],
              zfs=ACTIVE, lio=ACTIVE, pvs=ACTIVE, attached=ACTIVE, mounts=[])
rc = m.main()
check("dry run: exits 0", rc == 0, f"rc={rc}")
check("dry run: NO destructive command executed", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 9
# Missing STORAGE_NODE / NODE_NAME -> refuse to guess.
print("\n=== TEST 9: missing role configuration ===")
DESTRUCTIVE.clear()
env = dict(BASE_ENV, DRY_RUN="false")
env["STORAGE_NODE"] = ""
os.environ["STORAGE_NODE"] = ""
m = load_main(env)
install_fakes(m, sessions=ACTIVE, node_records=ACTIVE, zfs=ACTIVE, lio=ACTIVE,
              pvs=ACTIVE, attached=[], mounts=[])
rc = m.main()
check("no STORAGE_NODE: refuses (rc=2)", rc == 2, f"rc={rc}")
check("no STORAGE_NODE: NO destructive command", not DESTRUCTIVE)

# ---------------------------------------------------------------- TEST 10
# REGRESSION: /sys read from the container (empty LIO) must abort, not wipe.
print("\n=== TEST 10: LIO listing empty (container /sys instead of host) ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false", STORAGE_NODE="knas.asgard.lan",
                   NODE_NAME="knas.asgard.lan"))
install_fakes(m, sessions=ACTIVE, node_records=ACTIVE, zfs=ACTIVE, lio=[],
              pvs=ACTIVE, attached=ACTIVE, mounts=[])
rc = m.main()
check("empty LIO: aborts non-zero", rc == 1, f"rc={rc}")
check("empty LIO: NO destructive command", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 11
# Age gate: a freshly-unreferenced target is mid-convergence, not an orphan.
print("\n=== TEST 11: orphan-looking target that is only 2 minutes old ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false", MIN_AGE_MINUTES="10"))
install_fakes(m, sessions=ACTIVE + [ORPHAN], node_records=ACTIVE + [ORPHAN],
              zfs=ACTIVE, lio=ACTIVE, pvs=ACTIVE, attached=ACTIVE, mounts=[],
              age_minutes=2)
rc = m.main()
check("too recent: exits 0", rc == 0, f"rc={rc}")
check("too recent: NOT deleted", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------- TEST 12
# Age unknown must be treated as too young, never as old enough.
print("\n=== TEST 12: target whose age cannot be determined ===")
DESTRUCTIVE.clear()
m = load_main(dict(BASE_ENV, DRY_RUN="false", MIN_AGE_MINUTES="10"))
install_fakes(m, sessions=ACTIVE + [ORPHAN], node_records=ACTIVE + [ORPHAN],
              zfs=ACTIVE, lio=ACTIVE, pvs=ACTIVE, attached=ACTIVE, mounts=[],
              age_minutes=None)
rc = m.main()
check("unknown age: exits 0", rc == 0, f"rc={rc}")
check("unknown age: NOT deleted", not DESTRUCTIVE, f"{len(DESTRUCTIVE)} cmds")

# ---------------------------------------------------------------------------
print("\n" + "=" * 62)
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("FAILED:")
    for n, _, d in failed:
        print(f"  - {n} {d}")
sys.exit(1 if failed else 0)
