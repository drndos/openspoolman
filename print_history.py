import os
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB_NAME = "3d_printer_logs.db"
DB_ENV_VAR = "OPENSPOOLMAN_PRINT_HISTORY_DB"


def _default_db_path() -> Path:
    """Resolve the print history database path, allowing an env override."""

    env_path = os.getenv(DB_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser().resolve()

    return Path(__file__).resolve().parent / "data" / DEFAULT_DB_NAME


db_config = {"db_path": str(_default_db_path())}  # Configuration for database location


def create_database() -> None:
    """
    Create the SQLite database schema if it does not exist.
    """
    db_path = Path(db_config["db_path"])
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS prints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                print_date TEXT NOT NULL,
                file_name TEXT NOT NULL,
                print_type TEXT NOT NULL,
                image_file TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS filament_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                print_id INTEGER NOT NULL,
                spool_id INTEGER,
                filament_type TEXT NOT NULL,
                color TEXT NOT NULL,
                grams_used REAL NOT NULL,
                ams_slot INTEGER NOT NULL,
                FOREIGN KEY (print_id) REFERENCES prints (id) ON DELETE CASCADE
            )
        ''')

        conn.commit()
        conn.close()


def insert_print(file_name: str, print_type: str, image_file: str = None, print_date: str = None) -> int:
    """
    Inserts a new print job into the database and returns the print ID.
    If no print_date is provided, the current timestamp is used.
    """
    if print_date is None:
        print_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO prints (print_date, file_name, print_type, image_file)
        VALUES (?, ?, ?, ?)
    ''', (print_date, file_name, print_type, image_file))
    print_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return print_id

def insert_filament_usage(print_id: int, filament_type: str, color: str, grams_used: float, ams_slot: int) -> None:
    """
    Inserts a new filament usage entry for a specific print job.
    """
    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO filament_usage (print_id, filament_type, color, grams_used, ams_slot)
        VALUES (?, ?, ?, ?, ?)
    ''', (print_id, filament_type, color, grams_used, ams_slot))
    conn.commit()
    conn.close()

def update_filament_spool(print_id: int, filament_id: int, spool_id: int) -> None:
    """
    Updates the spool_id for a given filament usage entry, ensuring it belongs to the specified print job.
    """
    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE filament_usage
        SET spool_id = ?
        WHERE ams_slot = ? AND print_id = ?
    ''', (spool_id, filament_id, print_id))
    conn.commit()
    conn.close()

def update_filament_grams_used(print_id: int, filament_id: int, grams_used: float) -> None:
    """
    Updates the grams_used for a given filament usage entry, ensuring it belongs to the specified print job.
    """
    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE filament_usage
        SET grams_used = ?
        WHERE ams_slot = ? AND print_id = ?
    ''', (grams_used, filament_id, print_id))
    conn.commit()
    conn.close()


def get_prints_with_filament(limit: int | None = None, offset: int | None = None):
    """
    Retrieves print jobs along with their associated filament usage, grouped by print job.

    A total count is returned to support pagination.
    """
    conn = sqlite3.connect(db_config["db_path"])
    conn.row_factory = sqlite3.Row  # Enable column name access

    count_cursor = conn.cursor()
    count_cursor.execute("SELECT COUNT(*) FROM prints")
    total_count = count_cursor.fetchone()[0]

    cursor = conn.cursor()
    query = '''
        SELECT p.id AS id, p.print_date AS print_date, p.file_name AS file_name,
               p.print_type AS print_type, p.image_file AS image_file,
               (
                   SELECT json_group_array(json_object(
                       'spool_id', f.spool_id,
                       'filament_type', f.filament_type,
                       'color', f.color,
                       'grams_used', f.grams_used,
                       'ams_slot', f.ams_slot
                   )) FROM filament_usage f WHERE f.print_id = p.id
               ) AS filament_info
        FROM prints p
        ORDER BY p.print_date DESC
    '''
    params: list[int] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
        if offset is not None:
            query += " OFFSET ?"
            params.append(offset)

    cursor.execute(query, params)
    prints = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return prints, total_count

def get_prints_by_spool(spool_id: int):
    """
    Retrieves all print jobs that used a specific spool.
    """
    conn = sqlite3.connect(db_config["db_path"])
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT p.* FROM prints p
        JOIN filament_usage f ON p.id = f.print_id
        WHERE f.spool_id = ?
    ''', (spool_id,))
    prints = cursor.fetchall()
    conn.close()
    return prints

def get_filament_for_slot(print_id: int, ams_slot: int):
  conn = sqlite3.connect(db_config["db_path"])
  conn.row_factory = sqlite3.Row  # Enable column name access
  cursor = conn.cursor()

  cursor.execute('''
      SELECT * FROM filament_usage
      WHERE print_id = ? AND ams_slot = ?
  ''', (print_id, ams_slot))

  results = cursor.fetchone()
  conn.close()
  return results

def get_all_filament_usage_for_print(print_id: int):
  """
  Retrieves all filament usage entries for a specific print.
  Returns a dict mapping ams_slot to grams_used.
  """
  conn = sqlite3.connect(db_config["db_path"])
  conn.row_factory = sqlite3.Row
  cursor = conn.cursor()

  cursor.execute('''
      SELECT ams_slot, grams_used FROM filament_usage
      WHERE print_id = ?
  ''', (print_id,))

  results = {row["ams_slot"]: row["grams_used"] for row in cursor.fetchall()}
  conn.close()
  return results

# Example for creating the database if it does not exist
create_database()
