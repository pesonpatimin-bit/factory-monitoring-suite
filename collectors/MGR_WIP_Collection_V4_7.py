import os
import requests
import sqlite3
import time
import datetime
import json
import logging
from bs4 import BeautifulSoup
from requests_ntlm import HttpNtlmAuth
from threading import Thread
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw

# ==========================================
# [ตั้งค่าตัวแปรระบบพื้นฐาน]
# ==========================================
LOGIN_URL = "http://****************/JQWebClient/LogOn.aspx"
API_URL = "http://****************/JQWebClient/handler/JQDataHandle.ashx?RemoteName=SMGR_OUT_WipOut"

DB_DIR = r"D:\CKA30_Database"
DB_PATH = os.path.join(DB_DIR, "MGR_WIP_history.db")
CREDS_FILE = os.path.join(DB_DIR, "MGR_credentials.json") 
CONFIG_FILE = os.path.join(DB_DIR, "MGR_PN_Observer.config")

# กลับมาใช้ชื่อไฟล์ Log เดิมที่แยกชัดเจน
LOG_FILE = os.path.join(DB_DIR, "Log_MGR_Automation_WIP.txt")

status_user1 = "Unknown"
status_user2 = "Unknown"
last_run = "Never"
next_run = "Calculating..."

# ==========================================
# [ระบบบันทึก Log แบบปลอดภัยสำหรับ No-Console]
# ==========================================
# สร้างโฟลเดอร์ให้ชัวร์ก่อนเขียน Log ป้องกัน Error
if not os.path.exists(DB_DIR): os.makedirs(DB_DIR)

# ตั้งค่าให้เขียนลงไฟล์โดยตรง (รองรับภาษาไทยและ Emoji)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, 'a', 'utf-8')]
)

def log_message(msg):
    logging.info(msg)

# ==========================================
# [Database Setup]
# ==========================================
def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS WIP_History (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_time TEXT, pn TEXT, name TEXT, dept TEXT,
            process TEXT, wip_n INTEGER, total_out INTEGER, query_range TEXT
        )
    ''')
    try:
        cursor.execute("ALTER TABLE WIP_History ADD COLUMN prev_wip INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE WIP_History ADD COLUMN prev_out INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

# ==========================================
# [ฟังก์ชันล็อกอินแบบปลอมตัว]
# ==========================================
def attempt_login(username, password):
    session = requests.Session()
    session.auth = HttpNtlmAuth(username, password)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "th-TH,th;q=0.9,en-TH;q=0.8,en;q=0.7"
    }
    
    try:
        resp = session.get(LOGIN_URL, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None, None, f"HTTP Error ({resp.status_code})"

        soup = BeautifulSoup(resp.text, 'html.parser')
        viewstate_tag = soup.find('input', id='__VIEWSTATE')
        eventval_elem = soup.find('input', id='__EVENTVALIDATION')
        viewgen_elem = soup.find('input', id='__VIEWSTATEGENERATOR')
        
        if not viewstate_tag:
            return None, None, "Page Error: Cannot find __VIEWSTATE tag"
            
        login_payload = {
            "__EVENTTARGET": "", "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate_tag['value'], 
            "__VIEWSTATEGENERATOR": viewgen_elem['value'] if viewgen_elem else "", 
            "__EVENTVALIDATION": eventval_elem['value'] if eventval_elem else "",
            "Login1$UserName": username, "Login1$Password": password, 
            "Login1$Database": "WIP", "Login1$Solution": "SL_MGR", "Login1$LoginButton": "Login"
        }
        
        login_resp = session.post(LOGIN_URL, data=login_payload, headers=headers, allow_redirects=False, timeout=15)
        
        if login_resp.status_code in [200, 302]: 
            return session, headers, "Success"
        return None, None, f"Fail({login_resp.status_code})"
        
    except Exception as e:
        return None, None, f"Error: {str(e)}"

# ==========================================
# [ฟังก์ชันดึงข้อมูลและบันทึก]
# ==========================================
def job_process():
    global status_user1, status_user2, last_run
    now = datetime.datetime.now()
    last_run = now.strftime("%Y-%m-%d %H:%M:%S")
    log_message(f"--- [RUN] Started at {now.strftime('%H:%M:%S')} ---")
    
    if not os.path.exists(CREDS_FILE) or not os.path.exists(CONFIG_FILE):
        log_message("[Error] Credentials or Config file missing.")
        return

    with open(CREDS_FILE, 'r', encoding='utf-8') as f:
        creds = json.load(f)

    u1, p1 = creds["user1"]["username"], creds["user1"]["password"]
    session, headers, msg1 = attempt_login(u1, p1)
    status_user1 = "Online" if session else f"Offline ({msg1})"
    
    if not session:
        log_message(f"User 1 Failed: {msg1}. Switching to User 2...")
        u2, p2 = creds["user2"]["username"], creds["user2"]["password"]
        session, headers, msg2 = attempt_login(u2, p2)
        status_user2 = "Online" if session else f"Offline ({msg2})"
    else:
        status_user2 = "Standby"

    if not session:
        log_message("[Critical] Both users failed to login. No data collected.")
        return

    yesterday = now - datetime.timedelta(days=1)
    start_date = f"{yesterday.strftime('%Y%m%d')} 2300"
    end_date = f"{now.strftime('%Y%m%d')} 2300"
    query_range = f"{start_date} to {end_date}"

    tasks = []
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            line = line.strip()
            if i == 0 or not line or line.startswith('('): continue
            cols = line.split(',')
            if len(cols) >= 7 and cols[0].strip().upper() == 'Y':
                tasks.append([c.strip() for c in cols])

    if not tasks:
        log_message("[Warning] No active tasks in Config file.")
        return

    api_headers = headers.copy()
    api_headers["X-Requested-With"] = "XMLHttpRequest"
    api_headers["Referer"] = "http://***************/JQWebClient/SL_MGR_OUT/WMGR_OUT_WipOutSum.aspx?undefined"

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for task in tasks:
        active_obs, pn, name, obs_dep, obs_process, prev_dep, prev_process = task[0:7]
        
        first_letter_dep = obs_dep[0] if obs_dep else "A"
        first_letter_prev_dep = prev_dep[0] if prev_dep else "A"
        
        pn_clean = pn.strip().upper()
        is_all_query = pn_clean.startswith("ALL") or pn_clean == ""
        
        api_pn_param = "" if is_all_query else pn.strip()
        display_pn = pn.strip() if pn.strip() else "ALL"

        payload_str_curr = f"OutSum|CK|CK|{start_date}|{end_date}|PNL|{obs_dep}|{obs_process}|||{api_pn_param}|||{first_letter_dep}"
        api_payload_curr = {"mode": "method", "method": "ExecProc", "parameters": payload_str_curr}

        curr_wip_n, total_out, prev_wip_n, prev_out_n = 0, 0, 0, 0
        status_label = "Zero"

        try:
            data_resp_curr = session.post(API_URL, data=api_payload_curr, headers=api_headers, timeout=15)
            if data_resp_curr.status_code == 200:
                json_data_curr = data_resp_curr.json()
                if isinstance(json_data_curr, list) and len(json_data_curr) > 0:
                    curr_wip_n = int(float(json_data_curr[0].get("SF_WIP_N", 0.0)))
                    total_out = int(float(json_data_curr[0].get("SF_TOTAL", 0.0)))
                    status_label = "Found"

            if not is_all_query:
                payload_str_prev = f"OutSum|CK|CK|{start_date}|{end_date}|PNL|{prev_dep}|{prev_process}|||{api_pn_param}|||{first_letter_prev_dep}"
                api_payload_prev = {"mode": "method", "method": "ExecProc", "parameters": payload_str_prev}
                
                data_resp_prev = session.post(API_URL, data=api_payload_prev, headers=api_headers, timeout=15)
                if data_resp_prev.status_code == 200:
                    json_data_prev = data_resp_prev.json()
                    if isinstance(json_data_prev, list) and len(json_data_prev) > 0:
                        prev_wip_n  = int(float(json_data_prev[0].get("SF_WIP_N", 0.0)))
                        prev_out_n  = int(float(json_data_prev[0].get("SF_TOTAL", 0.0)))
            
            cursor.execute('''
                INSERT INTO WIP_History (record_time, pn, name, dept, process, wip_n, total_out, query_range, prev_wip, prev_out)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (last_run, display_pn, name, obs_dep, obs_process, curr_wip_n, total_out, query_range, prev_wip_n, prev_out_n))
            
            if is_all_query:
                log_message(f"[{status_label}] PN: {display_pn:<10} | ALL WIP: {curr_wip_n:<5} | Total Out: {total_out:<5} (Prev: Skipped)")
            else:
                log_message(f"[{status_label}] PN: {display_pn:<10} | Prev WIP: {prev_wip_n:<5} | Prev Out: {prev_out_n:<5} | Curr WIP: {curr_wip_n:<5} | Total Out: {total_out:<5}")

        except Exception as e:
            log_message(f"[Error] PN: {display_pn:<10} | Failed: {e}")
            
    conn.commit()
    conn.close()
    log_message("Data successfully collected and saved.")

