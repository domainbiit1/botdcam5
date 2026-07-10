# LegitSMS Tool (PyQt6 Hacker UI)

## Overview
Desktop tool for LegitSMS API with a hacker-style PyQt6 interface:
- Connect API key
- Auto-load services by server/country
- Rent phone number
- Auto-refresh SMS status every 3 seconds
- Stop refresh for a phone automatically after code is received
- Left-click phone/code cells to copy instantly

## Main UX behavior
- **Phone copy rule**: if phone format is `1xxxxxxxxxx` (11 digits, starts with `1`), copied value removes the first `1`.
- **Code copy**: clicking CODE column copies code to clipboard.
- **Auto-stop refresh**: when status is `STATUS_OK:<code>`, that order is excluded from future polling.
- **Advanced fields** (`Max Price`, `Operator`) are hidden by default.

## Run
```bash
python3 legitsms_tool.py
```

## Dependency
Install PyQt6 first:
```bash
pip install PyQt6
```

## Build `.exe` (Windows)
Use existing batch script:
```bat
build_legitsms_exe.bat
```

## API notes
- Base URL: `https://api.legitsms.com/api/handler/?`
- Rate limit from docs: 1 request / 2 seconds
- This app polls every 3 seconds

