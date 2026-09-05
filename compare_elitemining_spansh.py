#!/usr/bin/env python3
import argparse
import csv
import json
import random
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import requests

DB_URL = "https://github.com/Viper-Dude/EliteMining/raw/refs/heads/main/app/data/UserDb%20for%20install/user_data.db"
SPANSH_URL = "https://spansh.co.uk/api/bodies/search"
USER_AGENT = "EliteMining-Spansh-Compare/1.0"

def download_db(path: Path):
    if path.exists():
        return
    print(f"Downloading EliteMining DB -> {path}")
    r = requests.get(DB_URL, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)

def norm(s):
    return " ".join((s or "").strip().lower().split())

def norm_ring(system, ring):
    s = (ring or "").strip()
    if s.lower().startswith(system.lower()):
        s = s[len(system):].strip()
    return norm(s)

def load_elite(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(hotspot_data)")}
    wanted = ["system_name","body_name","material_name","hotspot_count","ring_type",
              "reserve_level","ls_distance","data_source"]
    select = [c for c in wanted if c in cols]
    rows = conn.execute(f"SELECT {','.join(select)} FROM hotspot_data").fetchall()
    conn.close()

    data = {}
    systems = set()
    for r in rows:
        d = dict(r)
        system = (d.get("system_name") or "").strip()
        ring = (d.get("body_name") or "").strip()
        material = (d.get("material_name") or "").strip()
        if not system or not ring or not material:
            continue
        key = (norm(system), norm_ring(system, ring), norm(material))
        d["_system"] = system
        d["_ring"] = ring
        d["_material"] = material
        data[key] = d
        systems.add(system)
    return data, sorted(systems, key=str.lower)

def fetch_spansh(session, systems, delay=1.6, page_size=500):
    all_rows = {}
    page = 0
    while True:
        payload = {
            "filters": {
                "system_name": {"value": systems},
                "rings": {"value": [True]}
            },
            "size": page_size,
            "page": page
        }
        r = session.post(SPANSH_URL, json=payload, timeout=60)
        r.raise_for_status()
        obj = r.json()
        bodies = obj.get("results", [])
        total = int(obj.get("count", 0) or 0)

        for body in bodies:
            system = (body.get("system_name") or "").strip()
            reserve = body.get("reserve_level", "")
            ls = body.get("distance_to_arrival", "")
            for ring in body.get("rings", []) or []:
                ring_name = (ring.get("name") or "").strip()
                ring_type = ring.get("type", "")
                for sig in ring.get("signals", []) or []:
                    material = (sig.get("name") or "").strip()
                    count = sig.get("count", 0)
                    if not system or not ring_name or not material:
                        continue
                    key = (norm(system), norm_ring(system, ring_name), norm(material))
                    all_rows[key] = {
                        "system_name": system,
                        "body_name": ring_name,
                        "material_name": material,
                        "hotspot_count": count,
                        "ring_type": ring_type,
                        "reserve_level": reserve,
                        "ls_distance": ls,
                    }

        if not bodies or (page + 1) * page_size >= total:
            break
        page += 1
        time.sleep(delay)
    return all_rows

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="elitemining_user_data.db")
    ap.add_argument("--sample", type=int, default=0,
                    help="0 = all systems; otherwise random sample of N systems")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--delay", type=float, default=1.6)
    ap.add_argument("--out", default="comparison_output")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db = Path(args.db)
    download_db(db)

    elite, systems = load_elite(db)
    print(f"EliteMining records: {len(elite):,}")
    print(f"Distinct systems: {len(systems):,}")

    if args.sample and args.sample < len(systems):
        random.seed(args.seed)
        systems = sorted(random.sample(systems, args.sample), key=str.lower)
        print(f"Random sample: {len(systems):,} systems")

    wanted = {norm(s) for s in systems}
    elite_subset = {k:v for k,v in elite.items() if k[0] in wanted}

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json"
    })

    spansh = {}
    batches = list(chunks(systems, args.batch_size))
    failures = []

    for i, batch in enumerate(batches, 1):
        print(f"[{i}/{len(batches)}] Spansh: {len(batch)} systems")
        try:
            rows = fetch_spansh(session, batch, args.delay)
            spansh.update(rows)
        except Exception as e:
            print(f"  ERROR: {e}")
            failures.extend(batch)
        if i < len(batches):
            time.sleep(args.delay)

    elite_keys = set(elite_subset)
    spansh_keys = set(spansh)

    missing_on_spansh = elite_keys - spansh_keys
    only_spansh = spansh_keys - elite_keys
    common = elite_keys & spansh_keys

    count_diff = []
    exact = 0
    for k in common:
        ec = int(elite_subset[k].get("hotspot_count") or 0)
        sc = int(spansh[k].get("hotspot_count") or 0)
        if ec == sc:
            exact += 1
        else:
            count_diff.append(k)

    def write_rows(filename, keys, source):
        with (out / filename).open("w", newline="", encoding="utf-8-sig") as f:
            fields = ["system_name","body_name","material_name","hotspot_count",
                      "ring_type","reserve_level","ls_distance","data_source"]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for k in sorted(keys):
                d = source[k]
                w.writerow({x: d.get(x, "") for x in fields})

    write_rows("missing_on_spansh.csv", missing_on_spansh, elite_subset)
    write_rows("only_on_spansh.csv", only_spansh, spansh)

    with (out / "count_differences.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["system_name","body_name","material_name","elite_count","spansh_count"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in sorted(count_diff):
            e, s = elite_subset[k], spansh[k]
            w.writerow({
                "system_name": e.get("system_name",""),
                "body_name": e.get("body_name",""),
                "material_name": e.get("material_name",""),
                "elite_count": e.get("hotspot_count",""),
                "spansh_count": s.get("hotspot_count","")
            })

    missing_systems = sorted({elite_subset[k]["_system"] for k in missing_on_spansh}, key=str.lower)
    (out / "systems_to_scan.txt").write_text("\n".join(missing_systems) + ("\n" if missing_systems else ""), encoding="utf-8")

    summary = {
        "systems_tested": len(systems),
        "elite_records_tested": len(elite_subset),
        "spansh_records_found": len(spansh),
        "common_records": len(common),
        "exact_count_matches": exact,
        "count_differences": len(count_diff),
        "elite_records_missing_on_spansh": len(missing_on_spansh),
        "systems_with_elite_data_missing_on_spansh": len(missing_systems),
        "spansh_records_missing_in_elite_db": len(only_spansh),
        "failed_system_queries": len(failures),
        "failed_systems": failures,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Outputs written to: {out.resolve()}")

if __name__ == "__main__":
    main()
