import argparse
import logging
import os
import shutil
import sqlite3
import sys
from typing import Iterable, List, Sequence, Set

import pandas as pd


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

SCHEMA_MAJOR = 4
OUTPUT_PREFIX = "output_"

# Schema 4 uses these fields to reference technologies/commodities.  The
# fallback names are intentionally kept here even when a FK is missing from a
# particular table in the schema (for example limit_tech_input_split_annual).
TECH_REFERENCE_COLUMNS = {"tech", "primary_tech", "driven_tech"}
COMMODITY_REFERENCE_COLUMNS = {
    "name",          # commodity.name
    "commodity",     # demand.commodity
    "input_comm",
    "output_comm",
    "emis_comm",
    "demand_name",   # demand_specific_distribution.demand_name
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# SQLite helpers
# -----------------------------------------------------------------------------

def quote_identifier(name: str) -> str:
    """Safely quote a SQLite identifier."""
    return '"' + name.replace('"', '""') + '"'


def get_tables(conn: sqlite3.Connection) -> List[str]:
    """Return user table names, excluding SQLite internal tables."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def get_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    """Return the ordered column names for a table."""
    safe = quote_identifier(table_name)
    return [row[1] for row in conn.execute(f"PRAGMA table_info({safe})")]


def find_first_file(directory: str, extension: str) -> str | None:
    """Find the first file with a given extension in a directory."""
    if not os.path.exists(directory):
        return None
    files = sorted(f for f in os.listdir(directory) if f.endswith(extension))
    return os.path.join(directory, files[0]) if files else None


def dedupe(values: Iterable[str]) -> List[str]:
    """Drop blanks/duplicates while retaining the original order."""
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if pd.isna(value):
            continue
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def get_schema_major(conn: sqlite3.Connection) -> int | None:
    """Read DB_MAJOR from schema metadata, if available."""
    if not table_exists(conn, "metadata"):
        return None
    row = conn.execute(
        "SELECT value FROM metadata WHERE element = 'DB_MAJOR'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def validate_schema4(path: str, role: str) -> None:
    """Fail early when a source/input DB is not CANOE schema 4."""
    conn = sqlite3.connect(path)
    try:
        major = get_schema_major(conn)
        required = {"data_set", "technology", "commodity", "efficiency"}
        present = set(get_tables(conn))

        if major != SCHEMA_MAJOR:
            raise RuntimeError(
                f"{role} database '{path}' is not schema 4 "
                f"(DB_MAJOR={major!r})."
            )

        missing = sorted(required - present)
        if missing:
            raise RuntimeError(
                f"{role} database '{path}' is missing required schema 4 "
                f"tables: {', '.join(missing)}"
            )
    finally:
        conn.close()


def get_input_data_ids(path: str | None) -> List[str]:
    """
    Return data_ids represented by an input database.

    data_set is authoritative when populated.  If it is empty, fall back to
    scanning data_id columns so regionless schema-4 records can still be
    replaced safely.
    """
    if not path or not os.path.exists(path):
        return []

    conn = sqlite3.connect(path)
    try:
        ids: List[str] = []
        if table_exists(conn, "data_set"):
            ids = dedupe(
                row[0]
                for row in conn.execute(
                    "SELECT data_id FROM data_set WHERE data_id IS NOT NULL"
                )
            )
        if ids:
            return ids

        discovered: List[str] = []
        for table in get_tables(conn):
            if table.startswith(OUTPUT_PREFIX):
                continue
            cols = get_columns(conn, table)
            if "data_id" not in cols:
                continue
            qtable = quote_identifier(table)
            discovered.extend(
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT data_id FROM {qtable} "
                    "WHERE data_id IS NOT NULL"
                )
            )
        return dedupe(discovered)
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Schema-4 swap logic
# -----------------------------------------------------------------------------

def clear_output_tables(conn: sqlite3.Connection) -> None:
    """Remove stale solver outputs from the copied base database."""
    for table in get_tables(conn):
        if not table.startswith(OUTPUT_PREFIX):
            continue
        conn.execute(f"DELETE FROM {quote_identifier(table)}")
        logger.info("Cleared stale output table '%s'.", table)
    conn.commit()


def build_match_clause(
    columns: Sequence[str],
    values: Sequence[str],
) -> tuple[str, List[str]]:
    """Build `(col IN (...) OR col2 IN (...))` and its parameter list."""
    if not columns or not values:
        return "", []

    ph = placeholders(len(values))
    pieces = [f"{quote_identifier(col)} IN ({ph})" for col in columns]
    params: List[str] = []
    for _ in columns:
        params.extend(values)
    return "(" + " OR ".join(pieces) + ")", params


def delete_selected_rows(
    conn: sqlite3.Connection,
    tech_list: Sequence[str],
    comm_list: Sequence[str],
    region_list: Sequence[str],
    input_data_ids: Sequence[str],
) -> None:
    """
    Delete rows being replaced across all schema-4 input tables.

    Rules:
      * output_* tables are not part of a swap;
      * region-bearing tables are scoped to the requested regions;
      * regionless data_id tables are scoped to data_ids supplied by the
        incoming database, preventing unrelated datasets from being removed;
      * global label/enum tables without region/data_id are left alone during a
        region-scoped swap.
    """
    total_deleted = 0

    for table in get_tables(conn):
        if table.startswith(OUTPUT_PREFIX):
            continue

        cols = get_columns(conn, table)
        colset = set(cols)

        tech_cols = sorted(TECH_REFERENCE_COLUMNS & colset)
        comm_cols = sorted(COMMODITY_REFERENCE_COLUMNS & colset)

        tech_clause, tech_params = build_match_clause(tech_cols, tech_list)
        comm_clause, comm_params = build_match_clause(comm_cols, comm_list)
        match_clauses = [c for c in (tech_clause, comm_clause) if c]
        if not match_clauses:
            continue

        where = "(" + " OR ".join(match_clauses) + ")"
        params: List[str] = tech_params + comm_params

        if region_list and "region" in colset:
            where += f" AND {quote_identifier('region')} IN ({placeholders(len(region_list))})"
            params.extend(region_list)
        elif region_list:
            # Regionless schema-4 records (e.g. technology, commodity,
            # tech_group_member) are shared. Replace only records belonging to
            # datasets represented by the incoming DB.
            if "data_id" in colset and input_data_ids:
                where += f" AND {quote_identifier('data_id')} IN ({placeholders(len(input_data_ids))})"
                params.extend(input_data_ids)
            else:
                continue

        sql = f"DELETE FROM {quote_identifier(table)} WHERE {where}"
        cur = conn.execute(sql, params)
        if cur.rowcount and cur.rowcount > 0:
            total_deleted += cur.rowcount
            logger.info("Table '%s': deleted %d rows.", table, cur.rowcount)

    conn.commit()
    logger.info("Deletion phase complete: %d rows removed.", total_deleted)


def merge_db_into_target(
    conn_target: sqlite3.Connection,
    src_path: str,
    batch_size: int = 1000,
    allowed_regions: Sequence[str] | None = None,
) -> None:
    """
    Merge schema-4 input tables into the copied target database.

    output_* tables are intentionally ignored.  Column order may differ
    between otherwise-compatible databases; data are selected using the
    target's column order.
    """
    allowed_regions = dedupe(allowed_regions or [])
    logger.info("Merging data from: %s", src_path)
    if allowed_regions:
        logger.info("Allowed regions for merge: %s", allowed_regions)

    conn_src = sqlite3.connect(src_path, timeout=30.0)
    cur_src = conn_src.cursor()
    cur_tgt = conn_target.cursor()

    try:
        for table in get_tables(conn_src):
            if table.startswith(OUTPUT_PREFIX):
                logger.info("Skipping output table '%s'.", table)
                continue

            if not table_exists(conn_target, table):
                logger.warning("Skipping table '%s' (not found in target DB).", table)
                continue

            src_cols = get_columns(conn_src, table)
            tgt_cols = get_columns(conn_target, table)

            if set(src_cols) != set(tgt_cols):
                logger.warning(
                    "Skipping table '%s' due to column mismatch. source=%s target=%s",
                    table,
                    src_cols,
                    tgt_cols,
                )
                continue

            # Select/insert in target order so order-only differences are safe.
            ordered_cols = tgt_cols
            col_list = ", ".join(quote_identifier(c) for c in ordered_cols)
            values = ", ".join("?" for _ in ordered_cols)
            qtable = quote_identifier(table)

            insert_sql = (
                f"INSERT OR IGNORE INTO {qtable} ({col_list}) VALUES ({values})"
            )
            select_sql = f"SELECT {col_list} FROM {qtable}"
            params: List[str] = []

            if allowed_regions and "region" in ordered_cols:
                select_sql += (
                    f" WHERE {quote_identifier('region')} "
                    f"IN ({placeholders(len(allowed_regions))})"
                )
                params.extend(allowed_regions)

            cur_src.execute(select_sql, params)
            processed = 0
            inserted_before = conn_target.total_changes

            while True:
                rows = cur_src.fetchmany(batch_size)
                if not rows:
                    break
                cur_tgt.executemany(insert_sql, rows)
                processed += len(rows)

            inserted = conn_target.total_changes - inserted_before
            logger.info(
                "Table '%s': processed %d rows, inserted %d rows.",
                table,
                processed,
                inserted,
            )

        conn_target.commit()
    finally:
        conn_src.close()


def row_is_referenced(
    conn: sqlite3.Connection,
    entity: str,
    value: str,
    data_id: str,
) -> bool:
    """Check schema-4 data tables for a data_id-aware entity reference."""
    if entity == "tech":
        reference_columns = TECH_REFERENCE_COLUMNS
        skip_tables = {"technology", "technology_label"}
    elif entity == "commodity":
        reference_columns = COMMODITY_REFERENCE_COLUMNS
        skip_tables = {"commodity", "commodity_label"}
    else:
        raise ValueError(f"Unsupported entity type: {entity}")

    for table in get_tables(conn):
        if table in skip_tables or table.startswith(OUTPUT_PREFIX):
            continue

        cols = set(get_columns(conn, table))
        if "data_id" not in cols:
            continue

        candidate_cols = sorted(reference_columns & cols)
        if not candidate_cols:
            continue

        match_clause, match_params = build_match_clause(candidate_cols, [value])
        sql = (
            f"SELECT 1 FROM {quote_identifier(table)} "
            f"WHERE {quote_identifier('data_id')} = ? AND {match_clause} LIMIT 1"
        )
        if conn.execute(sql, [data_id] + match_params).fetchone():
            return True

    return False


def cleanup_orphans(
    conn: sqlite3.Connection,
    tech_list: Sequence[str],
    comm_list: Sequence[str],
) -> None:
    """Remove unreferenced schema-4 technology/commodity rows by data_id."""
    removed_tech = 0
    removed_comm = 0

    if tech_list and table_exists(conn, "technology"):
        ph = placeholders(len(tech_list))
        candidates = conn.execute(
            f"SELECT tech, data_id FROM technology WHERE tech IN ({ph})",
            list(tech_list),
        ).fetchall()
        for tech, data_id in candidates:
            if data_id is None:
                continue
            if not row_is_referenced(conn, "tech", tech, data_id):
                cur = conn.execute(
                    "DELETE FROM technology WHERE tech = ? AND data_id = ?",
                    (tech, data_id),
                )
                removed_tech += max(cur.rowcount, 0)

    if comm_list and table_exists(conn, "commodity"):
        ph = placeholders(len(comm_list))
        candidates = conn.execute(
            f"SELECT name, data_id FROM commodity WHERE name IN ({ph})",
            list(comm_list),
        ).fetchall()
        for commodity, data_id in candidates:
            if data_id is None:
                continue
            if not row_is_referenced(conn, "commodity", commodity, data_id):
                cur = conn.execute(
                    "DELETE FROM commodity WHERE name = ? AND data_id = ?",
                    (commodity, data_id),
                )
                removed_comm += max(cur.rowcount, 0)

    conn.commit()
    logger.info(
        "Orphan cleanup complete: %d technology rows, %d commodity rows removed.",
        removed_tech,
        removed_comm,
    )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="CANOE SWAP: schema-4 database merge & cleanup tool"
    )
    parser.add_argument("--source", help="Path to the base schema-4 SQLite file.")
    parser.add_argument("--input", help="Path to the incoming schema-4 SQLite file.")
    parser.add_argument("--output", help="Path for the final SQLite file.")
    parser.add_argument("--config", help="Path to the swap configuration CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()

    source_path = args.source or find_first_file("data/source", ".sqlite")
    if not source_path:
        logger.error("No source database found in 'data/source/' and no --source provided.")
        sys.exit(1)

    input_path = args.input or find_first_file("data/input", ".sqlite")

    output_path = args.output or "data/output/output.sqlite"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    config_path = args.config or "data/items_to_be_removed.csv"
    if not os.path.exists(config_path):
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)

    try:
        validate_schema4(source_path, "Source")
        if input_path and os.path.exists(input_path):
            validate_schema4(input_path, "Input")
    except Exception as exc:
        logger.error("Schema validation failed: %s", exc)
        sys.exit(1)

    logger.info("Configuration set:")
    logger.info("  Source: %s", source_path)
    logger.info("  Input:  %s", input_path if input_path else "(None - skipping merge)")
    logger.info("  Output: %s", output_path)
    logger.info("  Config: %s", config_path)

    # Preserve the original behaviour: if output exists, continue modifying it;
    # otherwise start from a copy of source.
    if os.path.exists(output_path):
        logger.info("Modifying existing output file: %s", output_path)
    else:
        logger.info("Creating new output file from source...")
        try:
            shutil.copy2(source_path, output_path)
        except Exception as exc:
            logger.error("Failed to copy source to output: %s", exc)
            sys.exit(1)

    try:
        items_df = pd.read_csv(config_path)
        tech_list = dedupe(items_df["Technology"].tolist()) if "Technology" in items_df else []
        comm_list = dedupe(items_df["Commodity"].tolist()) if "Commodity" in items_df else []
        region_list = dedupe(items_df["Region"].tolist()) if "Region" in items_df else []
    except Exception as exc:
        logger.error("Error reading CSV: %s", exc)
        sys.exit(1)

    logger.info(
        "Loaded config: %d techs, %d commodities, %d regions.",
        len(tech_list),
        len(comm_list),
        len(region_list),
    )

    input_data_ids = get_input_data_ids(input_path)
    if input_data_ids:
        logger.info("Incoming data_ids: %s", input_data_ids)

    conn = sqlite3.connect(output_path, timeout=30.0)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")

        clear_output_tables(conn)

        logger.info("Starting schema-4 deletion phase...")
        delete_selected_rows(
            conn,
            tech_list=tech_list,
            comm_list=comm_list,
            region_list=region_list,
            input_data_ids=input_data_ids,
        )

        if input_path and os.path.exists(input_path):
            merge_db_into_target(
                conn,
                input_path,
                allowed_regions=region_list,
            )
            logger.info("Merge complete.")
        else:
            logger.warning("No input file provided/found. Skipping merge.")

        logger.info("Starting data_id-aware orphan cleanup...")
        cleanup_orphans(conn, tech_list, comm_list)

    except Exception:
        conn.rollback()
        logger.exception("CANOE SWAP failed. Changes since the last commit were rolled back.")
        raise
    finally:
        conn.close()

    logger.info("Process finished successfully.")


if __name__ == "__main__":
    main()
