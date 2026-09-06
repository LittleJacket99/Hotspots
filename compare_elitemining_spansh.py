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
USER_AGENT = "EliteMining-Spansh-Compare/4.0"

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
    if system and s.lower().startswith(system.lower()):
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
        "ring_type", "reserve_level", "ls_distance", "data_source",
        "system_address"
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

def request_spansh(session, systems, page, page_size, retries=3):
    payload = {
        "filters": {
            "system_name": {"value": systems}
        },
        "size": page_size,
        "page": page
    }

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = session.post(SPANSH_URL, json=payload, timeout=90)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_error = e
            if attempt < retries:
                wait = 2 ** attempt
                print(f"    retry {attempt}/{retries - 1} after error: {e} (wait {wait}s)")
                time.sleep(wait)

    raise last_error

def fetch_spansh(session, systems, delay=1.6, page_size=500, retries=3):
    hotspots = {}
    systems_seen = set()
    rings_seen = set()

    page = 0

    while True:
        obj = request_spansh(
            session=session,
            systems=systems,
            page=page,
            page_size=page_size,
            retries=retries,
        )

        bodies = obj.get("results", []) or []
        total = int(obj.get("count", 0) or 0)

        for body in bodies:
            system = (body.get("system_name") or "").strip()
            if not system:
                continue

            system_key = norm(system)
            systems_seen.add(system_key)

            reserve = body.get("reserve_level", "")
            ls = body.get("distance_to_arrival", "")

            for ring in body.get("rings", []) or []:
                ring_name = (ring.get("name") or "").strip()
                if not ring_name:
                    continue

                ring_key = norm_ring(system, ring_name)
                rings_seen.add((system_key, ring_key))

                ring_type = ring.get("type", "")

                for sig in ring.get("signals", []) or []:
                    material = (sig.get("name") or "").strip()
                    if not material:
                        continue

                    key = (
                        system_key,
                        ring_key,
                        norm_material(material),
                    )

                    hotspots[key] = {
                        "system_name": system,
                        "body_name": ring_name,
                        "material_name": material,
                        "hotspot_count": sig.get("count", 0),
                        "ring_type": ring_type,
                        "reserve_level": reserve,
                        "ls_distance": ls,
                        "data_source": "Spansh",
                    }

        if not bodies or (page + 1) * page_size >= total:
            break

        page += 1
        time.sleep(delay)

    return hotspots, systems_seen, rings_seen

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def write_simple_rows(path, keys, source):
    fields = [
        "system_name", "body_name", "material_name", "hotspot_count",
        "ring_type", "reserve_level", "ls_distance", "data_source",
        "system_address"
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
    ap.add_argument("--sample", type=int, default=0,
                    help="0 = all systems; otherwise random sample of N systems")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--delay", type=float, default=1.6)
    ap.add_argument("--retries", type=int, default=3)
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
    spansh_systems_seen = set()
    spansh_rings_seen = set()

    failed_batches = []
    unresolved_systems = []

    batches = list(chunks(systems, args.batch_size))

    for i, batch in enumerate(batches, 1):
        print(f"[{i}/{len(batches)}] Spansh batch: {len(batch)} systems")

        try:
            rows, seen_systems, seen_rings = fetch_spansh(
                session,
                batch,
                delay=args.delay,
                retries=args.retries,
            )

            spansh.update(rows)
            spansh_systems_seen.update(seen_systems)
            spansh_rings_seen.update(seen_rings)

        except Exception as e:
            print(f"  BATCH FAILED after retries: {e}")
            print("  Falling back to one-system-at-a-time queries...")
            failed_batches.append(list(batch))

            for j, system in enumerate(batch, 1):
                print(f"    [{j}/{len(batch)}] {system}")

                try:
                    rows, seen_systems, seen_rings = fetch_spansh(
                        session,
                        [system],
                        delay=args.delay,
                        retries=args.retries,
                    )

                    spansh.update(rows)
                    spansh_systems_seen.update(seen_systems)
                    spansh_rings_seen.update(seen_rings)

                except Exception as single_error:
                    print(f"      UNRESOLVED: {single_error}")
                    unresolved_systems.append(system)

                time.sleep(args.delay)

        if i < len(batches):
            time.sleep(args.delay)

    unresolved_keys = {norm(s) for s in unresolved_systems}

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

    classified_missing = {
        "UNKNOWN": [],
        "MISSING_SYSTEM": [],
        "MISSING_RING": [],
        "MISSING_HOTSPOT": [],
    }

    for k in missing:
        system_key, ring_key, _material_key = k

        if system_key in unresolved_keys:
            classified_missing["UNKNOWN"].append(k)
        elif system_key not in spansh_systems_seen:
            classified_missing["MISSING_SYSTEM"].append(k)
        elif (system_key, ring_key) not in spansh_rings_seen:
            classified_missing["MISSING_RING"].append(k)
        else:
            classified_missing["MISSING_HOTSPOT"].append(k)

    write_simple_rows(out / "missing_on_spansh.csv", missing, elite_subset)
    write_simple_rows(out / "only_on_spansh.csv", only_spansh, spansh)

    for status, filename in [
        ("UNKNOWN", "unknown_systems.csv"),
        ("MISSING_SYSTEM", "missing_systems.csv"),
        ("MISSING_RING", "missing_rings.csv"),
        ("MISSING_HOTSPOT", "missing_hotspots.csv"),
    ]:
        write_simple_rows(out / filename, classified_missing[status], elite_subset)

    fields = [
        "status", "system_name", "body_name",
        "elite_material", "spansh_material", "normalized_material",
        "elite_count", "spansh_count",
        "elite_ring_type", "spansh_ring_type",
        "elite_reserve_level", "spansh_reserve_level",
        "elite_ls_distance", "spansh_ls_distance",
        "elite_data_source", "system_address"
    ]

    with (out / "comparison_detailed.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as f:
        w = csv.DictWriter(f, fieldnames=fields)
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
                "system_address": e.get("system_address", ""),
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
                "system_address": e.get("system_address", ""),
            })

        for status in ("UNKNOWN", "MISSING_SYSTEM", "MISSING_RING", "MISSING_HOTSPOT"):
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
                    "system_address": e.get("system_address", ""),
                })

    with (out / "count_differences.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as f:
        count_fields = [
            "system_name", "body_name", "elite_material", "spansh_material",
            "elite_count", "spansh_count", "system_address"
        ]

        w = csv.DictWriter(f, fieldnames=count_fields)
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
                "system_address": e.get("system_address", ""),
            })

    category_systems = {}

    for status in ("UNKNOWN", "MISSING_SYSTEM", "MISSING_RING", "MISSING_HOTSPOT"):
        category_systems[status] = sorted(
            {elite_subset[k]["_system"] for k in classified_missing[status]},
            key=str.lower
        )

    with (out / "systems_by_category.txt").open("w", encoding="utf-8") as f:
        for status in ("UNKNOWN", "MISSING_SYSTEM", "MISSING_RING", "MISSING_HOTSPOT"):
            vals = category_systems[status]

            f.write(f"{status} ({len(vals)} systems)\n")
            f.write("=" * 60 + "\n")

            for s in vals:
                f.write(s + "\n")

            f.write("\n")

    summary = {
        "systems_tested": len(systems),
        "elite_records_tested": len(elite_subset),

        "spansh_hotspot_records_found": len(spansh),
        "spansh_systems_with_body_data_seen": len(spansh_systems_seen),
        "spansh_rings_seen": len(spansh_rings_seen),

        "matched_records": len(common),
        "exact_count_matches": len(exact_match),
        "count_differences": len(count_diff),

        "unknown_records": len(classified_missing["UNKNOWN"]),
        "unknown_systems_unique": len(category_systems["UNKNOWN"]),

        "missing_system_records": len(classified_missing["MISSING_SYSTEM"]),
        "missing_systems_unique": len(category_systems["MISSING_SYSTEM"]),

        "missing_ring_records": len(classified_missing["MISSING_RING"]),
        "missing_rings_unique": len({
            (k[0], k[1]) for k in classified_missing["MISSING_RING"]
        }),
        "systems_with_missing_rings": len(category_systems["MISSING_RING"]),

        "missing_hotspot_records": len(classified_missing["MISSING_HOTSPOT"]),
        "missing_hotspots_total_count": sum(
            safe_int(elite_subset[k].get("hotspot_count"))
            for k in classified_missing["MISSING_HOTSPOT"]
        ),
        "systems_with_missing_hotspots": len(category_systems["MISSING_HOTSPOT"]),

        "elite_records_missing_on_spansh_total": len(missing),
        "spansh_records_missing_in_elite_db": len(only_spansh),

        "failed_batches_before_fallback": len(failed_batches),
        "unresolved_system_queries": len(unresolved_systems),
        "unresolved_systems": unresolved_systems,

        "normalization_aliases": MATERIAL_ALIASES,
    }

    if elite_subset:
        summary["elite_record_match_rate_percent"] = round(
            len(common) / len(elite_subset) * 100, 3
        )

        summary["elite_exact_count_match_rate_percent"] = round(
            len(exact_match) / len(elite_subset) * 100, 3
        )

    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Outputs written to: {out.resolve()}")

if __name__ == "__main__":
    main()
