<div align="center">

<img src="./assets/CANOE_SWAP.png" alt="CANOE SWAP Logo" width="300"/>

### Database Merge & Cleanup Utility — CANOE Schema 4

</div>

---

## Overview

**CANOE SWAP** is a Python utility for replacing model sectors or database fragments inside a CANOE SQLite database.

This version is designed for **CANOE database schema 4.x** and understands the schema 4 `data_id` structure.

## Schema 4 behaviour

- Requires both the base and incoming databases to report `DB_MAJOR = 4` in the `metadata` table.
- Removes selected technologies and commodities from all applicable schema 4 input tables, rather than from a small hard-coded table list.
- Applies the requested `Region` filter to region-bearing tables.
- Uses incoming `data_id` values when replacing regionless records such as `technology`, `commodity`, and `tech_group_member`, preventing unrelated datasets from being removed.
- Does not merge `output_*` tables from the incoming database.
- Clears existing `output_*` tables in the output database because solver results are stale after a model-data swap.
- Performs `data_id`-aware orphan cleanup for selected technologies and commodities.
- Accepts tables whose columns are the same even if the physical column order differs.

The schema definition used for this update is retained at:

```text
assets/schema_4.sql
```

The script does not create a database from that file; it validates and modifies existing schema 4 SQLite databases.

## Installation

1. Clone the repository:

```bash
git clone https://github.com/CANOE-main/CANOE_SWAP.git
cd CANOE_SWAP
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

### Automatic mode

Place the files in the standard folders:

```text
data/source/                 Base schema 4 database
data/input/                  Incoming schema 4 database
data/items_to_be_removed.csv Swap configuration
data/output/                 Generated output database
```

Run:

```bash
python src/main.py
```

The default output is:

```text
data/output/output.sqlite
```

### Command-line mode

```bash
python src/main.py \
  --source "custom_folder/base.sqlite" \
  --input "custom_folder/replacement.sqlite" \
  --output "results/final_model.sqlite" \
  --config "configs/items_to_be_removed.csv"
```

## Arguments

| Argument | Description | Default |
|---|---|---|
| `--source` | Base schema 4 SQLite database | First `.sqlite` in `data/source/` |
| `--input` | Incoming schema 4 SQLite database | First `.sqlite` in `data/input/` |
| `--output` | Final database | `data/output/output.sqlite` |
| `--config` | Swap configuration CSV | `data/items_to_be_removed.csv` |

## Configuration CSV

The existing three-column workflow is retained:

```csv
Technology,Commodity,Region
E_NG_CG-EXS,NG,LH-MC
```

Columns may contain blanks. Duplicate values are automatically removed.

- **Technology** — technology names whose existing sector data should be replaced.
- **Commodity** — commodity names whose existing sector data should be replaced.
- **Region** — regions to remove from the base database and import from the incoming database.

## How the schema 4 swap works

1. Validate the base database and incoming database as schema 4.
2. Copy the base database to the output path if the output does not already exist.
3. Clear stale `output_*` result tables.
4. Read the technology, commodity, and region lists from the configuration CSV.
5. Detect the `data_id` values represented by the incoming database.
6. Remove matching rows from schema 4 input tables:
   - Region-bearing tables are restricted to the requested regions.
   - Regionless `data_id` tables are restricted to incoming `data_id` values.
7. Merge all compatible non-output tables from the incoming database.
8. Remove selected technology/commodity rows that are no longer referenced for their specific `data_id`.

## Important note about existing output files

For compatibility with the original CANOE_SWAP workflow, if the requested output file already exists, the script modifies that file instead of copying the source again.

For a completely fresh run, delete the old output first:

```bash
rm data/output/output.sqlite
```

On Windows PowerShell:

```powershell
Remove-Item data/output/output.sqlite
```
