# LegitSMS Tool (Desktop)

## What this tool does
- Enter your `api_key`
- Auto-load service list based on selected server (`getServices`)
- Professional 2-panel layout (left: services, right: rented numbers)
- Press `Enter` on API key to connect instantly
- Auto-reload services when server/country changes
- Cleaner workflow: primary action is only **Rent Selected Service**
- Right-click order row for quick **Cancel / Complete**
- Optional advanced fields (Max Price / Operator) are hidden by default
- Buy a number (`getNumber`)
- Automatically refresh SMS status every 3 seconds (`getStatus`) without pressing Start
- Cancel order (`setStatus=8`) or mark complete (`setStatus=6`)

## Run locally (Python)
```bash
python3 legitsms_tool.py
```

## Build `.exe` (Windows)
On Windows CMD in this folder:
```bat
build_legitsms_exe.bat
```

Output:
`dist\LegitSMS-Tool.exe`

## Important API notes from docs
- Base URL: `https://api.legitsms.com/api/handler/?`
- Rate limit: **1 request per 2 seconds**
- This app polls every **3 seconds** (safe with limit)

## Quick defaults in UI
- Server: `1`
- Country: `187`

Select service from the left panel, then click **Rent Selected Service** (or double-click a service row).

