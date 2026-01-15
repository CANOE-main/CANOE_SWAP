<div align="center">

<img src="./assets/CANOE_SWAP.png" alt="CANOE SWAP Logo" width="300"/>

### Database Merge & Cleanup Utility

</div>

---

## Overview

**CANOE SWAP** is a Python-based utility designed for the CANOE modeling group. It streamlines the process of merging multiple SQLite database fragments into a master database while simultaneously filtering out specific commodities, technologies, and regions based on input criteria.

## Features

- **Flexible Usage:** Run automatically with default file paths or specify custom paths via command line.
- **Smart Merging:** Combines tables from source databases into a target schema.
- **Automated Cleanup:** Identifies and removes orphaned commodities and technologies (e.g., stripping items not used in `Efficiency` or `EmissionActivity`).
- **Configurable Filtering:** Uses a CSV input to strip out unwanted data elements (e.g., outdated technologies) during the merge.
- **Region Filtering:** Merges only data relevant to the regions specified in the configuration file.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/david-turnbull/CANOE_SWAP.git
   cd canoe-swap
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

You can run the tool in **Automatic Mode** (easiest) or **Command Line Mode** (best for automation).

### Method 1: Automatic Mode

Place your files in the standard directory structure, and the script will find them automatically:

- Base model database goes in: `data/source/` (e.g., `data/source/base.sqlite`)
- New data database goes in: `data/input/` (e.g., `data/input/updates.sqlite`)
- Config file should be at: `data/items.csv`

Run the script:

```bash
python src/main.py
```

Output will be written to:

- `data/output/output.sqlite`

### Method 2: Command Line Mode

For full control over input and output paths, use arguments:

```bash
python src/main.py \
  --source "custom_folder/my_model.db" \
  --input "custom_folder/new_data.db" \
  --output "results/final_model.db" \
  --config "configs/removal_list.csv"
```

## Arguments

| Argument   | Description                                   | Default (if not provided)                 |
|-----------|-----------------------------------------------|-------------------------------------------|
| `--source` | Path to the base source SQLite database.       | First `.sqlite` file in `data/source/`    |
| `--input`  | Path to the database to merge in.              | First `.sqlite` file in `data/input/`     |
| `--output` | Path where the final database will be saved.   | `data/output/output.sqlite`               |
| `--config` | Path to the CSV file with removal criteria.    | `data/items.csv`                          |

## Configuration File (`items.csv`)

The configuration CSV must contain the following headers:

- **Technology**: Techs to be removed / checked for orphans  
- **Commodity**: Commodities to be removed / checked for orphans  
- **Region**: The specific regions to filter operations by  

Example structure:

```csv
Technology,Commodity,Region
E_NG_CG-EXS,NG,LH-MC
...
```


