import pandas as pd
import sqlite3
import shutil
import os
import argparse
import logging
import sys
from typing import List

# --- Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def get_tables(conn: sqlite3.Connection) -> List[str]:
    """Return a list of table names (excluding SQLite internal tables)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cur.fetchall()]

def get_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    """Return ordered list of column names for a table."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info('{table_name}')")
    return [row[1] for row in cur.fetchall()]

def find_first_file(directory: str, extension: str) -> str:
    """Helper to find the first file with a specific extension in a folder."""
    if not os.path.exists(directory):
        return None
    files = [f for f in os.listdir(directory) if f.endswith(extension)]
    if files:
        return os.path.join(directory, files[0])
    return None

def merge_db_into_target(conn_target: sqlite3.Connection, src_path: str, batch_size: int = 1000, allowed_regions: List[str] = None):
    """Merge tables from src_path into conn_target with optional region filtering."""
    logger.info(f"Merging data from: {src_path}")
    
    if allowed_regions:
        # Dedupe and maintain order
        allowed_regions = list(dict.fromkeys(allowed_regions))
        logger.info(f"Allowed regions for merge: {allowed_regions}")

    conn_src = sqlite3.connect(src_path, timeout=30.0)
    cur_src = conn_src.cursor()
    cur_tgt = conn_target.cursor()

    try:
        src_tables = get_tables(conn_src)

        for table in src_tables:
            # Check table existence in target
            cur_tgt.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if not cur_tgt.fetchone():
                logger.warning(f"Skipping table '{table}' (not found in target DB).")
                continue

            # Check column compatibility
            src_cols = get_columns(conn_src, table)
            tgt_cols = get_columns(conn_target, table)

            if src_cols != tgt_cols:
                logger.warning(f"Skipping table '{table}' due to column mismatch.")
                continue

            col_list = ", ".join(f'"{c}"' for c in src_cols)
            placeholders = ", ".join("?" for _ in src_cols)

            insert_sql = f'INSERT OR IGNORE INTO "{table}" ({col_list}) VALUES ({placeholders});'
            select_sql = f'SELECT {col_list} FROM "{table}"'
            params = ()

            # Apply Region Filter
            if allowed_regions and "region" in src_cols:
                region_placeholders = ", ".join("?" for _ in allowed_regions)
                select_sql += f' WHERE "region" IN ({region_placeholders})'
                params = tuple(allowed_regions)
                logger.info(f"Table '{table}': restricting to allowed regions.")

            cur_src.execute(select_sql, params)

            total_rows = 0
            while True:
                rows = cur_src.fetchmany(batch_size)
                if not rows:
                    break
                cur_tgt.executemany(insert_sql, rows)
                total_rows += len(rows)

            logger.info(f"Table '{table}': processed {total_rows} rows.")

        conn_target.commit()

    finally:
        conn_src.close()

def parse_arguments():
    parser = argparse.ArgumentParser(description="CANOE SWAP: Database Merge & Cleanup Tool")
    
    # NOTE: required=False allows us to use defaults if the user doesn't provide flags
    parser.add_argument("--source", required=False, help="Path to the base source SQLite file.")
    parser.add_argument("--input", required=False, help="Path to the input SQLite file.")
    parser.add_argument("--output", required=False, help="Path where the final SQLite file will be saved.")
    parser.add_argument("--config", required=False, help="Path to the configuration CSV.")
    
    return parser.parse_args()

# --- Main Logic ---

