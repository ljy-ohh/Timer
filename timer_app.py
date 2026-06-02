from __future__ import annotations

import json
import queue
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Any

import tkinter as tk
from tkinter import simpledialog

import ttkbootstrap as tb
from ttkbootstrap.constants import *


@dataclass(frozen=True)
class Stage:
    label: str
    seconds: int


def _fmt_mmss(total_seconds: int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    m, s = divmod(int(total_seconds), 60)
    return f"{m:02d}:{s:02d}"


def _sidecar_dir() -> Path:
    """Directory next to exe/script (portable mode)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _user_config_dir() -> Path:
    """Per-user config directory (default).

    This avoids leaving config.json next to the exe (e.g. Desktop).
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "ZuotiTimer"
        return Path.home() / "AppData" / "Roaming" / "ZuotiTimer"
    # Fallback for non-Windows
    return Path.home() / ".config" / "ZuotiTimer"


def _config_path() -> Path:
    # Optional portable mode:
    # set env TIMER_PORTABLE=1 to read/write config.json beside the exe.
    if os.environ.get("TIMER_PORTABLE", "").strip() == "1":
        return _sidecar_dir() / "config.json"
    return _user_config_dir() / "config.json"


def _resource_path(filename: str) -> Path:
    """Get path to a bundled resource (PyInstaller onefile compatible)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve() / filename
    return Path(__file__).resolve().parent / filename


def _find_icon_path() -> Path | None:
    # Prefer bundled my.ico, then sidecar next to exe/script.
    for p in (
        _resource_path("my.ico"),
        _sidecar_dir() / "my.ico",
    ):
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return None


DEFAULT_STAGES: List[Stage] = [
    Stage("读题阶段", 20),
    Stage("第一题", 50),
    Stage("第二题", 50),
    Stage("第三题", 50),
    Stage("第四题", 50),
    Stage("第五题", 50),
]


def _parse_stages(raw: Any) -> List[Stage]:
    stages: List[Stage] = []
    if not isinstance(raw, list):
        return DEFAULT_STAGES
    for item in raw:
        if isinstance(item, Stage):
            if int(item.seconds) > 0:
                stages.append(Stage(label=str(item.label).strip() or "未命名阶段", seconds=int(item.seconds)))
            continue
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip() or "未命名阶段"
        try:
            seconds = int(item.get("seconds", 0))
        except Exception:
            seconds = 0
        if seconds <= 0:
            continue
        stages.append(Stage(label=label, seconds=seconds))
    return stages or DEFAULT_STAGES


def _stages_to_json(stages: List[Stage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in stages:
        if isinstance(s, Stage):
            out.append({"label": s.label, "seconds": int(s.seconds)})
        elif isinstance(s, dict):
            # Be forgiving: if callers accidentally provide dict stages.
            label = str(s.get("label", "")).strip() or "未命名阶段"
            try:
                seconds = int(s.get("seconds", 0))
            except Exception:
                seconds = 0
            if seconds > 0:
                out.append({"label": label, "seconds": seconds})
    return out


def _new_profile_id(existing: set[str]) -> str:
    while True:
        pid = f"p{uuid.uuid4().hex[:8]}"
        if pid not in existing:
            return pid


def _normalize_profiles(raw_profiles: Any) -> list[dict[str, Any]]:
    """Return profiles as a list of dicts with keys: id, name, stages (List[Stage])."""
    profiles: list[dict[str, Any]] = []

    if isinstance(raw_profiles, dict):
        # Accept dict-based schema: {profile_id: {name, stages}}
        for pid, pdata in raw_profiles.items():
            if not isinstance(pdata, dict):
                continue
            profiles.append({
                "id": str(pid).strip() or "",
                "name": str(pdata.get("name", "")).strip() or "未命名模块",
                "stages": _parse_stages(pdata.get("stages", [])),
            })
    elif isinstance(raw_profiles, list):
        for p in raw_profiles:
            if not isinstance(p, dict):
                continue
            profiles.append({
                "id": str(p.get("id", "")).strip() or "",
                "name": str(p.get("name", "")).strip() or "未命名模块",
                "stages": _parse_stages(p.get("stages", [])),
            })
    else:
        return [{"id": "default", "name": "默认", "stages": DEFAULT_STAGES[:]}]

    # Ensure unique, non-empty ids.
    seen: set[str] = set()
    for p in profiles:
        pid = str(p.get("id", "")).strip()
        if not pid or pid in seen:
            pid = _new_profile_id(seen)
            p["id"] = pid
        seen.add(pid)

    return profiles or [{"id": "default", "name": "默认", "stages": DEFAULT_STAGES[:]}]


def _default_app_config() -> dict[str, Any]:
    return {
        "active_profile_id": "default",
        "profiles": [
            {"id": "default", "name": "默认", "stages": DEFAULT_STAGES[:]},
        ],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        # If we cannot create the directory, we still try to write and let it fail loudly.
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_app_config() -> dict[str, Any]:
    """Load config supporting both old and new schemas.

    New schema:
      {"active_profile_id": str, "profiles": [{"id", "name", "stages": [...]}, ...]}

    Old schema:
      {"stages": [...]}
    """

    path = _config_path()
    if not path.exists():
        return _default_app_config()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_app_config()

        # New schema
        if "profiles" in data:
            profiles = _normalize_profiles(data.get("profiles"))
            active = str(data.get("active_profile_id", "")).strip()
            active_ids = {p["id"] for p in profiles}
            if active not in active_ids:
                active = profiles[0]["id"]
            return {"active_profile_id": active, "profiles": profiles}

        # Old schema -> migrate (wrap in one profile)
        if "stages" in data:
            migrated = {
                "active_profile_id": "default",
                "profiles": [
                    {
                        "id": "default",
                        "name": "默认",
                        "stages": _parse_stages(data.get("stages", [])),
                    }
                ],
            }
            # Best-effort backup once.
            try:
                bak = path.with_suffix(path.suffix + ".bak")
                if not bak.exists():
                    bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
            # Persist migration so future saves don't wipe schema.
            try:
                save_app_config(migrated)
            except Exception:
                # If migration write fails, still run with migrated in-memory config.
                pass
            return migrated

        return _default_app_config()
    except Exception:
        return _default_app_config()


def save_app_config(cfg: dict[str, Any]) -> None:
    path = _config_path()

    profiles = _normalize_profiles(cfg.get("profiles"))
    active = str(cfg.get("active_profile_id", "")).strip()
    active_ids = {p["id"] for p in profiles}
    if active not in active_ids:
        active = profiles[0]["id"]

    payload = {
        "active_profile_id": active,
        "profiles": [
            {
                "id": p["id"],
                "name": str(p.get("name", "")).strip() or "未命名模块",
                "stages": _stages_to_json(
                    p.get("stages") if isinstance(p.get("stages"), list) else []
                ),
            }
            for p in profiles
        ],
    }
    _atomic_write_json(path, payload)


def _get_profile_by_id(cfg: dict[str, Any], profile_id: str) -> dict[str, Any] | None:
    for p in cfg.get("profiles", []) if isinstance(cfg.get("profiles"), list) else []:
        if isinstance(p, dict) and str(p.get("id", "")).strip() == profile_id:
            return p
    return None


def _get_active_profile(cfg: dict[str, Any]) -> dict[str, Any]:
    profiles = cfg.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        cfg.update(_default_app_config())
        profiles = cfg["profiles"]
    active_id = str(cfg.get("active_profile_id", "")).strip()
    p = _get_profile_by_id(cfg, active_id)
    if p is None:
        cfg["active_profile_id"] = str(profiles[0].get("id", "default"))
        p = profiles[0]
    if not isinstance(p.get("stages"), list) or not p["stages"]:
        p["stages"] = DEFAULT_STAGES[:]
    return p


class Speaker:
    """Background TTS worker (pyttsx3).

    If TTS init fails, we silently no-op. (UI remains functional.)
    """

    def __init__(self) -> None:
        self._q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="speaker", daemon=True)
        self._thread.start()

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._q.put(text)

    def close(self) -> None:
        self._stop.set()
        self._q.put("")

    def _run(self) -> None:
        try:
            import pyttsx3

            engine = pyttsx3.init()
            # Mildly slower speech for clarity.
            try:
                rate = engine.getProperty("rate")
                engine.setProperty("rate", max(120, int(rate) - 30))
            except Exception:
                pass
        except Exception:
            # No TTS available.
            while not self._stop.is_set():
                try:
                    self._q.get(timeout=0.5)
                except Exception:
                    pass
            return

        while not self._stop.is_set():
            try:
                text = self._q.get(timeout=0.5)
            except Exception:
                continue
            if self._stop.is_set():
                break
            text = (text or "").strip()
            if not text:
                continue
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                # Swallow TTS runtime errors.
                pass


class TimerModel:
    def __init__(self, stages: List[Stage]):
        self.set_stages(stages)

    def set_stages(self, stages: List[Stage]) -> None:
        self.stages: List[Stage] = stages[:]
        if not self.stages:
            self.stages = DEFAULT_STAGES[:]
        self.reset()

    def reset(self) -> None:
        self.running = False
        self.current_index = 0
        self._elapsed_before = 0.0
        self._start_mono: float | None = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._start_mono = time.monotonic()

    def pause(self) -> None:
        if not self.running:
            return
        now = time.monotonic()
        if self._start_mono is not None:
            self._elapsed_before += now - self._start_mono
        self._start_mono = None
        self.running = False

    def _stage_elapsed(self) -> float:
        if not self.running or self._start_mono is None:
            return self._elapsed_before
        return self._elapsed_before + (time.monotonic() - self._start_mono)

    def stage_total_seconds(self) -> int:
        return int(self.stages[self.current_index].seconds)

    def stage_elapsed_seconds(self) -> int:
        return int(self._stage_elapsed())

    def stage_remaining_seconds(self) -> int:
        return max(0, self.stage_total_seconds() - self.stage_elapsed_seconds())

    def stage_label(self) -> str:
        return self.stages[self.current_index].label

    def is_finished(self) -> bool:
        return self.current_index >= len(self.stages)

    def advance_if_needed(self) -> str | None:
        """Advance stage if time is up.

        Returns next stage label to speak (e.g. at the end of read stage, speak "第一题").
        If final stage completes, returns "完成".
        """
        if self.current_index >= len(self.stages):
            return None
        if self.stage_elapsed_seconds() < self.stage_total_seconds():
            return None

        next_index = self.current_index + 1
        if next_index >= len(self.stages):
            # Finished.
            self.current_index = len(self.stages)
            self.running = False
            self._start_mono = None
            self._elapsed_before = 0.0
            return "完成"

        # Move to next stage.
        self.current_index = next_index
        self._elapsed_before = 0.0
        self._start_mono = time.monotonic() if self.running else None
        return self.stages[self.current_index].label


class SettingsDialog(tb.Toplevel):
    def __init__(
        self,
        master: tb.Window,
        stages: List[Stage],
        icon_path: Path | None = None,
        title: str = "设置阶段",
    ):
        super().__init__(master)
        self.title(title)
        self.resizable(True, True)
        self.geometry("760x500")
        self.minsize(720, 420)
        self.transient(master)
        self.grab_set()

        if icon_path is not None:
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self._rows: List[dict[str, object]] = []  # {frame, e_label, e_sec}
        self._result: List[Stage] | None = None

        outer = tb.Frame(self, padding=14)
        outer.pack(fill=BOTH, expand=True)

        tb.Label(
            outer,
            text="修改后点击【保存并关闭】立即生效（会写入配置文件）",
            font=("Segoe UI", 10),
        ).pack(fill=X)

        header = tb.Frame(outer)
        header.pack(fill=X, pady=(10, 0))
        tb.Label(header, text="阶段文字", width=36).pack(side=LEFT, anchor=W)
        tb.Label(header, text="秒数", width=10).pack(side=LEFT, anchor=W, padx=(12, 0))
        tb.Label(header, text="操作", width=8).pack(side=RIGHT, anchor=E)

        # Bottom buttons first, so they never get pushed off-screen.
        btns = tb.Frame(outer)
        btns.pack(side=BOTTOM, fill=X, pady=(12, 0))
        tb.Button(btns, text="+ 添加阶段", bootstyle=SECONDARY, command=self._add_row).pack(side=LEFT)
        tb.Frame(btns).pack(side=LEFT, fill=X, expand=True)
        tb.Button(btns, text="取消", bootstyle=SECONDARY, command=self._cancel).pack(side=RIGHT)
        tb.Button(btns, text="保存并关闭", bootstyle=PRIMARY, command=self._save).pack(
            side=RIGHT, padx=(0, 10)
        )

        # Scrollable list occupies remaining space.
        list_container = tb.Frame(outer)
        list_container.pack(side=TOP, fill=BOTH, expand=True, pady=(8, 0))

        self._canvas = tb.Canvas(list_container, highlightthickness=0)
        self._canvas.pack(side=LEFT, fill=BOTH, expand=True)
        vsb = tb.Scrollbar(list_container, orient=VERTICAL, command=self._canvas.yview)
        vsb.pack(side=RIGHT, fill=Y)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._list = tb.Frame(self._canvas)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._list, anchor="nw")

        self._list.bind("<Configure>", self._on_list_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Shortcuts
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Control-s>", lambda _e: self._save())

        for s in stages:
            self._add_row(s.label, str(int(s.seconds)))

    def _on_list_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        # Keep inner frame width synced to canvas.
        self._canvas.itemconfigure(self._canvas_window, width=event.width)

    def _add_row(self, label: str = "", seconds: str = "30") -> None:
        row = tb.Frame(self._list)
        row.pack(fill=X, pady=4)
        e1 = tb.Entry(row)
        e1.insert(0, label)
        e1.pack(side=LEFT, fill=X, expand=True)
        e2 = tb.Entry(row, width=10)
        e2.insert(0, seconds)
        e2.pack(side=LEFT, padx=(12, 0))
        btn_del = tb.Button(
            row,
            text="删除",
            bootstyle=DANGER,
            command=lambda r=row: self._delete_row_by_frame(r),
            width=6,
        )
        btn_del.pack(side=RIGHT)
        self._rows.append({"frame": row, "e_label": e1, "e_sec": e2})

    def _delete_row_by_frame(self, frame: tb.Frame) -> None:
        idx = None
        for i, item in enumerate(self._rows):
            if item["frame"] is frame:
                idx = i
                break
        if idx is None:
            return
        item = self._rows.pop(idx)
        cast_frame = item["frame"]
        if isinstance(cast_frame, tb.Frame):
            cast_frame.destroy()

    def _cancel(self) -> None:
        self._result = None
        self.destroy()

    def _save(self) -> None:
        stages: List[Stage] = []
        for item in self._rows:
            e_label = item["e_label"]
            e_sec = item["e_sec"]
            if not isinstance(e_label, tb.Entry) or not isinstance(e_sec, tb.Entry):
                continue
            label = e_label.get().strip() or "未命名阶段"
            try:
                seconds = int(str(e_sec.get()).strip())
            except Exception:
                seconds = 0
            if seconds <= 0:
                tb.Messagebox.show_error("秒数必须为正整数", parent=self)
                return
            stages.append(Stage(label=label, seconds=seconds))

        if not stages:
            tb.Messagebox.show_error("至少需要一个阶段（秒数必须为正整数）", parent=self)
            return
        self._result = stages
        self.destroy()

    def result(self) -> List[Stage] | None:
        self.wait_window(self)
        return self._result


class App(tb.Window):
    def __init__(self):
        super().__init__(themename="flatly")
        self.title("做题计时器")
        self.geometry("640x440")
        self.minsize(600, 420)

        # Custom widget styles
        try:
            style = tb.Style()
            style.configure("Timer.Horizontal.TProgressbar", thickness=18)
        except Exception:
            pass

        self.speaker = Speaker()
        self.cfg = load_app_config()
        active = _get_active_profile(self.cfg)
        self.model = TimerModel(active["stages"])

        self._icon_path = _find_icon_path()
        if self._icon_path is not None:
            try:
                self.iconbitmap(str(self._icon_path))
            except Exception:
                pass

        self._tick_ms = 100
        self._last_spoken: str | None = None

        root = tb.Frame(self, padding=20)
        root.pack(fill=BOTH, expand=True)

        header = tb.Frame(root)
        header.pack(fill=X)

        # Profiles bar (module switch)
        profiles_bar = tb.Frame(header)
        profiles_bar.pack(fill=X)

        tb.Label(profiles_bar, text="模块：", font=("Segoe UI", 10)).pack(side=LEFT)

        canvas_wrap = tb.Frame(profiles_bar)
        canvas_wrap.pack(side=LEFT, fill=X, expand=True, padx=(8, 8))
        self._profiles_canvas = tb.Canvas(canvas_wrap, height=34, highlightthickness=0)
        self._profiles_canvas.pack(side=TOP, fill=X, expand=True)
        hsb = tb.Scrollbar(canvas_wrap, orient=HORIZONTAL, command=self._profiles_canvas.xview)
        hsb.pack(side=BOTTOM, fill=X)
        self._profiles_canvas.configure(xscrollcommand=hsb.set)
        self._profiles_inner = tb.Frame(self._profiles_canvas)
        self._profiles_canvas_window = self._profiles_canvas.create_window(
            (0, 0), window=self._profiles_inner, anchor="nw"
        )
        self._profiles_inner.bind("<Configure>", self._on_profiles_inner_configure)
        self._profiles_canvas.bind("<Configure>", self._on_profiles_canvas_configure)

        tb.Button(profiles_bar, text="管理模块", bootstyle=SECONDARY, command=self.open_profiles).pack(
            side=RIGHT
        )

        # Stage header
        top = tb.Frame(header)
        top.pack(fill=X, pady=(8, 0))
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        top.columnconfigure(2, weight=1)

        self.stage_var = tb.StringVar(value=self.model.stage_label())
        tb.Label(
            top,
            textvariable=self.stage_var,
            font=("Segoe UI", 32, "bold"),
            anchor=CENTER,
        ).grid(row=0, column=1, sticky="ew")
        tb.Button(top, text="设置", bootstyle=SECONDARY, command=self.open_settings).grid(
            row=0, column=2, sticky=E
        )

        tb.Separator(root).pack(fill=X, pady=12)

        self.time_var = tb.StringVar(value="00:00/00:00")
        tb.Label(
            root,
            textvariable=self.time_var,
            font=("Consolas", 64, "bold"),
            anchor=CENTER,
            justify=CENTER,
        ).pack(pady=(10, 14))

        self.progress = tb.Progressbar(
            root, mode="determinate", style="Timer.Horizontal.TProgressbar", bootstyle=INFO
        )
        self.progress.pack(fill=X, pady=(0, 14))

        self.hint_var = tb.StringVar(value="")
        tb.Label(root, textvariable=self.hint_var, font=("Segoe UI", 12), anchor=CENTER).pack(
            fill=X
        )

        controls = tb.Frame(root)
        controls.pack(fill=X, pady=(18, 0))
        self.btn_start = tb.Button(
            controls, text="开始", bootstyle=SUCCESS, command=self.toggle_start_pause
        )
        self.btn_start.pack(side=LEFT, fill=X, expand=True)
        tb.Button(controls, text="重置", bootstyle=WARNING, command=self.reset).pack(
            side=LEFT, fill=X, expand=True, padx=10
        )
        tb.Button(controls, text="退出", bootstyle=DANGER, command=self.on_close).pack(
            side=LEFT, fill=X, expand=True
        )

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(self._tick_ms, self._tick)
        self._render_profiles()
        self._refresh_ui()

    def open_settings(self) -> None:
        if self.model.running:
            self.model.pause()
            self.btn_start.configure(text="开始")

        active = _get_active_profile(self.cfg)
        title = f"设置阶段（{active.get('name', '未命名模块')}）"
        dlg = SettingsDialog(self, active["stages"], icon_path=self._icon_path, title=title)
        result = dlg.result()
        if result is None:
            self._refresh_ui()
            return

        active["stages"] = result
        save_app_config(self.cfg)

        self.model.set_stages(result)
        self._last_spoken = None
        self._refresh_ui()

    def open_profiles(self) -> None:
        if self.model.running:
            self.model.pause()
            self.btn_start.configure(text="开始")
        dlg = ProfilesDialog(self, self.cfg, icon_path=self._icon_path)
        result = dlg.result()
        if result is None:
            self._render_profiles()
            self._refresh_ui()
            return
        self.cfg = result
        save_app_config(self.cfg)
        active = _get_active_profile(self.cfg)
        self.model.set_stages(active["stages"])
        self.btn_start.configure(text="开始")
        self._last_spoken = None
        self._render_profiles()
        self._refresh_ui()

    def switch_profile(self, profile_id: str) -> None:
        profile_id = str(profile_id or "").strip()
        if not profile_id:
            return
        current = str(self.cfg.get("active_profile_id", "")).strip()
        if profile_id == current:
            return

        p = _get_profile_by_id(self.cfg, profile_id)
        if p is None:
            return

        if self.model.running:
            self.model.pause()
        self.btn_start.configure(text="开始")

        self.cfg["active_profile_id"] = profile_id
        save_app_config(self.cfg)

        stages = p.get("stages") if isinstance(p.get("stages"), list) else DEFAULT_STAGES[:]
        self.model.set_stages(stages)
        self._last_spoken = None
        self._render_profiles()
        self._refresh_ui()

    def _on_profiles_inner_configure(self, _event=None):
        try:
            self._profiles_canvas.configure(scrollregion=self._profiles_canvas.bbox("all"))
        except Exception:
            pass

    def _on_profiles_canvas_configure(self, event):
        try:
            self._profiles_canvas.itemconfigure(self._profiles_canvas_window, height=event.height)
        except Exception:
            pass

    def _render_profiles(self) -> None:
        # Rebuild buttons for profiles.
        for w in list(self._profiles_inner.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass

        profiles = self.cfg.get("profiles")
        if not isinstance(profiles, list):
            return
        active_id = str(self.cfg.get("active_profile_id", "")).strip()

        for p in profiles:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id", "")).strip()
            name = str(p.get("name", "")).strip() or "未命名模块"
            if not pid:
                continue
            style = PRIMARY if pid == active_id else SECONDARY
            tb.Button(
                self._profiles_inner,
                text=name,
                bootstyle=style,
                command=lambda _pid=pid: self.switch_profile(_pid),
            ).pack(side=LEFT, padx=(0, 8))

    def toggle_start_pause(self) -> None:
        if self.model.current_index >= len(self.model.stages):
            self.model.reset()
        if self.model.running:
            self.model.pause()
            self.btn_start.configure(text="开始")
        else:
            self.model.start()
            self.btn_start.configure(text="暂停")
        self._refresh_ui()

    def reset(self) -> None:
        self.model.reset()
        self.btn_start.configure(text="开始")
        self._last_spoken = None
        self._refresh_ui()

    def _refresh_ui(self) -> None:
        if self.model.current_index >= len(self.model.stages):
            self.stage_var.set("已完成")
            self.time_var.set("00:00/00:00")
            self.progress.configure(maximum=1, value=1)
            self.hint_var.set("所有阶段已完成")
            return

        total = self.model.stage_total_seconds()
        elapsed = self.model.stage_elapsed_seconds()
        self.stage_var.set(self.model.stage_label())
        self.time_var.set(f"{_fmt_mmss(elapsed)}/{_fmt_mmss(total)}")
        self.progress.configure(maximum=max(1, total), value=min(total, elapsed))
        idx = self.model.current_index + 1
        n = len(self.model.stages)
        self.hint_var.set(f"阶段 {idx}/{n}  ·  结束后将提示：{self._next_label_preview()}")

    def _next_label_preview(self) -> str:
        ni = self.model.current_index + 1
        if ni >= len(self.model.stages):
            return "完成"
        return self.model.stages[ni].label

    def _tick(self) -> None:
        if self.model.running and self.model.current_index < len(self.model.stages):
            spoken = self.model.advance_if_needed()
            if spoken and spoken != self._last_spoken:
                self._last_spoken = spoken
                self.speaker.speak(spoken)
        self._refresh_ui()
        self.after(self._tick_ms, self._tick)

    def on_close(self) -> None:
        try:
            self.speaker.close()
        finally:
            self.destroy()


class ProfilesDialog(tb.Toplevel):
    def __init__(self, master: tb.Window, cfg: dict[str, Any], icon_path: Path | None = None):
        super().__init__(master)
        self.title("管理模块")
        self.resizable(True, True)
        self.geometry("520x420")
        self.minsize(480, 380)
        self.transient(master)
        self.grab_set()

        if icon_path is not None:
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        # Work on a normalized copy.
        self._result: dict[str, Any] | None = None
        self._cfg: dict[str, Any] = {
            "active_profile_id": str(cfg.get("active_profile_id", "")).strip(),
            "profiles": _normalize_profiles(cfg.get("profiles")),
        }
        _get_active_profile(self._cfg)  # ensure valid

        outer = tb.Frame(self, padding=14)
        outer.pack(fill=BOTH, expand=True)

        tb.Label(
            outer,
            text="新增/删除/重命名模块；每个模块拥有独立的阶段设置。",
            font=("Segoe UI", 10),
        ).pack(fill=X)

        body = tb.Frame(outer)
        body.pack(fill=BOTH, expand=True, pady=(10, 0))

        left = tb.Frame(body)
        left.pack(side=LEFT, fill=BOTH, expand=True)

        self._list = tk.Listbox(left, height=12)
        self._list.pack(side=LEFT, fill=BOTH, expand=True)
        vsb = tb.Scrollbar(left, orient=VERTICAL, command=self._list.yview)
        vsb.pack(side=RIGHT, fill=Y)
        self._list.configure(yscrollcommand=vsb.set)

        right = tb.Frame(body)
        right.pack(side=RIGHT, fill=Y, padx=(12, 0))

        tb.Button(right, text="+ 新增", bootstyle=SECONDARY, command=self._add).pack(fill=X)
        tb.Button(right, text="重命名", bootstyle=SECONDARY, command=self._rename).pack(
            fill=X, pady=(8, 0)
        )
        tb.Button(right, text="删除", bootstyle=DANGER, command=self._delete).pack(
            fill=X, pady=(8, 0)
        )
        tb.Separator(right).pack(fill=X, pady=12)
        tb.Button(right, text="设为当前", bootstyle=PRIMARY, command=self._set_active).pack(
            fill=X
        )

        btns = tb.Frame(outer)
        btns.pack(fill=X, pady=(12, 0))
        tb.Button(btns, text="取消", bootstyle=SECONDARY, command=self._cancel).pack(side=RIGHT)
        tb.Button(btns, text="保存", bootstyle=PRIMARY, command=self._save).pack(
            side=RIGHT, padx=(0, 10)
        )

        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Control-s>", lambda _e: self._save())

        self._refresh_list(select_active=True)

    def _profiles(self) -> list[dict[str, Any]]:
        profiles = self._cfg.get("profiles")
        return profiles if isinstance(profiles, list) else []

    def _selected_index(self) -> int | None:
        sel = self._list.curselection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except Exception:
            return None

    def _selected_profile(self) -> dict[str, Any] | None:
        idx = self._selected_index()
        if idx is None:
            return None
        profiles = self._profiles()
        if idx < 0 or idx >= len(profiles):
            return None
        return profiles[idx]

    def _refresh_list(self, select_active: bool = False) -> None:
        self._list.delete(0, tk.END)
        active_id = str(self._cfg.get("active_profile_id", "")).strip()
        to_select: int | None = None
        for i, p in enumerate(self._profiles()):
            pid = str(p.get("id", "")).strip()
            name = str(p.get("name", "")).strip() or "未命名模块"
            prefix = "● " if pid == active_id else "  "
            self._list.insert(tk.END, f"{prefix}{name}")
            if select_active and pid == active_id:
                to_select = i
        if to_select is not None:
            try:
                self._list.selection_clear(0, tk.END)
                self._list.selection_set(to_select)
                self._list.see(to_select)
            except Exception:
                pass

    def _add(self) -> None:
        name = simpledialog.askstring("新增模块", "模块名称：", parent=self)
        if name is None:
            return
        name = str(name).strip()
        if not name:
            tb.Messagebox.show_error("模块名称不能为空", parent=self)
            return

        profiles = self._profiles()
        existing = {str(p.get("id", "")).strip() for p in profiles}
        pid = _new_profile_id(existing)

        # Default: copy current module stages (more convenient than empty)
        active = _get_active_profile(self._cfg)
        stages = active.get("stages") if isinstance(active.get("stages"), list) else DEFAULT_STAGES[:]
        profiles.append({"id": pid, "name": name, "stages": stages[:]})
        self._refresh_list(select_active=False)
        try:
            self._list.selection_clear(0, tk.END)
            self._list.selection_set(len(profiles) - 1)
            self._list.see(len(profiles) - 1)
        except Exception:
            pass

    def _rename(self) -> None:
        p = self._selected_profile()
        if p is None:
            tb.Messagebox.show_error("请先选择一个模块", parent=self)
            return
        old = str(p.get("name", "")).strip() or ""
        name = simpledialog.askstring("重命名模块", "新的模块名称：", initialvalue=old, parent=self)
        if name is None:
            return
        name = str(name).strip()
        if not name:
            tb.Messagebox.show_error("模块名称不能为空", parent=self)
            return
        p["name"] = name
        self._refresh_list(select_active=False)

    def _delete(self) -> None:
        idx = self._selected_index()
        if idx is None:
            tb.Messagebox.show_error("请先选择一个模块", parent=self)
            return
        profiles = self._profiles()
        if len(profiles) <= 1:
            tb.Messagebox.show_error("至少需要保留一个模块", parent=self)
            return
        p = profiles[idx]
        name = str(p.get("name", "")).strip() or "未命名模块"
        if tb.Messagebox.yesno(f"确定删除模块：{name}？", parent=self) != "Yes":
            return

        pid = str(p.get("id", "")).strip()
        profiles.pop(idx)

        # If deleting active, switch to first.
        if pid and pid == str(self._cfg.get("active_profile_id", "")).strip():
            self._cfg["active_profile_id"] = str(profiles[0].get("id", "default"))

        self._refresh_list(select_active=True)

    def _set_active(self) -> None:
        p = self._selected_profile()
        if p is None:
            tb.Messagebox.show_error("请先选择一个模块", parent=self)
            return
        pid = str(p.get("id", "")).strip()
        if not pid:
            return
        self._cfg["active_profile_id"] = pid
        self._refresh_list(select_active=True)

    def _cancel(self) -> None:
        self._result = None
        self.destroy()

    def _save(self) -> None:
        # Normalize again to ensure validity.
        self._cfg["profiles"] = _normalize_profiles(self._cfg.get("profiles"))
        _get_active_profile(self._cfg)
        self._result = self._cfg
        self.destroy()

    def result(self) -> dict[str, Any] | None:
        self.wait_window(self)
        return self._result


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
