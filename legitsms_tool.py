#!/usr/bin/env python3
"""LegitSMS Tool - PyQt6 hacker-style desktop UI."""

import json
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PyQt6.QtCore import QTimer, Qt, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

BASE_URL = "https://api.legitsms.com/api/handler/"
POLL_INTERVAL_MS = 10000
REFUND_DELAY_SEC = 120


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


class AsyncBridge(QObject):
    done = pyqtSignal(object)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LegitSMS // Hacker Console")
        self.resize(1240, 800)
        self.setMinimumSize(1080, 700)

        self.api = None
        self.selected_service_code = ""
        self.all_services = []
        self.filtered_services = []
        self.orders = {}
        self.order_row_map = {}
        self._bridges = []
        self.country_refresh_timer = QTimer(self)
        self.country_refresh_timer.setSingleShot(True)
        self.country_refresh_timer.timeout.connect(self.refresh_services)
        self.inflight_orders = set()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_tick)
        self.poll_timer.start(POLL_INTERVAL_MS)
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._tick_order_countdowns)
        self.ui_timer.start(1000)
        self._refund_blink_on = False

        self._build_ui()
        self._apply_hacker_theme()
        self.log("Ready. Enter API key and press Connect. Auto refresh every 10s.")

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 8, 10, 8)
        top_layout.setSpacing(8)
        top_layout.addWidget(QLabel("API KEY"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.returnPressed.connect(self.set_api_key)
        top_layout.addWidget(self.api_key_input, 1)
        connect_btn = QPushButton("CONNECT")
        connect_btn.clicked.connect(self.set_api_key)
        top_layout.addWidget(connect_btn)
        self.connected_label = QLabel("DISCONNECTED")
        top_layout.addWidget(self.connected_label)
        root_layout.addWidget(top_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("SERVICES // MARKET"))

        row = QHBoxLayout()
        row.addWidget(QLabel("Server"))
        self.server_combo = QComboBox()
        self.server_combo.addItems(["1", "2", "3"])
        self.server_combo.currentTextChanged.connect(lambda _v: self.refresh_services())
        row.addWidget(self.server_combo)
        row.addWidget(QLabel("Country"))
        self.country_input = QLineEdit("187")
        self.country_input.returnPressed.connect(self.refresh_services)
        self.country_input.textChanged.connect(lambda _t: self.country_refresh_timer.start(500))
        row.addWidget(self.country_input)
        left_layout.addLayout(row)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("search service...")
        self.search_input.textChanged.connect(self.apply_service_filter)
        search_row.addWidget(self.search_input, 1)
        reload_btn = QPushButton("RELOAD")
        reload_btn.clicked.connect(self.refresh_services)
        search_row.addWidget(reload_btn)
        left_layout.addLayout(search_row)

        self.service_table = QTableWidget(0, 2)
        self.service_table.setHorizontalHeaderLabels(["SERVICE", "PRICE"])
        self.service_table.verticalHeader().setVisible(False)
        self.service_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.service_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.service_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.service_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.service_table.itemSelectionChanged.connect(self.on_service_select)
        self.service_table.cellDoubleClicked.connect(lambda _r, _c: self.rent_selected_service())
        left_layout.addWidget(self.service_table, 1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("ORDERS // RUNTIME"))
        self.counter_label = QLabel("0 / 5")
        self.counter_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.counter_label, 1)
        right_layout.addLayout(title_row)

        controls = QHBoxLayout()
        rent_btn = QPushButton("RENT SELECTED SERVICE")
        rent_btn.clicked.connect(self.rent_selected_service)
        controls.addWidget(rent_btn)
        refresh_btn = QPushButton("REFRESH NOW")
        refresh_btn.clicked.connect(self.check_all_statuses)
        controls.addWidget(refresh_btn)
        self.refund_btn = QPushButton("CANCEL / REFUND SELECTED")
        self.refund_btn.clicked.connect(self.refund_selected_order)
        self.refund_btn.setEnabled(False)
        controls.addWidget(self.refund_btn)
        self.complete_btn = QPushButton("COMPLETE SELECTED")
        self.complete_btn.clicked.connect(self.complete_selected_order)
        self.complete_btn.setEnabled(False)
        controls.addWidget(self.complete_btn)
        controls.addStretch(1)
        self.advanced_chk = QCheckBox("ADVANCED")
        self.advanced_chk.stateChanged.connect(self._toggle_advanced)
        controls.addWidget(self.advanced_chk)
        right_layout.addLayout(controls)

        self.advanced_box = QWidget()
        adv_form = QFormLayout(self.advanced_box)
        adv_form.setContentsMargins(0, 0, 0, 0)
        self.max_price_input = QLineEdit()
        self.operator_input = QLineEdit()
        adv_form.addRow("Max Price", self.max_price_input)
        adv_form.addRow("Operator", self.operator_input)
        self.advanced_box.setVisible(False)
        right_layout.addWidget(self.advanced_box)

        self.orders_table = QTableWidget(0, 6)
        self.orders_table.setHorizontalHeaderLabels(["ID", "SERVICE", "PHONE", "CODE", "COST", "STATUS"])
        self.orders_table.verticalHeader().setVisible(False)
        self.orders_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.orders_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.orders_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.orders_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.orders_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.orders_table.cellClicked.connect(self._handle_order_click_copy)
        self.orders_table.itemSelectionChanged.connect(self._update_action_buttons)
        self.orders_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.orders_table.customContextMenuRequested.connect(self._show_order_context_menu)
        right_layout.addWidget(self.orders_table, 2)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        right_layout.addWidget(self.log_text, 1)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

    def _apply_hacker_theme(self):
        self.setStyleSheet(
            """
            QWidget {
                background: #060b08;
                color: #86ff9d;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 12px;
            }
            QMainWindow, QSplitter, QTextEdit, QTableWidget, QLineEdit, QComboBox {
                background: #0a120d;
                border: 1px solid #1e5b2f;
            }
            QPushButton {
                background: #102718;
                border: 1px solid #2f9a50;
                color: #a6ffbc;
                padding: 6px 10px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #15351f;
            }
            QHeaderView::section {
                background: #102718;
                color: #8dffab;
                border: 1px solid #2f9a50;
                padding: 4px;
            }
            QTableWidget::item:selected {
                background: #1a3b25;
                color: #d7ffe0;
            }
            QLineEdit, QComboBox, QTextEdit {
                selection-background-color: #215a32;
                selection-color: #e2ffea;
            }
            """
        )

    def log(self, message: str):
        self.log_text.append(message)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _run_bg(self, fn, callback):
        bridge = AsyncBridge()
        self._bridges.append(bridge)

        def finish(result):
            try:
                callback(result)
            finally:
                if bridge in self._bridges:
                    self._bridges.remove(bridge)

        bridge.done.connect(finish)

        def worker():
            try:
                result = fn()
            except Exception as exc:
                result = f"WORKER_ERROR:{exc}"
            bridge.done.emit(result)

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_api(self, warn=True):
        if self.api is None:
            if warn:
                QMessageBox.warning(self, "API key missing", "Please connect API key first.")
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

    @staticmethod
    def _normalize_phone_for_copy(phone: str) -> str:
        raw = str(phone).strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            return digits[1:]
        return raw

    def _copy(self, text: str):
        value = str(text).strip()
        if not value or value == "-":
            return
        QApplication.clipboard().setText(value)

    def _toggle_advanced(self):
        self.advanced_box.setVisible(bool(self.advanced_chk.isChecked()))

    def set_api_key(self):
        key = self.api_key_input.text().strip()
        if not key:
            QMessageBox.warning(self, "Missing API key", "Please enter API key.")
            return
        self.api = LegitSMSApi(key)
        self.connected_label.setText("CONNECTED")
        self.log("Connected. Loading services...")
        self.refresh_services()

    def refresh_services(self):
        if not self._ensure_api(warn=False):
            return
        server = self.server_combo.currentText().strip()
        country = self.country_input.text().strip()
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
        q = self.search_input.text().strip().lower()
        items = [x for x in self.all_services if q in x["label"].lower()] if q else list(self.all_services)
        self.filtered_services = items
        self.service_table.setRowCount(len(items))
        for i, row in enumerate(items):
            self.service_table.setItem(i, 0, QTableWidgetItem(row["label"]))
            self.service_table.setItem(i, 1, QTableWidgetItem(row["price"]))

    def on_service_select(self):
        row_idx = self.service_table.currentRow()
        if row_idx < 0 or row_idx >= len(self.filtered_services):
            return
        row = self.filtered_services[row_idx]
        self.selected_service_code = row["code"]
        self._update_selected_price(row)

    def _update_selected_price(self, row):
        if not self._ensure_api(warn=False):
            return
        server = self.server_combo.currentText().strip()
        country = self.country_input.text().strip()
        service = row["code"]

        def task():
            return self.api.get_price(server=server, service=service, country=country)

        def done(resp):
            data = self._parse_json(resp)
            if isinstance(data, dict) and str(data.get("status", "")).upper() == "SUCCESS":
                row["price"] = f"${str(data.get('price', '-'))}"
                self.apply_service_filter()

        self._run_bg(task, done)

    def _selected_order_id(self):
        row = self.orders_table.currentRow()
        if row < 0:
            return ""
        item = self.orders_table.item(row, 0)
        return item.text().strip() if item else ""

    @staticmethod
    def _is_final_status(status_text: str) -> bool:
        s = str(status_text or "").strip().upper()
        if s.startswith("STATUS_OK:"):
            return True
        return s in {"STATUS_CANCEL", "ACCESS_CANCEL", "ACCESS_ACTIVATION", "STATUS_FINISH"}

    def _refund_remaining_sec(self, order: dict) -> int:
        created_ts = float(order.get("created_ts", 0.0) or 0.0)
        if created_ts <= 0:
            return REFUND_DELAY_SEC
        return max(0, int(created_ts + REFUND_DELAY_SEC - time.time()))

    def _can_refund(self, order: dict) -> bool:
        if not isinstance(order, dict):
            return False
        if bool(order.get("stop_refresh")):
            return False
        status_text = str(order.get("status", ""))
        if self._is_final_status(status_text):
            return False
        if str(order.get("code", "-")).strip() not in {"", "-"}:
            return False
        return self._refund_remaining_sec(order) <= 0

    def _status_display(self, order: dict) -> str:
        status_text = str(order.get("status", "-"))
        if bool(order.get("stop_refresh")):
            return status_text
        if str(order.get("code", "-")).strip() not in {"", "-"}:
            return status_text
        if self._can_refund(order):
            return f"{status_text} | REFUND READY"
        remain = self._refund_remaining_sec(order)
        mm = remain // 60
        ss = remain % 60
        return f"{status_text} | refund in {mm:02d}:{ss:02d}"

    def _tick_order_countdowns(self):
        if not self.orders:
            return
        self._refund_blink_on = not self._refund_blink_on
        for order in self.orders.values():
            self._upsert_order_row(order)
        self._update_action_buttons()

    def _update_action_buttons(self):
        oid = self._selected_order_id()
        order = self.orders.get(oid) if oid else None
        self.complete_btn.setEnabled(bool(order))
        self.refund_btn.setEnabled(self._can_refund(order) if order else False)

    def _paint_order_row(self, row_index: int, status_text: str, order=None):
        if order is not None and self._can_refund(order):
            color = QColor("#2f4a12") if self._refund_blink_on else QColor("#4d2f09")
        elif status_text.startswith("STATUS_OK:"):
            color = QColor("#12331e")
        elif status_text.startswith("ERROR") or status_text.startswith("HTTP_ERROR") or status_text.startswith("NO_ACTIVATION"):
            color = QColor("#3b1b1b")
        else:
            color = QColor("#2d290f")
        for col in range(self.orders_table.columnCount()):
            item = self.orders_table.item(row_index, col)
            if item is not None:
                item.setBackground(color)

    def _upsert_order_row(self, order):
        oid = str(order["id"])
        status_ui = self._status_display(order)
        values = [
            oid,
            str(order.get("service", "-")),
            str(order.get("phone", "-")),
            str(order.get("code", "-")),
            str(order.get("cost", "-")),
            status_ui,
        ]
        if oid in self.order_row_map:
            row = self.order_row_map[oid]
        else:
            row = self.orders_table.rowCount()
            self.orders_table.insertRow(row)
            self.order_row_map[oid] = row

        for col, value in enumerate(values):
            item = self.orders_table.item(row, col)
            if item is None:
                item = QTableWidgetItem(value)
                self.orders_table.setItem(row, col, item)
            else:
                item.setText(value)

        self._paint_order_row(row, values[5], order)
        self.counter_label.setText(f"{len(self.orders)} / 5")

    def rent_selected_service(self):
        if not self._ensure_api():
            return
        service = self.selected_service_code.strip()
        if not service:
            QMessageBox.warning(self, "Service", "Please select a service from left table.")
            return
        server = self.server_combo.currentText().strip()
        country = self.country_input.text().strip()
        max_price = self.max_price_input.text().strip()
        operator = self.operator_input.text().strip()
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
                        "stop_refresh": False,
                        "created_ts": time.time(),
                    }
                    self._upsert_order_row(self.orders[oid])
            elif resp.startswith("HTTP_ERROR:429"):
                self.log("Rate limited (429). API limit is 1 request each 2 seconds.")

        self._run_bg(task, done)

    def _set_status(self, order_id: str, status_code: str):
        if not order_id:
            QMessageBox.warning(self, "Order", "Please select an order first.")
            return

        def task():
            return self.api.set_status(order_id, status_code)

        def done(resp):
            self.log(f"setStatus({status_code})[{order_id}] => {resp}")
            if order_id in self.orders:
                self.orders[order_id]["status"] = resp
                if status_code in {"6", "8"}:
                    self.orders[order_id]["stop_refresh"] = True
                self._upsert_order_row(self.orders[order_id])

        self._run_bg(task, done)

    def refund_selected_order(self):
        oid = self._selected_order_id()
        if not oid:
            QMessageBox.warning(self, "Order", "Please select an order first.")
            return
        order = self.orders.get(oid)
        if not self._can_refund(order):
            remain = self._refund_remaining_sec(order or {})
            mm = remain // 60
            ss = remain % 60
            self.log(f"Refund locked for {oid}: wait {mm:02d}:{ss:02d} (needs 2m without code).")
            return
        self._set_status(oid, "8")

    def cancel_selected_order(self):
        self._set_status(self._selected_order_id(), "8")

    def complete_selected_order(self):
        self._set_status(self._selected_order_id(), "6")

    def check_all_statuses(self):
        if not self._ensure_api(warn=False):
            return
        for oid, order in list(self.orders.items()):
            if bool(order.get("stop_refresh")):
                continue
            self._check_status_order(oid)

    def _check_status_order(self, order_id: str):
        if order_id in self.inflight_orders:
            return
        self.inflight_orders.add(order_id)

        def task():
            return self.api.get_status(order_id)

        def done(resp):
            self.inflight_orders.discard(order_id)
            if order_id in self.orders:
                self.orders[order_id]["status"] = resp
                code = self._extract_code(resp)
                self.orders[order_id]["code"] = code
                if resp.startswith("STATUS_OK:"):
                    self.orders[order_id]["stop_refresh"] = True
                    self._copy(code)
                self._upsert_order_row(self.orders[order_id])
            self.log(f"getStatus({order_id}) => {resp}")

        self._run_bg(task, done)

    def _poll_tick(self):
        if self.orders:
            self.check_all_statuses()

    def _handle_order_click_copy(self, row: int, column: int):
        if row < 0:
            return
        if column == 2:
            phone_item = self.orders_table.item(row, 2)
            if phone_item:
                phone = self._normalize_phone_for_copy(phone_item.text())
                self._copy(phone)
                self.log(f"Copied phone: {phone}")
        elif column == 3:
            code_item = self.orders_table.item(row, 3)
            if code_item:
                code = code_item.text().strip()
                self._copy(code)
                self.log(f"Copied code: {code}")

    def _show_order_context_menu(self, pos):
        row = self.orders_table.rowAt(pos.y())
        if row >= 0:
            self.orders_table.selectRow(row)
        oid = self._selected_order_id()
        order = self.orders.get(oid) if oid else None
        menu = QMenu(self)
        cancel_action = QAction("Cancel/Refund selected", self)
        done_action = QAction("Complete selected", self)
        cancel_action.setEnabled(self._can_refund(order) if order else False)
        cancel_action.triggered.connect(self.refund_selected_order)
        done_action.triggered.connect(self.complete_selected_order)
        done_action.setEnabled(bool(order))
        menu.addAction(cancel_action)
        menu.addAction(done_action)
        menu.exec(self.orders_table.viewport().mapToGlobal(pos))


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

