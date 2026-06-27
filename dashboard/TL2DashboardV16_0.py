import socket
import threading
import time
import os
import sys
import logging
import sqlite3
import json
import tkinter as tk
from tkinter import simpledialog, messagebox
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template_string, request, send_file
from waitress import serve  # ใช้ Waitress สำหรับรัน Production
import requests as http_requests
from requests_ntlm import HttpNtlmAuth
from bs4 import BeautifulSoup

# ================= การตั้งค่า (Configuration) =================
SERVER_IP = "10.61.16.1"
SERVER_PORT = 6370
WEB_PORT = 8080
PASSWORD_TO_CLOSE = "@A30.123"   # รหัสผ่านสำหรับปิดโปรแกรม
COLOR_UNLOCK_CODE = "999"        # รหัสปลดล็อคเปลี่ยนสีครั้งแรก
DB_DIR  = r"D:\CKA30_Database"
DB_PATH = os.path.join(DB_DIR, "tl2_dashboard.db")
PHOTO_DIR = r"D:\A30-Monitoring\Employee Picture"
# ==========================================================

# ================= MGR Credentials =================
MGR_LOGIN_URL = "http://home30.compeq.co.th/JQWebClient/LogOn.aspx"
MGR_API_URL   = "http://home30.compeq.co.th/JQWebClient/handler/JQDataHandle.ashx?RemoteName=SMGR_PRODUCT_PLC"
MGR_CREDENTIALS_FILE = r"D:\CKA30_Database\MGR_credentials.json"

def load_mgr_credentials():
    """อ่าน username/password จาก MGR_credentials.json"""
    import json
    try:
        with open(MGR_CREDENTIALS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # ใช้ user1 เป็นหลัก ถ้าไม่มีให้ fallback user2
        user = data.get('user1') or data.get('user2') or {}
        username = user.get('username', '')
        password = user.get('password', '')
        if not username or not password:
            raise ValueError("ไม่พบ username หรือ password ใน credentials file")
        return username, password
    except Exception as e:
        logging.error(f"load_mgr_credentials error: {e}")
        raise RuntimeError(f"โหลด MGR credentials ไม่ได้: {e}\nตรวจสอบไฟล์: {MGR_CREDENTIALS_FILE}")

MGR_DOMAIN = "KDOMAIN"
# ====================================================

# ระบบ Logging: บันทึก Error ลงไฟล์เมื่อไม่มีหน้าต่าง Console
logging.basicConfig(
    filename='dashboard_system.log', 
    level=logging.ERROR, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

machine_data = {}
app = Flask(__name__)

# ================= SQLite DB Init =================
def init_db():
    """สร้างตาราง DB สำหรับ color, layout"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS part_colors (
        part_no TEXT PRIMARY KEY,
        color   TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS machine_layout (
        machine TEXT PRIMARY KEY,
        col     INTEGER NOT NULL,
        row     INTEGER NOT NULL,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS layout_spacers (
        id      INTEGER PRIMARY KEY,
        type    TEXT NOT NULL,
        idx     INTEGER NOT NULL,
        UNIQUE(type, idx)
    )""")
    conn.commit()
    conn.close()

def db_get_colors():
    """โหลด partColorMap จาก DB"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute("SELECT part_no, color FROM part_colors").fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}

def db_set_color(part_no, color):
    """บันทึกสีลง DB"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("INSERT OR REPLACE INTO part_colors (part_no, color) VALUES (?,?)", (part_no, color))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"db_set_color error: {e}")

def db_get_layout():
    """โหลด layout + spacers จาก DB"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = [r for r in conn.execute(
            "SELECT machine, col, row FROM machine_layout"
        ).fetchall() if not r[0].startswith('__')]
        spacer_rows = [r[0] for r in conn.execute(
            "SELECT idx FROM layout_spacers WHERE type='row' ORDER BY idx"
        ).fetchall()]
        spacer_cols = [r[0] for r in conn.execute(
            "SELECT idx FROM layout_spacers WHERE type='col' ORDER BY idx"
        ).fetchall()]
        conn.close()
        result = {r[0]: {"c": r[1], "r": r[2]} for r in rows}
        result["__spacerRows"] = {"spacers": spacer_rows}
        result["__spacerCols"] = {"spacers": spacer_cols}
        return result
    except Exception:
        return {}

def db_save_layout(layout_dict):
    """บันทึก layout + spacers ลง DB"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        # Save machines (skip __ metadata keys)
        for machine, pos in layout_dict.items():
            if machine.startswith("__"):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO machine_layout (machine, col, row) VALUES (?,?,?)",
                (machine, pos["c"], pos["r"])
            )
        # Save spacers separately
        if "__spacerRows" in layout_dict:
            conn.execute("DELETE FROM layout_spacers WHERE type='row'")
            for idx in (layout_dict["__spacerRows"].get("spacers") or []):
                conn.execute("INSERT OR IGNORE INTO layout_spacers (type, idx) VALUES ('row',?)", (int(idx),))
        if "__spacerCols" in layout_dict:
            conn.execute("DELETE FROM layout_spacers WHERE type='col'")
            for idx in (layout_dict["__spacerCols"].get("spacers") or []):
                conn.execute("INSERT OR IGNORE INTO layout_spacers (type, idx) VALUES ('col',?)", (int(idx),))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"db_save_layout error: {e}")

# ================= Employee Photo Helper =================
_photo_index = {}  # eid (str) -> full file path
_photo_index_lock = threading.Lock()

def build_photo_index():
    """Scan PHOTO_DIR และ build index: employee_id -> path"""
    import re
    idx = {}
    if not os.path.isdir(PHOTO_DIR):
        return
    for fname in os.listdir(PHOTO_DIR):
        m = re.match(r'^(\d+)', fname)
        if m:
            eid = m.group(1).lstrip('0') or '0'
            idx[eid] = os.path.join(PHOTO_DIR, fname)
    with _photo_index_lock:
        _photo_index.clear()
        _photo_index.update(idx)

def get_photo_path(employee_id):
    eid = str(employee_id).lstrip('0') or '0'
    with _photo_index_lock:
        return _photo_index.get(eid)

# ================= MGR PLC Data Cache =================
mgr_plc_cache = []          # list of records จาก API
mgr_plc_lock  = threading.Lock()
mgr_last_fetch = 0.0        # epoch ของการ fetch ล่าสุด
MGR_FETCH_INTERVAL = 300    # วินาที: fetch ใหม่ทุก 5 นาที

def _mgr_login_and_fetch(start_date_str: str, end_date_str: str) -> list:
    """Login MGR แล้วดึง SMGR_PRODUCT_PLC สำหรับ date range ที่กำหนด"""
    MGR_USERNAME, MGR_PASSWORD = load_mgr_credentials()
    session = http_requests.Session()
    session.auth = HttpNtlmAuth(f'{MGR_DOMAIN}\\{MGR_USERNAME}', MGR_PASSWORD)

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en,th;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "DNT": "1",
    }
    login_headers = base_headers.copy()
    login_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    login_headers["Upgrade-Insecure-Requests"] = "1"

    # Step 1: GET login page → ดึง ViewState
    resp = session.get(MGR_LOGIN_URL, headers=login_headers, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    vs   = soup.find('input', id='__VIEWSTATE')
    ev   = soup.find('input', id='__EVENTVALIDATION')
    vg   = soup.find('input', id='__VIEWSTATEGENERATOR')
    if not vs:
        raise RuntimeError("ไม่พบ __VIEWSTATE บนหน้า Login")

    # Step 2: POST login
    login_payload = {
        "__EVENTTARGET": "", "__EVENTARGUMENT": "",
        "__VIEWSTATE": vs['value'],
        "__VIEWSTATEGENERATOR": vg['value'] if vg else "",
        "__EVENTVALIDATION": ev['value'] if ev else "",
        "Login1$UserName": MGR_USERNAME,
        "Login1$Password": MGR_PASSWORD,
        "Login1$Database": "WIP",
        "Login1$Solution": "SL_MGR",
        "Login1$LoginButton": "Login",
        "hfOValue": "", "hfNValue": ""
    }
    lr = session.post(MGR_LOGIN_URL, data=login_payload, headers=login_headers,
                      allow_redirects=False, timeout=10)
    if lr.status_code not in [200, 302]:
        raise RuntimeError(f"Login MGR failed: HTTP {lr.status_code}")

    # Step 3: POST API
    api_headers = base_headers.copy()
    api_headers["X-Requested-With"] = "XMLHttpRequest"
    api_headers["Content-Type"]     = "application/x-www-form-urlencoded; charset=UTF-8"
    api_headers["Referer"]          = "http://home30.compeq.co.th/JQWebClient/SL_MGR_PRODUCT/WMGR_PRODUCT_PLC.aspx?undefined"
    api_headers["Origin"]           = "http://home30.compeq.co.th"
    api_headers["Host"]             = "home30.compeq.co.th"
    api_headers["Cache-Control"]    = "no-cache"
    api_headers["Pragma"]           = "no-cache"

    api_payload = {
        "mode": "method",
        "method": "ExecPLC",
        "parameters": f"CK|A30|Md|ALL|||{start_date_str}|{end_date_str}|#dataGridMaster"
    }
    dr = session.post(MGR_API_URL, data=api_payload, headers=api_headers, timeout=15)
    dr.raise_for_status()
    result = dr.json()
    if result is False or not isinstance(result, list):
        return []
    return result

def mgr_plc_collector():
    """Background thread: ดึงข้อมูล MGR PLC ทุก MGR_FETCH_INTERVAL วินาที"""
    global mgr_plc_cache, mgr_last_fetch
    while True:
        try:
            now   = datetime.now()
            start = (now - timedelta(days=1)).strftime("%Y.%m.%d")
            end   = now.strftime("%Y.%m.%d")
            records = _mgr_login_and_fetch(start, end)
            with mgr_plc_lock:
                mgr_plc_cache = records
                mgr_last_fetch = time.time()
        except Exception as e:
            logging.error(f"MGR PLC fetch error: {e}")
        time.sleep(MGR_FETCH_INTERVAL)

CONFIG_FILE = r'D:\CKA30_Database\MGR_PN_Observer.config'
pn_config = {}   # key=7-digit PN string → {stk, cycle_min}

def load_pn_config():
    """อ่าน MGR_PN_Observer.config แล้วสร้าง dict key=7หลัก → stk, cycle"""
    global pn_config
    result = {}
    try:
        if not os.path.exists(CONFIG_FILE):
            return
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                line = line.strip()
                if i == 0 or not line or line.startswith('('):
                    continue
                cols = [c.strip() for c in line.split(',')]
                if len(cols) < 9 or cols[0].upper() != 'Y':
                    continue
                raw_pn = cols[1].strip()
                # ตัด suffix หลังจุดออก แล้วดึงตัวเลข เช่น b6970511.pk1k → 6970511
                base_pn = raw_pn.split('.')[0]
                digits = ''.join(c for c in base_pn if c.isdigit())
                if len(digits) >= 7:
                    key = digits[:7]
                    try:
                        stk       = int(cols[7]) if cols[7].strip() else 0
                        cycle_min = int(cols[8]) if cols[8].strip() else 0   # หน่วยเป็นนาทีอยู่แล้ว
                    except (ValueError, IndexError):
                        stk, cycle_min = 0, 0
                    result[key] = {'stk': stk, 'cycle_min': cycle_min}
    except Exception as e:
        logging.error(f"load_pn_config error: {e}")
    pn_config = result

load_pn_config()

def decode_string(byte_array):
    end_idx = byte_array.find(b'\x00')
    if end_idx != -1: 
        byte_array = byte_array[:end_idx]
    return byte_array.decode('ascii', errors='ignore').strip()

# ================= แผนกหลังบ้าน: ดึงข้อมูล (Data Collector) =================
def data_collector():
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((SERVER_IP, SERVER_PORT))

            while True:
                sock.sendall(b'\xaa')
                data = sock.recv(4096)
                if not data: break
                
                if len(data) == 304:
                    m_name = decode_string(data[0:20])
                    u_name = decode_string(data[24:44]) 
                    p_name = decode_string(data[44:100])
                    
                    spindle_byte = data[107] 
                    spindles = [
                        bool(spindle_byte & 1), bool(spindle_byte & 2), bool(spindle_byte & 4),
                        bool(spindle_byte & 8), bool(spindle_byte & 16), bool(spindle_byte & 32)
                    ]
                    
                    m_status = data[106]
                    
                    sim_time = int.from_bytes(data[110:112], byteorder='little', signed=False)

                    online_sec = int.from_bytes(data[296:300], byteorder='little', signed=False)
                    stop_sec = int.from_bytes(data[300:304], byteorder='little', signed=False)
                    
                    calculated_rate = 0.0
                    if online_sec > 0:
                        calculated_rate = (1 - (stop_sec / online_sec)) * 100
                        calculated_rate = max(0.0, min(100.0, calculated_rate))

                    if m_name != "":
                        if m_name not in machine_data:
                            machine_data[m_name] = {}
                        
                        machine_data[m_name].update({
                            "part_no": p_name,
                            "user": u_name,
                            "status": m_status, 
                            "run_rate": f"{calculated_rate:.1f}",
                            "spindles": spindles,
                            "sim_time": sim_time,
                            "last_update": time.strftime("%H:%M:%S")
                        })
                time.sleep(0.01)

        except Exception as e:
            logging.error(f"Socket Data Error: {e}")
            time.sleep(3)
        finally:
            if 'sock' in locals(): sock.close()

# ================= API & WEB FRONTEND =================

@app.route('/api/machines')
def get_machine_data():
    return jsonify(machine_data)

@app.route('/api/pn_config')
def get_pn_config():
    load_pn_config()   # reload ทุกครั้งเผื่อ config เปลี่ยน
    return jsonify(pn_config)

@app.route('/api/pn_config/debug')
def get_pn_config_debug():
    load_pn_config()
    return jsonify({'config_file': CONFIG_FILE, 'exists': os.path.exists(CONFIG_FILE), 'data': pn_config})

@app.route('/api/mgr_plc')
def get_mgr_plc():
    """คืน MGR PLC cache ทั้งหมด (2 วันล่าสุด)"""
    with mgr_plc_lock:
        return jsonify(mgr_plc_cache)

@app.route('/api/mgr_plc/machine/<machine_id>')
def get_mgr_plc_machine(machine_id):
    """คืน records ของเครื่องเดียว (LINE_NUM ตรงกัน, รองรับ T01=T1)"""
    def _norm(s):
        # T01 -> T1, T09 -> T9, T10 ไม่เปลี่ยน
        if len(s) >= 3 and s[0].upper() == 'T' and s[1] == '0':
            return 'T' + s[2:]
        return s
    target = _norm(machine_id.upper())
    with mgr_plc_lock:
        filtered = [r for r in mgr_plc_cache if _norm((r.get('LINE_NUM') or '').upper()) == target]
    filtered.sort(key=lambda r: r.get('START_TIME') or '', reverse=True)
    return jsonify(filtered)

# ================= NEW V16 API ROUTES =================

@app.route('/api/colors', methods=['GET'])
def api_colors_get():
    return jsonify(db_get_colors())

@app.route('/api/colors', methods=['POST'])
def api_colors_set():
    data = request.get_json(force=True)
    part_no = data.get('part_no', '').strip()
    color   = data.get('color', '').strip()
    if not part_no or not color:
        return jsonify({'error': 'missing part_no or color'}), 400
    db_set_color(part_no, color)
    return jsonify({'ok': True, 'part_no': part_no, 'color': color})

@app.route('/api/layout', methods=['GET'])
def api_layout_get():
    return jsonify(db_get_layout())

@app.route('/api/layout', methods=['POST'])
def api_layout_save():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'expected object'}), 400
    db_save_layout(data)
    return jsonify({'ok': True, 'saved': len(data)})