# ==========================================
# [ระบบ System Tray]
# ==========================================
def create_image():
    image = Image.new('RGB', (64, 64), color=(255, 255, 255))
    d = ImageDraw.Draw(image)
    d.ellipse((10, 10, 54, 54), fill=(0, 150, 136))
    return image

def quit_window(icon, item):
    icon.stop()
    os._exit(0)

def tray_icon_thread():
    icon = pystray.Icon("MGR_Auto", create_image(), "MGR WIP Master")
    def update_menu(icon):
        while icon.visible:
            icon.menu = pystray.Menu(
                item(f'User 1: {status_user1}', lambda: None, enabled=False),
                item(f'User 2: {status_user2}', lambda: None, enabled=False),
                item(f'Last Run: {last_run}', lambda: None, enabled=False),
                item(f'Next Run: {next_run}', lambda: None, enabled=False),
                item('---', lambda: None, enabled=False),
                item('Open Log File', lambda: os.startfile(LOG_FILE)),
                item('Exit', quit_window)
            )
            time.sleep(5)
    Thread(target=update_menu, args=(icon,), daemon=True).start()
    icon.run()

# ==========================================
# [Main Scheduler Loop]
# ==========================================
if __name__ == "__main__":
    setup_database()
    log_message("🚀 MGR Automation WIP V4.6 Started (Hourly Mode)")
    
    Thread(target=tray_icon_thread, daemon=True).start()
    
    while True:
        try:
            job_process()
        except Exception as e:
            log_message(f"Runtime Error: {e}")

        now = datetime.datetime.now()
        target_time = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait_seconds = (target_time - now).total_seconds()
        next_run = target_time.strftime("%H:00")
        
        log_message(f"Waiting {int(wait_seconds/60)} mins until {next_run}...")
        
        try:
            time.sleep(wait_seconds)
        except KeyboardInterrupt:
            break
