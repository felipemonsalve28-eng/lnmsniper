#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LNM Sniper Command Center · Flet Edition (v7.3 Sats Net Balance Audited)
100% independiente. Lectura pasiva y atómica de q_brain_advanced_lnm.json + sniper_advanced.log.
Optimizado para Termux (Web :8550) y Desktop. Cero SyntaxWarnings.
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
    for candidate in (path, Path(f"{path}.bak")):
        try:
            if candidate.exists():
                with open(candidate, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            continue
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


def tail_log(path: Path, n: int = 180) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            block = min(fsize, max(16384, n * 190))
            f.seek(max(0, fsize - block), os.SEEK_SET)
            raw = f.read().decode("utf-8", errors="ignore")
        return raw.splitlines()[-n:]
    except Exception:
        return []


RE_PEAK_SYNC = re.compile(r"Peak=([\d,]+)\s*sats\s*\|\s*Piso Duro\s*\(75%\)=([\d,]+)\s*sats")
RE_BAL_NEW = re.compile(r"Bal:([\d,]+)")
RE_EQ_ANY = re.compile(r"Eq:([\d,]+)")
RE_LATENCY = re.compile(r"\|\s*(\d+)ms\b")
RE_EDGE_DEG = re.compile(r"\[DEGRADACIÓN DE EDGE\]\s*ID:([a-f0-9]+)\s*\|\s*Act:(\d+)\s*\|\s*Edge:([+-]?\d+\.\d+)")


def parse_log_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line or line.startswith(("nohup", "=")):
        return None
    r: Dict[str, Any] = {
        "raw": line, "ts": None, "mode": None, "state": None,
        "price": None, "equity": None, "balance": None,
        "active": None, "dd": None, "ovr": None, "latency_ms": None,
        "sync_peak": None, "sync_floor": None, "edge_warn": None,
    }
    m_sync = RE_PEAK_SYNC.search(line)
    if m_sync:
        try:
            r["sync_peak"] = int(m_sync.group(1).replace(",", ""))
            r["sync_floor"] = int(m_sync.group(2).replace(",", ""))
        except Exception:
            pass

    m_edge = RE_EDGE_DEG.search(line)
    if m_edge:
        try:
            r["edge_warn"] = (m_edge.group(1), int(m_edge.group(2)), float(m_edge.group(3)))
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

    # Soporte dual para formato nuevo (Bal:X (Y% Meta) | Eq:Z) y formato antiguo (Eq:Z (Bal:X))
    m_bal = RE_BAL_NEW.search(line)
    if m_bal:
        try:
            r["balance"] = int(m_bal.group(1).replace(",", ""))
        except Exception:
            pass
    m_eq = RE_EQ_ANY.search(line)
    if m_eq:
        try:
            r["equity"] = int(m_eq.group(1).replace(",", ""))
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
    m_lat = RE_LATENCY.search(line)
    if m_lat:
        try:
            r["latency_ms"] = int(m_lat.group(1))
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
        self.balance_hist: List[int] = []
        self.equity_hist: List[int] = []
        self.metrics: Dict[str, Any] = {
            "mode": "—", "state": "—", "equity": 0, "balance": 0,
            "realized_pnl": 0, "fees_paid": 0, "locked_margin": 0,
            "exposure_pct": 0.0, "floating_pnl": 0, "degraded_count": 0,
            "profit_factor": 0.0, "expectancy_trade": 0.0, "win_rate": 0.0,
            "active": "0/5", "dd": 0.0, "bal_dd": 0.0, "price": 0.0, "ovr": "OFF",
            "latency_ms": 0, "api_errors": 0, "cycles_per_min": 0.0,
            "peak": 0, "peak_balance": 0, "floor": 0, "target": 300000,
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
        p.title = "LNM Sniper v7.3 · Sats Net Balance Command Center"
        p.theme_mode = ft.ThemeMode.DARK
        p.bgcolor = C["bg"]
        p.padding = 0
        p.spacing = 0

    def _kpi_card(self, title: str, value_ref: ft.Ref[ft.Text], color: str, subtitle_ref: Optional[ft.Ref[ft.Text]] = None) -> ft.Container:
        items = [
            ft.Text(title, size=10, color=C["dim"], weight=ft.FontWeight.W_600),
            ft.Text("—", size=16, color=color, weight=ft.FontWeight.BOLD, ref=value_ref),
        ]
        if subtitle_ref is not None:
            items.append(ft.Text("—", size=10, color=C["dim"], ref=subtitle_ref))
        return ft.Container(
            content=ft.Column(items, spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=C["card"], border=ft.border.all(1, C["border"]),
            border_radius=12, padding=ft.padding.symmetric(horizontal=8, vertical=9),
            col={"xs": 6, "sm": 4, "md": 2},
        )

    def _info_row(self, label: str, value_ref: ft.Ref[ft.Text]) -> ft.Row:
        return ft.Row([
            ft.Text(label, size=12, color=C["dim"], width=155),
            ft.Text("—", size=12, color=C["text"], weight=ft.FontWeight.W_600,
                    ref=value_ref, expand=True, text_align=ft.TextAlign.RIGHT),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    def _build_ui(self):
        # 6 KPIs Superiores centrados en la Verdad Económica
        self.ref_balance = ft.Ref[ft.Text]()
        self.ref_goal_sub = ft.Ref[ft.Text]()
        self.ref_realized = ft.Ref[ft.Text]()
        self.ref_fees_sub = ft.Ref[ft.Text]()
        self.ref_equity = ft.Ref[ft.Text]()
        self.ref_float_sub = ft.Ref[ft.Text]()
        self.ref_dd = ft.Ref[ft.Text]()
        self.ref_floor_sub = ft.Ref[ft.Text]()
        self.ref_mode = ft.Ref[ft.Text]()
        self.ref_active_sub = ft.Ref[ft.Text]()
        self.ref_price = ft.Ref[ft.Text]()
        self.ref_ovr_sub = ft.Ref[ft.Text]()

        # Filas detalladas de rendimiento económico, riesgo y telemetría HFT
        self.ref_state = ft.Ref[ft.Text]()
        self.ref_target = ft.Ref[ft.Text]()
        self.ref_goal = ft.Ref[ft.Text]()
        self.ref_peak = ft.Ref[ft.Text]()
        self.ref_floor = ft.Ref[ft.Text]()
        self.ref_exposure = ft.Ref[ft.Text]()
        self.ref_pf_exp = ft.Ref[ft.Text]()
        self.ref_edge_deg = ft.Ref[ft.Text]()
        self.ref_trained = ft.Ref[ft.Text]()
        self.ref_hft_perf = ft.Ref[ft.Text]()

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
                ft.DataColumn(ft.Text("Edge / SL", color=C["accent"])),
                ft.DataColumn(ft.Text("Guardián", color=C["accent"])),
            ],
            rows=[], border=ft.border.all(1, C["border"]), border_radius=8,
            heading_row_color=C["card2"], data_row_min_height=36, column_spacing=12,
        )
        self.brain_refs = {k: ft.Ref[ft.Text]() for k in
                           ("states", "trained", "online", "epsilon", "target", "milestone", "peak", "floor")}
        self.q_list = ft.ListView(expand=True, spacing=2, padding=8)

        header = ft.Container(
            content=ft.Row([
                ft.Text("⚡ LNM SNIPER", size=17, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Text("v7.3 · Sats Netos", size=12, color=C["dim"]),
                ft.Container(expand=True),
                ft.Text("—", size=12, color=C["dim"], ref=self.ref_clock),
                ft.Text("● LIVE", size=12, weight=ft.FontWeight.BOLD, color=C["green"], ref=self.ref_live),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=C["card"], padding=ft.padding.symmetric(horizontal=14, vertical=10),
        )

        kpi_grid = ft.ResponsiveRow([
            self._kpi_card("BALANCE DISPONIBLE", self.ref_balance, C["green"], self.ref_goal_sub),
            self._kpi_card("BENEFICIO REALIZADO", self.ref_realized, C["accent"], self.ref_fees_sub),
            self._kpi_card("EQUITY PATRIMONIAL", self.ref_equity, C["text"], self.ref_float_sub),
            self._kpi_card("DRAWDOWN / PISO", self.ref_dd, C["orange"], self.ref_floor_sub),
            self._kpi_card("GUARDIÁN / ACTIVAS", self.ref_mode, C["accent2"], self.ref_active_sub),
            self._kpi_card("BTC USD / OVERRIDE", self.ref_price, C["green"], self.ref_ovr_sub),
        ], spacing=8, run_spacing=8)

        info_panel = ft.Container(
            content=ft.Column([
                ft.Text("AUDITORÍA ECONÓMICA Y RIESGO EN VIVO", size=12, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Divider(color=C["border"], height=1),
                self._info_row("Estado Táctico", self.ref_state),
                self._info_row("Meta de Acumulación", self.ref_target),
                self._info_row("Avance Real (Balance)", self.ref_goal),
                self._info_row("Peak Equity / Balance", self.ref_peak),
                self._info_row("Piso Duro Protegido", self.ref_floor),
                self._info_row("Margen / Exposición", self.ref_exposure),
                self._info_row("Profit Factor / Expect.", self.ref_pf_exp),
                self._info_row("Edge Degradado (Pos)", self.ref_edge_deg),
                self._info_row("Aprendizaje (Cierres/Onl)", self.ref_trained),
                self._info_row("Latencia / Salud API", self.ref_hft_perf),
            ], spacing=5),
            bgcolor=C["card"], border=ft.border.all(1, C["border"]),
            border_radius=12, padding=14, col={"xs": 12, "md": 5},
        )

        chart_box = ft.Container(
            content=ft.Column([
                ft.Text("ACUMULACIÓN DE BALANCE DISPONIBLE vs EQUITY (SATS)", size=12, color=C["dim"], weight=ft.FontWeight.W_600),
                ft.Text("Cargando telemetría…", size=12, color=C["accent"], ref=self.equity_chart_text, font_family="monospace"),
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
                ft.Text("POSICIONES ACTIVAS Y ESTADO DEL GUARDIÁN", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
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
                ft.Text("CEREBRO Q-LEARNING (INCLUYE NO_TRADE GREEDY)", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Container(expand=True),
                ft.ElevatedButton("↻ Recargar", on_click=lambda e: self._load_brain_ui(),
                                  bgcolor=C["accent2"], color=C["text"]),
                ft.ElevatedButton("💾 Backup", on_click=lambda e: self._backup_brain(),
                                  bgcolor=C["card2"], color=C["text"]),
            ]),
            ft.Row([brain_stat("states", "Estados Q"), brain_stat("trained", "Cierres"),
                    brain_stat("online", "Online"), brain_stat("epsilon", "Epsilon")], spacing=6),
            ft.Row([brain_stat("target", "Meta Balance"), brain_stat("milestone", "Nivel Meta"),
                    brain_stat("peak", "Peak Eq"), brain_stat("floor", "Piso Duro")], spacing=6),
            ft.Container(
                content=ft.Column([ft.Text("Top estados por visitas (evaluando acciones 0..8)", size=12, color=C["dim"]), self.q_list], expand=True),
                bgcolor=C["card"], border_radius=12, padding=10, expand=True,
            ),
        ], spacing=10, expand=True)

        strategy_params = [
            ("MAX_CONCURRENT_TRADES", "5", "Máx posiciones"),
            ("RISK_NORMAL", "0.12", "Riesgo normal"),
            ("RISK_WARMUP", "0.06", "Riesgo warmup"),
            ("SOFT_EXPOSURE_LIMIT", "0.38", "Exposición proyectada máx"),
            ("ENTRY_COOLDOWN_SEC", "35", "Cooldown entradas (s)"),
            ("RECOVERY_ENTER_DD", "0.095", "Entrar Recovery DD"),
            ("RECOVERY_EXIT_DD", "0.065", "Salir Recovery DD"),
            ("MAX_DAILY_LOSS_PCT", "0.11", "Pérdida diaria máx"),
            ("MAX_DRAWDOWN_PCT", "0.16", "Max Drawdown"),
            ("CONSECUTIVE_LOSS_LIMIT", "4", "Límite rachas"),
            ("MIN_LEVERAGE", "15", "Lev mínimo"),
            ("MAX_LEVERAGE", "95", "Lev máximo"),
            ("TAKER_FEE", "0.001", "Taker fee"),
            ("ALPHA_INTERMEDIATE", "0.018", "Alpha táctico"),
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
            ft.Text("PARÁMETROS DE ESTRATEGIA", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
            ft.Text("Referencia de configuración auditable (strategy_overrides.json)", size=11, color=C["dim"]),
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
                     ["TODO", "ERROR", "DEGRADACIÓN DE EDGE", "GUARDIÁN", "RECOVERY", "NORMAL", "IA ONLINE", "OVR:BULL", "OVR:BEAR"]],
            value="TODO", width=180, dense=True, border_color=C["border"], color=C["text"],
            on_change=lambda e: self._update_ui(),
        )
        logs_tab = ft.Column([
            ft.Row([
                ft.Text("LOG EN VIVO", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
                ft.Container(expand=True), self.log_filter_dd,
                ft.ElevatedButton("🗑 Limpiar vista", on_click=lambda e: self._clear_logs(),
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
            ft.Text("AJUSTES DEL COMMAND CENTER", size=15, weight=ft.FontWeight.BOLD, color=C["accent"]),
            ft.Container(content=ft.Column([
                self.brain_path_tf, self.log_path_tf,
                ft.Text("Intervalo de refresco (segundos)", size=12, color=C["dim"]),
                self.refresh_slider,
            ], spacing=12), bgcolor=C["card"], border_radius=12, padding=14),
            ft.Container(content=ft.Text(
                "LNM Sniper Command Center · v7.3 Audited Edition\n"
                "Fuente de verdad económica: Balance Disponible Real (Sats)\n"
                "Termux Web: http://127.0.0.1:8550  |  Desktop: ventana nativa",
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
            self.metrics["realized_pnl"] = int(bd.get("realized_net_profit_sats", 0))
            self.metrics["fees_paid"] = int(bd.get("total_fees_paid_sats", 0))

            active_dict = bd.get("active_trades", {})
            locked_m = 0
            deg_cnt = 0
            for _, meta in active_dict.items():
                if isinstance(meta, dict):
                    locked_m += int(meta.get("margin", 0))
                    if float(meta.get("last_edge_score", 0.0)) < 0.0 or int(meta.get("edge_deg_streak", 0)) > 0:
                        deg_cnt += 1
            self.metrics["locked_margin"] = locked_m
            self.metrics["degraded_count"] = deg_cnt

            # Estimar Profit Factor y Expectativa a partir de la memoria PER y ciclos
            mem = bd.get("memory", [])
            pos_r = sum(float(x[3]) for x in mem if isinstance(x, (list, tuple)) and len(x) >= 4 and float(x[3]) > 0)
            neg_r = abs(sum(float(x[3]) for x in mem if isinstance(x, (list, tuple)) and len(x) >= 4 and float(x[3]) < 0))
            self.metrics["profit_factor"] = round(pos_r / neg_r, 2) if neg_r > 0 else (round(pos_r, 2) if pos_r > 0 else 1.0)
            wins_mem = sum(1 for x in mem if isinstance(x, (list, tuple)) and len(x) >= 4 and float(x[3]) > 0)
            self.metrics["win_rate"] = round((wins_mem / len(mem)) * 100.0, 1) if mem else 0.0

        lpath = Path(self.log_path_tf.value) if hasattr(self, "log_path_tf") else LOG_FILE
        lines = tail_log(lpath, 160)
        eq_series: List[int] = []
        bal_series: List[int] = []
        clean_lines: List[str] = []
        err_cnt = 0
        recent_edge_ids = set()

        for line in lines:
            s_line = line.strip()
            if not s_line:
                continue
            clean_lines.append(s_line)
            if "[ERROR " in s_line or "[API CIRCUIT BREAKER]" in s_line:
                err_cnt += 1
            parsed = parse_log_line(s_line)
            if parsed:
                if parsed.get("sync_peak"):
                    self.metrics["peak"] = parsed["sync_peak"]
                if parsed.get("sync_floor"):
                    self.metrics["floor"] = parsed["sync_floor"]
                if parsed.get("edge_warn"):
                    recent_edge_ids.add(parsed["edge_warn"][0])
                if parsed.get("equity") is not None:
                    for k in ("ts", "mode", "state", "price", "equity", "balance", "active", "dd", "ovr", "latency_ms"):
                        if parsed.get(k) is not None:
                            self.metrics[k] = parsed[k]
                    eq_series.append(parsed["equity"])
                    if parsed["equity"] > self.metrics.get("peak", 0):
                        self.metrics["peak"] = parsed["equity"]
                if parsed.get("balance") is not None:
                    bal_series.append(parsed["balance"])
                    if parsed["balance"] > self.metrics.get("peak_balance", 0):
                        self.metrics["peak_balance"] = parsed["balance"]

        self.metrics["api_errors"] = err_cnt
        if self.metrics["degraded_count"] == 0 and recent_edge_ids and len(self.brain.get("active_trades", {})) > 0:
            self.metrics["degraded_count"] = min(len(self.brain.get("active_trades", {})), len(recent_edge_ids))

        eq_now = int(self.metrics.get("equity", 0))
        bal_now = int(self.metrics.get("balance", 0))
        locked_now = int(self.metrics.get("locked_margin", 0))
        self.metrics["floating_pnl"] = eq_now - bal_now - locked_now if eq_now > 0 and bal_now > 0 else 0
        self.metrics["exposure_pct"] = round((locked_now / max(eq_now, 1)) * 100.0, 1) if eq_now > 0 else 0.0

        pk_bal = max(self.metrics.get("peak_balance", 0), bal_now, 1)
        self.metrics["bal_dd"] = round(max(0.0, (pk_bal - bal_now) / pk_bal * 100.0), 1)

        trained_cnt = max(1, int(self.metrics.get("trained", 1)))
        self.metrics["expectancy_trade"] = round(self.metrics.get("realized_pnl", 0) / trained_cnt, 1)

        if clean_lines:
            self.log_lines = clean_lines[-140:]
        if eq_series:
            self.equity_hist = eq_series[-60:]
        if bal_series:
            self.balance_hist = bal_series[-60:]

    def _update_ui(self):
        m = self.metrics
        bal = int(m.get("balance", 0))
        eq = int(m.get("equity", 0))
        target = max(int(m.get("target", 300000)), 1)
        # CORRECCIÓN P0: Progreso de la meta calculado EXCLUSIVAMENTE sobre Balance Disponible
        goal_pct = (bal / target) * 100.0
        realized = int(m.get("realized_pnl", 0))
        fees = int(m.get("fees_paid", 0))
        fl_pnl = int(m.get("floating_pnl", 0))
        dd = float(m.get("dd", 0.0))
        mode = str(m.get("mode", "—"))
        ovr = str(m.get("ovr", "OFF"))
        p = float(m.get("price", 0.0))

        if self.ref_balance.current:
            self.ref_balance.current.value = f"{bal:,} sats"
            self.ref_balance.current.color = C["green"] if bal > 0 else C["text"]
        if self.ref_goal_sub.current:
            self.ref_goal_sub.current.value = f"Meta: {goal_pct:.2f}% (Nvl {m.get('milestone', 1)})"

        if self.ref_realized.current:
            self.ref_realized.current.value = f"{realized:+,} sats"
            self.ref_realized.current.color = C["green"] if realized >= 0 else C["red"]
        if self.ref_fees_sub.current:
            self.ref_fees_sub.current.value = f"Fees: {fees:,} sats | Exp: {m.get('expectancy_trade', 0):+.1f}/t"

        if self.ref_equity.current:
            self.ref_equity.current.value = f"{eq:,} sats"
        if self.ref_float_sub.current:
            self.ref_float_sub.current.value = f"Flotante: {fl_pnl:+,} sats"
            self.ref_float_sub.current.color = C["green"] if fl_pnl >= 0 else C["orange"]

        if self.ref_dd.current:
            self.ref_dd.current.value = f"{dd:.1f}% (Bal {m.get('bal_dd', 0.0):.1f}%)"
            self.ref_dd.current.color = C["green"] if dd < 5 else (C["orange"] if dd < 10 else C["red"])
        if self.ref_floor_sub.current:
            self.ref_floor_sub.current.value = f"Piso: {m.get('floor', 0):,} sats"

        if self.ref_mode.current:
            self.ref_mode.current.value = mode
            self.ref_mode.current.color = {"NORMAL": C["green"], "RECOVERY": C["orange"], "DEFENSIVE": C["red"]}.get(mode, C["text"])
        if self.ref_active_sub.current:
            self.ref_active_sub.current.value = f"Pos: {m.get('active', '0/5')} | Exp: {m.get('exposure_pct', 0.0):.1f}%"

        if self.ref_price.current:
            self.ref_price.current.value = f"${p:,.1f}" if p else "$—"
        if self.ref_ovr_sub.current:
            self.ref_ovr_sub.current.value = f"OVR: {ovr}"
            self.ref_ovr_sub.current.color = C["green"] if ovr == "BULL" else (C["red"] if ovr == "BEAR" else C["dim"])

        if self.ref_state.current:
            self.ref_state.current.value = str(m.get("state", "—"))[:42]
        if self.ref_target.current:
            self.ref_target.current.value = f"{target:,} sats"
        if self.ref_goal.current:
            self.ref_goal.current.value = f"{goal_pct:.2f}% ({bal:,} / {target:,})"
            self.ref_goal.current.color = C["green"]
        if self.ref_peak.current:
            self.ref_peak.current.value = f"Eq: {m.get('peak', 0):,} | Bal: {m.get('peak_balance', 0):,}"
        if self.ref_floor.current:
            self.ref_floor.current.value = f"{m.get('floor', 0):,} sats (75%/88%)"
        if self.ref_exposure.current:
            exp_pct = float(m.get("exposure_pct", 0.0))
            self.ref_exposure.current.value = f"{m.get('locked_margin', 0):,} sats ({exp_pct:.1f}% / 38%)"
            self.ref_exposure.current.color = C["red"] if exp_pct > 38.0 else (C["orange"] if exp_pct > 28.0 else C["green"])
        if self.ref_pf_exp.current:
            self.ref_pf_exp.current.value = f"PF: {m.get('profit_factor', 1.0):.2f} | WR: {m.get('win_rate', 0.0):.0f}%"
        if self.ref_edge_deg.current:
            deg = int(m.get("degraded_count", 0))
            self.ref_edge_deg.current.value = f"{deg} en vigilancia" if deg > 0 else "0 (Óptimo)"
            self.ref_edge_deg.current.color = C["orange"] if deg > 0 else C["green"]
        if self.ref_trained.current:
            self.ref_trained.current.value = f"{m.get('trained', 0)} cierres | {m.get('online', 0)} tácticos (ε={m.get('epsilon', 0):.3f})"
        if self.ref_hft_perf.current:
            lat = int(m.get("latency_ms", 0))
            errs = int(m.get("api_errors", 0))
            self.ref_hft_perf.current.value = f"{lat} ms | Errores log: {errs}"

        if self.ref_clock.current:
            self.ref_clock.current.value = datetime.now().strftime("%H:%M:%S")
        if self.ref_live.current:
            self.ref_live.current.color = C["green"] if mode != "DEFENSIVE" else C["red"]
        if self.ref_status.current:
            self.ref_status.current.value = (
                f"Bal Disponible: {bal:,} sats ({goal_pct:.2f}% Meta) · Eq: {eq:,} · "
                f"Piso: {m.get('floor', 0):,} · DD: {dd:.1f}% · Modo: {mode} · {m.get('latency_ms', 0)}ms"
            )

        if self.equity_chart_text.current:
            series = self.balance_hist[-40:] if len(self.balance_hist) >= 2 else self.equity_hist[-40:]
            if len(series) >= 2:
                mn, mx = min(series), max(series)
                span = max(mx - mn, 1)
                bars = "▁▂▃▄▅▆▇█"
                spark = "".join(bars[min(7, int((v - mn) / span * 7))] for v in series)
                self.equity_chart_text.current.value = (
                    f"Balance Spark: {spark}\n"
                    f"Bal Actual: {bal:,} sats (Mín:{mn:,} | Máx:{mx:,})  |  Equity Actual: {eq:,} sats"
                )
            else:
                self.equity_chart_text.current.value = f"Bal Disponible: {bal:,} sats  |  Equity: {eq:,} sats"

        filt = self.log_filter_dd.value if self.log_filter_dd else "TODO"
        self.log_list.controls.clear()
        for line in self.log_lines[-85:]:
            if filt == "TODO" or filt in line:
                col = C["red"] if "ERROR" in line else (C["orange"] if ("DEGRADACIÓN" in line or "RECOVERY" in line) else C["text"])
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
            ft.Text(f"{'Estado':<44} {'Vis':>5}  Mejor Acción (0..8)", size=11, color=C["dim"], font_family="monospace")
        )
        for st, vis in top:
            q = q_table.get(st, [])
            if isinstance(q, list) and len(q) >= 9:
                # Incluir Acción 0 (NO_TRADE) en la evaluación de la mejor política aprendida
                best_idx = max(range(0, 9), key=lambda i: float(q[i]))
                best_str = f"a{best_idx}:{ACTION_NAMES.get(best_idx, '')} ({float(q[best_idx]):+.3f})"
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
                      [ft.DataCell(ft.Text("—", color=C["dim"])) for _ in range(6)]
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
                edge_val = float(meta.get("last_edge_score", 0.0))
                sl_lock = "🔒SL" if meta.get("sl_locked") else "⏳SL"
                edge_str = f"{edge_val:+.2f} | {sl_lock}"
                cells = [
                    str(tid)[:8], str(side).upper(), strat_label,
                    f"{int(meta.get('margin', 0)):,} sats", pnl_str,
                    edge_str, str(meta.get("last_guardian_mode", "—"))
                ]
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
            "SOFT_EXPOSURE_LIMIT": "0.38", "ENTRY_COOLDOWN_SEC": "35",
            "RECOVERY_ENTER_DD": "0.095", "RECOVERY_EXIT_DD": "0.065",
            "MAX_DAILY_LOSS_PCT": "0.11", "MAX_DRAWDOWN_PCT": "0.16", "CONSECUTIVE_LOSS_LIMIT": "4",
            "MIN_LEVERAGE": "15", "MAX_LEVERAGE": "95", "TAKER_FEE": "0.001",
            "ALPHA_INTERMEDIATE": "0.018", "WARMUP_TRADES": "20",
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
        ft.app(main, view=ft.AppView.WEB_BROWSER, port=port, host="127.0.0.1")
    else:
        ft.app(target=main)
