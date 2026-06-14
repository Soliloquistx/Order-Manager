import sqlite3
from contextlib import contextmanager
from datetime import datetime
from config import DB_PATH


def utc_now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_cursor():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_cursor() as conn:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'operator',
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_no TEXT NOT NULL UNIQUE,
              external_order_no TEXT,
              product_name TEXT NOT NULL,
              route_name TEXT,
              channel TEXT NOT NULL,
              source_platform TEXT,
              customer_name TEXT NOT NULL,
              customer_phone TEXT,
              backup_contact TEXT,
              customer_note TEXT,
              departure_date TEXT NOT NULL,
              return_date TEXT,
              adult_count INTEGER NOT NULL DEFAULT 1,
              child_count INTEGER NOT NULL DEFAULT 0,
              room_count INTEGER,
              total_amount REAL,
              paid_amount REAL NOT NULL DEFAULT 0,
              currency TEXT NOT NULL DEFAULT 'CNY',
              payment_status TEXT NOT NULL,
              order_status TEXT NOT NULL,
              follow_status TEXT NOT NULL,
              priority TEXT NOT NULL DEFAULT '普通',
              owner_id INTEGER,
              next_follow_up_at TEXT,
              last_follow_up_at TEXT,
              latest_note_summary TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              is_archived INTEGER NOT NULL DEFAULT 0,
              archived_at TEXT,
              FOREIGN KEY (owner_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS order_notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER NOT NULL,
              note_type TEXT NOT NULL,
              content TEXT NOT NULL,
              follow_status_after TEXT,
              next_follow_up_at TEXT,
              created_by INTEGER,
              created_at TEXT NOT NULL,
              FOREIGN KEY (order_id) REFERENCES orders(id),
              FOREIGN KEY (created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS order_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER NOT NULL,
              action TEXT NOT NULL,
              field_name TEXT,
              old_value TEXT,
              new_value TEXT,
              description TEXT,
              created_by INTEGER,
              created_at TEXT NOT NULL,
              FOREIGN KEY (order_id) REFERENCES orders(id),
              FOREIGN KEY (created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS order_travellers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              phone TEXT,
              id_type TEXT,
              id_no TEXT,
              gender TEXT,
              birth_date TEXT,
              age INTEGER NOT NULL DEFAULT 0,
              person_type TEXT NOT NULL DEFAULT '成人',
              native_place TEXT,
              note TEXT,
              encrypted_info_revealed INTEGER NOT NULL DEFAULT 0,
              from_vbk_detail INTEGER NOT NULL DEFAULT 0,
              sort_index INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS order_pickup_dropoff (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER NOT NULL,
              action TEXT NOT NULL,
              date TEXT,
              location TEXT,
              flight_no TEXT,
              time_text TEXT,
              description TEXT,
              vehicle_company TEXT,
              driver_name TEXT,
              project_name TEXT,
              enabled INTEGER NOT NULL DEFAULT 1,
              sort_index INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS vbk_detail_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_no TEXT NOT NULL UNIQUE,
              order_type_text TEXT,
              confirm_status_text TEXT,
              payment_status_text TEXT,
              departure_date TEXT,
              return_date TEXT,
              departure_city TEXT,
              customer_name TEXT,
              customer_phone TEXT,
              distribution_channel TEXT,
              scenic_booking_no TEXT,
              reservation_scenic_name TEXT,
              merchant_note TEXT,
              raw_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_departure_date ON orders(departure_date);
            CREATE INDEX IF NOT EXISTS idx_orders_owner_id ON orders(owner_id);
            CREATE INDEX IF NOT EXISTS idx_orders_order_status ON orders(order_status);
            CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status);
            CREATE INDEX IF NOT EXISTS idx_orders_follow_status ON orders(follow_status);
            CREATE INDEX IF NOT EXISTS idx_orders_updated_at ON orders(updated_at);
            CREATE INDEX IF NOT EXISTS idx_order_notes_order_id ON order_notes(order_id);
            CREATE INDEX IF NOT EXISTS idx_order_notes_created_at ON order_notes(created_at);
            CREATE INDEX IF NOT EXISTS idx_order_logs_order_id ON order_logs(order_id);
            CREATE INDEX IF NOT EXISTS idx_order_logs_created_at ON order_logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_order_travellers_order_id ON order_travellers(order_id);
            CREATE INDEX IF NOT EXISTS idx_order_travellers_sort_index ON order_travellers(order_id, sort_index);
            CREATE INDEX IF NOT EXISTS idx_order_pickup_dropoff_order_id ON order_pickup_dropoff(order_id);
            CREATE INDEX IF NOT EXISTS idx_order_pickup_dropoff_sort_index ON order_pickup_dropoff(order_id, sort_index);
            CREATE INDEX IF NOT EXISTS idx_vbk_detail_snapshots_order_no ON vbk_detail_snapshots(order_no);
            '''
        )

        existing = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if existing == 0:
            conn.execute(
                "INSERT INTO users (username, display_name, role, created_at) VALUES (?, ?, ?, ?)",
                ("feiyu", "飞鱼", "admin", utc_now_str()),
            )
