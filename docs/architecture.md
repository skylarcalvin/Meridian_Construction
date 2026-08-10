# Architecture & Design Notes

Detailed design documentation for the Fabric Construction Analytics project. Where the README describes *what* the project is, this document explains *why* it's built the way it is — the design decisions, trade-offs, and lessons that shaped it.

---

## 1. Platform choices

**Microsoft Fabric (Lakehouse + Spark notebooks + Direct Lake).** The project targets a Fabric-native construction analytics scenario, so every layer uses Fabric primitives: a Lakehouse for storage (Delta tables + raw Files), Spark notebooks for transformation and ML, MLflow (built into Fabric) for experiment tracking and the model registry, and a Direct Lake semantic model for reporting.

**Direct Lake** was chosen for the semantic model because it reads Delta/Parquet directly from OneLake — no import refresh, no DirectQuery round-trips. It's the Fabric flagship reporting mode and the right fit for a gold layer that already lives as Delta tables.

**Capacity:** an F2 pay-as-you-go capacity, paused when idle to control cost. Reporting requires per-user Power BI Pro on sub-F64 capacities.

---

## 2. Medallion architecture

The pipeline follows the bronze → silver → gold medallion pattern. Each layer is a deliberate stage with a single responsibility.

### Bronze — raw landing
Synthetic data is generated with intentional quality problems and landed exactly as produced, as CSV in Lakehouse `Files/`. Bronze is faithful to source: nothing is cleaned here. This preserves the "what the source systems actually emit" state so silver has real work to demonstrate.

### Silver — cleaned & conformed
Silver resolves the mess and writes managed Delta tables:
- **Date parsing:** a multi-format parser tries each known format, then range-bounds the result (implausible dates outside 1990–2035 → null) so a year-1 or year-2099 value can't poison downstream logic.
- **Deduplication:** exact-duplicate rows dropped by primary key.
- **Vendor entity-resolution:** variant spellings (`Sawyer Group LLC` / `SAWYER GROUP L.L.C.` / `  sawyer group llc `) collapse to one entity via a normalized match key (lowercased, punctuation/whitespace stripped, legal-suffix normalized).
- **Type enforcement & boolean normalization:** `Y`/`Yes`/`TRUE` → a single boolean; numeric coercion with invalid values flagged.
- **Lineage stamping:** every row gets `batch_id`, `ingested_at`, and `data_split` — the columns that later enable provenance-based train/test evaluation.

### Gold — dimensional model
Gold reshapes silver into a star schema optimized for analytics and ML, plus model-ready feature tables. Details in §3–§4.

### Incremental ingest
`03_incremental_ingest` adds new batches to silver without rebuilding: it auto-detects the next batch number from what's already present, generates a signal-bearing batch with a distinct seed, stamps it as held-out `test` data, and appends idempotently (create-or-append with a key-based dedup safety net so re-runs can't duplicate).

---

## 3. Dimensional model

The gold star schema:

- **Dimensions:** `dim_project`, `dim_date`, `dim_vendor`, `dim_division`
- **Facts:** `fact_cost` (project × division), `fact_labor` (crew-day), `fact_safety` (incident), `fact_schedule` (task)

**Deterministic surrogate keys.** Dimensions assign surrogate keys with `row_number()` over an ordered window, and the dimension is materialized (cached) before any fact joins to it. This is deliberate: `monotonically_increasing_id()` — the obvious choice — is *unsafe* as a surrogate key, because Spark can recompute it lazily and produce different values when a fact table joins back to the dimension, silently corrupting the joins. This bug produces plausible-but-wrong results (the worst kind), so the design avoids it entirely.

**Referential integrity is validated, not enforced.** A Lakehouse has no foreign-key constraints. Gold therefore includes an explicit RI check: every fact row's surrogate key must resolve to a dimension, and orphans are counted and reported. Relationships are then declared in the Power BI semantic model (many-to-one, single-direction filter) so the report can navigate the star.

---

## 4. Feature engineering & leakage discipline

Two model-ready tables are built in gold:
- `ml_cost_training` — line-item grain, for cost analysis
- `ml_project_classification` — **project grain**, the table the risk classifier trains on

**Grain matters.** The business question is "is *this project* at risk of overrunning?" — one prediction per project — so the classifier trains at project grain, aggregating cost lines to a project-level outcome.

**Leakage discipline.** The single most important modeling decision: features include only what is known at project **start**. Project type, delivery method, region, contract value, square footage, planned duration, winter-start flag, and MEP budget share are all fixed at kickoff. Anything realized *during* execution — actual costs, actual durations, actual change orders, safety incidents — is excluded, because it wouldn't exist for a live project being scored. Including it would leak the outcome into the features and produce a model that can't actually predict at kickoff.

---

## 5. The model

**Framing: binary classification, not regression.** Predicting a probability of over-cost is more useful and more honest than predicting a dollar overrun on synthetic data. A calibrated probability drives action (add contingency, tighten scope); a fake-precise point estimate doesn't.

**Label & threshold.** `is_overrun` = project `actual ÷ budget > 1.18`. The threshold sits near the project-grain median, chosen so:
1. "Over-cost" means *worse than a typical project* (a meaningful flag, not "any overrun at all"), and
2. the class balance is naturally ~50/50 **without resampling**.

