#!/usr/bin/env python3
"""
Synthetic construction-company dataset generator (JE Dunn-style).

Produces a multi-table relational dataset with INTENTIONAL data-quality issues,
designed to feed a Fabric Lakehouse bronze layer and justify a bronze->silver->gold
medallion cleanup story.

Tables:
    projects.csv          - master project list
    cost_line_items.csv   - budget vs actual by CSI division, change orders
    schedule_tasks.csv    - planned vs actual durations, dependencies
    labor_timesheets.csv  - crew hours by trade, overtime
    subcontractors.csv    - vendor master with dirty names
    sub_bids.csv          - subcontractor bids per project/division
    safety_incidents.csv  - incident log (classification target)

Deliberate mess injected: missing values, inconsistent date formats, duplicate
rows, mismatched/duplicated vendor spellings, negative & zero costs, whitespace,
mixed casing, outliers, and a few impossible dates.

Usage:
    pip install faker numpy pandas
    python generate_construction_data.py --projects 120 --seed 42 --outdir ./construction_raw
"""

import argparse
import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from faker import Faker

fake = Faker()

# ----------------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------------

# JE Dunn's actual verticals
PROJECT_TYPES = [
    "Healthcare", "Commercial", "Higher Education", "K-12 Education",
    "Government/Civic", "Aviation", "Data Center", "Mission Critical",
    "Industrial", "Sports & Entertainment",
]

DELIVERY_METHODS = ["CM at Risk", "Design-Build", "Design-Bid-Build", "IPD", "CM Agency"]

REGIONS = [
    "Kansas City", "Denver", "Dallas", "Houston", "Atlanta", "Phoenix",
    "Portland", "Nashville", "Minneapolis", "Omaha", "Austin", "Charlotte",
]

# CSI MasterFormat-style divisions (the language a construction shop speaks)
CSI_DIVISIONS = {
    "01": "General Requirements",
    "02": "Existing Conditions",
    "03": "Concrete",
    "04": "Masonry",
    "05": "Metals",
    "06": "Wood, Plastics & Composites",
    "07": "Thermal & Moisture Protection",
    "08": "Openings",
    "09": "Finishes",
    "21": "Fire Suppression",
    "22": "Plumbing",
    "23": "HVAC",
    "26": "Electrical",
    "27": "Communications",
    "31": "Earthwork",
    "32": "Exterior Improvements",
    "33": "Utilities",
}

TRADES = [
    "Carpenter", "Electrician", "Plumber", "Ironworker", "Laborer",
    "Operator", "Concrete Finisher", "HVAC Tech", "Foreman", "Superintendent",
]

INCIDENT_TYPES = [
    "Slip/Trip/Fall", "Struck-by", "Caught-between", "Electrical",
    "Fall from Height", "Laceration", "Strain/Sprain", "Near Miss",
    "Equipment Damage", "Heat Illness",
]

SEVERITY = ["First Aid", "Recordable", "Lost Time", "Near Miss"]

# ----------------------------------------------------------------------------
# Mess helpers
# ----------------------------------------------------------------------------

def maybe_missing(value, p=0.05):
    """Randomly blank out a value."""
    return value if random.random() > p else None


def messy_date(dt, p_format=0.3, p_impossible=0.02):
    """Return a date string in an inconsistent format, occasionally impossible."""
    if dt is None:
        return None
    if random.random() < p_impossible:
        # impossible / obviously-wrong date to be caught in silver
        return random.choice(["2099-13-01", "0001-01-01", "13/45/2021", ""])
    fmts = ["%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%m-%d-%y", "%Y/%m/%d", "%b %d, %Y"]
    weights = [0.5, 0.2, 0.1, 0.1, 0.05, 0.05] if random.random() < p_format else [1, 0, 0, 0, 0, 0]
    fmt = random.choices(fmts, weights=weights)[0]
    return dt.strftime(fmt)


def dirty_name(name, p=0.25):
    """Introduce whitespace / casing / punctuation inconsistencies into a vendor name."""
    if random.random() > p:
        return name
    variants = [
        name.upper(),
        name.lower(),
        f"  {name} ",
        name.replace(",", ""),
        name.replace("LLC", "L.L.C."),
        name.replace("Inc", "Inc."),
        name + " ",
        name.replace(" ", "  "),
    ]
    return random.choice(variants)


# ----------------------------------------------------------------------------
# Generators
# ----------------------------------------------------------------------------