@app.route('/api/emp_photo/<employee_id>')
def api_emp_photo(employee_id):
    path = get_photo_path(employee_id)
    if path and os.path.isfile(path):
        return send_file(path)
    return '', 404

@app.route('/api/emp_info/<employee_id>')
def api_emp_info(employee_id):
    """ดึงชื่อพนักงานจาก employee DB (WIP_Rate_MC DB)"""
    try:
        emp_db = os.path.join(DB_DIR, "employee.db")
        conn = sqlite3.connect(emp_db, timeout=5)
        row = conn.execute(
            "SELECT name FROM employees WHERE employee_id=? LIMIT 1",
            (str(employee_id),)
        ).fetchone()
        conn.close()
        if row:
            return jsonify({'employee_id': employee_id, 'name': row[0]})
    except Exception:
        pass
    return jsonify({'employee_id': employee_id, 'name': None})


@app.route('/')
def index():
    html_template = """
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=1400">
        <title>A30 Machine Status Dashboard</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;700;900&display=swap');
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { font-family: 'Roboto', sans-serif; background: #f0f4f8; color: #1a202c; overflow-x: hidden; }

            /* ===== HEADER ===== */
            .main-header {
                text-align: center; color: #1565c0;
                text-transform: uppercase; font-weight: 900;
                letter-spacing: 2px; font-size: 26px;
                padding: 16px 0 14px 0;
            }

            /* ===== SECTION LABEL ===== */
            .sec-label {
                font-size: 12px; font-weight: 700; letter-spacing: 2px;
                text-transform: uppercase; color: #546e7a;
                display: flex; align-items: center; gap: 8px;
                margin: 0 0 10px 0;
            }
            .sec-label::after { content: ''; flex: 1; height: 1px; background: linear-gradient(to right,#b0bec5,transparent); }

            /* ===== SECTION WRAPPER ===== */
            .section { padding: 0 15px 28px 15px; }
            .section + .section { border-top: 1px solid #cfd8dc; padding-top: 22px; }

            /* ===== LEGEND ===== */
            .legend-bar { display: flex; gap: 14px; justify-content: flex-end; align-items: center; margin-bottom: 10px; font-size: 11px; color: #546e7a; flex-wrap: wrap; }
            .legend-chip { display: flex; align-items: center; gap: 5px; }
            .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
            .dot-run { background: #1aff6e; } .dot-stop { background: #ffd23f; } .dot-alarm { background: #ff4d4d; }

            /* ===== SECTION 1: PART SUMMARY ===== */
            .summary-wrapper { overflow-x: auto; }
            .summary-table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 480px; }
            .summary-table thead tr { background: #e3f2fd; border-bottom: 2px solid #90caf9; }
            .summary-table th { padding: 5px 10px; text-align: center; font-weight: 700; font-size: 11px; text-transform: uppercase; color: #1565c0; letter-spacing: 1px; }
            .summary-table th:first-child { text-align: left; }
            .summary-table tbody tr { border-bottom: 1px solid #e0e0e0; background: #ffffff; }
            .summary-table tbody tr:hover { background: #e8f4fd; }
            .summary-table td { padding: 4px 10px; text-align: center; vertical-align: middle; }
            .summary-table td:first-child { text-align: left; }
            .badge-cell { font-size: 15px; font-weight: 900; }
            .badge-run { color: #00a83b; } .badge-stop { color: #e08000; } .badge-alarm { color: #d32f2f; }
            .badge-total { color: #37474f; font-size: 13px; }
            .summary-table tfoot tr { background: #e3f2fd; border-top: 2px solid #90caf9; }
            .summary-table tfoot td { padding: 5px 10px; font-weight: 700; color: #546e7a; font-size: 12px; text-align: center; }
            .summary-table tfoot td:first-child { text-align: left; }

            .part-color-cell { display: flex; align-items: center; gap: 6px; }
            .part-color-chip { width: 26px; height: 26px; border-radius: 5px; border: 2px solid rgba(0,0,0,0.25); flex-shrink: 0; cursor: pointer; transition: transform 0.1s, box-shadow 0.1s; }
            .part-color-chip:hover { transform: scale(1.2); box-shadow: 0 2px 8px rgba(0,0,0,0.25); }

            /* ===== SECTION 2: MAP ===== */
            .card {
                border-radius: 8px; padding: 8px;
                border-top: 5px solid #607d8b;
                border-left: 1px solid rgba(0,0,0,0.10);
                border-right: 1px solid rgba(0,0,0,0.10);
                border-bottom: 1px solid rgba(0,0,0,0.10);
                display: flex; flex-direction: column; justify-content: space-between;
                box-shadow: 0 2px 8px rgba(0,0,0,0.15); overflow: visible;
                position: relative;
            }
            .card-offline { background: #e8ecef !important; border-top-color: #b0bec5; opacity: 0.45; min-height: var(--offline-min, 10px); }
            .card-offline.hidden-row { min-height: var(--offline-min, 10px); padding: 0 !important; overflow: hidden; }
            .status-run   { border-top-color: #00c853 !important; }
            .status-stop  { border-top-color: #ff9800 !important; }
            .status-error { border-top-color: #f44336 !important; }
            .top-row { display: flex; justify-content: space-between; align-items: baseline; }
            .machine-name { margin: 0; font-size: 18px; color: #1a202c; font-weight: 900; }
            .run-rate { font-size: 16px; font-weight: 900; }
            .part-no { font-size: 12px; color: #5d4037; font-weight: 700; margin: 4px 0; word-break: break-all; }
            .user-info { font-size: 9px; color: #546e7a; }
            .color-run { color: #00a83b; font-weight: 900; } .color-stop { color: #e08000; font-weight: 900; }
            .color-error { color: #d32f2f; font-weight: 900; } .color-offline { color: #78909c; }
            .spindle-container { display: flex; gap: 3px; justify-content: center; margin-top: 4px; background: rgba(0,0,0,0.06); padding: 3px; border-radius: 4px; }
            .spindle { width: 8px; height: 8px; border-radius: 50%; border: 1px solid rgba(0,0,0,0.2); background: rgba(0,0,0,0.10); }
            .sp-on { background: #00b84a; border-color: #00b84a; }
            .card-dimmed { opacity: 0.12; filter: grayscale(60%); transition: opacity 0.3s, filter 0.3s; pointer-events: none; }
            .card { transition: opacity 0.3s, filter 0.3s; }

            /* ===== MARCHING ANTS + BG PULSE ===== */
            .card-marching { position: relative; }

            /* background pulse: สีเดิม → fade → สีเดิม */
            @keyframes bg-pulse {
                0%,100% { filter: brightness(1);   }
                50%      { filter: brightness(1.55) saturate(0.6); }
            }
            .card-marching { animation: bg-pulse 1.4s ease-in-out infinite; }

            /* SVG marching-ants stroke animation */
            @keyframes march-offset {
                from { stroke-dashoffset: 0; }
                to   { stroke-dashoffset: -19; }
            }

            /* ===== SECTION 3: LIST TABLE ===== */
            .list-controls { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
            .search-box {
                background: #ffffff; border: 1px solid #90caf9; border-radius: 6px;
                color: #1a202c; padding: 7px 12px; font-size: 13px;
                font-family: 'Roboto', sans-serif; outline: none; flex: 1; min-width: 160px;
            }
            .search-box:focus { border-color: #1565c0; }
            .filter-btn {
                background: #ffffff; border: 1px solid #90caf9; border-radius: 6px;
                color: #546e7a; padding: 7px 14px; font-size: 12px; font-weight: 700;
                cursor: pointer; font-family: 'Roboto', sans-serif; transition: background 0.15s, color 0.15s;
            }
            .filter-btn:hover { background: #e3f2fd; color: #1a202c; }
            .filter-btn.f-run.active   { border-color: #00a83b; color: #00a83b; background: #e8f5e9; }
            .filter-btn.f-stop.active  { border-color: #e08000; color: #e08000; background: #fff8e1; }
            .filter-btn.f-alarm.active { border-color: #d32f2f; color: #d32f2f; background: #ffebee; }
            .list-wrapper { overflow-x: auto; }
            .list-table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 640px; }
            .list-table thead tr { background: #e3f2fd; border-bottom: 2px solid #90caf9; }
            .list-table th { padding: 10px 12px; text-align: left; font-weight: 700; font-size: 11px; text-transform: uppercase; color: #1565c0; letter-spacing: 1px; cursor: pointer; user-select: none; white-space: nowrap; }
            .list-table th:hover { color: #0d47a1; }
            .list-table th.sort-asc::after  { content: ' ▲'; font-size: 9px; }
            .list-table th.sort-desc::after { content: ' ▼'; font-size: 9px; }
            .list-table tbody tr { border-bottom: 1px solid #e0e0e0; background: #ffffff; transition: background 0.1s; }
            .list-table tbody tr:hover { background: #e8f4fd; }
            .list-table td { padding: 9px 12px; vertical-align: middle; }
            .pill { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; white-space: nowrap; }
            .pill-run   { background: rgba(0,168,59,0.12);  color: #00a83b; border: 1px solid #00a83b; }
            .pill-stop  { background: rgba(224,128,0,0.12); color: #e08000; border: 1px solid #e08000; }
            .pill-alarm { background: rgba(211,47,47,0.12); color: #d32f2f; border: 1px solid #d32f2f; }
            .pill-off   { background: rgba(120,144,156,0.1); color: #78909c; border: 1px solid #b0bec5; }
            .sp-mini { display: inline-flex; gap: 2px; vertical-align: middle; }
            .sp-dot { width: 7px; height: 7px; border-radius: 50%; background: #e0e0e0; border: 1px solid #bdbdbd; display: inline-block; }
            .sp-dot.on { background: #00b84a; border-color: #00b84a; }
            .rate-bar-wrap { display: flex; align-items: center; gap: 6px; min-width: 90px; }
            .rate-bar { flex: 1; height: 6px; background: #e0e0e0; border-radius: 3px; overflow: hidden; }
            .rate-fill { height: 100%; border-radius: 3px; }
            .rate-text { font-size: 12px; font-weight: 700; min-width: 38px; text-align: right; color: #37474f; }
            .color-swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: middle; margin-right: 5px; }

            /* ===== FIT SCREEN MODE ===== */
            body.fit-screen { overflow: hidden; }
            body.fit-screen .main-header { display: none !important; }
            body.fit-screen .section:not(#s2-section) { display: none !important; }
            body.fit-screen #s2-section { padding: 0; display: flex; flex-direction: row; height: 100vh; }
            body.fit-screen .sec-label { display: none; }

            /* sidebar */
            #fit-sidebar {
                display: none;
                flex-direction: column;
                gap: 8px;
                width: 160px;
                min-width: 160px;
                background: #e3f2fd;
                border-right: 1px solid #90caf9;
                padding: 10px 8px;
                overflow-y: auto;
                z-index: 10;
            }
            body.fit-screen #fit-sidebar { display: flex; }
            body.fit-screen .main-controls { display: none; }  /* ซ่อน controls บน */
            body.fit-screen #grid-scale-wrap { flex: 1; overflow: hidden; }

            #fit-sidebar .sb-label {
                font-size: 9px; font-weight: 700; letter-spacing: 1.5px;
                text-transform: uppercase; color: #546e7a;
                margin-top: 8px; padding-bottom: 3px;
                border-bottom: 1px solid #90caf9;
            }
            #fit-sidebar .sb-btn {
                background: #ffffff; border: 1px solid #90caf9; border-radius: 6px;
                color: #546e7a; padding: 6px 8px; font-size: 11px; font-weight: 700;
                cursor: pointer; font-family: 'Roboto', sans-serif;
                transition: background 0.15s, color 0.15s; text-align: left; width: 100%;
            }
            #fit-sidebar .sb-btn:hover { background: #e3f2fd; color: #1a202c; }
            #fit-sidebar .sb-btn.f-run.active   { border-color: #00a83b; color: #00a83b; }
            #fit-sidebar .sb-btn.f-stop.active  { border-color: #e08000; color: #e08000; }
            #fit-sidebar .sb-btn.f-alarm.active { border-color: #d32f2f; color: #d32f2f; }
            #fit-sidebar .sb-radio-group { display: flex; flex-direction: column; gap: 5px; }
            #fit-sidebar label { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #1a202c; cursor: pointer; }
            #fit-sidebar .sb-select {
                background: #ffffff; border: 1px solid #90caf9; border-radius: 5px;
                color: #1a202c; padding: 4px 6px; font-size: 11px; width: 100%; cursor: pointer;
            }
            #fit-sidebar .sb-range { width: 100%; accent-color: #e08000; cursor: pointer; }
            #fit-sidebar .sb-scale-row { display: flex; align-items: center; gap: 4px; }
            #fit-sidebar .sb-scale-val { font-size: 11px; color: #e08000; font-weight: 700; min-width: 32px; }
            #fit-sidebar .sb-auto-btn {
                font-size: 10px; padding: 2px 6px; border: 1px solid #e08000;
                border-radius: 4px; background: transparent; color: #e08000; cursor: pointer;
            }
            #fit-sidebar .sb-blink-row { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
            #fit-sidebar .sb-blink-input {
                width: 52px; padding: 3px 4px; font-size: 11px;
                background: #ffffff; border: 1px solid #e08000; border-radius: 4px;
                color: #e08000; font-weight: 700; text-align: center; outline: none;
            }
            #grid-scaler { transform-origin: top left; transition: transform 0.15s; }

            /* ===== RESPONSIVE ===== */
            /* ===== FIXED LAYOUT — ไม่ responsive ===== */
            .main-header { font-size: 26px; }
            .grid-container {
                display: grid;
                grid-template-columns: repeat(var(--grid-cols, 9), minmax(90px, 1fr));
                grid-template-rows: repeat(var(--grid-rows, 16), minmax(var(--row-min, 10px), auto));
                gap: 8px; margin: 0 auto;
            }
            .card { grid-column: var(--map-c); grid-row: var(--map-r); }

            /* ===== MGR MODAL ===== */
            #mgr-modal {
                display: none; position: fixed; inset: 0;
                background: rgba(0,0,0,0.55); z-index: 9999;
                align-items: center; justify-content: center;
                padding: 16px;
            }
            .mgr-modal-box {
                background: #fff; border-radius: 10px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
                max-width: 980px; width: 100%;
                max-height: 92vh; display: flex; flex-direction: column;
                overflow: hidden;
            }
            .mgr-modal-header {
                display: flex; align-items: center; justify-content: space-between;
                padding: 14px 18px;
                background: linear-gradient(135deg,#1565c0,#1976d2);
                color: #fff; flex-shrink: 0;
            }
            .mgr-modal-header h3 { font-size: 15px; font-weight: 900; margin: 0; }
            .mgr-modal-close {
                background: rgba(255,255,255,0.2); border: none; color: #fff;
                border-radius: 50%; width: 28px; height: 28px;
                font-size: 16px; cursor: pointer; display: flex;
                align-items: center; justify-content: center;
                transition: background 0.15s;
            }
            .mgr-modal-close:hover { background: rgba(255,255,255,0.35); }
            .mgr-modal-body { padding: 16px; overflow-y: auto; flex: 1; }
            /* Operator badge in modal */
            .op-badge {
                display: flex; align-items: center; gap: 14px;
                background: linear-gradient(135deg,#e3f2fd,#f8fbff);
                border: 1px solid #90caf9; border-radius: 10px;
                padding: 12px 16px; margin-bottom: 14px;
            }
            .op-photo {
                width: 72px; height: 72px; border-radius: 50%;
                object-fit: cover; border: 3px solid #1565c0;
                flex-shrink: 0; background: #e0e0e0;
            }
            .op-photo-placeholder {
                width: 72px; height: 72px; border-radius: 50%;
                background: #cfd8dc; display: flex; align-items: center;
                justify-content: center; font-size: 30px; flex-shrink: 0;
                border: 3px solid #b0bec5;
            }
            .op-info { flex: 1; }
            .op-name { font-size: 18px; font-weight: 900; color: #1a202c; }
            .op-id   { font-size: 12px; color: #78909c; margin-top: 2px; }
            .op-user-row { font-size: 13px; color: #1565c0; font-weight: 700; margin-top: 6px; }
            /* ===== LAYOUT EDITOR MODAL ===== */
            #le-modal {
                display: none; position: fixed; inset: 0;
                background: rgba(0,0,0,0.65); z-index: 9500;
                align-items: center; justify-content: center; padding: 12px;
            }
            #le-modal.open { display: flex; }
            .le-box {
                background: #f4f6f9; border-radius: 12px;
                box-shadow: 0 12px 48px rgba(0,0,0,0.4);
                width: 96vw; max-width: 1400px; height: 90vh;
                display: flex; flex-direction: column; overflow: hidden;
            }
            .le-header {
                background: linear-gradient(135deg,#1565c0,#1976d2);
                color: #fff; padding: 12px 18px;
                display: flex; align-items: center; gap: 12px; flex-shrink: 0;
            }
            .le-header h3 { margin: 0; font-size: 15px; font-weight: 900; flex: 1; }
            .le-header-btns { display: flex; gap: 8px; }
            .le-btn { border: none; border-radius: 6px; padding: 7px 16px; font-size: 12px; font-weight: 700; cursor: pointer; }
            .le-btn-save   { background: #00c853; color: #fff; }
            .le-btn-cancel { background: rgba(255,255,255,0.22); color: #fff; }
            .le-body { display: flex; flex: 1; overflow: hidden; gap: 0; }
            /* Left panel: tools */
            .le-panel {
                width: 200px; flex-shrink: 0; background: #fff;
                border-right: 1px solid #cfd8dc;
                padding: 12px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;
            }
            .le-panel-title { font-size: 11px; font-weight: 900; color: #546e7a; letter-spacing: 1px; text-transform: uppercase; }
            .le-dim-row { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #37474f; }
            .le-dim-row input { width: 52px; padding: 4px 6px; border: 1px solid #b0bec5; border-radius: 5px; font-size: 13px; font-weight: 700; text-align: center; }
            .le-dim-btn { border: none; border-radius: 5px; padding: 4px 10px; font-size: 18px; font-weight: 900; cursor: pointer; background: #e3f2fd; color: #1565c0; line-height: 1; }
            .le-dim-btn:hover { background: #1565c0; color: #fff; }
            .le-sep { border: none; border-top: 1px solid #e0e0e0; margin: 4px 0; }
            /* Machine pool (unplaced) */
            .le-pool { display: flex; flex-direction: column; gap: 5px; }
            .le-pool-item {
                background: #fff3e0; border: 1.5px solid #ffb74d; border-radius: 6px;
                padding: 6px 10px; font-size: 12px; font-weight: 700; color: #e65100;
                cursor: grab; user-select: none; text-align: center;
            }
            .le-pool-item:hover { background: #ffe0b2; }
            .le-pool-item.dragging-pool { opacity: 0.4; }
            /* New machine input */
            .le-new-row { display: flex; gap: 5px; }
            .le-new-input { flex: 1; padding: 5px 8px; border: 1px solid #b0bec5; border-radius: 5px; font-size: 12px; }
            .le-new-btn { border: none; border-radius: 5px; padding: 5px 10px; background: #1565c0; color: #fff; font-size: 13px; font-weight: 900; cursor: pointer; }
            /* Right: grid canvas */
            .le-canvas-wrap { flex: 1; overflow: auto; padding: 16px; }
            .le-grid {
                display: inline-grid; gap: 6px;
                background: #e8edf2; padding: 8px; border-radius: 8px;
                min-width: max-content;
            }
            .le-cell.has-machine {
                border: 2px solid transparent; background: transparent;
            }
            .le-cell.drag-over-cell {
                border-color: #ff9800; background: rgba(255,152,0,0.12);
            }
            .le-chip {
                width: 100%; height: 100%; border-radius: 5px;
                display: flex; flex-direction: column; align-items: center; justify-content: center;
                cursor: grab; font-weight: 900; font-size: 13px; color: #fff;
                text-shadow: 0 1px 3px rgba(0,0,0,0.4); user-select: none;
                position: relative; border: 2px solid rgba(255,255,255,0.3);
            }
            .le-chip:active { cursor: grabbing; }
            .le-chip-name { font-size: 13px; font-weight: 900; }
            .le-chip-remove {
                position: absolute; top: 2px; right: 3px; background: rgba(0,0,0,0.3);
                border: none; color: #fff; border-radius: 50%; width: 16px; height: 16px;
                font-size: 10px; cursor: pointer; display: flex; align-items: center; justify-content: center;
                line-height: 1;
            }
            .le-chip-remove:hover { background: #d32f2f; }
            /* Headers fill their grid cell naturally — no fixed width/height */
            .le-col-header {
                font-size: 10px; font-weight: 700; color: #90a4ae;
                display: flex; align-items: center; justify-content: center;
                cursor: pointer; border-radius: 4px;
                transition: background 0.12s, color 0.12s;
                user-select: none; overflow: hidden;
            }
            .le-row-header {
                font-size: 10px; font-weight: 700; color: #90a4ae;
                display: flex; align-items: center; justify-content: center;
                cursor: pointer; border-radius: 4px;
                transition: background 0.12s, color 0.12s;
                user-select: none; overflow: hidden;
                writing-mode: horizontal-tb;
            }
            .le-col-header:hover, .le-row-header:hover {
                background: #e3f2fd; color: #1565c0;
            }
            .le-col-header.is-spacer {
                background: #fff8e1; color: #e65100;
                border: 1.5px dashed #ffb300; border-radius: 4px;
            }
            .le-row-header.is-spacer {
                background: #fff8e1; color: #e65100;
                border: 1.5px dashed #ffb300; border-radius: 4px;
            }
            /* le-cell fills its grid cell — no fixed width/height */
            .le-cell {
                border-radius: 6px;
                border: 2px dashed #b0bec5; background: #f0f4f8;
                display: flex; align-items: center; justify-content: center;
                font-size: 10px; color: #b0bec5; position: relative;
                transition: background 0.1s, border-color 0.1s;
                overflow: hidden; min-width: 0; min-height: 0;
            }
            /* ===== PW MODAL ===== */
            #pw-modal {
                display: none; position: fixed; inset: 0;
                background: rgba(0,0,0,0.5); z-index: 10000;
                align-items: center; justify-content: center;
            }
            .pw-modal-box {
                background: #fff; border-radius: 12px; padding: 28px 32px;
                min-width: 280px; text-align: center;
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            }
            .pw-modal-box h3 { margin: 0 0 14px; font-size: 15px; color: #1565c0; }
            .pw-modal-box input {
                width: 100%; padding: 9px 12px; border: 1px solid #90caf9;
                border-radius: 6px; font-size: 16px; text-align: center;
                letter-spacing: 4px; margin-bottom: 14px;
            }
            .pw-modal-btns { display: flex; gap: 10px; justify-content: center; }
            .pw-ok     { background: #1565c0; color: #fff; border: none; border-radius: 6px; padding: 8px 22px; font-weight: 700; cursor: pointer; }
            .pw-cancel { background: #e0e0e0; color: #333; border: none; border-radius: 6px; padding: 8px 16px; font-weight: 700; cursor: pointer; }
            .pw-err    { color: #d32f2f; font-size: 12px; min-height: 16px; margin-bottom: 8px; }
        </style>
    </head>
    <body>
        <h1 class="main-header">&#9881; A30 Machine Status Dashboard V16</h1>

        <!-- ===== SECTION 1 ===== -->
        <div class="section">
            <div class="sec-label">&#128202; Section 1 — Part Number Summary</div>
            <div class="legend-bar">
                <div class="legend-chip"><div class="legend-dot dot-run"></div> Run</div>
                <div class="legend-chip"><div class="legend-dot dot-stop"></div> Stop</div>
                <div class="legend-chip"><div class="legend-dot dot-alarm"></div> Alarm</div>
            </div>
            <div class="summary-wrapper">
                <table class="summary-table">
                    <thead><tr>
                        <th style="text-align:left;">สี</th>
                        <th style="text-align:left;">Part Number</th>
                        <th>&#128994; Run</th><th>&#128993; Stop</th><th>&#128308; Alarm</th><th>Total</th>
                        <th>Stk</th><th>Cycle (min)</th>
                        <th>&#128260; Spindle open<br><span style="font-size:9px;font-weight:400;letter-spacing:0;">(Run+Alarm)</span></th>
                        <th>&#128203; pnl<br><span style="font-size:9px;font-weight:400;letter-spacing:0;">(spindle×stk)</span></th>
                    </tr></thead>
                    <tbody id="summary-body">
                        <tr><td colspan="10" style="text-align:center;color:#90a4ae;padding:20px;">กำลังโหลด...</td></tr>
                    </tbody>
                    <tfoot><tr>
                        <td></td>
                        <td>รวมทั้งหมด</td>
                        <td id="foot-run" class="badge-run">—</td>
                        <td id="foot-stop" class="badge-stop">—</td>
                        <td id="foot-alarm" class="badge-alarm">—</td>
                        <td id="foot-total" class="badge-total">—</td>
                        <td>—</td><td>—</td>
                        <td id="foot-spindle" style="font-weight:700;color:#5c6bc0;">—</td>
                        <td id="foot-sheets"  style="font-weight:700;color:#00796b;">—</td>
                    </tr></tfoot>
                </table>
            </div>
        </div>

        <!-- ===== SECTION 2 ===== -->
        <div class="section" id="s2-section">
            <div class="sec-label">&#128205; Section 2 — ตำแหน่งเครื่อง Realtime</div>
            <div class="list-controls main-controls" style="margin-bottom:14px;flex-wrap:wrap;">
                <button id="fit-btn" class="filter-btn" onclick="toggleFitScreen()"
                    style="color:#e08000;border-color:#e08000;font-size:13px;padding:7px 16px;font-weight:900;letter-spacing:1px;">
                    &#9635; Fit Screen
                </button>
                <button id="layout-edit-btn" class="filter-btn" onclick="toggleLayoutEdit()"
                    style="color:#1565c0;border-color:#1565c0;font-size:13px;padding:7px 16px;font-weight:900;letter-spacing:1px;">
                    &#9999;&#65039; Edit Layout
                </button>
                <span id="scale-slider-wrap" style="display:none;align-items:center;gap:6px;font-size:12px;color:#e08000;font-weight:700;">
                    &#128269; Scale:
                    <input type="range" id="scale-slider" min="20" max="150" value="100" step="1"
                        oninput="onSliderChange(this.value)"
                        style="width:110px;accent-color:#e08000;cursor:pointer;">
                    <span id="scale-label" style="min-width:36px;"></span>
                    <button onclick="manualScale=null;applyScale();"
                        style="font-size:11px;padding:2px 8px;border:1px solid #e08000;border-radius:5px;background:transparent;color:#e08000;cursor:pointer;">Auto</button>
                </span>
                <button class="filter-btn f-run"   id="map-fb-run"   onclick="toggleMapFilter('run')">&#9646; Run</button>
                <button class="filter-btn f-stop"  id="map-fb-stop"  onclick="toggleMapFilter('stop')">&#9646; Stop</button>
                <button class="filter-btn f-alarm" id="map-fb-alarm" onclick="toggleMapFilter('alarm')">&#9646; Alarm</button>
                <select class="search-box" id="map-part-select" onchange="applyMapFilter()" style="flex:0 1 220px;cursor:pointer;">
                    <option value="">&#128196; ทุก Part No.</option>
                </select>
                <button class="filter-btn" onclick="resetMapFilter()" style="color:#1565c0;border-color:#1565c0;">&#10006; Reset</button>
                <span style="display:flex;align-items:center;gap:10px;margin-left:8px;font-size:12px;color:#546e7a;font-weight:700;">
                    แสดงตัวเลข:
                    <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
                        <input type="radio" name="card-display" value="rate" checked onchange="setCardDisplay('rate')" style="accent-color:#1565c0;">
                        <span style="color:#1a202c;">Run Rate</span>
                    </label>
                    <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
                        <input type="radio" name="card-display" value="runtime" onchange="setCardDisplay('runtime')" style="accent-color:#1565c0;">
                        <span style="color:#1a202c;">Run Time <span style='font-size:10px;color:#90a4ae;'>(hh:mm:ss)</span></span>
                    </label>
                    <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
                        <input type="radio" name="card-display" value="remain" onchange="setCardDisplay('remain')" checked style="accent-color:#1565c0;">
                        <span style="color:#1a202c;">Remain Time <span style='font-size:10px;color:#90a4ae;'>(hh:mm:ss)</span></span>
                    </label>
                    <label style="display:flex;align-items:center;gap:4px;cursor:pointer;">
                        <input type="radio" name="card-display" value="eta" onchange="setCardDisplay('eta')" style="accent-color:#1565c0;">
                        <span style="color:#1a202c;">Finish Time <span style='font-size:10px;color:#90a4ae;'>(hh:mm)</span></span>
                    </label>
                    <span style="display:flex;align-items:center;gap:6px;margin-left:4px;">
                        <span style="color:#e08000;font-size:11px;">&#9650; กระพริบถ้าเหลือ &lt;</span>
                        <input type="number" id="blink-threshold" value="30" min="1" max="9999"
                            oninput="applyBlinkClass()"
                            style="width:56px;padding:3px 6px;font-size:12px;background:#ffffff;border:1px solid #e08000;border-radius:5px;color:#e08000;font-weight:700;outline:none;text-align:center;">
                        <span style="color:#e08000;font-size:11px;">นาที</span>
                    </span>
                </span>
            </div>
            <!-- FIT SIDEBAR -->
            <div id="fit-sidebar">
                <div style="font-size:11px;font-weight:900;color:#e08000;letter-spacing:1px;">&#9635; FIT VIEW</div>

                <div class="sb-label">Scale</div>
                <div class="sb-scale-row">
                    <input type="range" class="sb-range" id="scale-slider-sb" min="20" max="150" value="100" step="1"
                        oninput="onSliderChange(this.value)">
                    <span class="sb-scale-val" id="scale-label-sb">Auto</span>
                </div>
                <button class="sb-auto-btn" onclick="manualScale=null;applyScale();">&#8635; Auto</button>

                <div class="sb-label">แสดงตัวเลข</div>
                <div class="sb-radio-group">
                    <label><input type="radio" name="card-display" value="rate" onchange="setCardDisplay('rate')" style="accent-color:#1565c0;"> Run Rate</label>
                    <label><input type="radio" name="card-display" value="runtime" onchange="setCardDisplay('runtime')" style="accent-color:#1565c0;"> Run Time</label>
                    <label><input type="radio" name="card-display" value="remain" onchange="setCardDisplay('remain')" checked style="accent-color:#1565c0;"> Remain</label>
                    <label><input type="radio" name="card-display" value="eta" onchange="setCardDisplay('eta')" style="accent-color:#1565c0;"> Finish Time</label>
                </div>

                <div class="sb-label">กระพริบถ้าเหลือ &lt;</div>
                <div class="sb-blink-row">
                    <input type="number" class="sb-blink-input" id="blink-sb" value="30" min="1" max="9999"
                        oninput="document.getElementById('blink-threshold').value=this.value;applyBlinkClass();">
                    <span style="font-size:10px;color:#546e7a;">นาที</span>
                </div>

                <div class="sb-label">Filter Status</div>
                <button class="sb-btn f-run"   id="sb-fb-run"   onclick="toggleMapFilter('run');syncSbFilter()">&#9646; Run</button>
                <button class="sb-btn f-stop"  id="sb-fb-stop"  onclick="toggleMapFilter('stop');syncSbFilter()">&#9646; Stop</button>
                <button class="sb-btn f-alarm" id="sb-fb-alarm" onclick="toggleMapFilter('alarm');syncSbFilter()">&#9646; Alarm</button>

                <div class="sb-label">Filter Part No.</div>
                <select class="sb-select" id="map-part-select-sb" onchange="document.getElementById('map-part-select').value=this.value;applyMapFilter();">
                    <option value="">ทุก Part No.</option>
                </select>
                <button class="sb-btn" onclick="resetMapFilter();syncSbFilter();" style="color:#1565c0;border-color:#1565c0;">&#10006; Reset</button>

                <div class="sb-label">ระยะห่างแถวว่าง</div>
                <div class="sb-scale-row">
                    <input type="range" class="sb-range" id="empty-row-slider" min="0" max="90" value="10" step="5"
                        oninput="setEmptyRowHeight(this.value)" style="accent-color:#546e7a;">
                    <span class="sb-scale-val" id="empty-row-label" style="color:#546e7a;">10px</span>
                </div>

                <div style="margin-top:auto;padding-top:10px;">
                    <button class="sb-btn" onclick="toggleFitScreen()" style="color:#e08000;border-color:#e08000;width:100%;text-align:center;">&#10005; Exit Fit</button>
                </div>
            </div>
            <div id="grid-scale-wrap" style="overflow:hidden;">
                <div id="grid-scaler">
                    <div class="grid-container" id="dashboard"></div>
                </div>
            </div>
        </div>

        <!-- ===== SECTION 3 ===== -->
        <div class="section">
            <div class="sec-label">&#128203; Section 3 — รายการเครื่องจักร</div>
            <div class="list-controls">
                <input class="search-box" type="text" id="list-search"
                    placeholder="&#128269; ค้นหา เครื่อง / Part No. / User..."
                    oninput="renderList()">
                <button class="filter-btn f-run"   id="fb-run"   onclick="toggleFilter('run')">&#9646; Run</button>
                <button class="filter-btn f-stop"  id="fb-stop"  onclick="toggleFilter('stop')">&#9646; Stop</button>
                <button class="filter-btn f-alarm" id="fb-alarm" onclick="toggleFilter('alarm')">&#9646; Alarm</button>
            </div>
            <div class="list-wrapper">
                <table class="list-table">
                    <thead><tr>
                        <th onclick="sortList('name')"   id="th-name">เครื่อง</th>
                        <th onclick="sortList('status')" id="th-status">สถานะ</th>
                        <th onclick="sortList('part')"   id="th-part">Part No.</th>
                        <th onclick="sortList('user')"   id="th-user">User</th>
                        <th onclick="sortList('rate')"   id="th-rate">Run Rate</th>
                        <th>Spindle</th>
                        <th onclick="sortList('sim_time')" id="th-sim">Run Time <span style='font-size:9px;color:#90a4ae;font-weight:400;'>(hh:mm:ss)</span></th>
                        <th onclick="sortList('remaining')" id="th-remaining">Remaining <span style='font-size:9px;color:#90a4ae;font-weight:400;'>(hh:mm:ss)</span></th>
                        <th onclick="sortList('eta')" id="th-eta">เสร็จเวลา <span style='font-size:9px;color:#90a4ae;font-weight:400;'>(hh:mm)</span></th>
                        <th onclick="sortList('update')" id="th-update">อัพเดท</th>
                    </tr></thead>
                    <tbody id="list-body">
                        <tr><td colspan="10" style="text-align:center;color:#90a4ae;padding:24px;">กำลังโหลด...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>


        <!-- ===== MGR MODAL ===== -->
        <div id="mgr-modal">
            <div class="mgr-modal-box">
                <div class="mgr-modal-header">
                    <h3 id="mgr-modal-title">📋 Machine History</h3>
                    <button class="mgr-modal-close" onclick="closeMgrModal()">✕</button>
                </div>
                <div class="mgr-modal-body" id="mgr-modal-body"></div>
            </div>
        </div>

        <!-- ===== LAYOUT EDITOR MODAL ===== -->
        <div id="le-modal">
          <div class="le-box">
            <div class="le-header">
              <h3>&#9999;&#65039; Visual Layout Editor — ลากเครื่องไปยังตำแหน่งที่ต้องการ</h3>
              <div class="le-header-btns">
                <button class="le-btn le-btn-save"   onclick="leSave()">&#128190; บันทึก</button>
                <button class="le-btn le-btn-cancel" onclick="leClose()">&#10005; ปิด</button>
              </div>
            </div>
            <div class="le-body">
              <!-- Left panel -->
              <div class="le-panel">
                <div class="le-panel-title">&#128203; ขนาด Grid</div>
                <div class="le-dim-row">
                  <button class="le-dim-btn" onclick="leAdjust('c',-1)">&#8722;</button>
                  <span>Col</span>
                  <input type="number" id="le-cols" value="9" min="1" max="20" onchange="leRender()">
                  <button class="le-dim-btn" onclick="leAdjust('c',1)">&#43;</button>
                </div>
                <div class="le-dim-row">
                  <button class="le-dim-btn" onclick="leAdjust('r',-1)">&#8722;</button>
                  <span>Row</span>
                  <input type="number" id="le-rows" value="16" min="1" max="30" onchange="leRender()">
                  <button class="le-dim-btn" onclick="leAdjust('r',1)">&#43;</button>
                </div>
                <hr class="le-sep">
                <div class="le-panel-title">&#10010; เพิ่มเครื่องใหม่</div>
                <div class="le-new-row">
                  <input class="le-new-input" type="text" id="le-new-name" placeholder="ชื่อเครื่อง"
                    onkeydown="if(event.key==='Enter')leAddMachine()">
                  <button class="le-new-btn" onclick="leAddMachine()">&#43;</button>
                </div>
                <hr class="le-sep">
                <div class="le-panel-title">&#128230; เครื่องที่ยังไม่มีตำแหน่ง</div>
                <div class="le-pool" id="le-pool"></div>
              </div>
              <!-- Grid canvas -->
              <div class="le-canvas-wrap">
                <div class="le-grid" id="le-grid"></div>
              </div>
            </div>
          </div>
        </div>

        <!-- ===== PASSWORD MODAL ===== -->
        <div id="pw-modal" onclick="if(event.target===this)closePwModal()">
            <div class="pw-modal-box">
                <h3 id="pw-modal-title">&#128274; ใส่รหัสผ่าน</h3>
                <input type="password" id="pw-input" placeholder="รหัส" autocomplete="off" maxlength="20"
                    onkeydown="if(event.key==='Enter')confirmPw();if(event.key==='Escape')closePwModal();">
                <div class="pw-err" id="pw-err"></div>
                <div class="pw-modal-btns">
                    <button class="pw-ok"     onclick="confirmPw()">&#10003; ยืนยัน</button>
                    <button class="pw-cancel" onclick="closePwModal()">&#10005; ยกเลิก</button>
                </div>
            </div>
        </div>

        <script>
            /* ===== MAP LAYOUT ===== */
            // Default layout — overridden by DB on load
            var mapLayout = {
                'M1':{c:1,r:1},'D1':{c:2,r:1},'D2':{c:3,r:1},'T65':{c:4,r:1},'T66':{c:5,r:1},'T67':{c:6,r:1},'T68':{c:7,r:1},
                'T58':{c:1,r:2},'T59':{c:2,r:2},'T60':{c:3,r:2},'T61':{c:4,r:2},'T62':{c:5,r:2},'T63':{c:6,r:2},'T64':{c:7,r:2},
                'T51':{c:1,r:4},'T52':{c:2,r:4},'T53':{c:3,r:4},'T54':{c:4,r:4},'T55':{c:5,r:4},'T56':{c:6,r:4},'T57':{c:7,r:4},
                'T44':{c:1,r:5},'T45':{c:2,r:5},'T46':{c:3,r:5},'T47':{c:4,r:5},'T48':{c:5,r:5},'T49':{c:6,r:5},'T50':{c:7,r:5},
                'T37':{c:1,r:7},'T38':{c:2,r:7},'T39':{c:3,r:7},'T40':{c:4,r:7},'T41':{c:5,r:7},'T42':{c:6,r:7},'T43':{c:7,r:7},
                'T30':{c:1,r:8},'T31':{c:2,r:8},'T32':{c:3,r:8},'T33':{c:4,r:8},'T34':{c:5,r:8},'T35':{c:6,r:8},'T36':{c:7,r:8},
                'T23':{c:1,r:10},'T24':{c:2,r:10},'T25':{c:3,r:10},'T26':{c:4,r:10},'T27':{c:5,r:10},'T28':{c:6,r:10},'T29':{c:7,r:10},'T73':{c:9,r:10},
                'T16':{c:1,r:11},'T17':{c:2,r:11},'T18':{c:3,r:11},'T19':{c:4,r:11},'T20':{c:5,r:11},'T21':{c:6,r:11},'T22':{c:7,r:11},'T72':{c:9,r:11},
                'T09':{c:1,r:13},'T10':{c:2,r:13},'T11':{c:3,r:13},'T12':{c:4,r:13},'T13':{c:5,r:13},'T14':{c:6,r:13},'T15':{c:7,r:13},'T71':{c:9,r:13},
                'T05':{c:1,r:14},'T06':{c:2,r:14},'T07':{c:3,r:14},'T08':{c:4,r:14},'4':{c:5,r:14},'5':{c:6,r:14},'6':{c:7,r:14},'T70':{c:9,r:14},
                'T01':{c:1,r:16},'T02':{c:2,r:16},'T03':{c:3,r:16},'T04':{c:4,r:16},'1':{c:5,r:16},'2':{c:6,r:16},'3':{c:7,r:16},'T69':{c:9,r:16}
            };

            /* Load layout from DB on startup */
            (function loadLayoutFromDB() {
                fetch('/api/layout')
                    .then(function(r){ return r.json(); })
                    .then(function(dbLayout) {
                        if (!dbLayout || Object.keys(dbLayout).length === 0) return;
                        Object.keys(dbLayout).forEach(function(m) {
                            if (m === '__spacerRows') {
                                mapLayout.__spacerRows = (dbLayout[m].spacers || []).map(Number);
                            } else if (m === '__spacerCols') {
                                mapLayout.__spacerCols = (dbLayout[m].spacers || []).map(Number);
                            } else {
                                mapLayout[m] = dbLayout[m];
                            }
                        });
                        // Apply after dashboard is rendered (wait for first updateDashboard)
                        setTimeout(applySpacerToDashboard, 1200);
                    })
                    .catch(function(){});
            })();

            /* ===== PART COLOR SYSTEM (user-editable, persisted to DB) ===== */
            var partColorMap = {};   // key=partNo → hex color string (user override)
            var partColorsMap = {};  // key=partNo → {bg} auto-generated

            // Load colors from DB on startup
            (function loadColorsFromDB() {
                fetch('/api/colors')
                    .then(function(r){ return r.json(); })
                    .then(function(dbColors) {
                        Object.keys(dbColors).forEach(function(pn) {
                            partColorMap[pn] = dbColors[pn];
                        });
                    })
                    .catch(function(){});
            })();

            /* ===== PASSWORD UNLOCK for color change (per-machine, first click) ===== */
            var colorUnlocked = {};   // partNo → true if unlocked this session
            var _pendingColorPart = null;
            var _pwCallback = null;

            function openPwModal(cb, title) {
                _pwCallback = cb;
                document.getElementById('pw-input').value = '';
                document.getElementById('pw-err').textContent = '';
                var tEl = document.getElementById('pw-modal-title');
                if (tEl) tEl.textContent = title || '🔒 ใส่รหัสผ่าน';
                document.getElementById('pw-modal').style.display = 'flex';
                setTimeout(function(){ document.getElementById('pw-input').focus(); }, 50);
            }
            function closePwModal() {
                document.getElementById('pw-modal').style.display = 'none';
                _pwCallback = null;
            }
            function confirmPw() {
                var val = document.getElementById('pw-input').value;
                if (_pwCallback) {
                    var ok = _pwCallback(val);
                    if (!ok) {
                        document.getElementById('pw-err').textContent = '❌ รหัสไม่ถูกต้อง';
                    }
                }
            }

            function hslAutoColor(partNo) {
                var h1 = 2166136261;
                for (var i = 0; i < partNo.length; i++)
                    h1 = ((h1 ^ partNo.charCodeAt(i)) * 16777619) & 0xFFFFFFFF;
                var h2 = 2166136261;
                for (var i = partNo.length - 1; i >= 0; i--)
                    h2 = ((h2 ^ partNo.charCodeAt(i)) * 16777619) & 0xFFFFFFFF;
                var hue = Math.floor(((h1 * 0.6180339887) % 1.0) * 360);
                var sat = 55 + (h2 % 25);
                var lit = 42 + ((h2 / 256 | 0) % 14);  // 42-56% → mid-tone readable on light bg
                return 'hsl('+hue+','+sat+'%,'+lit+'%)';
            }

            function getPartBgColor(partNo) {
                if (!partNo || partNo.trim()==='' || partNo==='---' || partNo==='NO PROGRAM')
                    return {bg:'#cfd8dc'};
                // user override takes priority
                if (partColorMap[partNo]) return {bg: partColorMap[partNo]};
                if (!partColorsMap[partNo]) {
                    partColorsMap[partNo] = {bg: hslAutoColor(partNo)};
                }
                return partColorsMap[partNo];
            }

            function setPartColor(partNo, color) {
                partColorMap[partNo] = color;
                // cards in section 2
                document.querySelectorAll('#dashboard .card[data-part]').forEach(function(card) {
                    if (card.getAttribute('data-part') === partNo) card.style.background = color;
                });
                // chips in section 1
                document.querySelectorAll('.part-color-chip[data-part]').forEach(function(chip) {
                    if (chip.getAttribute('data-part') === partNo) chip.style.background = color;
                });
                // swatches in section 3
                document.querySelectorAll('.color-swatch[data-part]').forEach(function(sw) {
                    if (sw.getAttribute('data-part') === partNo) sw.style.background = color;
                });
                // Persist to DB
                fetch('/api/colors', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({part_no: partNo, color: color})
                }).catch(function(){});
            }

            function randomPartColor(el) {
                var partNo = el.getAttribute('data-part');
                if (!partNo) return;
                // First click = require password unlock
                if (!colorUnlocked[partNo]) {
                    openPwModal(function(val) {
                        if (val === '999') {
                            colorUnlocked[partNo] = true;
                            closePwModal();
                            _doRandomColor(el, partNo);
                            return true;
                        }
                        return false;
                    }, '🎨 ใส่รหัสเพื่อเปลี่ยนสี (999)');
                    return;
                }
                _doRandomColor(el, partNo);
            }

            function _doRandomColor(el, partNo) {
                var h = Math.floor(Math.random() * 360);
                var s = 50 + Math.floor(Math.random() * 30);
                var l = 38 + Math.floor(Math.random() * 18);
                var color = 'hsl(' + h + ',' + s + '%,' + l + '%)';
                setPartColor(partNo, color);
            }

            /* ===== STATUS ===== */
            function getStatusInfo(s) {
                s = parseInt(s);
                if (s===1||s===49) return {cardClass:'status-error',textClass:'color-error',type:'alarm',label:'ALARM'};
                if (s===2||s===50) return {cardClass:'status-stop', textClass:'color-stop', type:'stop', label:'STOP'};
                if (s===3||s===51) return {cardClass:'status-run',  textClass:'color-run',  type:'run',  label:'RUN'};
                return {cardClass:'',textClass:'color-offline',type:'offline',label:'OFFLINE'};
            }

            /* ===== LIST STATE ===== */
            var listData  = [];
            var sortCol   = 'status';
            var sortAsc   = true;
            var filterSet    = {};
            var mapFilterSet = {};

            function toggleMapFilter(type) {
                mapFilterSet[type] = !mapFilterSet[type];
                var btn = document.getElementById('map-fb-'+type);
                if (mapFilterSet[type]) btn.classList.add('active');
                else btn.classList.remove('active');
                applyMapFilter();
            }

            function applyMapFilter() {
                var af   = Object.keys(mapFilterSet).filter(function(k){ return mapFilterSet[k]; });
                var selEl = document.getElementById('map-part-select');
                var selPart = selEl ? selEl.value : '';
                var cards = document.querySelectorAll('#dashboard .card');
                cards.forEach(function(card) {
                    var t    = card.getAttribute('data-type');
                    var p    = card.getAttribute('data-part');
                    var passStatus = (af.length === 0) || (af.indexOf(t) >= 0);
                    var passPart   = (selPart === '') || (p === selPart);
                    if (passStatus && passPart) card.classList.remove('card-dimmed');
                    else card.classList.add('card-dimmed');
                });
            }

            function resetMapFilter() {
                mapFilterSet = {};
                ['run','stop','alarm'].forEach(function(t) {
                    var btn = document.getElementById('map-fb-'+t);
                    if (btn) btn.classList.remove('active');
                });
                var sel = document.getElementById('map-part-select');
                if (sel) sel.value = '';
                applyMapFilter();
            }

            function updatePartSelect(data) {
                var parts = {};
                Object.keys(data).forEach(function(k) {
                    var p = (data[k].part_no || '').trim();
                    if (p && p !== '---' && p !== 'NO PROGRAM') parts[p] = true;
                });
                var sel = document.getElementById('map-part-select');
                if (!sel) return;
                var cur = sel.value;
                var sorted = Object.keys(parts).sort();
                var html = '<option value="">&#128196; ทุก Part No.</option>';
                sorted.forEach(function(p) {
                    html += '<option value="'+p+'"'+(p===cur?' selected':'')+'>'+p+'</option>';
                });
                sel.innerHTML = html;
            }

            function toggleFilter(type) {
                filterSet[type] = !filterSet[type];
                var btn = document.getElementById('fb-'+type);
                if (filterSet[type]) btn.classList.add('active');
                else btn.classList.remove('active');
                renderList();
            }

            function sortList(col) {
                if (sortCol===col) sortAsc = !sortAsc;
                else { sortCol=col; sortAsc=true; }
                ['name','status','part','user','rate','sim','remaining','eta','update'].forEach(function(c) {
                    var th = document.getElementById('th-'+c);
                    if (!th) return;
                    th.classList.remove('sort-asc','sort-desc');
                    var colMatch = (c==='sim') ? (col==='sim_time') : (c===col);
                    if (colMatch) th.classList.add(sortAsc?'sort-asc':'sort-desc');
                });
                renderList();
            }

            function renderList() {
                var q = (document.getElementById('list-search').value||'').toLowerCase();
                var af = Object.keys(filterSet).filter(function(k){ return filterSet[k]; });
                var rows = listData.filter(function(d) {
                    if (af.length>0 && af.indexOf(d.type)<0) return false;
                    if (q && (d.name+d.part+d.user).toLowerCase().indexOf(q)<0) return false;
                    return true;
                });
                rows.sort(function(a,b) {
                    var va,vb;
                    if (sortCol==='rate')     { return sortAsc?(parseFloat(a.rate)||0)-(parseFloat(b.rate)||0):(parseFloat(b.rate)||0)-(parseFloat(a.rate)||0); }
                    if (sortCol==='sim_time')  { return sortAsc?(a.sim_time||0)-(b.sim_time||0):(b.sim_time||0)-(a.sim_time||0); }
                    if (sortCol==='remaining') { var ra=a.remaining!=null?a.remaining:Infinity, rb=b.remaining!=null?b.remaining:Infinity; return sortAsc?ra-rb:rb-ra; }
                    if (sortCol==='eta')       { var ea=a.eta||'99:99', eb=b.eta||'99:99'; return sortAsc?(ea<eb?-1:ea>eb?1:0):(ea>eb?-1:ea<eb?1:0); }
                    if (sortCol==='name')   { va=a.name;   vb=b.name; }
                    else if (sortCol==='status') {
                        var sOrd={'alarm':0,'stop':1,'run':2,'offline':3};
                        var oa=sOrd[a.type]!=null?sOrd[a.type]:4;
                        var ob=sOrd[b.type]!=null?sOrd[b.type]:4;
                        return sortAsc ? oa-ob : ob-oa;
                    }
                    else if (sortCol==='part')   { va=a.part;   vb=b.part; }
                    else if (sortCol==='user')   { va=a.user;   vb=b.user; }
                    else if (sortCol==='update') { va=a.update; vb=b.update; }
                    else { va=''; vb=''; }
                    if (va<vb) return sortAsc?-1:1;
                    if (va>vb) return sortAsc?1:-1;
                    return 0;
                });
                if (!rows.length) {
                    document.getElementById('list-body').innerHTML='<tr><td colspan="10" style="text-align:center;color:#484f58;padding:24px;">ไม่พบข้อมูล</td></tr>';
                    return;
                }
                var html='';
                rows.forEach(function(d) {
                    var pc = getPartBgColor(d.part);
                    var pillC = {run:'pill-run',stop:'pill-stop',alarm:'pill-alarm',offline:'pill-off'}[d.type]||'pill-off';
                    var rn = parseFloat(d.rate)||0;
                    var rc = rn>=80?'#00a83b':rn>=50?'#e08000':'#d32f2f';
                    var sp='<span class="sp-mini">';
                    d.spindles.forEach(function(on){ sp+='<span class="sp-dot'+(on?' on':'')+'"></span>'; });
                    sp+='</span>';
                    var remCell;
                    if (d.remaining===null) {
                        remCell = '—';
                    } else if (d.remaining <= 0) {
                        var dcL = getDoneCount(d.name, 0);
                        remCell = '<span style="color:#d32f2f;font-weight:900;font-size:15px;">Soon!</span>';
                    } else {
                        remCell = '<span style="color:#00a83b;font-weight:700;">'+(function(s){var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=s%60;return h+':'+String(m).padStart(2,'0')+':'+String(sc).padStart(2,'0');})(d.remaining)+'</span>';
                    }
                    var etaCell;
                    if (d.eta) {
                        etaCell = '<span style="font-weight:900;font-size:14px;color:#1565c0;">'+d.eta+'</span>';
                    } else if (d.remaining !== null && d.remaining <= 0) {
                        var dcE = getDoneCount(d.name, 0);
                        etaCell = '<span style="color:#d32f2f;font-weight:900;font-size:15px;">Soon!</span>';
                    } else {
                        etaCell = '—';
                    }
                    html+=
                        '<tr>'+
                        '<td style="font-weight:900;color:#1a202c;font-size:15px;">'+d.name+'</td>'+
                        '<td><span class="pill '+pillC+'">'+d.label+'</span></td>'+
                        '<td><span class="color-swatch" data-part="'+d.part+'" style="background:'+pc.bg+'"></span>'+(d.part||'—')+'</td>'+
                        '<td style="color:#37474f;">'+(d.user||'—')+'</td>'+
                        '<td><div class="rate-bar-wrap">'+
                            '<div class="rate-bar"><div class="rate-fill" style="width:'+rn+'%;background:'+rc+'"></div></div>'+
                            '<span class="rate-text" style="color:'+rc+'">'+d.rate+'%</span>'+
                        '</div></td>'+
                        '<td>'+sp+'</td>'+
                        '<td style="color:#b07500;font-weight:700;text-align:right;">'+
                            (d.sim_time>0?(function(s){var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=s%60;return h+':'+String(m).padStart(2,'0')+':'+String(sc).padStart(2,'0');})(d.sim_time):'—')+
                        '</td>'+
                        '<td style="font-weight:700;text-align:right;">'+remCell+'</td>'+
                        '<td style="font-weight:900;font-size:14px;text-align:center;">'+etaCell+'</td>'+
                        '<td style="color:#78909c;font-size:11px;">'+(d.update||'—')+'</td>'+
                        '</tr>';
                });
                document.getElementById('list-body').innerHTML=html;
            }

            /* ===== DONE COUNTER (counts how many cycles overdue) ===== */
            var doneCounters = {};   // key = machineName → { startSec: epoch_ms, count: n }

            function getDoneCount(machineName, cycleSec) {
                /* เมื่อ remSec <= 0 ให้คำนวณว่าเกินไปกี่ cycle แล้ว */
                if (!doneCounters[machineName]) {
                    doneCounters[machineName] = { startMs: Date.now(), cycleSec: cycleSec };
                }
                var entry = doneCounters[machineName];
                // ถ้า cycle เปลี่ยน reset
                if (cycleSec && entry.cycleSec !== cycleSec) {
                    entry.startMs = Date.now();
                    entry.cycleSec = cycleSec;
                }
                var elapsedSec = (Date.now() - entry.startMs) / 1000;
                var overSec = cycleSec > 0 ? elapsedSec : elapsedSec;
                var count = Math.max(1, Math.floor(overSec / Math.max(cycleSec||60, 60)) + 1);
                return count;
            }

            function resetDoneCounter(machineName) {
                delete doneCounters[machineName];
            }

            /* ===== CARD DISPLAY MODE ===== */
            var cardDisplayMode = 'remain';
            var FMT_HMS = function(s){var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=s%60;return h+':'+String(m).padStart(2,'0')+':'+String(sc).padStart(2,'0');};

            var fitScreenMode = false;
            var manualScale = null; // null = auto
            var _autoInitScale = null; // scale applied on startup (no fit-screen mode)

            function calcAutoScale() {
                var scaler = document.getElementById('grid-scaler');
                if (!scaler) return 1;
                // natural size ของ grid (ก่อน scale)
                var naturalW = scaler.scrollWidth;
                var naturalH = scaler.scrollHeight;
                // พื้นที่ที่ใช้ได้
                var ctrl = document.querySelector('#s2-section .list-controls');
                var ctrlH = ctrl ? ctrl.getBoundingClientRect().height + 10 : 60;
                var availW = window.innerWidth  - 16;
                var availH = window.innerHeight - ctrlH - 16;
                var sx = availW / naturalW;
                var sy = availH / naturalH;
                return Math.min(sx, sy, 1);
            }

            function applyScale() {
                var scaler = document.getElementById('grid-scaler');
                if (!scaler || !fitScreenMode) return;
                var s = manualScale !== null ? manualScale : calcAutoScale();
                scaler.style.transform = 'scale('+s+')';
                scaler.style.transformOrigin = 'top left';
                // ปรับ container height ให้ตาม scaled height
                var wrap = document.getElementById('grid-scale-wrap');
                if (wrap) wrap.style.height = Math.round(scaler.scrollHeight * s) + 'px';
                // sync slider
                var sl = document.getElementById('scale-slider');
                if (sl && manualScale !== null) sl.value = Math.round(manualScale * 100);
                var slSb = document.getElementById('scale-slider-sb');
                if (slSb) slSb.value = manualScale !== null ? Math.round(manualScale*100) : (calcAutoScale()*100).toFixed(0);
                var lblSb = document.getElementById('scale-label-sb');
                if (lblSb) lblSb.textContent = manualScale !== null ? Math.round(manualScale*100)+'%' : 'Auto';
            }

            function toggleFitScreen() {
                fitScreenMode = !fitScreenMode;
                var btn = document.getElementById('fit-btn');
                var sliderWrap = document.getElementById('scale-slider-wrap');
                if (fitScreenMode) {
                    btn.style.cssText = 'color:#080c12;background:#e3b341;border-color:#e3b341;font-size:13px;padding:7px 16px;font-weight:900;letter-spacing:1px;';
                    btn.innerHTML = '&#10005; Exit Fit';
                    document.body.classList.add('fit-screen');
                    if (sliderWrap) sliderWrap.style.display = 'flex';
                    // Clear startup auto-scale before applying fit-screen scale
                    _autoInitScale = null;
                    var wrap0 = document.getElementById('grid-scale-wrap');
                    if (wrap0) { wrap0.style.height=''; wrap0.style.width=''; wrap0.style.overflow=''; }
                    var sc0 = document.getElementById('grid-scaler');
                    if (sc0) sc0.style.transform = '';
                    manualScale = null;
                    setTimeout(applyScale, 50);
                } else {
                    btn.style.cssText = 'color:#e3b341;border-color:#e3b341;font-size:13px;padding:7px 16px;font-weight:900;letter-spacing:1px;';
                    btn.innerHTML = '&#9635; Fit Screen';
                    document.body.classList.remove('fit-screen');
                    if (sliderWrap) sliderWrap.style.display = 'none';
                    var scaler = document.getElementById('grid-scaler');
                    if (scaler) { scaler.style.transform = ''; }
                    var wrap = document.getElementById('grid-scale-wrap');
                    if (wrap) wrap.style.height = '';
                    manualScale = null;
                }
            }

            function onSliderChange(val) {
                manualScale = val / 100;
                var lbl = document.getElementById('scale-label');
                if (lbl) lbl.textContent = val + '%';
                var lblSb = document.getElementById('scale-label-sb');
                if (lblSb) lblSb.textContent = val + '%';
                var sl = document.getElementById('scale-slider-sb');
                if (sl) sl.value = val;
                applyScale();
            }

            window.addEventListener('resize', function(){
                if (fitScreenMode && manualScale === null) applyScale();
                // re-inject SVG overlays since card sizes may have changed
                document.querySelectorAll('#dashboard .card.card-marching').forEach(function(card) {
                    var isOverdue = (parseInt(card.getAttribute('data-remain')||'1')) <= 0;
                    injectMarchSvg(card, isOverdue);
                });
            });

            /* ===== VISUAL LAYOUT EDITOR ===== */
            var layoutEditMode = false; // true while layout editor is open
            var leLayout = {};      // working copy: machine → {c, r}
            var leDragSrc = null;   // 'machine:T01' or 'pool:T01'
            var leAllMachines = []; // all known machines
            var leSpacerRows = {};  // row index (int) → true = spacer
            var leSpacerCols = {};  // col index (int) → true = spacer

            function toggleLayoutEdit() {
                // Require password before opening editor
                openPwModal(function(val) {
                    if (val !== '@A30.123') return false;
                    closePwModal();
                    _openLayoutEditor();
                    return true;
                }, '🔒 ใส่รหัส Edit Layout');
            }

            function _openLayoutEditor() {
                layoutEditMode = true;
                // Deep-copy current mapLayout to working copy
                leLayout = {};
                leSpacerRows = {};
                leSpacerCols = {};
                Object.keys(mapLayout).forEach(function(m) {
                    leLayout[m] = Object.assign({}, mapLayout[m]);
                });
                // Load spacer metadata if stored
                if (mapLayout.__spacerRows) {
                    mapLayout.__spacerRows.forEach(function(r){ leSpacerRows[r] = true; });
                }
                if (mapLayout.__spacerCols) {
                    mapLayout.__spacerCols.forEach(function(c){ leSpacerCols[c] = true; });
                }
                // Collect all machines from mapLayout + live data
                var known = {};
                Object.keys(mapLayout).forEach(function(m){ known[m] = true; });
                Object.keys(machine_data_snapshot || {}).forEach(function(m){ known[m] = true; });
                leAllMachines = Object.keys(known).sort();
                // Set grid size from current layout extent
                var maxC = 9, maxR = 16;
                Object.values(leLayout).forEach(function(p){
                    if (p.c > maxC) maxC = p.c;
                    if (p.r > maxR) maxR = p.r;
                });
                document.getElementById('le-cols').value = maxC;
                document.getElementById('le-rows').value = maxR;
                leRender();
                document.getElementById('le-modal').classList.add('open');
            }  // end _openLayoutEditor

            function leClose() {
                layoutEditMode = false;
                document.getElementById('le-modal').classList.remove('open');
            }

            function leAdjust(dim, delta) {
                var id = dim === 'c' ? 'le-cols' : 'le-rows';
                var el = document.getElementById(id);
                var val = parseInt(el.value) + delta;
                if (val < 1) val = 1;
                if (val > (dim === 'c' ? 20 : 30)) return;
                el.value = val;
                leRender();
            }

            function leAddMachine() {
                var inp = document.getElementById('le-new-name');
                var name = inp.value.trim().toUpperCase();
                if (!name) return;
                if (leAllMachines.indexOf(name) < 0) leAllMachines.push(name);
                inp.value = '';
                leRender();
            }

            function leRender() {
                var cols = Math.max(1, parseInt(document.getElementById('le-cols').value) || 9);
                var rows = Math.max(1, parseInt(document.getElementById('le-rows').value) || 16);

                // Build reverse map: "c,r" → machine
                var cellMap = {};
                Object.keys(leLayout).forEach(function(m) {
                    var p = leLayout[m];
                    cellMap[p.c + ',' + p.r] = m;
                });

                // Pool
                var placed = {};
                Object.keys(leLayout).forEach(function(m){ placed[m] = true; });
                var pool = leAllMachines.filter(function(m){ return !placed[m]; });
                var poolEl = document.getElementById('le-pool');
                if (pool.length === 0) {
                    poolEl.innerHTML = '<div style="font-size:11px;color:#b0bec5;text-align:center;">ทุกเครื่องมีตำแหน่งแล้ว</div>';
                } else {
                    poolEl.innerHTML = pool.map(function(m) {
                        return '<div class="le-pool-item" draggable="true"'
                            + ' data-src="pool" data-machine="' + m + '"'
                            + ' ondragstart="leDragStart(event,this)"'
                            + ' ondragend="leDragEndPool(event)">'
                            + m + '</div>';
                    }).join('');
                }

                // Grid template — spacer cols/rows are compressed
                var colTpl = '24px';
                for (var ci = 1; ci <= cols; ci++) {
                    colTpl += leSpacerCols[ci] ? ' 14px' : ' 88px';
                }
                var rowTpl = '20px';
                for (var ri = 1; ri <= rows; ri++) {
                    rowTpl += leSpacerRows[ri] ? ' 14px' : ' 68px';
                }
                var grid = document.getElementById('le-grid');
                grid.style.gridTemplateColumns = colTpl;
                grid.style.gridTemplateRows    = rowTpl;

                var html = '<div></div>';
                // Col headers
                for (var c = 1; c <= cols; c++) {
                    var cSp = !!leSpacerCols[c];
                    html += '<div class="le-col-header' + (cSp ? ' is-spacer' : '') + '"'
                        + ' title="' + (cSp ? 'ยกเลิก spacer Col ' + c : 'ตั้ง spacer Col ' + c) + '"'
                        + ' onclick="leToggleSpacerCol(' + c + ')">'
                        + (cSp ? '&#8596;' : 'C' + c) + '</div>';
                }
                // Rows
                for (var r = 1; r <= rows; r++) {
                    var rSp = !!leSpacerRows[r];
                    html += '<div class="le-row-header' + (rSp ? ' is-spacer' : '') + '"'
                        + ' title="' + (rSp ? 'ยกเลิก spacer Row ' + r : 'ตั้ง spacer Row ' + r) + '"'
                        + ' onclick="leToggleSpacerRow(' + r + ')">'
                        + (rSp ? '&#8597;' : 'R' + r) + '</div>';
                    for (var c2 = 1; c2 <= cols; c2++) {
                        var key2 = c2 + ',' + r;
                        var mach = cellMap[key2] || '';
                        var cSp2 = !!leSpacerCols[c2];
                        var xCls = (rSp ? ' spacer-row' : '') + (cSp2 ? ' spacer-col' : '');
                        if (mach) {
                            var bg = getPartBgColor(
                                (machine_data_snapshot && machine_data_snapshot[mach])
                                    ? machine_data_snapshot[mach].part_no : '').bg;
                            html += '<div class="le-cell has-machine' + xCls + '"'
                                + ' data-c="' + c2 + '" data-r="' + r + '"'
                                + ' ondragover="leCellOver(event)" ondragleave="leCellLeave(event)" ondrop="leCellDrop(event,' + c2 + ',' + r + ')">'
                                + '<div class="le-chip' + (rSp ? ' spacer-row' : '') + '"'
                                + ' style="background:' + bg + ';"'
                                + ' data-src="placed" data-machine="' + mach + '"'
                                + ' ondragstart="leDragStart(event,this)"'
                                + ' ondragend="leDragEndChip(event)">'
                                + (rSp ? '' : '<button class="le-chip-remove" data-machine="' + mach + '" onclick="leRemove(event,this)">&#10005;</button>')
                                + '<div class="le-chip-name" style="font-size:' + (rSp ? 9 : 13) + 'px;">' + mach + '</div>'
                                + '</div></div>';
                        } else {
                            html += '<div class="le-cell' + xCls + '"'
                                + ' data-c="' + c2 + '" data-r="' + r + '"'
                                + ' ondragover="leCellOver(event)" ondragleave="leCellLeave(event)" ondrop="leCellDrop(event,' + c2 + ',' + r + ')">'
                                + '</div>';
                        }
                    }
                }
                grid.innerHTML = html;
            }

            function leToggleSpacerRow(r) {
                if (leSpacerRows[r]) { delete leSpacerRows[r]; } else { leSpacerRows[r] = true; }
                leRender();
            }

            function leToggleSpacerCol(c) {
                if (leSpacerCols[c]) { delete leSpacerCols[c]; } else { leSpacerCols[c] = true; }
                leRender();
            }

            function leDragStart(e, el) {
                var srcType = el.getAttribute('data-src');
                var machine = el.getAttribute('data-machine');
                leDragSrc = srcType + ':' + machine;
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', machine);
                if (srcType === 'placed') {
                    el.parentElement.style.opacity = '0.3';
                } else {
                    el.style.opacity = '0.3';
                }
            }
            function leDragEndChip(e) {
                e.currentTarget.parentElement.style.opacity = '';
            }
            function leDragEndPool(e) {
                e.currentTarget.style.opacity = '';
            }
            function leCellOver(e) {
                e.preventDefault();
                e.currentTarget.classList.add('drag-over-cell');
            }
            function leCellLeave(e) {
                e.currentTarget.classList.remove('drag-over-cell');
            }
            function leCellDrop(e, col, row) {
                e.preventDefault();
                e.currentTarget.classList.remove('drag-over-cell');
                if (!leDragSrc) return;
                var parts = leDragSrc.split(':');
                var srcType = parts[0];
                var machine = parts.slice(1).join(':');

                // Find existing occupant at target cell
                var targetKey = col + ',' + row;
                var occupant = null;
                Object.keys(leLayout).forEach(function(m) {
                    if (leLayout[m].c === col && leLayout[m].r === row) occupant = m;
                });

                if (srcType === 'placed') {
                    // Get source position
                    var srcPos = leLayout[machine] ? Object.assign({}, leLayout[machine]) : null;
                    if (occupant && occupant !== machine) {
                        // Swap
                        leLayout[occupant] = srcPos;
                    }
                    leLayout[machine] = {c: col, r: row};
                } else {
                    // From pool
                    if (occupant) {
                        // Swap occupant to pool
                        delete leLayout[occupant];
                    }
                    leLayout[machine] = {c: col, r: row};
                }
                leDragSrc = null;
                leRender();
            }

            function leRemove(e, btn) {
                e.stopPropagation();
                var machine = btn.getAttribute('data-machine');
                delete leLayout[machine];
                leRender();
            }

            function leSave() {
                // Apply leLayout to mapLayout
                Object.keys(leLayout).forEach(function(m) {
                    mapLayout[m] = Object.assign({}, leLayout[m]);
                });
                // Machines removed from layout — clear from mapLayout
                Object.keys(mapLayout).forEach(function(m) {
                    if (m.indexOf('__') === 0) return;
                    if (!leLayout[m]) delete mapLayout[m];
                });
                // Store spacer metadata on mapLayout for reuse
                mapLayout.__spacerRows = Object.keys(leSpacerRows).map(Number);
                mapLayout.__spacerCols = Object.keys(leSpacerCols).map(Number);

                // Apply spacer to real dashboard grid immediately
                applySpacerToDashboard();

                // Build payload — machines only (no __ keys)
                var payload = {};
                Object.keys(leLayout).forEach(function(m) {
                    if (m.indexOf('__') !== 0) payload[m] = leLayout[m];
                });
                // Spacers as separate keys (handled by backend into layout_spacers table)
                payload['__spacerRows'] = {spacers: Object.keys(leSpacerRows).map(Number)};
                payload['__spacerCols'] = {spacers: Object.keys(leSpacerCols).map(Number)};

                fetch('/api/layout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                })
                .then(function(r){ return r.json(); })
                .then(function(res) {
                    leClose();
                    if (res.ok) { updateDashboard(); }
                })
                .catch(function(err){ alert('บันทึกไม่ได้: ' + err); });
            }

            function applySpacerToDashboard() {
                var maxR = 16, maxC = 9;
                Object.keys(mapLayout).forEach(function(k) {
                    var p = mapLayout[k];
                    if (!p || k.indexOf('__') === 0) return;
                    if (p.r && p.r > maxR) maxR = p.r;
                    if (p.c && p.c > maxC) maxC = p.c;
                });
                var sRows = mapLayout.__spacerRows || [];
                var sCols = mapLayout.__spacerCols || [];

                // Row template
                var rowTpl = '';
                for (var r = 1; r <= maxR; r++) {
                    rowTpl += sRows.indexOf(r) >= 0
                        ? ' minmax(6px,8px)'
                        : ' minmax(var(--row-min,10px),auto)';
                }
                // Col template
                var colTpl = '';
                for (var c = 1; c <= maxC; c++) {
                    colTpl += sCols.indexOf(c) >= 0
                        ? ' minmax(6px,10px)'
                        : ' minmax(90px,1fr)';
                }

                var dashEl = document.getElementById('dashboard');
                if (dashEl) {
                    dashEl.style.gridTemplateRows    = rowTpl.trim();
                    dashEl.style.gridTemplateColumns = colTpl.trim();
                    dashEl.style.setProperty('--grid-cols', maxC);
                    dashEl.style.setProperty('--grid-rows', maxR);
                }
                if (fitScreenMode) setTimeout(applyScale, 50);
            }

            var emptyRowPx = 10;
            function setEmptyRowHeight(val) {
                emptyRowPx = parseInt(val);
                var lbl = document.getElementById('empty-row-label');
                if (lbl) lbl.textContent = val + 'px';
                // อัพเดท CSS variable บน grid container
                var grid = document.getElementById('dashboard');
                if (grid) {
                    grid.style.setProperty('--row-min', val + 'px');
                    // ปรับ card-offline ด้วย
                    grid.querySelectorAll('.card-offline').forEach(function(c) {
                        c.style.minHeight = val + 'px';
                        c.style.maxHeight = val + 'px';
                        c.style.padding   = val < 10 ? '0' : '';
                        c.style.overflow  = 'hidden';
                    });
                }
                // re-fit ถ้า fit mode อยู่
                if (fitScreenMode) setTimeout(applyScale, 30);
            }

            function syncSbFilter() {
                ['run','stop','alarm'].forEach(function(t) {
                    var sb = document.getElementById('sb-fb-'+t);
                    var mb = document.getElementById('map-fb-'+t);
                    if (!sb || !mb) return;
                    if (mb.classList.contains('active')) sb.classList.add('active');
                    else sb.classList.remove('active');
                });
            }

            function syncSbPartSelect(data) {
                var sel = document.getElementById('map-part-select');
                var sbSel = document.getElementById('map-part-select-sb');
                if (!sel || !sbSel) return;
                sbSel.innerHTML = sel.innerHTML;
                sbSel.value = sel.value;
            }

            function injectMarchSvg(card, isOverdue) {
                var old = card.querySelector('.march-svg');
                if (old) old.remove();
                var W = card.offsetWidth;
                var H = card.offsetHeight;
                if (!W || !H) return; // not laid out yet
                var svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
                svg.setAttribute('class','march-svg');
                svg.setAttribute('viewBox','0 0 '+W+' '+H);
                svg.setAttribute('preserveAspectRatio','none');
                svg.style.cssText =
                    'position:absolute;top:0;left:0;width:100%;height:100%;'+
                    'pointer-events:none;z-index:30;overflow:visible;';
                var rect = document.createElementNS('http://www.w3.org/2000/svg','rect');
                rect.setAttribute('x','2'); rect.setAttribute('y','2');
                rect.setAttribute('width', W-4);
                rect.setAttribute('height', H-4);
                rect.setAttribute('rx','7'); rect.setAttribute('ry','7');
                rect.setAttribute('fill','none');
                rect.setAttribute('stroke', '#ffffff');
                rect.setAttribute('stroke-width','3');
                rect.setAttribute('stroke-dasharray','12 6');
                rect.style.animation = 'march-offset 0.55s linear infinite';
                svg.appendChild(rect);
                card.appendChild(svg);
            }

            function applyBlinkClass() {
                var threshEl = document.getElementById('blink-threshold');
                var threshSec = threshEl ? (parseFloat(threshEl.value)||30)*60 : 30*60;
                var cards = document.querySelectorAll('#dashboard .card[data-remain]');
                cards.forEach(function(card) {
                    var rem = card.getAttribute('data-remain');
                    var remSec = (rem!==''&&rem!==null) ? parseInt(rem) : null;
                    var isNearDone = remSec!==null && remSec>0 && remSec<threshSec;
                    var isOverdue  = remSec!==null && remSec<=0;
                    var shouldMarch = (cardDisplayMode==='remain'||cardDisplayMode==='eta') && (isNearDone||isOverdue);

                    if (shouldMarch) {
                        card.classList.add('card-marching');
                        if (!card.querySelector('.march-svg')) {
                            // defer one frame so layout is complete
                            (function(c, od){ requestAnimationFrame(function(){ injectMarchSvg(c, od); }); })(card, isOverdue);
                        }
                    } else {
                        card.classList.remove('card-marching');
                        var oldSvg = card.querySelector('.march-svg');
                        if (oldSvg) oldSvg.remove();
                    }
                });
            }

            function setCardDisplay(mode) {
                cardDisplayMode = mode;
                var cards = document.querySelectorAll('#dashboard .card[data-runtime]');
                cards.forEach(function(card) {
                    var rateEl = card.querySelector('.run-rate');
                    if (!rateEl) return;
                    var mName = card.querySelector('.machine-name');
                    var mKey = mName ? mName.textContent.trim() : '';
                    if (mode === 'runtime') {
                        var s = parseInt(card.getAttribute('data-runtime')||'0');
                        rateEl.textContent = s>0?FMT_HMS(s):'—';
                        rateEl.style.color = '';
                    } else if (mode === 'remain') {
                        var rem = card.getAttribute('data-remain');
                        if (rem === null || rem === 'null' || rem === '') {
                            rateEl.textContent = '—'; rateEl.style.color = 'rgba(255,255,255,0.5)';
                        } else {
                            var rs = parseInt(rem);
                            if (rs <= 0) {
                                var dc = getDoneCount(mKey, 0);
                                rateEl.innerHTML = '<span style="color:#ff4d4d;font-weight:900;font-size:14px;">Soon!</span>';
                            } else {
                                rateEl.innerHTML = '<span style="color:#a5f3c0;">'+FMT_HMS(rs)+'</span>';
                            }
                        }
                    } else if (mode === 'eta') {
                        var eta = card.getAttribute('data-eta')||'';
                        var rem2 = card.getAttribute('data-remain');
                        var threshSec3=(parseFloat((document.getElementById('blink-threshold')||{}).value||30))*60;
                        var remNum2 = (rem2!==''&&rem2!==null)?parseInt(rem2):null;
                        var etaCol = (remNum2!==null&&remNum2>0&&remNum2<threshSec3)?'#ff6b35':'#a5f3c0';
                        if (eta) { rateEl.innerHTML='<span style="color:'+etaCol+';font-weight:900;">'+eta+'</span>'; }
                        else if (remNum2!==null&&remNum2<=0) {
                            var dc4 = getDoneCount(mKey, 0);
                            rateEl.innerHTML='<span style="color:#ff4d4d;font-weight:900;font-size:14px;">Soon!</span>';
                        }
                        else { rateEl.textContent='—'; rateEl.style.color='rgba(255,255,255,0.5)'; }
                    } else {
                        rateEl.textContent = card.getAttribute('data-runrate')+'%';
                        rateEl.style.color = '';
                    }
                });
                applyBlinkClass();
            }

            
            /* ===== OPERATOR PHOTO ERROR HANDLER ===== */
            function opPhotoErr(img) {
                img.style.display = 'none';
                var ph = img.nextElementSibling;
                if (ph) ph.style.display = 'flex';
            }

            /* ===== MGR PLC DATA ===== */
            var mgrLatestByMachine = {};   // key=LINE_NUM normalized → latest record
            var machine_data_snapshot = {};  // snapshot ล่าสุดจาก /api/machines

            function normMgrKey(m) {
                // T1→T01, T9→T09 (reverse: ระบบ monitoring ใช้ T01-T09)
                // และ T01→T1 (MGR ส่งมา T1-T9 ไม่มีเลขศูนย์นำหน้า)
                // ใช้ทั้งสองทิศทาง: ถ้า m='T1' return 'T01', ถ้า m='T01' return 'T1'
                if (/^T[1-9]$/.test(m)) return 'T0' + m[1];   // T1 → T01
                if (/^T0[1-9]$/.test(m)) return 'T' + m[2];    // T01 → T1
                return m;
            }

            function fmtMgrTime(t) {
                if (!t) return '—';
                // "2026/06/09 00:05:34" → "06/09 00:05"
                var p = t.split(' ');
                if (p.length >= 2) {
                    var datePart = p[0].split('/').slice(1).join('/'); // MM/DD
                    var timePart = p[1].substring(0,5);               // HH:MM
                    return datePart + ' ' + timePart;
                }
                return t.substring(0,16);
            }

            function loadMgrData() {
                fetch('/api/mgr_plc')
                    .then(function(r){ return r.json(); })
                    .then(function(records) {
                        var latest = {};
                        records.forEach(function(r) {
                            var ln = (r.LINE_NUM || '').trim();
                            if (!ln) return;
                            // เก็บ record ล่าสุดต่อเครื่อง (เรียงโดย START_TIME)
                            if (!latest[ln] || (r.START_TIME||'') > (latest[ln].START_TIME||'')) {
                                latest[ln] = r;
                            }
                        });
                        mgrLatestByMachine = latest;
                    })
                    .catch(function(){});
            }

            // โหลด MGR ทันทีและทุก 2 นาที
            loadMgrData();
            setInterval(loadMgrData, 300000);  // ทุก 5 นาที ตาม backend

            /* ===== MGR MODAL ===== */
            function openMgrModal(machineName) {
                if (layoutEditMode) return; // ไม่เปิด modal ตอน edit layout
                var modal = document.getElementById('mgr-modal');
                var mTitle = document.getElementById('mgr-modal-title');
                var mBody  = document.getElementById('mgr-modal-body');
                mTitle.textContent = '📋 ' + machineName + ' — ประวัติ 2 วันล่าสุด';
                mBody.innerHTML = '<div style="text-align:center;padding:30px;color:#90a4ae;">กำลังโหลด...</div>';
                modal.style.display = 'flex';

                fetch('/api/mgr_plc/machine/' + encodeURIComponent(machineName))
                    .then(function(r){ return r.json(); })
                    .then(function(rows) {
                        var machInfo = machine_data_snapshot ? machine_data_snapshot[machineName] : null;
                        var machUser = machInfo ? (machInfo.user || '—') : '—';

                        // -- Operator badge: ดึงชื่อ+รูปจาก rows[0] (latest) --
                        var latestOp = '';
                        if (rows && rows.length > 0) {
                            latestOp = (rows[0].OPERATOR_NO || '').trim();
                        }

                        function buildBadge(opName) {
                            var photoUrl = latestOp ? ('/api/emp_photo/' + encodeURIComponent(latestOp)) : '';
                            var photoTag = photoUrl
                                ? '<img class="op-photo" src="' + photoUrl + '" alt="op" onerror="opPhotoErr(this)">'
                                  + '<div class="op-photo-placeholder" style="display:none;">&#128100;</div>'
                                : '<div class="op-photo-placeholder">&#128100;</div>';
                            var nameHtml = opName
                                ? '<div class="op-name">' + opName + '</div><div class="op-id">ID: ' + latestOp + '</div>'
                                : (latestOp
                                    ? '<div class="op-name" style="color:#78909c;">' + latestOp + '</div><div class="op-id">ไม่พบชื่อใน DB</div>'
                                    : '<div class="op-name" style="color:#78909c;">ไม่มีข้อมูล Operator</div>');
                            return '<div class="op-badge">'
                                + photoTag
                                + '<div class="op-info">'
                                + '<div style="font-size:11px;color:#78909c;margin-bottom:4px;">Operator ล่าสุด (MGR)</div>'
                                + nameHtml
                                + '<div class="op-user-row">&#129302; Machine User: <span style="color:#37474f;font-weight:400;">' + machUser + '</span></div>'
                                + '</div></div>';
                        }

                        function buildTable(rows) {
                            if (!rows || rows.length === 0) {
                                return '<div style="text-align:center;padding:20px;color:#90a4ae;">ไม่พบข้อมูลในช่วง 2 วันที่ผ่านมา</div>';
                            }
                            var html = '<div style="overflow-x:auto;">'
                                + '<table style="width:100%;border-collapse:collapse;font-size:12px;">'
                                + '<thead><tr style="background:#e3f2fd;border-bottom:2px solid #90caf9;">'
                                + '<th style="padding:7px 10px;text-align:left;color:#1565c0;white-space:nowrap;">#</th>'
                                + '<th style="padding:7px 10px;text-align:left;color:#1565c0;white-space:nowrap;">LOT NUM</th>'
                                + '<th style="padding:7px 10px;text-align:left;color:#1565c0;white-space:nowrap;">PART NUM</th>'
                                + '<th style="padding:7px 10px;text-align:center;color:#1565c0;white-space:nowrap;">OPERATOR</th>'
                                + '<th style="padding:7px 10px;text-align:center;color:#1565c0;white-space:nowrap;">QTY</th>'
                                + '<th style="padding:7px 10px;text-align:center;color:#1565c0;white-space:nowrap;">START TIME</th>'
                                + '<th style="padding:7px 10px;text-align:center;color:#1565c0;white-space:nowrap;">END TIME</th>'
                                + '<th style="padding:7px 10px;text-align:left;color:#1565c0;white-space:nowrap;">PROGRAM</th>'
                                + '</tr></thead><tbody>';
                            rows.forEach(function(r, i) {
                                var bg = i % 2 === 0 ? '#ffffff' : '#f5f8ff';
                                var endVal = r.END_TIME || '<span style="color:#e08000;">—</span>';
                                var prog = (r.PROGRAM_NAME || '').replace(/^PC:/,'').replace(/;/g,' ');
                                html += '<tr style="background:' + bg + ';border-bottom:1px solid #e0e0e0;">'
                                    + '<td style="padding:5px 10px;color:#90a4ae;font-size:11px;">' + (i+1) + '</td>'
                                    + '<td style="padding:5px 10px;font-weight:700;color:#1a202c;">' + (r.LOT_NUM||'—') + '</td>'
                                    + '<td style="padding:5px 10px;color:#1565c0;font-weight:700;">' + (r.PART_NUM||'—') + '</td>'
                                    + '<td style="padding:5px 10px;text-align:center;color:#37474f;">' + (r.OPERATOR_NO||'—') + '</td>'
                                    + '<td style="padding:5px 10px;text-align:center;font-weight:700;color:#00796b;">' + (r.LOT_QTY||'—') + '</td>'
                                    + '<td style="padding:5px 10px;text-align:center;color:#5c6bc0;white-space:nowrap;">' + (r.START_TIME||'—') + '</td>'
                                    + '<td style="padding:5px 10px;text-align:center;white-space:nowrap;">' + endVal + '</td>'
                                    + '<td style="padding:5px 10px;color:#546e7a;font-size:11px;word-break:break-all;">' + prog + '</td>'
                                    + '</tr>';
                            });
                            html += '</tbody></table></div>';
                            return html;
                        }

                        // ดึงชื่อพนักงานจาก employee DB
                        if (latestOp) {
                            fetch('/api/emp_info/' + encodeURIComponent(latestOp))
                                .then(function(r){ return r.json(); })
                                .then(function(empData) {
                                    mBody.innerHTML = buildBadge(empData.name) + buildTable(rows);
                                })
                                .catch(function() {
                                    mBody.innerHTML = buildBadge(null) + buildTable(rows);
                                });
                        } else {
                            mBody.innerHTML = buildBadge(null) + buildTable(rows);
                        }
                    })
                    .catch(function(e) {
                        mBody.innerHTML = '<div style="text-align:center;padding:30px;color:#d32f2f;">โหลดข้อมูลไม่ได้: ' + e + '</div>';
                    });
            }

            function closeMgrModal() {
                document.getElementById('mgr-modal').style.display = 'none';
            }

            // ปิด modal เมื่อกด background
            document.getElementById('mgr-modal').addEventListener('click', function(e) {
                if (e.target === this) closeMgrModal();
            });

/* ===== MAIN UPDATE ===== */
            function updateDashboard() {
                fetch('/api/machines')
                    .then(function(r){ return r.json(); })
                    .then(function(data) {
                        machine_data_snapshot = data;  // snapshot สำหรับ modal

                        /* Section 1: Part Summary */
                        var ps={};
                        Object.keys(data).forEach(function(k) {
                            var info=data[k];
                            var part=(info.part_no&&info.part_no.trim()!=='')?info.part_no.trim():'(ไม่มี Part No.)';
                            if(!ps[part]) ps[part]={run:0,stop:0,alarm:0,spindles:0};
                            var si=getStatusInfo(info.status);
                            if(si.type==='run')   ps[part].run++;
                            if(si.type==='stop')  ps[part].stop++;
                            if(si.type==='alarm') ps[part].alarm++;
                            // นับ spindle open เฉพาะเครื่องที่ Run หรือ Alarm (ไม่รวม Stop)
                            if((si.type==='run'||si.type==='alarm') && info.spindles && Array.isArray(info.spindles)) {
                                info.spindles.forEach(function(on){ if(on) ps[part].spindles++; });
                            }
                        });

                        // helper: ดึง 7 หลักตัวเลขจาก part no ของเครื่อง
                        // เช่น b6970511.pk1k → ตัดตัวอักษรนำหน้า+suffix หลังจุด → "6970511"
                        function extract7(partNo) {
                            if (!partNo) return '';
                            // ตัด suffix หลังจุดออกก่อน เช่น b6970511.pk1k → b6970511
                            var base = partNo.split('.')[0];
                            // ดึงเฉพาะตัวเลข
                            var digits = base.replace(/[^0-9]/g, '');
                            return digits.length >= 7 ? digits.substring(0, 7) : digits;
                        }

                        fetch('/api/pn_config')
                            .then(function(r){ return r.json(); })
                            .then(function(cfg) {
                                var sp=Object.keys(ps).sort(), tr=0,ts=0,ta=0,tb='',tsp=0,tsh=0;
                                sp.forEach(function(part) {
                                    var s=ps[part]; tr+=s.run; ts+=s.stop; ta+=s.alarm;
                                    var tot=s.run+s.stop+s.alarm;
                                    var key=extract7(part);
                                    var info=cfg[key]||{};
                                    var stkVal  = (info.stk!=null)      ? info.stk       : '—';
                                    var cycleVal = (info.cycle_min!=null && info.cycle_min>0) ? info.cycle_min : '—';
                                    var spCount = s.spindles||0;
                                    var stkNum  = (info.stk!=null && info.stk>0) ? info.stk : 0;
                                    var sheets  = spCount > 0 && stkNum > 0 ? spCount * stkNum : '—';
                                    tsp += spCount;
                                    if (typeof sheets === 'number') tsh += sheets;
                                    var curColor = getPartBgColor(part).bg;
                                    var safeP = part.replace(/"/g,'&quot;');
                                    var idKey = 'ci_' + part.replace(/[^a-z0-9]/gi,'_');
                                    tb+='<tr>'+
                                        '<td><div class="part-color-cell">'+
                                          '<div class="part-color-chip" id="chip_'+idKey+'" style="background:'+curColor+';" '+
                                               'title="คลิกเพื่อสุ่มสีใหม่" '+
                                               'onclick="randomPartColor(this)" '+
                                               'data-part="'+safeP+'"></div>'+
                                        '</div></td>'+
                                        '<td style="font-weight:700;color:#1565c0;font-size:13px;text-align:left;">'+part+'</td>'+
                                        '<td class="badge-cell badge-run">'+(s.run>0?s.run:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td class="badge-cell badge-stop">'+(s.stop>0?s.stop:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td class="badge-cell badge-alarm">'+(s.alarm>0?s.alarm:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td class="badge-total">'+tot+'</td>'+
                                        '<td style="color:#1565c0;font-weight:700;">'+stkVal+'</td>'+
                                        '<td style="color:#e08000;font-weight:700;">'+cycleVal+'</td>'+
                                        '<td style="color:#5c6bc0;font-weight:700;">'+(spCount>0?spCount:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td style="color:#00796b;font-weight:700;">'+(typeof sheets==='number'?sheets:'—')+'</td>'+
                                        '</tr>';
                                });
                                if(!sp.length) tb='<tr><td colspan="10" style="text-align:center;color:#90a4ae;padding:20px;">ยังไม่มีข้อมูล</td></tr>';
                                document.getElementById('summary-body').innerHTML=tb;
                                document.getElementById('foot-run').textContent=tr;
                                document.getElementById('foot-stop').textContent=ts;
                                document.getElementById('foot-alarm').textContent=ta;
                                document.getElementById('foot-total').textContent=tr+ts+ta;
                                document.getElementById('foot-spindle').textContent=tsp>0?tsp:'—';
                                document.getElementById('foot-sheets').textContent=tsh>0?tsh:'—';
                            })
                            .catch(function(){
                                // fallback ไม่มี cfg
                                var sp=Object.keys(ps).sort(), tr=0,ts=0,ta=0,tb='',tsp=0;
                                sp.forEach(function(part) {
                                    var s=ps[part]; tr+=s.run; ts+=s.stop; ta+=s.alarm;
                                    var tot=s.run+s.stop+s.alarm;
                                    var spCount = s.spindles||0;
                                    tsp += spCount;
                                    var curColor = getPartBgColor(part).bg;
                                    var safeP = part.replace(/"/g,'&quot;');
                                    var idKey = 'ci_' + part.replace(/[^a-z0-9]/gi,'_');
                                    tb+='<tr>'+
                                        '<td><div class="part-color-cell">'+
                                          '<div class="part-color-chip" id="chip_'+idKey+'" style="background:'+curColor+';" '+
                                               'title="คลิกเพื่อสุ่มสีใหม่" '+
                                               'onclick="randomPartColor(this)" '+
                                               'data-part="'+safeP+'"></div>'+
                                        '</div></td>'+
                                        '<td style="font-weight:700;color:#1565c0;font-size:13px;text-align:left;">'+part+'</td>'+
                                        '<td class="badge-cell badge-run">'+(s.run>0?s.run:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td class="badge-cell badge-stop">'+(s.stop>0?s.stop:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td class="badge-cell badge-alarm">'+(s.alarm>0?s.alarm:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td class="badge-total">'+tot+'</td>'+
                                        '<td>—</td><td>—</td>'+
                                        '<td style="color:#5c6bc0;font-weight:700;">'+(spCount>0?spCount:'<span style="color:#ccc">0</span>')+'</td>'+
                                        '<td style="color:#ccc;">—</td>'+
                                        '</tr>';
                                });
                                document.getElementById('summary-body').innerHTML=tb||'<tr><td colspan="10" style="text-align:center;color:#90a4ae;padding:20px;">ยังไม่มีข้อมูล</td></tr>';
                                document.getElementById('foot-run').textContent=tr;
                                document.getElementById('foot-stop').textContent=ts;
                                document.getElementById('foot-alarm').textContent=ta;
                                document.getElementById('foot-total').textContent=tr+ts+ta;
                                document.getElementById('foot-spindle').textContent=tsp>0?tsp:'—';
                                document.getElementById('foot-sheets').textContent='—';
                            });

                        /* Section 2: Machine Map */
                        fetch('/api/pn_config')
                            .then(function(r){ return r.json(); })
                            .catch(function(){ return {}; })
                            .then(function(cfg2) {
                                var allM=Object.keys(mapLayout).filter(function(k){ return k.indexOf('__') !== 0; });
                                Object.keys(data).forEach(function(k){ if(allM.indexOf(k)<0) allM.push(k); });
                                allM.sort();
                                var mb='';
                                allM.forEach(function(m) {
                                    var info=data[m], gp=mapLayout[m];
                                    var gs=gp?('--map-c:'+gp.c+';--map-r:'+gp.r+';'):'';
                                    if(!info) {
                                        mb+='<div class="card card-offline" style="'+gs+'">'+
                                            '<div class="top-row"><h2 class="machine-name" style="color:#555;">'+m+'</h2></div>'+
                                            '<div class="part-no" style="color:#555;">OFFLINE</div></div>';
                                        return;
                                    }
                                    var si=getStatusInfo(info.status), pc=getPartBgColor(info.part_no);
                                    var spH='';
                                    info.spindles.forEach(function(on){ spH+='<div class="spindle '+(on?'sp-on':'')+'"></div>'; });
                                    var safePart=(info.part_no||'').replace(/"/g,'&quot;');
                                    var rtSec = info.sim_time||0;
                                    var rtStr = rtSec>0?FMT_HMS(rtSec):'—';
                                    // คำนวณ remain
                                    var key2 = extract7(info.part_no||'');
                                    var cfg2info = cfg2[key2]||{};
                                    var cycleSec2 = (cfg2info.cycle_min&&cfg2info.cycle_min>0)?cfg2info.cycle_min*60:0;
                                    var remSec2 = (cycleSec2>0&&rtSec>0)?(cycleSec2-rtSec):null;
                                    var remAttr = remSec2!==null?remSec2:'';
                                    // คำนวณ ETA
                                    var etaAttr2 = '';
                                    if (remSec2!==null && remSec2>0) {
                                        var etaD2 = new Date(Date.now()+remSec2*1000);
                                        etaAttr2 = String(etaD2.getHours()).padStart(2,'0')+':'+String(etaD2.getMinutes()).padStart(2,'0');
                                    }
                                    // display value ตาม mode
                                    var displayVal;
                                    if (cardDisplayMode==='runtime') {
                                        displayVal = rtStr;
                                        if (remSec2!==null && remSec2<=0) resetDoneCounter(m);
                                    } else if (cardDisplayMode==='remain') {
                                        if (remSec2===null) { displayVal='<span style="color:rgba(255,255,255,0.5);">—</span>'; resetDoneCounter(m); }
                                        else if (remSec2<=0) {
                                            var dc2 = getDoneCount(m, cycleSec2);
                                            displayVal='<span style="color:#ff4d4d;font-weight:900;font-size:14px;">Soon!</span>';
                                        }
                                        else { displayVal='<span style="color:#a5f3c0;">'+FMT_HMS(remSec2)+'</span>'; resetDoneCounter(m); }
                                    } else if (cardDisplayMode==='eta') {
                                        var threshSec2=(parseFloat((document.getElementById('blink-threshold')||{}).value||30))*60;
                                        var etaColor2=(remSec2!==null&&remSec2>0&&remSec2<threshSec2)?'#ff6b35':'#a5f3c0';
                                        if (!etaAttr2) {
                                            if (remSec2!==null&&remSec2<=0) {
                                                var dc3 = getDoneCount(m, cycleSec2);
                                                displayVal='<span style="color:#ff4d4d;font-weight:900;font-size:14px;">Soon!</span>';
                                            } else {
                                                displayVal='<span style="color:rgba(255,255,255,0.5);">—</span>';
                                                resetDoneCounter(m);
                                            }
                                        } else {
                                            displayVal='<span style="color:'+etaColor2+';font-weight:900;">'+etaAttr2+'</span>';
                                            resetDoneCounter(m);
                                        }
                                    } else {
                                        displayVal = info.run_rate+'%';
                                        resetDoneCounter(m);
                                    }
                                    // --- MGR PLC: ดึงข้อมูลล่าสุดของเครื่องนี้ ---
                                    var mgrRec = mgrLatestByMachine[m] || mgrLatestByMachine[normMgrKey(m)] || null;
                                    var mgrLot = mgrRec ? (mgrRec.LOT_NUM      || '\u2014') : '\u2014';
                                    var mgrSt  = mgrRec ? fmtMgrTime(mgrRec.START_TIME) : '\u2014';
                                    var mgrOp  = mgrRec ? (mgrRec.OPERATOR_NO  || '\u2014') : '\u2014';
                                    var safeM  = m.replace(/['"]/g, '');

                                    mb+='<div class="card '+si.cardClass+'" data-type="'+si.type+'" data-part="'+safePart+'" data-runtime="'+rtSec+'" data-runrate="'+info.run_rate+'" data-remain="'+remAttr+'" data-eta="'+etaAttr2+'" data-machine="'+safeM+'" style="background:'+pc.bg+';'+gs+';cursor:pointer;" onclick="openMgrModal(this.dataset.machine)">'
                                        +'<div class="top-row"><h2 class="machine-name" style="color:#fff;text-shadow:0 1px 3px rgba(0,0,0,0.5);">'+m+'</h2><div class="run-rate '+si.textClass+'">'+displayVal+'</div></div>'
                                        +'<div class="part-no" style="color:rgba(255,255,255,0.85);">'+(info.part_no||'---')+'</div>'
                                        +'<div style="display:flex;justify-content:space-between;align-items:baseline;margin:2px 0;gap:3px;">'
                                            +'<div style="color:rgba(255,255,200,0.9);font-size:11px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;">&#128230; '+mgrLot+'</div>'
                                            +'<div style="color:rgba(255,255,255,0.75);font-size:10px;white-space:nowrap;flex-shrink:0;">&#128100; '+mgrOp+'</div>'
                                        +'</div>'
                                        +'<div style="display:flex;justify-content:space-between;align-items:flex-end;gap:4px;">'
                                            +'<div class="user-info" style="color:rgba(200,255,200,0.85);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;">&#128336; '+mgrSt+'</div>'
                                            +'<div class="spindle-container">'+spH+'</div>'
                                        +'</div></div>';
                                });
                                document.getElementById('dashboard').innerHTML=mb;
                                // Auto-set grid columns/rows and apply spacers
                                (function() {
                                    var maxC = 9, maxR = 16;
                                    allM.forEach(function(m) {
                                        if (m.indexOf('__') === 0) return;
                                        var p = mapLayout[m];
                                        if (p) { if(p.c > maxC) maxC = p.c; if(p.r > maxR) maxR = p.r; }
                                    });
                                    var grid = document.getElementById('dashboard');
                                    if (grid) {
                                        grid.style.setProperty('--grid-cols', maxC);
                                        grid.style.setProperty('--grid-rows', maxR);
                                    }
                                    // Apply spacers every render cycle
                                    if ((mapLayout.__spacerRows && mapLayout.__spacerRows.length > 0) ||
                                        (mapLayout.__spacerCols && mapLayout.__spacerCols.length > 0)) {
                                        applySpacerToDashboard();
                                    }
                                })();
                                updatePartSelect(data);
                                applyMapFilter();
                                applyBlinkClass();
                                syncSbPartSelect(data);
                                if(fitScreenMode) setTimeout(applyScale, 30);
                            });

                        /* Section 3: List Table */
                        fetch('/api/pn_config')
                            .then(function(r){ return r.json(); })
                            .then(function(cfg3) {
                                listData=[];
                                Object.keys(data).forEach(function(m) {
                                    var info=data[m], si=getStatusInfo(info.status);
                                    var rtSec3 = info.sim_time||0;
                                    var key3 = extract7(info.part_no||'');
                                    var cfg3info = cfg3[key3]||{};
                                    var cycleSec3 = (cfg3info.cycle_min&&cfg3info.cycle_min>0) ? cfg3info.cycle_min*60 : 0;
                                    var remSec3 = (cycleSec3>0 && rtSec3>0) ? (cycleSec3 - rtSec3) : null;
                                    var etaStr3 = '';
                                    if (remSec3!==null && remSec3>0) {
                                        var etaMs = Date.now() + remSec3*1000;
                                        var etaD = new Date(etaMs);
                                        etaStr3 = String(etaD.getHours()).padStart(2,'0')+':'+String(etaD.getMinutes()).padStart(2,'0');
                                    }
                                    listData.push({
                                        name:m, type:si.type, label:si.label,
                                        part:info.part_no||'', user:info.user||'',
                                        rate:info.run_rate||'0.0',
                                        spindles:info.spindles||[],
                                        sim_time:rtSec3,
                                        remaining:remSec3,
                                        eta:etaStr3,
                                        update:info.last_update||''
                                    });
                                });
                                renderList();
                            })
                            .catch(function(){
                                listData=[];
                                Object.keys(data).forEach(function(m) {
                                    var info=data[m], si=getStatusInfo(info.status);
                                    listData.push({
                                        name:m, type:si.type, label:si.label,
                                        part:info.part_no||'', user:info.user||'',
                                        rate:info.run_rate||'0.0',
                                        spindles:info.spindles||[],
                                        sim_time:info.sim_time||0,
                                        remaining:null, eta:'',
                                        update:info.last_update||''
                                    });
                                });
                                renderList();
                            });
                    })
                    .catch(function(){});
            }

            /* init sort indicator */
            document.getElementById('th-status').classList.add('sort-asc');

            /* default filter: Stop + Alarm */
            ['stop','alarm'].forEach(function(t) {
                filterSet[t] = true;
                var btn = document.getElementById('fb-'+t);
                if (btn) btn.classList.add('active');
            });

            setInterval(updateDashboard, 5000);
            updateDashboard();
            // Auto-scale grid on startup without hiding sections (simple CSS scale)
            setTimeout(function() {
                var scaler = document.getElementById('grid-scaler');
                var wrap   = document.getElementById('grid-scale-wrap');
                if (!scaler || !wrap) return;
                var availW = window.innerWidth - 32;
                var naturalW = scaler.scrollWidth;
                if (naturalW > availW) {
                    var s = Math.max(0.4, availW / naturalW);
                    scaler.style.transform = 'scale(' + s + ')';
                    scaler.style.transformOrigin = 'top left';
                    wrap.style.height = Math.round(scaler.scrollHeight * s) + 'px';
                    wrap.style.width  = Math.round(naturalW * s) + 'px';
                    wrap.style.overflow = 'hidden';
                    _autoInitScale = s;
                }
            }, 900);
        </script>
    </body>
    </html>
    """

    return render_template_string(html_template)


