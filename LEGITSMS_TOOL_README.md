# LegitSMS Tool (PyQt6 Hacker UI)

## Overview
Desktop tool for LegitSMS API with a hacker-style PyQt6 interface:
- Connect API key
- Auto-load services by server/country
- Rent phone number
- Auto-refresh SMS status every 10 seconds
- Stop refresh for a phone automatically after code is received
- Left-click phone/code cells to copy instantly

## Main UX behavior
- **Phone copy rule**: if phone format is `1xxxxxxxxxx` (11 digits, starts with `1`), copied value removes the first `1`.
- **Code copy**: clicking CODE column copies code to clipboard.
- **Auto-stop refresh**: when status is `STATUS_OK:<code>`, that order is excluded from future polling.
- **Request optimization**: tool does not prefetch all service prices. `cost` is loaded only for phones that were actually rented.
- **Refund lock**: Cancel/Refund is locked for 2 minutes after rent. Countdown is shown in STATUS.
- **Refund ready**: if code is still missing after 2 minutes, STATUS shows `REFUND READY` and Refund button/menu becomes clickable.
- **Multi-phone independent timers**: each order has its own 2-minute countdown and refund state.
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
- This app polls every 10 seconds

