"""Shirma — переключатель виртуальных фонов для OBS в трее.

OBS показывает картинку active.jpg и сам перечитывает её при изменении файла,
поэтому смена фона — это подмена файла. Камеру этот скрипт не трогает.
Фоны — любые картинки в папке backgrounds; название в меню = имя файла.
Подпапки backgrounds становятся подменю (картинки во вложенных папках попадают
в подменю своей папки верхнего уровня).
Запускается из OBS (shirma.lua) и закрывается вместе с ним.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageOps

from cameras import list_cameras
from i18n import t

# Свои картинки пользователя — отдельной папкой рядом с «Корп» и «Юмор»
CUSTOM_DIR = t("Свои", "Custom")

APP = Path(__file__).resolve().parent
DATA = APP.parent  # %LOCALAPPDATA%\Shirma
BG_DIR = DATA / "backgrounds"
ACTIVE = DATA / "active.jpg"
STATE = DATA / "state.json"
ICON_FILE = DATA / "icon.ico"
W, H = 1920, 1080
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
STARTUP_LNK = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Shirma.lnk"


def _config():
    try:
        return json.loads((DATA / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CFG = _config()
DEFAULT_BG = CFG.get("default_background", "Библиотека")
OBS = Path(CFG.get("obs", r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"))
OBS_ARGS = (f"--startvirtualcam --minimize-to-tray --disable-updater --disable-shutdown-check "
            f"--profile {CFG.get('profile', 'Shirma')} --collection {CFG.get('collection', 'Shirma')}")
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _images(files):
    return sorted((p for p in files if p.is_file() and p.suffix.lower() in IMG_EXT), key=lambda p: p.stem.lower())


def tree():
    """(картинки в корне, [(папка, её картинки)]) — то, что показывает меню «Фон»."""
    root = _images(BG_DIR.iterdir())
    folders = []
    for d in sorted((d for d in BG_DIR.iterdir() if d.is_dir()), key=lambda d: d.name.lower()):
        imgs = _images(d.rglob("*"))
        if imgs:  # пустые папки в меню не показываем
            folders.append((d.name, imgs))
    return root, folders


def backgrounds():
    root, folders = tree()
    return root + [p for _, imgs in folders for p in imgs]


def rel(path: Path) -> str:
    return path.relative_to(BG_DIR).as_posix()


def folder_snapshot():
    try:
        return tuple((rel(p), p.stat().st_mtime_ns) for p in backgrounds())
    except OSError:
        return ()


def set_active(path: Path):
    """Готовит active.jpg: любая картинка из папки → 1920x1080 «с обрезкой»."""
    img = ImageOps.fit(ImageOps.exif_transpose(Image.open(path)).convert("RGB"), (W, H), Image.LANCZOS)
    # Через временный файл и replace: OBS не должен прочитать недописанную картинку
    tmp = ACTIVE.with_suffix(".tmp")
    img.save(tmp, "JPEG", quality=92)
    os.replace(tmp, ACTIVE)
    state = read_state()
    state["active"] = rel(path)
    state["active_mtime"] = path.stat().st_mtime_ns
    write_state(state)


def import_image(src: Path):
    """Копирует картинку в папку пользовательских фонов под её же именем (оно и будет названием)."""
    stem = re.sub(r'[<>:"/\\|?*]', "_", src.stem).strip() or "Фон"
    folder = BG_DIR / CUSTOM_DIR
    folder.mkdir(parents=True, exist_ok=True)
    dst, n = folder / f"{stem}{src.suffix.lower()}", 2
    while dst.exists():
        dst, n = folder / f"{stem} ({n}){src.suffix.lower()}", n + 1
    shutil.copy2(src, dst)
    return dst


def pick_files():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    files = filedialog.askopenfilenames(parent=root, title=t("Добавить фон", "Add background"),
                                        filetypes=[(t("Картинки", "Images"), "*.jpg *.jpeg *.png *.bmp *.webp")])
    root.destroy()
    return [Path(f) for f in files]


def write_json_atomic(path: Path, data):
    """Через временный файл и replace: Lua в OBS читает файлы раз в 0.5 с и не должен
    застать их недописанными (раньше это откатывало «Своё» на «Высокое»)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# Модели сегментации плагина: (файл, подпись). Первая — рекомендуемая.