# ================= Web Server Control =================
def run_web_server():
    try:
        # ใช้ Waitress รับโหลด และป้องกันคอขวด
        serve(app, host='0.0.0.0', port=WEB_PORT, threads=6)
    except OSError as e:
        # ดักจับพอร์ตชน (Zombie Process)
        err_root = tk.Tk()
        err_root.withdraw()
        err_root.attributes("-topmost", True)
        messagebox.showerror(
            "CRITICAL SYSTEM ERROR", 
            f"ไม่สามารถเปิดพอร์ต {WEB_PORT} ได้!\n\nสาเหตุ: มีโปรแกรมค้างอยู่ในระบบ (Zombie Process)\nไปที่ Task Manager แล้ว End Task โปรแกรมนี้ทิ้งก่อนรันใหม่",
            parent=err_root
        )
        logging.error(f"Port {WEB_PORT} in use. Server failed to start.")
        os._exit(1)
    except Exception as e:
        logging.error(f"Web Server Crash: {e}")
        os._exit(1)

# ================= GUI Control Panel =================
if __name__ == "__main__":
    init_db()
    build_photo_index()
    threading.Thread(target=data_collector, daemon=True).start()
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=mgr_plc_collector, daemon=True).start()

    root = tk.Tk()
    root.title("COMPEQ Dashboard V16")
    root.geometry("320x160")
    root.resizable(False, False)
    root.configure(bg="#f0f4f8")

    def on_closing():
        pwd = simpledialog.askstring("Security Check", "กรุณาใส่รหัสผ่านเพื่อปิดระบบ:", show='*', parent=root)
        if pwd == PASSWORD_TO_CLOSE:
            root.destroy()
            os._exit(0) # บังคับปิดทุก Thread เด็ดขาด
        elif pwd is not None:
            messagebox.showerror("Access Denied", "รหัสผ่านไม่ถูกต้อง!", parent=root)

    root.protocol("WM_DELETE_WINDOW", on_closing)

    tk.Label(root, text="🏭 COMPEQ Dashboard V16", font=("Helvetica", 14, "bold"), bg="#f0f4f8", fg="#1565c0").pack(pady=(15, 5))
    tk.Label(root, text=f"Service is running on Port: {WEB_PORT}", font=("Helvetica", 10), bg="#f0f4f8", fg="#546e7a").pack()
    tk.Label(root, text="(Running in Background)", font=("Helvetica", 9), bg="#f0f4f8", fg="#e08000").pack()

    stop_btn = tk.Button(root, text="Stop Server", command=on_closing, bg="#f44336", fg="white", font=("Helvetica", 10, "bold"), relief="flat", padx=20)
    stop_btn.pack(pady=15)

    root.mainloop()