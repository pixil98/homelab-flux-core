#!/usr/bin/env python3
"""
Reclaim leaked democratic-csi iSCSI volumes on TrueNAS. Runs ON the NAS, as root.

Measured costs on this system:
  iscsi.* mutation  ~27s  (each triggers a service.control RELOAD iscsitarget)
  zfs destroy       0.15s (free)
  origin snapshot   free  (defer_destroy=on, reaps with its clone)

So the entire cost is the reload, and the only lever is doing fewer mutations.
Work is ordered join -> target -> extent so target count -- what drives reload
duration -- falls as fast as possible and later operations get cheaper.

  ./reclaim-iscsi.py                    # report only (default, read-only)
  ./reclaim-iscsi.py --purge --limit 10 # trial batch, measure, then commit
  ./reclaim-iscsi.py --purge            # the rest; resumes if interrupted

State is written to STATE_FILE after every volume, so an interrupted run picks
up where it left off rather than restarting a multi-hour job.

The ugre cluster shares this NAS. Everything is scoped to PARENT by extent path
and by target alias, so ugre's objects are never enumerated.
"""

import argparse
import json
import os
import subprocess
import sys
import time

PARENT = "ssd/kubernetes/production"
PREFIX = f"zvol/{PARENT}/"
ALIAS_PREFIX = f"CSI volume {PARENT}/"
STATE_FILE = "/root/.reclaim-iscsi-state.json"

# Live PVs from the cluster on 2026-08-12: 18 truenas-iscsi + 7 volsync caches.
# This list goes stale -- the volsync cache PVCs in particular are recreated
# from time to time. Regenerate before any run that is not same-day:
#
#   kubectl get pv -o jsonpath='{range .items[?(@.spec.csi.driver=="org.democratic-csi.iscsi")]}{.metadata.name}{"\n"}{end}'
#
# The script refuses to purge if any KEEP entry has no extent, which catches a
# list that has drifted -- but it cannot detect a live volume missing from KEEP,
# and that one would be deleted. Regenerate rather than trusting the date above.
KEEP = {
    "pvc-0c1749ee-9b7a-4686-bb5c-b4f23991f880",
    "pvc-0e142878-f599-4e0f-bcbe-c405b6a0ef4c",
    "pvc-0e837ae0-0c25-402c-a8a1-bdeaa94fdff3",
    "pvc-1278aa11-61f7-42ef-8139-9dde8c16942f",
    "pvc-4b2e7075-eccf-42b2-858f-8617e1686069",
    "pvc-55f1c69f-bf04-4599-bf80-69ffb7ea6a2f",
    "pvc-640133a7-9796-4139-b547-290169362c39",
    "pvc-91e9df23-f130-4c78-83e2-65bd3f1c3d4b",
    "pvc-93b47557-0ce9-41a3-b8f5-2ca07f42bf90",
    "pvc-9f78a5f7-0dea-4054-91c3-e7879ba9ef71",
    "pvc-a3a24a33-69b1-4a9b-90d1-2070a977ecf1",
    "pvc-a7298c2b-3223-4c91-8156-4a156343d30c",
    "pvc-adcfb338-0cf9-4c84-8e3f-1ff542e002bd",
    "pvc-b57a1ae7-32de-4fd8-9b0f-ec2af7f5991c",
    "pvc-b6f700c1-dccc-4037-a95b-f47c71d84b0d",
    "pvc-c627b83d-5319-4c9b-b3fe-2ee0c94a0fd3",
    "pvc-dbb7070c-8264-42be-8610-beb78611a45a",
    "pvc-e27c9385-8a47-4135-9e34-f2feef2adff8",
    "pvc-09b15a09-1e56-4ccd-8b55-0af665ef19b9",
    "pvc-2150869f-e910-4c4b-af67-8430259b5fb6",
    "pvc-407bde1a-4411-4709-a745-078ea33bc722",
    "pvc-438957a9-ded8-449b-b7b3-814fcfc428f2",
    "pvc-4c4b4f5e-0b80-472a-a9ba-9fc321ac9d09",
    "pvc-639da326-1b4b-4808-99bc-852a7b20103c",
    "pvc-bdb14220-1db3-4324-91db-3bb1a749b430",
}
assert len(KEEP) == 25, f"KEEP must hold 25 entries, has {len(KEEP)}"