def gen_projects(n):
    rows = []
    for i in range(1, n + 1):
        pid = f"PRJ-{i:05d}"
        ptype = random.choice(PROJECT_TYPES)
        region = random.choice(REGIONS)
        # contract value: log-normal-ish, healthcare/data center skew high
        base = np.random.lognormal(mean=16.3, sigma=0.9)  # ~ $12M median
        if ptype in ("Healthcare", "Data Center", "Mission Critical"):
            base *= random.uniform(1.5, 3.0)
        contract_value = round(base, 2)
        # occasional outlier / bad value
        if random.random() < 0.02:
            contract_value = random.choice([-contract_value, 0, contract_value * 50])

        start = fake.date_between(start_date="-5y", end_date="-6M")
        planned_dur = random.randint(180, 1100)  # days
        planned_end = start + timedelta(days=planned_dur)
        # actual end drifts (delays common)
        drift = int(np.random.normal(loc=planned_dur * 0.08, scale=planned_dur * 0.12))
        actual_end = planned_end + timedelta(days=max(drift, -30))

        status = random.choices(
            ["Active", "Complete", "On Hold", "Closed"],
            weights=[0.35, 0.5, 0.05, 0.1],
        )[0]

        rows.append({
            "project_id": pid,
            "project_name": maybe_missing(f"{fake.company()} {ptype} Facility", p=0.02),
            "project_type": maybe_missing(ptype, p=0.03),
            "region": region,
            "delivery_method": random.choice(DELIVERY_METHODS),
            "contract_value": contract_value,
            "start_date": messy_date(start),
            "planned_end_date": messy_date(planned_end),
            "actual_end_date": messy_date(actual_end) if status in ("Complete", "Closed") else None,
            "square_footage": maybe_missing(random.randint(15000, 900000), p=0.04),
            "status": status,
            "project_manager": maybe_missing(fake.name(), p=0.03),
        })
    df = pd.DataFrame(rows)
    # inject ~2% duplicate rows
    dupes = df.sample(frac=0.02, random_state=1)
    return pd.concat([df, dupes], ignore_index=True)


def gen_subcontractors(n=80):
    rows = []
    names = []
    for i in range(1, n + 1):
        sid = f"SUB-{i:04d}"
        raw = f"{fake.company()} {random.choice(['LLC', 'Inc', 'Contractors', 'Construction', 'Mechanical', 'Electric Co'])}"
        names.append((sid, raw))
        rows.append({
            "sub_id": sid,
            "sub_name": dirty_name(raw),
            "trade_focus": random.choice(list(CSI_DIVISIONS.values())),
            "region": random.choice(REGIONS),
            "performance_rating": maybe_missing(round(random.uniform(2.0, 5.0), 1), p=0.08),
            "prequalified": random.choice(["Y", "N", "Yes", "No", "TRUE", "FALSE", None]),
        })
    df = pd.DataFrame(rows)
    # inject duplicate vendors under slightly different spellings (classic entity-resolution mess)
    for sid, raw in random.sample(names, k=int(n * 0.15)):
        df = pd.concat([df, pd.DataFrame([{
            "sub_id": f"{sid}-DUP",
            "sub_name": dirty_name(raw, p=1.0),
            "trade_focus": random.choice(list(CSI_DIVISIONS.values())),
            "region": random.choice(REGIONS),
            "performance_rating": round(random.uniform(2.0, 5.0), 1),
            "prequalified": random.choice(["Y", "N"]),
        }])], ignore_index=True)
    return df


def gen_cost_line_items(projects):
    rows = []
    lid = 1
    for _, p in projects.drop_duplicates("project_id").iterrows():
        cv = p["contract_value"]
        if not isinstance(cv, (int, float)) or cv <= 0:
            cv = random.uniform(5e6, 3e7)
        divs = random.sample(list(CSI_DIVISIONS.keys()), k=random.randint(6, len(CSI_DIVISIONS)))
        # random weights that roughly sum to 1
        w = np.random.dirichlet(np.ones(len(divs)))
        for code, frac in zip(divs, w):
            budget = round(cv * frac, 2)
            # actual varies around budget; overruns common
            actual = round(budget * random.uniform(0.85, 1.4), 2)
            change_orders = round(budget * random.uniform(0, 0.15), 2) if random.random() < 0.4 else 0.0
            # inject mess
            if random.random() < 0.03:
                actual = random.choice([None, -actual, 0])
            rows.append({
                "line_item_id": f"CLI-{lid:07d}",
                "project_id": p["project_id"],
                "csi_division": code,
                "division_name": CSI_DIVISIONS[code],
                "budget_amount": maybe_missing(budget, p=0.02),
                "actual_amount": actual,
                "change_order_amount": change_orders,
                "cost_code_notes": maybe_missing(fake.sentence(nb_words=4), p=0.7),
            })
            lid += 1
    return pd.DataFrame(rows)


