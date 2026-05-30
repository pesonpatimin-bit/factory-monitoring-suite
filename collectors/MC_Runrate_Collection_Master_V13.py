import socket
import struct
import sqlite3
import os
import time
import threading
from datetime import datetime, timedelta
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item

# ================= CONFIGURATION =================
SERVER_IP = "10.61.16.1"
SERVER_PORT = 6370
DB_FOLDER = r"D:\CKA30_Database"
DB_NAME = os.path.join(DB_FOLDER, "MC_runrate_history.db")
# อัปเดตชื่อไฟล์ LOG ใหม่ตามที่บี๋ต้องการ
LOG_FILE = os.path.join(DB_FOLDER, "LOG_MC_Runrate_Collection_Master.txt") 
# =================================================

running = True

def write_log(message):
    """ฟังก์ชันบันทึกเหตุการณ์ลงไฟล์ text"""
    try:
        if not os.path.exists(DB_FOLDER):
            os.makedirs(DB_FOLDER)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{now}] {message}\n")
    except:
        pass

def collect_and_save():
    now = datetime.now()
    current_time_str = now.strftime("%H:%M")
    
    db_path = DB_NAME if os.path.exists(DB_FOLDER) else "MC_runrate_history.db"

    target_shift = "Manual"      
    target_date = now.strftime("%Y-%m-%d")
    
    if "07:30" <= current_time_str <= "08:30":
        target_shift = "Night"
        target_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    elif "19:30" <= current_time_str <= "20:30":
        target_shift = "Day"
        target_date = now.strftime("%Y-%m-%d")

    write_log(f"Starting Job: Shift={target_shift}, WorkDate={target_date}")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS runrate_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            timestamp DATETIME, date TEXT, shift TEXT, 
            machine_name TEXT, run_rate REAL)''')
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(8.0) 
            s.connect((SERVER_IP, SERVER_PORT))
            
            found_machines = []
            for i in range(500): 
                try:
                    s.sendall(b'\xaa')
                    data = b""
                    while len(data) < 304:
                        chunk = s.recv(304 - len(data))
                        if not chunk: break
                        data += chunk
                    
                    if len(data) == 304:
                        name = data[0:20].split(b'\x00')[0].decode('ascii', errors='ignore').strip()
                        if not name or name == "DECODER": continue
                        if name in found_machines: break 

                        online = struct.unpack('<I', data[296:300])[0]
                        stop = struct.unpack('<I', data[300:304])[0]
                        rate = round((1 - (stop / online)) * 100, 2) if online > 0 else 0

                        cursor.execute('''INSERT INTO runrate_logs (timestamp, date, shift, machine_name, run_rate) 
                                        VALUES (?, ?, ?, ?, ?)''', 
                                        (now.strftime("%Y-%m-%d %H:%M:%S"), target_date, target_shift, name, rate))
                        found_machines.append(name)
                        for _ in range(2):
                            s.sendall(b'\xaa'); s.recv(4)
                except Exception:
                    continue
            
            conn.commit()
            write_log(f"Success: Recorded {len(found_machines)} machines.")
    except Exception as e:
        write_log(f"Error during collection: {str(e)}")
    finally:
        conn.close()

def run_logic(icon):
    global running
    last_triggered_key = "" 
    
    write_log("Service Started: Monitoring 07:55 and 19:55")

    while running:
        now = datetime.now()
        current_hm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        if ("07:55" <= current_hm <= "07:57") or ("19:55" <= current_hm <= "19:57"):
            trigger_key = f"{today}_{'Morning' if current_hm.startswith('07') else 'Evening'}"
            if last_triggered_key != trigger_key:
                collect_and_save()
                last_triggered_key = trigger_key
        
        if now.minute == 0 and now.second < 15:
            write_log("Heartbeat: Service is alive.")
            time.sleep(15)

        time.sleep(15)

def create_image():
    # สร้าง Icon สีฟ้า
    width, height = 64, 64
    image = Image.new('RGB', (width, height), "blue")
    dc = ImageDraw.Draw(image)
    dc.ellipse((width // 4, height // 4, width * 3 // 4, height * 3 // 4), fill="white")
    return image

def on_quit(icon, item):
    global running
    write_log("Service Stopped by User via Tray Icon.")
    running = False
    icon.stop()

def setup_tray():
    # เมนูคลิกขวา: บันทึกทันที และ ปิดโปรแกรม
    menu = (item('Manual Record Now', lambda: threading.Thread(target=collect_and_save).start()),
            item('Quit', on_quit),)
    icon = pystray.Icon("MC_Collector", create_image(), "MC Runrate Collector", menu)
    
    thread = threading.Thread(target=run_logic, args=(icon,))
    thread.daemon = True
    thread.start()
    icon.run()

if __name__ == "__main__":
    setup_tray()