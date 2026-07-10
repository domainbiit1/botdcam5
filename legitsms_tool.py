#!/usr/bin/env python3
"""LegitSMS Tool - professional two-panel UI."""

import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.legitsms.com/api/handler/"
POLL_INTERVAL_MS = 3000


class LegitSMSApi:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    def _request(self, **params):
        query = {"api_key": self.api_key, **params}
        url = f"{BASE_URL}?{urlencode(query)}"
        req = Request(url, method="GET")
        try:
            with urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="replace").strip()
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                body = str(exc)
            return f"HTTP_ERROR:{exc.code}:{body}"
        except URLError as exc:
            return f"NETWORK_ERROR:{exc}"

    def get_services(self, server: str, country: str = ""):
        params = {"action": "getServices", "server": server.strip()}
        if server.strip() == "3":
            params["country"] = country.strip()
        return self._request(**params)

    def get_price(self, server: str, service: str, country: str):
        return self._request(
            action="getPrices",
            server=server.strip(),
            service=service.strip(),
            country=country.strip(),
        )

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
        self.geometry("1180x760")
        self.minsize(1080, 700)

        self.api = None
        self.status_in_flight = False
        self.selected_service_code = ""
        self.filtered_services = []
        self.all_services = []
        self.orders = {}  # order_id -> dict
        self.polling_enabled = True

        self._init_style()
        self._build_ui()
        self._poll_tick()

    def _init_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#f5f7fb")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Header.TLabel", background="#ffffff", font=("Segoe UI", 18, "bold"), foreground="#0f172a")
        style.configure("Sub.TLabel", background="#ffffff", font=("Segoe UI", 10), foreground="#475569")
        style.configure("Small.TLabel", background="#ffffff", font=("Segoe UI", 9), foreground="#64748b")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        self.configure(bg="#f5f7fb")

    def _build_ui(self):
        root = ttk.Frame(self, style="App.TFrame", padding=12)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root, style="Card.TFrame", padding=(12, 10))
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="API Key", style="Small.TLabel").pack(side="left", padx=(0, 8))
        self.api_key_entry = ttk.Entry(top, width=70, show="*")
        self.api_key_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Set API Key", command=self.set_api_key, style="Accent.TButton").pack(side="left", padx=(8, 0))

        body = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, style="Card.TFrame", padding=12)
        right = ttk.Frame(body, style="Card.TFrame", padding=12)
        body.add(left, weight=1)
        body.add(right, weight=2)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent):
        ttk.Label(parent, text="Phone Verifications", style="Header.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Rent a phone and auto-check incoming SMS code.", style="Sub.TLabel").pack(anchor="w", pady=(2, 12))

        row0 = ttk.Frame(parent, style="Card.TFrame")
        row0.pack(fill="x", pady=(0, 8))
        ttk.Label(row0, text="Server", style="Small.TLabel").pack(side="left")
        self.server_combo = ttk.Combobox(row0, values=["1", "2", "3"], state="readonly", width=6)
        self.server_combo.set("1")
        self.server_combo.pack(side="left", padx=(8, 16))
        self.server_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_services())
        ttk.Label(row0, text="Country", style="Small.TLabel").pack(side="left")
        self.country_entry = ttk.Entry(row0, width=12)
        self.country_entry.insert(0, "187")
        self.country_entry.pack(side="left", padx=(8, 0))
        self.country_entry.bind("<Return>", lambda _e: self.refresh_services())

        search_row = ttk.Frame(parent, style="Card.TFrame")
        search_row.pack(fill="x", pady=(0, 10))
        self.search_entry = ttk.Entry(search_row)
        self.search_entry.insert(0, "Search service...")
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<KeyRelease>", lambda _e: self.apply_service_filter())
        ttk.Button(search_row, text="Refresh", command=self.refresh_services).pack(side="left", padx=(8, 0))

        cols = ("service", "price")
        self.service_tree = ttk.Treeview(parent, columns=cols, show="headings", height=20)
        self.service_tree.heading("service", text="SERVICE")
        self.service_tree.heading("price", text="PRICE")
        self.service_tree.column("service", width=240, anchor="w")
        self.service_tree.column("price", width=80, anchor="e")
        self.service_tree.pack(fill="both", expand=True)
        self.service_tree.bind("<<TreeviewSelect>>", self.on_service_select)
        self.service_tree.bind("<Double-1>", lambda _e: self.rent_selected_service())

    def _build_right_panel(self, parent):
        top = ttk.Frame(parent, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Rented numbers", style="Header.TLabel").pack(side="left")
        self.counter_var = tk.StringVar(value="0 / 5")
        ttk.Label(top, textvariable=self.counter_var, style="Sub.TLabel").pack(side="right")

        controls = ttk.Frame(parent, style="Card.TFrame")
        controls.pack(fill="x", pady=(10, 8))
        ttk.Label(controls, text="Max Price", style="Small.TLabel").pack(side="left")
        self.max_price_entry = ttk.Entry(controls, width=10)
        self.max_price_entry.pack(side="left", padx=(6, 12))
        ttk.Label(controls, text="Operator", style="Small.TLabel").pack(side="left")
        self.operator_entry = ttk.Entry(controls, width=14)
        self.operator_entry.pack(side="left", padx=(6, 12))
        ttk.Button(controls, text="Rent Selected Service", command=self.rent_selected_service, style="Accent.TButton").pack(side="left")
        ttk.Button(controls, text="Cancel Selected", command=self.cancel_selected_order).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Complete Selected", command=self.complete_selected_order).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Check Now", command=self.check_all_statuses).pack(side="left", padx=(8, 0))

        cols = ("id", "service", "phone", "code", "cost", "status", "actions")
        self.orders_tree = ttk.Treeview(parent, columns=cols, show="headings", height=16)
        headings = {
            "id": "ID",
            "service": "SERVICE",
            "phone": "PHONE",
            "code": "CODE",
            "cost": "COST",
            "status": "STATUS",
            "actions": "ACTIONS",
        }
        widths = {"id": 90, "service": 180, "phone": 130, "code": 100, "cost": 90, "status": 200, "actions": 110}
        for c in cols:
            self.orders_tree.heading(c, text=headings[c])
            self.orders_tree.column(c, width=widths[c], anchor="w")
        self.orders_tree.pack(fill="both", expand=True)

        log_box = ttk.LabelFrame(parent, text="Logs", padding=8)
        log_box.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_box, height=8, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        self.log("Ready. Set API key to load services. SMS refresh is automatic every 3 seconds.")

    def log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _run_bg(self, fn, on_done):
        def worker():
            result = fn()
            self.after(0, lambda: on_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_api(self, warn=True):
        if self.api is None:
            if warn:
                messagebox.showwarning("API key missing", "Please set API key first.")
            return False
        return True

    @staticmethod
    def _parse_json(text: str):
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _extract_code(status_text: str):
        if isinstance(status_text, str) and status_text.startswith("STATUS_OK:"):
            return status_text.split(":", 1)[1].strip()
        return "-"

    def set_api_key(self):
        key = self.api_key_entry.get().strip()
        if not key:
            messagebox.showwarning("Missing API key", "Please enter API key.")
            return
        self.api = LegitSMSApi(key)
        self.log("API key set.")
        self.refresh_services()

    def refresh_services(self):
        if not self._ensure_api(warn=False):
            return
        server = self.server_combo.get().strip()
        country = self.country_entry.get().strip()
        if server == "3" and not country:
            self.log("Server 3 requires country.")
            return
        self.log(f"Loading services for server={server} ...")

        def task():
            return self.api.get_services(server=server, country=country)

        def done(resp):
            data = self._parse_json(resp)
            if data is None:
                self.log(f"getServices raw: {resp}")
                return
            items = []
            if server == "1":
                for x in data.get("services", []) if isinstance(data, dict) else []:
                    code = str(x.get("code", "")).strip()
                    name = str(x.get("name", "")).strip()
                    if code:
                        items.append({"label": f"{name} ({code})" if name else code, "code": code, "price": "-"})
            elif server == "2":
                for x in data if isinstance(data, list) else []:
                    sid = str(x.get("ID", "")).strip()
                    name = str(x.get("name", "")).strip()
                    if sid:
                        items.append({"label": f"{name} ({sid})" if name else sid, "code": sid, "price": "-"})
            else:
                for key in data.keys() if isinstance(data, dict) else []:
                    items.append({"label": str(key), "code": str(key), "price": "-"})

            self.all_services = sorted(items, key=lambda z: z["label"].lower())
            self.apply_service_filter()
            self.log(f"Loaded {len(self.all_services)} services.")

        self._run_bg(task, done)

    def apply_service_filter(self):
        q = self.search_entry.get().strip().lower()
        items = [x for x in self.all_services if q in x["label"].lower()] if q else list(self.all_services)
        self.filtered_services = items
        for it in self.service_tree.get_children():
            self.service_tree.delete(it)
        for row in items:
            self.service_tree.insert("", "end", values=(row["label"], row["price"]))

    def on_service_select(self, _event=None):
        selected = self.service_tree.selection()
        if not selected:
            return
        vals = self.service_tree.item(selected[0], "values")
        if not vals:
            return
        label = vals[0]
        for row in self.filtered_services:
            if row["label"] == label:
                self.selected_service_code = row["code"]
                self._update_selected_price(row)
                return

    def _update_selected_price(self, row):
        if not self._ensure_api(warn=False):
            return
        server = self.server_combo.get().strip()
        country = self.country_entry.get().strip()
        service = row["code"]

        def task():
            return self.api.get_price(server=server, service=service, country=country)

        def done(resp):
            data = self._parse_json(resp)
            if isinstance(data, dict) and str(data.get("status", "")).upper() == "SUCCESS":
                price = str(data.get("price", "-"))
                row["price"] = f"${price}"
                self.apply_service_filter()

        self._run_bg(task, done)

    def _selected_order_id(self):
        selected = self.orders_tree.selection()
        if not selected:
            return ""
        vals = self.orders_tree.item(selected[0], "values")
        return str(vals[0]) if vals else ""

    def _upsert_order_row(self, order):
        oid = str(order["id"])
        values = (
            oid,
            order.get("service", "-"),
            order.get("phone", "-"),
            order.get("code", "-"),
            order.get("cost", "-"),
            order.get("status", "-"),
            "Cancel / Done",
        )
        if self.orders_tree.exists(oid):
            self.orders_tree.item(oid, values=values)
        else:
            self.orders_tree.insert("", "end", iid=oid, values=values)
        self.counter_var.set(f"{len(self.orders)} / 5")

    def rent_selected_service(self):
        if not self._ensure_api():
            return
        service = self.selected_service_code.strip()
        if not service:
            messagebox.showwarning("Service", "Please select a service from the left list.")
            return
        server = self.server_combo.get().strip()
        country = self.country_entry.get().strip()
        max_price = self.max_price_entry.get().strip()
        operator = self.operator_entry.get().strip()
        self.log(f"Buying number: server={server} country={country} service={service}")

        def task():
            return self.api.get_number(server=server, service=service, country=country, max_price=max_price, operator=operator)

        def done(resp):
            self.log(f"getNumber response: {resp}")
            if resp.startswith("ACCESS_NUMBER:"):
                parts = resp.split(":")
                if len(parts) >= 3:
                    oid = parts[1].strip()
                    phone = parts[2].strip()
                    self.orders[oid] = {
                        "id": oid,
                        "service": service,
                        "phone": phone,
                        "code": "-",
                        "cost": "-",
                        "status": "STATUS_WAIT_CODE",
                    }
                    self._upsert_order_row(self.orders[oid])
            elif resp.startswith("HTTP_ERROR:429"):
                self.log("Rate limited (429). API limit is 1 request each 2 seconds.")

        self._run_bg(task, done)

    def _set_status(self, order_id: str, status_code: str):
        if not order_id:
            messagebox.showwarning("Order", "Please select an order first.")
            return

        def task():
            return self.api.set_status(order_id, status_code)

        def done(resp):
            self.log(f"setStatus({status_code})[{order_id}] => {resp}")
            if order_id in self.orders:
                self.orders[order_id]["status"] = resp
                self._upsert_order_row(self.orders[order_id])

        self._run_bg(task, done)

    def cancel_selected_order(self):
        self._set_status(self._selected_order_id(), "8")

    def complete_selected_order(self):
        self._set_status(self._selected_order_id(), "6")

    def check_all_statuses(self):
        if not self._ensure_api(warn=False):
            return
        for oid in list(self.orders.keys()):
            self._check_status_order(oid)

    def _check_status_order(self, order_id: str):
        if self.status_in_flight:
            return
        self.status_in_flight = True

        def task():
            return self.api.get_status(order_id)

        def done(resp):
            self.status_in_flight = False
            if order_id in self.orders:
                self.orders[order_id]["status"] = resp
                self.orders[order_id]["code"] = self._extract_code(resp)
                self._upsert_order_row(self.orders[order_id])
            self.log(f"getStatus({order_id}) => {resp}")

        self._run_bg(task, done)

    def _poll_tick(self):
        if self.polling_enabled and self.orders:
            self.check_all_statuses()
        self.after(POLL_INTERVAL_MS, self._poll_tick)


if __name__ == "__main__":
    app = App()
    app.mainloop()

