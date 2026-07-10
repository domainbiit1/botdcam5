#!/usr/bin/env python3
"""LegitSMS desktop helper with auto refresh + dynamic service list."""

import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

BASE_URL = "https://api.legitsms.com/api/handler/"
POLL_INTERVAL_MS = 3000


class LegitSMSApi:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    def _request(self, **params):
        q = {"api_key": self.api_key, **params}
        url = f"{BASE_URL}?{urlencode(q)}"
        req = Request(url, method="GET")
        try:
            with urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="replace").strip()
        except HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                err_body = str(exc)
            return f"HTTP_ERROR:{exc.code}:{err_body}"
        except URLError as exc:
            return f"NETWORK_ERROR:{exc}"

    def get_services(self, server: str, country: str = ""):
        params = {"action": "getServices", "server": server.strip()}
        if server.strip() == "3":
            params["country"] = country.strip()
        return self._request(**params)

    def get_number(self, server: str, service: str, country: str, max_price: str = "", operator: str = ""):
        params = {
            "action": "getNumber",
            "server": server.strip(),
            "service": service.strip(),
            "country": country.strip(),
        }
        if max_price.strip():
            params["max_price"] = max_price.strip()
        if operator.strip():
            params["operator"] = operator.strip()
        return self._request(**params)

    def get_status(self, order_id: str):
        return self._request(action="getStatus", id=order_id.strip())

    def set_status(self, order_id: str, status_code: str):
        return self._request(action="setStatus", id=order_id.strip(), status=status_code.strip())


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LegitSMS Tool")
        self.geometry("980x700")
        self.minsize(940, 640)

        self.api = None
        self.status_in_flight = False
        self.order_id_var = tk.StringVar()
        self.phone_var = tk.StringVar(value="-")
        self.last_status_var = tk.StringVar(value="-")
        self.last_code_var = tk.StringVar(value="-")
        self.service_map = {}
        self.polling_enabled = True

        self._build_ui()
        self._poll_tick()

    def _build_ui(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        ttk.Label(top, text="API Key").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.api_key_entry = ttk.Entry(top, width=62, show="*")
        self.api_key_entry.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="Set API Key", command=self.set_api_key).grid(row=0, column=2, padx=8, pady=4)

        ttk.Label(top, text="Server").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.server_combo = ttk.Combobox(top, values=["1", "2", "3"], state="readonly", width=8)
        self.server_combo.set("1")
        self.server_combo.grid(row=1, column=1, sticky="w", pady=4)
        self.server_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_services())

        ttk.Label(top, text="Service").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.service_combo = ttk.Combobox(top, width=46)
        self.service_combo.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Button(top, text="Refresh Services", command=self.refresh_services).grid(row=2, column=2, padx=8, pady=4)

        ttk.Label(top, text="Country").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.country_entry = ttk.Entry(top, width=25)
        self.country_entry.insert(0, "187")
        self.country_entry.grid(row=3, column=1, sticky="w", pady=4)
        self.country_entry.bind("<Return>", lambda _e: self.refresh_services())
        self.country_entry.bind("<FocusOut>", lambda _e: self.refresh_services(server3_only=True))

        ttk.Label(top, text="Max Price (optional)").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        self.max_price_entry = ttk.Entry(top, width=25)
        self.max_price_entry.grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(top, text="Operator (optional)").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        self.operator_entry = ttk.Entry(top, width=25)
        self.operator_entry.grid(row=5, column=1, sticky="w", pady=4)

        top.columnconfigure(1, weight=1)

        actions = ttk.Frame(self, padding=(12, 0, 12, 8))
        actions.pack(fill="x")
        ttk.Button(actions, text="Get Number", command=self.get_number).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Check SMS Now", command=lambda: self.check_status_once(show_warnings=True)).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Cancel Order (status=8)", command=self.cancel_order).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Complete Order (status=6)", command=self.complete_order).pack(side="left", padx=(0, 8))

        status_box = ttk.LabelFrame(self, text="Current Order", padding=12)
        status_box.pack(fill="x", padx=12, pady=8)
        ttk.Label(status_box, text="Order ID").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        self.order_id_entry = ttk.Entry(status_box, textvariable=self.order_id_var, width=24)
        self.order_id_entry.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(status_box, text="Phone").grid(row=0, column=2, sticky="w", padx=(20, 8), pady=2)
        ttk.Label(status_box, textvariable=self.phone_var).grid(row=0, column=3, sticky="w", pady=2)
        ttk.Label(status_box, text="Last Status").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Label(status_box, textvariable=self.last_status_var).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(status_box, text="Last SMS Code").grid(row=1, column=2, sticky="w", padx=(20, 8), pady=2)
        ttk.Label(status_box, textvariable=self.last_code_var).grid(row=1, column=3, sticky="w", pady=2)

        log_box = ttk.LabelFrame(self, text="Logs", padding=8)
        log_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_text = tk.Text(log_box, height=22, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        self.log("Ready. Set API key. SMS auto-refresh runs every 3 seconds.")

    def log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_api_key(self):
        key = self.api_key_entry.get().strip()
        if not key:
            messagebox.showwarning("Missing API key", "Please enter API key.")
            return
        self.api = LegitSMSApi(key)
        self.log("API key set.")
        self.refresh_services()

    def _ensure_api(self, show_warning=True):
        if self.api is None:
            if show_warning:
                messagebox.showwarning("API key missing", "Please set API key first.")
            return False
        return True

    def _run_bg(self, fn, on_done):
        def worker():
            result = fn()
            self.after(0, lambda: on_done(result))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _parse_code(status_text: str):
        if status_text.startswith("STATUS_OK:"):
            return status_text.split(":", 1)[1].strip()
        return None

    def _parse_json(self, text: str):
        try:
            return json.loads(text)
        except Exception:
            return None

    def refresh_services(self, server3_only=False):
        if not self._ensure_api(show_warning=False):
            return
        server = self.server_combo.get().strip()
        if server3_only and server != "3":
            return
        country = self.country_entry.get().strip()
        if server == "3" and not country:
            return

        self.log(f"Loading services for server={server} ...")

        def task():
            return self.api.get_services(server=server, country=country)

        def done(resp):
            data = self._parse_json(resp)
            if data is None:
                self.log(f"getServices raw: {resp}")
                return
            self.service_map = {}
            values = []
            if server == "1":
                services = data.get("services", []) if isinstance(data, dict) else []
                for item in services:
                    code = str(item.get("code", "")).strip()
                    name = str(item.get("name", "")).strip()
                    if code:
                        label = f"{code} | {name}" if name else code
                        self.service_map[label] = code
                        values.append(label)
            elif server == "2":
                arr = data if isinstance(data, list) else []
                for item in arr:
                    sid = str(item.get("ID", "")).strip()
                    name = str(item.get("name", "")).strip()
                    if sid:
                        label = f"{sid} | {name}" if name else sid
                        self.service_map[label] = sid
                        values.append(label)
            else:
                if isinstance(data, dict):
                    for key in data.keys():
                        code = str(key).strip()
                        if code:
                            self.service_map[code] = code
                            values.append(code)
            values = sorted(values, key=lambda x: x.lower())
            self.service_combo["values"] = values
            if values:
                self.service_combo.set(values[0])
                self.log(f"Loaded {len(values)} services.")
            else:
                self.log("No services found for current server/country.")

        self._run_bg(task, done)

    def _selected_service_code(self):
        raw = self.service_combo.get().strip()
        if not raw:
            return ""
        if raw in self.service_map:
            return self.service_map[raw]
        return raw.split("|", 1)[0].strip()

    def get_number(self):
        if not self._ensure_api():
            return
        server = self.server_combo.get().strip()
        service = self._selected_service_code()
        country = self.country_entry.get().strip()
        max_price = self.max_price_entry.get().strip()
        operator = self.operator_entry.get().strip()
        if not service or not country:
            messagebox.showwarning("Missing params", "Service and country are required.")
            return
        self.log(f"Requesting number: server={server} service={service} country={country}")

        def task():
            return self.api.get_number(server, service, country, max_price=max_price, operator=operator)

        def done(resp):
            self.log(f"getNumber response: {resp}")
            if resp.startswith("ACCESS_NUMBER:"):
                parts = resp.split(":")
                if len(parts) >= 3:
                    self.order_id_var.set(parts[1].strip())
                    self.phone_var.set(parts[2].strip())
                    self.last_status_var.set("NEW_ORDER")
                    self.last_code_var.set("-")
            elif resp.startswith("HTTP_ERROR:429"):
                self.log("Rate limited (429). API limit is 1 request per 2 seconds.")

        self._run_bg(task, done)

    def check_status_once(self, show_warnings=False):
        if not self._ensure_api(show_warning=show_warnings):
            return
        order_id = self.order_id_var.get().strip()
        if not order_id:
            if show_warnings:
                messagebox.showwarning("Order missing", "Please enter or buy an order first.")
            return
        if self.status_in_flight:
            return
        self.status_in_flight = True

        def task():
            return self.api.get_status(order_id)

        def done(resp):
            self.status_in_flight = False
            self.last_status_var.set(resp)
            code = self._parse_code(resp)
            if code:
                self.last_code_var.set(code)
            self.log(f"getStatus({order_id}) => {resp}")
            if resp.startswith("HTTP_ERROR:429"):
                self.log("Rate limited (429). Keep polling interval >= 2 seconds.")

        self._run_bg(task, done)

    def _poll_tick(self):
        if self.polling_enabled:
            self.check_status_once(show_warnings=False)
        self.after(POLL_INTERVAL_MS, self._poll_tick)

    def _set_status(self, status_code: str):
        if not self._ensure_api():
            return
        order_id = self.order_id_var.get().strip()
        if not order_id:
            messagebox.showwarning("Order missing", "Please enter order id.")
            return

        def task():
            return self.api.set_status(order_id, status_code)

        def done(resp):
            self.log(f"setStatus({status_code}) => {resp}")

        self._run_bg(task, done)

    def cancel_order(self):
        self._set_status("8")

    def complete_order(self):
        self._set_status("6")


if __name__ == "__main__":
    app = App()
    app.mainloop()

