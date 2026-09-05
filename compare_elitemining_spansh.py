#!/usr/bin/env python3
import argparse
import csv
import json
import random
import sqlite3
import time
from pathlib import Path

import requests

DB_URL = "https://github.com/Viper-Dude/EliteMining/raw/refs/heads/main/app/data/UserDb%20for%20install/user_data.db"
SPANSH_URL = "https://spansh.co.uk/api/bodies/search"
USER_AGENT = "EliteMining-Spansh-Compare/2.0"

# Material aliases known to be naming-only differences between datasets.
MATERIAL_ALIASES = {
    "void opals": "void opal",
}

def download_db(path: Path):
    if path.exists():
        return
    print(f"Downloading EliteMining DB -> {path}")
    r = requests.get(DB_URL, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)

def norm(s):
    return " ".join((s or "").strip().lower().split())

def norm_material(s):
    n = norm(s)
    return MATERIAL_ALIASES.get(n, n)

def norm_ring(system, ring):
    s = (ring or "").strip()
    if s.lower().startswith(system.lower()):
        s = s[len(system):].strip()
    return norm(s)

def safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

def load_elite(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(hotspot_data)")}
    wanted = [
        "system_name", "body_name", "material_name", "hotspot_count",
        "ring_type", "reserve_level", "ls_distance", "data_source"
    ]
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

        key = (
            norm(system),
            norm_ring(system, ring),
            norm_material(material),
        )
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

        bodies = obj.get("results", []) or []
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

                    key = (
                        norm(system),
                        norm_ring(system, ring_name),
                        norm_material(material),
                    )

                    all_rows[key] = {
                        "system_name": system,
                        "body_name": ring_name,
                        "material_name": material,
                        "hotspot_count": count,
                        "ring_type": ring_type,
                        "reserve_level": reserve,
                        "ls_distance": ls,
                        "data_source": "Spansh",
                    }

        if not bodies or (page + 1) * page_size >= total:
            break

        page += 1
        time.sleep(delay)

    return all_rows

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def build_indexes(data):
    systems = set()
    rings = set()

    for system_key, ring_key, _material_key in data:
        systems.add(system_key)
        rings.add((system_key, ring_key))

    return systems, rings

def write_simple_rows(path, keys, source):
    fields = [
        "system_name", "body_name", "material_name", "hotspot_count",
        "ring_type", "reserve_level", "ls_distance", "data_source"
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in sorted(keys):
            d = source[k]
            w.writerow({x: d.get(x, "") for x in fields})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="elitemining_user_data.db")
    ap.add_argument(
        "--sample", type=int, default=0,
        help="0 = all systems; otherwise random sample of N systems"
    )
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
        print(f"Random sample: {len(systems):,} systems (seed={args.seed})")

    wanted = {norm(s) for s in systems}
    elite_subset = {k: v for k, v in elite.items() if k[0] in wanted}

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
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

    common = elite_keys & spansh_keys
    only_spansh = spansh_keys - elite_keys
    missing = elite_keys - spansh_keys

    exact_match = []
    count_diff = []

    for k in common:
        ec = safe_int(elite_subset[k].get("hotspot_count"))
        sc = safe_int(spansh[k].get("hotspot_count"))

        if ec == sc:
            exact_match.append(k)
        else:
            count_diff.append(k)

    spansh_systems, spansh_rings = build_indexes(spansh)

    classified_missing = {
        "MISSING_SYSTEM": [],
        "MISSING_RING": [],
        "MISSING_HOTSPOT": [],
    }

    for k in missing:
        system_key, ring_key, _material_key = k
        if system_key not in spansh_systems:
            classified_missing["MISSING_SYSTEM"].append(k)
        elif (system_key, ring_key) not in spansh_rings:
            classified_missing["MISSING_RING"].append(k)
        else:
            classified_missing["MISSING_HOTSPOT"].append(k)

    # Legacy/simple files retained for convenience.
    write_simple_rows(out / "missing_on_spansh.csv", missing, elite_subset)
    write_simple_rows(out / "only_on_spansh.csv", only_spansh, spansh)

    # Detailed comparison report.
    detail_fields = [
        "status",
        "system_name",
        "body_name",
        "elite_material",
        "spansh_material",
        "normalized_material",
        "elite_count",
        "spansh_count",
        "elite_ring_type",
        "spansh_ring_type",
        "elite_reserve_level",
        "spansh_reserve_level",
        "elite_ls_distance",
        "spansh_ls_distance",
        "elite_data_source",
    ]

    with (out / "comparison_detailed.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=detail_fields)
        w.writeheader()

        for k in sorted(exact_match):
            e, s = elite_subset[k], spansh[k]
            w.writerow({
                "status": "MATCH",
                "system_name": e.get("system_name", ""),
                "body_name": e.get("body_name", ""),
                "elite_material": e.get("material_name", ""),
                "spansh_material": s.get("material_name", ""),
                "normalized_material": k[2],
                "elite_count": e.get("hotspot_count", ""),
                "spansh_count": s.get("hotspot_count", ""),
                "elite_ring_type": e.get("ring_type", ""),
                "spansh_ring_type": s.get("ring_type", ""),
                "elite_reserve_level": e.get("reserve_level", ""),
                "spansh_reserve_level": s.get("reserve_level", ""),
                "elite_ls_distance": e.get("ls_distance", ""),
                "spansh_ls_distance": s.get("ls_distance", ""),
                "elite_data_source": e.get("data_source", ""),
            })

        for k in sorted(count_diff):
            e, s = elite_subset[k], spansh[k]
            w.writerow({
                "status": "COUNT_DIFFERENCE",
                "system_name": e.get("system_name", ""),
                "body_name": e.get("body_name", ""),
                "elite_material": e.get("material_name", ""),
                "spansh_material": s.get("material_name", ""),
                "normalized_material": k[2],
                "elite_count": e.get("hotspot_count", ""),
                "spansh_count": s.get("hotspot_count", ""),
                "elite_ring_type": e.get("ring_type", ""),
                "spansh_ring_type": s.get("ring_type", ""),
                "elite_reserve_level": e.get("reserve_level", ""),
                "spansh_reserve_level": s.get("reserve_level", ""),
                "elite_ls_distance": e.get("ls_distance", ""),
                "spansh_ls_distance": s.get("ls_distance", ""),
                "elite_data_source": e.get("data_source", ""),
            })

        for status in ("MISSING_HOTSPOT", "MISSING_RING", "MISSING_SYSTEM"):
            for k in sorted(classified_missing[status]):
                e = elite_subset[k]
                w.writerow({
                    "status": status,
                    "system_name": e.get("system_name", ""),
                    "body_name": e.get("body_name", ""),
                    "elite_material": e.get("material_name", ""),
                    "spansh_material": "",
                    "normalized_material": k[2],
                    "elite_count": e.get("hotspot_count", ""),
                    "spansh_count": "",
                    "elite_ring_type": e.get("ring_type", ""),
                    "spansh_ring_type": "",
                    "elite_reserve_level": e.get("reserve_level", ""),
                    "spansh_reserve_level": "",
                    "elite_ls_distance": e.get("ls_distance", ""),
                    "spansh_ls_distance": "",
                    "elite_data_source": e.get("data_source", ""),
                })

    # Dedicated count difference file.
    with (out / "count_differences.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = [
            "system_name", "body_name", "elite_material", "spansh_material",
            "elite_count", "spansh_count"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        for k in sorted(count_diff):
            e, s = elite_subset[k], spansh[k]
            w.writerow({
                "system_name": e.get("system_name", ""),
                "body_name": e.get("body_name", ""),
                "elite_material": e.get("material_name", ""),
                "spansh_material": s.get("material_name", ""),
                "elite_count": e.get("hotspot_count", ""),
                "spansh_count": s.get("hotspot_count", ""),
            })

    # Separate files for the three genuinely interesting missing categories.
    for status, filename in [
        ("MISSING_SYSTEM", "missing_systems.csv"),
        ("MISSING_RING", "missing_rings.csv"),
        ("MISSING_HOTSPOT", "missing_hotspots.csv"),
    ]:
        write_simple_rows(out / filename, classified_missing[status], elite_subset)

    systems_to_scan = sorted(
        {
            elite_subset[k]["_system"]
            for status in ("MISSING_SYSTEM", "MISSING_RING", "MISSING_HOTSPOT")
            for k in classified_missing[status]
        },
        key=str.lower,
    )
    (out / "systems_to_scan.txt").write_text(
        "\n".join(systems_to_scan) + ("\n" if systems_to_scan else ""),
        encoding="utf-8",
    )

    summary = {
        "systems_tested": len(systems),
        "elite_records_tested": len(elite_subset),
        "spansh_records_found": len(spansh),
        "matched_records": len(common),
        "exact_count_matches": len(exact_match),
        "count_differences": len(count_diff),
        "missing_system_records": len(classified_missing["MISSING_SYSTEM"]),
        "missing_ring_records": len(classified_missing["MISSING_RING"]),
        "missing_hotspot_records": len(classified_missing["MISSING_HOTSPOT"]),
        "elite_records_missing_on_spansh_total": len(missing),
        "systems_with_any_missing_elite_data": len(systems_to_scan),
        "spansh_records_missing_in_elite_db": len(only_spansh),
        "failed_system_queries": len(failures),
        "failed_systems": failures,
        "normalization_aliases": MATERIAL_ALIASES,
    }

    if elite_subset:
        summary["elite_record_match_rate_percent"] = round(
            (len(common) / len(elite_subset)) * 100, 3
        )
        summary["elite_exact_count_match_rate_percent"] = round(
            (len(exact_match) / len(elite_subset)) * 100, 3
        )

    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Outputs written to: {out.resolve()}")

if __name__ == "__main__":
    main()
