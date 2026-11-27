"""SQLite 연결과 스키마."""
import os
import sqlite3
import threading
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DRUG_ORDER_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "pharmacy.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS wholesalers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  header TEXT, line_fmt TEXT, footer TEXT,
  ledger_format TEXT DEFAULT '',
  sort INTEGER DEFAULT 0,
  billing_day INTEGER,           -- 결제 마감일(매월 n일), 비우면 말일
  discount_rate REAL DEFAULT 0,  -- 결제 할인율(%)
  discount_on INTEGER DEFAULT 0, -- 할인 적용 여부
  biz_no TEXT                    -- 도매상 사업자번호(매출원장 자동 인식용)
);

CREATE TABLE IF NOT EXISTS staff (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS catalog (
  code TEXT PRIMARY KEY, name TEXT, maker TEXT, claim_code TEXT, ingr_code TEXT,
  insured INTEGER, price REAL, inout TEXT, used INTEGER
);

CREATE TABLE IF NOT EXISTS drugs (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  order_name TEXT,
  maker TEXT DEFAULT '',
  claim_code TEXT, ingr_code TEXT,
  insured INTEGER NOT NULL DEFAULT 1,
  price REAL DEFAULT 0,          -- 급여 1정(단위) 약가
  buy_price REAL,                -- 비급여 1통 매입가
  pack INTEGER DEFAULT 0,        -- 1통에 몇 정
  pack_label TEXT DEFAULT '',
  pack_src TEXT DEFAULT '',      -- ledger | manual | ''
  unit TEXT DEFAULT '정',
  psy INTEGER DEFAULT 0,
  target REAL,                   -- 목표 재고(정)
  pref_ws INTEGER,
  base_qty REAL, base_date TEXT, -- 기준 재고(그 날짜 마감 기준)
  is_new INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  alias_of TEXT,                 -- 같은 제품의 다른 eghis 코드면 대표 코드
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start TEXT NOT NULL, end TEXT NOT NULL, filename TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (
  import_id INTEGER NOT NULL, code TEXT NOT NULL, qty REAL, price REAL, amount REAL, patients INTEGER,
  PRIMARY KEY (import_id, code)
);

CREATE TABLE IF NOT EXISTS cart (
  code TEXT PRIMARY KEY, ws_id INTEGER NOT NULL, packs INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ws_id INTEGER NOT NULL, order_date TEXT NOT NULL, created_at TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL, code TEXT NOT NULL,
  packs INTEGER NOT NULL, pack INTEGER NOT NULL,
  checked INTEGER NOT NULL DEFAULT 0,
  cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER, code TEXT NOT NULL, ws_id INTEGER,
  packs INTEGER NOT NULL, pack INTEGER NOT NULL,
  date TEXT NOT NULL, staff TEXT, note TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS counts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, date TEXT, qty REAL, created_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ws_id INTEGER, period_start TEXT, period_end TEXT, filename TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id INTEGER, date TEXT, name TEXT, spec TEXT, qty REAL, price REAL, amount REAL,
  is_disc INTEGER DEFAULT 0, code TEXT
);
CREATE TABLE IF NOT EXISTS import_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source TEXT, filename TEXT, ok INTEGER, message TEXT
);
CREATE TABLE IF NOT EXISTS name_links (
  ws_id INTEGER, ledger_name TEXT, code TEXT, PRIMARY KEY (ws_id, ledger_name)
);
CREATE TABLE IF NOT EXISTS psy_usage (
  code TEXT NOT NULL, date TEXT NOT NULL, qty REAL NOT NULL, staff TEXT, created_at TEXT,
  PRIMARY KEY (code, date)
);
-- 직원 연차: 하루(또는 반나절)마다 한 줄. grp = 한 번에 신청한 묶음
CREATE TABLE IF NOT EXISTS leaves (
  id INTEGER PRIMARY KEY, staff_id INTEGER NOT NULL, date TEXT NOT NULL, kind TEXT DEFAULT 'full',
  days REAL DEFAULT 1, status TEXT DEFAULT 'wait', reason TEXT, grp INTEGER, created_at TEXT, decided_at TEXT
);
-- 비품 (간식·사무용품·위생용품 등 약이 아닌 물건)
CREATE TABLE IF NOT EXISTS supply_cats (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS supplies (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, cat TEXT, unit TEXT DEFAULT '개',
  qty REAL DEFAULT 0, min_qty REAL DEFAULT 0, price REAL, mall TEXT, links TEXT DEFAULT '[]',
  memo TEXT, active INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS supply_log (
  id INTEGER PRIMARY KEY, supply_id INTEGER, at TEXT, kind TEXT, delta REAL, qty_after REAL,
  who TEXT, note TEXT, undone INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS supply_reqs (
  id INTEGER PRIMARY KEY, supply_id INTEGER, qty REAL NOT NULL, status TEXT DEFAULT 'wait',
  who TEXT, price REAL, created_at TEXT, decided_at TEXT, done_at TEXT
);
"""

DEFAULTS = {
    "clinic_name": "",
    "default_days": "21",
    "msg_header": "안녕하세요, {병원이름} 약품 주문 합니다.",
    "msg_line": "{주문명} {통}통",
    "msg_footer": "입니다.\n\n좋은 하루 되세요. 감사합니다.",
    "backup_dir": "",
    "report_pw": "",
    "last_backup": "",
    "prev_commit": "",
    "watch_dir": "",
    "watch_mode": "always",
    "watch_day": "0",
    "watch_time": "09:00",
    "watch_last": "",
    "auto_update": "1",       # 밤에 새 버전이 있으면 자동 업데이트 (설정에서 끌 수 있음)
    "auto_update_day": "",    # 마지막으로 확인한 날 (하루 한 번)
    "auto_update_msg": "",    # 마지막 자동 확인 결과 (설정 화면에 표시)
    "psy_mode": "excel",  # 향정 재고: manual(향정 관리 기록) / excel(처방 통계)
}


def connect():
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return conn


def close():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(drugs)")}
    if "alias_of" not in cols:  # 예전 DB 업그레이드
        conn.execute("ALTER TABLE drugs ADD COLUMN alias_of TEXT")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(staff)")}
    for col, typ in (("leave_base", "TEXT"), ("leave_days", "REAL")):  # 연차 기준일, 1년 연차 일수
        if col not in scols:
            conn.execute(f"ALTER TABLE staff ADD COLUMN {col} {typ}")
    wcols = {r[1] for r in conn.execute("PRAGMA table_info(wholesalers)")}
    for col, typ in (("billing_day", "INTEGER"), ("discount_rate", "REAL DEFAULT 0"), ("discount_on", "INTEGER DEFAULT 0"), ("biz_no", "TEXT")):
        if col not in wcols:
            conn.execute(f"ALTER TABLE wholesalers ADD COLUMN {col} {typ}")
    # 향정 재고 방식이 아직 없으면: 이미 향정 관리에 기록해 온 병원은 수기, 아니면 엑셀
    if not conn.execute("SELECT 1 FROM settings WHERE key='psy_mode'").fetchone():
        had = conn.execute("SELECT 1 FROM psy_usage LIMIT 1").fetchone()
        conn.execute("INSERT INTO settings(key, value) VALUES ('psy_mode', ?)", ("manual" if had else "excel",))
    # 시작 도우미: 새로 설치한 병원만 처음에 자동으로 열림 (이미 쓰던 병원은 도움말에서 열 수 있음)
    if not conn.execute("SELECT 1 FROM settings WHERE key='tour'").fetchone():
        used = conn.execute("SELECT 1 FROM drugs LIMIT 1").fetchone()
        conn.execute("INSERT INTO settings(key, value) VALUES ('tour', ?)", ("done" if used else "open",))
    if not conn.execute("SELECT 1 FROM supply_cats").fetchone():
        for i, n in enumerate(("간식·탕비실", "사무용품", "위생·청소", "진료 소모품")):
            conn.execute("INSERT INTO supply_cats(name, sort) VALUES (?, ?)", (n, i))
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    # 2026-11-21: 처방발생 통계가 등록수가로 잘못 읽혀 비워진 약 목록(catalog) 이름 복구
    if not conn.execute("SELECT 1 FROM settings WHERE key='fix_catalog_1121'").fetchone():
        conn.execute("""UPDATE catalog SET name=(SELECT name FROM drugs WHERE drugs.code=catalog.code),
                          maker=COALESCE(NULLIF(maker,''),(SELECT maker FROM drugs WHERE drugs.code=catalog.code)),
                          price=COALESCE(price,(SELECT price FROM drugs WHERE drugs.code=catalog.code))
                        WHERE COALESCE(name,'')='' AND code IN (SELECT code FROM drugs)""")
        conn.execute("DELETE FROM catalog WHERE COALESCE(name,'')='' AND code NOT IN (SELECT code FROM drugs)")
        conn.execute("INSERT INTO settings(key, value) VALUES ('fix_catalog_1121', '1')")
    conn.commit()


def q(sql, args=()):
    return connect().execute(sql, args).fetchall()


def one(sql, args=()):
    return connect().execute(sql, args).fetchone()


def run(sql, args=()):
    conn = connect()
    cur = conn.execute(sql, args)
    conn.commit()
    return cur


def get_setting(key):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else DEFAULTS.get(key, "")


def set_setting(key, value):
    run("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def today():
    return date.today().isoformat()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
