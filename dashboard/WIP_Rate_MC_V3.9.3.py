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
from threading import Thread
from waitress import serve as waitress_serve
from flask import Flask, Response
import tkinter as st_gui
from tkinter import messagebox, simpledialog

# --- CONFIG ---
SERVER_IP = "0.0.0.0"
PORT = 8501
DB_DIR = r"D:\CKA30_Database"
EXIT_PASSWORD = "@A30.123"
DISPLAY_IP = "10.61.16.1"
CACHE_TTL = 55          # วินาที — refresh cache ก่อน browser reload (60s)
LOG_FILE = r"D:\CKA30_Database\dashboard.log"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("dashboard")

# --- HTML CACHE ---
_cache_lock = threading.Lock()
_cached_html: str = ""
_cache_time: float = 0.0


# --- DATA ENGINE ---
def build_html() -> str:
    """Query DB และสร้าง HTML — เรียกใช้ผ่าน cache เท่านั้น"""
    db_wip = os.path.join(DB_DIR, "MGR_WIP_history.db")
    db_runrate = os.path.join(DB_DIR, "MC_runrate_history.db")

    html = """<html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <style>
        :root {
            --bg:        #f4f4f9;
            --surface:   #ffffff;
            --surface2:  #eef2f7;
            --border:    #d0d7e3;
            --accent:    #1a6fc4;
            --text:      #1a1a2e;
            --text-dim:  #666;
            --radius:    10px;
            --thead-bg:  #2c3e50; --thead-fg: #ffffff;
            --rr-fg:     #1a2a40;
            --rr-index-fg: #1a5276;
            --prev-bg:   #e8f4fd; --prev-fg: #1a5276;
            --curr-bg:   #eafaf1; --curr-fg: #1a5c35;
            --out-bg:    #fef5e7; --out-fg:  #7d4e00;
            --outhr-bg:  #f5eef8; --outhr-fg:#6c3483;
            --all-bg:    #fff9c4; --all-fg:  #7d6608;
            --err-bg:    #ffdada; --err-bd:  #e74c3c; --err-fg: #c0392b;
            --sb-thumb:  #a0aabf;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
        h1 { font-size: 20px; font-weight: 700; letter-spacing: 1px; color: var(--accent); margin-bottom: 6px; }
        h2 { font-size: 18px; font-weight: 700; color: var(--accent); border-left: 5px solid var(--accent); padding: 8px 0 8px 14px; margin: 32px 0 12px; background: var(--surface2); border-radius: 0 var(--radius) var(--radius) 0; }
        h3 { font-size: 15px; font-weight: 700; margin: 16px 0 8px; padding: 5px 12px; border-radius: 6px; display: inline-block; }
        .scroll-container    { overflow-x: auto; overflow-y: hidden; max-width: 100%; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); margin-bottom: 16px; }
        .filter-bar { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px; }
        .filter-bar label { font-size:12px; font-weight:600; color:var(--text-dim); }
        .filter-bar select { font-size:12px; padding:3px 8px; border:1px solid var(--border); border-radius:6px; background:var(--surface); color:var(--text); cursor:pointer; }
        .filter-bar select:focus { outline: 2px solid var(--accent); }
        .scroll-container-xy { overflow-x: auto; overflow-y: auto;   max-width: 100%; max-height: 220px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); margin-bottom: 16px; }
        table { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 11.5px; }
        th, td { border-bottom: 1px solid var(--border); border-right: 1px solid var(--border); padding: 5px 8px; text-align: center; white-space: nowrap; line-height: 1.3; }
        th:last-child, td:last-child { border-right: none; }
        thead th { position: sticky; top: 0; z-index: 10; background: var(--thead-bg); color: var(--thead-fg); font-weight: 600; font-size: 11px; letter-spacing: .3px; border-bottom: 2px solid var(--accent); }
        tbody tr:hover td, tbody tr:hover th { filter: brightness(1.15); }
        .wip-table th:nth-child(1), .wip-table td:nth-child(1) { position: sticky; left: 0;     min-width: 90px;  z-index: 5; }
        .wip-table th:nth-child(2), .wip-table td:nth-child(2) { position: sticky; left: 90px;  min-width: 75px;  z-index: 5; }
        .wip-table th:nth-child(3), .wip-table td:nth-child(3) { position: sticky; left: 165px; min-width: 100px; z-index: 5; border-right: 2px solid var(--accent) !important; }
        .wip-table thead th:nth-child(1), .wip-table thead th:nth-child(2), .wip-table thead th:nth-child(3) { z-index: 15; }
        .wip-table td:nth-child(1), .wip-table td:nth-child(2), .wip-table td:nth-child(3) { background-color: inherit !important; color: inherit !important; }
        .row-prev  { background-color: var(--prev-bg)  !important; color: var(--prev-fg);  }
        .row-curr  { background-color: var(--curr-bg)  !important; color: var(--curr-fg);  }
        .row-out   { background-color: var(--out-bg)   !important; color: var(--out-fg);   }
        .row-outhr { background-color: var(--outhr-bg) !important; color: var(--outhr-fg); font-style: italic; }
        .row-all   { background-color: var(--all-bg)   !important; color: var(--all-fg);   font-weight: 700; }
        .row-all td{ background-color: var(--all-bg)   !important; color: var(--all-fg);   }
        .runrate-table thead th:nth-child(1) { position: sticky; left: 0; z-index: 15; border-right: 2px solid var(--accent) !important; }
        .runrate-table tbody th { position: sticky; left: 0; z-index: 5; background: var(--surface) !important; color: var(--accent); border-right: 2px solid var(--accent); }
        .runrate-table tr              { background: var(--surface);  }
        .runrate-table tr:nth-child(even) { background: var(--surface2); }
        .runrate-table tbody tr:nth-child(even) th { background: var(--surface2) !important; }
        .runrate-table td { color: var(--rr-fg); }
        .runrate-table tbody th { color: var(--rr-index-fg) !important; }
        .graph-container { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; margin-bottom: 20px; }
        .error   { color: var(--err-fg); font-weight: bold; padding: 10px 14px; background: var(--err-bg); border: 1px solid var(--err-bd); border-radius: var(--radius); margin-bottom: 10px; }
        .info-bar{ font-size: 11px; color: var(--text-dim); margin-bottom: 14px; padding: 4px 0; border-bottom: 1px solid var(--border); display:flex; align-items:center; gap:12px; }
        ::-webkit-scrollbar { height: 6px; width: 6px; }
        ::-webkit-scrollbar-track { background: var(--surface); }
        ::-webkit-scrollbar-thumb { background: var(--sb-thumb); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--accent); }


    </style>
    <script>
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
        window.onload = scrollToRight;
    </script>
    </head><body>
    <h1>📊 A30 Mechanical Drill Dashboard V3.9</h1>
    """

    from datetime import datetime
    html += f"<div class='info-bar'>🕐 Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>"

    if not os.path.exists(DB_DIR):
        return html + f"<div class='error'>❌ ไม่พบโฟลเดอร์ฐานข้อมูลที่: {DB_DIR}</div></body></html>"

    # --- READ WIP DB (with guaranteed close) ---
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

    # --- SECTION 1: WIP & OUTPUT TABLE (แยก Previous / Current+Out) ---
    if not df_wip_raw.empty:
        try:
            df = df_wip_raw.copy()
            df['record_time'] = pd.to_datetime(df['record_time']).dt.strftime('%m-%d<br>%H:%M')
            last_48 = sorted(df['record_time'].unique())[-48:]
            df = df[df['record_time'].isin(last_48)]
            time_cols = sorted(df['record_time'].unique())

            html += "<h2>📌 Section 1: WIP & Output</h2>"

            # ── ตาราง 1a: Previous WIP + Prev Out + Prev Out/hr (ไม่แสดงแถว ALL) ──
            df_prev = df[~df['pn'].str.upper().str.startswith('ALL', na=False)]

            piv_prev_wip = df_prev.pivot_table(
                index=['pn', 'name'], columns='record_time',
                values='prev_wip', aggfunc='last'
            ).reindex(columns=time_cols).fillna(0)

            piv_prev_out = df_prev.pivot_table(
                index=['pn', 'name'], columns='record_time',
                values='prev_out', aggfunc='last'
            ).reindex(columns=time_cols).fillna(0)

            # คำนวณ Prev Out/hr = prev_out[t] - prev_out[t-1]
            piv_prev_outhr = piv_prev_out.diff(axis=1)
            piv_prev_outhr.iloc[:, 0] = float('nan')

            prev_sorted_idx = sorted(piv_prev_wip.index, key=lambda x: str(x[0]))

            prev_pns = sorted(set(p for (p,n) in prev_sorted_idx))
            prev_opts = "".join(f"<option value='{p}'>{p}</option>" for p in prev_pns)
            html += "<h3 style='color:#2980b9; margin-top:16px;'>📋 Table A &nbsp;|&nbsp; 🔙 Previous Process</h3>"
            html += (
                "<div class='filter-bar'>"
                "<label>🔍 Filter PN:</label>"
                f"<select onchange=\"filterTable(this,'tbody-prev')\"><option value='ALL'>— All PN —</option>{prev_opts}</select>"
                "</div>"
            )
            tbl_prev = "<table class='wip-table'><thead><tr><th>Type</th><th>PN</th><th>Name</th>"
            for col in piv_prev_wip.columns:
                tbl_prev += f"<th>{col}</th>"
            tbl_prev += "</tr></thead><tbody id='tbody-prev'>"

            # กลุ่ม 1: Prev WIP ทุก PN
            for (row_pn, row_name) in prev_sorted_idx:
                tbl_prev += f"<tr class='row-prev'><td>🔙 Prev WIP</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_prev_wip.loc[(row_pn, row_name)]:
                    tbl_prev += f"<td>{int(val)}</td>"
                tbl_prev += "</tr>"

            # กลุ่ม 2: Prev Out ทุก PN
            for (row_pn, row_name) in prev_sorted_idx:
                tbl_prev += f"<tr class='row-out'><td>🔙 Prev Out</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_prev_out.loc[(row_pn, row_name)]:
                    tbl_prev += f"<td>{int(val)}</td>"
                tbl_prev += "</tr>"

            # กลุ่ม 3: Prev Out/hr ทุก PN
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

            # ── ตาราง 1b: Current WIP + Output + Out/hr รวมกัน (Type column) + มี ALL ──

            # pivot Curr และ Out แยกก่อน
            piv_curr = df.pivot_table(
                index=['pn', 'name'], columns='record_time',
                values='wip_n', aggfunc='last'
            ).reindex(columns=time_cols).fillna(0)

            piv_out = df.pivot_table(
                index=['pn', 'name'], columns='record_time',
                values='total_out', aggfunc='last'
            ).reindex(columns=time_cols).fillna(0)

            # คำนวณ Out/hr = out[t] - out[t-1], คอลัมน์แรกเป็น "-"
            piv_outhr = piv_out.diff(axis=1)          # diff ตามแนวคอลัมน์ (เวลา)
            piv_outhr.iloc[:, 0] = float('nan')       # คอลัมน์แรกไม่มี prev → NaN

            # เรียง index: PN ปกติก่อน ALL อยู่ท้าย
            def sort_key(pn): return (1 if str(pn).upper().startswith('ALL') else 0, str(pn))
            sorted_idx = sorted(piv_curr.index, key=lambda x: sort_key(x[0]))
            piv_curr   = piv_curr.reindex(sorted_idx)
            piv_out    = piv_out.reindex(sorted_idx)
            piv_outhr  = piv_outhr.reindex(sorted_idx)

            co_pns = sorted(set(p for (p,n) in sorted_idx if not str(p).upper().startswith('ALL')))
            co_all_pns = sorted(set(p for (p,n) in sorted_idx if str(p).upper().startswith('ALL')))
            co_opts = "".join(f"<option value='{p}'>{p}</option>" for p in co_pns)
            co_all_opts = "".join(f"<option value='{p}'>{p}</option>" for p in co_all_pns)
            html += "<h3 style='color:#27ae60; margin-top:16px;'>📋 Table B &nbsp;|&nbsp; 📦 Current WIP &amp; ✅ Output</h3>"
            html += (
                "<div class='filter-bar'>"
                "<label>🔍 Filter PN:</label>"
                f"<select onchange=\"filterTable(this,'tbody-co')\"><option value='ALL'>— All PN —</option>{co_opts}"
                f"<option disabled>── ALL group ──</option>{co_all_opts}</select>"
                "</div>"
            )
            tbl_co = "<table class='wip-table'><thead><tr><th>Type</th><th>PN</th><th>Name</th>"
            for col in piv_curr.columns:
                tbl_co += f"<th>{col}</th>"
            tbl_co += "</tr></thead><tbody id='tbody-co'>"

            # กลุ่ม 1: แถว Curr ทุก PN
            for (row_pn, row_name) in sorted_idx:
                is_all = str(row_pn).upper().startswith('ALL')
                rc = "row-all" if is_all else "row-curr"
                tbl_co += f"<tr class='{rc}'><td>📦 Curr</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_curr.loc[(row_pn, row_name)]:
                    tbl_co += f"<td>{int(val)}</td>"
                tbl_co += "</tr>"

            # กลุ่ม 2: แถว Out ทุก PN
            for (row_pn, row_name) in sorted_idx:
                is_all = str(row_pn).upper().startswith('ALL')
                rc = "row-all" if is_all else "row-out"
                tbl_co += f"<tr class='{rc}'><td>✅ Out</td><td>{row_pn}</td><td>{row_name}</td>"
                for val in piv_out.loc[(row_pn, row_name)]:
                    tbl_co += f"<td>{int(val)}</td>"
                tbl_co += "</tr>"

            # กลุ่ม 3: แถว Out/hr ทุก PN
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

            log.info("Section 1 OK (prev separated)")
        except Exception as e:
            log.error(f"Section 1 error: {e}")
            html += f"<div class='error'>⚠️ Section 1 error: {e}</div>"

    # --- SECTION 2: ALL TREND GRAPH ---
    if not df_wip_raw.empty:
        try:
            df_all = df_wip_raw[df_wip_raw['pn'].str.startswith('ALL', na=False)].copy()
            if not df_all.empty:
                df_all['record_time'] = pd.to_datetime(df_all['record_time'])
                df_all = df_all.sort_values('record_time')
                cutoff = df_all['record_time'].max() - pd.Timedelta(days=4)
                df_all = df_all[df_all['record_time'] >= cutoff]
                df_trend = df_all.groupby('record_time').agg({'wip_n': 'sum', 'total_out': 'sum'}).reset_index()
                fig = px.line(
                    df_trend, x='record_time', y=['wip_n', 'total_out'],
                    labels={'value': 'Units', 'record_time': 'Time', 'variable': 'Type'}
                )
                fig.update_layout(height=400, margin=dict(l=20, r=20, t=40, b=20), template="plotly_white")
                html += f"<h2>📈 Section 2: Overall Trend (ALL)</h2><div class='graph-container'>{pio.to_html(fig, full_html=False, include_plotlyjs='cdn')}</div>"
                log.info("Section 2 OK")
        except Exception as e:
            log.error(f"Section 2 error: {e}")
            html += f"<div class='error'>⚠️ Section 2 error: {e}</div>"

    # --- SECTION 3: MACHINE RUNRATE ---
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
                    piv_rr.columns.name = None  # ลบแถว 'machine_name' ออก
                    piv_rr.index.name   = None  # ลบแถว 'Label' ออก
                    rr_html = piv_rr.to_html(escape=False, classes='runrate-table')
                    html += f"<h2>⚙️ Section 3: Machine Runrate History</h2><div class='scroll-container-xy'>{rr_html}</div>"
                    log.info("Section 3 OK")
        except Exception as e:
            log.error(f"Section 3 error: {e}")
            html += f"<div class='error'>⚠️ Section 3 error: {e}</div>"
        finally:
            if conn:
                conn.close()

    html += "</body></html>"
    return html