MODELS = [
    ("models/pphumanseg_fp32.with_runtime_opt.ort", ("PP-HumanSeg — рекомендуется", "PP-HumanSeg — recommended")),
    ("models/rvm_mobilenetv3_fp32.with_runtime_opt.ort",
     ("RVM — мягкий край, тяжелее (лучше без чёткой границы)", "RVM — soft edge, heavier (best without hard edge)")),
    ("models/selfie_segmentation.with_runtime_opt.ort", ("Selfie Segmentation — самая быстрая", "Selfie Segmentation — fastest")),
    ("models/selfie_multiclass_256x256.with_runtime_opt.ort",
     ("Selfie Multiclass — точнее, тяжёлая", "Selfie Multiclass — more precise, heavy")),
    ("models/mediapipe.with_runtime_opt.ort", ("MediaPipe (широкий кадр)", "MediaPipe (landscape)")),
    ("models/SINet_Softmax_simple.with_runtime_opt.ort", ("SINet", "SINet")),
]
MASK_EVERY = [(1, ("каждый кадр", "every frame")), (2, ("через кадр", "every 2nd frame")),
              (3, ("раз в 3 кадра", "every 3rd frame"))]

# Ползунки: (ключ фильтра, подпись, мин, макс, шаг, «высокое», развернуть, нужен порог).
# «Развернуть» — показываем наоборот параметру плагина, чтобы «больше» значило «лучше»:
# порог (ниже — меньше режет одежду) и доля новой маски (ниже — сильнее сглаживание).
# Запас и мягкость плагин считает на полном разрешении кадра (эрозия/расширение/размытие
# по всем пикселям каждый кадр): на 1080p большие значения давали 14 к/с вместо 30 —
# поэтому диапазоны урезаны.
TUNER = [
    ("threshold", ("Чувствительность (выше — меньше режет одежду)", "Sensitivity (higher keeps more clothing)"),
     0.2, 0.7, 0.01, 0.4, True, True),
    ("mask_expansion", ("Запас вокруг силуэта, px", "Margin around the silhouette, px"), 0, 8, 1, 4, False, True),
    ("feather", ("Мягкость края", "Edge softness"), 0.0, 0.3, 0.01, 0.2, False, True),
    ("temporal_smooth_factor", ("Плавность границы во времени (выше — меньше дрожит, но отстаёт)",
                                "Edge smoothing over time (higher jitters less but lags)"), 0.3, 1.0, 0.01, 0.5,
     True, False),
]


