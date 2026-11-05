#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LNM Sniper Command Center · Flet Edition
100% independiente. Solo lee brain + log.
Optimizado para Termux (web :8550) y Desktop.
"""
from __future__ import annotations

import os
import re
import json
import time
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List

import flet as ft

BASE_DIR = Path(__file__).resolve().parent
CANDIDATE_DIRS = [
    BASE_DIR, Path.cwd(), Path.home() / "downloads",
    Path.home() / "storage" / "downloads", Path.home() / "lnm_sniper",
]

def find_file(name: str) -> Optional[Path]:
    for d in CANDIDATE_DIRS:
        try:
            p = d / name
            if p.exists():
                return p
        except Exception:
            continue
    return None

BRAIN_FILE = find_file("q_brain_advanced_lnm.json") or (BASE_DIR / "q_brain_advanced_lnm.json")
LOG_FILE = find_file("sniper_advanced.log") or (BASE_DIR / "sniper_advanced.log")
OVERRIDES_FILE = BASE_DIR / "strategy_overrides.json"

ACTION_NAMES = {
    0: "NO_TRADE", 1: "LONG_TREND", 2: "LONG_REVERSION", 3: "LONG_SNIPER",
    4: "LONG_DEF_SCALP", 5: "SHORT_TREND", 6: "SHORT_REVERSION",
    7: "SHORT_SNIPER", 8: "SHORT_DEF_SCALP",
}

C = {
    "bg": "#0b0e17", "card": "#121826", "card2": "#1a2235", "border": "#1e2a44",
    "accent": "#00e5ff", "accent2": "#7c4dff", "green": "#00e676",
    "red": "#ff1744", "orange": "#ffab00", "text": "#e8eaf6", "dim": "#8b95b0",
}

def safe_load_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def safe_save_json(path: Path, data: Any) -> bool:
    try:
        tmp = path.with_suffix(".tmp_ui")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[APP] save error: {e}")
        return False

def tail_log(path: Path, n: int = 160) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            block = min(fsize, max(16384, n * 180))
            f.seek(max(0, fsize - block), os.SEEK_SET)
            raw = f.read().decode("utf-8", errors="ignore")
        return raw.splitlines()[-n:]
    except Exception:
        return []

RE_PEAK_SYNC = re.compile(r"Peak=([\d,]+)\s*sats\s*\|\s*Piso Duro\s*\(75%\)=([\d,]+)\s*sats")

def parse_log_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line or line.startswith(("nohup", "=")):
        return None
    r: Dict[str, Any] = {
        "raw": line, "ts": None, "mode": None, "state": None,
        "price": None, "equity": None, "balance": None,
        "active": None, "dd": None, "ovr": None,
        "sync_peak": None, "sync_floor": None,
    }
    m_sync = RE_PEAK_SYNC.search(line)
    if m_sync:
        try:
            r["sync_peak"] = int(m_sync.group(1).replace(",", ""))
            r["sync_floor"] = int(m_sync.group(2).replace(",", ""))
        except Exception:
            pass
    if line.startswith("[") and "]" in line:
        try:
            r["ts"] = line[1:line.index("]")]
        except Exception:
            pass
    for m in ("NORMAL", "RECOVERY", "DEFENSIVE"):
        if f"[{m}]" in line:
            r["mode"] = m
            break
    if "Eq:" in line:
        try:
            part = line.split("Eq:")[1].split("|")[0].strip()
            r["equity"] = int(part.split("(")[0].replace(",", "").strip())
            if "Bal:" in part:
                r["balance"] = int(part.split("Bal:")[1].split(")")[0].replace(",", "").strip())
        except Exception:
            pass
    if "Act:" in line:
        try:
            r["active"] = line.split("Act:")[1].split("|")[0].strip()
        except Exception:
            pass
    if "DD:" in line:
        try:
            r["dd"] = float(line.split("DD:")[1].split("%")[0].strip())
        except Exception:
            pass
    if "$" in line:
        try:
            r["price"] = float(line[line.index("$") + 1:].split()[0].replace(",", ""))
        except Exception:
            pass
    if "OVR:" in line:
        try:
            r["ovr"] = line.split("OVR:")[1].split("]")[0].strip()
        except Exception:
            pass
    if "][" in line and r["mode"]:
        try:
            for p in line.split("]"):
                if "|" in p and any(x in p for x in ("RANGE", "TREND", "CHOP")):
                    r["state"] = p.lstrip("[").strip()
                    break
        except Exception:
            pass
    return r


class SniperApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.running = True
        self.refresh_sec = 2.5
        self.equity_hist: List[int] = []
        self.metrics: Dict[str, Any] = {
            "mode": "—", "state": "—", "equity": 0, "balance": 0,
            "active": "0/5", "dd": 0.0, "price": 0.0, "ovr": "OFF",
            "peak": 0, "floor": 0, "target": 300000,
            "trained": 0, "online": 0, "q_states": 0, "epsilon": 0.4, "milestone": 1,
        }
        self.brain: Dict[str, Any] = {}
        self.overrides = safe_load_json(OVERRIDES_FILE, {})
        self.log_lines: List[str] = []
        self.strat_fields: Dict[str, ft.TextField] = {}
        self._setup_page()
        self._build_ui()
        self._poll_data()
        self._update_ui()
        self._refresh_positions()
        self._load_brain_ui(notify=False)
        self._start_monitor()

    def _notify(self, msg: str, color: str = C["accent2"]):
        sb = ft.SnackBar(ft.Text(msg), bgcolor=color)
        try:
            self.page.snack_bar = sb
            self.page.snack_bar.open = True
            self.page.update()
        except Exception:
            pass

    def _setup_page(self):
        p = self.page
        p.title = "LNM Sniper · Command Center"
        p.theme_mode = ft.ThemeMode.DARK
        p.bgcolor = C["bg"]
        p.padding = 0
        p.spacing = 0

    def _kpi_card(self, title: str, value_ref: ft.Ref[ft.Text], color: str) -> ft.Container:
        return ft.Container(
            content=ft.Column([
                ft.Text(title, size=11, color=C["dim"]),
                ft.Text("—", size=17, color=color, weight=ft.FontWeight.BOLD, ref=value_ref),
            ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=C["card"], border=ft.border.all(1, C["border"]),
            border_radius=12, padding=ft.padding.symmetric(horizontal=8, vertical=10),
            col={"xs": 6, "sm": 4, "md": 2},
        )

    def _info_row(self, label: str, value_ref: ft.Ref[ft.Text]) -> ft.Row:
        return ft.Row([
            ft.Text(label, size=12, color=C["dim"], width=120),
            ft.Text("—", size=12, color=C["text"], weight=ft.FontWeight.W_600,
                    ref=value_ref, expand=True, text_align=ft.TextAlign.RIGHT),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    def _build_ui(self):
        self.ref_equity = ft.Ref[ft.Text]()
        self.ref_dd = ft.Ref[ft.Text]()
        self.ref_mode = ft.Ref[ft.Text]()
        self.ref_active = ft.Ref[ft.Text]()
        self.ref_price = ft.Ref[ft.Text]()
        self.ref_ovr = ft.Ref[ft.Text]()
        self.ref_state = ft.Ref[ft.Text]()
        self.ref_peak = ft.Ref[ft.Text]()
        self.ref_floor = ft.Ref[ft.Text]()
        self.ref_target = ft.Ref[ft.Text]()
        self.ref_goal = ft.Ref[ft.Text]()
        self.ref_trained = ft.Ref[ft.Text]()
        self.ref_online = ft.Ref[ft.Text]()
        self.ref_qstates = ft.Ref[ft.Text]()
        self.ref_clock = ft.Ref[ft.Text]()
        self.ref_status = ft.Ref[ft.Text]()
        self.ref_live = ft.Ref[ft.Text]()
        self.equity_chart_text = ft.Ref[ft.Text]()

        self.log_list = ft.ListView(expand=True, spacing=2, auto_scroll=True, padding=8)
        self.pos_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("ID", color=C["accent"])),
                ft.DataColumn(ft.Text("Lado", color=C["accent"])),
                ft.DataColumn(ft.Text("Estrategia", color=C["accent"])),
                ft.DataColumn(ft.Text("Margen", color=C["accent"])),
                ft.DataColumn(ft.Text("PnL ATR", color=C["accent"])),
                ft.DataColumn(ft.Text("Guardián", color=C["accent"])),
            ],
            rows=[], border=ft.border.all(1, C["border"]), border_radius=8,
            heading_row_color=C["card2"], data_row_min_height=36, column_spacing=14,
        )
        self.brain_refs = {k: ft.Ref[ft.Text]() for k in
                           ("states", "trained", "online", "epsilon", "target", "milestone", "peak", "floor")}
        self.q_list = ft.ListView(expand=True, spacing=2, padding=8)

        header = ft.Container(
            content=ft.Row([
                ft.Text("⚡ LNM SNIPER", size=17, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Text("v7.3", size=13, color=C["dim"]),
                ft.Container(expand=True),
                ft.Text("—", size=12, color=C["dim"], ref=self.ref_clock),
                ft.Text("● LIVE", size=12, weight=ft.FontWeight.BOLD, color=C["green"], ref=self.ref_live),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=C["card"], padding=ft.padding.symmetric(horizontal=14, vertical=10),
        )

        kpi_grid = ft.ResponsiveRow([
            self._kpi_card("EQUITY", self.ref_equity, C["accent"]),
            self._kpi_card("DRAWDOWN", self.ref_dd, C["orange"]),
            self._kpi_card("MODO", self.ref_mode, C["accent2"]),
            self._kpi_card("ACTIVOS", self.ref_active, C["text"]),
            self._kpi_card("BTC USD", self.ref_price, C["green"]),
            self._kpi_card("OVR", self.ref_ovr, C["dim"]),
        ], spacing=8, run_spacing=8)

        info_panel = ft.Container(
            content=ft.Column([
                ft.Text("ESTADO DEL SISTEMA", size=13, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Divider(color=C["border"], height=1),
                self._info_row("Estado", self.ref_state),
                self._info_row("Peak Equity", self.ref_peak),
                self._info_row("Piso Duro (75%)", self.ref_floor),
                self._info_row("Meta Activa", self.ref_target),
                self._info_row("Avance Meta", self.ref_goal),
                self._info_row("Cierres IA", self.ref_trained),
                self._info_row("Updates Online", self.ref_online),
                self._info_row("Estados Q", self.ref_qstates),
            ], spacing=6),
            bgcolor=C["card"], border=ft.border.all(1, C["border"]),
            border_radius=12, padding=14, col={"xs": 12, "md": 5},
        )

        chart_box = ft.Container(
            content=ft.Column([
                ft.Text("CURVA DE EQUITY (SATS EN VIVO)", size=12, color=C["dim"], weight=ft.FontWeight.W_600),
                ft.Text("Cargando…", size=12, color=C["accent"], ref=self.equity_chart_text, font_family="monospace"),
            ], spacing=8),
            bgcolor=C["card"], border=ft.border.all(1, C["border"]),
            border_radius=12, padding=14, col={"xs": 12, "md": 7},
        )

        dash_body = ft.Column([
            kpi_grid,
            ft.ResponsiveRow([chart_box, info_panel], spacing=10, run_spacing=10),
            ft.Container(
                content=ft.Text("Monitoreando…", size=11, color=C["dim"], ref=self.ref_status),
                bgcolor=C["card2"], border_radius=8,
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
            ),
        ], spacing=10, expand=True, scroll=ft.ScrollMode.AUTO)

        pos_tab = ft.Column([
            ft.Row([
                ft.Text("POSICIONES ACTIVAS", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Container(expand=True),
                ft.ElevatedButton("↻ Refrescar", on_click=lambda e: self._refresh_positions(),
                                  bgcolor=C["accent2"], color=C["text"]),
            ]),
            ft.Container(content=self.pos_table, bgcolor=C["card"], border_radius=12, padding=8, expand=True),
        ], spacing=10, expand=True)

        def brain_stat(key, label):
            return ft.Container(
                content=ft.Column([
                    ft.Text(label, size=10, color=C["dim"]),
                    ft.Text("—", size=14, weight=ft.FontWeight.BOLD, color=C["accent"], ref=self.brain_refs[key]),
                ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=C["card"], border=ft.border.all(1, C["border"]),
                border_radius=10, padding=10, expand=True,
            )

        brain_tab = ft.Column([
            ft.Row([
                ft.Text("CEREBRO Q-LEARNING", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Container(expand=True),
                ft.ElevatedButton("↻ Recargar", on_click=lambda e: self._load_brain_ui(),
                                  bgcolor=C["accent2"], color=C["text"]),
                ft.ElevatedButton("💾 Backup", on_click=lambda e: self._backup_brain(),
                                  bgcolor=C["card2"], color=C["text"]),
            ]),
            ft.Row([brain_stat("states", "Estados Q"), brain_stat("trained", "Trained"),
                    brain_stat("online", "Online"), brain_stat("epsilon", "Epsilon")], spacing=6),
            ft.Row([brain_stat("target", "Target"), brain_stat("milestone", "Milestone"),
                    brain_stat("peak", "Peak"), brain_stat("floor", "Floor")], spacing=6),
            ft.Container(
                content=ft.Column([ft.Text("Top estados por visitas", size=12, color=C["dim"]), self.q_list], expand=True),
                bgcolor=C["card"], border_radius=12, padding=10, expand=True,
            ),
        ], spacing=10, expand=True)

        strategy_params = [
            ("MAX_CONCURRENT_TRADES", "5", "Máx posiciones"),
            ("RISK_NORMAL", "0.12", "Riesgo normal"),
            ("RISK_WARMUP", "0.06", "Riesgo warmup"),
            ("SOFT_EXPOSURE_LIMIT", "0.38", "Exposición soft"),
            ("ENTRY_COOLDOWN_SEC", "40", "Cooldown entradas (s)"),
            ("RECOVERY_ENTER_DD", "0.095", "Entrar Recovery DD"),
            ("RECOVERY_EXIT_DD", "0.065", "Salir Recovery DD"),
            ("RECOVERY_RISK_MULT", "0.50", "Mult. riesgo Recovery"),
            ("MAX_DRAWDOWN_PCT", "0.16", "Max Drawdown"),
            ("CONSECUTIVE_LOSS_LIMIT", "4", "Límite rachas"),
            ("MIN_LEVERAGE", "15", "Lev mínimo"),
            ("MAX_LEVERAGE", "95", "Lev máximo"),
            ("TAKER_FEE", "0.001", "Taker fee"),
            ("ALPHA_INTERMEDIATE", "0.020", "Alpha intermedio"),
            ("WARMUP_TRADES", "20", "Trades warmup"),
        ]
        strat_controls = []
        for key, default, label in strategy_params:
            cur = str(self.overrides.get(key, default))
            tf = ft.TextField(label=f"{label} ({key})", value=cur, dense=True,
                              border_color=C["border"], focused_border_color=C["accent"],
                              color=C["text"], text_size=13)
            self.strat_fields[key] = tf
            strat_controls.append(tf)

        strat_tab = ft.Column([
            ft.Text("AJUSTE DE ESTRATEGIA", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
            ft.Text("Overrides → strategy_overrides.json (el bot no los lee solo)", size=11, color=C["dim"]),
            ft.Container(content=ft.Column(strat_controls, spacing=8, scroll=ft.ScrollMode.AUTO),
                         bgcolor=C["card"], border_radius=12, padding=12, expand=True),
            ft.Row([
                ft.ElevatedButton("💾 Guardar", on_click=lambda e: self._save_overrides(),
                                  bgcolor=C["green"], color="#000"),
                ft.ElevatedButton("↺ Restaurar", on_click=lambda e: self._reset_overrides(),
                                  bgcolor=C["card2"], color=C["text"]),
            ], spacing=10),
        ], spacing=10, expand=True)

        self.log_filter_dd = ft.Dropdown(
            options=[ft.dropdown.Option(x) for x in
                     ["TODO", "ERROR", "RECOVERY", "NORMAL", "IA ONLINE", "OVR:BULL", "OVR:BEAR"]],
            value="TODO", width=145, dense=True, border_color=C["border"], color=C["text"],
            on_change=lambda e: self._update_ui(),
        )
        logs_tab = ft.Column([
            ft.Row([
                ft.Text("LOG EN VIVO", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Container(expand=True), self.log_filter_dd,
                ft.ElevatedButton("🗑 Limpiar", on_click=lambda e: self._clear_logs(),
                                  bgcolor=C["card2"], color=C["text"]),
            ]),
            ft.Container(content=self.log_list, bgcolor=C["card"], border_radius=12, expand=True),
        ], spacing=10, expand=True)

        self.brain_path_tf = ft.TextField(label="Ruta Brain JSON", value=str(BRAIN_FILE),
                                          dense=True, border_color=C["border"], color=C["text"])
        self.log_path_tf = ft.TextField(label="Ruta Log", value=str(LOG_FILE),
                                        dense=True, border_color=C["border"], color=C["text"])
        self.refresh_slider = ft.Slider(min=1, max=8, divisions=7, value=2.5, label="{value}s",
                                        active_color=C["accent"], on_change=self._on_refresh_change)

        settings_tab = ft.Column([
            ft.Text("AJUSTES", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
            ft.Container(content=ft.Column([
                self.brain_path_tf, self.log_path_tf,
                ft.Text("Intervalo de refresco (segundos)", size=12, color=C["dim"]),
                self.refresh_slider,
            ], spacing=12), bgcolor=C["card"], border_radius=12, padding=14),
            ft.Container(content=ft.Text(
                "LNM Sniper Command Center · Flet Edition\n"
                "100% Independiente · Lectura pasiva\n"
                "Termux: http://127.0.0.1:8550  |  Desktop: ventana nativa",
                size=12, color=C["dim"]), bgcolor=C["card"], border_radius=12, padding=14),
        ], spacing=12, expand=True, scroll=ft.ScrollMode.AUTO)

        self.tabs = ft.Tabs(
            selected_index=0, animation_duration=180,
            indicator_color=C["accent"], label_color=C["accent"],
            unselected_label_color=C["dim"], expand=True,
            tabs=[
                ft.Tab(text="Dashboard", content=ft.Container(content=dash_body, padding=12, expand=True)),
                ft.Tab(text="Posiciones", content=ft.Container(content=pos_tab, padding=12, expand=True)),
                ft.Tab(text="Cerebro", content=ft.Container(content=brain_tab, padding=12, expand=True)),
                ft.Tab(text="Estrategia", content=ft.Container(content=strat_tab, padding=12, expand=True)),
                ft.Tab(text="Logs", content=ft.Container(content=logs_tab, padding=12, expand=True)),
                ft.Tab(text="Ajustes", content=ft.Container(content=settings_tab, padding=12, expand=True)),
            ],
        )
        self.page.add(header, self.tabs)
        self.page.update()

    def _start_monitor(self):
        def loop():
            while self.running:
                try:
                    self._poll_data()
                    self._update_ui()
                except Exception as ex:
                    print(f"[monitor] {ex}")
                time.sleep(max(1.0, self.refresh_sec))
        threading.Thread(target=loop, daemon=True).start()

    def _poll_data(self):
        bpath = Path(self.brain_path_tf.value) if hasattr(self, "brain_path_tf") else BRAIN_FILE
        bd = safe_load_json(bpath, {})
        if bd:
            self.brain = bd
            floor_val = int(bd.get("locked_floor", 0))
            inferred_peak = int(bd.get("peak_equity", 0)) or (int(round(floor_val / 0.75)) if floor_val > 0 else 0)
            self.metrics["floor"] = floor_val
            if inferred_peak > 0:
                self.metrics["peak"] = max(self.metrics.get("peak", 0), inferred_peak)
            self.metrics["target"] = int(bd.get("current_target", 300000))
            self.metrics["trained"] = int(bd.get("total_trained", 0))
            self.metrics["online"] = int(bd.get("total_intermediate_updates", 0))
            self.metrics["q_states"] = len(bd.get("q_table", {}))
            self.metrics["epsilon"] = float(bd.get("epsilon", 0.4))
            self.metrics["milestone"] = int(bd.get("milestone_level", 1))

        lpath = Path(self.log_path_tf.value) if hasattr(self, "log_path_tf") else LOG_FILE
        lines = tail_log(lpath, 120)
        eq_series: List[int] = []
        clean_lines: List[str] = []
        for line in lines:
            s_line = line.strip()
            if not s_line:
                continue
            clean_lines.append(s_line)
            parsed = parse_log_line(s_line)
            if parsed:
                if parsed.get("sync_peak"):
                    self.metrics["peak"] = parsed["sync_peak"]
                if parsed.get("sync_floor"):
                    self.metrics["floor"] = parsed["sync_floor"]
                if parsed.get("equity") is not None:
                    for k in ("ts", "mode", "state", "price", "equity", "balance", "active", "dd", "ovr"):
                        if parsed.get(k) is not None:
                            self.metrics[k] = parsed[k]
                    eq_series.append(parsed["equity"])
                    if parsed["equity"] > self.metrics.get("peak", 0):
                        self.metrics["peak"] = parsed["equity"]
        if clean_lines:
            self.log_lines = clean_lines[-120:]
        if eq_series:
            self.equity_hist = eq_series[-60:]

    def _update_ui(self):
        m = self.metrics
        eq = m.get("equity", 0)
        dd = m.get("dd", 0.0)
        mode = m.get("mode", "—")

        if self.ref_equity.current:
            self.ref_equity.current.value = f"{eq:,} sats"
            self.ref_equity.current.color = C["green"] if eq > 0 else C["text"]
        if self.ref_dd.current:
            self.ref_dd.current.value = f"{dd:.1f}%"
            self.ref_dd.current.color = C["green"] if dd < 5 else (C["orange"] if dd < 10 else C["red"])
        if self.ref_mode.current:
            self.ref_mode.current.value = mode
            self.ref_mode.current.color = {"NORMAL": C["green"], "RECOVERY": C["orange"], "DEFENSIVE": C["red"]}.get(mode, C["text"])
        if self.ref_active.current:
            self.ref_active.current.value = str(m.get("active", f"{len(self.brain.get('active_trades', {}))}/5"))
        if self.ref_price.current:
            p = m.get("price", 0)
            self.ref_price.current.value = f"\( {p:,.1f}" if p else " \)—"
        if self.ref_ovr.current:
            o = m.get("ovr", "OFF")
            self.ref_ovr.current.value = o
            self.ref_ovr.current.color = C["green"] if o == "BULL" else (C["red"] if o == "BEAR" else C["dim"])
        if self.ref_state.current:
            self.ref_state.current.value = str(m.get("state", "—"))[:42]
        if self.ref_peak.current:
            self.ref_peak.current.value = f"{m.get('peak', 0):,} sats"
        if self.ref_floor.current:
            self.ref_floor.current.value = f"{m.get('floor', 0):,} sats"
        if self.ref_target.current:
            self.ref_target.current.value = f"{m.get('target', 300000):,} sats"
        if self.ref_goal.current:
            self.ref_goal.current.value = f"{(eq / max(m.get('target', 1), 1)) * 100:.2f}%"
        if self.ref_trained.current:
            self.ref_trained.current.value = str(m.get("trained", 0))
        if self.ref_online.current:
            self.ref_online.current.value = str(m.get("online", 0))
        if self.ref_qstates.current:
            self.ref_qstates.current.value = str(m.get("q_states", 0))
        if self.ref_clock.current:
            self.ref_clock.current.value = datetime.now().strftime("%H:%M:%S")
        if self.ref_live.current:
            self.ref_live.current.color = C["green"] if mode != "DEFENSIVE" else C["red"]
        if self.ref_status.current:
            self.ref_status.current.value = (
                f"Eq: {eq:,} sats (Bal: {m.get('balance', 0):,}) · "
                f"Piso: {m.get('floor', 0):,} · DD: {dd:.1f}% · Modo: {mode}"
            )
        if self.equity_chart_text.current and len(self.equity_hist) >= 2:
            vals = self.equity_hist[-40:]
            mn, mx = min(vals), max(vals)
            span = max(mx - mn, 1)
            bars = "▁▂▃▄▅▆▇█"
            spark = "".join(bars[min(7, int((v - mn) / span * 7))] for v in vals)
            self.equity_chart_text.current.value = f"{spark}\nActual: {vals[-1]:,} sats  |  Mín: {mn:,}  |  Máx: {mx:,}"

        filt = self.log_filter_dd.value if self.log_filter_dd else "TODO"
        self.log_list.controls.clear()
        for line in self.log_lines[-80:]:
            if filt == "TODO" or filt in line:
                col = C["red"] if "ERROR" in line else (C["orange"] if "RECOVERY" in line else C["text"])
                self.log_list.controls.append(ft.Text(line, size=11, color=col, font_family="monospace"))
        try:
            self.page.update()
        except Exception:
            pass

    def _load_brain_ui(self, notify: bool = True):
        self._poll_data()
        bd = self.brain
        if not bd:
            if notify:
                self._notify("No se encontró q_brain_advanced_lnm.json", C["red"])
            return
        floor_val = int(bd.get("locked_floor", 0))
        peak_val = self.metrics.get("peak", 0) or int(round(floor_val / 0.75))
        mapping = {
            "states": str(len(bd.get("q_table", {}))),
            "trained": str(bd.get("total_trained", 0)),
            "online": str(bd.get("total_intermediate_updates", 0)),
            "epsilon": f"{float(bd.get('epsilon', 0)):.3f}",
            "target": f"{int(bd.get('current_target', 300000)):,}",
            "milestone": str(bd.get("milestone_level", 1)),
            "peak": f"{peak_val:,}",
            "floor": f"{floor_val:,}",
        }
        for k, v in mapping.items():
            if self.brain_refs[k].current:
                self.brain_refs[k].current.value = v
        visits = bd.get("state_visits", {})
        q_table = bd.get("q_table", {})
        top = sorted(visits.items(), key=lambda x: -x[1])[:30]
        self.q_list.controls.clear()
        self.q_list.controls.append(
            ft.Text(f"{'Estado':<44} {'Vis':>5}  Mejor Acción", size=11, color=C["dim"], font_family="monospace")
        )
        for st, vis in top:
            q = q_table.get(st, [])
            if isinstance(q, list) and len(q) >= 9:
                best_idx = max(range(1, 9), key=lambda i: q[i])
                best_str = f"a{best_idx}:{ACTION_NAMES.get(best_idx, '')} ({q[best_idx]:+.2f})"
            else:
                best_str = "-"
            self.q_list.controls.append(
                ft.Text(f"{st[:43]:<44} {vis:>5}  {best_str}", size=11, color=C["text"], font_family="monospace")
            )
        if notify:
            self._notify(f"Cerebro cargado · {len(q_table)} estados", C["green"])
        self.page.update()

    def _backup_brain(self):
        path = Path(self.brain_path_tf.value) if self.brain_path_tf.value else BRAIN_FILE
        if not path.exists():
            self._notify("Archivo de cerebro no encontrado", C["red"])
            return
        bak = path.with_name(f"{path.stem}_ui_bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            shutil.copy2(path, bak)
            self._notify(f"Backup: {bak.name}", C["green"])
        except Exception as e:
            self._notify(str(e), C["red"])

    def _refresh_positions(self):
        self._poll_data()
        self.pos_table.rows.clear()
        active = self.brain.get("active_trades", {})
        if not active:
            self.pos_table.rows.append(ft.DataRow(
                cells=[ft.DataCell(ft.Text("Sin posiciones", color=C["dim"]))] +
                      [ft.DataCell(ft.Text("—", color=C["dim"])) for _ in range(5)]
            ))
        else:
            for tid, meta in active.items():
                if not isinstance(meta, dict):
                    continue
                act_id = int(meta.get("action", 2))
                side = meta.get("side") or ("buy" if act_id <= 4 else "sell")
                strat_label = ACTION_NAMES.get(act_id, str(meta.get("style", "—")))
                pnl_atr = meta.get("last_pnl_atr", 0.0)
                pnl_str = f"{float(pnl_atr):+.2f} ATR" if isinstance(pnl_atr, (int, float)) else str(pnl_atr)
                cells = [str(tid)[:8], str(side).upper(), strat_label,
                         f"{int(meta.get('margin', 0)):,} sats", pnl_str,
                         str(meta.get("last_guardian_mode", "—"))]
                self.pos_table.rows.append(ft.DataRow(
                    cells=[ft.DataCell(ft.Text(c, size=11, color=C["text"])) for c in cells]
                ))
        self.page.update()

    def _save_overrides(self):
        data = {}
        for k, tf in self.strat_fields.items():
            try:
                raw = tf.value.strip()
                data[k] = float(raw) if "." in raw else int(raw)
            except Exception:
                data[k] = tf.value
        data["_updated"] = datetime.now().isoformat()
        ok = safe_save_json(OVERRIDES_FILE, data)
        self._notify("Overrides guardados" if ok else "Error al guardar", C["green"] if ok else C["red"])

    def _reset_overrides(self):
        defaults = {
            "MAX_CONCURRENT_TRADES": "5", "RISK_NORMAL": "0.12", "RISK_WARMUP": "0.06",
            "SOFT_EXPOSURE_LIMIT": "0.38", "ENTRY_COOLDOWN_SEC": "40",
            "RECOVERY_ENTER_DD": "0.095", "RECOVERY_EXIT_DD": "0.065", "RECOVERY_RISK_MULT": "0.50",
            "MAX_DRAWDOWN_PCT": "0.16", "CONSECUTIVE_LOSS_LIMIT": "4",
            "MIN_LEVERAGE": "15", "MAX_LEVERAGE": "95", "TAKER_FEE": "0.001",
            "ALPHA_INTERMEDIATE": "0.020", "WARMUP_TRADES": "20",
        }
        for k, v in defaults.items():
            if k in self.strat_fields:
                self.strat_fields[k].value = v
        self._notify("Defaults restaurados", C["accent2"])
        self.page.update()

    def _clear_logs(self):
        self.log_lines.clear()
        self.log_list.controls.clear()
        self.page.update()

    def _on_refresh_change(self, e):
        self.refresh_sec = float(e.control.value)


def main(page: ft.Page):
    SniperApp(page)


if __name__ == "__main__":
    is_termux = "com.termux" in os.environ.get("PREFIX", "") or not os.environ.get("DISPLAY")
    port = int(os.environ.get("SNIPER_UI_PORT", "8550"))
    if is_termux:
        print(f"LNM Sniper Command Center -> http://127.0.0.1:{port}")
        ft.app(main)
    else:
        ft.app(target=main)