def midclt(*args):
    r = subprocess.run(["midclt", "call", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"midclt call {' '.join(args)}: {r.stderr.strip()[:300]}")
    out = r.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Queries return JSON, but scalar returns come back as Python reprs
        # (True/False/None). Delete methods hit this path; their value is unused.
        return {"True": True, "False": False, "None": None}.get(out, out)


def rel_id(v):
    """Public and datastore APIs disagree on whether FKs are ids or objects."""
    return v["id"] if isinstance(v, dict) else v


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f).get("done", []))
    return set()


def save_state(done):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"done": sorted(done)}, f)
    os.replace(tmp, STATE_FILE)


def survey():
    extents = midclt("iscsi.extent.query")
    joins = midclt("iscsi.targetextent.query")
    targets = midclt("iscsi.target.query")

    by_extent = {}
    refs = {}
    for j in joins:
        eid, tid = rel_id(j["extent"]), rel_id(j["target"])
        by_extent.setdefault(eid, []).append((j["id"], tid))
        refs[tid] = refs.get(tid, 0) + 1

    doomed, kept, foreign = [], [], 0
    for e in extents:
        path = e.get("path") or ""
        if not path.startswith(PREFIX):
            foreign += 1
            continue
        name = path[len(PREFIX):]
        if name in KEEP:
            kept.append(name)
            continue
        doomed.append({"name": name, "extent_id": e["id"],
                       "joins": by_extent.get(e["id"], [])})

    # Targets belonging to this cluster that no join row references. Identified
    # by alias, which carries the full dataset path in both target name forms.
    stranded = [
        t["id"] for t in targets
        if (t.get("alias") or "").startswith(ALIAS_PREFIX)
        and refs.get(t["id"], 0) == 0
        and (t.get("alias") or "")[len(ALIAS_PREFIX):] not in KEEP
    ]
    return doomed, kept, foreign, refs, stranded, stray_zvols(extents)


def destroy_zvol(name, attempts=6, delay=5):
    """Two ways a destroy fails here, both recoverable:

    'busy'         -- removing the extent only frees the device once the reload
                      lands, so a destroy issued right after can still see it
                      held. Retry rather than stranding a zvol that no longer
                      has an extent to find it by.
    'has children' -- the zvol carries its own snapshots. Escalate to -r, which
                      takes those too. Never -R: that would cascade into any
                      dependent clones, and -r simply errors in that case.
    """
    target = f"{PARENT}/{name}"
    flags = []
    for i in range(attempts):
        r = subprocess.run(["zfs", "destroy", *flags, target],
                           capture_output=True, text=True)
        if r.returncode == 0 or "does not exist" in r.stderr:
            return True, ""
        if "has children" in r.stderr and not flags:
            flags = ["-r"]
            continue
        if "busy" in r.stderr and i < attempts - 1:
            time.sleep(delay)
            continue
        return False, r.stderr.strip()
    return False, "still busy after retries"