def gen_schedule_tasks(projects):
    rows = []
    tid = 1
    task_names = [
        "Mobilization", "Site Prep", "Foundations", "Structural Steel",
        "Concrete Pour", "Roofing", "MEP Rough-in", "Drywall", "Finishes",
        "Commissioning", "Punch List", "Substantial Completion",
    ]
    for _, p in projects.drop_duplicates("project_id").iterrows():
        try:
            start = pd.to_datetime(p["start_date"], errors="coerce")
        except Exception:
            start = None
        if start is None or pd.isna(start):
            start = datetime.now() - timedelta(days=random.randint(200, 800))
        cursor = start
        prev_task = None
        for tname in task_names:
            planned_dur = random.randint(10, 90)
            actual_dur = max(1, int(np.random.normal(planned_dur * 1.05, planned_dur * 0.2)))
            t_start = cursor
            rows.append({
                "task_id": f"TSK-{tid:07d}",
                "project_id": p["project_id"],
                "task_name": tname,
                "predecessor_task": prev_task,
                "planned_start": messy_date(t_start, p_impossible=0.0),
                "planned_duration_days": planned_dur,
                "actual_duration_days": maybe_missing(actual_dur, p=0.05),
                "percent_complete": maybe_missing(
                    random.choice([0, 25, 50, 75, 100, random.randint(0, 100)]), p=0.03
                ),
            })
            prev_task = tname
            cursor = cursor + timedelta(days=actual_dur)
            tid += 1
    return pd.DataFrame(rows)


def gen_labor_timesheets(projects, n_per_project=40):
    rows = []
    wid = 1
    for _, p in projects.drop_duplicates("project_id").iterrows():
        for _ in range(random.randint(15, n_per_project)):
            trade = random.choice(TRADES)
            reg_hours = random.choice([8, 8, 8, 10, 4, 12])
            ot = random.choice([0, 0, 0, 2, 4, random.randint(0, 6)])
            rate = round(random.uniform(28, 78), 2)
            work_date = fake.date_between(start_date="-3y", end_date="today")
            rows.append({
                "timesheet_id": f"TS-{wid:08d}",
                "project_id": p["project_id"],
                "worker_name": maybe_missing(fake.name(), p=0.04),
                "trade": trade,
                "work_date": messy_date(work_date),
                "regular_hours": reg_hours,
                "overtime_hours": ot,
                "hourly_rate": maybe_missing(rate, p=0.03),
                # note: no total column on purpose — a gold-layer calc
            })
            wid += 1
    return pd.DataFrame(rows)


def gen_sub_bids(projects, subs):
    rows = []
    bid = 1
    sub_ids = subs["sub_id"].tolist()
    for _, p in projects.drop_duplicates("project_id").iterrows():
        divs = random.sample(list(CSI_DIVISIONS.keys()), k=random.randint(3, 8))
        for code in divs:
            n_bidders = random.randint(1, 5)
            for _ in range(n_bidders):
                amt = round(np.random.lognormal(14.5, 0.6), 2)
                rows.append({
                    "bid_id": f"BID-{bid:07d}",
                    "project_id": p["project_id"],
                    "sub_id": random.choice(sub_ids),
                    "csi_division": code,
                    "bid_amount": amt,
                    "awarded": random.choice(["Y", "N", "N", "N"]),
                    "bid_date": messy_date(fake.date_between(start_date="-4y", end_date="today")),
                })
                bid += 1
    return pd.DataFrame(rows)


def gen_safety_incidents(projects):
    rows = []
    iid = 1
    for _, p in projects.drop_duplicates("project_id").iterrows():
        n = np.random.poisson(1.5)  # most projects few/none
        for _ in range(n):
            sev = random.choices(SEVERITY, weights=[0.5, 0.25, 0.1, 0.15])[0]
            rows.append({
                "incident_id": f"INC-{iid:06d}",
                "project_id": p["project_id"],
                "incident_date": messy_date(fake.date_between(start_date="-3y", end_date="today")),
                "incident_type": random.choice(INCIDENT_TYPES),
                "severity": sev,
                "trade_involved": random.choice(TRADES),
                "lost_days": maybe_missing(
                    random.randint(0, 30) if sev == "Lost Time" else 0, p=0.05
                ),
                "description": maybe_missing(fake.sentence(nb_words=8), p=0.3),
                "root_cause": maybe_missing(
                    random.choice(["Housekeeping", "PPE", "Training", "Equipment", "Weather", "Unknown"]),
                    p=0.2,
                ),
            })
            iid += 1
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--outdir", type=str, default="./construction_raw")
    args = ap.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    Faker.seed(args.seed)

    import os
    os.makedirs(args.outdir, exist_ok=True)

    print(f"Generating {args.projects} projects...")
    projects = gen_projects(args.projects)
    subs = gen_subcontractors()
    costs = gen_cost_line_items(projects)
    schedule = gen_schedule_tasks(projects)
    labor = gen_labor_timesheets(projects)
    bids = gen_sub_bids(projects, subs)
    safety = gen_safety_incidents(projects)

    tables = {
        "projects": projects,
        "subcontractors": subs,
        "cost_line_items": costs,
        "schedule_tasks": schedule,
        "labor_timesheets": labor,
        "sub_bids": bids,
        "safety_incidents": safety,
    }

    for name, df in tables.items():
        path = os.path.join(args.outdir, f"{name}.csv")
        df.to_csv(path, index=False)
        print(f"  {name:20s} {len(df):>7,} rows -> {path}")

    total = sum(len(d) for d in tables.values())
    print(f"\nDone. {total:,} total rows across {len(tables)} tables in {args.outdir}/")


if __name__ == "__main__":
    main()