def mask_tuner(quality_file, read_quality_file):
    """Окно настройки маски. Пишет quality.json при каждом изменении — OBS применяет за ~0.5 с."""
    import tkinter as tk
    from tkinter import ttk
    data = read_quality_file()
    custom = dict(data.get("custom", {}))
    root = tk.Tk()
    root.title(t("Shirma — настройка маски", "Shirma — mask tuning"))
    root.attributes("-topmost", True)
    root.resizable(False, False)
    pad = {"padx": 14, "pady": 3}
    tk.Label(root, text=t("Изменения видны примерно через полсекунды — удобно держать открытым превью.",
                          "Changes apply in about half a second — keep the preview open."),
             wraplength=460, justify="left", fg="#555").pack(anchor="w", padx=14, pady=(10, 6))

    # --- модель и частота маски --------------------------------------------------
    model_labels = [t(*lbl) for _, lbl in MODELS]
    model_by_label = {t(*lbl): f for f, lbl in MODELS}
    label_by_model = {f: t(*lbl) for f, lbl in MODELS}
    tk.Label(root, text=t("Модель", "Model")).pack(anchor="w", **pad)
    model_var = tk.StringVar(value=label_by_model.get(custom.get("model_select"), model_labels[0]))
    ttk.Combobox(root, textvariable=model_var, values=model_labels, state="readonly", width=56).pack(anchor="w", padx=14)
    every_labels = [t(*lbl) for _, lbl in MASK_EVERY]
    every_by_label = {t(*lbl): n for n, lbl in MASK_EVERY}
    label_by_every = {n: t(*lbl) for n, lbl in MASK_EVERY}
    tk.Label(root, text=t("Расчёт маски (реже — легче для процессора, но граница отстаёт)",
                          "Mask update (less often is lighter for the CPU but the edge lags)")).pack(anchor="w", **pad)
    every_var = tk.StringVar(value=label_by_every.get(int(custom.get("mask_every_x_frames", 1)), every_labels[0]))
    ttk.Combobox(root, textvariable=every_var, values=every_labels, state="readonly", width=56).pack(anchor="w", padx=14)
    hard_var = tk.BooleanVar(value=bool(custom.get("enable_threshold", True)))
    tk.Checkbutton(root, text=t("Чёткая граница (порог) — без неё чувствительность, запас и мягкость не действуют",
                                "Hard edge (threshold) — without it sensitivity, margin and softness do nothing"),
                   variable=hard_var, wraplength=460, justify="left").pack(anchor="w", padx=10, pady=(8, 0))

    # --- ползунки ---------------------------------------------------------------
    vars_, scales = {}, {}
    spec = {key: (lo, hi, inv) for key, _l, lo, hi, _s, _d, inv, _n in TUNER}

    def to_display(key, value):
        lo, hi, invert = spec[key]
        return lo + hi - value if invert else value  # обратное преобразование то же

    def refresh_state():
        for key, *_rest, needs_threshold in TUNER:
            scales[key].config(state="normal" if hard_var.get() or not needs_threshold else "disabled")

    def save(*_):
        custom["model_select"] = model_by_label.get(model_var.get(), MODELS[0][0])
        custom["mask_every_x_frames"] = every_by_label.get(every_var.get(), 1)
        custom["enable_threshold"] = bool(hard_var.get())
        for key, var in vars_.items():
            custom[key] = round(to_display(key, var.get()), 3)
        out = read_quality_file()
        out.update(quality="custom", custom=custom)
        write_json_atomic(quality_file, out)
        refresh_state()

    for key, (ru, en), lo, hi, step, default, _inv, _n in TUNER:
        tk.Label(root, text=t(ru, en)).pack(anchor="w", **pad)
        cur = min(max(float(custom.get(key, default)), lo), hi)  # старые значения вне нового диапазона
        var = tk.DoubleVar(value=to_display(key, cur))
        vars_[key] = var
        scales[key] = tk.Scale(root, from_=lo, to=hi, resolution=step, orient="horizontal", length=460,
                               variable=var, showvalue=True, command=save)
        scales[key].pack(anchor="w", padx=14)
    tk.Label(root, text=t("Запас и мягкость считаются по всему кадру: на 1080p большие значения заметно "
                          "нагружают процессор — смотрите «Диагностику».",
                          "Margin and softness are computed over the whole frame: at 1080p large values load the "
                          "CPU noticeably — check Diagnostics."),
             wraplength=460, justify="left", fg="#8a5a00").pack(anchor="w", padx=14, pady=(6, 0))
    model_var.trace_add("write", save)
    every_var.trace_add("write", save)
    hard_var.trace_add("write", save)

    def reset():
        model_var.set(model_labels[0])
        every_var.set(every_labels[0])
        hard_var.set(True)
        for key, _l, _lo, _hi, _s, default, _inv, _n in TUNER:
            vars_[key].set(to_display(key, default))
        save()

    row = tk.Frame(root)
    row.pack(fill="x", padx=14, pady=12)
    tk.Button(row, text=t("Сбросить к «Высокому»", "Reset to High"), command=reset).pack(side="left")
    tk.Button(row, text=t("Готово", "Done"), command=root.destroy).pack(side="right")
    save()
    root.mainloop()