Avoiding resampling is deliberate: SMOTE/oversampling to force balance distorts the base rate and de-calibrates the predicted probabilities. Since the entire value of the output is a *trustworthy probability*, preserving the real base rate matters more than a balanced training set. Setting the threshold at the distribution's center achieves balance honestly.

**Bake-off & selection.** Five classifiers — logistic regression, Naive Bayes, random forest, gradient boosting, SVM — are compared under one shared preprocessing pipeline (one-hot for categoricals, standardization for numerics), with 5-fold stratified cross-validation on the training split. Five folds (not ten) suit the dataset size: at ~120 training projects, ten folds would leave too few positives per fold to estimate AUC stably.

The models perform comparably (CV-AUC ≈ 0.82–0.86, clustered within one standard deviation). **Logistic regression is selected** even though another model occasionally edges it on CV-AUC, because: (a) the CV margin is within noise, (b) LR had the best *held-out* AUC and best calibration (lowest Brier), and (c) LR yields interpretable odds ratios. For a decision-support score, interpretability + calibration outweigh a noise-level accuracy difference. The selection rationale is logged to MLflow alongside the metrics.

**Interpretability.** LR coefficients exponentiate to odds ratios that recover the engineered signal — CM Agency and Design-Bid-Build carry several times the odds of over-cost versus IPD. These are explainable to a non-technical stakeholder.

---

## 6. Training, tracking, and frozen scoring

**MLflow.** Each bake-off model is logged as its own run under one experiment (params, metrics, model artifact). The selected model is registered as `construction_overrun_classifier`. Fabric's native MLflow integration means this all appears in the workspace with no extra setup.

**Train/score separation.** Training (`06`) and scoring (`07`) are separate notebooks by design:
- `06` fits and registers the model.
- `07` loads the *registered, frozen* model and scores projects **without retraining**.

This separation is what makes evaluation on new data honest. When a new batch arrives, the refresh sequence is `03 → 04 → 05 → 07` (rebuild features, then score) — **skipping `06`**. The model stays fixed, so the new batch is a genuine holdout and predicted probabilities remain comparable across batches. Retraining on every batch would silently turn "validate the model on unseen data" into "train a new model each time," a different and weaker claim.

**Provenance-based split.** Because `data_split` is stamped at ingest and keyed to immutable `batch_id`, train/test membership is durable across gold rebuilds. The original load is `train`; incremental batches are `test`. This is a more realistic evaluation than a random shuffle — it mirrors "model trained on history, scored on new projects."

**Output.** `07` writes `project_risk_scores` — one row per project with `risk_probability`, a banded `risk_band` (Low/Medium/High), `predicted_overrun`, and the actual outcome for completed projects. This table feeds the dashboard's risk page directly.

---

## 7. Reporting

A Direct Lake semantic model over the gold tables plus `project_risk_scores`, with declared relationships (facts → dimensions, many-to-one) and DAX measures (totals, overrun %, counts, risk aggregates). Three report pages:

1. **Historical Portfolio** — the track record and the engineered signal made visible (overrun by project type and delivery method).
2. **Current Project Status** — operational view of active work (budget burn, schedule progress).
3. **Over-Cost Risk Analysis** — the model in action: risk-band distribution, a **calibration chart** (actual overrun rate rising with predicted risk band), a predicted-vs-actual scatter, and a flagged high-risk project list. A `status` slicer flips between validating on completed projects and assessing active ones.

The calibration chart is the analytical payoff: it shows the risk score is trustworthy — Low-band projects overran ~14% of the time, High-band ~89%.

---

## 8. Lessons & notable debugging

A few issues caught and resolved during the build — the kind of thing that separates a working pipeline from a *correct* one:

- **Surrogate-key corruption.** An early version used `monotonically_increasing_id()` for surrogate keys; joining facts back to dimensions scrambled the delivery-method signal because Spark recomputed the IDs. Caught by checking signal *after* the joins, not just before. Fixed with deterministic `row_number()` keys.
- **Signal-free incremental batches.** The incremental generator initially produced flat-noise overruns (no engineered signal), so the model trained on strong-signal data and tested on signal-free data — producing a below-0.5 test AUC. The pooled diagnostics hid it; a *per-split* signal check exposed the test batch as flat. Fixed by porting the full signal coefficients into the incremental generator. Lesson: always validate signal *within each split*, not pooled.
- **Out-of-range dates.** Injected impossible dates (year 1) survived to gold and broke both the Parquet writer and Power BI's DateTime transport range. Fixed by range-bounding dates at the gold layer (clamp to 1990–2035 → null) in addition to silver parsing.
- **Grain / threshold interaction.** The over-cost threshold that balanced classes at line-item grain produced an 80/20 imbalance at project grain, because project-level overruns cluster tighter. Resolved by setting the threshold at the project-grain median rather than reusing the line-item value.

---

## 9. Production considerations (out of scope, noted for completeness)

- **Orchestration:** the notebook sequence would run as a **Fabric Data Pipeline**, scheduled or triggered when new files land in bronze, rather than run by hand.
- **Incremental gold:** at scale, gold would MERGE new batches rather than full-overwrite.
- **Drift monitoring:** because every batch is lineage-tagged, per-batch model accuracy can be tracked over time to detect data drift.
- **Threshold as config:** the over-cost threshold could be made distribution-relative per batch rather than a fixed constant, if batch distributions shift materially.
