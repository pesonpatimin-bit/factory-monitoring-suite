# 🏭 Factory Monitoring Suite
### Production monitoring and data collection system for PCB drilling operations — deployed in factory

A collection of tools built to give production engineers and foremen real-time and historical visibility into factory performance — without relying on manual data collection or walking the floor.

**All tools are deployed and actively used in production at COMPEQ Manufacturing Co., Ltd.**

---

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    FACTORY NETWORK                          │
│                                                             │
│  Machine Servers ──TCP──► [MC_Runrate_Collection]           │
│  (10.61.16.1:6370)         Saves to MC_runrate_history.db  │
│                                                             │
│  Factory WIP Web ──HTTP──► [MGR_WIP_Collection]             │
│  (home30.compeq.co.th)     Saves to MGR_WIP_history.db     │
│                                                             │
│       Both DBs ──────────► [WIP_Rate Dashboard]             │
│                             Analytics & charts :8501        │
│                                                             │
│  Machine Servers ──TCP──► [TL2 Live Dashboard]              │
│                             Real-time status  :8080         │
└─────────────────────────────────────────────────────────────┘
```

---

## Tools in This Suite

### 1. 📊 TL2 Live Dashboard (`dashboard/TL2DashboardV13_5.py`)
Real-time machine monitoring dashboard. Connects directly to factory servers via TCP socket and parses raw 304-byte binary packets to display live status across all drilling machines.

**Features:**
- Live machine status, spindle state, part number, operator
- Run rate (MOR) calculated from online/stop seconds
- Job completion ETA based on cycle time vs. elapsed time
- Card view and sortable table view
- Auto-refresh every 5 seconds
- Password-protected shutdown (Tkinter GUI)
- Runs on Waitress production server

---

### 2. 📈 WIP & Run Rate Analytics Dashboard (`dashboard/WIP_Rate_MC_V3_9_3.py`)
Historical analytics dashboard. Reads from both SQLite databases and renders interactive charts showing WIP trends and machine run rate history per shift.

**Features:**
- WIP history by part number and department
- Run rate history by machine, shift, and date
- Plotly-based interactive charts
- HTML cache with 55-second TTL (prevents DB overload)
- Runs on Waitress at port 8501

---

### 3. 🔄 Machine Run Rate Collector (`collectors/MC_Runrate_Collection_Master_V13.py`)
Background service that automatically records machine run rates at each shift change. Runs silently in the system tray.

**Schedule:**
- `07:55` — records **Night shift** run rates (previous day)
- `19:55` — records **Day shift** run rates (current day)

**Features:**
- TCP socket connection, same packet parsing as live dashboard
- Records all active machines per shift to SQLite
- Manual trigger via system tray right-click
- Heartbeat log every hour to confirm service is alive

---

### 4. 🌐 WIP Data Collector (`collectors/MGR_WIP_Collection_V4_7.py`)
Hourly background service that logs into the factory WIP web system and collects WIP and output counts for monitored part numbers. Runs silently in the system tray.

**Features:**
- NTLM authentication with dual-user failover
- Scrapes factory internal web API (not public)
- Reads part number config from `MGR_PN_Observer.config`
- Stores WIP history (current + previous process) in SQLite
- Scheduled hourly (runs at :00 of each hour)
- System tray icon with live status: last run, next run, user status

---

## Data Flow

```
Shift End (07:55 / 19:55)
    └──► MC_Runrate_Collection pulls TCP data
         └──► Saves to MC_runrate_history.db

Every Hour (:00)
    └──► MGR_WIP_Collection logs into factory web
         └──► Saves to MGR_WIP_history.db

On Request (browser)
    └──► WIP_Rate Dashboard reads both DBs
         └──► Renders charts and tables

Always (5s refresh)
    └──► TL2 Live Dashboard reads live TCP packets
         └──► Displays real-time machine status
```

---

## Database Schema

### `MC_runrate_history.db`
```sql
runrate_logs (
    id, timestamp, date, shift,
    machine_name, run_rate REAL
)
```

### `MGR_WIP_history.db`
```sql
WIP_History (
    id, record_time, pn, name, dept, process,
    wip_n, total_out, query_range,
    prev_wip, prev_out
)
```

---

## Configuration

All tools share a common config directory: `D:\CKA30_Database\`

| File | Purpose |
|---|---|
| `MGR_PN_Observer.config` | Part numbers to monitor — PN, name, dept, process, cycle time, stack count |
| `MGR_credentials.json` | Login credentials for WIP web system (user1 + user2 failover) |
| `MGR_WIP_history.db` | WIP history database |
| `MC_runrate_history.db` | Run rate history database |

**Network config** (edit top of each file):
```python
SERVER_IP   = "10.61.16.1"   # Factory machine server
SERVER_PORT = 6370
```

---

## Tech Stack

| Component | Technology |
|---|---|
| Live data | TCP Socket — raw binary packet parsing |
| Web scraping | requests, requests-ntlm, BeautifulSoup |
| Database | SQLite (via Python sqlite3) |
| Web server | Flask + Waitress |
| Charts | Plotly |
| Data processing | pandas |
| System tray | pystray, Pillow |
| GUI | Tkinter |
| Deployment | PyInstaller (.exe) |

---

## Why This Was Built

The factory had no centralized way to monitor production in real time. Foremen had to walk the floor or check each machine individually. WIP data was pulled manually from the web system. Run rate history existed nowhere.

These tools were built from scratch — by identifying the raw TCP protocol through observation, reverse-engineering the 304-byte packet structure, and automating data collection from the factory web system — to give the production team full visibility with zero manual effort.

---

## Status

✅ **All tools deployed and operational** — running in production daily at COMPEQ Manufacturing Co., Ltd., used by production engineers and foremen for floor monitoring, shift handover, and capacity planning.

---

## About

Built by **Peson Patimin** — Production Engineer at COMPEQ Manufacturing Co., Ltd.  
All tools designed, developed, and deployed independently.

🔗 [LinkedIn](https://linkedin.com/in/rispeson) | [GitHub](https://github.com/pesonpatimin-bit)