def diagnostics_text(status, samples):
    """Сводка для окна «Диагностика» и для копирования (без путей и имён)."""
    if not status or time.time() - status.get("updated", 0) > 10:
        return t("OBS не отвечает: Shirma не запущена или ещё стартует.",
                 "OBS is not responding: Shirma is not running or is still starting.")
    w, h = status.get("camera_width", 0), status.get("camera_height", 0)
    req = status.get("camera_requested", "")
    if not status.get("camera_shown"):
        cam = t("выключена — нет звонка и не открыто превью", "off — no call and no preview")
    elif w == 0 and status.get("camera_shown_for", 0) < 5:
        cam = t("включается…", "starting…")
    elif w == 0:
        cam = t("нет кадров", "no frames")
    else:
        cam = f"{w}×{h}"
    if req and req != "native":
        cam += t(f" (запрошено {req.replace('x', '×')})", f" (requested {req.replace('x', '×')})")
    if status.get("camera_mode_failed"):
        cam += t(" — разрешение кадра не поддерживается, родной режим камеры",
                 " — frame size not supported, using the camera's native mode")
    lag = "—"
    if len(samples) >= 2:
        (_, tot0, lag0), (_, tot1, lag1) = samples[0], samples[-1]
        if tot1 > tot0:
            lag = f"{100 * (lag1 - lag0) / (tot1 - tot0):.1f}%"
    canvas = status.get("canvas", "?").replace("x", "×")
    q = status.get("quality", "")
    q = q.split(":")[0] if q.startswith("custom") else q
    lines = [
        t(f"Камера: {cam}", f"Camera: {cam}"),
        t(f"Кадр (холст и виртуальная камера): {canvas}", f"Frame (canvas and virtual camera): {canvas}"),
        t(f"Рендер OBS: {status.get('render_fps', 0):.1f} к/с, кадр {status.get('frame_time_ms', 0):.1f} мс",
          f"OBS render: {status.get('render_fps', 0):.1f} fps, frame {status.get('frame_time_ms', 0):.1f} ms"),
        t(f"Опоздавшие кадры за последние 10 с: {lag}", f"Lagged frames, last 10 s: {lag}"),
        t(f"Качество маски: {q}", f"Mask quality: {q}"),
    ]
    return "\n".join(lines)


def diagnostics_window(status_file):
    import tkinter as tk
    from collections import deque
    samples = deque()  # (время, всего кадров, опоздавших) за последние 10 с
    root = tk.Tk()
    root.title(t("Shirma — диагностика", "Shirma — diagnostics"))
    root.attributes("-topmost", True)
    root.resizable(False, False)
    body = tk.Label(root, justify="left", anchor="w", font=("Segoe UI", 10), width=64)
    body.pack(padx=14, pady=(12, 4), anchor="w")
    tk.Label(root, justify="left", fg="#666", wraplength=520, text=t(
        "FPS рендера OBS — не частота обновления маски и не то, что программа звонка отправляет собеседнику.",
        "OBS render FPS is not the mask update rate and not what the call app sends to the other side.")
             ).pack(padx=14, anchor="w")
    state = {"text": ""}

    def refresh():
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        now = time.time()
        if status:
            samples.append((now, status.get("total_frames", 0), status.get("lagged_frames", 0)))
        while samples and now - samples[0][0] > 10:
            samples.popleft()
        state["text"] = diagnostics_text(status, list(samples))
        body.config(text=state["text"])
        root.after(1000, refresh)

    def copy():
        root.clipboard_clear()
        root.clipboard_append("Shirma\n" + state["text"])

    row = tk.Frame(root)
    row.pack(fill="x", padx=14, pady=12)
    tk.Button(row, text=t("Скопировать сводку", "Copy summary"), command=copy).pack(side="left")
    tk.Button(row, text=t("Закрыть", "Close"), command=root.destroy).pack(side="right")
    refresh()
    root.mainloop()


def camera_props_keeper(stop_event):
    """Запоминает настройки камеры и возвращает их, когда камера включается.

    Часть камер сбрасывает яркость, зум и т.п. при каждом открытии, а Shirma
    включает камеру только на время звонка или превью. Порядок важен: сначала
    вернуть сохранённое, и только потом начинать запоминать — иначе можно
    сохранить сброшенные значения поверх своих.
    """
    import logging
    try:
        import camprops
    except Exception:  # нет comtypes или COM недоступен — без этой функции
        logging.exception("camera settings keeper disabled")
        return
    store = DATA / "camera_props.json"
    active_since, applied_for = None, None
    last_save = 0.0
    while not stop_event.wait(1):
        try:
            status = json.loads((DATA / "status.json").read_text(encoding="utf-8"))
            cam_id = json.loads((DATA / "camera.json").read_text(encoding="utf-8")).get("id", "")
        except (OSError, ValueError):
            continue
        active = bool(status.get("camera_shown")) and status.get("camera_width", 0) > 0 \
            and time.time() - status.get("updated", 0) < 10
        cam = next((c for c in list_cameras() if c.obs_id == cam_id), None)
        if not active or cam is None:
            active_since, applied_for = None, None
            continue
        now = time.time()
        if active_since is None:
            active_since = now
        try:
            saved = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
        except ValueError:
            saved = {}
        try:
            if applied_for != cam.path and now - active_since >= 1:
                mine = saved.get(cam.path, {})
                if mine:
                    camprops.apply_settings(cam.path, mine)
                applied_for = cam.path
            elif applied_for == cam.path and now - active_since >= 4 and now - last_save >= 5:
                last_save = now
                current = {k: {"value": v["value"], "auto": v["auto"]}
                           for k, v in camprops.read_settings(cam.path).items()}
                if current and current != saved.get(cam.path):
                    saved[cam.path] = current
                    write_json_atomic(store, saved)
        except Exception:
            logging.exception("camera settings keeper")


