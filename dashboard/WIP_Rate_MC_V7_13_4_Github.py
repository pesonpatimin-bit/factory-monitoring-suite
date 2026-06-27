import os
import sys
import sqlite3
import pandas as pd
import re
import plotly.express as px
import plotly.io as pio
import logging
import threading
import time
import io
from threading import Thread
from waitress import serve as waitress_serve
from flask import Flask, Response, request, jsonify, send_file
import tkinter as st_gui
from tkinter import messagebox, simpledialog

# --- CONFIG ---
SERVER_IP   = "0.0.0.0"
PORT        = 8501
DB_DIR      = r"D:\CKA30_Database"
DB_EMP      = os.path.join(DB_DIR, "employee.db")
DB_MSG      = os.path.join(DB_DIR, "messages.db")
DB_MC       = os.path.join(DB_DIR, "machine_problems.db")
DB_MAT      = os.path.join(DB_DIR, "materials.db")
DB_EXAM     = os.path.join(DB_DIR, "exams.db")
DB_SETTINGS = os.path.join(DB_DIR, "settings.db")
DB_UPLOAD   = os.path.join(DB_DIR, "upload_paths.db")
TRAINING_VIDEO_DIR = r"D:\A30-Monitoring\Employee Training"
PHOTO_DIR   = r"D:\A30-Monitoring\Employee Picture"
EXIT_PASSWORD = "p@ssword"
MSG_ADD_DEL_PASSWORD = "999"
DISPLAY_IP  = "10.61.16.1"
CACHE_TTL   = 30
LOG_FILE    = r"D:\CKA30_Database\dashboard.log"

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("dashboard")

# --- HTML CACHE (tab1 only) ---
_cache_lock  = threading.Lock()
_cached_html: str  = ""
_cache_time: float = 0.0

# ----------------------------------------------
#  EMPLOYEE DB HELPERS
# ----------------------------------------------
EMP_COLS = [
    "no", "name", "employee_id", "resigned", "resign_date",
    "resign_reason", "nationality", "position", "card_last4",
    "start_date", "mc_id", "process",
    "train_work_unit", "train_theory", "train_operate", "train_sign_doc"
]
EMP_COL_LABELS = [
    "No.", "ชื่อ", "Employee ID", "ลาออก (Y/N)", "วันลาออก",
    "เหตุผลที่ออก", "สัญชาติ", "Position ตำแหน่ง", "เลขท้ายบัตร",
    "วันเริ่มงาน", "MC/ID", "Process",
    "สอบ Work Unit (วันที่)", "สอบ Theory (วันที่)", "สอบ Operate (วันที่)", "เซ็นเอกสาร (วันที่)"
]
EMP_TRAIN_COLS   = ["train_work_unit", "train_theory", "train_operate", "train_sign_doc"]
EMP_TRAIN_LABELS = ["สอบ Work Unit", "สอบ Theory", "สอบ Operate", "เซ็นเอกสาร"]

def init_emp_db():
    """สร้างตาราง employees ถ้ายังไม่มี และ migrate เพิ่ม columns ที่ขาด"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_EMP, timeout=10)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS employees (
            no           INTEGER,
            name         TEXT,
            employee_id  TEXT,
            resigned     TEXT,
            resign_date  TEXT,
            resign_reason TEXT,
            nationality  TEXT,
            position     TEXT,
            card_last4   TEXT,
            start_date   TEXT,
            mc_id        TEXT,
            process      TEXT,
            train_work_unit  TEXT DEFAULT '',
            train_theory     TEXT DEFAULT '',
            train_operate    TEXT DEFAULT '',
            train_sign_doc   TEXT DEFAULT ''
        )
    """)
    # migrate: เพิ่ม column ที่ขาดสำหรับ DB เก่า
    existing = [row[1] for row in conn.execute("PRAGMA table_info(employees)").fetchall()]
    for col in ["train_work_unit", "train_theory", "train_operate", "train_sign_doc"]:
        if col not in existing:
            conn.execute(f"ALTER TABLE employees ADD COLUMN {col} TEXT DEFAULT ''")
    conn.commit()
    conn.close()
    log.info("Employee DB initialized")


# ----------------------------------------------
#  MESSAGE DB HELPERS
# ----------------------------------------------
def init_msg_db():
    """สร้างตาราง messages และ message_comments ถ้ายังไม่มี"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_MSG, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            author    TEXT NOT NULL DEFAULT '',
            content   TEXT NOT NULL,
            done      INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            done_at    TEXT,
            priority   TEXT DEFAULT ''
        )
    """)
    # migration: author column
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN author TEXT NOT NULL DEFAULT ''")
        conn.commit()
        log.info("Migrated messages table: added author column")
    except sqlite3.OperationalError:
        pass
    # migration V6.3: priority column
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN priority TEXT DEFAULT ''")
        conn.commit()
        log.info("Migrated messages table: added priority column (V6.3)")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            author     TEXT NOT NULL DEFAULT '',
            content    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()
    log.info("Message DB initialized")


def load_employees() -> pd.DataFrame:
    conn = sqlite3.connect(DB_EMP, timeout=10)
    try:
        df = pd.read_sql_query("SELECT * FROM employees", conn)
        # เรียงตาม no แบบตัวเลข ไม่ใช่ string
        df["_no_num"] = pd.to_numeric(df["no"], errors="coerce").fillna(0)
        df = df.sort_values("_no_num").drop(columns=["_no_num"]).reset_index(drop=True)
    except Exception:
        df = pd.DataFrame(columns=EMP_COLS)
    finally:
        conn.close()
    return df


def save_employees(df: pd.DataFrame):
    conn = sqlite3.connect(DB_EMP, timeout=10)
    df.to_sql("employees", conn, if_exists="replace", index=False)
    conn.close()
    log.info(f"Employee DB saved: {len(df)} rows")


# ----------------------------------------------
#  TAB 1: OVERALL (existing logic)
# ----------------------------------------------
TAB_CSS = """
<style>
    /* -- Tab Nav -- */
    .tab-nav {
        display: flex; gap: 0; margin-bottom: 0;
        border-bottom: 3px solid var(--accent);
    }
    .tab-btn {
        padding: 10px 28px; font-size: 14px; font-weight: 700;
        cursor: pointer; border: none; background: var(--surface2);
        color: var(--text-dim); border-radius: 8px 8px 0 0;
        margin-right: 4px; letter-spacing: .3px;
        border: 1px solid var(--border); border-bottom: none;
        transition: background .15s, color .15s;
    }
    .tab-btn.active {
        background: var(--accent); color: #fff;
        border-color: var(--accent);
    }
    .tab-btn:hover:not(.active) { background: #d8e6f7; color: var(--accent); }
    .tab-panel { display: none; padding-top: 20px; }
    .tab-panel.active { display: block; }

    /* =========================================
       MESSAGE TAB — clean redesign
       ========================================= */

    .msg-board { max-width: 780px; margin: 0 auto; }

    /* -- Compose box -- */
    .msg-compose {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 18px 20px 14px;
        margin-bottom: 20px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.06);
    }
    .msg-compose-label {
        font-size: 11px; font-weight: 700; letter-spacing: .6px;
        color: var(--text-dim); text-transform: uppercase; margin-bottom: 10px;
    }
    .msg-compose-body {
        display: flex; gap: 10px; align-items: stretch;
    }
    .msg-compose textarea.inp-msg {
        flex: 1; padding: 9px 12px;
        border: 1.5px solid var(--border); border-radius: 7px;
        font-size: 13px; font-family: inherit;
        resize: vertical; min-height: 80px; max-height: 180px;
        background: var(--surface2); color: var(--text); line-height: 1.55;
        transition: border-color .15s; box-sizing: border-box;
    }
    .msg-compose textarea.inp-msg:focus {
        outline: none; border-color: var(--accent); background: #fff;
    }
    .msg-compose-right {
        display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; width: 160px;
    }
    .msg-compose input.inp-author {
        width: 100%; padding: 8px 12px;
        border: 1.5px solid var(--border);
        border-radius: 7px; font-size: 13px; font-family: inherit;
        background: var(--surface2); color: var(--text);
        transition: border-color .15s; box-sizing: border-box;
    }
    .msg-compose input.inp-author:focus {
        outline: none; border-color: var(--accent); background: #fff;
    }
    .btn-post {
        width: 100%; padding: 9px 0; background: var(--accent); color: #fff;
        border: none; border-radius: 7px; font-size: 13px;
        font-weight: 700; cursor: pointer;
        letter-spacing: .3px; transition: filter .15s, transform .1s;
        flex: 1;
    }
    .btn-post:hover { filter: brightness(1.1); }
    .btn-post:active { transform: scale(.97); }

    /* -- Filter bar -- */
    .msg-filter-bar {
        display: flex; gap: 6px; align-items: center; margin-bottom: 16px;
    }
    .msg-filter-bar .btn-flt {
        padding: 5px 16px; border-radius: 20px;
        border: 1.5px solid var(--border);
        background: var(--surface); color: var(--text-dim);
        font-size: 12px; font-weight: 600; cursor: pointer;
        transition: all .15s;
    }
    .msg-filter-bar .btn-flt:hover { border-color: var(--accent); color: var(--accent); }
    .msg-filter-bar .btn-flt.active-flt {
        background: var(--accent); color: #fff; border-color: var(--accent);
    }
    .msg-count-badge {
        margin-left: auto; font-size: 11px; color: var(--text-dim); font-weight: 600;
    }

    /* -- Message list -- */
    .msg-list { display: flex; flex-direction: column; gap: 8px; }

    .msg-card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-left: 4px solid var(--accent);
        border-radius: 10px;
        padding: 14px 16px 12px;
        transition: box-shadow .15s, border-left-color .15s;
        position: relative;
    }
    .msg-card:hover { box-shadow: 0 3px 14px rgba(0,0,0,0.08); }
    .msg-card.done-card {
        border-left-color: #27ae60;
        background: #f9fdf9; opacity: .65;
    }
    .msg-card.done-card .msg-content {
        text-decoration: line-through; color: #999;
    }
    /* Priority border colors */
    .msg-card.pri-K { border-left-color: #e74c3c; }
    .msg-card.pri-P { border-left-color: #f39c12; }
    .msg-card.pri-G { border-left-color: #27ae60; }
    .msg-card.pri-U { border-left-color: #95a5a6; }

    /* Priority badge */
    .pri-badge {
        display: inline-flex; align-items: center; justify-content: center;
        font-size: 10.5px; font-weight: 800; letter-spacing: .4px;
        border-radius: 4px; padding: 1px 8px; flex-shrink: 0;
        text-transform: uppercase; cursor: pointer;
        border: none; transition: filter .12s;
    }
    .pri-badge:hover { filter: brightness(1.15); }
    .pri-badge.pri-K { background: #e74c3c; color: #fff; }
    .pri-badge.pri-P { background: #f39c12; color: #fff; }
    .pri-badge.pri-G { background: #27ae60; color: #fff; }
    .pri-badge.pri-U { background: #95a5a6; color: #fff; }

    /* Priority filter buttons */
    .msg-filter-bar .btn-flt.flt-K.active-flt { background: #e74c3c; color: #fff; border-color: #e74c3c; }
    .msg-filter-bar .btn-flt.flt-P.active-flt { background: #f39c12; color: #fff; border-color: #f39c12; }
    .msg-filter-bar .btn-flt.flt-G.active-flt { background: #27ae60; color: #fff; border-color: #27ae60; }
    .msg-filter-bar .btn-flt.flt-U.active-flt { background: #95a5a6; color: #fff; border-color: #95a5a6; }

    /* Priority modal */
    .pri-select-grid {
        display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 14px 0;
    }
    .pri-select-btn {
        padding: 14px 10px; border: 2.5px solid transparent;
        border-radius: 8px; font-size: 13px; font-weight: 700;
        cursor: pointer; text-align: center; transition: transform .1s, box-shadow .1s;
    }
    .pri-select-btn:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
    .pri-select-btn.psk { background: #fde8e7; border-color: #e74c3c; color: #c0392b; }
    .pri-select-btn.psp { background: #fef4e0; border-color: #f39c12; color: #b7770d; }
    .pri-select-btn.psg { background: #eafaf1; border-color: #27ae60; color: #1a7a40; }
    .pri-select-btn.psu { background: #f2f3f4; border-color: #95a5a6; color: #5d6d7e; }

    /* card top row */
    .msg-card-top {
        display: flex; justify-content: space-between; align-items: flex-start;
        gap: 12px;
    }
    .msg-card-main { flex: 1; min-width: 0; }

    /* author + id row */
    .msg-header-row {
        display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
    }
    .msg-id {
        font-size: 10.5px; font-weight: 700; color: #fff;
        background: var(--accent); border-radius: 4px;
        padding: 1px 7px; flex-shrink: 0; letter-spacing: .3px;
    }
    .msg-author-name {
        font-size: 12px; font-weight: 700; color: #6c3483;
    }
    .msg-author-dot {
        width: 4px; height: 4px; border-radius: 50%;
        background: #ccc; flex-shrink: 0; margin-top: 1px;
    }
    .msg-time {
        font-size: 11px; color: var(--text-dim);
    }
    .msg-done-time {
        font-size: 11px; color: #27ae60; font-weight: 600;
    }

    .msg-content {
        font-size: 14px; line-height: 1.65; color: var(--text);
        white-space: pre-wrap; word-break: break-word;
    }

    /* action buttons */
    .msg-actions {
        display: flex; flex-direction: column; gap: 5px;
        flex-shrink: 0; align-items: flex-end;
    }
    .msg-actions-row { display: flex; gap: 5px; }

    .btn-act {
        padding: 5px 11px; border: none; border-radius: 6px;
        font-size: 11.5px; font-weight: 700; cursor: pointer;
        transition: filter .12s; white-space: nowrap;
    }
    .btn-act:hover { filter: brightness(1.12); }
    .btn-act:disabled { opacity: .4; cursor: default; filter: none; }
    .btn-act.green  { background: #27ae60; color: #fff; }
    .btn-act.blue   { background: #2980b9; color: #fff; }
    .btn-act.red    { background: #e74c3c; color: #fff; }

    /* comment toggle */
    .cmt-toggle-btn {
        margin-top: 8px; font-size: 11.5px; font-weight: 600;
        color: var(--accent); background: none; border: none;
        cursor: pointer; padding: 0; display: inline-flex;
        align-items: center; gap: 4px; transition: color .12s;
    }
    .cmt-toggle-btn:hover { color: #0d47a1; }
    .cmt-toggle-btn .cmt-arrow { font-size: 9px; transition: transform .15s; }
    .cmt-toggle-btn.open .cmt-arrow { transform: rotate(90deg); }

    /* comments section — always visible */
    .msg-comments {
        margin-top: 10px;
        border-top: 1px solid var(--border);
        padding-top: 10px;
    }
    .msg-comments-header {
        font-size: 11px; font-weight: 700; color: var(--text-dim);
        text-transform: uppercase; letter-spacing: .5px;
        margin-bottom: 8px;
    }
    .comment-item {
        display: flex; gap: 9px; align-items: flex-start;
        padding: 8px 10px; margin-bottom: 6px;
        background: var(--surface2); border-radius: 8px;
        border-left: 3px solid #90caf9;
    }
    .comment-item:last-child { margin-bottom: 0; }
    .comment-avatar {
        width: 28px; height: 28px; border-radius: 50%;
        background: #d6eaf8; display: flex; align-items: center;
        justify-content: center; font-size: 12px; flex-shrink: 0;
        color: #2980b9; font-weight: 700; margin-top: 1px;
    }
    .comment-body { flex: 1; min-width: 0; }
    .comment-meta {
        display: flex; align-items: baseline; gap: 6px; margin-bottom: 3px;
        flex-wrap: wrap;
    }
    .comment-author { font-size: 12px; font-weight: 700; color: #1a5276; }
    .comment-time   { font-size: 10.5px; color: var(--text-dim); }
    .comment-content {
        font-size: 13px; color: var(--text); line-height: 1.5;
        white-space: pre-wrap; word-break: break-word;
    }
    .no-comments {
        font-size: 12px; color: var(--text-dim);
        padding: 4px 2px; font-style: italic;
    }

    /* global expand/collapse bar */
    .msg-expand-bar {
        display: flex; justify-content: flex-end; gap: 8px;
        margin-bottom: 10px;
    }
    .btn-expand-all {
        font-size: 11.5px; font-weight: 600; color: var(--accent);
        background: none; border: 1.5px solid var(--border);
        border-radius: 20px; padding: 4px 14px; cursor: pointer;
        transition: all .12s;
    }
    .btn-expand-all:hover {
        background: var(--accent); color: #fff; border-color: var(--accent);
    }

    .msg-empty {
        text-align: center; padding: 50px 0; color: var(--text-dim);
        font-size: 14px;
    }

    /* =========================================
       MODALS — unified clean style
       ========================================= */
    .modal-overlay {
        display: none; position: fixed; inset: 0; z-index: 9998;
        background: rgba(0,0,0,0.45);
        align-items: center; justify-content: center;
        padding: 16px;
    }
    .modal-overlay.open { display: flex; }
    .modal-box {
        background: #fff; border-radius: 14px;
        padding: 28px 28px 22px;
        width: 100%; max-width: 400px;
        box-shadow: 0 12px 48px rgba(0,0,0,0.22);
    }
    .modal-title {
        font-size: 15px; font-weight: 700; color: var(--accent);
        margin-bottom: 18px; text-align: center;
    }
    .modal-field { margin-bottom: 11px; text-align: left; }
    .modal-field label {
        display: block; font-size: 11px; font-weight: 700;
        color: var(--text-dim); text-transform: uppercase;
        letter-spacing: .5px; margin-bottom: 5px;
    }
    .modal-field input,
    .modal-field textarea {
        width: 100%; padding: 9px 12px;
        border: 1.5px solid var(--border); border-radius: 7px;
        font-size: 13px; font-family: inherit; box-sizing: border-box;
        background: var(--surface2); color: var(--text);
        transition: border-color .15s;
    }
    .modal-field input:focus,
    .modal-field textarea:focus {
        outline: none; border-color: var(--accent); background: #fff;
    }
    .modal-field input[type=password] { letter-spacing: 3px; }
    .modal-field textarea { resize: vertical; min-height: 80px; line-height: 1.5; }
    .modal-err { font-size: 12px; color: #e74c3c; min-height: 18px; margin-bottom: 12px; text-align: center; }
    .modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }
    .btn-modal-ok {
        padding: 9px 24px; background: var(--accent); color: #fff;
        border: none; border-radius: 7px; font-weight: 700;
        font-size: 13px; cursor: pointer; transition: filter .12s;
    }
    .btn-modal-ok:hover { filter: brightness(1.1); }
    .btn-modal-cancel {
        padding: 9px 18px; background: var(--surface2); color: var(--text-dim);
        border: 1px solid var(--border); border-radius: 7px; font-weight: 600;
        font-size: 13px; cursor: pointer; transition: background .12s;
    }
    .btn-modal-cancel:hover { background: #dde; }

    /* -- Employee Table -- */
    .emp-toolbar {
        display: flex; gap: 10px; align-items: center;
        flex-wrap: wrap; margin-bottom: 14px;
    }
    .emp-toolbar input[type=text] {
        padding: 6px 12px; border: 1px solid var(--border);
        border-radius: 6px; font-size: 13px; width: 240px;
        background: var(--surface); color: var(--text);
    }
    .emp-toolbar input:focus { outline: 2px solid var(--accent); }
    .btn {
        padding: 7px 18px; border: none; border-radius: 6px;
        font-size: 13px; font-weight: 700; cursor: pointer;
        transition: filter .15s;
    }
    .btn:hover { filter: brightness(1.1); }
    .btn-import { background: #27ae60; color: #fff; }
    .btn-export { background: #2980b9; color: #fff; }
    .btn-save   { background: #e67e22; color: #fff; }
    .btn-add    { background: #8e44ad; color: #fff; }
    .btn-del    { background: #c0392b; color: #fff; font-size:11px; padding:3px 8px; }

    #emp-table-wrap { overflow-x: auto; overflow-y: visible;
        border: 1px solid var(--border); border-radius: var(--radius);
        background: var(--surface); }
    #emp-table { border-collapse: separate; border-spacing: 0;
        width: max-content; font-size: 12px; table-layout: fixed; }
    #emp-table col.col-no           { width: 40px; }
    #emp-table col.col-name         { width: 220px; }
    #emp-table col.col-employee_id  { width: 80px; }
    #emp-table col.col-resigned     { width: 70px; }
    #emp-table col.col-resign_date  { width: 85px; }
    #emp-table col.col-resign_reason{ width: 120px; }
    #emp-table col.col-nationality  { width: 65px; }
    #emp-table col.col-position     { width: 110px; }
    #emp-table col.col-card_last4   { width: 80px; }
    #emp-table col.col-start_date   { width: 85px; }
    #emp-table col.col-mc_id        { width: 65px; }
    #emp-table col.col-process      { width: 70px; }
    #emp-table col.col-del          { width: 36px; }
    #emp-table thead th {
        position: sticky; top: 0; z-index: 10;
        background: var(--thead-bg); color: var(--thead-fg);
        padding: 7px 12px; font-size: 11.5px; white-space: nowrap;
        border-bottom: 2px solid var(--accent);
        border-right: 1px solid rgba(255,255,255,.15);
        cursor: pointer; user-select: none;
    }
    #emp-table thead th:hover { background: #3d5a74; }
    #emp-table thead th:last-child { border-right: none; cursor: default; }
    #emp-table tbody td {
        padding: 5px 8px; border-bottom: 1px solid var(--border);
        border-right: 1px solid var(--border);
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        max-width: 0;
    }
    #emp-table tbody td:last-child { border-right: none; }
    #emp-table tbody tr:nth-child(even) { background: var(--surface2); }
    #emp-table tbody tr:hover td { background: #ddeeff !important; }
    #emp-table td.resigned-y { color: #c0392b; font-weight: 700; }
    #emp-table input.cell-input {
        width: 100%; border: none; background: transparent;
        font-size: 12px; color: inherit; font-family: inherit;
        padding: 0; outline: none;
    }
    #emp-table input.cell-input:focus {
        background: #fffbe6; border-radius: 3px;
        outline: 2px solid var(--accent);
    }
    .status-bar {
        font-size: 11px; color: var(--text-dim); margin-top: 8px;
        padding: 4px 0; border-top: 1px solid var(--border);
    }
    #upload-input { display: none; }

    /* -- Sort bar -- */
    .sort-bar {
        display: flex; gap: 8px; align-items: center;
        margin-bottom: 10px; flex-wrap: wrap;
    }
    .sort-bar label { font-size: 12px; font-weight: 600; color: var(--text-dim); }
    .sort-bar select {
        font-size: 12px; padding: 4px 10px; border: 1px solid var(--border);
        border-radius: 6px; background: var(--surface); color: var(--text); cursor: pointer;
    }
    .sort-bar select:focus { outline: 2px solid var(--accent); }
    .btn-sort-dir {
        padding: 4px 12px; border: 1px solid var(--border); border-radius: 6px;
        background: var(--surface2); color: var(--text-dim);
        font-size: 12px; font-weight: 700; cursor: pointer;
    }
    .btn-sort-dir:hover { background: var(--accent); color: #fff; border-color: var(--accent); }

    /* -- View toggle -- */
    .view-btn { background:#fff; border:1px solid #90caf9; color:#546e7a; }
    .view-btn.active-view { background:#1565c0; color:#fff; border-color:#1565c0; }

    /* -- Grid view -- */
    #emp-grid-wrap { display:flex; flex-wrap:wrap; gap:14px; padding:4px 0 16px; }
    .emp-card {
        width:140px; border-radius:10px; overflow:hidden;
        border:1px solid var(--border); background:var(--surface);
        box-shadow:0 2px 8px rgba(0,0,0,0.08);
        display:flex; flex-direction:column; align-items:center;
        padding-bottom:10px; cursor:default;
        transition:box-shadow 0.15s, transform 0.15s;
    }
    .emp-card:hover { box-shadow:0 4px 16px rgba(0,0,0,0.16); transform:translateY(-2px); }
    .emp-card-photo {
        width:140px; height:140px; object-fit:cover;
        background:#e0e0e0; display:block;
    }
    .emp-card-photo-placeholder {
        width:140px; height:140px; display:flex; align-items:center;
        justify-content:center; background:#eceff1;
        font-size:40px; color:#b0bec5;
    }
    .emp-card-id {
        font-size:11px; font-weight:700; color:#1565c0;
        margin:8px 6px 2px; text-align:center; cursor:pointer;
        text-decoration:underline;
    }
    .emp-card-id.no-photo { color:#d32f2f; }
    .emp-card-name {
        font-size:11px; color:#37474f; text-align:center;
        padding:0 6px; line-height:1.35;
        word-break:break-word;
    }
    .emp-card-pos {
        font-size:10px; color:#78909c; text-align:center;
        padding:2px 6px;
    }
    #photo-modal {
        display: none; position: fixed; inset: 0; z-index: 9999;
        background: rgba(0,0,0,0.72); align-items: center; justify-content: center;
    }
    #photo-modal.open { display: flex; }
    #photo-modal-box {
        background: #fff; border-radius: 14px; padding: 20px;
        max-width: 420px; width: 90%; text-align: center;
        box-shadow: 0 8px 40px rgba(0,0,0,0.45);
        position: relative;
    }
    #photo-modal-close {
        position: absolute; top: 10px; right: 14px;
        font-size: 22px; cursor: pointer; color: #888; border: none;
        background: none; line-height: 1;
    }
    #photo-modal-close:hover { color: #333; }
    #photo-modal-name {
        font-size: 15px; font-weight: 700; color: #1565c0;
        margin-bottom: 12px;
    }
    #photo-modal-img {
        max-width: 100%; max-height: 340px; border-radius: 8px;
        border: 1px solid #e0e0e0; object-fit: contain;
    }
    #photo-modal-msg { font-size: 13px; color: #999; margin-top: 8px; }
    .emp-id-link {
        color: #1565c0; cursor: pointer; text-decoration: underline;
        font-weight: 700;
    }
    .emp-id-link:hover { color: #0d47a1; }
</style>
"""

TAB_JS_CODE = """
/* -- Tab switching -- */
function switchTab(id) {
    // Block disabled tabs
    var btnEl = document.getElementById('btn-' + id);
    if (btnEl && btnEl.classList.contains('tab-disabled')) return;
    // Guard: panel must exist
    var panelEl = document.getElementById('panel-' + id);
    if (!panelEl) { console.error('switchTab: panel-' + id + ' not found'); return; }
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    panelEl.classList.add('active');
    if (btnEl) btnEl.classList.add('active');
    if (id === 'emp') { loadEmployees(); loadExams(); }
    if (id === 'msg') loadMessages();
    if (id === 'mc')  { loadMachines(); loadMCProblems(); }
    if (id === 'mat') loadMaterials();
    if (id === 'settings') initSettingsTab();
    if (id === 'upload') initUploadTab();
}

/* ==================
   SETTINGS TAB
   ================== */
var SETTINGS_PW     = 'p@ssword';
var settingsUnlocked = false;

function initSettingsTab() {
    var lockEl    = document.getElementById('settings-lock-screen');
    var contentEl = document.getElementById('settings-content');
    if (!lockEl || !contentEl) return;
    if (settingsUnlocked) {
        lockEl.style.display = 'none';
        contentEl.classList.add('unlocked');
        loadSettingsData();
    } else {
        lockEl.style.display = 'flex';
        contentEl.classList.remove('unlocked');
        var inp = document.getElementById('settings-pw-input');
        var err = document.getElementById('settings-lock-err');
        if (inp) { inp.value = ''; setTimeout(function(){ inp.focus(); }, 80); }
        if (err) err.textContent = '';
    }
}

function lockSettings() {
    settingsUnlocked = false;
    var lockEl    = document.getElementById('settings-lock-screen');
    var contentEl = document.getElementById('settings-content');
    if (lockEl)    lockEl.style.display = 'flex';
    if (contentEl) contentEl.classList.remove('unlocked');
    var inp = document.getElementById('settings-pw-input');
    if (inp) { inp.value = ''; setTimeout(function(){ inp.focus(); }, 80); }
}

function settingsPwKeydown(e) {
    if (e.key === 'Enter') unlockSettings();
}

function unlockSettings() {
    var inp = document.getElementById('settings-pw-input');
    var err = document.getElementById('settings-lock-err');
    if (!inp || !err) return;
    if (inp.value === SETTINGS_PW) {
        settingsUnlocked = true;
        inp.value = '';
        initSettingsTab();
    } else {
        err.textContent = '❌ รหัสผ่านไม่ถูกต้อง';
        inp.value = ''; inp.focus();
        setTimeout(function() { err.textContent = ''; }, 2000);
    }
}

function lockSettings() {
    settingsUnlocked = false;
    initSettingsTab();
}

function loadSettingsData() {
    fetch('/api/settings/tabs')
        .then(r => r.json())
        .then(function(d) {
            // d = {msg:false, overall:false, emp:true, ...}  true = disabled
            Object.keys(d).forEach(function(tabId) {
                var tog = document.getElementById('tog-tab-' + tabId);
                if (tog) tog.checked = !d[tabId];   // toggle ON = enabled
                updateToggleLabel(tabId, !d[tabId]);
            });
        })
        .catch(e => console.error('loadSettingsData:', e));
}

function onToggleTab(tabId) {
    var tog = document.getElementById('tog-tab-' + tabId);
    if (!tog) return;
    updateToggleLabel(tabId, tog.checked);
}

function updateToggleLabel(tabId, enabled) {
    var lbl = document.getElementById('tog-lbl-' + tabId);
    if (lbl) {
        lbl.textContent = enabled ? '✅ เปิด' : '🚫 ปิด';
        lbl.style.color = enabled ? '#1a7a40' : '#c0392b';
    }
}

function saveSettings() {
    var tabs = {};
    var tabIds = ['msg','overall','emp','mc','mat'];
    tabIds.forEach(function(id) {
        var tog = document.getElementById('tog-tab-' + id);
        tabs[id] = tog ? !tog.checked : false;  // disabled = !checked
    });
    fetch('/api/settings/tabs', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(tabs)
    })
    .then(r => r.json())
    .then(function(d) {
        if (d.ok) {
            applyTabDisabled(tabs);
            var msg = document.getElementById('settings-saved-msg');
            if (msg) { msg.style.display = 'inline'; setTimeout(function() { msg.style.display = 'none'; }, 2500); }
        }
    })
    .catch(e => alert('❌ ' + e));
}

function applyTabDisabled(disabledMap) {
    // disabledMap: {msg: false, overall: false, emp: true, ...}
    Object.keys(disabledMap).forEach(function(id) {
        var btn = document.getElementById('btn-' + id);
        if (!btn) return;
        if (disabledMap[id]) {
            btn.classList.add('tab-disabled');
            btn.title = '🚫 Tab นี้ถูกปิดโดย Admin';
        } else {
            btn.classList.remove('tab-disabled');
            btn.title = '';
        }
    });
}

// Apply disabled tabs on page load
function applyInitialDisabledTabs() {
    fetch('/api/settings/tabs')
        .then(r => r.json())
        .then(function(d) {
            if (d && typeof d === 'object' && !d.error) {
                applyTabDisabled(d);
            }
        })
        .catch(function() { /* on error, keep all tabs enabled */ });
}

/* ==================
   MESSAGE TAB LOGIC
   ================== */
var MSG_ADD_DEL_PW = '999';
var MSG_DONE_PW    = 'p@ssword';
var _pwCallback    = null;

function openPwModal(title, callback) {
    _pwCallback = callback;
    document.getElementById('pw-modal-title').textContent = title;
    document.getElementById('pw-modal-input').value = '';
    document.getElementById('pw-modal-err').textContent = '';
    document.getElementById('pw-modal').classList.add('open');
    setTimeout(function() { document.getElementById('pw-modal-input').focus(); }, 80);
}
function closePwModal() {
    document.getElementById('pw-modal').classList.remove('open');
    _pwCallback = null;
}
function confirmPw() {
    var val = document.getElementById('pw-modal-input').value;
    if (_pwCallback) {
        var ok = _pwCallback(val);
        if (ok) { closePwModal(); }
        else { document.getElementById('pw-modal-err').textContent = '❌ รหัสไม่ถูกต้อง'; }
    }
}
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        closePwModal();
        closePhotoModal();
        closeCommentModal();
        closePriModal();
    }
    if (e.key === 'Enter' && document.getElementById('pw-modal').classList.contains('open')) {
        confirmPw();
    }
});

var msgFilter    = 'pending';   // 'pending' | 'done' | 'all'
var msgPriFilter = '';          // '' | 'K' | 'P' | 'G' | 'U'

function setMsgFilter(val) {
    msgFilter = val;
    document.getElementById('mf-pending').classList.toggle('active-flt', val === 'pending');
    document.getElementById('mf-done').classList.toggle('active-flt', val === 'done');
    document.getElementById('mf-all').classList.toggle('active-flt', val === 'all');
    renderMessages();
}

function setMsgPriFilter(val) {
    msgPriFilter = (msgPriFilter === val) ? '' : val;  // toggle
    ['K','P','G','U'].forEach(function(p) {
        var el = document.getElementById('mf-pri-' + p);
        if (el) el.classList.toggle('active-flt', msgPriFilter === p);
    });
    renderMessages();
}

var allMessages = [];

function loadMessages() {
    fetch('/api/messages')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            allMessages = data;
            renderMessages();
        })
        .catch(function(e) { console.error('loadMessages error:', e); });
}

function renderMessages() {
    var list = document.getElementById('msg-list');
    if (!list) return;
    var rows = allMessages.filter(function(m) {
        if (msgFilter === 'pending') return !m.done;
        if (msgFilter === 'done')    return !!m.done;
        return true;
    });
    // priority filter
    if (msgPriFilter) {
        rows = rows.filter(function(m) {
            var p = (m.priority || '').toUpperCase();
            if (msgPriFilter === 'U') return (p === '' || p === 'U');
            return p === msgPriFilter;
        });
    }
    var badge = document.getElementById('msg-count-badge');
    if (badge) badge.textContent = rows.length + ' รายการ';
    if (rows.length === 0) {
        list.innerHTML = '<div class="msg-empty">📭 ไม่มีข้อความในหมวดนี้</div>';
        return;
    }
    var html = '';
    rows.forEach(function(m) {
        var pri       = (m.priority || '').toUpperCase();
        var priClass  = pri === 'K' ? 'pri-K' : pri === 'P' ? 'pri-P' : pri === 'G' ? 'pri-G' : 'pri-U';
        var priLabel  = pri === 'K' ? '🔴 K' : pri === 'P' ? '🟡 P' : pri === 'G' ? '🟢 G' : '⚪ -';
        var priTitle  = pri === 'K' ? 'Critical (K)' : pri === 'P' ? 'Priority (P)' : pri === 'G' ? 'General (G)' : 'ยังไม่ได้ระบุ';
        var doneClass    = m.done ? ' done-card' : '';
        var authorLabel  = m.author ? '<span class="msg-author-name">' + escMsg(m.author) + '</span><span class="msg-author-dot"></span>' : '';
        var doneLabel    = m.done && m.done_at
            ? '<span class="msg-done-time">✅ เสร็จ: ' + m.done_at + '</span>' : '';
        var cmtCount    = (m.comments && m.comments.length) ? m.comments.length : 0;
        var cmtLabel    = cmtCount > 0 ? '💬 ความคิดเห็น (' + cmtCount + ')' : '💬 ความคิดเห็น';
        var doneDisabled = m.done ? ' disabled' : '';
        html += '<div class="msg-card ' + priClass + doneClass + '" id="msgcard-' + m.id + '">'
            + '<div class="msg-card-top">'
            +   '<div class="msg-card-main">'
            +     '<div class="msg-header-row">'
            +       '<span class="msg-id">#' + m.id + '</span>'
            +       '<button class="pri-badge ' + priClass + '" title="' + priTitle + ' — คลิกเพื่อเปลี่ยน (ต้องใช้รหัส 999)" data-msgid="' + m.id + '" onclick="openPriModal(this.dataset.msgid)">' + priLabel + '</button>'
            +       authorLabel
            +       '<span class="msg-time">📅 ' + (m.created_at || '') + '</span>'
            +       (doneLabel ? '<span class="msg-author-dot"></span>' + doneLabel : '')
            +     '</div>'
            +     '<div class="msg-content">' + escMsg(m.content) + '</div>'
            +   '</div>'
            +   '<div class="msg-actions">'
            +     '<div class="msg-actions-row">'
            +       '<button class="btn-act blue" data-msgid="' + m.id + '" onclick="openCommentModal(this.dataset.msgid)">✏️ Comment</button>'
            +       '<button class="btn-act green" data-msgid="' + m.id + '" onclick="markDone(this.dataset.msgid)"' + doneDisabled + '>✅ เสร็จ</button>'
            +       '<button class="btn-act red" data-msgid="' + m.id + '" onclick="deleteMsg(this.dataset.msgid)">🗑</button>'
            +     '</div>'
            +   '</div>'
            + '</div>'
            + '<button class="cmt-toggle-btn" id="cmt-btn-' + m.id + '" data-cmtid="' + m.id + '" onclick="toggleComments(this.dataset.cmtid)">'
            +   '<span class="cmt-arrow">▶</span> ' + cmtLabel
            + '</button>'
            + '<div class="msg-comments" id="cmt-section-' + m.id + '" style="display:none;">'
            +   '<div class="msg-comments-header">' + cmtLabel + '</div>'
            +   renderCommentList(m.comments || [])
            + '</div>'
            + '</div>';
    });
    list.innerHTML = html;
}

function renderCommentList(comments) {
    if (!comments || comments.length === 0) {
        return '<div class="no-comments">ยังไม่มีความคิดเห็น — กด ✏️ Comment เพื่อเพิ่ม</div>';
    }
    var h = '';
    comments.forEach(function(c) {
        var initials = (c.author || '?').charAt(0).toUpperCase();
        h += '<div class="comment-item">'
            + '<div class="comment-avatar">' + initials + '</div>'
            + '<div class="comment-body">'
            +   '<div class="comment-meta">'
            +     '<span class="comment-author">' + escMsg(c.author) + '</span>'
            +     '<span class="comment-time">' + (c.created_at || '') + '</span>'
            +   '</div>'
            +   '<div class="comment-content">' + escMsg(c.content) + '</div>'
            + '</div>'
            + '</div>';
    });
    return h;
}

function toggleComments(msgId) {
    var sec = document.getElementById('cmt-section-' + msgId);
    var btn = document.getElementById('cmt-btn-' + msgId);
    if (!sec) return;
    var opening = sec.style.display === 'none';
    sec.style.display = opening ? 'block' : 'none';
    if (btn) {
        var arrow = btn.querySelector('.cmt-arrow');
        if (arrow) arrow.textContent = opening ? '▼' : '▶';
        btn.classList.toggle('open', opening);
    }
}

function expandAllComments() {
    document.querySelectorAll('[id^="cmt-section-"]').forEach(function(sec) {
        sec.style.display = 'block';
        var id = sec.id.replace('cmt-section-', '');
        var btn = document.getElementById('cmt-btn-' + id);
        if (btn) {
            var arrow = btn.querySelector('.cmt-arrow');
            if (arrow) arrow.textContent = '▼';
            btn.classList.add('open');
        }
    });
}

function collapseAllComments() {
    document.querySelectorAll('[id^="cmt-section-"]').forEach(function(sec) {
        sec.style.display = 'none';
        var id = sec.id.replace('cmt-section-', '');
        var btn = document.getElementById('cmt-btn-' + id);
        if (btn) {
            var arrow = btn.querySelector('.cmt-arrow');
            if (arrow) arrow.textContent = '▶';
            btn.classList.remove('open');
        }
    });
}

/* -- Comment Modal -- */
var _commentTargetId = null;

function openCommentModal(msgId) {
    _commentTargetId = parseInt(msgId, 10);
    document.getElementById('cmt-modal-author').value = '';
    document.getElementById('cmt-modal-text').value = '';
    document.getElementById('cmt-modal-err').textContent = '';
    document.getElementById('cmt-modal').classList.add('open');
    setTimeout(function() { document.getElementById('cmt-modal-author').focus(); }, 80);
}
function closeCommentModal() {
    document.getElementById('cmt-modal').classList.remove('open');
    _commentTargetId = null;
}
function submitComment() {
    var author  = (document.getElementById('cmt-modal-author').value || '').trim();
    var content = (document.getElementById('cmt-modal-text').value || '').trim();
    var pw      = (document.getElementById('cmt-modal-pw').value || '').trim();
    var errEl   = document.getElementById('cmt-modal-err');
    if (!author)  { errEl.textContent = '❌ กรุณาใส่ชื่อ'; return; }
    if (!content) { errEl.textContent = '❌ กรุณาพิมพ์ความคิดเห็น'; return; }
    if (pw !== MSG_ADD_DEL_PW) { errEl.textContent = '❌ รหัสไม่ถูกต้อง'; return; }
    if (!_commentTargetId) { errEl.textContent = '❌ เกิดข้อผิดพลาด'; return; }
    fetch('/api/messages/' + _commentTargetId + '/comments', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({author: author, content: content})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.error) { document.getElementById('cmt-modal-err').textContent = '❌ ' + d.error; return; }
        closeCommentModal();
        loadMessages();
    })
    .catch(function(e) { document.getElementById('cmt-modal-err').textContent = '❌ ' + e; });
}

function escMsg(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function addMessage() {
    var ta      = document.getElementById('msg-new-text');
    var ainput  = document.getElementById('msg-new-author');
    var content = (ta ? ta.value : '').trim();
    var author  = (ainput ? ainput.value : '').trim();
    if (!author) { ainput && ainput.focus(); ainput && (ainput.style.borderColor='#e74c3c'); return; }
    if (!content) { ta && ta.focus(); ta && (ta.style.borderColor='#e74c3c'); return; }
    openPwModal('🔐 ยืนยันการเพิ่มข้อความ', function(pw) {
        if (pw !== MSG_ADD_DEL_PW) return false;
        fetch('/api/messages', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({author: author, content: content})
        })
        .then(function(r) { return r.json(); })
        .then(function() {
            if (ta) { ta.value = ''; ta.style.borderColor = ''; }
            if (ainput) ainput.style.borderColor = '';
            msgFilter = 'pending';
            document.getElementById('mf-pending').classList.add('active-flt');
            document.getElementById('mf-done').classList.remove('active-flt');
            document.getElementById('mf-all').classList.remove('active-flt');
            loadMessages();
        })
        .catch(function(e) { alert('เพิ่มไม่ได้: ' + e); });
        return true;
    });
}

function deleteMsg(id) {
    id = parseInt(id, 10);
    openPwModal('🗑 ยืนยันการลบข้อความ', function(pw) {
        if (pw !== MSG_ADD_DEL_PW) return false;
        fetch('/api/messages/' + id, { method: 'DELETE' })
            .then(function() { loadMessages(); })
            .catch(function(e) { alert('ลบไม่ได้: ' + e); });
        return true;
    });
}

function markDone(id) {
    id = parseInt(id, 10);
    openPwModal('✅ ยืนยันว่างานเสร็จแล้ว', function(pw) {
        if (pw !== MSG_DONE_PW) return false;
        fetch('/api/messages/' + id + '/done', { method: 'POST' })
            .then(function() { loadMessages(); })
            .catch(function(e) { alert('ปิดไม่ได้: ' + e); });
        return true;
    });
}

/* ==================
   PRIORITY MODAL
   ================== */
var _priTargetId = null;

function openPriModal(msgId) {
    _priTargetId = parseInt(msgId, 10);
    document.getElementById('pri-modal-err').textContent = '';
    document.getElementById('pri-modal-pw').value = '';
    document.getElementById('pri-modal').classList.add('open');
    setTimeout(function() { document.getElementById('pri-modal-pw').focus(); }, 80);
}
function closePriModal() {
    document.getElementById('pri-modal').classList.remove('open');
    _priTargetId = null;
}
function setPriority(val) {
    var pw = (document.getElementById('pri-modal-pw').value || '').trim();
    var errEl = document.getElementById('pri-modal-err');
    if (pw !== MSG_ADD_DEL_PW) { errEl.textContent = '❌ รหัสไม่ถูกต้อง'; return; }
    if (!_priTargetId) { errEl.textContent = '❌ เกิดข้อผิดพลาด'; return; }
    fetch('/api/messages/' + _priTargetId + '/priority', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({priority: val})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.error) { document.getElementById('pri-modal-err').textContent = '❌ ' + d.error; return; }
        closePriModal();
        loadMessages();
    })
    .catch(function(e) { document.getElementById('pri-modal-err').textContent = '❌ ' + e; });
}

/* ==================
   EMPLOYEE SORT LOGIC
   ================== */
var sortKey = 'default';   // 'default'|'no'|'name'|'employee_id'|'start_date'|'mc_id'|'position'|'nationality'|'process'
var sortDir = 'asc';

function setSortKey(val) {
    sortKey = val;
    reRender();
}
function toggleSortDir() {
    sortDir = (sortDir === 'asc') ? 'desc' : 'asc';
    document.getElementById('btn-sort-dir').textContent = sortDir === 'asc' ? '⬆ ASC' : '⬇ DESC';
    reRender();
}

function sortEmployees(rows) {
    if (sortKey === 'default') return rows;   // ใช้ grouping เดิม
    var key = sortKey;
    return rows.slice().sort(function(a, b) {
        var av = String(a[key]||'');
        var bv = String(b[key]||'');
        // ถ้าเป็นตัวเลข ให้เปรียบเทียบเป็นตัวเลข
        var an = parseFloat(av), bn = parseFloat(bv);
        var cmp;
        if (!isNaN(an) && !isNaN(bn)) { cmp = an - bn; }
        else { cmp = av.localeCompare(bv, 'th'); }
        return sortDir === 'asc' ? cmp : -cmp;
    });
}


function loadEmployees() {
    Promise.all([
        fetch('/api/employees').then(r=>r.json()),
        fetch('/api/exam_results_summary').then(r=>r.json()).catch(()=>({}))
    ]).then(function(results) {
        var data = results[0];
        var summary = results[1];
        data.forEach(function(row) {
            var eid = String(row.employee_id||'').trim();
            var s = summary[eid] || {};
            row.last_theory    = s.last_theory   || null;
            row.last_operate   = s.last_operate  || null;
            row.all_theory     = s.all_theory    || [];
            row.all_operate    = s.all_operate   || [];
        });
        empData = data;
        if (currentView === 'grid') renderGridView(data);
        else renderEmpTable(data);
        setStatus('โหลดข้อมูลสำเร็จ — ' + data.length + ' รายการ');
    }).catch(e => setStatus('❌ โหลดไม่ได้: ' + e));
}

var COL_KEYS = ['no','name','employee_id','start_date',
                'train_work_unit','train_theory','train_operate','train_sign_doc',
                'exam_edit',
                'resigned','resign_date','resign_reason',
                'nationality','position','card_last4',
                'mc_id','process'];
var COL_LABELS = ['No.','ชื่อ','Employee ID','วันเริ่มงาน',
                  'สอบ Work Unit','สอบ Theory','สอบ Operate','เซ็นเอกสาร',
                  '📋 แก้ไขสอบ',
                  'ลาออก (Y/N)','วันลาออก','เหตุผลที่ออก',
                  'สัญชาติ','Position','เลขท้ายบัตร',
                  'MC/ID','Process'];
var TRAIN_KEYS = ['train_work_unit','train_theory','train_operate','train_sign_doc'];
var FREEZE_KEYS = ['no','name','employee_id'];  // freeze first 3 columns


function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
            .replace(/"/g,'&quot;');
}

function cellChanged(el) {
    var ri  = parseInt(el.getAttribute('data-ri'));
    var key = el.getAttribute('data-key');
    var val = el.value;
    if (empData[ri] !== undefined) {
        empData[ri][key] = val;
    }
}

function trainDateChanged(el) {
    var ri  = parseInt(el.getAttribute('data-ri'));
    var key = el.getAttribute('data-key');
    var val = el.value;
    if (empData[ri] !== undefined) empData[ri][key] = val;
    // update badge next to input
    var td = el.closest('td');
    if (td) {
        var badge = td.querySelector('.train-pass, .train-none');
        if (badge) {
            if (val) {
                badge.className = 'train-pass';
                badge.textContent = '✅ ' + val;
            } else {
                badge.className = 'train-none';
                badge.textContent = '—';
            }
        }
    }
}

function deleteRow(ri) {
    if (!confirm('ลบแถวนี้?')) return;
    empData.splice(ri, 1);
    renderEmpTable(empData);
    setStatus('ลบแถวแล้ว (ยังไม่ได้บันทึก — กด Save)');
}

var VIRTUAL_KEYS = ['exam_edit','all_theory','all_operate','last_theory','last_operate'];

/* ── Exam filter state ── */
var examFilter = { type: '', setName: '' };

function applyExamFilterFromSelects() {
    var typeEl = document.getElementById('exam-flt-type');
    var nameEl = document.getElementById('exam-flt-name');
    if (!typeEl || !nameEl) return;
    examFilter.type    = typeEl.value;
    examFilter.setName = nameEl.value;
    reRender();
}

function onExamTypeChange() {
    var typeEl = document.getElementById('exam-flt-type');
    var nameEl = document.getElementById('exam-flt-name');
    if (!typeEl || !nameEl) return;
    var selType = typeEl.value;
    nameEl.innerHTML = '<option value="">— ทุกชุด —</option>';
    if (selType) {
        examSets.filter(function(s){ return s.exam_type === selType; })
            .forEach(function(s){
                var opt = document.createElement('option');
                opt.value = s.name; opt.textContent = s.name;
                nameEl.appendChild(opt);
            });
    }
    examFilter.setName = '';
    nameEl.value = '';
    applyExamFilterFromSelects();
}

function clearExamFilter() {
    examFilter = { type: '', setName: '' };
    var typeEl = document.getElementById('exam-flt-type');
    var nameEl = document.getElementById('exam-flt-name');
    if (typeEl) typeEl.value = '';
    if (nameEl) { nameEl.innerHTML = '<option value="">— ทุกชุด —</option>'; nameEl.value = ''; }
    reRender();
}

function buildExamFilterBar() {
    var bar = document.getElementById('exam-filter-bar');
    if (!bar) return;
    bar.innerHTML =
        '<span style="font-size:11px;font-weight:600;color:var(--text-dim);">🎓 filter ผ่านสอบ:</span>'
        + '<select id="exam-flt-type" onchange="onExamTypeChange()" '
        + 'style="padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--surface);">'
        + '<option value="">— ประเภท —</option>'
        + '<option value="theory">Theory</option>'
        + '<option value="operate">Operate</option>'
        + '</select>'
        + '<select id="exam-flt-name" onchange="applyExamFilterFromSelects()" '
        + 'style="padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;background:var(--surface);min-width:140px;">'
        + '<option value="">— ทุกชุด —</option>'
        + '</select>'
        + '<button class="btn" onclick="clearExamFilter()" '
        + 'style="font-size:11px;padding:3px 12px;background:#e74c3c;color:#fff;">✕ ล้าง</button>';
    // restore current selections
    var typeEl = document.getElementById('exam-flt-type');
    var nameEl = document.getElementById('exam-flt-name');
    if (typeEl && examFilter.type) {
        typeEl.value = examFilter.type;
        examSets.filter(function(s){ return s.exam_type === examFilter.type; })
            .forEach(function(s){
                var opt = document.createElement('option');
                opt.value = s.name; opt.textContent = s.name;
                nameEl.appendChild(opt);
            });
        if (examFilter.setName) nameEl.value = examFilter.setName;
    }
}
function addRow() { openAddEmpModal(); }

function openAddEmpModal() {
    // Clear all fields
    ['add-emp-no','add-emp-name','add-emp-id','add-emp-start',
     'add-emp-nationality','add-emp-position','add-emp-card',
     'add-emp-mc','add-emp-process'].forEach(function(id){
        var el=document.getElementById(id); if(el) el.value='';
    });
    document.getElementById('add-emp-err').textContent='';
    document.getElementById('add-emp-modal').classList.add('open');
    setTimeout(function(){ var el=document.getElementById('add-emp-name'); if(el)el.focus(); },80);
}
function closeAddEmpModal(){
    document.getElementById('add-emp-modal').classList.remove('open');
}
function submitAddEmp(){
    var name = (document.getElementById('add-emp-name').value||'').trim();
    var eid  = (document.getElementById('add-emp-id').value||'').trim();
    var err  = document.getElementById('add-emp-err');
    if(!name){ err.textContent='❌ กรุณาใส่ชื่อ'; return; }
    // Build row object
    var maxNo = empData.reduce(function(m,r){ return Math.max(m, parseInt(r.no)||0); },0);
    var row = {
        no:           (document.getElementById('add-emp-no').value||'').trim() || String(maxNo+1),
        name:         name,
        employee_id:  eid,
        start_date:   (document.getElementById('add-emp-start').value||'').trim(),
        nationality:  (document.getElementById('add-emp-nationality').value||'').trim(),
        position:     (document.getElementById('add-emp-position').value||'').trim(),
        card_last4:   (document.getElementById('add-emp-card').value||'').trim(),
        mc_id:        (document.getElementById('add-emp-mc').value||'').trim(),
        process:      (document.getElementById('add-emp-process').value||'').trim(),
        resigned:'', resign_date:'', resign_reason:'',
        train_work_unit:'', train_theory:'', train_operate:'', train_sign_doc:''
    };
    err.textContent='⏳ กำลังบันทึก...';
    fetch('/api/employees/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(row)})
    .then(r=>r.json()).then(function(d){
        if(d.error){ err.textContent='❌ '+d.error; return; }
        closeAddEmpModal();
        loadEmployees();
        setStatus('✅ เพิ่มพนักงาน "'+name+'" แล้ว');
    }).catch(function(e){ err.textContent='❌ '+e; });
}
function saveEmployees() {
    fetch('/api/employees', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(empData)
    })
    .then(r => r.json())
    .then(d => setStatus('✅ บันทึกแล้ว — ' + d.saved + ' รายการ'))
    .catch(e => setStatus('❌ บันทึกไม่ได้: ' + e));
}

function exportEmployees() {
    window.location.href = '/api/employees/export';
}

function triggerImport() {
    document.getElementById('upload-input').click();
}

function importEmployees(input) {
    if (!input.files || !input.files[0]) return;
    var fd = new FormData();
    fd.append('file', input.files[0]);
    setStatus('⏳ กำลัง import...');
    fetch('/api/employees/import', { method: 'POST', body: fd })
        .then(function(r) {
            return r.json().then(function(d) {
                return { ok: r.ok, data: d };
            });
        })
        .then(function(res) {
            if (!res.ok || res.data.error) {
                setStatus('❌ Import error: ' + (res.data.error || 'unknown'));
                return;
            }
            var msg = '✅ Import สำเร็จ — ' + res.data.imported + ' รายการ';
            if (res.data.training_imported) msg += ', Training ' + res.data.training_imported + ' รายการ';
            setStatus(msg + ' — กำลังโหลด...');
            // reload employees and re-render
            fetch('/api/employees')
                .then(function(r2) { return r2.json(); })
                .then(function(data) {
                    empData = data;
                    loadEmployees();  // full reload with exam summary
                    setStatus(msg);
                })
                .catch(function(e) { setStatus('❌ โหลดข้อมูลไม่ได้หลัง import: ' + e); });
        })
        .catch(function(e) { setStatus('❌ Import ไม่ได้: ' + e); });
    input.value = '';
}

/* -- View mode -- */
var currentView  = 'table';
var resignFilter = 'active';   // 'active' | 'resigned' | 'all'

function setView(mode) {
    currentView = mode;
    document.getElementById('btn-view-table').classList.toggle('active-view', mode === 'table');
    document.getElementById('btn-view-grid').classList.toggle('active-view', mode === 'grid');
    reRender();
}

function setResignFilter(val) {
    resignFilter = val;
    document.getElementById('rf-active').classList.toggle('active-view',  val === 'active');
    document.getElementById('rf-resigned').classList.toggle('active-view', val === 'resigned');
    document.getElementById('rf-all').classList.toggle('active-view',      val === 'all');
    reRender();
}

function reRender() {
    if (currentView === 'table') renderEmpTable(empData);
    else renderGridView(empData);
}

function filterEmp() { reRender(); }

/* -- shared filter helper -- */
function applyFilters(data) {
    var q = ((document.getElementById('emp-search') || {}).value || '').toLowerCase();

    // 1. resign filter
    var rows = data.filter(function(r) {
        var res = String(r.resigned||'').toUpperCase() === 'Y';
        if (resignFilter === 'active')  return !res;
        if (resignFilter === 'resigned') return res;
        return true;
    });

    // 2. search filter
    if (q) {
        rows = rows.filter(function(r) {
            return COL_KEYS.some(function(k) {
                return String(r[k]||'').toLowerCase().includes(q);
            });
        });
    }

    // 3. exam set filter
    if (examFilter && examFilter.setName) {
        var eft = examFilter.type;
        var efn = examFilter.setName;
        rows = rows.filter(function(r) {
            var arr = eft === 'theory' ? (r.all_theory||[]) : (r.all_operate||[]);
            return arr.some(function(e){ return e.set_name === efn && e.passed; });
        });
    }

    // 3. sort or group
    if (sortKey !== 'default') {
        // เรียงตาม sortKey ที่เลือก (ไม่แบ่งกลุ่ม)
        rows = sortEmployees(rows);
        return { flat: rows, others: [], ops: [] };
    }

    // default: group non-operator first, operator last (เหมือนเดิม)
    var ops    = rows.filter(function(r) { return String(r.position||'').toLowerCase().includes('operator'); });
    var others = rows.filter(function(r) { return !String(r.position||'').toLowerCase().includes('operator'); });
    return { flat: null, others: others, ops: ops };
}

function renderEmpTable(data) {
    var wrapEl = document.getElementById('emp-table-wrap');
    if (!wrapEl) return;

    var f      = applyFilters(data);
    var others = f.others;
    var ops    = f.ops;
    var flat   = f.flat;   // non-null when sortKey !== 'default'

    function makeRows(rows) {
        var freezeLeft = {'no':'0px','name':'44px','employee_id':'184px'};
        var EXAM_KEYS       = ['train_theory','train_operate'];
        var DATE_ONLY_KEYS  = ['train_work_unit','train_sign_doc'];
        var h = '';
        rows.forEach(function(row) {
            // Use findIndex by employee_id for reliability (indexOf fails on filtered/sorted copies)
            var ri = empData.findIndex(function(r) {
                return r === row || (r.employee_id && r.employee_id === row.employee_id && r.no === row.no);
            });
            if (ri < 0) ri = empData.indexOf(row);  // fallback
            var res = String(row.resigned||'').toUpperCase() === 'Y';
            h += '<tr' + (res ? ' style="opacity:.55"' : '') + '>';
            COL_KEYS.forEach(function(k) {
                var isFreeze = freezeLeft[k] !== undefined;
                var fCls     = isFreeze ? ' freeze' : '';
                var fStyle   = isFreeze ? ' style="left:' + freezeLeft[k] + ';"' : '';
                var resCls   = (k==='resigned' && res) ? ' resigned-y' : '';

                if (k === 'employee_id' && String(row[k]||'').trim()) {
                    // clickable employee ID → photo
                    var eid   = escHtml(String(row[k]||'').trim());
                    var ename = escHtml(String(row['name']||'').trim());
                    h += '<td class="' + fCls + '"' + fStyle + '>'
                        + '<span class="emp-id-link" id="eid-' + eid + '" '
                        + 'data-eid="' + eid + '" data-ename="' + ename + '" '
                        + 'onclick="showEmpPhoto(this)" style="color:#1565c0;">'
                        + eid + '</span></td>';

                } else if (k === 'exam_edit') {
                    // Single "แก้ไข/ดูสอบ" button column
                    var eid2x = String(row['employee_id']||'').trim();
                    h += '<td style="text-align:center;" class="' + fCls + '"' + fStyle + '>'
                        + (eid2x ? '<button class="btn-exam-view" style="font-size:10px;padding:3px 8px;width:auto;" '
                          + 'data-eid="'+escHtml(eid2x)+'" data-ri="'+ri+'" onclick="openEmpTrainModal(this.dataset.eid,this.dataset.ri)">📋 แก้ไข / ดูสอบ</button>' : '')
                        + '</td>';

                } else if (EXAM_KEYS.indexOf(k) !== -1) {
                    // Theory/Operate — badges only, no button
                    var allKey   = k === 'train_theory' ? 'all_theory' : 'all_operate';
                    var allPassed= row[allKey] || [];
                    var badgeHtml = '';
                    if (allPassed.length > 0) {
                        badgeHtml = allPassed.map(function(item) {
                            var sn = item.set_name || '';
                            var snShort = sn.length > 14 ? sn.substring(0,14)+'…' : sn;
                            return '<span class="exam-score-badge exam-pass" title="'+escHtml(sn)+' ('+item.taken_at+')">✅ '+escHtml(snShort)+'</span>';
                        }).join(' ');
                    } else {
                        badgeHtml = '<span class="exam-none">—</span>';
                    }
                    h += '<td class="exam-cell' + fCls + '"' + fStyle
                        + ' style="min-width:120px;">'
                        + '<div style="display:flex;flex-wrap:wrap;gap:3px;justify-content:center;">'
                        + badgeHtml + '</div>'
                        + '</td>';

                } else if (DATE_ONLY_KEYS.indexOf(k) !== -1) {
                    // Work Unit / เซ็นเอกสาร → badge only
                    var valD = String(row[k]||'');
                    var badgeD = valD.trim() !== ''
                        ? '<span class="train-pass">✅ ' + escHtml(valD) + '</span>'
                        : '<span class="train-none">—</span>';
                    h += '<td class="train-cell' + fCls + '"' + fStyle + '>' + badgeD + '</td>';

                } else {
                    h += '<td class="' + resCls + fCls + '"' + fStyle + '>'
                        + '<input class="cell-input" value="'
                        + escHtml(String(row[k]||''))
                        + '" data-ri="' + ri + '" data-key="' + k + '" onchange="cellChanged(this)"></td>';
                }
            });
            h += '<td><button class="btn btn-del" onclick="deleteRow(' + ri + ')">✕</button></td>';
            h += '</tr>';
        });
        return h;
    }

    function makeHeader() {
        // pre-compute freeze offsets: no=0px, name=44px, employee_id=184px
        var freezeLeft = {'no':'0px','name':'44px','employee_id':'184px'};
        var h = '<col class="col-del"></colgroup><thead><tr>';
        COL_LABELS.forEach(function(lbl, i) {
            var k = COL_KEYS[i];
            var arrow = (sortKey === k) ? (sortDir === 'asc' ? ' ⬆' : ' ⬇') : '';
            var fCls  = '';
            var fStyle = '';
            if (freezeLeft[k] !== undefined) {
                fCls  = ' freeze freeze-' + i;
                fStyle = ' style="left:' + freezeLeft[k] + ';"';
            }
            h += '<th class="' + fCls + '"' + fStyle
               + ' onclick="setSortKey(this.dataset.sortkey)" data-sortkey="' + k
               + '" title="Sort by ' + lbl + '">' + lbl + arrow + '</th>';
        });
        h += '<th>ลบ</th></tr></thead>';
        return h;
    }

    if (flat !== null) {
        // Flat sorted view (no grouping)
        if (flat.length === 0) {
            wrapEl.innerHTML = '<div style="padding:30px;text-align:center;color:#999;">ไม่มีข้อมูล</div>';
            return;
        }
        var html = '<table id="emp-table"><colgroup>';
        COL_KEYS.forEach(function(k) { html += '<col class="col-' + k + '">'; });
        html += makeHeader() + '<tbody>';
        html += makeRows(flat);
        html += '</tbody></table>';
        wrapEl.innerHTML = html;
        probePhotos(flat);
        return;
    }

    if (others.length + ops.length === 0) {
        wrapEl.innerHTML = '<div style="padding:30px;text-align:center;color:#999;">ไม่มีข้อมูล</div>';
        return;
    }

    var html = '<table id="emp-table"><colgroup>';
    COL_KEYS.forEach(function(k) { html += '<col class="col-' + k + '">'; });
    html += makeHeader() + '<tbody>';

    if (others.length > 0) {
        html += '<tr><td colspan="' + (COL_KEYS.length+1) + '" '
            + 'style="background:#e8f5e9;color:#2e7d32;font-weight:700;font-size:12px;padding:5px 10px;border-bottom:2px solid #a5d6a7;">'
            + '👥 ตำแหน่งอื่นๆ (' + others.length + ' คน)</td></tr>';
        html += makeRows(others);
    }
    if (ops.length > 0) {
        html += '<tr><td colspan="' + (COL_KEYS.length+1) + '" '
            + 'style="background:#e3f2fd;color:#1565c0;font-weight:700;font-size:12px;padding:5px 10px;border-bottom:2px solid #90caf9;">'
            + '⚙️ Operator (' + ops.length + ' คน)</td></tr>';
        html += makeRows(ops);
    }

    html += '</tbody></table>';
    wrapEl.innerHTML = html;

    probePhotos([].concat(others, ops));
}

function probePhotos(rows) {
    rows.forEach(function(row) {
        var eid  = String(row['employee_id']||'').trim();
        if (!eid) return;
        var span = document.getElementById('eid-' + eid);
        if (!span) return;
        var probe = new Image();
        probe.onerror = function() { if (span) span.style.color = '#d32f2f'; };
        probe.src = '/api/emp_photo/' + encodeURIComponent(eid);
    });
}

function renderGridView(data) {
    var wrapEl = document.getElementById('emp-table-wrap');
    if (!wrapEl) return;

    var f      = applyFilters(data);
    var others = f.others;
    var ops    = f.ops;
    var flat   = f.flat;

    function makeCards(rows) {
        var h = '';
        rows.forEach(function(row) {
            var eid      = String(row['employee_id']||'').trim();
            var ename    = String(row['name']||'').trim();
            var epos     = String(row['position']||'').trim();
            var res      = String(row['resigned']||'').toUpperCase() === 'Y';
            var photoUrl = eid ? ('/api/emp_photo/' + encodeURIComponent(eid)) : '';
            var opacity  = res ? 'opacity:0.5;' : '';
            h += '<div class="emp-card" style="' + opacity + '" id="card-' + eid + '">';
            if (photoUrl) {
                h += '<img class="emp-card-photo" src="' + photoUrl + '" '
                    + 'loading="lazy" data-eid="' + eid + '" '
                    + 'onerror="onCardPhotoErr(this)" alt="' + eid + '">';
            } else {
                h += '<div class="emp-card-photo-placeholder">👤</div>';
            }
            h += '<div class="emp-card-id" id="geid-' + eid + '" '
                + 'data-eid="' + eid + '" data-ename="' + ename + '" '
                + 'onclick="showEmpPhoto(this)">' + (eid||'—') + '</div>';
            h += '<div class="emp-card-name">' + ename + '</div>';
            h += '<div class="emp-card-pos">' + epos + '</div>';
            h += '</div>';
        });
        return h;
    }

    if (flat !== null) {
        if (flat.length === 0) {
            wrapEl.innerHTML = '<div style="padding:30px;text-align:center;color:#999;">ไม่มีข้อมูล</div>';
            return;
        }
        wrapEl.innerHTML = '<div id="emp-grid-wrap">' + makeCards(flat) + '</div>';
        return;
    }

    if (others.length + ops.length === 0) {
        wrapEl.innerHTML = '<div style="padding:30px;text-align:center;color:#999;">ไม่มีข้อมูล</div>';
        return;
    }

    var html = '';
    if (others.length > 0) {
        html += '<div style="font-size:13px;font-weight:700;color:#2e7d32;padding:8px 0 6px;border-bottom:2px solid #a5d6a7;margin-bottom:10px;">👥 ตำแหน่งอื่นๆ (' + others.length + ' คน)</div>';
        html += '<div id="emp-grid-wrap" style="margin-bottom:20px;">' + makeCards(others) + '</div>';
    }
    if (ops.length > 0) {
        html += '<div style="font-size:13px;font-weight:700;color:#1565c0;padding:8px 0 6px;border-bottom:2px solid #90caf9;margin-bottom:10px;">⚙️ Operator (' + ops.length + ' คน)</div>';
        html += '<div id="emp-grid-wrap-ops" style="display:flex;flex-wrap:wrap;gap:14px;padding:4px 0 16px;">' + makeCards(ops) + '</div>';
    }
    wrapEl.innerHTML = html;
}

function onCardPhotoErr(img) {
    var eid = img.getAttribute('data-eid') || '';
    var placeholder = document.createElement('div');
    placeholder.className = 'emp-card-photo-placeholder';
    placeholder.textContent = '👤';
    img.parentNode.replaceChild(placeholder, img);
    var idEl = document.getElementById('geid-' + eid);
    if (idEl) idEl.classList.add('no-photo');
}

/* -- end filterEmp override -- */

function setStatus(msg) {
    var el = document.getElementById('emp-status');
    if (el) el.textContent = msg;
}

/* -- Tab 1 helpers (existing) -- */
function refreshPhotos() {
    setStatus('🔄 กำลัง refresh รูปภาพ...');
    fetch('/api/emp_photo/refresh', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            setStatus('✅ Refresh สำเร็จ — พบรูป ' + d.refreshed + ' ไฟล์');
            // re-render เพื่อโหลดรูปใหม่
            if (currentView === 'grid') renderGridView(empData);
            else renderEmpTable(empData);
        })
        .catch(function(e) { setStatus('❌ Refresh ไม่ได้: ' + e); });
}
function scrollToRight() {
    var c = document.getElementsByClassName('scroll-container');
    for (var i = 0; i < c.length; i++) { c[i].scrollLeft = c[i].scrollWidth; }
}
function filterTable(selectEl, tbodyId) {
    var pn = selectEl.value;
    var tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    var rows = tbody.getElementsByTagName('tr');
    for (var i = 0; i < rows.length; i++) {
        var cells = rows[i].getElementsByTagName('td');
        if (cells.length < 2) { rows[i].style.display = ''; continue; }
        var rowPn = cells[1].textContent.trim();
        rows[i].style.display = (pn === 'ALL' || rowPn === pn) ? '' : 'none';
    }
}
/* -- Photo Modal -- */
function showEmpPhoto(el) {
    var empId   = el.getAttribute('data-eid');
    var empName = el.getAttribute('data-ename');
    var modal = document.getElementById('photo-modal');
    var img   = document.getElementById('photo-modal-img');
    var msg   = document.getElementById('photo-modal-msg');
    var title = document.getElementById('photo-modal-name');

    title.textContent = empName ? (empId + '  ' + empName) : empId;
    img.style.display = 'none';
    msg.textContent   = '⏳ กำลังโหลด...';
    modal.classList.add('open');

    var url = '/api/emp_photo/' + encodeURIComponent(empId.trim());
    img.onload  = function() { img.style.display = 'block'; msg.textContent = ''; };
    img.onerror = function() {
        img.style.display = 'none';
        msg.textContent = '❌ ไม่พบรูปสำหรับรหัส ' + empId;
    };
    img.src = url;
}

function closePhotoModal() {
    var modal = document.getElementById('photo-modal');
    modal.classList.remove('open');
    document.getElementById('photo-modal-img').src = '';
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closePhotoModal();
});

window.onload = function() {
    // Stamp build time to confirm new file is loaded
    var ts = document.getElementById('build-ts');
    if (ts) ts.textContent = '(loaded ' + new Date().toLocaleTimeString() + ')';
    scrollToRight();
    loadMessages();
    applyInitialDisabledTabs();
};
"""

TAB_JS = """
<!-- Photo Modal -->
<div id="photo-modal" onclick="if(event.target===this)closePhotoModal()">
  <div id="photo-modal-box">
    <button id="photo-modal-close" onclick="closePhotoModal()">&#10005;</button>
    <div id="photo-modal-name"></div>
    <img id="photo-modal-img" src="" alt="Employee Photo">
    <div id="photo-modal-msg"></div>
  </div>
</div>

<!-- Password Modal -->
<div class="modal-overlay" id="pw-modal" onclick="if(event.target===this)closePwModal()">
  <div class="modal-box">
    <div class="modal-title" id="pw-modal-title">🔐 ยืนยันรหัสผ่าน</div>
    <div class="modal-field">
      <label>รหัสผ่าน</label>
      <input type="password" id="pw-modal-input" placeholder="••••••" autocomplete="off">
    </div>
    <div class="modal-err" id="pw-modal-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closePwModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="confirmPw()">✔ ยืนยัน</button>
    </div>
  </div>
</div>

<!-- Comment Modal -->
<div class="modal-overlay" id="cmt-modal" onclick="if(event.target===this)closeCommentModal()">
  <div class="modal-box" style="max-width:520px;">
    <div class="modal-title">✏️ เพิ่มความคิดเห็น</div>
    <div class="msg-compose-body" style="margin-bottom:10px;">
      <textarea id="cmt-modal-text" class="inp-msg" placeholder="พิมพ์ความคิดเห็น..." style="min-height:90px;"></textarea>
      <div class="msg-compose-right">
        <input type="text" id="cmt-modal-author" class="inp-author" placeholder="ชื่อของคุณ">
        <input type="password" id="cmt-modal-pw" class="inp-author" placeholder="••••••" autocomplete="off" style="letter-spacing:3px;">
        <button class="btn-post" onclick="submitComment()">✔ บันทึก</button>
      </div>
    </div>
    <div class="modal-err" id="cmt-modal-err"></div>
    <div style="text-align:right;">
      <button class="btn-modal-cancel" onclick="closeCommentModal()">ยกเลิก</button>
    </div>
  </div>
</div>

<!-- Priority Modal -->
<div class="modal-overlay" id="pri-modal" onclick="if(event.target===this)closePriModal()">
  <div class="modal-box" style="max-width:360px;">
    <div class="modal-title">🏷️ ตั้ง Priority</div>
    <div class="modal-field">
      <label>รหัสผ่าน (999)</label>
      <input type="password" id="pri-modal-pw" placeholder="••••••" autocomplete="off">
    </div>
    <div class="pri-select-grid">
      <button class="pri-select-btn psk" onclick="setPriority('K')">🔴 K<br><small>Critical</small></button>
      <button class="pri-select-btn psp" onclick="setPriority('P')">🟡 P<br><small>Priority</small></button>
      <button class="pri-select-btn psg" onclick="setPriority('G')">🟢 G<br><small>General</small></button>
      <button class="pri-select-btn psu" onclick="setPriority('')">⚪ —<br><small>Uncategorized</small></button>
    </div>
    <div class="modal-err" id="pri-modal-err"></div>
    <div style="text-align:right;">
      <button class="btn-modal-cancel" onclick="closePriModal()">ยกเลิก</button>
    </div>
  </div>
</div>
"""

def build_tab1_content() -> str:
    """สร้าง HTML content สำหรับ Tab 1 (Overall) — ดึงมาจาก V4 ทั้งหมด"""
    db_wip     = os.path.join(DB_DIR, "MGR_WIP_history.db")
    db_runrate = os.path.join(DB_DIR, "MC_runrate_history.db")
    db_summary = os.path.join(DB_DIR, "machine_summary.db")
    section1_time_cols = []
    html = ""

    from datetime import datetime
    html += f"<div class='info-bar'>🕐 Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>"

    if not os.path.exists(DB_DIR):
        return html + f"<div class='error'>❌ ไม่พบโฟลเดอร์ฐานข้อมูลที่: {DB_DIR}</div>"

    # --- READ WIP DB ---
    df_wip_raw = pd.DataFrame()
    if os.path.exists(db_wip):
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_wip}?mode=ro", uri=True, timeout=5)
            df_wip_raw = pd.read_sql_query(
                "SELECT record_time, pn, name, prev_wip, prev_out, wip_n, total_out FROM WIP_History", conn
            )
            log.info(f"WIP DB loaded: {len(df_wip_raw)} rows")
        except Exception as e:
            log.error(f"WIP DB error: {e}")
        finally:
            if conn:
                conn.close()

    # --- SECTION 1: WIP & OUTPUT TABLE ---
    if not df_wip_raw.empty:
        try:
            df = df_wip_raw.copy()
            df['record_time'] = pd.to_datetime(df['record_time']).dt.strftime('%m-%d<br>%H:%M')
            last_48 = sorted(df['record_time'].unique())[-48:]
            df = df[df['record_time'].isin(last_48)]
            time_cols = sorted(df['record_time'].unique())
            section1_time_cols = time_cols.copy()

            html += "<h2>📌 Section 1: WIP & Output</h2>"

            df_prev = df[~df['pn'].str.upper().str.startswith('ALL', na=False)]
            piv_prev_wip = df_prev.pivot_table(
                index=['pn', 'name'], columns='record_time', values='prev_wip', aggfunc='last'
            ).reindex(columns=time_cols).fillna(0)
            piv_prev_out = df_prev.pivot_table(
                index=['pn', 'name'], columns='record_time', values='prev_out', aggfunc='last'
            ).reindex(columns=time_cols).fillna(0)
            piv_prev_outhr = piv_prev_out.diff(axis=1)
            piv_prev_outhr.iloc[:, 0] = float('nan')
            prev_sorted_idx = sorted(piv_prev_wip.index, key=lambda x: str(x[0]))

            prev_pns  = sorted(set(p for (p, n) in prev_sorted_idx))
            prev_opts = "".join(f"<option value='{p}'>{p}</option>" for p in prev_pns)
            html += "<h3 style='color:#2980b9; margin-top:16px;'>📋 Table A &nbsp;|&nbsp; 🔙 Previous Process</h3>"
            html += (
                "<div class='filter-bar'><label>🔍 Filter PN:</label>"
                f"<select onchange=\"filterTable(this,'tbody-prev')\"><option value='ALL'>— All PN —</option>{prev_opts}</select></div>"
            )
            tbl_prev = "<table class='wip-table'><thead><tr><th>Type</th><th>PN</th><th>Name</th>"
            for col in piv_prev_wip.columns:
                tbl_prev += f"<th>{col}</th>"
            tbl_prev += "</tr></thead><tbody id='tbody-prev'>"
            for (row_pn, row_name) in prev_sorted_idx:
                tbl_prev += f"<tr class='row-prev'><td>🔙 Prev WIP</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_prev_wip.loc[(row_pn, row_name)]:
                    tbl_prev += f"<td>{int(val)}</td>"
                tbl_prev += "</tr>"
            for (row_pn, row_name) in prev_sorted_idx:
                tbl_prev += f"<tr class='row-out'><td>🔙 Prev Out</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_prev_out.loc[(row_pn, row_name)]:
                    tbl_prev += f"<td>{int(val)}</td>"
                tbl_prev += "</tr>"
            for (row_pn, row_name) in prev_sorted_idx:
                tbl_prev += f"<tr class='row-outhr'><td>⚡ Prev Out/hr</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_prev_outhr.loc[(row_pn, row_name)]:
                    if pd.isna(val):
                        tbl_prev += "<td>-</td>"
                    elif val < 0:
                        tbl_prev += "<td style='color:#c0392b;'>N/A</td>"
                    else:
                        tbl_prev += f"<td>{int(val)}</td>"
                tbl_prev += "</tr>"
            tbl_prev += "</tbody></table>"
            html += f"<div class='scroll-container'>{tbl_prev}</div>"

            piv_curr  = df.pivot_table(index=['pn','name'], columns='record_time', values='wip_n', aggfunc='last').reindex(columns=time_cols).fillna(0)
            piv_out   = df.pivot_table(index=['pn','name'], columns='record_time', values='total_out', aggfunc='last').reindex(columns=time_cols).fillna(0)
            piv_outhr = piv_out.diff(axis=1)
            piv_outhr.iloc[:, 0] = float('nan')
            def sort_key(pn): return (1 if str(pn).upper().startswith('ALL') else 0, str(pn))
            sorted_idx = sorted(piv_curr.index, key=lambda x: sort_key(x[0]))
            piv_curr  = piv_curr.reindex(sorted_idx)
            piv_out   = piv_out.reindex(sorted_idx)
            piv_outhr = piv_outhr.reindex(sorted_idx)
            co_pns      = sorted(set(p for (p, n) in sorted_idx if not str(p).upper().startswith('ALL')))
            co_all_pns  = sorted(set(p for (p, n) in sorted_idx if str(p).upper().startswith('ALL')))
            co_opts     = "".join(f"<option value='{p}'>{p}</option>" for p in co_pns)
            co_all_opts = "".join(f"<option value='{p}'>{p}</option>" for p in co_all_pns)
            html += "<h3 style='color:#27ae60; margin-top:16px;'>📋 Table B &nbsp;|&nbsp; 📦 Current WIP &amp; ✅ Output</h3>"
            html += (
                "<div class='filter-bar'><label>🔍 Filter PN:</label>"
                f"<select onchange=\"filterTable(this,'tbody-co')\"><option value='ALL'>— All PN —</option>{co_opts}"
                f"<option disabled>-- ALL group --</option>{co_all_opts}</select></div>"
            )
            tbl_co = "<table class='wip-table'><thead><tr><th>Type</th><th>PN</th><th>Name</th>"
            for col in piv_curr.columns:
                tbl_co += f"<th>{col}</th>"
            tbl_co += "</tr></thead><tbody id='tbody-co'>"
            for (row_pn, row_name) in sorted_idx:
                is_all = str(row_pn).upper().startswith('ALL')
                rc = "row-all" if is_all else "row-curr"
                tbl_co += f"<tr class='{rc}'><td>📦 Curr</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_curr.loc[(row_pn, row_name)]:
                    tbl_co += f"<td>{int(val)}</td>"
                tbl_co += "</tr>"
            for (row_pn, row_name) in sorted_idx:
                is_all = str(row_pn).upper().startswith('ALL')
                rc = "row-all" if is_all else "row-out"
                tbl_co += f"<tr class='{rc}'><td>✅ Out</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_out.loc[(row_pn, row_name)]:
                    tbl_co += f"<td>{int(val)}</td>"
                tbl_co += "</tr>"
            for (row_pn, row_name) in sorted_idx:
                is_all = str(row_pn).upper().startswith('ALL')
                rc = "row-all" if is_all else "row-outhr"
                tbl_co += f"<tr class='{rc}'><td>⚡ Out/hr</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_outhr.loc[(row_pn, row_name)]:
                    if pd.isna(val):
                        tbl_co += "<td>-</td>"
                    elif val < 0:
                        tbl_co += "<td style='color:#c0392b;'>N/A</td>"
                    else:
                        tbl_co += f"<td>{int(val)}</td>"
                tbl_co += "</tr>"
            tbl_co += "</tbody></table>"
            html += f"<div class='scroll-container'>{tbl_co}</div>"
            log.info("Section 1 OK")
        except Exception as e:
            log.error(f"Section 1 error: {e}")
            html += f"<div class='error'>⚠️ Section 1 error: {e}</div>"

    # --- SECTION 2: MACHINE RUN/STOP SUMMARY ---
    try:
        if not section1_time_cols:
            html += "<h2>🟢 Section 2: Machine Run / Stop Summary</h2>"
            html += "<div class='error'>⚠️ Section 2 skipped: ไม่พบ timestamp จาก Section 1</div>"
        elif not os.path.exists(db_summary):
            html += "<h2>🟢 Section 2: Machine Run / Stop Summary</h2>"
            html += f"<div class='error'>❌ ไม่พบ machine_summary.db ที่: {db_summary}</div>"
        else:
            conn = None
            try:
                conn = sqlite3.connect(f"file:{db_summary}?mode=ro", uri=True, timeout=5)
                df_sum = pd.read_sql_query("""
                    SELECT production_date, slot_time, total_machine, run_count,
                           stop_count, no_data_count, run_percent, stop_percent, updated_at
                    FROM hourly_summary
                """, conn)
            finally:
                if conn: conn.close()

            html += "<h2>🟢 Section 2: Machine Run / Stop Summary</h2>"
            if df_sum.empty:
                html += "<div class='error'>⚠️ ไม่มีข้อมูลใน hourly_summary</div>"
            else:
                df_sum = df_sum.copy()
                df_sum['production_date'] = df_sum['production_date'].astype(str)
                df_sum['slot_time'] = df_sum['slot_time'].astype(str).str.strip()
                df_sum['summary_dt'] = pd.to_datetime(
                    df_sum['production_date'] + ' ' + df_sum['slot_time'], errors='coerce')
                df_sum = df_sum.dropna(subset=['summary_dt'])
                df_sum['Label'] = df_sum['summary_dt'].dt.strftime('%m-%d<br>%H:%M')
                df_sum['updated_at_dt'] = pd.to_datetime(df_sum['updated_at'], errors='coerce')
                df_sum = df_sum.sort_values(['Label', 'updated_at_dt', 'summary_dt'])
                df_sum = df_sum.drop_duplicates(subset=['Label'], keep='last').set_index('Label')
                df_sum = df_sum.reindex(section1_time_cols)
                df_display = pd.DataFrame(index=section1_time_cols)
                def fmt_int(v): return '-' if pd.isna(v) else str(int(v))
                def fmt_pct(v): return '-' if pd.isna(v) else f"{float(v):.1f}%"
                df_display['Total_MC']   = df_sum['total_machine'].apply(fmt_int)
                df_display['Running_MC'] = df_sum['run_count'].apply(fmt_int)
                df_display['Stop_MC']    = df_sum['stop_count'].apply(fmt_int)
                df_display['No_Data_MC'] = df_sum['no_data_count'].apply(fmt_int)
                df_display['Run_%']      = df_sum['run_percent'].apply(fmt_pct)
                df_display['Stop_%']     = df_sum['stop_percent'].apply(fmt_pct)
                piv_status = df_display[['Total_MC','Running_MC','Stop_MC','No_Data_MC','Run_%','Stop_%']].T
                piv_status.index = ['Total MC','🟢 Running MC','🔴 Stop MC','⚪ No Data MC','Run %','Stop %']
                html += f"<div class='scroll-container'>{piv_status.to_html(escape=False, classes='runrate-table')}</div>"
                df_chart = df_sum.reset_index().rename(columns={'index': 'Label'})
                df_chart = df_chart.dropna(subset=['total_machine'])
                if not df_chart.empty:
                    fig_status = px.bar(
                        df_chart, x='Label', y=['run_count', 'stop_count', 'no_data_count'],
                        labels={'value': 'Machines', 'Label': 'Time', 'variable': 'Status'}, barmode='stack'
                    )
                    fig_status.update_layout(height=330, margin=dict(l=20,r=20,t=35,b=20), template="plotly_white")
                    html += f"<div class='graph-container'>{pio.to_html(fig_status, full_html=False, include_plotlyjs=False)}</div>"
                log.info("Section 2 OK")
    except Exception as e:
        log.error(f"Section 2 error: {e}")
        html += f"<div class='error'>⚠️ Section 2 error: {e}</div>"

    # --- SECTION 3: ALL TREND ---
    if not df_wip_raw.empty:
        try:
            df_all = df_wip_raw[df_wip_raw['pn'].str.startswith('ALL', na=False)].copy()
            if not df_all.empty:
                df_all['record_time'] = pd.to_datetime(df_all['record_time'])
                df_all = df_all.sort_values('record_time')
                cutoff = df_all['record_time'].max() - pd.Timedelta(days=4)
                df_all = df_all[df_all['record_time'] >= cutoff]
                df_trend = df_all.groupby('record_time').agg({'wip_n':'sum','total_out':'sum'}).reset_index()
                fig = px.line(df_trend, x='record_time', y=['wip_n','total_out'],
                              labels={'value':'Units','record_time':'Time','variable':'Type'})
                fig.update_layout(height=400, margin=dict(l=20,r=20,t=40,b=20), template="plotly_white")
                html += f"<h2>📈 Section 3: Overall Trend (ALL)</h2><div class='graph-container'>{pio.to_html(fig, full_html=False, include_plotlyjs=False)}</div>"
        except Exception as e:
            log.error(f"Section 3 error: {e}")
            html += f"<div class='error'>⚠️ Section 3 error: {e}</div>"

    # --- SECTION 4: MACHINE RUNRATE ---
    if os.path.exists(db_runrate):
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_runrate}?mode=ro", uri=True, timeout=5)
            tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
            if not tables.empty:
                target_table = tables.iloc[0, 0]
                df_rr = pd.read_sql_query(f"SELECT * FROM {target_table}", conn)
                if not df_rr.empty:
                    df_rr['timestamp'] = pd.to_datetime(df_rr['timestamp'])
                    df_rr['Label'] = (
                        df_rr['timestamp'].dt.strftime('%m-%d<br>%H:%M')
                        + " (" + df_rr['shift'].astype(str) + ")"
                    )
                    piv_rr = df_rr.pivot_table(
                        index='Label', columns='machine_name', values='run_rate', aggfunc='last'
                    ).fillna(0.0)
                    sorted_cols = sorted(
                        piv_rr.columns,
                        key=lambda c: [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', str(c))]
                    )
                    piv_rr = piv_rr.reindex(columns=sorted_cols).sort_index(ascending=False)
                    piv_rr.columns.name = None
                    piv_rr.index.name   = None
                    html += f"<h2>⚙️ Section 4: Machine Runrate History</h2><div class='scroll-container-xy'>{piv_rr.to_html(escape=False, classes='runrate-table')}</div>"
                    log.info("Section 4 OK")
        except Exception as e:
            log.error(f"Section 4 error: {e}")
            html += f"<div class='error'>⚠️ Section 4 error: {e}</div>"
        finally:
            if conn: conn.close()

    return html


def build_tab_msg_content() -> str:
    """สร้าง HTML content สำหรับ Tab 1 (Message Board)"""
    return """
<div class="msg-board">
    <h2 style="margin-top:0;">💬 Message Board</h2>

    <!-- Compose box -->
    <div class="msg-compose">
        <div class="msg-compose-label">✍️ เพิ่มข้อความใหม่</div>
        <div class="msg-compose-body">
            <textarea class="inp-msg" id="msg-new-text"
                      placeholder="พิมพ์รายละเอียดงานที่ต้องทำ / ข้อความที่อยากฝากไว้..."></textarea>
            <div class="msg-compose-right">
                <input class="inp-author" type="text" id="msg-new-author"
                       placeholder="ชื่อผู้เขียน">
                <button class="btn-post" onclick="addMessage()">➕ โพสต์</button>
            </div>
        </div>
    </div>

    <!-- Status filter bar -->
    <div class="msg-filter-bar">
        <button class="btn-flt active-flt" id="mf-pending" onclick="setMsgFilter('pending')">⏳ รอดำเนินการ</button>
        <button class="btn-flt" id="mf-done"    onclick="setMsgFilter('done')">✅ เสร็จแล้ว</button>
        <button class="btn-flt" id="mf-all"     onclick="setMsgFilter('all')">📋 ทั้งหมด</button>
        <span class="msg-count-badge" id="msg-count-badge"></span>
    </div>

    <!-- Priority filter bar -->
    <div class="msg-filter-bar" style="margin-top:-6px; margin-bottom:12px;">
        <span style="font-size:11px;font-weight:700;color:#888;letter-spacing:.4px;text-transform:uppercase;">Priority:</span>
        <button class="btn-flt flt-K" id="mf-pri-K" onclick="setMsgPriFilter('K')">🔴 K — Critical</button>
        <button class="btn-flt flt-P" id="mf-pri-P" onclick="setMsgPriFilter('P')">🟡 P — Priority</button>
        <button class="btn-flt flt-G" id="mf-pri-G" onclick="setMsgPriFilter('G')">🟢 G — General</button>
        <button class="btn-flt flt-U" id="mf-pri-U" onclick="setMsgPriFilter('U')">⚪ Uncategorized</button>
    </div>

    <!-- Expand / collapse all comments -->
    <div class="msg-expand-bar">
        <button class="btn-expand-all" onclick="expandAllComments()">▼ แสดง Comment ทั้งหมด</button>
        <button class="btn-expand-all" onclick="collapseAllComments()">▲ ซ่อน Comment ทั้งหมด</button>
    </div>

    <!-- Message list -->
    <div class="msg-list" id="msg-list">
        <div class="msg-empty">⏳ กำลังโหลด...</div>
    </div>
</div>
"""


def build_tab2_content() -> str:
    """Tab 3 Employee V7.13"""
    return """
<!-- ===== SECTION 1: Employee Table ===== -->
<div id="emp-section1">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px;">
  <h2 style="margin:0;">👥 Section 1 — Employee Management</h2>
</div>
<div class="emp-toolbar">
    <input type="text" id="emp-search" placeholder="🔍 ค้นหา ชื่อ / ID / Position..." oninput="filterEmp()">
    <button class="btn btn-add"    onclick="addRow()">➕ เพิ่มพนักงาน</button>
    <button class="btn btn-import" onclick="triggerImport()">📥 Import Excel</button>
    <button class="btn btn-export" onclick="exportEmployees()">📤 Export Excel</button>
    <button class="btn btn-save"   onclick="saveEmployees()">💾 Save</button>
    <button class="btn" onclick="refreshPhotos()" style="background:#546e7a;color:#fff;" title="โหลดรูปใหม่หลังจากเพิ่มไฟล์รูป">🔄 Refresh Photos</button>
    <span style="margin-left:8px;border-left:1px solid #ccc;padding-left:12px;display:flex;gap:6px;align-items:center;">
        <span style="font-size:12px;color:#546e7a;font-weight:600;">แสดง:</span>
        <button class="btn view-btn active-view" id="rf-active"   onclick="setResignFilter('active')">✅ ยังอยู่</button>
        <button class="btn view-btn"             id="rf-resigned" onclick="setResignFilter('resigned')">🚪 ลาออก</button>
        <button class="btn view-btn"             id="rf-all"      onclick="setResignFilter('all')">👥 ทั้งหมด</button>
    </span>
    <span style="margin-left:8px;border-left:1px solid #ccc;padding-left:12px;display:flex;gap:6px;align-items:center;">
        <span style="font-size:12px;color:#546e7a;font-weight:600;">มุมมอง:</span>
        <button class="btn view-btn active-view" id="btn-view-table" onclick="setView('table')">☰ ตาราง</button>
        <button class="btn view-btn"             id="btn-view-grid"  onclick="setView('grid')">⊞ หมากรุก</button>
    </span>
    <input type="file" id="upload-input" accept=".xlsx,.xls"
           onchange="importEmployees(this)">
</div>
<div class="sort-bar">
    <label>🔀 Sort ตาม:</label>
    <select onchange="setSortKey(this.value)" id="sort-key-select">
        <option value="default">— Default (แบ่งกลุ่ม Position) —</option>
        <option value="no">No.</option>
        <option value="name">ชื่อ</option>
        <option value="employee_id">Employee ID</option>
        <option value="start_date">วันเริ่มงาน</option>
        <option value="mc_id">MC/ID</option>
        <option value="position">Position</option>
        <option value="nationality">สัญชาติ</option>
        <option value="process">Process</option>
        <option value="card_last4">เลขท้ายบัตร</option>
    </select>
    <button class="btn-sort-dir" id="btn-sort-dir" onclick="toggleSortDir()">⬆ ASC</button>
    <span style="font-size:11px;color:#999;">* คลิกหัว Column ในตารางเพื่อ sort ได้เช่นกัน</span>
</div>
<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:6px 10px;background:var(--surface2);border-radius:8px;border:1px solid var(--border);" id="exam-filter-bar">
    <span style="font-size:11px;color:#999;">⏳ โหลดชุดข้อสอบ...</span>
</div>
<div id="emp-table-wrap">
    <div style="padding:30px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>
</div>
<div class="status-bar" id="emp-status">พร้อมใช้งาน</div>
</div><!-- /emp-section1 -->

<hr style="margin:28px 0;border:none;border-top:2px solid var(--border);">

<!-- ===== SECTION 2: Training Videos ===== -->
<div id="emp-section2">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:10px;">
    <h2 style="margin:0;">🎬 Section 2 — Training Video</h2>
    <span style="font-size:12px;color:var(--text-dim);">* วางไฟล์วิดีโอไว้ใน D:\\A30-Monitoring\\Employee Training แล้วกด Refresh</span>
  </div>
  <div id="video-grid">
    <div style="padding:20px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>
  </div>
</div>

<!-- Modal: Play Video -->
<div class="modal-overlay" id="video-play-modal" onclick="if(event.target===this)closeVideoModal()">
  <div class="modal-box" style="max-width:820px;">
    <div class="modal-title" id="video-modal-title">▶ Video</div>
    <video id="video-modal-player" controls style="width:100%;border-radius:8px;background:#000;max-height:520px;"></video>
    <div style="text-align:right;margin-top:10px;">
      <button class="btn-modal-cancel" onclick="closeVideoModal()">ปิด</button>
    </div>
  </div>
</div>

<hr style="margin:28px 0;border:none;border-top:2px solid var(--border);">

<!-- ===== SECTION 3: Exam Sets ===== -->
<div id="emp-section3">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:10px;">
    <h2 style="margin:0;">📝 Section 3 — ชุดข้อสอบ</h2>
    <button class="btn btn-add" onclick="openAddExamSetModal()">➕ เพิ่มชุดข้อสอบ</button>
  </div>
  <div id="exam-sets-wrap">
    <div style="padding:20px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>
  </div>
</div>

<!-- Modal: Add Exam Set -->
<div class="modal-overlay" id="add-exam-modal" onclick="if(event.target===this)closeAddExamSetModal()">
  <div class="modal-box" style="max-width:400px;">
    <div class="modal-title">📝 เพิ่มชุดข้อสอบ</div>
    <div class="modal-field"><label>ชื่อชุดข้อสอบ</label><input type="text" id="add-exam-name" placeholder="เช่น Theory Batch 1"></div>
    <div style="display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:10px;">
      <div class="modal-field">
        <label>ประเภท</label>
        <select id="add-exam-type" style="width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface2);">
          <option value="theory">Theory (MCQ)</option>
          <option value="operate">Operate (ผ่าน/ไม่ผ่าน + Comment)</option>
        </select>
      </div>
      <div class="modal-field"><label>จำนวนข้อที่สุ่ม</label><input type="number" id="add-exam-n" value="10" min="1" style="width:100%;"></div>
      <div class="modal-field"><label>เกณฑ์ผ่าน (ข้อ)</label><input type="number" id="add-exam-pass" value="7" min="1" style="width:100%;"></div>
    </div>
    <div class="modal-field"><label>รหัสผ่าน (999)</label><input type="password" id="add-exam-pw" placeholder="••••••" autocomplete="off"></div>
    <div class="modal-err" id="add-exam-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeAddExamSetModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitAddExamSet()">✔ สร้าง</button>
    </div>
  </div>
</div>

<!-- Modal: Exam Editor -->
<div class="modal-overlay" id="exam-editor-modal" onclick="if(event.target===this)closeExamEditor()">
  <div class="modal-box" style="max-width:760px;max-height:90vh;display:flex;flex-direction:column;">
    <div class="modal-title">✏️ แก้ไขชุดข้อสอบ</div>
    
    <div id="exam-editor-pw-row" style="background:#fff8e1;border:1px solid #f39c12;border-radius:8px;padding:8px 12px;margin-bottom:8px;display:flex;align-items:center;gap:8px;">
      <span style="font-size:12px;font-weight:600;">🔐 ใส่รหัสผ่านก่อนแก้ไข:</span>
      <input type="password" id="exam-editor-pw-input" placeholder="level 1" style="width:80px;padding:4px 8px;border:1.5px solid var(--border);border-radius:6px;font-size:13px;" onkeydown="if(event.key==='Enter')checkExamEditorPw()">
      <button class="btn btn-save" style="font-size:11px;padding:4px 12px;" onclick="checkExamEditorPw()">ยืนยัน</button>
      <span id="exam-editor-pw-err" style="color:#e74c3c;font-size:11px;"></span>
    </div>
    
    <div id="exam-editor-unlocked" style="display:none;font-size:12px;color:#1a7a40;font-weight:600;margin-bottom:6px;">✅ ปลดล็อกแล้ว — แก้ไขได้</div>
    
    <!-- หุ้มข้อมูลที่ต้องการซ่อนไว้ใน div นี้ และซ่อนเป็นค่าเริ่มต้น -->
    <div id="exam-editor-content" style="display:none; flex-direction:column; flex:1; min-height:0;">
      <!-- Toolbar: name + random_n + export/import -->
      <div style="background:var(--surface2);padding:10px 14px;border-radius:8px;margin-bottom:10px;display:flex;flex-direction:column;gap:8px;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <label style="font-size:12px;font-weight:600;min-width:60px;">ชื่อชุด:</label>
          <input type="text" id="exam-editor-name" style="flex:1;min-width:180px;padding:5px 10px;border:1.5px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text);">
          <span id="exam-editor-type-badge" style="font-size:11px;font-weight:700;padding:3px 10px;border-radius:10px;"></span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <label style="font-size:12px;font-weight:600;min-width:60px;">สุ่มกี่ข้อ:</label>
          <input type="number" id="exam-editor-n" min="1" style="width:50px;padding:4px 8px;border:1.5px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text);">
          <label style="font-size:12px;font-weight:600;">เกณฑ์ผ่าน:</label>
          <input type="number" id="exam-editor-pass" min="1" style="width:50px;padding:4px 8px;border:1.5px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text);">
          <button class="btn btn-save" style="font-size:11px;padding:4px 14px;" onclick="saveEditorMeta()">💾 บันทึกชื่อ / จำนวน</button>
          <span style="border-left:1px solid var(--border);height:20px;"></span>
          <button class="btn btn-export" style="font-size:11px;padding:4px 12px;" onclick="exportExamSet()">📤 Export</button>
          <label class="btn btn-import" style="font-size:11px;padding:4px 12px;cursor:pointer;margin:0;">
            📥 Import
            <input type="file" id="exam-import-file" accept=".xlsx,.xls" style="display:none;" onchange="importExamSet(this)">
          </label>
        </div>
      </div>
      
      <div id="exam-editor-qlist" style="flex:1;overflow-y:auto;padding-right:4px;min-height:200px;">
        <div style="padding:20px;text-align:center;color:#999;">⏳...</div>
      </div>
      
      <div class="modal-btns" style="margin-top:10px;">
        <button class="btn btn-add" onclick="addEditorQ()">➕ เพิ่มข้อ</button>
        <button class="btn-modal-ok" onclick="saveEditorQuestions()">💾 บันทึกข้อสอบ</button>
      </div>
    </div>
    
    <!-- ปุ่มปิดแสดงเสมอเผื่อผู้ใช้ต้องการยกเลิก -->
    <div class="modal-btns" style="margin-top:10px; justify-content:flex-end;">
      <button class="btn-modal-cancel" onclick="closeExamEditor()">ปิด</button>
    </div>
  </div>
</div>

"""


UPLOAD_CSS = """
/* ========== UPLOAD TAB ========== */
#upload-lock-screen {
    display:flex; flex-direction:column; align-items:center;
    justify-content:center; min-height:300px; gap:16px;
}
.upload-lock-input {
    padding:10px 18px; font-size:16px; border:2px solid var(--border);
    border-radius:8px; text-align:center; letter-spacing:4px; width:240px;
    background:var(--surface2); color:var(--text); outline:none;
}
.upload-lock-input:focus { border-color:var(--accent); }
.upload-lock-err { color:#e74c3c; font-size:13px; min-height:18px; }
.upload-content { display:none; }
.upload-content.unlocked { display:block; }
.upload-path-list { display:flex; flex-direction:column; gap:10px; margin-top:14px; }
.upload-path-card {
    background:var(--surface2); border:1px solid var(--border);
    border-radius:10px; padding:14px 18px;
    display:flex; align-items:center; gap:14px; flex-wrap:wrap;
}
.upload-path-info { flex:1; min-width:160px; }
.upload-path-name { font-size:14px; font-weight:700; }
.upload-path-dir  { font-size:11px; color:var(--text-dim); margin-top:2px; word-break:break-all; }
.btn-upload-file {
    background:var(--accent); color:#fff; border:none; border-radius:7px;
    padding:8px 20px; font-size:13px; font-weight:600; cursor:pointer;
}
.btn-upload-file:hover { filter:brightness(1.1); }
.upload-path-del {
    background:#fde8e7; color:#c0392b; border:1px solid #e74c3c;
    border-radius:6px; padding:5px 12px; font-size:12px; cursor:pointer;
}
"""

UPLOAD_JS = r"""
/* ========== UPLOAD TAB ========== */
var UPLOAD_PW = '999';
var uploadUnlocked = false;
var uploadPaths = [];

function initUploadTab() {
    var lockEl    = document.getElementById('upload-lock-screen');
    var contentEl = document.getElementById('upload-content');
    if (!lockEl || !contentEl) return;
    if (uploadUnlocked) {
        lockEl.style.display = 'none';
        contentEl.classList.add('unlocked');
        loadUploadPaths();
    } else {
        lockEl.style.display = 'flex';
        contentEl.classList.remove('unlocked');
        var inp = document.getElementById('upload-lock-input');
        var err = document.getElementById('upload-lock-err');
        if (inp) { inp.value = ''; setTimeout(function(){ inp.focus(); }, 80); }
        if (err) err.textContent = '';
    }
}

function lockUpload() {
    uploadUnlocked = false;
    var lockEl    = document.getElementById('upload-lock-screen');
    var contentEl = document.getElementById('upload-content');
    if (lockEl)    lockEl.style.display = 'flex';
    if (contentEl) contentEl.classList.remove('unlocked');
    var inp = document.getElementById('upload-lock-input');
    if (inp) { inp.value = ''; setTimeout(function(){ inp.focus(); }, 80); }
}

function uploadPwKeydown(e) { if (e.key === 'Enter') unlockUpload(); }

function unlockUpload() {
    var inp = document.getElementById('upload-lock-input');
    var err = document.getElementById('upload-lock-err');
    if (!inp || !err) return;
    if (inp.value === UPLOAD_PW) {
        uploadUnlocked = true;
        inp.value = '';
        initUploadTab();
    } else {
        err.textContent = '❌ รหัสผ่านไม่ถูกต้อง';
        inp.value = ''; inp.focus();
        setTimeout(function() { err.textContent = ''; }, 2000);
    }
}

function lockUpload() {
    uploadUnlocked = false;
    initUploadTab();
}

function loadUploadPaths() {
    fetch('/api/upload_paths').then(r => r.json()).then(function(d) {
        uploadPaths = d;
        renderUploadPaths();
    }).catch(e => console.error('loadUploadPaths:', e));
}

function renderUploadPaths() {
    var wrap = document.getElementById('upload-path-list');
    if (!wrap) return;
    if (uploadPaths.length === 0) {
        wrap.innerHTML = '<div style="padding:20px;text-align:center;color:#999;">ยังไม่มี Path — กด ➕ เพิ่ม Path ใหม่</div>';
        return;
    }
    wrap.innerHTML = uploadPaths.map(function(p) {
        return '<div class="upload-path-card">'
            + '<div class="upload-path-info">'
            + '<div class="upload-path-name">📁 ' + escHtml(p.name) + '</div>'
            + '<div class="upload-path-dir">' + escHtml(p.path) + '</div>'
            + '</div>'
            + '<label class="btn-upload-file">📤 Upload'
            + '<input type="file" multiple style="display:none;" data-pathid="' + p.id + '" data-pathval="' + escHtml(p.path) + '" onchange="doUploadFiles(this)">'
            + '</label>'
            + '<button class="upload-path-del" data-pid="' + p.id + '" onclick="deleteUploadPath(this.dataset.pid)">🗑 ลบ</button>'
            + '</div>';
    }).join('');
}

function doUploadFiles(input) {
    var pathId   = input.getAttribute('data-pathid');
    var pathVal  = input.getAttribute('data-pathval');
    if (!input.files || input.files.length === 0) return;
    var files    = Array.from(input.files);
    var total    = files.length;
    var statusEl = document.getElementById('upload-status');
    // Find the label/button that triggered this and disable it
    var labelEl  = input.closest('label');
    if (labelEl) { labelEl.style.opacity='0.5'; labelEl.style.pointerEvents='none'; }
    if (statusEl) statusEl.textContent = '⏳ กำลังอัปโหลด 0/' + total + '...';

    // Upload files one-by-one to show progress
    var saved = 0;
    var errors = [];
    function uploadNext(i) {
        if (i >= files.length) {
            // Done
            if (labelEl) { labelEl.style.opacity=''; labelEl.style.pointerEvents=''; }
            input.value = '';
            if (statusEl) {
                statusEl.textContent = errors.length > 0
                    ? '⚠️ อัปโหลด ' + saved + '/' + total + ' ไฟล์ (ผิดพลาด: ' + errors.join(', ') + ')'
                    : '✅ อัปโหลดสำเร็จ ' + saved + '/' + total + ' ไฟล์ → ' + escHtml(pathVal);
                setTimeout(function() { if(statusEl) statusEl.textContent=''; }, 6000);
            }
            return;
        }
        var fd = new FormData();
        fd.append('files', files[i]);
        fd.append('path_id', pathId);
        if (statusEl) statusEl.textContent = '⏳ อัปโหลด ' + (i+1) + '/' + total + ': ' + escHtml(files[i].name);
        fetch('/api/upload_files', { method: 'POST', body: fd })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.error) { errors.push(files[i].name + '(' + d.error + ')'); }
            else { saved += d.saved || 0; }
            uploadNext(i + 1);
        })
        .catch(function(e) {
            errors.push(files[i].name);
            uploadNext(i + 1);
        });
    }
    uploadNext(0);
}

function openAddPathModal() {
    document.getElementById('add-path-name').value = '';
    document.getElementById('add-path-dir').value = '';
    document.getElementById('add-path-err').textContent = '';
    document.getElementById('add-path-modal').classList.add('open');
    setTimeout(function() { document.getElementById('add-path-name').focus(); }, 80);
}
function closeAddPathModal() { document.getElementById('add-path-modal').classList.remove('open'); }
function submitAddPath() {
    var name = (document.getElementById('add-path-name').value || '').trim();
    var path = (document.getElementById('add-path-dir').value || '').trim();
    var err  = document.getElementById('add-path-err');
    if (!name) { err.textContent = '❌ กรุณาใส่ชื่อ'; return; }
    if (!path) { err.textContent = '❌ กรุณาใส่ Directory Path'; return; }
    fetch('/api/upload_paths', { method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({name: name, path: path}) })
    .then(r => r.json())
    .then(function(d) {
        if (d.error) { err.textContent = '❌ ' + d.error; return; }
        closeAddPathModal(); loadUploadPaths();
    }).catch(function(e) { err.textContent = '❌ ' + e; });
}
function deleteUploadPath(id) {
    if (!confirm('ลบ Path นี้?')) return;
    fetch('/api/upload_paths/' + id, { method: 'DELETE' })
    .then(function() { loadUploadPaths(); })
    .catch(e => alert('❌ ' + e));
}
"""


def build_tab_upload_content() -> str:
    return """
<div style="max-width:800px;">
  <!-- Lock screen -->
  <div id="upload-lock-screen">
    <div style="font-size:52px;">📤</div>
    <div style="font-size:20px;font-weight:700;color:var(--accent);">Upload Files</div>
    <div style="font-size:13px;color:var(--text-dim);">กรุณาใส่รหัสผ่านเพื่อใช้งาน</div>
    <input type="password" class="upload-lock-input" id="upload-lock-input"
      placeholder="level 1" autocomplete="off" onkeydown="uploadPwKeydown(event)">
    <button class="btn btn-save" style="padding:10px 36px;font-size:14px;" onclick="unlockUpload()">
      🔓 เข้าสู่ระบบ
    </button>
    <div class="upload-lock-err" id="upload-lock-err"></div>
  </div>

  <!-- Upload content -->
  <div class="upload-content" id="upload-content">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px;">
      <h2 style="margin:0;">📤 Upload Files</h2>
      <div style="display:flex;gap:8px;align-items:center;">
        <span id="upload-status" style="font-size:12px;color:#1a7a40;font-weight:600;"></span>
        <button class="btn btn-add" onclick="openAddPathModal()">➕ เพิ่ม Path ใหม่</button>
        <button class="btn" style="background:#546e7a;color:#fff;font-size:12px;" onclick="lockUpload()">🔒 ล็อก</button>
      </div>
    </div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:12px;">
      กด <strong>Upload</strong> ที่ Path ต้องการ เพื่อเลือกไฟล์อัปโหลดไปยัง Directory นั้น (รองรับหลายไฟล์)
    </div>
    <div class="upload-path-list" id="upload-path-list">
      <div style="padding:20px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>
    </div>
  </div>
</div>

<!-- Modal: Add Path -->
<div class="modal-overlay" id="add-path-modal" onclick="if(event.target===this)closeAddPathModal()">
  <div class="modal-box" style="max-width:480px;">
    <div class="modal-title">📁 เพิ่ม Upload Path</div>
    <div class="modal-field">
      <label>ชื่อ</label>
      <input type="text" id="add-path-name" placeholder="เช่น Training Videos, Reports">
    </div>
    <div class="modal-field">
      <label>Directory Path</label>
      <input type="text" id="add-path-dir" placeholder="เช่น D:\\A30-Monitoring\\Employee Training">
      <small style="color:var(--text-dim);font-size:11px;">ใส่ path บน server ที่ต้องการบันทึกไฟล์</small>
    </div>
    <div class="modal-err" id="add-path-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeAddPathModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitAddPath()">✔ เพิ่ม</button>
    </div>
  </div>
</div>
"""


def build_tab_settings_content() -> str:
    """Tab Settings — password-locked, toggle tabs"""
    rows_html = ""
    for tab_id, tab_label in SETTING_TABS:
        rows_html += f"""
    <div class="settings-tab-row">
      <div>
        <div class="settings-tab-label">{tab_label}</div>
        <div class="settings-tab-note">Tab ID: {tab_id}</div>
      </div>
      <div class="toggle-wrap">
        <span class="toggle-label" id="tog-lbl-{tab_id}" style="color:#1a7a40;">✅ เปิด</span>
        <label class="toggle-switch">
          <input type="checkbox" id="tog-tab-{tab_id}" checked onchange="onToggleTab('{tab_id}')">
          <span class="toggle-slider"></span>
        </label>
      </div>
    </div>"""

    return f"""
<div style="max-width:680px;margin:0 auto;padding-top:20px;">
  <!-- Lock screen -->
  <div id="settings-lock-screen">
    <div class="settings-lock-icon">🔒</div>
    <div class="settings-lock-title">Settings — Admin Only</div>
    <div class="settings-lock-sub">กรุณาใส่รหัสผ่านเพื่อเข้าถึงหน้า Settings</div>
    <input type="password" class="settings-lock-input" id="settings-pw-input"
      placeholder="level 2" autocomplete="off"
      onkeydown="settingsPwKeydown(event)">
    <button class="btn btn-save" style="padding:10px 36px;font-size:14px;" onclick="unlockSettings()">
      🔓 เข้าสู่ระบบ
    </button>
    <div class="settings-lock-err" id="settings-lock-err"></div>
  </div>

  <!-- Settings content (hidden until unlocked) -->
  <div class="settings-content" id="settings-content">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
      <h2 style="margin:0;">⚙️ Settings</h2>
      <button class="btn" style="background:#546e7a;color:#fff;font-size:12px;padding:5px 14px;"
        onclick="lockSettings()">
        🔒 ล็อกหน้าจอ
      </button>
    </div>

    <div class="settings-section-title">🗂️ การแสดงผล Tab</div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:14px;">
      ปิด Tab ที่ไม่ต้องการใช้ — ผู้ใช้จะไม่สามารถกดเข้า Tab ที่ถูกปิดได้<br>
      <strong>หมายเหตุ:</strong> Tab Settings จะแสดงเสมอ ไม่สามารถปิดได้
    </div>
    <div class="settings-tab-list">
      {rows_html}
    </div>
    <div class="settings-save-row">
      <button class="btn btn-save" style="padding:9px 28px;font-size:14px;" onclick="saveSettings()">
        💾 บันทึกการตั้งค่า
      </button>
      <span class="settings-saved-msg" id="settings-saved-msg">✅ บันทึกแล้ว!</span>
    </div>
  </div>
</div>
"""


def build_html() -> str:
    """Build full page HTML with tab navigation"""
    tab_msg_content = build_tab_msg_content()
    tab1_content    = build_tab1_content()
    tab2_content    = build_tab2_content()
    tab4_content    = build_tab4_content()
    tab5_content    = build_tab5_content()

    # CSS & JS blocks are plain strings (no f-string to avoid { } conflicts)
    css_main = (
        "<style>\n"
        ":root {\n"
        "  --bg:#f4f4f9;--surface:#ffffff;--surface2:#eef2f7;--border:#d0d7e3;\n"
        "  --accent:#1a6fc4;--text:#1a1a2e;--text-dim:#666;--radius:10px;\n"
        "  --thead-bg:#2c3e50;--thead-fg:#ffffff;--rr-fg:#1a2a40;\n"
        "  --rr-index-fg:#1a5276;\n"
        "  --prev-bg:#e8f4fd;--prev-fg:#1a5276;\n"
        "  --curr-bg:#eafaf1;--curr-fg:#1a5c35;\n"
        "  --out-bg:#fef5e7;--out-fg:#7d4e00;\n"
        "  --outhr-bg:#f5eef8;--outhr-fg:#6c3483;\n"
        "  --all-bg:#fff9c4;--all-fg:#7d6608;\n"
        "  --err-bg:#ffdada;--err-bd:#e74c3c;--err-fg:#c0392b;\n"
        "  --sb-thumb:#a0aabf;\n"
        "}\n"
        "* { box-sizing:border-box; margin:0; padding:0; }\n"
        "body { font-family:'Segoe UI',sans-serif; background:var(--bg); color:var(--text); padding:20px; }\n"
        "h1 { font-size:20px; font-weight:700; letter-spacing:1px; color:var(--accent); margin-bottom:14px; }\n"
        "h2 { font-size:18px; font-weight:700; color:var(--accent); border-left:5px solid var(--accent); padding:8px 0 8px 14px; margin:32px 0 12px; background:var(--surface2); border-radius:0 var(--radius) var(--radius) 0; }\n"
        "h3 { font-size:15px; font-weight:700; margin:16px 0 8px; padding:5px 12px; border-radius:6px; display:inline-block; }\n"
        ".scroll-container { overflow-x:auto; overflow-y:hidden; max-width:100%; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); margin-bottom:16px; }\n"
        ".scroll-container-xy { overflow-x:auto; overflow-y:auto; max-width:100%; max-height:220px; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); margin-bottom:16px; }\n"
        ".filter-bar { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px; }\n"
        ".filter-bar label { font-size:12px; font-weight:600; color:var(--text-dim); }\n"
        ".filter-bar select { font-size:12px; padding:3px 8px; border:1px solid var(--border); border-radius:6px; background:var(--surface); color:var(--text); cursor:pointer; }\n"
        ".filter-bar select:focus { outline:2px solid var(--accent); }\n"
        "table { border-collapse:separate; border-spacing:0; width:100%; font-size:11.5px; }\n"
        "th,td { border-bottom:1px solid var(--border); border-right:1px solid var(--border); padding:5px 8px; text-align:center; white-space:nowrap; line-height:1.3; }\n"
        "th:last-child,td:last-child { border-right:none; }\n"
        "thead th { position:sticky; top:0; z-index:10; background:var(--thead-bg); color:var(--thead-fg); font-weight:600; font-size:11px; letter-spacing:.3px; border-bottom:2px solid var(--accent); }\n"
        "tbody tr:hover td,tbody tr:hover th { filter:brightness(1.15); }\n"
        ".wip-table th:nth-child(1),.wip-table td:nth-child(1) { position:sticky; left:0; min-width:90px; z-index:5; }\n"
        ".wip-table th:nth-child(2),.wip-table td:nth-child(2) { position:sticky; left:90px; min-width:75px; z-index:5; }\n"
        ".wip-table th:nth-child(3),.wip-table td:nth-child(3) { position:sticky; left:165px; min-width:100px; z-index:5; border-right:2px solid var(--accent) !important; }\n"
        ".wip-table thead th:nth-child(1),.wip-table thead th:nth-child(2),.wip-table thead th:nth-child(3) { z-index:15; }\n"
        ".wip-table td:nth-child(1),.wip-table td:nth-child(2),.wip-table td:nth-child(3) { background-color:inherit !important; color:inherit !important; }\n"
        ".row-prev { background-color:var(--prev-bg) !important; color:var(--prev-fg); }\n"
        ".row-curr { background-color:var(--curr-bg) !important; color:var(--curr-fg); }\n"
        ".row-out  { background-color:var(--out-bg)  !important; color:var(--out-fg);  }\n"
        ".row-outhr{ background-color:var(--outhr-bg) !important; color:var(--outhr-fg); font-style:italic; }\n"
        ".row-all  { background-color:var(--all-bg)  !important; color:var(--all-fg);  font-weight:700; }\n"
        ".row-all td{ background-color:var(--all-bg) !important; color:var(--all-fg);  }\n"
        ".runrate-table thead th:nth-child(1) { position:sticky; left:0; z-index:15; border-right:2px solid var(--accent) !important; }\n"
        ".runrate-table tbody th { position:sticky; left:0; z-index:5; background:var(--surface) !important; color:var(--accent); border-right:2px solid var(--accent); }\n"
        ".runrate-table tr { background:var(--surface); }\n"
        ".runrate-table tr:nth-child(even) { background:var(--surface2); }\n"
        ".runrate-table tbody tr:nth-child(even) th { background:var(--surface2) !important; }\n"
        ".runrate-table td { color:var(--rr-fg); }\n"
        ".runrate-table tbody th { color:var(--rr-index-fg) !important; }\n"
        ".graph-container { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:12px; margin-bottom:20px; }\n"
        ".error { color:var(--err-fg); font-weight:bold; padding:10px 14px; background:var(--err-bg); border:1px solid var(--err-bd); border-radius:var(--radius); margin-bottom:10px; }\n"
        ".info-bar { font-size:11px; color:var(--text-dim); margin-bottom:14px; padding:4px 0; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:12px; }\n"
        "::-webkit-scrollbar { height:6px; width:6px; }\n"
        "::-webkit-scrollbar-track { background:var(--surface); }\n"
        "::-webkit-scrollbar-thumb { background:var(--sb-thumb); border-radius:3px; }\n"
        "::-webkit-scrollbar-thumb:hover { background:var(--accent); }\n"
        "</style>\n"
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"th\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=1400\">\n"
        "<title>A30 Dashboard V7.13</title>\n"
        "<script src=\"https://cdn.plot.ly/plotly-2.27.0.min.js\" charset=\"utf-8\"></script>\n"
        + css_main
        + TAB_CSS
        + "<style>" + TAB45_CSS + UPLOAD_CSS + "</style>"
        + "</head>\n<body>\n"
        "<h1>\U0001f4ca A30 Mechanical Drill Dashboard V7.13 <span id=\"build-ts\" style=\"font-size:11px;font-weight:400;color:#888;margin-left:10px;\"></span></h1>\n"
        "<div class=\"tab-nav\">\n"
        "  <button class=\"tab-btn active\" id=\"btn-msg\" "
        "onclick=\"switchTab('msg')\">💬 1. Message</button>\n"
        "  <button class=\"tab-btn\" id=\"btn-overall\" "
        "onclick=\"switchTab('overall')\">\U0001f4ca 2. Overall</button>\n"
        "  <button class=\"tab-btn\" id=\"btn-emp\" "
        "onclick=\"switchTab('emp')\">\U0001f465 3. Employee</button>\n"
        "  <button class=\"tab-btn\" id=\"btn-mc\" "
        "onclick=\"switchTab('mc')\">🔧 4. Machine</button>\n"
        "  <button class=\"tab-btn\" id=\"btn-mat\" "
        "onclick=\"switchTab('mat')\">📦 5. Material</button>\n"
        "  <button class=\"tab-btn\" id=\"btn-upload\" "
        "onclick=\"switchTab('upload')\">📤 Upload</button>\n"
        "  <button class=\"tab-btn\" id=\"btn-settings\" "
        "onclick=\"switchTab('settings')\" style=\"margin-left:auto;background:#546e7a;color:#fff;\">⚙️ Settings</button>\n"
        "</div>\n"
        "<div class=\"tab-panel active\" id=\"panel-msg\">\n"
        + tab_msg_content
        + "\n</div>\n"
        "<div class=\"tab-panel\" id=\"panel-overall\">\n"
        + tab1_content
        + "\n</div>\n"
        "<div class=\"tab-panel\" id=\"panel-emp\">\n"
        + tab2_content
        + "\n</div>\n"
        "<div class=\"tab-panel\" id=\"panel-mc\">\n"
        + tab4_content
        + "\n</div>\n"
        "<div class=\"tab-panel\" id=\"panel-mat\">\n"
        + tab5_content
        + "\n</div>\n"
        "<div class=\"tab-panel\" id=\"panel-upload\">\n"
        + build_tab_upload_content()
        + "\n</div>\n"
        "<div class=\"tab-panel\" id=\"panel-settings\">\n"
        + build_tab_settings_content()
        + "\n</div>\n"
        + TAB_JS
        + "\n<script>" + TAB_JS_CODE + "</script>"
        + "\n<script>" + UPLOAD_JS + "</script>"
        + "\n<script>" + TAB4_JS_CODE + "</script>"
        + "\n<script>" + TAB5_JS_CODE + "</script>"
        + """
<!-- Modal: Add Employee -->
<div class="modal-overlay" id="add-emp-modal" onclick="if(event.target===this)closeAddEmpModal()">
  <div class="modal-box" style="max-width:560px;">
    <div class="modal-title">➕ เพิ่มพนักงานใหม่</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <div class="modal-field">
        <label>No.</label>
        <input type="text" id="add-emp-no" placeholder="ใส่หรือปล่อยว่างให้ระบบกำหนด">
      </div>
      <div class="modal-field">
        <label>ชื่อ-นามสกุล <span style="color:#e74c3c;">*</span></label>
        <input type="text" id="add-emp-name" placeholder="ชื่อพนักงาน">
      </div>
      <div class="modal-field">
        <label>Employee ID</label>
        <input type="text" id="add-emp-id" placeholder="รหัสพนักงาน">
      </div>
      <div class="modal-field">
        <label>วันเริ่มงาน</label>
        <input type="date" id="add-emp-start">
      </div>
      <div class="modal-field">
        <label>สัญชาติ</label>
        <input type="text" id="add-emp-nationality" placeholder="ไทย / เมียนมา / ...">
      </div>
      <div class="modal-field">
        <label>Position</label>
        <input type="text" id="add-emp-position" placeholder="Operator / Technician / ...">
      </div>
      <div class="modal-field">
        <label>เลขท้ายบัตร</label>
        <input type="text" id="add-emp-card" placeholder="4 ตัวสุดท้าย">
      </div>
      <div class="modal-field">
        <label>MC/ID</label>
        <input type="text" id="add-emp-mc" placeholder="หมายเลขเครื่อง">
      </div>
    </div>
    <div class="modal-field">
      <label>Process</label>
      <input type="text" id="add-emp-process" placeholder="Process code">
    </div>
    <div class="modal-err" id="add-emp-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeAddEmpModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitAddEmp()">✔ เพิ่มพนักงาน</button>
    </div>
  </div>
</div>

<!-- Modal: Employee Train/Exam (Edit dates + view exam history) -->
<div class="modal-overlay" id="emp-train-modal" onclick="if(event.target===this)closeEmpTrainModal()">
  <div class="modal-box" style="max-width:820px;max-height:90vh;display:flex;flex-direction:column;">
    <div class="modal-title">📋 แก้ไข / ดูข้อมูลการสอบ</div>
    <div id="emp-train-body" style="flex:1;overflow-y:auto;padding-right:4px;"></div>
    <div style="margin-top:10px;text-align:right;">
      <button class="btn-modal-cancel" onclick="closeEmpTrainModal()">ปิด</button>
    </div>
  </div>
</div>

<!-- Modal: Employee Exam (Take exam — kept for startExam flow) -->
<div class="modal-overlay" id="emp-exam-modal" onclick="if(event.target===this)closeEmpExamModal()">
  <div class="modal-box" style="max-width:680px;max-height:90vh;display:flex;flex-direction:column;">
    <div class="modal-title">📋 ข้อมูลการสอบ</div>
    <div id="emp-exam-body" style="flex:1;overflow-y:auto;padding-right:4px;"></div>
    <div style="margin-top:10px;text-align:right;">
      <button class="btn-modal-cancel" onclick="closeEmpExamModal()">ปิด</button>
    </div>
  </div>
</div>
"""
        + """

<!-- Modal: Delete Exam History -->
<div class="modal-overlay" id="del-history-modal" onclick="if(event.target===this)closeDeleteHistoryModal()">
  <div class="modal-box" style="max-width:380px;">
    <div class="modal-title">🗑 ลบประวัติการสอบ</div>
    <p style="font-size:13px;color:#c0392b;margin-bottom:14px;">⚠️ การลบจะลบประวัติสอบทั้งหมดของพนักงานคนนี้ ไม่สามารถกู้คืนได้</p>
    <div class="modal-field">
      <label>รหัสผ่าน (level 1)</label>
      <input type="password" id="del-history-pw" placeholder="••••••" autocomplete="off">
    </div>
    <div class="modal-err" id="del-history-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeDeleteHistoryModal()">ยกเลิก</button>
      <button class="btn" style="background:#e74c3c;color:#fff;padding:8px 20px;border-radius:7px;border:none;cursor:pointer;font-size:13px;font-weight:700;" onclick="confirmDeleteHistory()">🗑 ยืนยันลบ</button>
    </div>
  </div>
</div>

<!-- Modal: View Answer Detail -->
<div class="modal-overlay" id="answer-detail-modal" onclick="if(event.target===this)closeAnswerModal()">
  <div class="modal-box" style="max-width:680px;max-height:80vh;display:flex;flex-direction:column;">
    <div class="modal-title">🔍 ดูคำตอบ</div>
    <!-- Password lock -->
    <div id="answer-pw-row" style="display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid var(--border);margin-bottom:10px;">
      <span style="font-size:12px;font-weight:600;">🔐 รหัสผ่าน:</span>
      <input type="password" id="answer-pw-input" placeholder="level 1" autocomplete="off"
        style="width:80px;padding:5px 10px;border:1.5px solid var(--border);border-radius:6px;font-size:13px;"
        onkeydown="if(event.key==='Enter')confirmAnswerPw()">
      <button class="btn btn-save" style="font-size:11px;padding:4px 14px;" onclick="confirmAnswerPw()">ยืนยัน</button>
      <span id="answer-pw-err" style="color:#e74c3c;font-size:11px;"></span>
    </div>
    <div id="answer-detail-body" style="flex:1;overflow-y:auto;padding-right:4px;display:none;">
    </div>
    <div style="margin-top:10px;text-align:right;">
      <button class="btn-modal-cancel" onclick="closeAnswerModal()">ปิด</button>
    </div>
  </div>
</div>

"""
        + "\n</body>\n</html>"
    )

def get_html_cached() -> str:
    global _cached_html, _cache_time
    with _cache_lock:
        if time.time() - _cache_time > CACHE_TTL or not _cached_html:
            log.info("Cache miss — rebuilding HTML...")
            try:
                _cached_html = build_html()
                _cache_time  = time.time()
                log.info("HTML cache updated")
            except Exception as e:
                log.error(f"build_html failed: {e}")
                if not _cached_html:
                    _cached_html = f"<html><body><div style='color:red'>Build error: {e}</div></body></html>"
        return _cached_html


# ----------------------------------------------
#  FLASK ROUTES
# ----------------------------------------------
flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return Response(get_html_cached(), mimetype="text/html; charset=utf-8")


@flask_app.route("/api/employees", methods=["GET"])
def api_emp_get():
    df = load_employees()
    df = df.fillna("")
    return jsonify(df.to_dict(orient="records"))


@flask_app.route("/api/employees/add", methods=["POST"])
def api_emp_add():
    """Add a single new employee row"""
    data = request.get_json(force=True)
    try:
        df = load_employees()
        new_row = {col: str(data.get(col, "")).strip() for col in EMP_COLS}
        # auto-assign no if blank
        if not new_row.get("no"):
            max_no = df["no"].apply(lambda x: int(x) if str(x).isdigit() else 0).max() if len(df) else 0
            new_row["no"] = str(int(max_no) + 1)
        # check duplicate employee_id
        eid = new_row.get("employee_id", "").strip()
        if eid and eid in df["employee_id"].astype(str).values:
            return jsonify({"error": f"Employee ID '{eid}' มีอยู่แล้ว"}), 400
        new_df = pd.DataFrame([new_row], columns=EMP_COLS)
        df = pd.concat([df, new_df], ignore_index=True)
        save_employees(df)
        return jsonify({"ok": True, "no": new_row["no"]})
    except Exception as e:
        log.error(f"api_emp_add error: {e}")
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/employees", methods=["POST"])
def api_emp_post():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({"error": "expected list"}), 400
    df = pd.DataFrame(data)
    # ensure all columns exist
    for col in EMP_COLS:
        if col not in df.columns:
            df[col] = ""
    df = df[EMP_COLS]
    save_employees(df)
    return jsonify({"saved": len(df)})


@flask_app.route("/api/employees/export")
def api_emp_export():
    try:
        df = load_employees()
        df_out = df.copy()
        for col in EMP_COLS:
            if col not in df_out.columns:
                df_out[col] = ""
        df_out = df_out[EMP_COLS]
        df_out.columns = EMP_COL_LABELS

        # --- Training exam summary sheet ---
        conn_ex = sqlite3.connect(DB_EXAM, timeout=10)
        rows_ex = conn_ex.execute(
            "SELECT r.employee_id, s.name, s.exam_type, r.passed, r.taken_at "
            "FROM exam_results r JOIN exam_sets s ON s.id=r.set_id ORDER BY r.taken_at"
        ).fetchall()
        conn_ex.close()
        df_train = pd.DataFrame(rows_ex,
            columns=["Employee ID","ชื่อชุดข้อสอบ","ประเภท","ผ่าน (1=ผ่าน)","วันที่สอบ"])
        df_train["ผ่าน (1=ผ่าน)"] = df_train["ผ่าน (1=ผ่าน)"].map({1:"ผ่าน",0:"ไม่ผ่าน"})

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_out.to_excel(writer, index=False, sheet_name="Employees")
            df_train.to_excel(writer, index=False, sheet_name="Training History")
        buf.seek(0)

        from flask import make_response
        resp = make_response(buf.read())
        resp.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        resp.headers["Content-Disposition"] = "attachment; filename=employees.xlsx"
        return resp
    except Exception as e:
        log.error(f"Export error: {e}")
        return f"Export error: {e}", 500


@flask_app.route("/api/employees/import", methods=["POST"])
def api_emp_import():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    try:
        raw = f.read()
        import io as _io
        xl = pd.ExcelFile(_io.BytesIO(raw), engine="openpyxl")

        # --- Sheet 1: Employees ---
        df_in = xl.parse(xl.sheet_names[0], dtype=str).fillna("")
        label_to_key = dict(zip(EMP_COL_LABELS, EMP_COLS))
        df_in.columns = [label_to_key.get(str(c).strip(), str(c).strip()) for c in df_in.columns]
        for col in EMP_COLS:
            if col not in df_in.columns:
                df_in[col] = ""
        df_in = df_in[EMP_COLS]
        save_employees(df_in)
        imported = len(df_in)

        # --- Sheet 2: Training History (optional) ---
        train_imported = 0
        if len(xl.sheet_names) > 1:
            df_tr = xl.parse(xl.sheet_names[1], dtype=str).fillna("")
            df_tr.columns = [str(c).strip() for c in df_tr.columns]
            # expected cols: Employee ID, ชื่อชุดข้อสอบ, ประเภท, ผ่าน (1=ผ่าน), วันที่สอบ
            if "Employee ID" in df_tr.columns and "ชื่อชุดข้อสอบ" in df_tr.columns:
                conn_ex = sqlite3.connect(DB_EXAM, timeout=10)
                for _, row in df_tr.iterrows():
                    eid   = str(row.get("Employee ID","")).strip()
                    sname = str(row.get("ชื่อชุดข้อสอบ","")).strip()
                    etype = str(row.get("ประเภท","")).strip().lower()
                    p_raw = str(row.get("ผ่าน (1=ผ่าน)","")).strip()
                    passed = 1 if p_raw in ("ผ่าน","1","true") else 0
                    taken  = str(row.get("วันที่สอบ","")).strip()
                    if not eid or not sname: continue
                    # find set_id
                    s_row = conn_ex.execute(
                        "SELECT id FROM exam_sets WHERE name=? AND exam_type=?", (sname, etype)
                    ).fetchone()
                    if not s_row: continue
                    sid = s_row[0]
                    # insert if not duplicate
                    exist = conn_ex.execute(
                        "SELECT 1 FROM exam_results WHERE employee_id=? AND set_id=? AND taken_at=?",
                        (eid, sid, taken)
                    ).fetchone()
                    if not exist:
                        conn_ex.execute(
                            "INSERT INTO exam_results (employee_id,set_id,score,total,passed,taken_at) VALUES (?,?,?,?,?,?)",
                            (eid, sid, passed, 1, passed, taken or None)
                        )
                        train_imported += 1
                conn_ex.commit()
                conn_ex.close()

        log.info(f"Import OK: {imported} emp rows, {train_imported} training rows")
        return jsonify({"imported": imported, "training_imported": train_imported})
    except Exception as e:
        log.error(f"Import error: {e}")
        return jsonify({"error": str(e)}), 500


# ----------------------------------------------
#  FLASK ROUTES — MESSAGES
# ----------------------------------------------
@flask_app.route("/api/messages", methods=["GET"])
def api_msg_get():
    conn = sqlite3.connect(DB_MSG, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, author, content, done, created_at, done_at, priority FROM messages ORDER BY id DESC"
        ).fetchall()
        result = []
        for r in rows:
            msg_id = r[0]
            cmts = conn.execute(
                "SELECT id, author, content, created_at FROM message_comments WHERE message_id=? ORDER BY id ASC",
                (msg_id,)
            ).fetchall()
            comments = [
                {"id": c[0], "author": c[1], "content": c[2], "created_at": c[3]}
                for c in cmts
            ]
            result.append({
                "id": msg_id, "author": r[1] or "", "content": r[2],
                "done": bool(r[3]), "created_at": r[4], "done_at": r[5],
                "priority": r[6] or "",
                "comments": comments
            })
        return jsonify(result)
    finally:
        conn.close()


@flask_app.route("/api/messages", methods=["POST"])
def api_msg_post():
    data = request.get_json(force=True)
    content = (data.get("content") or "").strip()
    author  = (data.get("author")  or "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    conn = sqlite3.connect(DB_MSG, timeout=10)
    try:
        conn.execute("INSERT INTO messages (author, content) VALUES (?, ?)", (author, content))
        conn.commit()
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info(f"Message added id={msg_id} author={author}")
        return jsonify({"id": msg_id})
    finally:
        conn.close()


@flask_app.route("/api/messages/<int:msg_id>", methods=["DELETE"])
def api_msg_delete(msg_id):
    conn = sqlite3.connect(DB_MSG, timeout=10)
    try:
        conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))
        conn.commit()
        log.info(f"Message deleted id={msg_id}")
        return jsonify({"deleted": msg_id})
    finally:
        conn.close()


@flask_app.route("/api/messages/<int:msg_id>/done", methods=["POST"])
def api_msg_done(msg_id):
    from datetime import datetime
    done_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_MSG, timeout=10)
    try:
        conn.execute(
            "UPDATE messages SET done=1, done_at=? WHERE id=?",
            (done_at, msg_id)
        )
        conn.commit()
        log.info(f"Message done id={msg_id}")
        return jsonify({"done": msg_id, "done_at": done_at})
    finally:
        conn.close()


@flask_app.route("/api/messages/<int:msg_id>/priority", methods=["POST"])
def api_msg_priority(msg_id):
    data = request.get_json(force=True)
    priority = (data.get("priority") or "").strip().upper()
    if priority not in ("K", "P", "G", ""):
        return jsonify({"error": "priority must be K, P, G, or empty string"}), 400
    conn = sqlite3.connect(DB_MSG, timeout=10)
    try:
        conn.execute("UPDATE messages SET priority=? WHERE id=?", (priority, msg_id))
        conn.commit()
        log.info(f"Message priority set id={msg_id} priority={priority!r}")
        return jsonify({"id": msg_id, "priority": priority})
    finally:
        conn.close()


@flask_app.route("/api/messages/<int:msg_id>/comments", methods=["POST"])
def api_msg_comment_post(msg_id):
    data    = request.get_json(force=True)
    author  = (data.get("author")  or "").strip()
    content = (data.get("content") or "").strip()
    if not author:
        return jsonify({"error": "author required"}), 400
    if not content:
        return jsonify({"error": "content required"}), 400
    conn = sqlite3.connect(DB_MSG, timeout=10)
    try:
        # ตรวจว่า message มีอยู่จริง
        exists = conn.execute("SELECT id FROM messages WHERE id=?", (msg_id,)).fetchone()
        if not exists:
            return jsonify({"error": f"message {msg_id} not found"}), 404
        conn.execute(
            "INSERT INTO message_comments (message_id, author, content) VALUES (?, ?, ?)",
            (msg_id, author, content)
        )
        conn.commit()
        cmt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info(f"Comment added id={cmt_id} on message={msg_id} author={author}")
        return jsonify({"id": cmt_id})
    finally:
        conn.close()


_photo_cache: dict = {}   # emp_id → bytes
_photo_index: dict = {}   # emp_id → full file path  (built once at startup)

def build_photo_index():
    """scan PHOTO_DIR ครั้งเดียวตอน startup สร้าง index emp_id → path"""
    global _photo_index
    if not os.path.isdir(PHOTO_DIR):
        log.warning(f"PHOTO_DIR ไม่พบ: {PHOTO_DIR}")
        return
    idx = {}
    for fname in os.listdir(PHOTO_DIR):
        file_id = fname[:6].strip()
        if file_id:
            idx[file_id] = os.path.join(PHOTO_DIR, fname)
    _photo_index = idx
    log.info(f"Photo index built: {len(idx)} files")


@flask_app.route("/api/emp_photo/<emp_id>")
def api_emp_photo(emp_id):
    emp_id = emp_id.strip().zfill(6)

    # memory cache hit
    if emp_id in _photo_cache:
        cached = _photo_cache[emp_id]
        from flask import make_response
        resp = make_response(cached['data'])
        resp.headers["Content-Type"]  = cached['mime']
        resp.headers["Cache-Control"] = "max-age=3600"
        return resp

    # ถ้าหาใน index ไม่เจอ ให้ re-scan ก่อน (รองรับรูปที่เพิ่มหลัง startup)
    if emp_id not in _photo_index:
        build_photo_index()

    fpath = _photo_index.get(emp_id)
    if not fpath:
        return jsonify({"error": f"ไม่พบรูปสำหรับรหัส {emp_id}"}), 404

    ext  = os.path.splitext(fpath)[1].lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",  ".gif": "image/gif",
            ".bmp": "image/bmp",  ".webp": "image/webp"}.get(ext, "image/jpeg")
    with open(fpath, "rb") as f:
        data = f.read()
    _photo_cache[emp_id] = {'data': data, 'mime': mime}

    from flask import make_response
    resp = make_response(data)
    resp.headers["Content-Type"]  = mime
    resp.headers["Cache-Control"] = "max-age=3600"
    return resp


@flask_app.route("/api/emp_photo/refresh", methods=["POST"])
def api_photo_refresh():
    """clear cache และ re-scan index"""
    global _photo_cache
    _photo_cache = {}
    build_photo_index()
    return jsonify({"refreshed": len(_photo_index)})


# /app.js route removed - TAB_JS_CODE is now inline in HTML


# ==============================================================
#  MACHINE PROBLEM DB  (Tab 4)
# ==============================================================
MC_COLS       = ["id", "machine_name", "problem", "occurred_at",
                 "status", "completed_at", "remark"]
MC_COL_LABELS = ["ID", "ชื่อเครื่อง", "ปัญหา", "วันที่เกิด",
                 "สถานะ", "วันที่เสร็จ", "หมายเหตุ"]
MC_STATUSES   = ["Stop", "Running & Still broken", "OK"]

def init_mc_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_MC, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS machine_problems (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_name  TEXT NOT NULL,
            problem       TEXT NOT NULL,
            occurred_at   TEXT DEFAULT (datetime('now','localtime')),
            status        TEXT DEFAULT 'Stop',
            completed_at  TEXT,
            remark        TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS machines (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL UNIQUE,
            machine_type TEXT DEFAULT ''
        )
    """)
    # migrate: add machine_type if missing
    existing = [r[1] for r in conn.execute("PRAGMA table_info(machines)").fetchall()]
    if "machine_type" not in existing:
        conn.execute("ALTER TABLE machines ADD COLUMN machine_type TEXT DEFAULT ''")
    conn.commit()
    conn.close()
    log.info("Machine problem DB initialized")


# ==============================================================
#  MATERIAL DB  (Tab 5)
# ==============================================================
MAT_COLS       = ["id", "part_number", "name", "mat_category", "material_type", "size_x", "size_y", "thickness"]
MAT_COL_LABELS = ["ID", "Part Number", "ชื่อวัสดุ", "หมวด", "ประเภท", "Size X (mm)", "Size Y (mm)", "Thickness (mm)"]
MAT_CATEGORIES = ["Entry", "Backup"]
MAT_ENTRY_TYPES  = ["Alumi", "Coated", "No Coated", "2Side (Composite)"]
MAT_BACKUP_TYPES = ["Urea"]
MAT_TYPES        = MAT_ENTRY_TYPES + MAT_BACKUP_TYPES   # backward compat

def init_mat_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_MAT, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS materials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            part_number   TEXT NOT NULL UNIQUE,
            name          TEXT NOT NULL,
            mat_category  TEXT DEFAULT 'Entry',
            material_type TEXT DEFAULT '',
            size_x        REAL DEFAULT 0,
            size_y        REAL DEFAULT 0,
            thickness     REAL DEFAULT 0
        )
    """)
    # migrate old columns
    existing = [row[1] for row in conn.execute("PRAGMA table_info(materials)").fetchall()]
    for col, default in [("material_type", "''"), ("mat_category", "'Entry'")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE materials ADD COLUMN {col} TEXT DEFAULT {default}")
    # product_pn: new schema with process_code + product dimensions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_pn (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            pn             TEXT NOT NULL UNIQUE,
            name           TEXT DEFAULT '',
            entry_id       INTEGER REFERENCES materials(id),
            backup_id      INTEGER REFERENCES materials(id),
            process_code   TEXT DEFAULT '',
            prod_width     REAL DEFAULT 0,
            prod_length    REAL DEFAULT 0,
            prod_thickness REAL DEFAULT 0,
            stk            TEXT DEFAULT ''
        )
    """)
    # migrate product_pn columns
    ppn_existing = [r[1] for r in conn.execute("PRAGMA table_info(product_pn)").fetchall()]
    # rename legacy alumi/urea columns
    for old_c, new_c in [("alumi_id","entry_id"),("urea_id","backup_id")]:
        if old_c in ppn_existing and new_c not in ppn_existing:
            conn.execute(f"ALTER TABLE product_pn RENAME COLUMN {old_c} TO {new_c}")
            ppn_existing = [r[1] for r in conn.execute("PRAGMA table_info(product_pn)").fetchall()]
    # add new columns if missing
    for col_def in [
        ("process_code",   "TEXT",    "''"),
        ("stk",            "TEXT",    "''"),
        ("prod_width",     "REAL",    "0"),
        ("prod_length",    "REAL",    "0"),
        ("prod_thickness", "REAL",    "0"),
    ]:
        if col_def[0] not in ppn_existing:
            conn.execute(f"ALTER TABLE product_pn ADD COLUMN {col_def[0]} {col_def[1]} DEFAULT {col_def[2]}")
    conn.commit()
    conn.close()
    log.info("Material DB initialized")


# ==============================================================
#  EXAM DB  (Tab 3 Section 3)
# ==============================================================
def init_exam_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exam_sets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            exam_type   TEXT DEFAULT 'theory',
            random_n    INTEGER DEFAULT 10,
            pass_score  INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # migrate: เพิ่ม pass_score สำหรับ DB เก่า
    existing_sets = [r[1] for r in conn.execute("PRAGMA table_info(exam_sets)").fetchall()]
    if "pass_score" not in existing_sets:
        conn.execute("ALTER TABLE exam_sets ADD COLUMN pass_score INTEGER DEFAULT 0")
        conn.execute("UPDATE exam_sets SET pass_score = CAST(ROUND(random_n * 0.7) AS INTEGER) WHERE pass_score = 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exam_questions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id    INTEGER NOT NULL REFERENCES exam_sets(id) ON DELETE CASCADE,
            question  TEXT NOT NULL,
            choice_a  TEXT DEFAULT '',
            choice_b  TEXT DEFAULT '',
            choice_c  TEXT DEFAULT '',
            choice_d  TEXT DEFAULT '',
            answer    TEXT DEFAULT 'a',
            -- operate exam fields (used when exam_type='operate')
            op_item     TEXT DEFAULT '',
            op_criteria TEXT DEFAULT ''
        )
    """)
    # migrate: add operate columns if old DB
    existing_q = [r[1] for r in conn.execute("PRAGMA table_info(exam_questions)").fetchall()]
    for col in ["op_item", "op_criteria"]:
        if col not in existing_q:
            conn.execute(f"ALTER TABLE exam_questions ADD COLUMN {col} TEXT DEFAULT ''")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exam_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            set_id      INTEGER NOT NULL REFERENCES exam_sets(id),
            score       INTEGER DEFAULT 0,
            total       INTEGER DEFAULT 0,
            passed      INTEGER DEFAULT 0,
            taken_at    TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operate_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            set_id      INTEGER NOT NULL REFERENCES exam_sets(id),
            question_id INTEGER NOT NULL,
            op_passed   INTEGER DEFAULT 0,
            comment     TEXT DEFAULT '',
            taken_at    TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # theory_answers: per-question answer storage for theory exams
    conn.execute("""
        CREATE TABLE IF NOT EXISTS theory_answers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id   INTEGER NOT NULL REFERENCES exam_results(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL,
            question    TEXT DEFAULT '',
            answered    TEXT DEFAULT '',
            correct_ans TEXT DEFAULT '',
            is_correct  INTEGER DEFAULT 0
        )
    """)
    # training_videos table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS training_videos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            filename   TEXT NOT NULL,
            added_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()
    log.info("Exam DB initialized")


# ==============================================================
#  SETTINGS DB
# ==============================================================
SETTING_TABS = [
    ("msg",     "💬 1. Message"),
    ("overall", "📊 2. Overall"),
    ("emp",     "👥 3. Employee"),
    ("mc",      "🔧 4. Machine"),
    ("mat",     "📦 5. Material"),
]

def init_settings_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_SETTINGS, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)
    for tab_id, _ in SETTING_TABS:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (f"tab_disabled_{tab_id}", "0")
        )
    conn.commit()
    conn.close()
    log.info("Settings DB initialized")


def init_upload_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_UPLOAD, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS upload_paths (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            path       TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()
    log.info("Upload paths DB initialized")


def get_disabled_tabs() -> list:
    try:
        conn = sqlite3.connect(DB_SETTINGS, timeout=5)
        rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'tab_disabled_%'").fetchall()
        conn.close()
        return [r[0].replace("tab_disabled_", "") for r in rows if r[1] == "1"]
    except Exception:
        return []


TAB45_CSS = """
/* ========== GLOBAL FILTER BUTTONS (btn-flt / active-flt) ========== */
.btn-flt {
    padding:5px 13px; font-size:12px; font-weight:600; cursor:pointer;
    border:1.5px solid var(--border); border-radius:20px;
    background:var(--surface2); color:var(--text-dim);
    transition:background .12s, color .12s, border-color .12s;
}
.btn-flt:hover    { border-color:var(--accent); color:var(--accent); }
.btn-flt.active-flt {
    background:var(--accent); color:#fff; border-color:var(--accent);
}

/* ========== MACHINE STAT CARDS ========== */
.mc-stats {
    display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px;
}
.mc-stat-card {
    flex:1; min-width:110px; padding:12px 16px; border-radius:10px;
    text-align:center; font-weight:700;
}
.mc-stat-card .mc-stat-num { font-size:28px; line-height:1.1; }
.mc-stat-card .mc-stat-lbl {
    font-size:11px; text-transform:uppercase;
    letter-spacing:.5px; margin-top:2px;
}
.mc-stat-stop  { background:#fde8e7; color:#c0392b; border:1px solid #e74c3c; }
.mc-stat-run   { background:#fef4e0; color:#b7770d; border:1px solid #f39c12; }
.mc-stat-ok    { background:#eafaf1; color:#1a7a40; border:1px solid #27ae60; }
.mc-stat-total { background:#eef2f7; color:#1a1a2e; border:1px solid var(--border); }

/* ========== MACHINE PROBLEM TABLE ========== */
.mc-toolbar {
    display:flex; gap:10px; align-items:center;
    flex-wrap:wrap; margin-bottom:14px;
}
.mc-toolbar input[type=text] {
    padding:6px 12px; border:1px solid var(--border);
    border-radius:6px; font-size:13px; width:200px;
    background:var(--surface); color:var(--text);
}
.mc-toolbar input:focus { outline:2px solid var(--accent); }

#mc-table-wrap { overflow-x:auto; border:1px solid var(--border);
    border-radius:var(--radius); background:var(--surface); }
#mc-table { border-collapse:separate; border-spacing:0; width:100%; font-size:12px; }
#mc-table col.col-id        { width:44px; }
#mc-table col.col-machine   { width:120px; }
#mc-table col.col-problem   { width:260px; }
#mc-table col.col-occurred  { width:130px; }
#mc-table col.col-status    { width:150px; }
#mc-table col.col-completed { width:120px; }
#mc-table col.col-remark    { width:160px; }
#mc-table col.col-actions   { width:136px; }
.mc-action-cell { display:flex; gap:3px; align-items:center; justify-content:flex-start; }
#mc-table thead th {
    position:sticky; top:0; z-index:10;
    background:var(--thead-bg); color:var(--thead-fg);
    padding:7px 10px; font-size:11.5px; white-space:nowrap;
    border-bottom:2px solid var(--accent);
    border-right:1px solid rgba(255,255,255,.15);
}
#mc-table thead th:last-child { border-right:none; }
#mc-table tbody td {
    padding:5px 8px; border-bottom:1px solid var(--border);
    border-right:1px solid var(--border);
    vertical-align:top; word-break:break-word;
}
#mc-table tbody td:last-child { border-right:none; }
#mc-table tbody tr:nth-child(even) { background:var(--surface2); }
#mc-table tbody tr:hover td { background:#ddeeff !important; }

/* status badges */
.mc-status-badge {
    display:inline-block; padding:2px 10px; border-radius:12px;
    font-size:11px; font-weight:700; white-space:nowrap;
}
.mc-status-Stop { background:#fde8e7; color:#c0392b; border:1px solid #e74c3c; }
.mc-status-Run  { background:#fef4e0; color:#b7770d; border:1px solid #f39c12; }
.mc-status-OK   { background:#eafaf1; color:#1a7a40; border:1px solid #27ae60; }

/* inline action buttons */
.btn-mc-done {
    background:#27ae60; color:#fff; font-size:10px;
    padding:3px 8px; border:none; border-radius:5px; cursor:pointer; white-space:nowrap;
}
.btn-mc-del {
    background:#e74c3c; color:#fff; font-size:10px;
    padding:3px 7px; border:none; border-radius:5px; cursor:pointer; white-space:nowrap;
}
.btn-mc-edit {
    background:#f39c12; color:#fff; font-size:10px;
    padding:3px 7px; border:none; border-radius:5px; cursor:pointer; white-space:nowrap;
}
.btn-mc-done:hover { filter:brightness(1.12); }
.btn-mc-del:hover  { filter:brightness(1.12); }
.btn-mc-edit:hover { filter:brightness(1.12); }

/* ========== EMPLOYEE — TRAINING COLUMNS ========== */
.train-cell { min-width:110px; vertical-align:middle; }
.train-pass {
    display:inline-block; font-size:10px; font-weight:700;
    color:#1a7a40; background:#eafaf1; border:1px solid #a8ddb5;
    border-radius:10px; padding:1px 7px; margin-bottom:2px;
    white-space:nowrap;
}
.train-none { font-size:12px; color:#bbb; }
.train-date-input {
    display:block; width:100%; font-size:10px; margin-top:2px;
    padding:2px 4px; border:1px solid #ccc; border-radius:4px;
    background:var(--surface2); color:var(--text); cursor:pointer;
}
.train-date-input:focus { outline:2px solid var(--accent); }
#emp-table th[data-sortkey^="train"] {
    background:#e8f5e9 !important; color:#1a5c2a !important; font-size:10.5px !important;
}
/* Exam result cell */
.exam-cell { min-width:110px; vertical-align:middle; text-align:center; }
.exam-score-badge {
    display:inline-block; font-size:10px; font-weight:700;
    padding:2px 7px; border-radius:10px; white-space:nowrap;
}
.exam-pass  { background:#eafaf1; color:#1a7a40; border:1px solid #a8ddb5; }
.exam-fail  { background:#fde8e7; color:#c0392b; border:1px solid #e74c3c; }
.exam-none  { color:#bbb; font-size:12px; }
.btn-exam-view {
    display:block; margin-top:3px; width:100%;
    font-size:10px; padding:2px 6px; border:1px solid var(--accent);
    border-radius:5px; cursor:pointer; background:var(--surface);
    color:var(--accent); font-family:inherit;
}
.btn-exam-view:hover { background:var(--accent); color:#fff; }

/* ========== FREEZE COLUMNS — Employee Table ========== */
#emp-table-wrap { overflow-x:auto; overflow-y:auto; max-height:70vh; }
#emp-table { table-layout:fixed; border-collapse:separate; border-spacing:0; }
/* Freeze ALL headers (sticky top) */
#emp-table thead th {
    position:sticky; top:0; z-index:10;
    background:var(--thead-bg); color:var(--thead-fg);
}
/* Freeze left columns on top of sticky header */
#emp-table th.freeze, #emp-table td.freeze {
    position:sticky; z-index:5; background:var(--surface);
}
#emp-table thead th.freeze { z-index:20; background:var(--thead-bg); color:var(--thead-fg); }
#emp-table tbody tr:nth-child(even) td.freeze { background:var(--surface2); }
#emp-table tr:hover td.freeze { background:#ddeeff !important; }
/* freeze-0 = no, freeze-1 = name, freeze-2 = employee_id */
#emp-table .freeze-0 { left:0px; }
#emp-table .freeze-1 { left:44px; border-right:2px solid var(--accent) !important; }
#emp-table .freeze-2 { left:184px; border-right:2px solid var(--accent) !important; }

/* ========== MACHINE STATUS GRID — COMPACT 200+ ========== */
.mc-type-group {
    break-inside: avoid;
    margin-bottom:10px;
    background:var(--surface2);
    border:1px solid var(--border);
    border-radius:8px;
    padding:8px 10px;
    display:inline-block;
    width:100%;
    box-sizing:border-box;
}
.mc-type-group-title {
    font-size:11px; font-weight:700; color:var(--text-dim);
    letter-spacing:.5px; text-transform:uppercase;
    border-bottom:1px solid var(--border); padding-bottom:4px; margin-bottom:6px;
}
#mc-status-grid {
    column-count: 3;
    column-gap: 8px;
    margin-top:4px;
}
@media (min-width:1400px) { #mc-status-grid { column-count:4; } }
@media (max-width:900px)  { #mc-status-grid { column-count:2; } }
.mc-status-card {
    border-radius:6px; padding:5px 8px;
    border:1.5px solid transparent;
    box-shadow:0 1px 2px rgba(0,0,0,.06);
}
.mc-card-stop { background:#fde8e7; border-color:#e74c3c; }
.mc-card-run  { background:#fef4e0; border-color:#f39c12; }
.mc-card-ok   { background:#eafaf1; border-color:#b2dfdb; }
.mc-card-head { display:flex; align-items:center; gap:4px; }
.mc-card-icon { font-size:11px; line-height:1; flex-shrink:0; }
.mc-card-name {
    font-size:11px; font-weight:800; flex:1;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}
.mc-card-status-lbl {
    font-size:8px; font-weight:700; padding:1px 4px;
    border-radius:4px; white-space:nowrap; flex-shrink:0;
}
.mc-card-stop .mc-card-status-lbl { background:#e74c3c; color:#fff; }
.mc-card-run  .mc-card-status-lbl { background:#f39c12; color:#fff; }
.mc-card-ok   .mc-card-status-lbl { display:none; }  /* OK: no label needed */
.mc-card-type-tag { font-size:9px; color:var(--text-dim); margin-bottom:3px; font-style:italic; }
.mc-card-problems { display:flex; flex-direction:column; gap:2px; margin-top:4px; }
.mc-card-prob-row { display:flex; gap:3px; align-items:flex-start; }
.mc-prob-dot {
    width:5px; height:5px; border-radius:50%; flex-shrink:0; margin-top:3px;
}
.mc-prob-dot-stop { background:#e74c3c; }
.mc-prob-dot-run  { background:#f39c12; }
.mc-card-prob-text {
    font-size:10px; line-height:1.3;
    overflow:hidden; display:-webkit-box;
    -webkit-line-clamp:2; -webkit-box-orient:vertical;
}

/* ========== MAT TYPE SELECT IN TABLE ========== */
.mat-type-sel {
    width:100%; border:1px solid var(--border);
    background:var(--surface2); color:inherit;
    font-size:11px; padding:3px 4px; border-radius:4px; font-family:inherit;
}
.mat-type-sel:focus { outline:2px solid var(--accent); }
#mat-table col.col-type { width:120px; }
#mat-table col.col-cat  { width:80px; }

/* ========== PRODUCT PN TABLE ========== */
#ppn-table-wrap { overflow-x:auto; border:1px solid var(--border);
    border-radius:var(--radius); background:var(--surface); }
#ppn-table { border-collapse:separate; border-spacing:0; width:auto; min-width:100%; font-size:12px; }
#ppn-table thead th {
    position:sticky; top:0; z-index:10;
    background:var(--thead-bg); color:var(--thead-fg);
    padding:6px 8px; font-size:11px; white-space:nowrap;
    border-bottom:2px solid var(--accent);
    border-right:1px solid rgba(255,255,255,.15);
}
#ppn-table thead th:last-child { border-right:none; }
#ppn-table tbody td {
    padding:4px 6px; border-bottom:1px solid var(--border);
    border-right:1px solid var(--border); white-space:nowrap;
}
#ppn-table tbody td:last-child { border-right:none; }
#ppn-table tbody tr:nth-child(even) { background:var(--surface2); }
#ppn-table tbody tr:hover td { background:#ddeeff !important; }
/* compact inputs inside ppn table */
.ppn-input-xs  { width:64px !important; min-width:64px; }
.ppn-input-sm  { width:110px !important; min-width:80px; }
.ppn-input-md  { width:160px !important; min-width:120px; }
.ppn-sel       { min-width:160px; max-width:220px; font-size:11px !important; }

/* ========== TRAINING VIDEO ========== */
.video-grid {
    display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
    gap:14px; margin-top:10px;
}
.video-card {
    background:var(--surface2); border:1px solid var(--border);
    border-radius:10px; padding:14px; text-align:center;
}
.video-card video { width:100%; border-radius:6px; max-height:140px; background:#000; }
.video-card-title { font-size:13px; font-weight:700; margin-top:8px; }
.video-card-del {
    margin-top:6px; font-size:11px; cursor:pointer;
    background:#fde8e7; border:1px solid #e74c3c; color:#c0392b;
    border-radius:5px; padding:2px 10px;
}

/* ========== EXAM SECTION ========== */
.exam-set-card {
    background:var(--surface2); border:1px solid var(--border);
    border-radius:10px; padding:14px; margin-bottom:12px;
}
.exam-set-head {
    display:flex; justify-content:space-between; align-items:center;
    margin-bottom:8px; flex-wrap:wrap; gap:6px;
}
.exam-set-title { font-size:14px; font-weight:700; }
.exam-type-badge { font-size:10px; font-weight:700; padding:2px 10px; border-radius:10px; }
.badge-theory  { background:#e3f2fd; color:#1565c0; border:1px solid #90caf9; }
.badge-operate { background:#f3e5f5; color:#6a1b9a; border:1px solid #ce93d8; }
.btn-add-q {
    font-size:11px; padding:3px 10px; border-radius:5px;
    border:1px solid var(--accent); color:var(--accent);
    background:var(--surface); cursor:pointer;
}
.btn-add-q:hover { background:var(--accent); color:#fff; }
.q-row {
    border:1px solid var(--border); border-radius:6px;
    padding:8px; background:var(--surface); margin-bottom:8px;
}
.q-row textarea {
    width:100%; resize:vertical; font-size:12px;
    border:1px solid var(--border); border-radius:4px;
    padding:4px 6px; font-family:inherit; background:var(--surface2); color:var(--text);
}
.q-choices { display:grid; grid-template-columns:1fr 1fr; gap:4px; margin-top:4px; }
.q-choices input { font-size:11px; padding:3px 6px; border:1px solid var(--border);
    border-radius:4px; width:100%; background:var(--surface2); color:var(--text); }
.q-answer-row { margin-top:4px; font-size:11px; display:flex; align-items:center; gap:6px; }
.q-answer-row select { font-size:11px; border:1px solid var(--border);
    border-radius:4px; background:var(--surface2); color:var(--text); }

/* Exam modal */
.exam-modal-q { margin-bottom:16px; }
.exam-modal-q .q-text { font-size:14px; font-weight:600; margin-bottom:8px; }
.exam-choice-btn {
    display:block; width:100%; text-align:left;
    padding:8px 14px; margin-bottom:5px; border-radius:7px;
    border:1.5px solid var(--border); background:var(--surface2);
    cursor:pointer; font-size:13px; font-family:inherit; color:var(--text);
}
.exam-choice-btn:hover  { background:#ddeeff; border-color:var(--accent); }
.exam-choice-btn.picked { background:#1565c0; color:#fff; border-color:#1565c0; }
.exam-choice-btn.correct { background:#27ae60; color:#fff; border-color:#27ae60; }
.exam-choice-btn.wrong   { background:#e74c3c; color:#fff; border-color:#e74c3c; }

/* ========== OPERATE EXAM ========== */
.op-q-row {
    border:1px solid var(--border); border-radius:8px;
    padding:10px 12px; background:var(--surface); transition:background .15s;
}
.op-pass-btn, .op-fail-btn {
    padding:5px 16px; border-radius:6px; border:1.5px solid;
    font-size:12px; font-weight:700; cursor:pointer; font-family:inherit;
    background:var(--surface2); transition:background .12s, color .12s;
}
.op-pass-btn { border-color:#27ae60; color:#27ae60; }
.op-fail-btn { border-color:#e74c3c; color:#e74c3c; }
.op-pass-btn:hover { background:#27ae60; color:#fff; }
.op-fail-btn:hover { background:#e74c3c; color:#fff; }
.op-comment-input {
    flex:1; min-width:140px; padding:5px 9px; font-size:12px;
    border:1px solid var(--border); border-radius:6px;
    background:var(--surface2); color:var(--text); font-family:inherit;
}
.op-comment-input:focus { outline:2px solid var(--accent); }

/* ========== SETTINGS TAB ========== */
#settings-lock-screen {
    display:flex; flex-direction:column; align-items:center;
    justify-content:center; min-height:340px; gap:16px;
}
.settings-lock-icon { font-size:56px; }
.settings-lock-title { font-size:20px; font-weight:700; color:var(--accent); }
.settings-lock-sub { font-size:13px; color:var(--text-dim); }
.settings-lock-input {
    padding:10px 18px; font-size:16px; border:2px solid var(--border);
    border-radius:8px; text-align:center; letter-spacing:4px; width:260px;
    background:var(--surface2); color:var(--text); outline:none;
}
.settings-lock-input:focus { border-color:var(--accent); }
.settings-lock-err { color:#e74c3c; font-size:13px; min-height:18px; }
.settings-content { display:none; }
.settings-content.unlocked { display:block; }
.settings-section-title {
    font-size:16px; font-weight:700; color:var(--accent);
    border-left:5px solid var(--accent); padding:6px 14px;
    background:var(--surface2); border-radius:0 8px 8px 0;
    margin-bottom:16px;
}
.settings-tab-list { display:flex; flex-direction:column; gap:10px; }
.settings-tab-row {
    display:flex; align-items:center; justify-content:space-between;
    padding:12px 18px; background:var(--surface2); border:1px solid var(--border);
    border-radius:10px; transition:background .12s;
}
.settings-tab-row:hover { background:#e8f0f8; }
.settings-tab-label { font-size:14px; font-weight:600; }
.settings-tab-note  { font-size:11px; color:var(--text-dim); margin-top:2px; }
.toggle-wrap { display:flex; align-items:center; gap:10px; }
.toggle-label { font-size:12px; font-weight:600; width:52px; text-align:center; }
.toggle-switch { position:relative; width:52px; height:28px; cursor:pointer; }
.toggle-switch input { opacity:0; width:0; height:0; }
.toggle-slider {
    position:absolute; inset:0; border-radius:28px;
    background:#ccc; transition:.25s; cursor:pointer;
}
.toggle-slider:before {
    content:''; position:absolute; width:22px; height:22px;
    left:3px; bottom:3px; border-radius:50%;
    background:#fff; transition:.25s; box-shadow:0 1px 3px rgba(0,0,0,.25);
}
.toggle-switch input:checked + .toggle-slider { background:var(--accent); }
.toggle-switch input:checked + .toggle-slider:before { transform:translateX(24px); }
.settings-save-row { margin-top:20px; display:flex; gap:10px; align-items:center; }
.settings-saved-msg { font-size:13px; color:#1a7a40; font-weight:600; display:none; }
.tab-btn.tab-disabled {
    opacity:.4; cursor:not-allowed; pointer-events:none;
    text-decoration:line-through;
}
"""

# ---------- Tab-4 JS ----------
TAB4_JS_CODE = r"""
/* ========== TAB 4 — MACHINE PROBLEMS ========== */
var mcData=[], mcMachines=[], mcFilter='open', mcSearchQ='', mcViewMode='table';
function mcNumSort(a,b){
    function p(s){var m=String(s).match(/^([A-Za-z]*)(\d*)(.*)/);return[m[1]||'',parseInt(m[2])||0,m[3]||''];}
    var pa=p(a),pb=p(b);
    if(pa[0]!==pb[0])return pa[0].localeCompare(pb[0]);
    if(pa[1]!==pb[1])return pa[1]-pb[1];
    return pa[2].localeCompare(pb[2]);
}
function loadMachines(){fetch('/api/machines').then(r=>r.json()).then(d=>{mcMachines=d;}).catch(e=>console.error(e));}
function loadMCProblems(){
    fetch('/api/mc_problems').then(r=>r.json()).then(d=>{
        mcData=d; renderMCTable(); updateMCStats(); if(mcViewMode==='status')renderMCStatus();
    }).catch(e=>console.error(e));
}
function updateMCStats(){
    // Count unique machines by their worst status
    var stop=0, run=0, ok=0;
    mcMachines.forEach(function(mc){
        var w=getMcWorst(mc.name);
        if(w==='Stop') stop++;
        else if(w==='Running & Still broken') run++;
        else ok++;
    });
    var total=mcMachines.length;
    var el;
    el=document.getElementById('mc-stat-stop');  if(el)el.textContent=stop;
    el=document.getElementById('mc-stat-run');   if(el)el.textContent=run;
    el=document.getElementById('mc-stat-ok');    if(el)el.textContent=ok;
    el=document.getElementById('mc-stat-total'); if(el)el.textContent=total;
}
function setMCView(mode){
    mcViewMode=mode;
    document.getElementById('mc-view-table').classList.toggle('active-flt',mode==='table');
    document.getElementById('mc-view-status').classList.toggle('active-flt',mode==='status');
    document.getElementById('mc-table-section').style.display=mode==='table'?'':'none';
    document.getElementById('mc-status-section').style.display=mode==='status'?'':'none';
    if(mode==='status')renderMCStatus();
}
function getMcWorst(name){
    var p=mcData.filter(r=>r.machine_name===name&&r.status!=='OK');
    if(p.some(r=>r.status==='Stop'))return'Stop';
    if(p.some(r=>r.status==='Running & Still broken'))return'Running & Still broken';
    return'OK';
}
function getMcPrio(name){var s=getMcWorst(name);return s==='Stop'?0:s==='Running & Still broken'?1:2;}
var mcStatusFilter = {stop:true, run:true, ok:false};  // default: Stop+Broke
function toggleMCStatusFilter(val){
    mcStatusFilter[val] = !mcStatusFilter[val];
    var b = document.getElementById('mcst-'+val);
    if(b) b.classList.toggle('active-flt', mcStatusFilter[val]);
    renderMCStatus();
}
function setMCStatusAll(){
    ['stop','run','ok'].forEach(function(v){
        mcStatusFilter[v]=true;
        var b=document.getElementById('mcst-'+v); if(b)b.classList.add('active-flt');
    });
    renderMCStatus();
}
function renderMCStatus(){
    var wrap=document.getElementById('mc-status-grid');
    if(!wrap)return;
    if(mcMachines.length===0){wrap.innerHTML='<div style="padding:30px;text-align:center;color:#999;">ยังไม่มีเครื่อง</div>';return;}
    var searchQ=((document.getElementById('mc-status-search')||{}).value||'').toLowerCase();
    var numColsEl=document.getElementById('mc-num-cols');
    var numCols=parseInt((numColsEl&&numColsEl.value)||'0')||0;  // 0 = auto (CSS column-count)
    var groups={};
    mcMachines.forEach(function(mc){
        if(searchQ && !mc.name.toLowerCase().includes(searchQ)) return;
        var worst=getMcWorst(mc.name);
        // Multi-select filter
        if(worst==='Stop'                       && !mcStatusFilter.stop) return;
        if(worst==='Running & Still broken'     && !mcStatusFilter.run)  return;
        if(worst==='OK'                         && !mcStatusFilter.ok)   return;
        var t=mc.machine_type||'(ไม่ระบุประเภท)';
        if(!groups[t])groups[t]=[];
        groups[t].push(mc);
    });
    var typeKeys=Object.keys(groups).sort();
    if(typeKeys.length===0){
        wrap.innerHTML='<div style="padding:30px;text-align:center;color:#999;">ไม่มีเครื่องที่ตรงเงื่อนไข</div>';
        return;
    }
    var html='';
    typeKeys.forEach(function(tkey){
        var allMcs=mcMachines.filter(function(mc){ return (mc.machine_type||'(ไม่ระบุประเภท)')=== tkey; });
        var mcs=groups[tkey].slice().sort(function(a,b){
            var pa=getMcPrio(a.name),pb=getMcPrio(b.name);
            return pa!==pb?pa-pb:mcNumSort(a.name,b.name);
        });
        // Count from ALL machines in this type (not just filtered)
        var nTotal=allMcs.length;
        var nStop=allMcs.filter(m=>getMcWorst(m.name)==='Stop').length;
        var nRun =allMcs.filter(m=>getMcWorst(m.name)==='Running & Still broken').length;
        var nOk  =allMcs.filter(m=>getMcWorst(m.name)==='OK').length;
        // Stats: ชื่อประเภท + ทั้งหมดอยู่แถวเดียว, Stop/Broke/OK อยู่บรรทัดล่าง
        var statsRow2='';
        if(nStop) statsRow2+='<span style="color:#c0392b;font-weight:700;">🔴'+nStop+'</span> ';
        if(nRun)  statsRow2+='<span style="color:#b7770d;font-weight:700;">🟡'+nRun+'</span> ';
        statsRow2+='<span style="color:#1a7a40;font-weight:700;">🟢'+nOk+'</span>';

        html+='<div class="mc-type-group">'
            +'<div class="mc-type-group-title">'
            +'<div style="display:flex;justify-content:space-between;align-items:center;">'
            +'<span style="font-size:12px;font-weight:800;color:var(--text);">'+escHtml(tkey)+'</span>'
            +'<span style="font-size:11px;font-weight:600;color:var(--text-dim);">ทั้งหมด <strong style="color:var(--text);">'+nTotal+'</strong></span>'
            +'</div>'
            +'<div style="margin-top:3px;font-size:11px;display:flex;gap:8px;">'+statsRow2+'</div>'
            +'</div>';

        // Cards: numCols>0 = fixed N columns, else auto-fill
        if(numCols>0 && mcs.length>0){
            html+='<div style="display:grid;grid-template-columns:repeat('+numCols+',1fr);gap:4px;">';
        } else {
            html+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:4px;">';
        }
        mcs.forEach(function(mc){ html+=mcCard(mc); });
        html+='</div>';
        html+='</div>';  // close mc-type-group
    });
    wrap.innerHTML=html;
}

function mcCard(mc){
    var worst=getMcWorst(mc.name);
    var ccls=worst==='Stop'?'mc-card-stop':worst==='Running & Still broken'?'mc-card-run':'mc-card-ok';
    var icon=worst==='Stop'?'🔴':worst==='Running & Still broken'?'🟡':'🟢';
    var slbl=worst==='Stop'?'STOP':worst==='Running & Still broken'?'BROKE':'OK';
    var op=mcData.filter(r=>r.machine_name===mc.name&&r.status!=='OK')
        .sort(function(a,b){return(a.status==='Stop'?0:1)-(b.status==='Stop'?0:1);});
    var h='<div class="mc-status-card '+ccls+'">'
        +'<div class="mc-card-head"><span class="mc-card-icon">'+icon+'</span>'
        +'<span class="mc-card-name">'+escHtml(mc.name)+'</span>'
        +'<span class="mc-card-status-lbl">'+slbl+'</span></div>';
    if(op.length>0){
        h+='<div class="mc-card-problems">';
        op.forEach(function(p){
            h+='<div class="mc-card-prob-row">'
                +'<span class="mc-prob-dot '+(p.status==='Stop'?'mc-prob-dot-stop':'mc-prob-dot-run')+'"></span>'
                +'<span class="mc-card-prob-text">'+escHtml(p.problem)+'</span></div>';
        });
        h+='</div>';
    }else{ /* OK machines show nothing extra */ }
    h+='</div>';
    return h;
}
function setMCFilter(val){
    mcFilter=val;
    ['open','ok','all'].forEach(function(v){var b=document.getElementById('mcf-'+v);if(b)b.classList.toggle('active-flt',v===val);});
    renderMCTable();
}
function filterMC(){mcSearchQ=(document.getElementById('mc-search')||{}).value||'';renderMCTable();}
function renderMCTable(){
    var wrap=document.getElementById('mc-table-wrap');
    if(!wrap)return;
    var rows=mcData.filter(function(r){
        if(mcFilter==='open')return r.status!=='OK';
        if(mcFilter==='ok')return r.status==='OK';
        return true;
    });
    if(mcSearchQ){var q=mcSearchQ.toLowerCase();rows=rows.filter(function(r){return(r.machine_name||'').toLowerCase().includes(q)||(r.problem||'').toLowerCase().includes(q)||(r.remark||'').toLowerCase().includes(q);});}
    if(rows.length===0){wrap.innerHTML='<div style="padding:30px;text-align:center;color:#999;">ไม่มีข้อมูล</div>';return;}
    var sc={'Stop':'mc-status-Stop','Running & Still broken':'mc-status-Run','OK':'mc-status-OK'};
    var html='<table id="mc-table"><colgroup><col class="col-id"><col class="col-machine"><col class="col-problem"><col class="col-occurred"><col class="col-status"><col class="col-completed"><col class="col-remark"><col class="col-actions"></colgroup>'
        +'<thead><tr><th>ID</th><th>เครื่อง</th><th>ปัญหา</th><th>วันที่เกิด</th><th>สถานะ</th><th>วันที่เสร็จ</th><th>หมายเหตุ</th><th>Action</th></tr></thead><tbody>';
    rows.forEach(function(r){
        var cls=sc[r.status]||'mc-status-Stop';
        html+='<tr><td style="text-align:center;color:#888;">'+r.id+'</td>'
            +'<td><strong>'+escHtml(r.machine_name||'')+'</strong></td>'
            +'<td style="white-space:pre-wrap;">'+escHtml(r.problem||'')+'</td>'
            +'<td>'+escHtml(r.occurred_at||'')+'</td>'
            +'<td><span class="mc-status-badge '+cls+'">'+escHtml(r.status||'')+'</span></td>'
            +'<td>'+escHtml(r.completed_at||'-')+'</td>'
            +'<td style="white-space:pre-wrap;">'+escHtml(r.remark||'')+'</td>'
            +'<td><div class="mc-action-cell">'+(r.status==='OK'?'':'<button class="btn-mc-done" data-id="'+r.id+'" onclick="markMCDone(this.dataset.id)">✅ เสร็จ</button>')
            +'<button class="btn-mc-edit" data-id="'+r.id+'" onclick="editMCProblem(this.dataset.id)">✏️</button>'
            +'<button class="btn-mc-del" data-id="'+r.id+'" onclick="deleteMCProblem(this.dataset.id)">🗑</button></div></td></tr>';
    });
    wrap.innerHTML=html+'</tbody></table>';
}
function markMCDone(id){openPwModal('✅ ยืนยันว่าแก้ไขเสร็จแล้ว',function(pw){if(pw!==MSG_ADD_DEL_PW)return false;fetch('/api/mc_problems/'+id+'/done',{method:'POST'}).then(function(){loadMCProblems();});return true;});}

function editMCProblem(id){
    var row = mcData.find(function(r){ return String(r.id)===String(id); });
    if(!row) return;
    document.getElementById('edit-prob-id').value       = row.id;
    document.getElementById('edit-prob-problem').value  = row.problem || '';
    document.getElementById('edit-prob-status').value   = row.status  || 'Stop';
    document.getElementById('edit-prob-remark').value   = row.remark  || '';
    document.getElementById('edit-prob-pw').value       = '';
    document.getElementById('edit-prob-err').textContent = '';
    document.getElementById('edit-prob-machine-lbl').textContent = row.machine_name || '';
    document.getElementById('edit-problem-modal').classList.add('open');
    setTimeout(function(){ document.getElementById('edit-prob-problem').focus(); }, 80);
}
function closeEditProblemModal(){ document.getElementById('edit-problem-modal').classList.remove('open'); }
function submitEditProblem(){
    var id      = document.getElementById('edit-prob-id').value;
    var problem = (document.getElementById('edit-prob-problem').value||'').trim();
    var status  = document.getElementById('edit-prob-status').value;
    var remark  = (document.getElementById('edit-prob-remark').value||'').trim();
    var pw      = (document.getElementById('edit-prob-pw').value||'').trim();
    var errEl   = document.getElementById('edit-prob-err');
    if(!problem){ errEl.textContent='❌ กรุณาระบุปัญหา'; return; }
    if(pw!==MSG_ADD_DEL_PW){ errEl.textContent='❌ รหัสไม่ถูกต้อง'; return; }
    fetch('/api/mc_problems/'+id, {
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({problem:problem, status:status, remark:remark})
    })
    .then(r=>r.json())
    .then(function(d){
        if(d.error){ errEl.textContent='❌ '+d.error; return; }
        closeEditProblemModal(); loadMCProblems();
    }).catch(function(e){ errEl.textContent='❌ '+e; });
}
function deleteMCProblem(id){openPwModal('🗑 ยืนยันการลบ',function(pw){if(pw!==MSG_ADD_DEL_PW)return false;fetch('/api/mc_problems/'+id,{method:'DELETE'}).then(function(){loadMCProblems();});return true;});}
function exportMCProblems(){window.location.href='/api/mc_problems/export';}

/* ---- Machines Export / Import ---- */
function exportMachines() { window.location.href='/api/machines/export'; }
function importMachines(input) {
    if (!input.files || !input.files[0]) return;
    var fd = new FormData();
    fd.append('file', input.files[0]);
    fetch('/api/machines/import', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(function(d) {
        if (d.error) { alert('❌ Import เครื่อง: ' + d.error); return; }
        alert('✅ Import เครื่องสำเร็จ ' + d.imported + ' รายการ (ซ้ำ skip: ' + d.skipped + ')');
        loadMachines(); refreshMCSelect();
        if (mcViewMode === 'status') renderMCStatus();
    }).catch(e => alert('❌ ' + e));
    input.value = '';
}

/* ---- MC Problems Import ---- */
function importMCProblems(input) {
    if (!input.files || !input.files[0]) return;
    var fd = new FormData();
    fd.append('file', input.files[0]);
    fetch('/api/mc_problems/import', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(function(d) {
        if (d.error) { alert('❌ Import ปัญหา: ' + d.error); return; }
        alert('✅ Import ปัญหาสำเร็จ ' + d.imported + ' รายการ');
        loadMCProblems();
    }).catch(e => alert('❌ ' + e));
    input.value = '';
}
function openAddMachineModal(){
    ['add-mc-name-input','add-mc-type-input','add-mc-pw'].forEach(function(id){document.getElementById(id).value='';});
    document.getElementById('add-mc-err').textContent='';
    document.getElementById('add-machine-modal').classList.add('open');
    setTimeout(function(){document.getElementById('add-mc-name-input').focus();},80);
}
function closeAddMachineModal(){document.getElementById('add-machine-modal').classList.remove('open');}
function submitAddMachine(){
    var name=(document.getElementById('add-mc-name-input').value||'').trim();
    var mtype=(document.getElementById('add-mc-type-input').value||'').trim();
    var pw=(document.getElementById('add-mc-pw').value||'').trim();
    var e=document.getElementById('add-mc-err');
    if(!name){e.textContent='❌ กรุณาใส่ชื่อเครื่อง';return;}
    if(pw!==MSG_ADD_DEL_PW){e.textContent='❌ รหัสไม่ถูกต้อง';return;}
    fetch('/api/machines',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,machine_type:mtype})})
    .then(r=>r.json()).then(function(d){
        if(d.error){e.textContent='❌ '+d.error;return;}
        closeAddMachineModal();loadMachines();refreshMCSelect();alert('✅ เพิ่ม "'+name+'" เรียบร้อย');
    }).catch(function(err){e.textContent='❌ '+err;});
}
function openAddProblemModal(){
    ['add-prob-problem','add-prob-remark','add-prob-pw'].forEach(function(id){
        var el=document.getElementById(id); if(el)el.value='';
    });
    var errEl=document.getElementById('add-prob-err');
    if(errEl) errEl.textContent='';
    buildProbTypeSelect();
    document.getElementById('add-problem-modal').classList.add('open');
}
function closeAddProblemModal(){document.getElementById('add-problem-modal').classList.remove('open');}

function buildProbTypeSelect(){
    var typeSel=document.getElementById('add-prob-type');
    var mcSel  =document.getElementById('add-prob-machine');
    if(!typeSel||!mcSel) return;
    var types={};
    mcMachines.forEach(function(m){
        var t=m.machine_type||'(ไม่ระบุประเภท)';
        if(!types[t]) types[t]=[];
        types[t].push(m);
    });
    var typeKeys=Object.keys(types).sort();
    typeSel.innerHTML='<option value="">— เลือกประเภทเครื่อง —</option>'
        +typeKeys.map(function(t){
            return'<option value="'+escHtml(t)+'">'+escHtml(t)+' ('+types[t].length+' เครื่อง)</option>';
        }).join('');
    mcSel.innerHTML='<option value="">— เลือกประเภทก่อน —</option>';
}

function onProbTypeChange(){
    var typeSel=document.getElementById('add-prob-type');
    var mcSel  =document.getElementById('add-prob-machine');
    if(!typeSel||!mcSel) return;
    var selType=typeSel.value;
    if(!selType){
        mcSel.innerHTML='<option value="">— เลือกประเภทก่อน —</option>';
        return;
    }
    var filtered=mcMachines.filter(function(m){
        return (m.machine_type||'(ไม่ระบุประเภท)')===selType;
    }).slice().sort(function(a,b){return mcNumSort(a.name,b.name);});
    mcSel.innerHTML='<option value="">— เลือกเครื่อง —</option>'
        +filtered.map(function(m){
            return'<option value="'+escHtml(m.name)+'">'+escHtml(m.name)+'</option>';
        }).join('');
    if(filtered.length===1) mcSel.value=filtered[0].name;
}

function refreshMCSelect(){
    fetch('/api/machines').then(r=>r.json()).then(function(d){
        mcMachines=d;
        buildProbTypeSelect();
    });
}
function submitAddProblem(){
    var sel=document.getElementById('add-prob-machine');
    var machine=sel?sel.value:'';
    var problem=(document.getElementById('add-prob-problem').value||'').trim();
    var status=document.getElementById('add-prob-status').value;
    var remark=(document.getElementById('add-prob-remark').value||'').trim();
    var pw=(document.getElementById('add-prob-pw').value||'').trim();
    var e=document.getElementById('add-prob-err');
    if(!machine){e.textContent='❌ เลือกเครื่องก่อน';return;}
    if(!problem){e.textContent='❌ กรุณาระบุปัญหา';return;}
    if(pw!==MSG_ADD_DEL_PW){e.textContent='❌ รหัสไม่ถูกต้อง';return;}
    fetch('/api/mc_problems',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({machine_name:machine,problem:problem,status:status,remark:remark})})
    .then(r=>r.json()).then(function(d){if(d.error){e.textContent='❌ '+d.error;return;}closeAddProblemModal();loadMCProblems();}).catch(function(err){e.textContent='❌ '+err;});
}
"""

TAB5_JS_CODE = r"""
/* ========== TAB 5 — MATERIALS ========== */
var matData=[], ppnData=[], matTypeFilter='', matCatFilter='';
var ENTRY_TYPES=['Alumi','Coated','No Coated','2Side (Composite)'];
var BACKUP_TYPES=['Urea'];
function loadMaterials(){
    fetch('/api/materials').then(r=>r.json()).then(function(d){matData=d;renderMatTable();}).catch(e=>console.error(e));
    fetch('/api/product_pn').then(r=>r.json()).then(function(d){ppnData=d;renderPPNTable();populateVizSelect();}).catch(e=>console.error(e));
}
function setMatTypeFilter(val){
    matTypeFilter=(matTypeFilter===val)?'':val;
    document.querySelectorAll('.mat-type-btn').forEach(function(b){b.classList.toggle('active-flt',b.dataset.type===matTypeFilter);});
    renderMatTable();
}
function setMatCatFilter(val){
    matCatFilter=(matCatFilter===val)?'':val;
    document.querySelectorAll('.mat-cat-btn').forEach(function(b){b.classList.toggle('active-flt',b.dataset.cat===matCatFilter);});
    updateMatTypeButtons();
    renderMatTable();
}
function updateMatTypeButtons(){
    var types=matCatFilter==='Entry'?ENTRY_TYPES:matCatFilter==='Backup'?BACKUP_TYPES:[...ENTRY_TYPES,...BACKUP_TYPES];
    var tb=document.getElementById('mat-type-btns');
    if(!tb)return;
    tb.innerHTML=types.map(function(t){
        return'<button class="btn-flt mat-type-btn'+(t===matTypeFilter?' active-flt':'')+'" data-type="'+t+'" onclick="setMatTypeFilter(this.dataset.type)">'+t+'</button>';
    }).join('');
}
function filterMat(){renderMatTable();}
function renderMatTable(){
    var wrap=document.getElementById('mat-table-wrap');
    if(!wrap)return;
    var q=((document.getElementById('mat-search')||{}).value||'').toLowerCase();
    var rows=matData.filter(function(r){
        if(matCatFilter&&(r.mat_category||'')!==matCatFilter)return false;
        if(matTypeFilter&&(r.material_type||'')!==matTypeFilter)return false;
        if(q)return(r.part_number||'').toLowerCase().includes(q)||(r.name||'').toLowerCase().includes(q);
        return true;
    });
    if(rows.length===0){wrap.innerHTML='<div style="padding:30px;text-align:center;color:#999;">ไม่มีข้อมูล</div>';return;}
    var catBg={'Entry':'#e3f2fd','Backup':'#fff3e0',''  :'transparent'};
    var typeBg={'Urea':'#fff3e0','Alumi':'#e3f2fd','Coated':'#f3e5f5','No Coated':'#e8f5e9','2Side (Composite)':'#fce4ec',''  :'transparent'};
    var html='<table id="mat-table" style="width:auto;min-width:100%;"><colgroup><col class="col-id"><col class="col-cat"><col class="col-pn"><col class="col-name"><col class="col-type"><col class="col-sx"><col class="col-sy"><col class="col-th"><col class="col-del"></colgroup>'
        +'<thead><tr><th>ID</th><th>หมวด</th><th>Part Number</th><th>ชื่อวัสดุ</th><th>ประเภท</th><th>X(mm)</th><th>Y(mm)</th><th>Thickness</th><th>ลบ</th></tr></thead><tbody>';
    rows.forEach(function(r){
        var ri=matData.indexOf(r);
        var cat=r.mat_category||'Entry';
        var typ=r.material_type||'';
        var catTypes=cat==='Entry'?ENTRY_TYPES:BACKUP_TYPES;
        var catOpt='<option value="">—</option>'+catTypes.map(function(t){return'<option value="'+t+'"'+(t===typ?' selected':'')+'>'+t+'</option>';}).join('');
        html+='<tr style="background:'+typeBg[typ]+';">'
            +'<td style="text-align:center;color:#888;">'+r.id+'</td>'
            +'<td><select class="mat-type-sel" style="background:'+catBg[cat]+';" data-matri="'+ri+'" data-matkey="mat_category" onchange="matCatChanged(this)"><option value="Entry"'+(cat==='Entry'?' selected':'')+'>Entry</option><option value="Backup"'+(cat==='Backup'?' selected':'')+'>Backup</option></select></td>'
            +'<td><input class="cell-input" value="'+escHtml(String(r.part_number||''))+'" data-matri="'+ri+'" data-matkey="part_number" onchange="matCellChanged(this)"></td>'
            +'<td><input class="cell-input" value="'+escHtml(String(r.name||''))+'" data-matri="'+ri+'" data-matkey="name" onchange="matCellChanged(this)"></td>'
            +'<td id="mattype-'+ri+'"><select class="mat-type-sel" data-matri="'+ri+'" data-matkey="material_type" onchange="matCellChanged(this)">'+catOpt+'</select></td>'
            +'<td><input class="cell-input" value="'+escHtml(String(r.size_x||''))+'" data-matri="'+ri+'" data-matkey="size_x" onchange="matCellChanged(this)" style="text-align:right;"></td>'
            +'<td><input class="cell-input" value="'+escHtml(String(r.size_y||''))+'" data-matri="'+ri+'" data-matkey="size_y" onchange="matCellChanged(this)" style="text-align:right;"></td>'
            +'<td><input class="cell-input" value="'+escHtml(String(r.thickness||''))+'" data-matri="'+ri+'" data-matkey="thickness" onchange="matCellChanged(this)" style="text-align:right;"></td>'
            +'<td style="text-align:center;"><button class="btn btn-del" data-matid="'+r.id+'" onclick="deleteMaterial(this.dataset.matid)">✕</button></td></tr>';
    });
    wrap.innerHTML=html+'</tbody></table>';
}
function matCatChanged(el){
    var ri=parseInt(el.getAttribute('data-matri'));
    if(matData[ri]){
        matData[ri].mat_category=el.value;
        // reset type if incompatible
        var types=el.value==='Entry'?ENTRY_TYPES:BACKUP_TYPES;
        if(!types.includes(matData[ri].material_type||'')) matData[ri].material_type='';
        // re-render the type select for this row
        var typeCell=document.getElementById('mattype-'+ri);
        if(typeCell){
            var opts='<option value="">—</option>'+types.map(function(t){return'<option value="'+t+'">'+t+'</option>';}).join('');
            typeCell.innerHTML='<select class="mat-type-sel" data-matri="'+ri+'" data-matkey="material_type" onchange="matCellChanged(this)">'+opts+'</select>';
        }
    }
}
function matCellChanged(el){var ri=parseInt(el.getAttribute('data-matri'));var key=el.getAttribute('data-matkey');if(matData[ri]!==undefined)matData[ri][key]=el.value;}

/* ---- Product PN ---- */
function renderPPNTable(){
    var wrap=document.getElementById('ppn-table-wrap');
    if(!wrap)return;
    var entryMats=matData.filter(m=>m.mat_category==='Entry');
    var backupMats=matData.filter(m=>m.mat_category==='Backup');
    if(ppnData.length===0){wrap.innerHTML='<div style="padding:20px;text-align:center;color:#999;">ยังไม่มีข้อมูล — กด ➕ เพิ่ม Product PN</div>';return;}
    // Use auto-width table (fit content)
    var html='<table id="ppn-table" style="width:auto;min-width:100%;">'
        +'<thead><tr>'
        +'<th>ID</th><th>Product PN</th><th>ชื่อ</th><th>Process</th>'
        +'<th>Entry (วัสดุ)</th><th>Backup (วัสดุ)</th>'
        +'<th>กว้าง (in)</th><th>ยาว (in)</th><th>หนา (mil)</th>'
        +'<th>STK</th><th>ลบ</th>'
        +'</tr></thead><tbody>';
    ppnData.forEach(function(r,ri){
        var eo='<option value="">—</option>'+entryMats.map(m=>'<option value="'+m.id+'"'+(m.id==r.entry_id?' selected':'')+'>'+escHtml(m.part_number+' '+m.name)+'</option>').join('');
        var bo='<option value="">—</option>'+backupMats.map(m=>'<option value="'+m.id+'"'+(m.id==r.backup_id?' selected':'')+'>'+escHtml(m.part_number+' '+m.name)+'</option>').join('');
        html+='<tr>'
            +'<td style="text-align:center;color:#888;white-space:nowrap;">'+r.id+'</td>'
            +'<td><input class="cell-input ppn-input-sm" value="'+escHtml(String(r.pn||''))+'" data-ppnri="'+ri+'" data-ppnkey="pn" onchange="ppnCellChanged(this)"></td>'
            +'<td><input class="cell-input ppn-input-md" value="'+escHtml(String(r.name||''))+'" data-ppnri="'+ri+'" data-ppnkey="name" onchange="ppnCellChanged(this)"></td>'
            +'<td><input class="cell-input ppn-input-xs" value="'+escHtml(String(r.process_code||''))+'" data-ppnri="'+ri+'" data-ppnkey="process_code" onchange="ppnCellChanged(this)" placeholder="02S"></td>'
            +'<td><select class="mat-type-sel ppn-sel" data-ppnri="'+ri+'" data-ppnkey="entry_id" onchange="ppnCellChanged(this)">'+eo+'</select></td>'
            +'<td><select class="mat-type-sel ppn-sel" data-ppnri="'+ri+'" data-ppnkey="backup_id" onchange="ppnCellChanged(this)">'+bo+'</select></td>'
            +'<td><input class="cell-input ppn-input-xs" value="'+escHtml(String(r.prod_width||''))+'" data-ppnri="'+ri+'" data-ppnkey="prod_width" onchange="ppnCellChanged(this)" style="text-align:right;"></td>'
            +'<td><input class="cell-input ppn-input-xs" value="'+escHtml(String(r.prod_length||''))+'" data-ppnri="'+ri+'" data-ppnkey="prod_length" onchange="ppnCellChanged(this)" style="text-align:right;"></td>'
            +'<td><input class="cell-input ppn-input-xs" value="'+escHtml(String(r.prod_thickness||''))+'" data-ppnri="'+ri+'" data-ppnkey="prod_thickness" onchange="ppnCellChanged(this)" style="text-align:right;"></td>'
            +'<td><input class="cell-input ppn-input-xs" value="'+escHtml(String(r.stk||''))+'" data-ppnri="'+ri+'" data-ppnkey="stk" onchange="ppnCellChanged(this)"></td>'
            +'<td style="text-align:center;"><button class="btn btn-del" data-ppnid="'+r.id+'" onclick="deleteProductPN(this.dataset.ppnid)">✕</button></td></tr>';
    });
    wrap.innerHTML=html+'</tbody></table>';
}
function ppnCellChanged(el){var ri=parseInt(el.getAttribute('data-ppnri'));var key=el.getAttribute('data-ppnkey');if(ppnData[ri]!==undefined)ppnData[ri][key]=el.value;}
function addProductPN(){
    var a='<option value="">—</option>'+matData.filter(m=>m.mat_category==='Entry').map(m=>'<option value="'+m.id+'">'+escHtml(m.part_number+' '+m.name)+'</option>').join('');
    var b='<option value="">—</option>'+matData.filter(m=>m.mat_category==='Backup').map(m=>'<option value="'+m.id+'">'+escHtml(m.part_number+' '+m.name)+'</option>').join('');
    document.getElementById('add-ppn-entry').innerHTML=a;
    document.getElementById('add-ppn-backup').innerHTML=b;
    ['add-ppn-pn','add-ppn-name','add-ppn-process','add-ppn-width','add-ppn-length','add-ppn-thick','add-ppn-stk','add-ppn-pw'].forEach(function(id){
        var el=document.getElementById(id); if(el) el.value='';
    });
    document.getElementById('add-ppn-err').textContent='';
    document.getElementById('add-ppn-modal').classList.add('open');
    setTimeout(function(){document.getElementById('add-ppn-pn').focus();},80);
}
function closePPNModal(){document.getElementById('add-ppn-modal').classList.remove('open');}
function submitAddProductPN(){
    var pn   =(document.getElementById('add-ppn-pn').value||'').trim();
    var name =(document.getElementById('add-ppn-name').value||'').trim();
    var ei   =document.getElementById('add-ppn-entry').value;
    var bi   =document.getElementById('add-ppn-backup').value;
    var proc =(document.getElementById('add-ppn-process').value||'').trim();
    var pw_v  =(document.getElementById('add-ppn-width').value||'0');
    var pl_v  =(document.getElementById('add-ppn-length').value||'0');
    var pt_v  =(document.getElementById('add-ppn-thick').value||'0');
    var stk  =(document.getElementById('add-ppn-stk').value||'').trim();
    var pw   =(document.getElementById('add-ppn-pw').value||'').trim();
    var e    =document.getElementById('add-ppn-err');
    if(!pn){e.textContent='❌ กรุณาใส่ Product PN';return;}
    if(pw!==MSG_ADD_DEL_PW){e.textContent='❌ รหัสไม่ถูกต้อง';return;}
    fetch('/api/product_pn',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pn:pn,name:name,entry_id:ei,backup_id:bi,process_code:proc,prod_width:pw_v,prod_length:pl_v,prod_thickness:pt_v,stk:stk})})
    .then(r=>r.json()).then(function(d){if(d.error){e.textContent='❌ '+d.error;return;}closePPNModal();loadMaterials();}).catch(function(err){e.textContent='❌ '+err;});
}
function savePPNData(){fetch('/api/product_pn',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(ppnData)}).then(r=>r.json()).then(d=>alert('✅ บันทึก Product PN แล้ว '+d.saved+' รายการ')).catch(e=>alert('❌ '+e));}
function deleteProductPN(id){if(!confirm('ลบ Product PN นี้?'))return;fetch('/api/product_pn/'+id,{method:'DELETE'}).then(function(){loadMaterials();}).catch(e=>alert('❌ '+e));}

/* ---- Visualize: compare product vs entry/backup material ---- */
function populateVizSelect(){
    var sel=document.getElementById('viz-ppn-sel');
    if(!sel)return;
    var cur=sel.value;
    sel.innerHTML='<option value="">— เลือก Product PN —</option>'
        +ppnData.map(function(p){return'<option value="'+p.id+'"'+(p.id==cur?' selected':'')+'>'+escHtml(p.pn+(p.name?' - '+p.name:''))+'</option>';}).join('');
}

function toggleVizSection(type, show){
    var row = document.getElementById('viz-'+type+'-offset-row');
    if(row) row.style.opacity = show ? '1' : '0.3';
    renderVisualize();
}

function renderVisualize(){
    var sel  = document.getElementById('viz-ppn-sel');
    var area = document.getElementById('viz-area');
    if(!sel||!area) return;
    var pid  = parseInt(sel.value)||0;
    if(!pid){
        area.innerHTML='<div style="text-align:center;color:#999;padding:80px 0;">เลือก Product PN เพื่อดูการเปรียบเทียบขนาด</div>';
        return;
    }
    var ppn = ppnData.find(function(p){return p.id===pid;});
    if(!ppn){ area.innerHTML='<div style="color:#e74c3c;padding:20px;">ไม่พบข้อมูล</div>'; return; }

    var entryMat  = matData.find(function(m){return m.id==ppn.entry_id;})||null;
    var backupMat = matData.find(function(m){return m.id==ppn.backup_id;})||null;

    // Show/hide flags from checkboxes
    var showEntry  = (document.getElementById('viz-show-entry')||{}).checked !== false;
    var showBackup = (document.getElementById('viz-show-backup')||{}).checked !== false;

    // Board (product) size in inch
    var bw = parseFloat(ppn.prod_width)  || 0;
    var bl = parseFloat(ppn.prod_length) || 0;

    // Material sizes in inch
    var ew = entryMat  ? (parseFloat(entryMat.size_x)  || 0) : 0;
    var el = entryMat  ? (parseFloat(entryMat.size_y)  || 0) : 0;
    var bkw= backupMat ? (parseFloat(backupMat.size_x) || 0) : 0;
    var bkl= backupMat ? (parseFloat(backupMat.size_y) || 0) : 0;

    // Offsets (inch, from center)
    var eox = parseFloat((document.getElementById('viz-entry-ox')||{}).value)  || 0;
    var eoy = parseFloat((document.getElementById('viz-entry-oy')||{}).value)  || 0;
    var box = parseFloat((document.getElementById('viz-backup-ox')||{}).value) || 0;
    var boy = parseFloat((document.getElementById('viz-backup-oy')||{}).value) || 0;

    // ─── SVG diagram ───
    var canW = 760, canH = 400, pad = 40;
    var allDims = [bw, bl, ew+Math.abs(eox)*2, el+Math.abs(eoy)*2,
                   bkw+Math.abs(box)*2, bkl+Math.abs(boy)*2, 0.001];
    var maxDim = Math.max.apply(null, allDims);
    var scale  = Math.min((canW - pad*2) / maxDim, (canH - pad*2) / maxDim);

    // Center of canvas
    var cx = canW/2, cy = canH/2;

    function svgRect(wIn, lIn, offXIn, offYIn, fill, stroke, label, sublabel, opacity){
        var sw = wIn*scale, sh = lIn*scale;
        var rx = cx + offXIn*scale - sw/2;
        var ry = cy - offYIn*scale - sh/2;  // SVG Y inverted: +offY = move up
        var oc = opacity||0.75;
        return '<rect x="'+rx+'" y="'+ry+'" width="'+sw+'" height="'+sh
            +'" fill="'+fill+'" stroke="'+stroke+'" stroke-width="1.5" opacity="'+oc+'" rx="2"/>'
            +(label ?'<text x="'+(rx+sw/2)+'" y="'+(ry+sh/2- (sublabel?7:0))
                    +'" text-anchor="middle" dominant-baseline="middle" font-size="10" font-weight="600" fill="#333">'+label+'</text>':'')
            +(sublabel?'<text x="'+(rx+sw/2)+'" y="'+(ry+sh/2+9)
                    +'" text-anchor="middle" dominant-baseline="middle" font-size="9" fill="#555">'+sublabel+'</text>':'');
    }

    function fmt(v){ return v.toFixed(4)+'"'; }

    // clearance from board edge to material edge (signed: positive=inside, negative=outside)
    // Clearance = ระยะที่วัสดุยื่นเกิน board แต่ละด้าน
    // offX บวก = วัสดุเลื่อนขวา → ซ้ายเหลือน้อย, ขวาเหลือมาก
    // offY บวก = วัสดุเลื่อนขึ้น  → บนเหลือมาก,  ล่างเหลือน้อย
    function clearanceX(boardW, matW, offX){
        var half = (matW - boardW) / 2;
        return { left: half - offX, right: half + offX };
    }
    function clearanceY(boardL, matL, offY){
        var half = (matL - boardL) / 2;
        return { top: half + offY, bottom: half - offY };
    }

    var eCX = clearanceX(bw, ew,  eox);
    var eCY = clearanceY(bl, el,  eoy);
    var bCX = clearanceX(bw, bkw, box);
    var bCY = clearanceY(bl, bkl, boy);

    var svgParts = '';
    // Draw Backup (bottom layer)
    if(showBackup && bkw>0&&bkl>0) svgParts += svgRect(bkw,bkl,box,boy,'rgba(106,27,154,.15)','#6a1b9a',
        'Backup', backupMat?backupMat.part_number:'', 0.8);
    // Draw Entry (middle)
    if(showEntry && ew>0&&el>0) svgParts += svgRect(ew,el,eox,eoy,'rgba(21,101,192,.18)','#1565c0',
        'Entry', entryMat?entryMat.part_number:'', 0.8);
    // Draw Board (top, red border, no fill obstruct)
    if(bw>0&&bl>0) svgParts += svgRect(bw,bl,0,0,'rgba(231,76,60,.1)','#e74c3c',
        ppn.pn+(ppn.process_code?' ['+ppn.process_code+']':''),
        fmt(bw)+' × '+fmt(bl), 0.9);
    // Center crosshair
    svgParts += '<line x1="'+(cx-8)+'" y1="'+cy+'" x2="'+(cx+8)+'" y2="'+cy
        +'" stroke="#888" stroke-width="1" stroke-dasharray="2,2"/>'
        +'<line x1="'+cx+'" y1="'+(cy-8)+'" x2="'+cx+'" y2="'+(cy+8)
        +'" stroke="#888" stroke-width="1" stroke-dasharray="2,2"/>';

    var svg = '<svg width="100%" viewBox="0 0 '+canW+' '+canH+'" xmlns="http://www.w3.org/2000/svg" '
        +'style="max-width:'+canW+'px;display:block;margin:0 auto;">'
        +'<rect width="100%" height="100%" fill="var(--surface)"/>'
        +'<text x="'+canW/2+'" y="18" text-anchor="middle" font-size="12" font-weight="bold" fill="#333">'
        +escHtml(ppn.pn+(ppn.name?' — '+ppn.name:''))
        +'</text>'
        +svgParts
        +'</svg>';

    // ─── Clearance table ───
    function clRow(label, color, lv, rv, tv, bv){
        function cell(v){
            var ok = v>=0;
            return '<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;'
                +'font-weight:600;color:'+(ok?'#1a7a40':'#e74c3c')+';font-size:12px;">'
                +(ok?'':'-')+Math.abs(v).toFixed(4)+'"</td>';
        }
        return '<tr style="background:'+color+';">'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);font-weight:700;font-size:12px;">'+label+'</td>'
            +cell(lv)+cell(rv)+cell(tv)+cell(bv)
            +'</tr>';
    }

    var tbl='';
    if(showEntry && (ew>0||el>0)){
        tbl+=clRow('📥 Entry',  'rgba(21,101,192,.06)',  eCX.left, eCX.right, eCY.top, eCY.bottom);
    }
    if(showBackup && (bkw>0||bkl>0)){
        tbl+=clRow('🔄 Backup', 'rgba(106,27,154,.06)',  bCX.left, bCX.right, bCY.top, bCY.bottom);
    }

    var clTable = tbl ? '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:14px;">'
        +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
        +'<th style="padding:6px 10px;">วัสดุ</th>'
        +'<th style="padding:6px 10px;text-align:center;">← ซ้าย</th>'
        +'<th style="padding:6px 10px;text-align:center;">ขวา →</th>'
        +'<th style="padding:6px 10px;text-align:center;">↑ บน</th>'
        +'<th style="padding:6px 10px;text-align:center;">ล่าง ↓</th>'
        +'</tr></thead><tbody>'+tbl+'</tbody></table>'
        +'<div style="font-size:10px;color:var(--text-dim);margin-top:4px;">* ค่าบวก = วัสดุยื่นเกิน board | ค่าลบ = วัสดุสั้นกว่า board (แดง)</div>'
        : '';

    // ─── Info summary row ───
    function infoRow(icon, name, mat, offX, offY, szW, szL, thick){
        if(!mat&&szW===0) return '';
        var pn = mat?mat.part_number:'—';
        return '<tr style="font-size:12px;">'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);font-weight:700;">'+icon+' '+name+'</td>'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);">'+escHtml(pn)+'</td>'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+fmt(szW)+'</td>'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+fmt(szL)+'</td>'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+(thick?thick+' mil':'—')+'</td>'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+fmt(offX)+'</td>'
            +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+fmt(offY)+'</td>'
            +'</tr>';
    }
    var pt = parseFloat(ppn.prod_thickness)||0;
    var et = entryMat  ? (parseFloat(entryMat.thickness)||0)  : 0;
    var bt = backupMat ? (parseFloat(backupMat.thickness)||0) : 0;

    var infoTable = '<table style="width:100%;border-collapse:collapse;margin-top:12px;">'
        +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
        +'<th style="padding:6px 10px;">ชื่อ</th><th style="padding:6px 10px;">Part No.</th>'
        +'<th style="padding:6px 10px;text-align:center;">กว้าง (in)</th>'
        +'<th style="padding:6px 10px;text-align:center;">ยาว (in)</th>'
        +'<th style="padding:6px 10px;text-align:center;">หนา (mil)</th>'
        +'<th style="padding:6px 10px;text-align:center;">Offset X (in)</th>'
        +'<th style="padding:6px 10px;text-align:center;">Offset Y (in)</th>'
        +'</tr></thead><tbody>'
        +'<tr style="font-size:12px;">'
        +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);font-weight:700;">📦 Board</td>'
        +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);">'+escHtml(ppn.pn)+'</td>'
        +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+fmt(bw)+'</td>'
        +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+fmt(bl)+'</td>'
        +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">'+(pt?pt+' mil':'—')+'</td>'
        +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">0"</td>'
        +'<td style="padding:5px 10px;border-bottom:1px solid var(--border);text-align:center;">0"</td>'
        +'</tr>'
        +(showEntry  ? infoRow('📥','Entry',  entryMat,  eox, eoy, ew, el, et||'—') : '')
        +(showBackup ? infoRow('🔄','Backup', backupMat, box, boy, bkw, bkl, bt||'—') : '')
        +'</tbody></table>';

    area.innerHTML = svg + infoTable + clTable;
}

function addMaterial(){
    ['add-mat-pn','add-mat-name','add-mat-sx','add-mat-sy','add-mat-th','add-mat-pw'].forEach(function(id){document.getElementById(id).value='';});
    document.getElementById('add-mat-cat').value='Entry';
    document.getElementById('add-mat-type').value='';
    updateAddMatTypes();
    document.getElementById('add-mat-err').textContent='';
    document.getElementById('add-mat-modal').classList.add('open');
    setTimeout(function(){document.getElementById('add-mat-pn').focus();},80);
}
function updateAddMatTypes(){
    var cat=document.getElementById('add-mat-cat').value;
    var types=cat==='Entry'?ENTRY_TYPES:BACKUP_TYPES;
    var sel=document.getElementById('add-mat-type');
    sel.innerHTML='<option value="">— เลือกประเภท —</option>'+types.map(function(t){return'<option value="'+t+'">'+t+'</option>';}).join('');
}
function closeMatModal(){document.getElementById('add-mat-modal').classList.remove('open');}
function submitAddMaterial(){
    var pn=(document.getElementById('add-mat-pn').value||'').trim();
    var name=(document.getElementById('add-mat-name').value||'').trim();
    var cat=document.getElementById('add-mat-cat').value;
    var typ=document.getElementById('add-mat-type').value;
    var sx=document.getElementById('add-mat-sx').value;
    var sy=document.getElementById('add-mat-sy').value;
    var th=document.getElementById('add-mat-th').value;
    var pw=(document.getElementById('add-mat-pw').value||'').trim();
    var e=document.getElementById('add-mat-err');
    if(!pn){e.textContent='❌ กรุณาใส่ Part Number';return;}
    if(!name){e.textContent='❌ กรุณาใส่ชื่อวัสดุ';return;}
    if(pw!==MSG_ADD_DEL_PW){e.textContent='❌ รหัสไม่ถูกต้อง';return;}
    fetch('/api/materials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({part_number:pn,name:name,mat_category:cat,material_type:typ,size_x:sx,size_y:sy,thickness:th})})
    .then(r=>r.json()).then(function(d){if(d.error){e.textContent='❌ '+d.error;return;}closeMatModal();loadMaterials();}).catch(function(err){e.textContent='❌ '+err;});
}
function saveMaterials(){fetch('/api/materials',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(matData)}).then(r=>r.json()).then(function(d){alert('✅ บันทึกแล้ว '+d.saved+' รายการ');loadMaterials();}).catch(e=>alert('❌ '+e));}
function deleteMaterial(id){if(!confirm('ลบ Part Number นี้?'))return;fetch('/api/materials/'+id,{method:'DELETE'}).then(function(){loadMaterials();}).catch(e=>alert('❌ '+e));}
function exportMaterials(){window.location.href='/api/materials/export';}

/* ========== EXAM JS ========== */
var examSets=[], examVideos=[], currentExamSetId=null, examQuestions=[], examAnswers={}, examRevealed=false;
function loadExams(){
    fetch('/api/exam_sets').then(r=>r.json()).then(function(d){examSets=d;renderExamSets();buildExamFilterBar();}).catch(e=>console.error(e));
    fetch('/api/training_videos').then(r=>r.json()).then(function(d){examVideos=d;renderVideos();}).catch(e=>console.error(e));
}
/* videos */
function renderVideos(){
    var wrap=document.getElementById('video-grid');
    if(!wrap)return;
    if(examVideos.length===0){
        wrap.innerHTML='<div style="padding:20px;text-align:center;color:#999;">ยังไม่มีวิดีโอในโฟลเดอร์ Training</div>';
        return;
    }
    wrap.innerHTML='<div style="display:flex;flex-direction:column;gap:6px;">'
        +examVideos.map(function(v){
            var vidId='vid-dur-'+v.id;
            return'<div style="display:flex;align-items:center;gap:12px;padding:8px 14px;'
                +'background:var(--surface2);border:1px solid var(--border);border-radius:8px;">'
                +'<button style="background:none;border:none;cursor:pointer;color:var(--accent);'
                +'font-size:13px;font-weight:600;text-align:left;padding:0;flex:1;" '
                +'data-vid="'+v.id+'" data-title="'+escHtml(v.title)+'" '
                +'onclick="openVideoModal(this.dataset.vid,this.dataset.title)">▶ '+escHtml(v.title)+'</button>'
                +'<span id="'+vidId+'" style="font-size:11px;color:var(--text-dim);min-width:50px;text-align:right;">—</span>'
                +'<button class="video-card-del" style="padding:3px 10px;font-size:11px;" '
                +'data-vid="'+v.id+'" onclick="deleteVideo(this.dataset.vid)">🗑</button>'
                // Hidden video element to probe duration
                +'<video src="/api/training_video_stream/'+v.id+'" preload="metadata" style="display:none;" '
                +'onloadedmetadata="updateVideoDuration(this,\''+vidId+'\')"></video>'
                +'</div>';
        }).join('')
        +'</div>';
}
function updateVideoDuration(el, spanId){
    var dur = el.duration;
    var span = document.getElementById(spanId);
    if(!span || !dur || isNaN(dur)) return;
    var m = Math.floor(dur/60);
    var s = Math.floor(dur%60);
    span.textContent = m+':'+(s<10?'0':'')+s;
}
function openVideoModal(vid, title){
    document.getElementById('video-modal-title').textContent = title;
    var player = document.getElementById('video-modal-player');
    player.src = '/api/training_video_stream/' + vid;
    player.load();
    document.getElementById('video-play-modal').classList.add('open');
}
function closeVideoModal(){
    var player = document.getElementById('video-modal-player');
    player.pause(); player.src='';
    document.getElementById('video-play-modal').classList.remove('open');
}
function openAddVideoModal(){
    document.getElementById('add-video-title').value='';
    document.getElementById('add-video-file').value='';
    document.getElementById('add-video-err').textContent='';
    document.getElementById('add-video-modal').classList.add('open');
}
function closeAddVideoModal(){document.getElementById('add-video-modal').classList.remove('open');}
function submitAddVideo(){
    var title=(document.getElementById('add-video-title').value||'').trim();
    var fi=document.getElementById('add-video-file');
    var pw=(document.getElementById('add-video-pw').value||'').trim();
    var e=document.getElementById('add-video-err');
    if(!title){e.textContent='❌ กรุณาใส่ชื่อวิดีโอ';return;}
    if(!fi.files||!fi.files[0]){e.textContent='❌ กรุณาเลือกไฟล์วิดีโอ';return;}
    if(pw!==MSG_ADD_DEL_PW){e.textContent='❌ รหัสไม่ถูกต้อง';return;}
    var fd=new FormData();
    fd.append('title',title);fd.append('file',fi.files[0]);
    e.textContent='⏳ กำลังอัปโหลด...';
    fetch('/api/training_videos',{method:'POST',body:fd})
    .then(r=>r.json()).then(function(d){
        if(d.error){e.textContent='❌ '+d.error;return;}
        closeAddVideoModal();loadExams();
    }).catch(function(err){e.textContent='❌ '+err;});
}
function deleteVideo(id){
    if(!confirm('ลบวิดีโอนี้?'))return;
    fetch('/api/training_videos/'+id,{method:'DELETE'}).then(function(){loadExams();}).catch(e=>alert('❌ '+e));
}
/* exam sets */
function renderExamSets(){
    var wrap=document.getElementById('exam-sets-wrap');
    if(!wrap)return;
    if(examSets.length===0){wrap.innerHTML='<div style="padding:20px;text-align:center;color:#999;">ยังไม่มีชุดข้อสอบ</div>';return;}
    var theoryRows=examSets.filter(function(s){return s.exam_type==='theory';});
    var operateRows=examSets.filter(function(s){return s.exam_type==='operate';});
    function makeTable(rows, typeLbl, typeCls){
        if(rows.length===0)return'<tr><td colspan="5" style="padding:8px 12px;color:#999;font-style:italic;">ยังไม่มีชุดข้อสอบ '+typeLbl+'</td></tr>';
        return rows.map(function(s){
            return'<tr style="border-bottom:1px solid var(--border);">'
                +'<td style="padding:7px 12px;"><span class="exam-type-badge '+typeCls+'">'+typeLbl+'</span></td>'
                +'<td style="padding:7px 12px;font-weight:600;">'+escHtml(s.name)+'</td>'
                +'<td style="padding:7px 12px;text-align:center;color:var(--text-dim);">สุ่ม <strong>'+s.random_n+'</strong> ข้อ (ผ่าน <strong>'+(s.pass_score||0)+'</strong>) | ทั้งหมด '+(s.q_count||0)+' ข้อ</td>'
                +'<td style="padding:7px 12px;">'
                +'<button class="btn btn-add" style="font-size:11px;padding:3px 10px;" data-setid="'+s.id+'" onclick="openExamEditor(this.dataset.setid)">✏️ แก้ไข</button>'
                +'</td>'
                +'<td style="padding:7px 12px;">'
                +'<button class="btn btn-del" style="font-size:11px;padding:3px 8px;" data-setid="'+s.id+'" onclick="deleteExamSet(this.dataset.setid)">🗑</button>'
                +'</td>'
                +'</tr>';
        }).join('');
    }
    wrap.innerHTML='<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
        +'<th style="padding:8px 12px;width:90px;">ประเภท</th>'
        +'<th style="padding:8px 12px;">ชื่อชุดข้อสอบ</th>'
        +'<th style="padding:8px 12px;text-align:center;">จำนวนข้อ</th>'
        +'<th style="padding:8px 12px;width:100px;">แก้ไข</th>'
        +'<th style="padding:8px 12px;width:60px;">ลบ</th>'
        +'</tr></thead><tbody>'
        +makeTable(theoryRows,'Theory','badge-theory')
        +makeTable(operateRows,'Operate','badge-operate')
        +'</tbody></table>';
}
function openAddExamSetModal(){
    document.getElementById('add-exam-pass').value='7';
    document.getElementById('add-exam-name').value='';
    document.getElementById('add-exam-type').value='theory';
    document.getElementById('add-exam-n').value='10';
    document.getElementById('add-exam-pw').value='';
    document.getElementById('add-exam-err').textContent='';
    document.getElementById('add-exam-modal').classList.add('open');
    setTimeout(function(){document.getElementById('add-exam-name').focus();},80);
}
function closeAddExamSetModal(){document.getElementById('add-exam-modal').classList.remove('open');}
function submitAddExamSet(){
    var name=(document.getElementById('add-exam-name').value||'').trim();
    var typ=document.getElementById('add-exam-type').value;
    var n=parseInt(document.getElementById('add-exam-n').value)||10;
    var p=parseInt(document.getElementById('add-exam-pass').value)||7;  // เพิ่มบรรทัดนี้
    var pw=(document.getElementById('add-exam-pw').value||'').trim();
    var e=document.getElementById('add-exam-err');
    if(!name){e.textContent='❌ กรุณาใส่ชื่อชุดข้อสอบ';return;}
    if(pw!==MSG_ADD_DEL_PW){e.textContent='❌ รหัสไม่ถูกต้อง';return;}
    fetch('/api/exam_sets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,exam_type:typ,random_n:n,pass_score:p})})
    .then(r=>r.json()).then(function(d){if(d.error){e.textContent='❌ '+d.error;return;}closeAddExamSetModal();loadExams();}).catch(function(err){e.textContent='❌ '+err;});
}
function deleteExamSet(id){
    if(!confirm('ลบชุดข้อสอบนี้ (รวมทุกข้อ)?'))return;
    fetch('/api/exam_sets/'+id,{method:'DELETE'}).then(function(){loadExams();}).catch(e=>alert('❌ '+e));
}
/* exam editor */
var editorSetId=null, editorQuestions=[], editorSetData=null, examEditorPwOk=false;
function openExamEditor(setId){
    editorSetId=parseInt(setId); examEditorPwOk=false;
    editorSetData=examSets.find(function(s){return s.id===editorSetId;})||null;
    document.getElementById('exam-editor-modal').classList.add('open');
    
    // ตั้งค่ากล่องรหัสผ่านและส่วนแสดงข้อมูลให้กลับสู่ค่าเริ่มต้น
    var pwRow = document.getElementById('exam-editor-pw-row');
    var pwInp = document.getElementById('exam-editor-pw-input');
    var unlockMsg = document.getElementById('exam-editor-unlocked');
    var contentDiv = document.getElementById('exam-editor-content');
    var passEl = document.getElementById('exam-editor-pass');

    // 1. ซ่อนกล่องข้อมูลทั้งหมด
    if(contentDiv) contentDiv.style.display = 'none';
    
    // 2. แสดงกล่องกรอกรหัสผ่าน
    if(pwRow) pwRow.style.display = 'flex';
    if(pwInp){ pwInp.value=''; setTimeout(function(){pwInp.focus();},120); }
    if(unlockMsg) unlockMsg.style.display = 'none';
    
    var pwErr = document.getElementById('exam-editor-pw-err');
    if(pwErr) pwErr.textContent = '';
    
    // เตรียมข้อมูล Metadata ของชุดข้อสอบไว้ (แต่ยังไม่แสดงจนกว่าจะปลดล็อก)
    if(editorSetData){
        var nameEl = document.getElementById('exam-editor-name');
        if(nameEl) nameEl.value = editorSetData.name||'';
        var nEl = document.getElementById('exam-editor-n');
        if(nEl) nEl.value = editorSetData.random_n||10;
        if(passEl) passEl.value = editorSetData.pass_score || Math.ceil((editorSetData.random_n || 10) * 0.7);
        var typeBadge = document.getElementById('exam-editor-type-badge');
        if(typeBadge){
            typeBadge.textContent = editorSetData.exam_type==='theory'?'Theory (MCQ)':'Operate (ผ่าน/ไม่ผ่าน)';
            typeBadge.className = 'exam-type-badge '+(editorSetData.exam_type==='theory'?'badge-theory':'badge-operate');
        }
    }
    
    // รีเซ็ตรายการคำถาม และแจ้งให้กรอกรหัส
    document.getElementById('exam-editor-qlist').innerHTML='<div style="padding:20px;text-align:center;">🔒 กรุณาใส่รหัสผ่านเพื่อแสดงข้อมูล</div>';
    editorQuestions = [];
}

function closeExamEditor(){document.getElementById('exam-editor-modal').classList.remove('open');}

function saveEditorMeta(){
    var name=(document.getElementById('exam-editor-name')||{}).value||'';
    var n=parseInt((document.getElementById('exam-editor-n')||{}).value)||10;
    var p=parseInt((document.getElementById('exam-editor-pass')||{}).value)||7;  // เพิ่มบรรทัดนี้
    fetch('/api/exam_sets/'+editorSetId+'/meta',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim(),random_n:n,pass_score:p})})
    .then(r=>r.json()).then(function(d){
        if(d.ok){alert('✅ บันทึก "'+name+'" สุ่ม '+n+' ข้อ');loadExams();}
        else alert('❌ '+JSON.stringify(d));
    }).catch(e=>alert('❌ '+e));
}

// Keep old name for compat
function saveEditorRandomN(){ saveEditorMeta(); }

function renderEditorQuestions(){
    var wrap=document.getElementById('exam-editor-qlist');
    if(!wrap)return;
    var isOperate=editorSetData&&editorSetData.exam_type==='operate';
    var html=editorQuestions.map(function(q,i){
        if(isOperate){
            // Operate: โจทย์เท่านั้น — ไม่มีตัวเลือก/เฉลย
            return'<div class="q-row" id="qrow-'+i+'">'
                +'<div style="display:flex;justify-content:space-between;align-items:start;gap:8px;">'
                +'<div style="flex:1;">'
                +'<div style="font-size:11px;color:var(--text-dim);margin-bottom:3px;">รายการที่ '+(i+1)+'</div>'
                +'<textarea data-qi="'+i+'" data-qkey="question" onchange="qEditorChanged(this)" style="min-height:48px;" placeholder="โจทย์ / รายการที่ต้องประเมิน...">'+escHtml(q.question||'')+'</textarea>'
                +'</div>'
                +'<button class="btn btn-del" onclick="removeEditorQ('+i+')" style="margin-top:16px;">✕</button>'
                +'</div></div>';
        }else{
            // Theory: MCQ 4 ตัวเลือก
            return'<div class="q-row" id="qrow-'+i+'">'
                +'<div style="display:flex;justify-content:space-between;align-items:start;gap:8px;">'
                +'<div style="flex:1;">'
                +'<div style="font-size:11px;color:var(--text-dim);margin-bottom:3px;">ข้อที่ '+(i+1)+'</div>'
                +'<textarea data-qi="'+i+'" data-qkey="question" onchange="qEditorChanged(this)" style="min-height:60px;" placeholder="คำถาม...">'+escHtml(q.question||'')+'</textarea>'
                +'<div class="q-choices">'
                +'<input placeholder="A: ..." value="'+escHtml(q.choice_a||'')+'" data-qi="'+i+'" data-qkey="choice_a" onchange="qEditorChanged(this)">'
                +'<input placeholder="B: ..." value="'+escHtml(q.choice_b||'')+'" data-qi="'+i+'" data-qkey="choice_b" onchange="qEditorChanged(this)">'
                +'<input placeholder="C: ..." value="'+escHtml(q.choice_c||'')+'" data-qi="'+i+'" data-qkey="choice_c" onchange="qEditorChanged(this)">'
                +'<input placeholder="D: ..." value="'+escHtml(q.choice_d||'')+'" data-qi="'+i+'" data-qkey="choice_d" onchange="qEditorChanged(this)">'
                +'</div>'
                +'<div class="q-answer-row">เฉลย: <select data-qi="'+i+'" data-qkey="answer" onchange="qEditorChanged(this)">'
                +['a','b','c','d'].map(function(x){return'<option value="'+x+'"'+(q.answer===x?' selected':'')+'>'+x.toUpperCase()+'</option>';}).join('')
                +'</select></div></div>'
                +'<button class="btn btn-del" onclick="removeEditorQ('+i+')">✕</button>'
                +'</div></div>';
        }
    }).join('');
    if(html===''){
        var hint=isOperate?'กด ➕ เพิ่มรายการที่ต้องประเมิน':'กด ➕ เพิ่มข้อสอบ MCQ';
        html='<div style="padding:20px;text-align:center;color:#999;">ยังไม่มีข้อสอบ — '+hint+'</div>';
    }
    wrap.innerHTML=html;
}
function exportExamSet(){
    window.location.href='/api/exam_sets/'+editorSetId+'/export';
}
function importExamSet(input){
    if(!input.files||!input.files[0])return;
    var fd=new FormData();
    fd.append('file',input.files[0]);
    fetch('/api/exam_sets/'+editorSetId+'/import',{method:'POST',body:fd})
    .then(r=>r.json()).then(function(d){
        if(d.error){alert('❌ '+d.error);return;}
        alert('✅ Import สำเร็จ '+d.imported+' ข้อ');
        fetch('/api/exam_sets/'+editorSetId+'/questions').then(r=>r.json()).then(function(q){
            editorQuestions=q;renderEditorQuestions();
        });
        loadExams();
    }).catch(e=>alert('❌ '+e));
    input.value='';
}
function qEditorChanged(el){
    var qi=parseInt(el.getAttribute('data-qi'));
    var key=el.getAttribute('data-qkey');
    if(editorQuestions[qi]!==undefined)editorQuestions[qi][key]=el.value;
}
function checkExamEditorPw(){
    var pw = (document.getElementById('exam-editor-pw-input')||{}).value||'';
    var errEl = document.getElementById('exam-editor-pw-err');
    if(pw === MSG_ADD_DEL_PW){
        examEditorPwOk = true;
        
        // ซ่อนกล่องรหัสผ่าน โชว์ข้อความปลดล็อก
        var pwRow = document.getElementById('exam-editor-pw-row');
        if(pwRow) pwRow.style.display = 'none';
        var unlockMsg = document.getElementById('exam-editor-unlocked');
        if(unlockMsg) unlockMsg.style.display = '';
        if(errEl) errEl.textContent = '';
        
        // 3. ปลดล็อกและแสดงเนื้อหาข้อสอบ
        var contentDiv = document.getElementById('exam-editor-content');
        if(contentDiv) contentDiv.style.display = 'flex';
        
        // 4. เริ่มดึงข้อมูล (Fetch) เนื้อหาข้อสอบเมื่อรหัสผ่านถูก
        document.getElementById('exam-editor-qlist').innerHTML='<div style="padding:20px;text-align:center;">⏳ กำลังโหลด...</div>';
        fetch('/api/exam_sets/'+editorSetId+'/questions')
            .then(r => r.json())
            .then(function(d){
                editorQuestions = d; 
                renderEditorQuestions();
            })
            .catch(e => console.error(e));
    } else {
        if(errEl) errEl.textContent = '❌ รหัสไม่ถูกต้อง';
    }
}
function addEditorQ(){
    if(!examEditorPwOk){alert('❌ กรุณาใส่รหัสผ่าน level 1 ก่อน');return;}
    editorQuestions.push({id:null,set_id:editorSetId,question:'',choice_a:'',choice_b:'',choice_c:'',choice_d:'',answer:'a'});
    renderEditorQuestions();
    var wrap=document.getElementById('exam-editor-qlist');
    if(wrap)wrap.scrollTop=wrap.scrollHeight;
}
function removeEditorQ(i){
    if(!examEditorPwOk){alert('❌ กรุณาใส่รหัสผ่าน level 1 ก่อน');return;}
    editorQuestions.splice(i,1);renderEditorQuestions();
}
function saveEditorQuestions(){
    if(!examEditorPwOk){alert('❌ กรุณาใส่รหัสผ่าน level 1 ก่อน');return;}
    fetch('/api/exam_sets/'+editorSetId+'/questions',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(editorQuestions)})
    .then(r=>r.json()).then(function(d){alert('✅ บันทึก '+d.saved+' ข้อ');loadExams();}).catch(e=>alert('❌ '+e));
}

/* ===== EXAM TAKE MODAL (from employee table) ===== */
var takeEmpId='', takeEmpRi=-1, takeSetData=null, takeAnswers={}, takeRevealed=false, takeCurrentQ=0;
function getExamBody(){
    // Always write to emp-train-body (the combined modal)
    return document.getElementById('emp-train-body');
}
function backToTrainHome(){
    // Go back to the training/exam home screen
    openEmpTrainModal(takeEmpId, takeEmpRi);
}

function openEmpTrainModal(empId, ri){
    takeEmpId = empId;
    var foundRi = empData.findIndex(function(r){ return String(r.employee_id||'').trim() === String(empId).trim(); });
    takeEmpRi = foundRi >= 0 ? foundRi : (parseInt(ri) || 0);

    var modal = document.getElementById('emp-train-modal');
    var body  = document.getElementById('emp-train-body');
    if (!modal || !body) { alert('ไม่พบ modal element — โปรด Refresh หน้าเว็บ'); return; }
    modal.classList.add('open');
    body.innerHTML = '<div style="padding:20px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>';

    // Load examSets if not yet loaded (needed for makeSetBtns)
    var fetchSets = (examSets && examSets.length > 0)
        ? Promise.resolve(examSets)
        : fetch('/api/exam_sets').then(r=>r.json()).then(function(d){ examSets=d; return d; });

    Promise.all([
        fetch('/api/exam_results/' + empId).then(r => r.json()),
        fetch('/api/operate_results/' + empId).then(r => r.json()).catch(function(){ return []; }),
        fetchSets
    ]).then(function(res){
        renderEmpTrainHome(res[0], res[1]);
    }).catch(function(e){
        if (body) body.innerHTML = '<div style="color:#e74c3c;padding:20px;">❌ โหลดข้อมูลไม่ได้: ' + e + '</div>';
    });
}
function closeEmpTrainModal(){document.getElementById('emp-train-modal').classList.remove('open');}

function renderEmpTrainHome(examResults, operateDetails){
    var body=document.getElementById('emp-train-body');
    var row=empData[takeEmpRi]||{};
    window._trainOpDetails = operateDetails;

    // Training dates
    var trainFields=[
        {key:'train_work_unit', label:'สอบ Work Unit'},
        {key:'train_sign_doc',  label:'เซ็นเอกสาร'},
    ];
    var trainHtml='<div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:14px;">'
        +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
        +'<span style="font-size:13px;font-weight:700;">📅 วันที่ผ่าน</span>'
        +'<button class="btn" style="background:#e74c3c;color:#fff;font-size:11px;padding:3px 12px;" onclick="openDeleteHistoryModal()">🗑 ลบประวัติ</button>'
        +'</div>';
    trainFields.forEach(function(f){
        var val=String(row[f.key]||'');
        trainHtml+='<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">'
            +'<label style="font-size:12px;font-weight:600;min-width:120px;">'+escHtml(f.label)+'</label>'
            +'<input type="date" class="train-date-input" style="width:160px;" value="'+escHtml(val)+'" '
            +'data-ri="'+takeEmpRi+'" data-key="'+f.key+'" onchange="trainDateChanged(this)">'
            +(val?'<span class="train-pass">✅ '+escHtml(val)+'</span>':'<span class="train-none">—</span>')
            +'</div>';
    });
    trainHtml+='</div>';

    var theoryResults = examResults.filter(function(r){return r.exam_type==='theory';});
    var operateResults= examResults.filter(function(r){return r.exam_type==='operate';});

    function makeExamTable(rows, isOperate){
        if(!rows.length) return '<div style="color:#999;font-size:12px;margin-bottom:8px;">ยังไม่มีประวัติ</div>';
        var html='<table style="width:100%;font-size:12px;border-collapse:collapse;margin-bottom:10px;">'
            +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
            +'<th style="padding:5px 8px;">ชุดข้อสอบ</th>'
            +'<th style="padding:5px 8px;">ผล</th>'
            +'<th style="padding:5px 8px;text-align:center;">คะแนน</th>'
            +'<th style="padding:5px 8px;">วันที่สอบ</th>'
            +'<th style="padding:5px 8px;width:70px;text-align:center;">ดูคำตอบ</th>'
            +'</tr></thead><tbody>';
        rows.forEach(function(r, ri){
            var passed=r.passed
                ?'<span style="color:#1a7a40;font-weight:700;">✅ ผ่าน</span>'
                :'<span style="color:#c0392b;font-weight:700;">❌ ไม่ผ่าน</span>';
            var bg=ri%2===0?'var(--surface)':'var(--surface2)';
            var encData=encodeURIComponent(JSON.stringify({
                id:r.id, set_id:r.set_id, taken_at:r.taken_at||'', isOp:isOperate?1:0
            }));
            html+='<tr style="background:'+bg+';">'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);">'+escHtml(r.set_name||String(r.set_id)||'')+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);">'+passed+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;">'+r.score+'/'+r.total+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);font-size:11px;">'+escHtml(r.taken_at||'')+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;">'
                +'<button class="btn-exam-view" style="font-size:10px;padding:2px 8px;width:auto;" '
                +'data-enc="'+escHtml(encData)+'" onclick="openAnswerModal(this.dataset.enc)">🔍 ดู</button>'
                +'</td>'
                +'</tr>';
        });
        html+='</tbody></table>';
        return html;
    }

    var theorySetList =examSets.filter(s=>s.exam_type==='theory');
    var operateSetList=examSets.filter(s=>s.exam_type==='operate');
    function makeSetBtns(sets){
        return sets.map(function(s){
            var lbl=s.exam_type==='theory'?'Theory':'Operate';
            var bcls=s.exam_type==='theory'?'badge-theory':'badge-operate';
            return'<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);">'
                +'<span><strong>'+escHtml(s.name)+'</strong> <span class="exam-type-badge '+bcls+'">'+lbl+'</span>'
                +' <span style="font-size:11px;color:var(--text-dim);">(สุ่ม '+s.random_n+' ข้อ, ผ่าน '+(s.pass_score||0)+')</span></span>'
                +'<button class="btn btn-add" style="font-size:11px;padding:3px 10px;" data-setid="'+s.id+'" onclick="startExam(this.dataset.setid)">▶ สอบ</button>'
                +'</div>';
        }).join('');
    }

    body.innerHTML=trainHtml
        +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">'
        +'<div>'
        +'<div style="font-size:13px;font-weight:700;margin-bottom:6px;color:#1565c0;">📝 Theory</div>'
        +makeSetBtns(theorySetList)
        +'<div style="font-size:12px;color:var(--text-dim);margin:8px 0 4px;font-weight:600;">ประวัติ Theory</div>'
        +makeExamTable(theoryResults,false)
        +'</div>'
        +'<div>'
        +'<div style="font-size:13px;font-weight:700;margin-bottom:6px;color:#6a1b9a;">🔧 Operate</div>'
        +makeSetBtns(operateSetList)
        +'<div style="font-size:12px;color:var(--text-dim);margin:8px 0 4px;font-weight:600;">ประวัติ Operate</div>'
        +makeExamTable(operateResults,true)
        +'</div>'
        +'</div>';
}

/* ── Delete history modal ── */
function openDeleteHistoryModal(){
    var el=document.getElementById('del-history-pw');
    if(el){el.value='';setTimeout(function(){el.focus();},80);}
    var err=document.getElementById('del-history-err');
    if(err) err.textContent='';
    document.getElementById('del-history-modal').classList.add('open');
}
function closeDeleteHistoryModal(){
    document.getElementById('del-history-modal').classList.remove('open');
}
function confirmDeleteHistory(){
    var pw=(document.getElementById('del-history-pw').value||'').trim();
    var err=document.getElementById('del-history-err');
    if(pw!==MSG_ADD_DEL_PW){err.textContent='❌ รหัสผ่านไม่ถูกต้อง';return;}
    fetch('/api/exam_results/employee/'+takeEmpId,{method:'DELETE'})
    .then(r=>r.json()).then(function(d){
        closeDeleteHistoryModal();
        alert('✅ ลบประวัติสอบทั้งหมดแล้ว ('+d.deleted+' รายการ)');
        openEmpTrainModal(takeEmpId, takeEmpRi);
    }).catch(function(e){if(err)err.textContent='❌ '+e;});
}

/* ── Answer detail modal ── */
var _pendingAnswerEnc = null;

function openAnswerModal(enc){
    _pendingAnswerEnc = enc;
    var modal  = document.getElementById('answer-detail-modal');
    var pwRow  = document.getElementById('answer-pw-row');
    var pwInp  = document.getElementById('answer-pw-input');
    var pwErr  = document.getElementById('answer-pw-err');
    var body   = document.getElementById('answer-detail-body');
    if(!modal) return;
    // Reset lock state
    if(pwRow)  { pwRow.style.display = 'flex'; }
    if(body)   { body.style.display = 'none'; body.innerHTML = ''; }
    if(pwInp)  { pwInp.value = ''; }
    if(pwErr)  { pwErr.textContent = ''; }
    modal.classList.add('open');
    setTimeout(function(){ if(pwInp) pwInp.focus(); }, 80);
}

function confirmAnswerPw(){
    var pwInp = document.getElementById('answer-pw-input');
    var pwErr = document.getElementById('answer-pw-err');
    if(!pwInp) return;
    if(pwInp.value !== MSG_ADD_DEL_PW){
        pwErr.textContent = '❌ รหัสไม่ถูกต้อง';
        pwInp.value = ''; pwInp.focus();
        setTimeout(function(){ if(pwErr) pwErr.textContent=''; }, 2000);
        return;
    }
    // Unlock: hide pw row, show body
    var pwRow = document.getElementById('answer-pw-row');
    var body  = document.getElementById('answer-detail-body');
    if(pwRow) pwRow.style.display = 'none';
    if(body)  { body.style.display = 'block'; body.innerHTML = '<div style="padding:20px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>'; }
    // Now load the actual answer data
    _loadAnswerContent(_pendingAnswerEnc);
}

function _loadAnswerContent(enc){
    var body = document.getElementById('answer-detail-body');
    if(!body || !enc) return;
    try{
        var info=JSON.parse(decodeURIComponent(enc));
        if(info.isOp){
            var ops=(window._trainOpDetails||[]).filter(function(d){
                return d.set_id===info.set_id && d.taken_at===info.taken_at;
            });
            if(!ops.length){
                body.innerHTML='<div style="padding:20px;text-align:center;color:#999;">ไม่พบรายละเอียด</div>';
            } else {
                body.innerHTML='<table style="width:100%;font-size:12px;border-collapse:collapse;">'
                    +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
                    +'<th style="padding:6px 10px;">รายการ</th>'
                    +'<th style="padding:6px 10px;width:90px;text-align:center;">ผล</th>'
                    +'<th style="padding:6px 10px;">Comment</th>'
                    +'</tr></thead><tbody>'
                    +ops.map(function(d,di){
                        var bg=di%2===0?'var(--surface)':'var(--surface2)';
                        return'<tr style="background:'+bg+';">'
                            +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);">'+escHtml(d.question||('รายการที่ '+(di+1)))+'</td>'
                            +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);text-align:center;">'+(d.op_passed?'✅ ผ่าน':'❌ ไม่ผ่าน')+'</td>'
                            +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);color:#546e7a;font-style:italic;">'+escHtml(d.comment||'—')+'</td>'
                            +'</tr>';
                    }).join('')+'</tbody></table>';
            }
        } else {
            // Theory: fetch per-question answers
            fetch('/api/exam_results/'+info.id+'/answers').then(r=>r.json()).then(function(d){
                if(!d||d.length===0){
                    body.innerHTML='<div style="padding:16px;text-align:center;color:#999;">'
                        +'ไม่พบรายละเอียดคำตอบ<br><small>(ข้อมูลเก่าก่อน V7.13 ยังไม่ได้เก็บรายข้อ)</small></div>';
                } else {
                    var total=d.length;
                    var correct=d.filter(function(q){return q.is_correct;}).length;
                    body.innerHTML='<div style="font-size:12px;font-weight:700;margin-bottom:10px;padding:8px 12px;'
                        +'background:var(--surface2);border-radius:8px;">'
                        +'ถูก '+correct+'/'+total+' ข้อ</div>'
                        +'<table style="width:100%;font-size:12px;border-collapse:collapse;">'
                        +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
                        +'<th style="padding:6px 10px;">#</th>'
                        +'<th style="padding:6px 10px;">คำถาม</th>'
                        +'<th style="padding:6px 10px;width:60px;text-align:center;">ตอบ</th>'
                        +'<th style="padding:6px 10px;width:60px;text-align:center;">เฉลย</th>'
                        +'<th style="padding:6px 10px;width:60px;text-align:center;">ผล</th>'
                        +'</tr></thead><tbody>'
                        +d.map(function(q,qi){
                            var bg=qi%2===0?'var(--surface)':'var(--surface2)';
                            var ok=q.is_correct;
                            return'<tr style="background:'+bg+';">'
                                +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);text-align:center;color:#888;">'+(qi+1)+'</td>'
                                +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);">'+escHtml(q.question||'')+'</td>'
                                +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);text-align:center;font-weight:700;'
                                +(ok?'color:#1a7a40':'color:#c0392b')+';">'
                                +escHtml((q.answered||'—').toUpperCase())+'</td>'
                                +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);text-align:center;color:#1565c0;font-weight:700;">'
                                +escHtml((q.answer||'').toUpperCase())+'</td>'
                                +'<td style="padding:6px 10px;border-bottom:1px solid var(--border);text-align:center;">'+(ok?'✅':'❌')+'</td>'
                                +'</tr>';
                        }).join('')
                        +'</tbody></table>';
                }
            }).catch(function(){
                body.innerHTML='<div style="padding:20px;text-align:center;color:#e74c3c;">❌ โหลดคำตอบไม่ได้</div>';
            });
        }
    }catch(e){console.error('_loadAnswerContent:',e);}
}
function closeAnswerModal(){
    var modal = document.getElementById('answer-detail-modal');
    if(modal) modal.classList.remove('open');
    _pendingAnswerEnc = null;
}
// openEmpExamModal → uses the combined train modal
function openEmpExamModal(empId){
    var ri=empData.findIndex(function(r){return String(r.employee_id||'')=== String(empId);});
    openEmpTrainModal(empId, ri>=0?ri:0);
}
function closeEmpExamModal(){closeEmpTrainModal();}
function renderEmpExamHome(results){
    var body=getExamBody();
    var theoryResults  = results.filter(function(r){ return r.exam_type==='theory'; });
    var operateResults = results.filter(function(r){ return r.exam_type==='operate'; });
    function makeTable(rows){
        if(rows.length===0) return '<div style="color:#999;font-size:12px;margin-bottom:8px;">ยังไม่มีประวัติ</div>';
        return '<table style="width:100%;font-size:12px;border-collapse:collapse;margin-bottom:8px;">'
            +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
            +'<th style="padding:6px 8px;">ชุดข้อสอบ</th><th>คะแนน</th><th>ผล</th><th>วันที่</th></tr></thead><tbody>'
            +rows.map(function(r){
                var passed=r.passed?'<span style="color:#1a7a40;font-weight:700;">✅ ผ่าน</span>':'<span style="color:#c0392b;font-weight:700;">❌ ไม่ผ่าน</span>';
                return'<tr><td style="padding:5px 8px;border-bottom:1px solid var(--border);">'+escHtml(r.set_name||'')+'</td>'
                    +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;">'+r.score+'/'+r.total+'</td>'
                    +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;">'+passed+'</td>'
                    +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);">'+escHtml(r.taken_at||'')+'</td></tr>';
            }).join('')+'</tbody></table>';
    }
    // Available exam sets grouped by type
    var theorySetList  = examSets.filter(s=>s.exam_type==='theory');
    var operateSetList = examSets.filter(s=>s.exam_type==='operate');
    function makeSetList(sets){
        if(sets.length===0) return '<div style="color:#999;font-size:12px;margin-bottom:12px;">ยังไม่มีชุดข้อสอบ</div>';
        return sets.map(function(s){
            var bCls=s.exam_type==='theory'?'badge-theory':'badge-operate';
            var lbl=s.exam_type==='theory'?'Theory':'Operate';
            return'<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);">'
                +'<span><strong>'+escHtml(s.name)+'</strong> <span class="exam-type-badge '+bCls+'">'+lbl+'</span>'
                +' <span style="font-size:11px;color:var(--text-dim);">(สุ่ม '+s.random_n+' ข้อ)</span></span>'
                +'<button class="btn btn-add" style="font-size:11px;padding:3px 12px;" data-setid="'+s.id+'" onclick="startExam(this.dataset.setid)">▶ สอบ</button>'
                +'</div>';
        }).join('');
    }
    body.innerHTML=
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">'
        +'<div>'
        +'<h3 style="margin:0 0 8px;font-size:13px;color:#1565c0;">📝 Theory</h3>'
        +makeSetList(theorySetList)
        +'<h4 style="margin:10px 0 5px;font-size:12px;color:var(--text-dim);">ประวัติ Theory</h4>'
        +makeTable(theoryResults)
        +'</div>'
        +'<div>'
        +'<h3 style="margin:0 0 8px;font-size:13px;color:#6a1b9a;">🔧 Operate</h3>'
        +makeSetList(operateSetList)
        +'<h4 style="margin:10px 0 5px;font-size:12px;color:var(--text-dim);">ประวัติ Operate</h4>'
        +makeTable(operateResults)
        +'</div>'
        +'</div>';
}
function startExam(setId){
    fetch('/api/exam_sets/'+setId+'/take').then(r=>r.json()).then(function(d){
        if(d.error){alert('❌ '+d.error);return;}
        takeSetData=d; takeAnswers={}; takeRevealed=false; takeCurrentQ=0;
        if(d.exam_type==='operate'){
            renderOperateExam();
        } else {
            renderExamQuestion();
        }
    }).catch(e=>alert('❌ '+e));
}

/* ---- Operate exam: show all items, pass/fail toggle + comment ---- */
var operateAnswers={};  // {qIndex: {passed: 0/1, comment: ''}}
function renderOperateExam(){
    var body=getExamBody();
    var qs=takeSetData.questions;
    operateAnswers={};
    qs.forEach(function(_,i){ operateAnswers[i]={passed:-1,comment:''}; });
    var rows=qs.map(function(q,i){
        return'<div class="op-q-row" id="op-row-'+i+'">'
            +'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">'
            +'<span style="font-size:12px;font-weight:700;color:var(--text-dim);min-width:24px;">'+(i+1)+'.</span>'
            +'<span style="font-size:13px;font-weight:600;flex:1;min-width:140px;">'+escHtml(q.question)+'</span>'
            +'<div style="display:flex;gap:6px;align-items:center;">'
            +'<button class="op-pass-btn" id="op-pass-'+i+'" onclick="setOpAnswer('+i+',1)">✅ ผ่าน</button>'
            +'<button class="op-fail-btn" id="op-fail-'+i+'" onclick="setOpAnswer('+i+',0)">❌ ไม่ผ่าน</button>'
            +'<input class="op-comment-input" id="op-cmt-'+i+'" placeholder="Comment..."'
            +' data-qi="'+i+'" oninput="setOpComment(this)" style="width:160px;">'
            +'</div>'
            +'</div></div>';
    }).join('');
    body.innerHTML='<div style="font-size:12px;color:var(--text-dim);margin-bottom:10px;font-weight:600;">'+escHtml(takeSetData.name)+' ('+qs.length+' รายการ)</div>'
        +'<div style="display:flex;flex-direction:column;gap:6px;">'+rows+'</div>'
        +'<div style="margin-top:16px;display:flex;justify-content:space-between;align-items:center;">'
        +'<span id="op-progress" style="font-size:12px;color:var(--text-dim);"></span>'
        +'<button class="btn btn-save" onclick="submitOperateExam()">💾 บันทึกผล</button>'
        +'</div>';
    updateOpProgress(qs.length);
}
function updateOpProgress(total){
    var done=Object.values(operateAnswers).filter(function(a){return a.passed>=0;}).length;
    var el=document.getElementById('op-progress');
    if(el) el.textContent='ประเมินแล้ว '+done+'/'+total+' รายการ';
}
function setOpAnswer(qi,val){
    operateAnswers[qi].passed=val;
    var passBtn=document.getElementById('op-pass-'+qi);
    var failBtn=document.getElementById('op-fail-'+qi);
    var row=document.getElementById('op-row-'+qi);
    if(passBtn){passBtn.style.background=val===1?'#27ae60':'';passBtn.style.color=val===1?'#fff':'';}
    if(failBtn){failBtn.style.background=val===0?'#e74c3c':'';failBtn.style.color=val===0?'#fff':'';}
    if(row)row.style.background=val===1?'rgba(39,174,96,.08)':val===0?'rgba(231,76,60,.08)':'';
    if(takeSetData) updateOpProgress(takeSetData.questions.length);
}
function setOpComment(el){
    var qi=parseInt(el.getAttribute('data-qi'));
    operateAnswers[qi].comment=el.value;
}
function submitOperateExam(){
    var qs=takeSetData.questions;
    var unset=[];
    qs.forEach(function(_,i){ if(operateAnswers[i].passed===-1)unset.push(i+1); });
    if(unset.length>0){
        if(!confirm('ข้อที่ '+unset.join(', ')+' ยังไม่ได้ประเมิน บันทึกต่อหรือไม่?'))return;
    }
    var score=qs.filter(function(_,i){return operateAnswers[i].passed===1;}).length;
    var pass=score>=qs.length;  // ต้องผ่านทุกข้อจึงผ่าน
    var details=qs.map(function(q,i){return{question_id:q.id,op_passed:operateAnswers[i].passed<0?0:operateAnswers[i].passed,comment:operateAnswers[i].comment};});
    fetch('/api/operate_results',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({employee_id:takeEmpId,set_id:takeSetData.id,score:score,total:qs.length,passed:pass?1:0,details:details})})
    .then(r=>r.json()).then(function(){
        var body=getExamBody();
        body.innerHTML='<div style="text-align:center;padding:30px;">'
            +'<div style="font-size:48px;">'+(pass?'🎉':'📋')+'</div>'
            +'<div style="font-size:22px;font-weight:700;margin:10px 0;">'+score+'/'+qs.length+' ผ่าน</div>'
            +'<div style="font-size:16px;color:'+(pass?'#1a7a40':'#e67e22')+';font-weight:700;">'+(pass?'✅ ผ่านทั้งหมด!':'⚠️ บางข้อยังไม่ผ่าน')+'</div>'
            +'<button class="btn btn-add" style="margin-top:20px;" onclick="openEmpExamModal(takeEmpId)">🔄 ดูประวัติ</button>'
            +'<button class="btn" style="margin-top:20px;margin-left:10px;" onclick="closeEmpExamModal()">ปิด</button>'
            +'</div>';
        loadEmployees();
    }).catch(e=>alert('❌ '+e));
}

function renderExamQuestion(){
    var body=getExamBody();
    var qs=takeSetData.questions;
    var i=takeCurrentQ;
    if(i>=qs.length){showExamResult();return;}
    var q=qs[i];
    var choices=[['a',q.choice_a],['b',q.choice_b],['c',q.choice_c],['d',q.choice_d]].filter(function(c){return c[1];});
    var answered=qs.filter(function(_,idx){return takeAnswers[idx]!==undefined;}).length;
    var prog='ข้อที่ '+(i+1)+'/'+qs.length+' (ตอบแล้ว '+answered+'/'+qs.length+' ข้อ)';
    var btns=choices.map(function(c){
        var extra=c[0]===takeAnswers[i]?' picked':'';
        return'<button class="exam-choice-btn'+extra+'" data-choice="'+c[0]+'" onclick="pickChoice(this)">'
            +c[0].toUpperCase()+'. '+escHtml(c[1])+'</button>';
    }).join('');
    body.innerHTML='<div style="font-size:11px;color:var(--text-dim);margin-bottom:6px;">'+escHtml(takeSetData.name)+' — '+prog+'</div>'
        +'<div class="exam-modal-q"><div class="q-text">'+escHtml(q.question)+'</div>'+btns+'</div>'
        +'<div style="display:flex;gap:8px;justify-content:space-between;margin-top:12px;">'
        +(i>0?'<button class="btn" onclick="takeCurrentQ--;renderExamQuestion()">◀ ก่อนหน้า</button>':'<span></span>')
        +'<div style="display:flex;gap:8px;">'
        +(i<qs.length-1?'<button class="btn btn-save" onclick="takeCurrentQ++;renderExamQuestion()">ถัดไป ▶</button>'
            :'<button class="btn btn-save" onclick="confirmSubmitExam()">ส่งคำตอบ ✅</button>')
        +'</div></div>';
}
function pickChoice(btn){
    var choice=btn.getAttribute('data-choice');
    takeAnswers[takeCurrentQ]=choice;
    renderExamQuestion();
}
function confirmSubmitExam(){
    var qs=takeSetData.questions;
    var unanswered=[];
    qs.forEach(function(_,i){ if(takeAnswers[i]===undefined) unanswered.push(i+1); });
    if(unanswered.length>0){
        if(!confirm('⚠️ ยังมี '+unanswered.length+' ข้อที่ยังไม่ได้ตอบ (ข้อ '+unanswered.join(', ')+')\nต้องการส่งคำตอบหรือไม่?')) return;
    }
    submitExam();
}
function submitExam(){
    var qs=takeSetData.questions;
    var score=qs.filter(function(q,i){return takeAnswers[i]===q.answer;}).length;
    var passScore = takeSetData.pass_score > 0 ? takeSetData.pass_score : Math.ceil(qs.length * 0.7); // ดึงเกณฑ์ผ่าน
    var pass=score>=passScore;
    var ansDetails=qs.map(function(q,i){
        return{question_id:q.id,question:q.question,answered:takeAnswers[i]||'',
               correct_ans:q.answer,is_correct:(takeAnswers[i]===q.answer)?1:0};
    });
    fetch('/api/exam_results',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({employee_id:takeEmpId,set_id:takeSetData.id,score:score,total:qs.length,passed:pass?1:0,answers:ansDetails})})
    .then(function(){
        var body=getExamBody();
        // Show score + full answer review after submit
        var reviewRows=qs.map(function(q,i){
            var ans=takeAnswers[i]||'—';
            var correct=(ans===q.answer);
            var bg=correct?'rgba(39,174,96,.08)':'rgba(231,76,60,.08)';
            return'<tr style="background:'+bg+';font-size:12px;">'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;">'+(i+1)+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);">'+escHtml(q.question)+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;font-weight:700;'
                +(correct?'color:#1a7a40':'color:#e74c3c')+';">'+ans.toUpperCase()+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;color:#1565c0;font-weight:700;">'+q.answer.toUpperCase()+'</td>'
                +'<td style="padding:5px 8px;border-bottom:1px solid var(--border);text-align:center;">'+(correct?'✅':'❌')+'</td>'
                +'</tr>';
        }).join('');
        body.innerHTML='<div style="text-align:center;padding:12px 0 14px;">'
            +'<div style="font-size:40px;">'+(pass?'🎉':'😔')+'</div>'
            +'<div style="font-size:22px;font-weight:700;margin:6px 0;">'+score+'/'+qs.length+' คะแนน</div>'
            +'<div style="font-size:15px;color:'+(pass?'#1a7a40':'#c0392b')+';font-weight:700;">'+(pass?'✅ ผ่าน!':'❌ ไม่ผ่าน')+'</div>'
            +'</div>'
            +'<table style="width:100%;border-collapse:collapse;margin-bottom:14px;">'
            +'<thead><tr style="background:var(--thead-bg);color:var(--thead-fg);">'
            +'<th style="padding:6px 8px;width:36px;">#</th>'
            +'<th style="padding:6px 8px;">คำถาม</th>'
            +'<th style="padding:6px 8px;text-align:center;width:60px;">ตอบ</th>'
            +'<th style="padding:6px 8px;text-align:center;width:60px;">เฉลย</th>'
            +'<th style="padding:6px 8px;text-align:center;width:40px;">ผล</th>'
            +'</tr></thead><tbody>'+reviewRows+'</tbody></table>'
            +'<div style="display:flex;gap:8px;justify-content:flex-end;">'
            +'<button class="btn btn-add" onclick="backToTrainHome()">🔄 กลับ / สอบใหม่</button>'
            +'<button class="btn" onclick="closeEmpExamModal()">ปิด</button>'
            +'</div>';
        loadEmployees();
    }).catch(e=>alert('❌ '+e));
}
"""



def build_tab4_content() -> str:
    """Tab 4 — Machine Problem Tracking"""
    machines_opts = ""
    try:
        conn = sqlite3.connect(DB_MC, timeout=5)
        mcs = [r[0] for r in conn.execute("SELECT name FROM machines ORDER BY name").fetchall()]
        conn.close()
        machines_opts = "".join(f'<option value="{m}">{m}</option>' for m in mcs)
    except Exception:
        pass

    return """
<div style="max-width:1300px;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:10px;">
    <h2 style="margin:0;">🔧 Machine Problem Tracking</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-add"  onclick="openAddMachineModal()">➕ เพิ่มเครื่องใหม่</button>
      <button class="btn btn-save" onclick="openAddProblemModal()">📋 บันทึกปัญหา</button>
      <span style="border-left:1px solid #ccc;height:22px;align-self:center;"></span>
      <button class="btn btn-export" onclick="exportMachines()">📤 Export เครื่อง</button>
      <label class="btn btn-import" style="cursor:pointer;margin:0;">📥 Import เครื่อง
        <input type="file" accept=".xlsx,.xls" style="display:none;" onchange="importMachines(this)">
      </label>
      <span style="border-left:1px solid #ccc;height:22px;align-self:center;"></span>
      <button class="btn btn-export" onclick="exportMCProblems()">📤 Export ปัญหา</button>
      <label class="btn btn-import" style="cursor:pointer;margin:0;">📥 Import ปัญหา
        <input type="file" accept=".xlsx,.xls" style="display:none;" onchange="importMCProblems(this)">
      </label>
    </div>
  </div>

  <!-- stat cards -->
  <div class="mc-stats" id="mc-stats-row">
    <div class="mc-stat-card mc-stat-stop">
      <div class="mc-stat-num" id="mc-stat-stop">-</div>
      <div class="mc-stat-lbl">🔴 Stop</div>
    </div>
    <div class="mc-stat-card mc-stat-run">
      <div class="mc-stat-num" id="mc-stat-run">-</div>
      <div class="mc-stat-lbl">🟡 Broke &amp; Run</div>
    </div>
    <div class="mc-stat-card mc-stat-ok">
      <div class="mc-stat-num" id="mc-stat-ok">-</div>
      <div class="mc-stat-lbl">🟢 OK</div>
    </div>
    <div class="mc-stat-card mc-stat-total">
      <div class="mc-stat-num" id="mc-stat-total">-</div>
      <div class="mc-stat-lbl">📊 ทั้งหมด</div>
    </div>
  </div>

  <!-- view toggle -->
  <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;">
    <span style="font-size:12px;color:var(--text-dim);font-weight:600;">มุมมอง:</span>
    <button class="btn-flt active-flt" id="mc-view-table"  onclick="setMCView('table')">📋 รายการปัญหา</button>
    <button class="btn-flt"            id="mc-view-status" onclick="setMCView('status')">🏭 สถานะเครื่อง</button>
  </div>

  <!-- TABLE SECTION -->
  <div id="mc-table-section">
    <div class="mc-toolbar">
      <input type="text" id="mc-search" placeholder="🔍 ค้นหาเครื่อง / ปัญหา..." oninput="filterMC()">
      <button class="btn-flt active-flt" id="mcf-open" onclick="setMCFilter('open')">⚠️ ยังไม่เสร็จ</button>
      <button class="btn-flt"            id="mcf-ok"   onclick="setMCFilter('ok')">✅ เสร็จแล้ว</button>
      <button class="btn-flt"            id="mcf-all"  onclick="setMCFilter('all')">📋 ทั้งหมด</button>
    </div>
    <div id="mc-table-wrap">
      <div style="padding:30px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>
    </div>
  </div>

  <!-- STATUS GRID SECTION -->
  <div id="mc-status-section" style="display:none;">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px;">
      <input type="text" id="mc-status-search" placeholder="🔍 ค้นหาเครื่อง..."
        style="padding:6px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;width:180px;background:var(--surface);color:var(--text);"
        oninput="renderMCStatus()">
      <span style="font-size:12px;color:var(--text-dim);font-weight:600;">สถานะ:</span>
      <button class="btn-flt active-flt" id="mcst-stop" onclick="toggleMCStatusFilter('stop')">🔴 Stop</button>
      <button class="btn-flt active-flt" id="mcst-run"  onclick="toggleMCStatusFilter('run')">🟡 Broke &amp; Run</button>
      <button class="btn-flt"            id="mcst-ok"   onclick="toggleMCStatusFilter('ok')">🟢 OK</button>
      <button class="btn-flt" style="background:#546e7a;color:#fff;" onclick="setMCStatusAll()">📋 ทั้งหมด</button>
      <span style="border-left:1px solid #ccc;height:20px;"></span>
      <label style="font-size:12px;color:var(--text-dim);font-weight:600;">จำนวน Column:</label>
      <input type="number" id="mc-num-cols" min="0" max="20" value="0" placeholder="Auto"
        style="width:64px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text);"
        oninput="renderMCStatus()" title="0 = Auto, ใส่ตัวเลขเพื่อกำหนดจำนวน column ของการ์ดในแต่ละกลุ่ม">
      <span style="font-size:11px;color:var(--text-dim);">(0=Auto)</span>
    </div>
    <div id="mc-status-grid"></div>
  </div>
</div>

<!-- Modal: Add Machine -->
<div class="modal-overlay" id="add-machine-modal" onclick="if(event.target===this)closeAddMachineModal()">
  <div class="modal-box" style="max-width:380px;">
    <div class="modal-title">🏭 เพิ่มเครื่องใหม่</div>
    <div class="modal-field">
      <label>ชื่อเครื่อง</label>
      <input type="text" id="add-mc-name-input" placeholder="เช่น T01, T2, CCD1">
    </div>
    <div class="modal-field">
      <label>ประเภทเครื่อง</label>
      <input type="text" id="add-mc-type-input" placeholder="เช่น Drilling, CCD, Routing">
    </div>
    <div class="modal-field">
      <label>รหัสผ่าน (level 1)</label>
      <input type="password" id="add-mc-pw" placeholder="••••••" autocomplete="off">
    </div>
    <div class="modal-err" id="add-mc-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeAddMachineModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitAddMachine()">✔ บันทึก</button>
    </div>
  </div>
</div>

<!-- Modal: Edit Problem -->
<div class="modal-overlay" id="edit-problem-modal" onclick="if(event.target===this)closeEditProblemModal()">
  <div class="modal-box" style="max-width:500px;">
    <div class="modal-title">✏️ แก้ไขปัญหา — เครื่อง: <span id="edit-prob-machine-lbl" style="color:var(--accent);"></span></div>
    <input type="hidden" id="edit-prob-id">
    <div class="modal-field">
      <label>ปัญหา</label>
      <textarea id="edit-prob-problem" style="min-height:80px;resize:vertical;" placeholder="อธิบายปัญหา..."></textarea>
    </div>
    <div class="modal-field">
      <label>สถานะ</label>
      <select id="edit-prob-status" style="width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface2);">
        <option value="Stop">🔴 Stop</option>
        <option value="Running &amp; Still broken">🟡 Running &amp; Still broken</option>
        <option value="OK">🟢 OK</option>
      </select>
    </div>
    <div class="modal-field">
      <label>หมายเหตุ</label>
      <textarea id="edit-prob-remark" style="min-height:50px;resize:vertical;" placeholder="..."></textarea>
    </div>
    <div class="modal-field">
      <label>รหัสผ่าน (level 1)</label>
      <input type="password" id="edit-prob-pw" placeholder="••••••" autocomplete="off">
    </div>
    <div class="modal-err" id="edit-prob-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeEditProblemModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitEditProblem()">✔ บันทึก</button>
    </div>
  </div>
</div>


<!-- Modal: Add Problem -->
<div class="modal-overlay" id="add-problem-modal" onclick="if(event.target===this)closeAddProblemModal()">
  <div class="modal-box" style="max-width:500px;">
    <div class="modal-title">📋 บันทึกปัญหาเครื่อง</div>
    <div class="modal-field">
      <label>ประเภทเครื่อง</label>
      <select id="add-prob-type" onchange="onProbTypeChange()"
        style="width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface2);">
        <option value="">— เลือกประเภทเครื่อง —</option>
      </select>
    </div>
    <div class="modal-field">
      <label>เครื่อง</label>
      <select id="add-prob-machine"
        style="width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface2);">
        <option value="">— เลือกประเภทก่อน —</option>
      </select>
    </div>
    <div class="modal-field">
      <label>ปัญหา</label>
      <textarea id="add-prob-problem" style="min-height:80px;resize:vertical;" placeholder="อธิบายปัญหา..."></textarea>
    </div>
    <div class="modal-field">
      <label>สถานะเริ่มต้น</label>
      <select id="add-prob-status" style="width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface2);">
        <option value="Stop">🔴 Stop</option>
        <option value="Running &amp; Still broken">🟡 Running &amp; Still broken</option>
        <option value="OK">🟢 OK</option>
      </select>
    </div>
    <div class="modal-field">
      <label>หมายเหตุ (ไม่บังคับ)</label>
      <textarea id="add-prob-remark" style="min-height:50px;resize:vertical;" placeholder="..."></textarea>
    </div>
    <div class="modal-field">
      <label>รหัสผ่าน (level 1)</label>
      <input type="password" id="add-prob-pw" placeholder="••••••" autocomplete="off">
    </div>
    <div class="modal-err" id="add-prob-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeAddProblemModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitAddProblem()">✔ บันทึก</button>
    </div>
  </div>
</div>
"""


def build_tab5_content() -> str:
    """Tab 5 — Material V7.13: Section1=Materials, Section2=Product PN, Section3=Visualize"""
    return """
<div style="max-width:1400px;">

  <!-- ===== Section 1: วัสดุ ===== -->
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:10px;">
    <h2 style="margin:0;">📦 Section 1 — วัสดุ (Materials)</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-add"    onclick="addMaterial()">➕ เพิ่ม Part Number</button>
      <button class="btn btn-save"   onclick="saveMaterials()">💾 Save วัสดุ</button>
      <button class="btn btn-export" onclick="exportMaterials()">📤 Export Excel</button>
    </div>
  </div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">
    <input type="text" id="mat-search" placeholder="🔍 ค้นหา Part Number / ชื่อ..."
      style="padding:6px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;width:240px;background:var(--surface);color:var(--text);"
      oninput="filterMat()">
    <span style="font-size:12px;color:var(--text-dim);font-weight:600;">หมวด:</span>
    <button class="btn-flt mat-cat-btn" data-cat="Entry"  onclick="setMatCatFilter(this.dataset.cat)">📥 Entry</button>
    <button class="btn-flt mat-cat-btn" data-cat="Backup" onclick="setMatCatFilter(this.dataset.cat)">🔄 Backup</button>
    <span style="font-size:12px;color:var(--text-dim);font-weight:600;">ประเภท:</span>
    <span id="mat-type-btns" style="display:inline-flex;gap:6px;flex-wrap:wrap;">
      <button class="btn-flt mat-type-btn" data-type="Alumi"            onclick="setMatTypeFilter(this.dataset.type)">Alumi</button>
      <button class="btn-flt mat-type-btn" data-type="Coated"           onclick="setMatTypeFilter(this.dataset.type)">Coated</button>
      <button class="btn-flt mat-type-btn" data-type="No Coated"        onclick="setMatTypeFilter(this.dataset.type)">No Coated</button>
      <button class="btn-flt mat-type-btn" data-type="2Side (Composite)" onclick="setMatTypeFilter(this.dataset.type)">2Side</button>
      <button class="btn-flt mat-type-btn" data-type="Urea"             onclick="setMatTypeFilter(this.dataset.type)">Urea</button>
    </span>
  </div>
  <div id="mat-table-wrap">
    <div style="padding:30px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>
  </div>

  <hr style="margin:28px 0;border:none;border-top:2px solid var(--border);">

  <!-- ===== Section 2: Product PN ===== -->
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:10px;">
      <h2 style="margin:0;">🗂️ Section 2 — Product PN</h2>
      <div style="display:flex;gap:8px;">
        <button class="btn btn-add"  onclick="addProductPN()">➕ เพิ่ม Product PN</button>
        <button class="btn btn-save" onclick="savePPNData()">💾 Save</button>
      </div>
    </div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:10px;">
      กว้าง/ยาว = <strong>inch</strong> | หนา = <strong>mil</strong> | Entry = Alumi/Coated/No Coated/2Side | Backup = Urea
    </div>
    <div id="ppn-table-wrap">
      <div style="padding:30px;text-align:center;color:#999;">⏳ กำลังโหลด...</div>
    </div>
  </div>

  <hr style="margin:28px 0;border:none;border-top:2px solid var(--border);">

  <!-- ===== Section 3: Visualize ===== -->
  <div>
    <h2 style="margin:0 0 14px;">📐 Section 3 — Visualize: เปรียบเทียบขนาด</h2>
    <!-- Controls -->
    <div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:14px;">
      <div style="display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap;">
        <div>
          <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px;">🗂️ Product PN</label>
          <select id="viz-ppn-sel" onchange="renderVisualize()"
            style="padding:7px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;min-width:220px;background:var(--surface);color:var(--text);">
            <option value="">— เลือก Product PN —</option>
          </select>
        </div>
        <!-- Entry offset section -->
        <div style="border-left:1px solid var(--border);padding-left:14px;">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
            <input type="checkbox" id="viz-show-entry" checked onchange="toggleVizSection('entry',this.checked)"
              style="width:14px;height:14px;cursor:pointer;">
            <label for="viz-show-entry" style="font-size:11px;font-weight:600;color:#1565c0;cursor:pointer;">📥 Entry</label>
          </div>
          <div id="viz-entry-offset-row" style="display:flex;gap:8px;align-items:center;">
            <label style="font-size:11px;color:var(--text-dim);">Offset X:</label>
            <input type="number" id="viz-entry-ox" value="0" step="0.001"
              style="width:72px;padding:4px 8px;border:1px solid var(--border);border-radius:5px;font-size:12px;" oninput="renderVisualize()">
            <label style="font-size:11px;color:var(--text-dim);">Y:</label>
            <input type="number" id="viz-entry-oy" value="0" step="0.001"
              style="width:72px;padding:4px 8px;border:1px solid var(--border);border-radius:5px;font-size:12px;" oninput="renderVisualize()">
            <span style="font-size:10px;color:var(--text-dim);">inch</span>
          </div>
        </div>
        <!-- Backup offset section -->
        <div style="border-left:1px solid var(--border);padding-left:14px;">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
            <input type="checkbox" id="viz-show-backup" checked onchange="toggleVizSection('backup',this.checked)"
              style="width:14px;height:14px;cursor:pointer;">
            <label for="viz-show-backup" style="font-size:11px;font-weight:600;color:#6a1b9a;cursor:pointer;">🔄 Backup</label>
          </div>
          <div id="viz-backup-offset-row" style="display:flex;gap:8px;align-items:center;">
            <label style="font-size:11px;color:var(--text-dim);">Offset X:</label>
            <input type="number" id="viz-backup-ox" value="0" step="0.001"
              style="width:72px;padding:4px 8px;border:1px solid var(--border);border-radius:5px;font-size:12px;" oninput="renderVisualize()">
            <label style="font-size:11px;color:var(--text-dim);">Y:</label>
            <input type="number" id="viz-backup-oy" value="0" step="0.001"
              style="width:72px;padding:4px 8px;border:1px solid var(--border);border-radius:5px;font-size:12px;" oninput="renderVisualize()">
            <span style="font-size:10px;color:var(--text-dim);">inch</span>
          </div>
        </div>
        <button class="btn btn-save" onclick="renderVisualize()">🔄 คำนวณ</button>
      </div>
    </div>
    <!-- Visualize output -->
    <div id="viz-area" style="border:1px solid var(--border);border-radius:10px;padding:20px;min-height:320px;background:var(--surface);">
      <div style="text-align:center;color:#999;padding:80px 0;">เลือก Product PN เพื่อดูการเปรียบเทียบขนาด</div>
    </div>
  </div>

</div>

<!-- Modal: Add Material -->
<div class="modal-overlay" id="add-mat-modal" onclick="if(event.target===this)closeMatModal()">
  <div class="modal-box" style="max-width:500px;">
    <div class="modal-title">📦 เพิ่ม Part Number ใหม่</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <div class="modal-field">
        <label>หมวด (Category)</label>
        <select id="add-mat-cat" style="width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface2);" onchange="updateAddMatTypes()">
          <option value="Entry">📥 Entry</option>
          <option value="Backup">🔄 Backup</option>
        </select>
      </div>
      <div class="modal-field">
        <label>ประเภท</label>
        <select id="add-mat-type" style="width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface2);">
          <option value="">— เลือก —</option>
        </select>
      </div>
    </div>
    <div class="modal-field">
      <label>Material Part Number</label>
      <input type="text" id="add-mat-pn" placeholder="เช่น FPC-001A">
    </div>
    <div class="modal-field">
      <label>ชื่อวัสดุ (Name)</label>
      <input type="text" id="add-mat-name" placeholder="เช่น FR4 Prepreg">
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;">
      <div class="modal-field"><label>Size X (inch)</label><input type="number" id="add-mat-sx" step="0.01" placeholder="0.00"></div>
      <div class="modal-field"><label>Size Y (inch)</label><input type="number" id="add-mat-sy" step="0.01" placeholder="0.00"></div>
      <div class="modal-field"><label>Thickness (mil)</label><input type="number" id="add-mat-th" step="0.001" placeholder="0.000"></div>
    </div>
    <div class="modal-field">
      <label>รหัสผ่าน (level 1)</label>
      <input type="password" id="add-mat-pw" placeholder="••••••" autocomplete="off">
    </div>
    <div class="modal-err" id="add-mat-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closeMatModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitAddMaterial()">✔ เพิ่ม</button>
    </div>
  </div>
</div>

<!-- Modal: Add Product PN -->
<div class="modal-overlay" id="add-ppn-modal" onclick="if(event.target===this)closePPNModal()">
  <div class="modal-box" style="max-width:560px;">
    <div class="modal-title">🗂️ เพิ่ม Product PN</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <div class="modal-field"><label>Product PN</label><input type="text" id="add-ppn-pn" placeholder="เช่น A0001-001"></div>
      <div class="modal-field"><label>ชื่อ (Name)</label><input type="text" id="add-ppn-name" placeholder="ชื่อผลิตภัณฑ์"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <div class="modal-field">
        <label>📥 Entry (เลือกจาก Section 1)</label>
        <select id="add-ppn-entry" style="width:100%;padding:7px 10px;border:1.5px solid var(--border);border-radius:7px;font-size:12px;font-family:inherit;background:var(--surface2);"></select>
      </div>
      <div class="modal-field">
        <label>🔄 Backup (เลือกจาก Section 1)</label>
        <select id="add-ppn-backup" style="width:100%;padding:7px 10px;border:1.5px solid var(--border);border-radius:7px;font-size:12px;font-family:inherit;background:var(--surface2);"></select>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;">
      <div class="modal-field"><label>Process Code</label><input type="text" id="add-ppn-process" placeholder="เช่น 02S"></div>
      <div class="modal-field"><label>กว้าง (inch)</label><input type="number" id="add-ppn-width" step="0.01" placeholder="0.00"></div>
      <div class="modal-field"><label>ยาว (inch)</label><input type="number" id="add-ppn-length" step="0.01" placeholder="0.00"></div>
      <div class="modal-field"><label>หนา (mil)</label><input type="number" id="add-ppn-thick" step="0.001" placeholder="0.000"></div>
    </div>
    <div class="modal-field"><label>STK</label><input type="text" id="add-ppn-stk" placeholder="STK..."></div>
    <div class="modal-field"><label>รหัสผ่าน (level 1)</label><input type="password" id="add-ppn-pw" placeholder="••••••" autocomplete="off"></div>
    <div class="modal-err" id="add-ppn-err"></div>
    <div class="modal-btns">
      <button class="btn-modal-cancel" onclick="closePPNModal()">ยกเลิก</button>
      <button class="btn-modal-ok" onclick="submitAddProductPN()">✔ เพิ่ม</button>
    </div>
  </div>
</div>
"""


# ==============================================================
#  FLASK ROUTES — MACHINES
# ==============================================================
@flask_app.route("/api/machines", methods=["GET"])
def api_machines_get():
    conn = sqlite3.connect(DB_MC, timeout=10)
    try:
        rows = conn.execute("SELECT id, name, machine_type FROM machines ORDER BY name").fetchall()
        return jsonify([{"id": r[0], "name": r[1], "machine_type": r[2] or ""} for r in rows])
    finally:
        conn.close()


@flask_app.route("/api/machines", methods=["POST"])
def api_machines_post():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    mtype = (data.get("machine_type") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = sqlite3.connect(DB_MC, timeout=10)
    try:
        conn.execute("INSERT INTO machines (name, machine_type) VALUES (?,?)", (name, mtype))
        conn.commit()
        return jsonify({"ok": True, "name": name, "machine_type": mtype})
    except sqlite3.IntegrityError:
        return jsonify({"error": f"เครื่อง '{name}' มีอยู่แล้ว"}), 409
    finally:
        conn.close()


# ==============================================================
#  FLASK ROUTES — MACHINE PROBLEMS
# ==============================================================
@flask_app.route("/api/mc_problems", methods=["GET"])
def api_mc_problems_get():
    conn = sqlite3.connect(DB_MC, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, machine_name, problem, occurred_at, status, completed_at, remark "
            "FROM machine_problems ORDER BY id DESC"
        ).fetchall()
        return jsonify([
            {"id": r[0], "machine_name": r[1], "problem": r[2],
             "occurred_at": r[3], "status": r[4],
             "completed_at": r[5] or "", "remark": r[6] or ""}
            for r in rows
        ])
    finally:
        conn.close()


@flask_app.route("/api/mc_problems", methods=["POST"])
def api_mc_problems_post():
    data = request.get_json(force=True)
    machine = (data.get("machine_name") or "").strip()
    problem = (data.get("problem") or "").strip()
    status  = (data.get("status") or "Stop").strip()
    remark  = (data.get("remark") or "").strip()
    if not machine or not problem:
        return jsonify({"error": "machine_name and problem required"}), 400
    conn = sqlite3.connect(DB_MC, timeout=10)
    try:
        conn.execute(
            "INSERT INTO machine_problems (machine_name, problem, status, remark) VALUES (?,?,?,?)",
            (machine, problem, status, remark)
        )
        conn.commit()
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": pid})
    finally:
        conn.close()


@flask_app.route("/api/mc_problems/<int:pid>/done", methods=["POST"])
def api_mc_problems_done(pid):
    from datetime import datetime
    done_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_MC, timeout=10)
    try:
        conn.execute(
            "UPDATE machine_problems SET status='OK', completed_at=? WHERE id=?",
            (done_at, pid)
        )
        conn.commit()
        return jsonify({"done": pid, "completed_at": done_at})
    finally:
        conn.close()


@flask_app.route("/api/mc_problems/<int:pid>", methods=["PUT"])
def api_mc_problems_put(pid):
    data = request.get_json(force=True)
    problem = (data.get("problem") or "").strip()
    status  = (data.get("status") or "Stop").strip()
    remark  = (data.get("remark") or "").strip()
    if not problem:
        return jsonify({"error": "problem required"}), 400
    conn = sqlite3.connect(DB_MC, timeout=10)
    try:
        # If changing to OK, set completed_at
        if status == "OK":
            from datetime import datetime
            done_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE machine_problems SET problem=?, status=?, remark=?, completed_at=? WHERE id=?",
                (problem, status, remark, done_at, pid)
            )
        else:
            conn.execute(
                "UPDATE machine_problems SET problem=?, status=?, remark=?, completed_at=NULL WHERE id=?",
                (problem, status, remark, pid)
            )
        conn.commit()
        return jsonify({"ok": True, "id": pid})
    finally:
        conn.close()


@flask_app.route("/api/mc_problems/<int:pid>", methods=["DELETE"])
def api_mc_problems_delete(pid):
    conn = sqlite3.connect(DB_MC, timeout=10)
    try:
        conn.execute("DELETE FROM machine_problems WHERE id=?", (pid,))
        conn.commit()
        return jsonify({"deleted": pid})
    finally:
        conn.close()


@flask_app.route("/api/mc_problems/export")
def api_mc_problems_export():
    try:
        conn = sqlite3.connect(DB_MC, timeout=10)
        df = pd.read_sql_query(
            "SELECT id, machine_name, problem, occurred_at, status, completed_at, remark "
            "FROM machine_problems ORDER BY id DESC", conn
        )
        conn.close()
        df.columns = MC_COL_LABELS
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Machine Problems")
        buf.seek(0)
        from flask import make_response
        resp = make_response(buf.read())
        resp.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        resp.headers["Content-Disposition"] = "attachment; filename=machine_problems.xlsx"
        return resp
    except Exception as e:
        return f"Export error: {e}", 500


@flask_app.route("/api/mc_problems/import", methods=["POST"])
def api_mc_problems_import():
    """Import machine problems from Excel — DELETE all then re-insert (replace mode)"""
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    try:
        raw = f.read()
        df_in = pd.read_excel(io.BytesIO(raw), dtype=str, engine="openpyxl").fillna("")
        col_map = {
            "machine_name": "machine_name", "ชื่อเครื่อง": "machine_name",
            "problem": "problem", "ปัญหา": "problem",
            "occurred_at": "occurred_at", "วันที่เกิด": "occurred_at",
            "status": "status", "สถานะ": "status",
            "remark": "remark", "หมายเหตุ": "remark",
        }
        df_in.columns = [col_map.get(str(c).strip().lower(), str(c).strip().lower())
                         for c in df_in.columns]
        if "machine_name" not in df_in.columns or "problem" not in df_in.columns:
            return jsonify({"error": "ต้องมี column machine_name และ problem"}), 400
        for col in ["occurred_at", "status", "remark"]:
            if col not in df_in.columns:
                df_in[col] = ""
        df_in = df_in[df_in["machine_name"].str.strip() != ""].reset_index(drop=True)
        rows = []
        for _, row in df_in.iterrows():
            status = (row.get("status") or "").strip()
            if status not in MC_STATUSES:
                status = "Stop"
            rows.append((
                row["machine_name"].strip(),
                row["problem"].strip(),
                (row.get("occurred_at") or None) or None,
                status,
                row.get("remark", "")
            ))
        conn = sqlite3.connect(DB_MC, timeout=10)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("DELETE FROM machine_problems")
            conn.executemany(
                "INSERT INTO machine_problems (machine_name, problem, occurred_at, status, remark) VALUES (?,?,?,?,?)",
                rows
            )
            conn.execute("COMMIT")
            return jsonify({"imported": len(rows)})
        except Exception as e:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/machines/export")
def api_machines_export():
    try:
        conn = sqlite3.connect(DB_MC, timeout=10)
        df = pd.read_sql_query(
            "SELECT id, name, machine_type FROM machines ORDER BY name", conn
        )
        conn.close()
        df.columns = ["ID", "ชื่อเครื่อง", "ประเภทเครื่อง"]
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Machines")
        buf.seek(0)
        from flask import make_response
        resp = make_response(buf.read())
        resp.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        resp.headers["Content-Disposition"] = "attachment; filename=machines.xlsx"
        return resp
    except Exception as e:
        return f"Export error: {e}", 500


@flask_app.route("/api/machines/import", methods=["POST"])
def api_machines_import():
    """Import machines from Excel — DELETE all then re-insert (replace mode)"""
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    try:
        raw = f.read()
        df_in = pd.read_excel(io.BytesIO(raw), dtype=str, engine="openpyxl").fillna("")
        col_map = {
            "name": "name", "ชื่อเครื่อง": "name",
            "machine_type": "machine_type", "ประเภทเครื่อง": "machine_type",
            "ประเภท": "machine_type",
        }
        df_in.columns = [col_map.get(str(c).strip().lower(), str(c).strip().lower())
                         for c in df_in.columns]
        if "name" not in df_in.columns:
            return jsonify({"error": "ต้องมี column ชื่อเครื่อง (name)"}), 400
        if "machine_type" not in df_in.columns:
            df_in["machine_type"] = ""
        df_in = df_in[df_in["name"].str.strip() != ""].reset_index(drop=True)
        # Build data tuples first (fast)
        rows = [(r["name"].strip(), r.get("machine_type", "").strip()) for _, r in df_in.iterrows()]
        conn = sqlite3.connect(DB_MC, timeout=10)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("DELETE FROM machines")
            conn.executemany("INSERT OR IGNORE INTO machines (name, machine_type) VALUES (?,?)", rows)
            conn.execute("COMMIT")
            return jsonify({"imported": len(rows), "skipped": 0})
        except Exception as e:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==============================================================
#  FLASK ROUTES — SETTINGS
# ==============================================================
@flask_app.route("/api/materials", methods=["GET"])
def api_materials_get():
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, part_number, name, mat_category, material_type, size_x, size_y, thickness "
            "FROM materials ORDER BY mat_category, material_type, part_number"
        ).fetchall()
        return jsonify([
            {"id": r[0], "part_number": r[1], "name": r[2],
             "mat_category": r[3] or "Entry", "material_type": r[4] or "",
             "size_x": r[5], "size_y": r[6], "thickness": r[7]}
            for r in rows
        ])
    finally:
        conn.close()


@flask_app.route("/api/materials", methods=["POST"])
def api_materials_post():
    data = request.get_json(force=True)
    pn   = (data.get("part_number") or "").strip()
    name = (data.get("name") or "").strip()
    cat  = (data.get("mat_category") or "Entry").strip()
    typ  = (data.get("material_type") or "").strip()
    sx   = float(data.get("size_x") or 0)
    sy   = float(data.get("size_y") or 0)
    th   = float(data.get("thickness") or 0)
    if not pn or not name:
        return jsonify({"error": "part_number and name required"}), 400
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        conn.execute(
            "INSERT INTO materials (part_number, name, mat_category, material_type, size_x, size_y, thickness) "
            "VALUES (?,?,?,?,?,?,?)",
            (pn, name, cat, typ, sx, sy, th)
        )
        conn.commit()
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": mid})
    except sqlite3.IntegrityError:
        return jsonify({"error": f"Part Number '{pn}' มีอยู่แล้ว"}), 409
    finally:
        conn.close()


@flask_app.route("/api/materials", methods=["PUT"])
def api_materials_put():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({"error": "expected list"}), 400
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        for row in data:
            conn.execute(
                "UPDATE materials SET part_number=?, name=?, mat_category=?, material_type=?, "
                "size_x=?, size_y=?, thickness=? WHERE id=?",
                (
                    str(row.get("part_number", "")),
                    str(row.get("name", "")),
                    str(row.get("mat_category", "Entry")),
                    str(row.get("material_type", "")),
                    float(row.get("size_x") or 0),
                    float(row.get("size_y") or 0),
                    float(row.get("thickness") or 0),
                    int(row["id"])
                )
            )
        conn.commit()
        return jsonify({"saved": len(data)})
    finally:
        conn.close()


@flask_app.route("/api/materials/<int:mid>", methods=["DELETE"])
def api_materials_delete(mid):
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        conn.execute("DELETE FROM materials WHERE id=?", (mid,))
        conn.commit()
        return jsonify({"deleted": mid})
    finally:
        conn.close()


@flask_app.route("/api/materials/export")
def api_materials_export():
    try:
        conn = sqlite3.connect(DB_MAT, timeout=10)
        df = pd.read_sql_query(
            "SELECT id, part_number, name, mat_category, material_type, size_x, size_y, thickness "
            "FROM materials ORDER BY mat_category, material_type, part_number", conn
        )
        conn.close()
        df.columns = MAT_COL_LABELS
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Materials")
        buf.seek(0)
        from flask import make_response
        resp = make_response(buf.read())
        resp.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        resp.headers["Content-Disposition"] = "attachment; filename=materials.xlsx"
        return resp
    except Exception as e:
        return f"Export error: {e}", 500


# ==============================================================
#  FLASK ROUTES — PRODUCT PN
# ==============================================================
@flask_app.route("/api/product_pn", methods=["GET"])
def api_ppn_get():
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, pn, name, entry_id, backup_id, process_code, "
            "prod_width, prod_length, prod_thickness, stk "
            "FROM product_pn ORDER BY pn"
        ).fetchall()
        return jsonify([
            {"id": r[0], "pn": r[1], "name": r[2] or "",
             "entry_id": r[3], "backup_id": r[4],
             "process_code": r[5] or "",
             "prod_width": r[6] or 0, "prod_length": r[7] or 0,
             "prod_thickness": r[8] or 0, "stk": r[9] or ""}
            for r in rows
        ])
    finally:
        conn.close()


@flask_app.route("/api/product_pn", methods=["POST"])
def api_ppn_post():
    data   = request.get_json(force=True)
    pn     = (data.get("pn") or "").strip()
    name   = (data.get("name") or "").strip()
    ai     = data.get("entry_id") or None
    ui     = data.get("backup_id") or None
    proc   = (data.get("process_code") or "").strip()
    pw     = float(data.get("prod_width") or 0)
    pl     = float(data.get("prod_length") or 0)
    pt     = float(data.get("prod_thickness") or 0)
    stk    = (data.get("stk") or "").strip()
    if not pn:
        return jsonify({"error": "pn required"}), 400
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        conn.execute(
            "INSERT INTO product_pn (pn, name, entry_id, backup_id, process_code, "
            "prod_width, prod_length, prod_thickness, stk) VALUES (?,?,?,?,?,?,?,?,?)",
            (pn, name, ai or None, ui or None, proc, pw, pl, pt, stk)
        )
        conn.commit()
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": rid})
    except sqlite3.IntegrityError:
        return jsonify({"error": f"PN '{pn}' มีอยู่แล้ว"}), 409
    finally:
        conn.close()


@flask_app.route("/api/product_pn", methods=["PUT"])
def api_ppn_put():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({"error": "expected list"}), 400
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        for row in data:
            ai = row.get("entry_id") or None
            ui = row.get("backup_id") or None
            conn.execute(
                "UPDATE product_pn SET pn=?, name=?, entry_id=?, backup_id=?, "
                "process_code=?, prod_width=?, prod_length=?, prod_thickness=?, stk=? WHERE id=?",
                (
                    str(row.get("pn", "")),
                    str(row.get("name", "")),
                    int(ai) if ai else None,
                    int(ui) if ui else None,
                    str(row.get("process_code", "")),
                    float(row.get("prod_width") or 0),
                    float(row.get("prod_length") or 0),
                    float(row.get("prod_thickness") or 0),
                    str(row.get("stk", "")),
                    int(row["id"])
                )
            )
        conn.commit()
        return jsonify({"saved": len(data)})
    finally:
        conn.close()


@flask_app.route("/api/product_pn/<int:rid>", methods=["DELETE"])
def api_ppn_delete(rid):
    conn = sqlite3.connect(DB_MAT, timeout=10)
    try:
        conn.execute("DELETE FROM product_pn WHERE id=?", (rid,))
        conn.commit()
        return jsonify({"deleted": rid})
    finally:
        conn.close()


# ----------------------------------------------
#  SERVER & MAIN
# ----------------------------------------------
def run_web_server():
    try:
        log.info(f"Waitress server starting on {SERVER_IP}:{PORT}")
        waitress_serve(flask_app, host=SERVER_IP, port=PORT,
                       threads=4, channel_timeout=30, cleanup_interval=10)
    except Exception as e:
        log.critical(f"Waitress server crashed: {e}")


# ==============================================================
#  FLASK ROUTES — EXAM SETS & QUESTIONS
# ==============================================================
@flask_app.route("/api/exam_sets", methods=["GET"])
def api_exam_sets_get():
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        rows = conn.execute(
            "SELECT s.id, s.name, s.exam_type, s.random_n, s.created_at, "
            "COUNT(q.id) as q_count, s.pass_score "
            "FROM exam_sets s LEFT JOIN exam_questions q ON q.set_id=s.id "
            "GROUP BY s.id ORDER BY s.id"
        ).fetchall()
        return jsonify([{"id":r[0],"name":r[1],"exam_type":r[2],"random_n":r[3],
                         "created_at":r[4],"q_count":r[5],"pass_score":r[6]} for r in rows])
    finally:
        conn.close()


@flask_app.route("/api/exam_sets", methods=["POST"])
def api_exam_sets_post():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    typ  = (data.get("exam_type") or "theory").strip()
    n    = int(data.get("random_n") or 10)
    p    = int(data.get("pass_score") or max(1, int(n * 0.7)))
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        conn.execute("INSERT INTO exam_sets (name,exam_type,random_n,pass_score) VALUES (?,?,?,?)", (name,typ,n,p))
        conn.commit()
        sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": sid})
    finally:
        conn.close()

@flask_app.route("/api/exam_sets/<int:sid>", methods=["DELETE"])
def api_exam_sets_delete(sid):
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        conn.execute("DELETE FROM exam_questions WHERE set_id=?", (sid,))
        conn.execute("DELETE FROM exam_sets WHERE id=?", (sid,))
        conn.commit()
        return jsonify({"deleted": sid})
    finally:
        conn.close()


@flask_app.route("/api/exam_sets/<int:sid>/questions", methods=["GET"])
def api_exam_questions_get(sid):
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id,set_id,question,choice_a,choice_b,choice_c,choice_d,answer "
            "FROM exam_questions WHERE set_id=? ORDER BY id", (sid,)
        ).fetchall()
        return jsonify([{"id":r[0],"set_id":r[1],"question":r[2],
                         "choice_a":r[3],"choice_b":r[4],"choice_c":r[5],
                         "choice_d":r[6],"answer":r[7]} for r in rows])
    finally:
        conn.close()


@flask_app.route("/api/exam_sets/<int:sid>/questions", methods=["PUT"])
def api_exam_questions_put(sid):
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({"error": "expected list"}), 400
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        conn.execute("DELETE FROM exam_questions WHERE set_id=?", (sid,))
        for q in data:
            conn.execute(
                "INSERT INTO exam_questions (set_id,question,choice_a,choice_b,choice_c,choice_d,answer) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, q.get("question",""), q.get("choice_a",""), q.get("choice_b",""),
                 q.get("choice_c",""), q.get("choice_d",""), q.get("answer","a"))
            )
        conn.commit()
        return jsonify({"saved": len(data)})
    finally:
        conn.close()


@flask_app.route("/api/exam_sets/<int:sid>/random_n", methods=["PUT"])
def api_exam_random_n_put(sid):
    data = request.get_json(force=True)
    n = int(data.get("random_n") or 10)
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        conn.execute("UPDATE exam_sets SET random_n=? WHERE id=?", (n, sid))
        conn.commit()
        return jsonify({"ok": True, "random_n": n})
    finally:
        conn.close()


@flask_app.route("/api/exam_sets/<int:sid>/meta", methods=["PUT"])
def api_exam_meta_put(sid):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    n = int(data.get("random_n") or 10)
    p = int(data.get("pass_score") or max(1, int(n * 0.7)))
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        conn.execute("UPDATE exam_sets SET name=?, random_n=?, pass_score=? WHERE id=?", (name, n, p, sid))
        conn.commit()
        return jsonify({"ok": True, "name": name, "random_n": n, "pass_score": p})
    finally:
        conn.close()


@flask_app.route("/api/operate_results/<emp_id>", methods=["GET"])
def api_operate_results_get(emp_id):
    """Return per-item operate results for an employee, grouped by taken_at"""
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        rows = conn.execute(
            "SELECT o.id, o.set_id, o.question_id, o.op_passed, o.comment, o.taken_at, "
            "q.question "
            "FROM operate_results o "
            "LEFT JOIN exam_questions q ON q.id=o.question_id "
            "WHERE o.employee_id=? ORDER BY o.taken_at DESC, o.id ASC", (emp_id,)
        ).fetchall()
        return jsonify([{
            "id": r[0], "set_id": r[1], "question_id": r[2],
            "op_passed": r[3], "comment": r[4] or "",
            "taken_at": r[5] or "", "question": r[6] or ""
        } for r in rows])
    finally:
        conn.close()


@flask_app.route("/api/operate_results", methods=["POST"])
def api_operate_results_post():
    data = request.get_json(force=True)
    eid    = str(data.get("employee_id","")).strip()
    sid    = int(data.get("set_id") or 0)
    score  = int(data.get("score") or 0)
    total  = int(data.get("total") or 0)
    passed = 1 if data.get("passed") else 0
    details = data.get("details", [])
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        # Save summary to exam_results
        conn.execute(
            "INSERT INTO exam_results (employee_id,set_id,score,total,passed) VALUES (?,?,?,?,?)",
            (eid, sid, score, total, passed)
        )
        # Save per-item results
        for d in details:
            conn.execute(
                "INSERT INTO operate_results (employee_id,set_id,question_id,op_passed,comment) VALUES (?,?,?,?,?)",
                (eid, sid, int(d.get("question_id",0)), int(d.get("op_passed",0)), str(d.get("comment","")))
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@flask_app.route("/api/exam_sets/<int:sid>/export")
def api_exam_export(sid):
    """Export exam questions as Excel"""
    try:
        conn = sqlite3.connect(DB_EXAM, timeout=10)
        row = conn.execute("SELECT name, exam_type, random_n FROM exam_sets WHERE id=?", (sid,)).fetchone()
        if not row:
            return "Not found", 404
        qs = conn.execute(
            "SELECT question, choice_a, choice_b, choice_c, choice_d, answer "
            "FROM exam_questions WHERE set_id=? ORDER BY id", (sid,)
        ).fetchall()
        conn.close()
        df = pd.DataFrame(qs, columns=["Question", "Choice A", "Choice B", "Choice C", "Choice D", "Answer"])
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Questions")
            # meta sheet
            meta_df = pd.DataFrame([{"Set Name": row[0], "Type": row[1], "Random N": row[2]}])
            meta_df.to_excel(writer, index=False, sheet_name="Info")
        buf.seek(0)
        from flask import make_response
        resp = make_response(buf.read())
        resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', row[0])
        resp.headers["Content-Disposition"] = f"attachment; filename=exam_{safe_name}.xlsx"
        return resp
    except Exception as e:
        return f"Export error: {e}", 500


@flask_app.route("/api/exam_sets/<int:sid>/import", methods=["POST"])
def api_exam_import(sid):
    """Import exam questions from Excel — replaces all questions in set"""
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    try:
        raw = f.read()
        df_in = pd.read_excel(io.BytesIO(raw), dtype=str, engine="openpyxl").fillna("")
        # normalize column names
        col_map = {
            "question": "question", "คำถาม": "question",
            "choice a": "choice_a", "a": "choice_a", "ก": "choice_a",
            "choice b": "choice_b", "b": "choice_b", "ข": "choice_b",
            "choice c": "choice_c", "c": "choice_c", "ค": "choice_c",
            "choice d": "choice_d", "d": "choice_d", "ง": "choice_d",
            "answer": "answer", "เฉลย": "answer",
        }
        df_in.columns = [col_map.get(str(c).strip().lower(), str(c).strip().lower())
                         for c in df_in.columns]
        required = ["question"]
        for r in required:
            if r not in df_in.columns:
                return jsonify({"error": f"ไม่พบ column '{r}' ในไฟล์"}), 400
        for col in ["question","choice_a","choice_b","choice_c","choice_d","answer"]:
            if col not in df_in.columns:
                df_in[col] = ""
        df_in = df_in[["question","choice_a","choice_b","choice_c","choice_d","answer"]]
        df_in = df_in[df_in["question"].str.strip() != ""]  # drop blank rows
        conn = sqlite3.connect(DB_EXAM, timeout=10)
        try:
            conn.execute("DELETE FROM exam_questions WHERE set_id=?", (sid,))
            for _, row_data in df_in.iterrows():
                conn.execute(
                    "INSERT INTO exam_questions (set_id,question,choice_a,choice_b,choice_c,choice_d,answer) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (sid, row_data["question"], row_data["choice_a"], row_data["choice_b"],
                     row_data["choice_c"], row_data["choice_d"],
                     (row_data["answer"] or "a").lower().strip())
                )
            conn.commit()
            return jsonify({"imported": len(df_in)})
        finally:
            conn.close()
    except Exception as e:
        log.error(f"Exam import error: {e}")
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/exam_sets/<int:sid>/take", methods=["GET"])
def api_exam_take(sid):
    import random
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        row = conn.execute("SELECT id,name,exam_type,random_n,pass_score FROM exam_sets WHERE id=?", (sid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        qs = conn.execute(
            "SELECT id,question,choice_a,choice_b,choice_c,choice_d,answer "
            "FROM exam_questions WHERE set_id=?", (sid,)
        ).fetchall()
        n = min(row[3], len(qs))
        sample = random.sample(qs, n) if n > 0 else []
        return jsonify({
            "id": row[0], "name": row[1], "exam_type": row[2], "pass_score": row[4],
            "questions": [{"id":q[0],"question":q[1],"choice_a":q[2],"choice_b":q[3],
                           "choice_c":q[4],"choice_d":q[5],"answer":q[6]} for q in sample]
        })
    finally:
        conn.close()

@flask_app.route("/api/exam_results", methods=["POST"])
def api_exam_results_post():
    data    = request.get_json(force=True)
    eid     = str(data.get("employee_id","")).strip()
    sid     = int(data.get("set_id") or 0)
    score   = int(data.get("score") or 0)
    total   = int(data.get("total") or 0)
    passed  = 1 if data.get("passed") else 0
    answers = data.get("answers", [])   # theory per-question answers
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        conn.execute(
            "INSERT INTO exam_results (employee_id,set_id,score,total,passed) VALUES (?,?,?,?,?)",
            (eid, sid, score, total, passed)
        )
        result_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Save theory answers
        for a in answers:
            conn.execute(
                "INSERT INTO theory_answers (result_id,question_id,question,answered,correct_ans,is_correct) "
                "VALUES (?,?,?,?,?,?)",
                (result_id, int(a.get("question_id") or 0), str(a.get("question","")),
                 str(a.get("answered","")), str(a.get("correct_ans","")),
                 1 if a.get("is_correct") else 0)
            )
        conn.commit()
        return jsonify({"ok": True, "result_id": result_id})
    finally:
        conn.close()


@flask_app.route("/api/exam_results/<int:result_id>/answers", methods=["GET"])
def api_theory_answers_get(result_id):
    """Return per-question answers for a theory exam result"""
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        rows = conn.execute(
            "SELECT question_id, question, answered, correct_ans, is_correct "
            "FROM theory_answers WHERE result_id=? ORDER BY id",
            (result_id,)
        ).fetchall()
        return jsonify([{
            "question_id": r[0], "question": r[1],
            "answered": r[2], "answer": r[3], "is_correct": r[4]
        } for r in rows])
    finally:
        conn.close()


@flask_app.route("/api/exam_results/employee/<emp_id>", methods=["DELETE"])
def api_exam_results_delete_employee(emp_id):
    """Delete ALL exam results + answers for one employee"""
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        # Get all result IDs first for cascade delete of theory_answers
        result_ids = [r[0] for r in conn.execute(
            "SELECT id FROM exam_results WHERE employee_id=?", (emp_id,)
        ).fetchall()]
        if result_ids:
            placeholders = ','.join('?' * len(result_ids))
            conn.execute(f"DELETE FROM theory_answers WHERE result_id IN ({placeholders})", result_ids)
        r1 = conn.execute("DELETE FROM exam_results WHERE employee_id=?", (emp_id,))
        r2 = conn.execute("DELETE FROM operate_results WHERE employee_id=?", (emp_id,))
        conn.commit()
        return jsonify({"deleted": r1.rowcount + r2.rowcount + len(result_ids), "ok": True})
    finally:
        conn.close()


@flask_app.route("/api/exam_results/<emp_id>", methods=["GET"])
def api_exam_results_get(emp_id):
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        rows = conn.execute(
            "SELECT r.id,r.set_id,s.name,s.exam_type,r.score,r.total,r.passed,r.taken_at "
            "FROM exam_results r JOIN exam_sets s ON s.id=r.set_id "
            "WHERE r.employee_id=? ORDER BY r.taken_at DESC", (emp_id,)
        ).fetchall()
        return jsonify([{"id":r[0],"set_id":r[1],"set_name":r[2],"exam_type":r[3],
                         "score":r[4],"total":r[5],"passed":r[6],"taken_at":r[7]} for r in rows])
    finally:
        conn.close()


@flask_app.route("/api/exam_results_summary", methods=["GET"])
def api_exam_results_summary():
    """Return ALL passed exam sets per employee (theory/operate) for badge display"""
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        rows = conn.execute(
            "SELECT r.employee_id, s.exam_type, r.score, r.total, r.passed, r.taken_at, s.name, r.id "
            "FROM exam_results r JOIN exam_sets s ON s.id=r.set_id "
            "ORDER BY r.taken_at ASC"   # ASC so latest overwrites for last_X
        ).fetchall()
        summary = {}
        for r in rows:
            eid, etype, score, total, passed, taken_at, set_name, rid = r
            if eid not in summary:
                summary[eid] = {
                    "all_theory": [], "all_operate": [],
                    "last_theory": None, "last_operate": None
                }
            entry = {
                "id": rid, "score": score, "total": total, "passed": passed,
                "taken_at": taken_at, "set_name": set_name or "", "exam_type": etype
            }
            if etype == "theory":
                # Add to all_theory if not duplicate set_name+passed
                existing = [x["set_name"] for x in summary[eid]["all_theory"] if x["passed"]]
                if passed and set_name not in existing:
                    summary[eid]["all_theory"].append(entry)
                summary[eid]["last_theory"] = entry  # always track latest
            else:
                existing = [x["set_name"] for x in summary[eid]["all_operate"] if x["passed"]]
                if passed and set_name not in existing:
                    summary[eid]["all_operate"].append(entry)
                summary[eid]["last_operate"] = entry
        return jsonify(summary)
    finally:
        conn.close()


# ==============================================================
#  FLASK ROUTES — TRAINING VIDEOS
# ==============================================================
@flask_app.route("/api/training_videos", methods=["GET"])
def api_videos_get():
    """Return videos from DB + scan folder for any unlisted files"""
    os.makedirs(TRAINING_VIDEO_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        rows = conn.execute("SELECT id,title,filename,added_at FROM training_videos ORDER BY id").fetchall()
        db_videos = [{"id":r[0],"title":r[1],"filename":r[2],"added_at":r[3]} for r in rows]
        db_filenames = {r[2] for r in rows}

        # Auto-register files in folder that aren't in DB yet
        VIDEO_EXTS = {'.mp4','.webm','.avi','.mov','.mkv','.wmv'}
        try:
            folder_files = [f for f in os.listdir(TRAINING_VIDEO_DIR)
                            if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
        except Exception:
            folder_files = []

        for fn in folder_files:
            if fn not in db_filenames:
                title = os.path.splitext(fn)[0].replace('_',' ')
                conn.execute("INSERT OR IGNORE INTO training_videos (title,filename) VALUES (?,?)", (title, fn))
                conn.commit()
                vid_id = conn.execute("SELECT id FROM training_videos WHERE filename=?", (fn,)).fetchone()
                if vid_id:
                    db_videos.append({"id":vid_id[0],"title":title,"filename":fn,"added_at":""})

        return jsonify(db_videos)
    finally:
        conn.close()


@flask_app.route("/api/training_videos", methods=["POST"])
def api_videos_post():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f   = request.files["file"]
    title = (request.form.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    os.makedirs(TRAINING_VIDEO_DIR, exist_ok=True)
    filename = f.filename or "video.mp4"
    # sanitize
    filename = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
    filepath = os.path.join(TRAINING_VIDEO_DIR, filename)
    f.save(filepath)
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        conn.execute("INSERT INTO training_videos (title,filename) VALUES (?,?)", (title, filename))
        conn.commit()
        vid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": vid, "filename": filename})
    finally:
        conn.close()


@flask_app.route("/api/training_videos/<int:vid>", methods=["DELETE"])
def api_videos_delete(vid):
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        row = conn.execute("SELECT filename FROM training_videos WHERE id=?", (vid,)).fetchone()
        if row:
            filepath = os.path.join(TRAINING_VIDEO_DIR, row[0])
            try:
                os.remove(filepath)
            except Exception:
                pass
            conn.execute("DELETE FROM training_videos WHERE id=?", (vid,))
            conn.commit()
        return jsonify({"deleted": vid})
    finally:
        conn.close()


@flask_app.route("/api/training_video_stream/<int:vid>")
def api_video_stream(vid):
    conn = sqlite3.connect(DB_EXAM, timeout=10)
    try:
        row = conn.execute("SELECT filename FROM training_videos WHERE id=?", (vid,)).fetchone()
        if not row:
            return "Not found", 404
        filepath = os.path.join(TRAINING_VIDEO_DIR, row[0])
        if not os.path.exists(filepath):
            return "File not found", 404
        return send_file(filepath, conditional=True)
    finally:
        conn.close()



# ==============================================================
#  FLASK ROUTES — SETTINGS
# ==============================================================
@flask_app.route("/api/settings/tabs", methods=["GET"])
def api_settings_tabs_get():
    """Return {tab_id: disabled_bool} — always return all tabs enabled on error"""
    try:
        conn = sqlite3.connect(DB_SETTINGS, timeout=5)
        rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'tab_disabled_%'").fetchall()
        conn.close()
        result = {}
        for key, val in rows:
            tab_id = key.replace("tab_disabled_", "")
            result[tab_id] = (val == "1")
        # Ensure all tabs are in result (default false = enabled)
        for tab_id, _ in SETTING_TABS:
            if tab_id not in result:
                result[tab_id] = False
        return jsonify(result)
    except Exception:
        # On any error, return all tabs enabled
        return jsonify({t: False for t, _ in SETTING_TABS})


@flask_app.route("/api/version")
def api_version():
    return jsonify({"version": "7.2", "status": "ok"})


@flask_app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    global _cached_html, _cache_time
    with _cache_lock:
        _cached_html = ""
        _cache_time  = 0.0
    log.info("HTML cache cleared via API")
    return jsonify({"ok": True, "msg": "Cache cleared — next request will rebuild HTML"})


@flask_app.route("/api/settings/tabs", methods=["POST"])
def api_settings_tabs_post():
    """Save {tab_id: disabled_bool}"""
    data = request.get_json(force=True)
    try:
        conn = sqlite3.connect(DB_SETTINGS, timeout=10)
        for tab_id, disabled in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (f"tab_disabled_{tab_id}", "1" if disabled else "0")
            )
        conn.commit()
        conn.close()
        log.info(f"Settings saved: {data}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==============================================================
#  FLASK ROUTES — UPLOAD PATHS & FILES
# ==============================================================
@flask_app.route("/api/upload_paths", methods=["GET"])
def api_upload_paths_get():
    conn = sqlite3.connect(DB_UPLOAD, timeout=5)
    try:
        rows = conn.execute("SELECT id,name,path,created_at FROM upload_paths ORDER BY id").fetchall()
        return jsonify([{"id":r[0],"name":r[1],"path":r[2],"created_at":r[3]} for r in rows])
    finally:
        conn.close()


@flask_app.route("/api/upload_paths", methods=["POST"])
def api_upload_paths_post():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    path = (data.get("path") or "").strip()
    if not name or not path:
        return jsonify({"error": "name and path required"}), 400
    conn = sqlite3.connect(DB_UPLOAD, timeout=10)
    try:
        conn.execute("INSERT INTO upload_paths (name,path) VALUES (?,?)", (name, path))
        conn.commit()
        uid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": uid})
    finally:
        conn.close()


@flask_app.route("/api/upload_paths/<int:uid>", methods=["DELETE"])
def api_upload_paths_delete(uid):
    conn = sqlite3.connect(DB_UPLOAD, timeout=10)
    try:
        conn.execute("DELETE FROM upload_paths WHERE id=?", (uid,))
        conn.commit()
        return jsonify({"deleted": uid})
    finally:
        conn.close()


@flask_app.route("/api/upload_files", methods=["POST"])
def api_upload_files():
    path_id = request.form.get("path_id")
    if not path_id:
        return jsonify({"error": "path_id required"}), 400
    conn = sqlite3.connect(DB_UPLOAD, timeout=5)
    try:
        row = conn.execute("SELECT path FROM upload_paths WHERE id=?", (path_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "path not found"}), 404
    dest_dir = row[0]
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"Cannot create directory: {e}"}), 500
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files"}), 400
    saved = 0
    for f in files:
        fn = re.sub(r'[^\w.\-]', '_', f.filename or "file")
        try:
            f.save(os.path.join(dest_dir, fn))
            saved += 1
        except Exception as e:
            log.error(f"Upload error: {e}")
    return jsonify({"saved": saved, "dest": dest_dir})


if __name__ == "__main__":
    log.info("=== Dashboard V7.13 starting ===")
    init_emp_db()
    init_msg_db()
    init_mc_db()
    init_mat_db()
    init_exam_db()
    init_settings_db()
    init_upload_db()
    build_photo_index()
    get_html_cached()
    Thread(target=run_web_server, daemon=True).start()

    root = st_gui.Tk()
    root.title("A30 Master Server V7.13")
    root.geometry("380x180")
    st_gui.Label(
        root,
        text=f"🚀 Server is ONLINE\n\nURL: http://{DISPLAY_IP}:{PORT}\n\nLog: {LOG_FILE}",
        pady=20, fg="#4a9eff", font=("Arial", 10, "bold")
    ).pack()

    def on_closing():
        pwd = simpledialog.askstring("Exit", "Password:", show="*")
        if pwd == EXIT_PASSWORD:
            log.info("=== Dashboard V7.13 stopped by user ===")
            root.destroy()
            sys.exit()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()