def main():
    args = parse_arguments()

    # 1. RESOLVE PATHS (CLI Args vs. Defaults)
    
    # Source DB: Use arg if provided, else find first .sqlite in 'data/source'
    source_path = args.source
    if not source_path:
        source_path = find_first_file('data/source', '.sqlite')
        if not source_path:
            logger.error("No source database found in 'data/source/' and no --source arg provided.")
            sys.exit(1)

    # Input DB: Use arg if provided, else find first .sqlite in 'data/input'
    input_path = args.input
    if not input_path:
        input_path = find_first_file('data/input', '.sqlite')
        # It's okay if input is missing, we just skip merge later

    # Output DB: Use arg if provided, else default to 'data/output/output.sqlite'
    output_path = args.output
    if not output_path:
        os.makedirs('data/output', exist_ok=True)
        output_path = 'data/output/output.sqlite'
    else:
        # Ensure directory exists if user provided a custom path
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Config CSV: Use arg if provided, else default to 'data/items.csv'
    config_path = args.config
    if not config_path:
        config_path = 'data/items_to_be_removed.csv'
        if not os.path.exists(config_path):
             logger.error(f"Default config file not found at '{config_path}' and no --config arg provided.")
             sys.exit(1)

    logger.info("Configuration set:")
    logger.info(f"  Source: {source_path}")
    logger.info(f"  Input:  {input_path if input_path else '(None - Skipping Merge)'}")
    logger.info(f"  Output: {output_path}")
    logger.info(f"  Config: {config_path}")

    # 2. Initialize Output File
    if os.path.exists(output_path):
        logger.info(f"Modifying existing output file: {output_path}")
    else:
        logger.info("Creating new output file from source...")
        try:
            shutil.copy2(source_path, output_path)
        except Exception as e:
            logger.error(f"Failed to copy source to output: {e}")
            sys.exit(1)

    # 3. Load CSV Data
    try:
        items_df = pd.read_csv(config_path)
        # Safely extract lists, dropping NaNs
        tech_list = items_df['Technology'].dropna().tolist() if 'Technology' in items_df.columns else []
        comm_list = items_df['Commodity'].dropna().tolist() if 'Commodity' in items_df.columns else []
        
        # Get regions and dedupe
        if 'Region' in items_df.columns:
            region_list = list(dict.fromkeys(items_df['Region'].dropna().tolist()))
        else:
            region_list = []
        
        logger.info(f"Loaded config: {len(tech_list)} techs, {len(comm_list)} comms, {len(region_list)} regions.")
    except Exception as e:
        logger.error(f"Error reading CSV: {e}")
        sys.exit(1)

    # 4. Connect to Output DB
    conn = sqlite3.connect(output_path)
    cur = conn.cursor()
    cur.execute('PRAGMA Foreign_keys = off;')

    target_tables = ['Commodity', 'Demand', 'Efficiency', 'LimitTechInputSplitAnnual', 'Technology']

    # --- PHASE 1: Deletions ---
    logger.info("Starting deletion process based on config...")

    for table in target_tables:
        # Delete Technologies
        for t in tech_list:
            for r in region_list:
                try:
                    cur.execute(f"DELETE FROM {table} WHERE tech = ? AND region = ?", (t, r))
                except sqlite3.OperationalError:
                    pass # Table might not have tech/region columns
                except Exception as e:
                    logger.warning(f"Error deleting in {table}: {e}")
                    
        # Delete Commodities
        for c in comm_list:
            for r in region_list:
                try:
                    cur.execute(f"DELETE FROM {table} WHERE commodity = ? AND region = ?", (c, r))
                except sqlite3.OperationalError:
                    pass
                except Exception as e:
                    logger.warning(f"Error deleting in {table}: {e}")

    conn.commit()
    logger.info("Deletions committed.")

    # --- PHASE 2: Merge with Input ---
    if input_path and os.path.exists(input_path):
        merge_db_into_target(conn, input_path, allowed_regions=region_list)
        logger.info("Merge complete.")
    else:
        logger.warning("No input file provided or file not found. Skipping merge.")

    # --- PHASE 3: Post-processing (Orphan Cleanup) ---
    logger.info("Starting post-processing cleanup...")

    # Cleanup Technologies
    if tech_list:
        placeholders = ', '.join(['?'] * len(tech_list))
        delete_tech_sql = f"""
            DELETE FROM Technology
            WHERE tech NOT IN (
                SELECT DISTINCT tech FROM Efficiency WHERE tech IS NOT NULL
            )
            AND tech IN ({placeholders});
        """
        try:
            cur.execute(delete_tech_sql, tech_list)
            if cur.rowcount > 0:
                logger.info(f"Removed {cur.rowcount} orphan technologies.")
            else:
                logger.info("No orphan technologies found.")
        except sqlite3.OperationalError as e:
            logger.warning(f"Skipping Technology cleanup: {e}")

    # Cleanup Commodities
    if comm_list:
        placeholders = ', '.join(['?'] * len(comm_list))
        delete_comm_sql = f"""
            DELETE FROM Commodity
            WHERE name NOT IN (
                SELECT input_comm FROM Efficiency
                UNION
                SELECT output_comm FROM Efficiency
                UNION
                SELECT emis_comm from EmissionActivity
            )
            AND name IN ({placeholders});
        """
        try:
            cur.execute(delete_comm_sql, comm_list)
            if cur.rowcount > 0:
                logger.info(f"Removed {cur.rowcount} orphan commodities.")
            else:
                logger.info("No orphan commodities found.")
        except sqlite3.OperationalError as e:
            logger.warning(f"Skipping Commodity cleanup: {e}")

    conn.commit()
    conn.close()
    logger.info("Process finished successfully.")

if __name__ == "__main__":

    main()