def stray_zvols(extents):
    """Zvols under PARENT with no extent. survey() walks extents, so without
    this they are invisible -- which is exactly how a failed destroy hides."""
    paths = {e.get("path") for e in extents}
    r = subprocess.run(["zfs", "list", "-H", "-t", "volume", "-o", "name", "-r", PARENT],
                       capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.rsplit("/", 1)[-1]
        if name not in KEEP and f"zvol/{line}" not in paths:
            out.append(name)
    return out


def zvol_sizes():
    r = subprocess.run(["zfs", "list", "-H", "-p", "-t", "volume", "-o",
                        "name,used", "-r", PARENT], capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        if line.strip():
            n, used = line.split("\t")[:2]
            out[n.rsplit("/", 1)[-1]] = int(used)
    return out


def hms(sec):
    return f"{int(sec // 3600)}h{int(sec % 3600 // 60):02d}m"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge", action="store_true", help="actually delete")
    ap.add_argument("--limit", type=int, help="stop after N volumes (trial batch)")
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    args = ap.parse_args()

    doomed, kept, foreign, refs, stranded, strays = survey()
    sizes = zvol_sizes()
    done = load_state()
    todo = [d for d in doomed if d["name"] not in done]
    ops = sum(len(d["joins"]) * 2 + 1 for d in todo) + len(stranded)

    print(f"parent:           {PARENT}")
    print(f"extents in scope: {len(doomed) + len(kept)}   (other clusters: {foreign}, untouched)")
    print(f"keeping:          {len(kept)}")
    print(f"to reclaim:       {len(doomed)}  (~{sum(sizes.get(d['name'], 0) for d in doomed) / 1024**3:.1f} GiB)")
    if done:
        print(f"already done:     {len(done)}  (from {STATE_FILE})")
    print(f"remaining:        {len(todo)}")
    print(f"stranded targets: {len(stranded)}")
    if strays:
        print(f"stray zvols:      {len(strays)}  (extent already gone, destroy failed earlier)")
    print(f"iscsi mutations:  {ops}  -> ~{hms(ops * 27)} at 27s, less as the config shrinks")

    missing = KEEP - set(kept)
    if missing:
        print(f"\nWARNING: {len(missing)} KEEP entries have no extent under {PARENT}:")
        for m in sorted(missing):
            print(f"  {m}")
        print("Stale KEEP list, or a live volume lost its extent. Investigate before purging.")
        if args.purge:
            sys.exit(1)

    if not args.purge:
        print("\nRe-run with --purge (add --limit 10 for a trial batch).")
        return
    if not todo and not stranded:
        print("\nNothing to do.")
        return

    batch = todo[:args.limit] if args.limit else todo
    if not args.yes:
        print(f"\nAbout to reclaim {len(batch)} volume(s). Expect I/O latency spikes on"
              f"\nboth clusters throughout -- each mutation reloads the iSCSI config.")
        if input("Type 'yes' to continue: ").strip() != "yes":
            print("Aborted.")
            sys.exit(1)

    started = time.time()
    for i, d in enumerate(batch, 1):
        t0 = time.time()
        try:
            # join -> target -> extent: drops target count first, so each
            # subsequent reload has less config to regenerate.
            for join_id, target_id in d["joins"]:
                midclt("iscsi.targetextent.delete", str(join_id))
                refs[target_id] = refs.get(target_id, 1) - 1
            for _, target_id in d["joins"]:
                if refs.get(target_id, 0) <= 0:
                    midclt("iscsi.target.delete", str(target_id), "true")
                    refs[target_id] = -999
            midclt("iscsi.extent.delete", str(d["extent_id"]), "false", "true")

            ok, err = destroy_zvol(d["name"])
            if not ok:
                print(f"  WARN {d['name']}: zfs destroy: {err}")
        except RuntimeError as e:
            print(f"  FAIL {d['name']}: {e}")
            continue

        done.add(d["name"])
        save_state(done)
        el = time.time() - t0
        rate = (time.time() - started) / i
        print(f"  [{i}/{len(batch)}] {d['name']}  {el:.0f}s   eta {hms(rate * (len(batch) - i))}")

    if strays:
        print(f"\ndestroying {len(strays)} stray zvol(s)")
        for name in strays:
            ok, err = destroy_zvol(name)
            print(f"  {'OK  ' if ok else 'FAIL'} {name}{'' if ok else ': ' + err}")

    if stranded and not args.limit:
        print(f"\nremoving {len(stranded)} stranded target(s)")
        for tid in stranded:
            try:
                midclt("iscsi.target.delete", str(tid), "true")
            except RuntimeError as e:
                print(f"  FAIL target {tid}: {e}")

    print(f"\ndone in {hms(time.time() - started)}")
    print(f"extents now:   {len(midclt('iscsi.extent.query'))}")
    print(f"targets now:   {len(midclt('iscsi.target.query'))}")
    print(f"snapshots now: {subprocess.run(['zfs','list','-H','-t','snapshot','-r',PARENT], capture_output=True, text=True).stdout.count(chr(10))}")


if __name__ == "__main__":
    main()