def obs_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"],
                         capture_output=True, creationflags=NO_WINDOW).stdout
    return b"obs64.exe" in out


def set_autostart(enable):
    if enable:
        # Ярлык ведёт прямо на OBS: значок в трее OBS поднимет сам
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:VBG_LNK);"
              "$s.TargetPath=$env:VBG_OBS;$s.Arguments=$env:VBG_ARGS;$s.WorkingDirectory=$env:VBG_DIR;"
              "$s.IconLocation=$env:VBG_ICON;$s.WindowStyle=7;$s.Save()")
        env = dict(os.environ, VBG_LNK=str(STARTUP_LNK), VBG_OBS=str(OBS), VBG_ARGS=OBS_ARGS,
                   VBG_DIR=str(OBS.parent), VBG_ICON=f"{ICON_FILE},0")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, creationflags=NO_WINDOW)
    else:
        STARTUP_LNK.unlink(missing_ok=True)


def make_icon():
    """Ширма-гармошка: три створки зигзагом, на них «подменённый фон» — солнце и горы.

    Рисуем в 1024 px и сжимаем до 256: pystray кладёт картинку в ICO,
    а Windows берёт из него мелкие размеры — без запаса иконка в трее мылится.
    """
    S = 1024
    top_c, bot_c = (18, 78, 96), (38, 148, 156)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        k = y / S
        gd.line((0, y, S, y), fill=tuple(int(top_c[i] + (bot_c[i] - top_c[i]) * k) for i in range(3)) + (255,))
    tile = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile).rounded_rectangle((24, 24, S - 24, S - 24), 220, fill=255)
    img.paste(grad, (0, 0), tile)
    d = ImageDraw.Draw(img)

    # Створки: параллелограммы, через одну наклонены в разные стороны — зигзаг гармошки
    x0, x1, top, bottom, skew = 170, 854, 230, 840, 70
    wood = [(222, 170, 120, 255), (186, 128, 84, 255), (222, 170, 120, 255)]
    w = (x1 - x0) / 3
    panels = []
    for i, color in enumerate(wood):
        a, b = x0 + i * w, x0 + (i + 1) * w
        up = skew if i % 2 == 0 else -skew
        poly = [(a, top + (0 if up > 0 else -up)), (b, top + (up if up > 0 else 0)),
                (b, bottom - (0 if up > 0 else -up) * 0.4), (a, bottom - (up if up > 0 else 0) * 0.4)]
        d.polygon(poly, fill=color)
        panels.append(poly)

    # «Пейзаж» на створках — это и есть фон, который ширма подставляет за спину
    land = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(land)
    ld.ellipse((560, 330, 700, 470), fill=(255, 196, 92, 255))
    ld.polygon([(170, 700), (360, 470), (480, 600), (640, 440), (854, 690), (854, 840), (170, 840)],
               fill=(10, 52, 66, 235))
    panel_mask = Image.new("L", (S, S), 0)
    pm = ImageDraw.Draw(panel_mask)
    for poly in panels:
        pm.polygon(poly, fill=255)
    img.alpha_composite(Image.composite(land, Image.new("RGBA", (S, S), (0, 0, 0, 0)), panel_mask))

    # Швы и петли между створками
    for poly in panels:
        d.line([poly[1], poly[2]], fill=(90, 60, 40, 255), width=14)
    for x in (398, 626):
        for y in (360, 690):
            d.ellipse((x - 14, y - 14, x + 14, y + 14), fill=(240, 220, 180, 255))

    img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (S, S), 0), tile))
    return img.resize((256, 256), Image.LANCZOS)


def save_icon_file():
    """icon.ico для ярлыков — чтобы они не выглядели как ярлык OBS.

    Перерисовываем при каждом запуске: так после обновления ярлыки получают новую
    иконку (Windows может показывать старую из кэша, пока не обновит его сам).
    """
    make_icon().save(ICON_FILE, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48),
                                       (64, 64), (128, 128), (256, 256)])