def get_html_cached() -> str:
    """คืน HTML จาก cache ถ้ายังไม่หมดอายุ ไม่งั้น build ใหม่"""
    global _cached_html, _cache_time
    with _cache_lock:
        if time.time() - _cache_time > CACHE_TTL or not _cached_html:
            log.info("Cache miss — rebuilding HTML...")
            try:
                _cached_html = build_html()
                _cache_time = time.time()
                log.info("HTML cache updated")
            except Exception as e:
                log.error(f"build_html failed: {e}")
                if not _cached_html:
                    _cached_html = f"<html><body><div style='color:red'>Build error: {e}</div></body></html>"
        return _cached_html


# --- FLASK APP ---
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return Response(get_html_cached(), mimetype="text/html; charset=utf-8")


# --- SERVER ENGINE (Waitress) ---
def run_web_server():
    try:
        log.info(f"Waitress server starting on {SERVER_IP}:{PORT}")
        waitress_serve(
            flask_app,
            host=SERVER_IP,
            port=PORT,
            threads=4,               # รองรับ client พร้อมกัน
            channel_timeout=30,
            cleanup_interval=10,
        )
    except Exception as e:
        log.critical(f"Waitress server crashed: {e}")


# --- MAIN ---
if __name__ == "__main__":
    log.info("=== Dashboard V3.9 starting ===")

    # Pre-build cache ก่อน server ขึ้น
    get_html_cached()

    Thread(target=run_web_server, daemon=True).start()

    root = st_gui.Tk()
    root.title("A30 Master Server V3.9")
    root.geometry("380x180")
    st_gui.Label(
        root,
        text=f"🚀 Server is ONLINE\n\nURL: http://{DISPLAY_IP}:{PORT}\n\nLog: {LOG_FILE}",
        pady=20, fg="#4a9eff", font=("Arial", 10, "bold")
    ).pack()

    def on_closing():
        pwd = simpledialog.askstring("Exit", "Password:", show="*")
        if pwd == EXIT_PASSWORD:
            log.info("=== Dashboard stopped by user ===")
            root.destroy()
            sys.exit()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()