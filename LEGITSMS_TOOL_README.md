# LegitSMS Tool (Desktop)

## What this tool does
- Enter your `api_key`
- Buy a number (`getNumber`)
- Automatically refresh SMS status every 3 seconds (`getStatus`)
- Cancel order (`setStatus=8`) or mark complete (`setStatus=6`)

## Run locally (Python)
```bash
python legit_sms_tool.py
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
- Service: `wa`
- Country: `187`

Change service/country based on your account/API availability.