def promote_tray_icon_once():
    """Показывать значок прямо на панели, а не в «^» — один раз, при первом запуске.

    Это тот же переключатель, что в «Параметры → Панель задач → Другие значки»:
    HKCU\\Control Panel\\NotifyIconSettings\\*\\IsPromoted. Запись Windows заводит
    по пути к программе, которая держит значок, — сверяемся с путём нашего процесса
    (для venv это базовый pythonw.exe). Если потом пользователь спрячет значок сам,
    второй раз не навязываемся.
    """
    import winreg
    if read_state().get("tray_promoted"):
        return
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
    me = buf.value.lower()
    root = r"Control Panel\NotifyIconSettings"
    for _ in range(15):  # запись появляется не сразу после регистрации значка
        time.sleep(2)
        promoted = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    with winreg.OpenKey(k, sub, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as s:
                        try:
                            path = winreg.QueryValueEx(s, "ExecutablePath")[0].lower()
                        except OSError:
                            continue
                        # Путь бывает вида {GUID-известной-папки}\остаток
                        tail = path.split("}\\", 1)[1] if path.startswith("{") and "}\\" in path else None
                        if path == me or (tail and me.endswith("\\" + tail)):
                            winreg.SetValueEx(s, "IsPromoted", 0, winreg.REG_DWORD, 1)
                            promoted = True
        except OSError:
            return
        if promoted:
            state = read_state()
            state["tray_promoted"] = True
            write_state(state)
            return


def ensure_active():
    """Если текущий фон удалили или перезаписали — выбрать заново."""
    files = backgrounds()
    if not files:
        return
    state = read_state()
    cur = BG_DIR / state.get("active", "")
    if cur.is_file() and cur in files:
        if cur.stat().st_mtime_ns != state.get("active_mtime"):
            set_active(cur)
        return
    # Файл переложили в другую папку — находим его по имени, а не сбрасываем на фон по умолчанию
    name = Path(state.get("active", "")).name
    moved = next((p for p in files if p.name == name), None) if name else None
    default = next((p for p in files if p.stem == DEFAULT_BG), files[0])
    set_active(moved or default)


def main():
    import logging
    logging.basicConfig(filename=DATA / "shirma.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    # Одна копия: Lua-скрипт OBS запускает нас при каждой загрузке сцены
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ShirmaTray")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    # Без этого Windows рисует меню в 100% и растягивает картинкой — мыло на 125–200%
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

    save_icon_file()
    ensure_active()

    bgmode_file = DATA / "bgmode.json"

    def get_bgmode():
        try:
            m = json.loads(bgmode_file.read_text(encoding="utf-8"))
            return m.get("mode", "image"), int(m.get("blur", 0))
        except (OSError, ValueError, TypeError):
            return "image", 0

    def set_bgmode(mode, blur=0):
        def _(icon, _item):
            write_json_atomic(bgmode_file, {"mode": mode, "blur": blur})
            icon.update_menu()
        return _

    def choose(path):
        def _(icon, _item):
            set_active(path)
            write_json_atomic(bgmode_file, {"mode": "image", "blur": 0})  # выбрали картинку — режим замены фона
            icon.update_menu()
        return _

    def items():
        cur = read_state().get("active", "")
        mode, blur = get_bgmode()
        root, folders = tree()

        def entry(p):
            return pystray.MenuItem(p.stem, choose(p), radio=True,
                                    checked=lambda _i, r=rel(p): mode == "image" and cur == r)

        yield pystray.MenuItem(t("Без фона — только камера", "No background — camera only"), set_bgmode("none"),
                               radio=True, checked=lambda _i: mode == "none")
        yield pystray.MenuItem(t("Размытие", "Blur"), pystray.Menu(*[
            pystray.MenuItem(label, set_bgmode("blur", level), radio=True,
                             checked=lambda _i, level=level: mode == "blur" and blur == level)
            for level, label in ((4, t("Лёгкое", "Light")), (8, t("Среднее", "Medium")), (14, t("Сильное", "Strong")))]),
            checked=lambda _i: mode == "blur")
        yield pystray.Menu.SEPARATOR

        for name, imgs in folders:
            # Галочка на папке подсказывает, где лежит текущий фон
            yield pystray.MenuItem(name, pystray.Menu(*[entry(p) for p in imgs]),
                                   checked=lambda _i, n=name: mode == "image" and cur.startswith(n + "/"))
        if folders and root:
            yield pystray.Menu.SEPARATOR
        for p in root:
            yield entry(p)

    def add(icon, _item):
        def work():
            added = [import_image(p) for p in pick_files()]
            if added:
                set_active(added[-1])
                icon.update_menu()
        threading.Thread(target=work, daemon=True).start()

    quality_file = DATA / "quality.json"

    def get_quality():
        try:
            q = json.loads(quality_file.read_text(encoding="utf-8")).get("quality", "high")
            return {"fast": "medium", "low": "medium"}.get(q, q)  # старые имена режимов
        except (OSError, ValueError):
            return "high"

    def read_quality_file():
        try:
            return json.loads(quality_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_quality(q):
        # Lua-скрипт в OBS перечитывает файл раз в 0.5 с и меняет фильтр на лету;
        # свои значения ползунков сохраняем, чтобы к «Своё» можно было вернуться
        def _(icon, _item):
            data = read_quality_file()
            data["quality"] = q
            write_json_atomic(quality_file, data)
            icon.update_menu()
        return _

    tuner_open = threading.Event()

    def open_tuner(icon, _item):
        if tuner_open.is_set():
            return
        tuner_open.set()

        def work():
            try:
                mask_tuner(quality_file, read_quality_file)
            finally:
                tuner_open.clear()
                icon.update_menu()
        threading.Thread(target=work, daemon=True).start()

    camera_file = DATA / "camera.json"

    def current_camera():
        try:
            return json.loads(camera_file.read_text(encoding="utf-8")).get("id", "")
        except (OSError, ValueError):
            return ""

    def choose_camera(cam):
        # Lua-скрипт в OBS перечитывает файл раз в 2 с и переключает источник на лету
        def _(icon, _item):
            write_json_atomic(camera_file, {"id": cam.obs_id, "name": cam.name})
            icon.update_menu()
        return _

    def camera_items():
        cams = list_cameras()
        if not cams:
            yield pystray.MenuItem(t("Камеры не найдены", "No cameras found"), None, enabled=False)
            return
        cur = current_camera()
        for c in cams:
            label = c.name + ("" if c.is_usb else t(" (встроенная)", " (built-in)"))
            yield pystray.MenuItem(label, choose_camera(c), radio=True,
                                   checked=lambda _i, i=c.obs_id: cur == i)

    def show_preview(icon, _item):
        # Окно-проектор откроет Lua-скрипт в OBS: ему нужен только новый номер запроса
        f = DATA / "preview.json"
        try:
            seq = json.loads(f.read_text(encoding="utf-8")).get("seq", 0) + 1
        except (OSError, ValueError):
            seq = 1
        write_json_atomic(f, {"seq": seq})

    mirror_file = DATA / "mirror.json"

    def get_mirror():
        try:
            return json.loads(mirror_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def toggle_mirror(key):
        # Lua-скрипт в OBS перечитывает файл раз в 2 с и отражает слой на лету
        def _(icon, _item):
            m = get_mirror()
            m[key] = not m.get(key, False)
            write_json_atomic(mirror_file, m)
            icon.update_menu()
        return _

    resolution_file = DATA / "resolution.json"

    def get_resolution():
        try:
            return int(json.loads(resolution_file.read_text(encoding="utf-8")).get("height", 720))
        except (OSError, ValueError, TypeError):
            return 720

    def set_resolution(hgt):
        # Lua-скрипт в OBS на секунду остановит виртуальную камеру, сменит размер кадра и запустит снова
        def _(icon, _item):
            write_json_atomic(resolution_file, {"height": hgt})
            icon.update_menu()
        return _

    diag_open = threading.Event()

    def open_diagnostics(icon, _item):
        if diag_open.is_set():
            return
        diag_open.set()

        def work():
            try:
                diagnostics_window(DATA / "status.json")
            finally:
                diag_open.clear()
        threading.Thread(target=work, daemon=True).start()

    def open_camera_settings(icon, _item):
        # Окно свойств камеры откроет Lua в OBS: включит камеру и нажмёт «Настроить видео»
        f = DATA / "camera_settings.json"
        try:
            seq = json.loads(f.read_text(encoding="utf-8")).get("seq", 0) + 1
        except (OSError, ValueError):
            seq = 1
        write_json_atomic(f, {"seq": seq})

    def toggle_autostart(icon, _item):
        set_autostart(not STARTUP_LNK.exists())
        icon.update_menu()

    def shutdown(icon, _item):
        # Сначала вежливо, чтобы OBS закрылся штатно; если не послушался — принудительно
        subprocess.run(["taskkill", "/IM", "obs64.exe"], capture_output=True, creationflags=NO_WINDOW)
        for _ in range(10):
            if not obs_running():
                break
            time.sleep(0.5)
        else:
            subprocess.run(["taskkill", "/IM", "obs64.exe", "/F"], capture_output=True, creationflags=NO_WINDOW)
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(t("Показать превью", "Show preview"), show_preview, default=True),
        pystray.MenuItem(t("Фон", "Background"), pystray.Menu(items)),
        pystray.MenuItem(t("Добавить свой фон…", "Add your own background…"), add),
        pystray.MenuItem(t("Открыть папку с фонами", "Open backgrounds folder"), lambda *_: os.startfile(BG_DIR)),
        pystray.MenuItem(t("Камера", "Camera"), pystray.Menu(camera_items)),
        pystray.MenuItem(t("Настройки", "Settings"), pystray.Menu(
            pystray.MenuItem(t("Качество маски", "Mask quality"), pystray.Menu(*[
                pystray.MenuItem(label, set_quality(q), radio=True, checked=lambda _i, q=q: get_quality() == q)
                for q, label in (("high", t("Высокое", "High")), ("medium", t("Среднее", "Medium")),
                                 ("custom", t("Своё", "Custom")))
                if q != "custom" or "custom" in read_quality_file()],
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(t("Настроить маску…", "Tune the mask…"), open_tuner))),
            pystray.MenuItem(t("Камера: яркость, зум, фокус…", "Camera: brightness, zoom, focus…"), open_camera_settings),
            pystray.MenuItem(t("Разрешение", "Resolution"), pystray.Menu(*[
                pystray.MenuItem(label, set_resolution(hgt), radio=True,
                                 checked=lambda _i, hgt=hgt: get_resolution() == hgt)
                for hgt, label in ((720, "720p (1280×720)"), (1080, "1080p (1920×1080)"))])),
            pystray.MenuItem(t("Отразить по горизонтали", "Mirror horizontally"), pystray.Menu(
                pystray.MenuItem(t("Фон", "Background"), toggle_mirror("background"),
                                 checked=lambda _i: get_mirror().get("background", False)),
                pystray.MenuItem(t("Камеру", "Camera"), toggle_mirror("camera"),
                                 checked=lambda _i: get_mirror().get("camera", False)))))),
        pystray.MenuItem(t("Диагностика…", "Diagnostics…"), open_diagnostics),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(t("Автозапуск", "Start with Windows"), toggle_autostart, checked=lambda _i: STARTUP_LNK.exists()),
        pystray.MenuItem(t("Выключить Shirma", "Quit Shirma"), shutdown),
    )
    icon = pystray.Icon("shirma", make_icon(), t("Shirma — виртуальный фон", "Shirma — virtual background"), menu)

    def camera_snapshot():
        try:
            return tuple(c.obs_id for c in list_cameras())
        except OSError:
            return ()

    def watch_folder():
        # pystray на Windows собирает меню один раз — пересобираем, когда изменилась
        # папка фонов или список камер (подключили/отключили)
        snap, cams = folder_snapshot(), camera_snapshot()
        while True:
            time.sleep(2)
            now, now_cams = folder_snapshot(), camera_snapshot()
            if now != snap:
                ensure_active()
            if now != snap or now_cams != cams:
                snap, cams = now, now_cams
                icon.update_menu()

    def watch_obs():
        # OBS закрыли из его собственного трея — уходим вместе с ним
        time.sleep(20)
        while obs_running():
            time.sleep(3)
        icon.stop()

    threading.Thread(target=watch_folder, daemon=True).start()
    threading.Thread(target=promote_tray_icon_once, daemon=True).start()
    threading.Thread(target=camera_props_keeper, args=(threading.Event(),), daemon=True).start()
    threading.Thread(target=watch_obs, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
