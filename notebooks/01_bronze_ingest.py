# Fabric notebook -- Bronze ingest
# =================================
# Generates the synthetic Meridian Construction Group dataset with intentional
# data-quality issues and lands it as raw CSVs in the Lakehouse Files area.
#
# HOW TO USE IN FABRIC:
#   1. Attach a Lakehouse to this notebook (the "Add Lakehouse" panel on the left).
#      This makes the path /lakehouse/default/Files/ available.
#   2. Run all cells. The messy CSVs land in Files/bronze/construction_raw/.
#   3. Bronze is intentionally RAW -- do not clean here. Cleaning happens in silver.
#
# Each "# CELL" marker below is a separate notebook cell.


# CELL 1 -- install Faker (Fabric has pandas/numpy; Faker may need installing)
# In Fabric, %pip runs against the session.
%pip install faker


# CELL 2 -- imports and reproducible seeds
import os
import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from faker import Faker

SEED = 42
N_PROJECTS = 120

random.seed(SEED)
np.random.seed(SEED)
Faker.seed(SEED)
fake = Faker()

# Land inside the attached Lakehouse. Bronze = raw landing zone.
OUTDIR = "/lakehouse/default/Files/bronze/construction_raw"
os.makedirs(OUTDIR, exist_ok=True)


# CELL 3 -- reference data
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
CSI_DIVISIONS = {
    "01": "General Requirements", "02": "Existing Conditions", "03": "Concrete",
    "04": "Masonry", "05": "Metals", "06": "Wood, Plastics & Composites",
    "07": "Thermal & Moisture Protection", "08": "Openings", "09": "Finishes",
    "21": "Fire Suppression", "22": "Plumbing", "23": "HVAC", "26": "Electrical",
    "27": "Communications", "31": "Earthwork", "32": "Exterior Improvements",
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


# CELL 4 -- mess helpers
def maybe_missing(value, p=0.05):
    return value if random.random() > p else None

def messy_date(dt, p_format=0.3, p_impossible=0.02):
    if dt is None:
        return None
    if random.random() < p_impossible:
        return random.choice(["2099-13-01", "0001-01-01", "13/45/2021", ""])
    fmts = ["%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%m-%d-%y", "%Y/%m/%d", "%b %d, %Y"]
    weights = [0.5, 0.2, 0.1, 0.1, 0.05, 0.05] if random.random() < p_format else [1, 0, 0, 0, 0, 0]
    return dt.strftime(random.choices(fmts, weights=weights)[0])

def dirty_name(name, p=0.25):
    if random.random() > p:
        return name
    return random.choice([
        name.upper(), name.lower(), f"  {name} ", name.replace(",", ""),
        name.replace("LLC", "L.L.C."), name.replace("Inc", "Inc."),
        name + " ", name.replace(" ", "  "),
    ])


# CELL 5 -- generators
def gen_projects(n):
    rows = []
    for i in range(1, n + 1):
        ptype = random.choice(PROJECT_TYPES)
        base = np.random.lognormal(mean=16.3, sigma=0.9)
        if ptype in ("Healthcare", "Data Center", "Mission Critical"):
            base *= random.uniform(1.5, 3.0)
        contract_value = round(base, 2)
        if random.random() < 0.02:
            contract_value = random.choice([-contract_value, 0, contract_value * 50])
        start = fake.date_between(start_date="-5y", end_date="-6M")
        planned_dur = random.randint(180, 1100)
        planned_end = start + timedelta(days=planned_dur)
        drift = int(np.random.normal(loc=planned_dur * 0.08, scale=planned_dur * 0.12))
        actual_end = planned_end + timedelta(days=max(drift, -30))
        status = random.choices(["Active", "Complete", "On Hold", "Closed"],
                                weights=[0.35, 0.5, 0.05, 0.1])[0]
        rows.append({
            "project_id": f"PRJ-{i:05d}",
            "project_name": maybe_missing(f"{fake.company()} {ptype} Facility", p=0.02),
            "project_type": maybe_missing(ptype, p=0.03),
            "region": random.choice(REGIONS),
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
    return pd.concat([df, df.sample(frac=0.02, random_state=1)], ignore_index=True)

def gen_subcontractors(n=80):
    rows, names = [], []
    for i in range(1, n + 1):
        sid = f"SUB-{i:04d}"
        raw = f"{fake.company()} {random.choice(['LLC', 'Inc', 'Contractors', 'Construction', 'Mechanical', 'Electric Co'])}"
        names.append((sid, raw))
        rows.append({
            "sub_id": sid, "sub_name": dirty_name(raw),
            "trade_focus": random.choice(list(CSI_DIVISIONS.values())),
            "region": random.choice(REGIONS),
            "performance_rating": maybe_missing(round(random.uniform(2.0, 5.0), 1), p=0.08),
            "prequalified": random.choice(["Y", "N", "Yes", "No", "TRUE", "FALSE", None]),
        })
    df = pd.DataFrame(rows)
    for sid, raw in random.sample(names, k=int(n * 0.15)):
        df = pd.concat([df, pd.DataFrame([{
            "sub_id": f"{sid}-DUP", "sub_name": dirty_name(raw, p=1.0),
            "trade_focus": random.choice(list(CSI_DIVISIONS.values())),
            "region": random.choice(REGIONS),
            "performance_rating": round(random.uniform(2.0, 5.0), 1),
            "prequalified": random.choice(["Y", "N"]),
        }])], ignore_index=True)
    return df

def gen_cost_line_items(projects):
    rows, lid = [], 1
    for _, p in projects.drop_duplicates("project_id").iterrows():
        cv = p["contract_value"]
        if not isinstance(cv, (int, float)) or cv <= 0:
            cv = random.uniform(5e6, 3e7)
        divs = random.sample(list(CSI_DIVISIONS.keys()), k=random.randint(6, len(CSI_DIVISIONS)))
        w = np.random.dirichlet(np.ones(len(divs)))
        for code, frac in zip(divs, w):
            budget = round(cv * frac, 2)
            actual = round(budget * random.uniform(0.85, 1.4), 2)
            change_orders = round(budget * random.uniform(0, 0.15), 2) if random.random() < 0.4 else 0.0
            if random.random() < 0.03:
                actual = random.choice([None, -actual, 0])
            rows.append({
                "line_item_id": f"CLI-{lid:07d}", "project_id": p["project_id"],
                "csi_division": code, "division_name": CSI_DIVISIONS[code],
                "budget_amount": maybe_missing(budget, p=0.02), "actual_amount": actual,
                "change_order_amount": change_orders,
                "cost_code_notes": maybe_missing(fake.sentence(nb_words=4), p=0.7),
            })
            lid += 1
    return pd.DataFrame(rows)

def gen_schedule_tasks(projects):
    rows, tid = [], 1
    task_names = ["Mobilization", "Site Prep", "Foundations", "Structural Steel",
                  "Concrete Pour", "Roofing", "MEP Rough-in", "Drywall", "Finishes",
                  "Commissioning", "Punch List", "Substantial Completion"]
    for _, p in projects.drop_duplicates("project_id").iterrows():
        start = pd.to_datetime(p["start_date"], errors="coerce")
        if pd.isna(start):
            start = datetime.now() - timedelta(days=random.randint(200, 800))
        cursor, prev_task = start, None
        for tname in task_names:
            planned_dur = random.randint(10, 90)
            actual_dur = max(1, int(np.random.normal(planned_dur * 1.05, planned_dur * 0.2)))
            rows.append({
                "task_id": f"TSK-{tid:07d}", "project_id": p["project_id"],
                "task_name": tname, "predecessor_task": prev_task,
                "planned_start": messy_date(cursor, p_impossible=0.0),
                "planned_duration_days": planned_dur,
                "actual_duration_days": maybe_missing(actual_dur, p=0.05),
                "percent_complete": maybe_missing(
                    random.choice([0, 25, 50, 75, 100, random.randint(0, 100)]), p=0.03),
            })
            prev_task, cursor = tname, cursor + timedelta(days=actual_dur)
            tid += 1
    return pd.DataFrame(rows)

def gen_labor_timesheets(projects, n_per_project=40):
    rows, wid = [], 1
    for _, p in projects.drop_duplicates("project_id").iterrows():
        for _ in range(random.randint(15, n_per_project)):
            rows.append({
                "timesheet_id": f"TS-{wid:08d}", "project_id": p["project_id"],
                "worker_name": maybe_missing(fake.name(), p=0.04),
                "trade": random.choice(TRADES),
                "work_date": messy_date(fake.date_between(start_date="-3y", end_date="today")),
                "regular_hours": random.choice([8, 8, 8, 10, 4, 12]),
                "overtime_hours": random.choice([0, 0, 0, 2, 4, random.randint(0, 6)]),
                "hourly_rate": maybe_missing(round(random.uniform(28, 78), 2), p=0.03),
            })
            wid += 1
    return pd.DataFrame(rows)

def gen_sub_bids(projects, subs):
    rows, bid = [], 1
    sub_ids = subs["sub_id"].tolist()
    for _, p in projects.drop_duplicates("project_id").iterrows():
        for code in random.sample(list(CSI_DIVISIONS.keys()), k=random.randint(3, 8)):
            for _ in range(random.randint(1, 5)):
                rows.append({
                    "bid_id": f"BID-{bid:07d}", "project_id": p["project_id"],
                    "sub_id": random.choice(sub_ids), "csi_division": code,
                    "bid_amount": round(np.random.lognormal(14.5, 0.6), 2),
                    "awarded": random.choice(["Y", "N", "N", "N"]),
                    "bid_date": messy_date(fake.date_between(start_date="-4y", end_date="today")),
                })
                bid += 1
    return pd.DataFrame(rows)

def gen_safety_incidents(projects):
    rows, iid = [], 1
    for _, p in projects.drop_duplicates("project_id").iterrows():
        for _ in range(np.random.poisson(1.5)):
            sev = random.choices(SEVERITY, weights=[0.5, 0.25, 0.1, 0.15])[0]
            rows.append({
                "incident_id": f"INC-{iid:06d}", "project_id": p["project_id"],
                "incident_date": messy_date(fake.date_between(start_date="-3y", end_date="today")),
                "incident_type": random.choice(INCIDENT_TYPES), "severity": sev,
                "trade_involved": random.choice(TRADES),
                "lost_days": maybe_missing(random.randint(0, 30) if sev == "Lost Time" else 0, p=0.05),
                "description": maybe_missing(fake.sentence(nb_words=8), p=0.3),
                "root_cause": maybe_missing(
                    random.choice(["Housekeeping", "PPE", "Training", "Equipment", "Weather", "Unknown"]), p=0.2),
            })
            iid += 1
    return pd.DataFrame(rows)


# CELL 6 -- generate all tables
projects = gen_projects(N_PROJECTS)
subs     = gen_subcontractors()
costs    = gen_cost_line_items(projects)
schedule = gen_schedule_tasks(projects)
labor    = gen_labor_timesheets(projects)
bids     = gen_sub_bids(projects, subs)
safety   = gen_safety_incidents(projects)

tables = {
    "projects": projects, "subcontractors": subs, "cost_line_items": costs,
    "schedule_tasks": schedule, "labor_timesheets": labor,
    "sub_bids": bids, "safety_incidents": safety,
}


# CELL 7 -- write raw CSVs to the bronze Files area
for name, df in tables.items():
    path = f"{OUTDIR}/{name}.csv"
    df.to_csv(path, index=False)
    print(f"{name:20s} {len(df):>7,} rows -> {path}")

print(f"\nBronze landing complete: {sum(len(d) for d in tables.values()):,} rows across {len(tables)} files.")


# CELL 8 -- quick sanity peek (confirm the mess survived into bronze)
import pandas as pd
preview = pd.read_csv(f"{OUTDIR}/projects.csv")
print("Sample of raw project rows (note inconsistent date formats, possible nulls):")
preview[["project_id", "start_date", "planned_end_date", "actual_end_date", "contract_value"]].head(10)
