import sqlite3

DB_FILE = "training_log.db"


def create_table():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    distance_km REAL NOT NULL,
                    duration_min REAL NOT NULL,
                    notes TEXT
                )
            """)
    except sqlite3.Error as e:
        print(f"Could not create table: {e}")


def add_run(date, distance_km, duration_min, notes):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO runs (date, distance_km, duration_min, notes) VALUES (?, ?, ?, ?)",
                (date, distance_km, duration_min, notes),
            )
    except sqlite3.Error as e:
        print(f"Could not add run: {e}")


def show_summary():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute(
                "SELECT date, distance_km, duration_min, notes FROM runs ORDER BY date"
            ).fetchall()

        print("Training log:")
        if not rows:
            print("  No runs logged yet.")
            return

        total_distance = 0
        for date, distance_km, duration_min, notes in rows:
            print(f"  {date} - {distance_km} km in {duration_min} min - {notes}")
            total_distance += distance_km

        print(f"\nTotal distance: {total_distance} km")
    except sqlite3.Error as e:
        print(f"Could not read training log: {e}")


if __name__ == "__main__":
    create_table()

    add_run("2026-08-20", 8.5, 48, "Easy morning run")
    add_run("2026-08-25", 12.0, 65, "Long run, felt strong")
    add_run("2026-09-01", 6.2, 32, "Recovery run after work")

    show_summary()
