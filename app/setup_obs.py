"""Готовит OBS для Shirma: профиль, сцену «фон + камера с вырезанием», Lua-скрипт трея.

Ничего не запускает и камеру не открывает. Пишет только свои профиль и сцену
(имя Shirma); чужие профили и сцены OBS не трогает.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cameras import list_cameras  # noqa: E402
from i18n import t  # noqa: E402

NAME = "Shirma"
FPS = 30
RESOLUTIONS = {720: (1280, 720), 1080: (1920, 1080)}
W, H = RESOLUTIONS[720]  # переопределяется в main() по resolution.json
OBS_CFG = Path(os.environ["APPDATA"]) / "obs-studio"
# Модель — PP-HumanSeg (~7 мс): держит плечи на тёмной одежде и не тормозит видео.
# Проверено на живых звонках и отвергнуто:
#   RVM — модель матирования с мягким краем, но на тёмном пиджаке перед тёмным
#         креслом целиком теряет плечо, а маска раз в 2–3 кадра даёт шлейф;
#   selfie_segmentation — грубый край, спинка кресла просвечивает.
# Плагин считает маску синхронно в потоке видео, поэтому тяжёлые модели под
# нагрузкой от программы звонка задерживают кадры.
# Два уровня в трее — сглаживание против скорости реакции:
#   high   — устойчивая граница: сильнее сглаживание во времени и контура;
#   medium — граница быстрее догоняет движение, но подрагивает.
# temporal_smooth_factor — доля новой маски; с порогом плагин не опускает её ниже
# threshold (0.5), так что 0.5 — максимум сглаживания. mask_expansion > 0 расширяет
# человека наружу (растушёвка плагина сначала съедает край). Пропуск «похожих»
# кадров выключен — из-за него маска дёргалась.
_PPHUMAN = {"model_select": "models/pphumanseg_fp32.with_runtime_opt.ort", "mask_every_x_frames": 1,
            "numThreads": 2, "enable_threshold": True, "threshold": 0.5}
QUALITY = {
    # Порог 0.4: при 0.5 тёмные края одежды, где модель уверена меньше чем наполовину, уходили в фон
    "high": {**_PPHUMAN, "threshold": 0.4, "temporal_smooth_factor": 0.5, "smooth_contour": 1.0,
             "mask_expansion": 4.0, "feather": 0.2},
    "medium": {**_PPHUMAN, "temporal_smooth_factor": 0.85, "smooth_contour": 0.5, "mask_expansion": 2.0, "feather": 0.15},
}
DEFAULT_QUALITY = "high"
FILTER_SETTINGS = {
    "useGPU": "cpu",
    "enable_image_similarity": False,
    "threshold": 0.5,
    "smooth_contour": 0.5,
    "feather": 0.05,
    **QUALITY[DEFAULT_QUALITY],
}
LUA = r"""-- Shirma: поднимает свой значок в трее, убирает значок OBS (в трее остаётся один)
-- и на лету применяет выбор из трея: качество (quality.json), камеру (camera.json),
-- превью (preview.json -> окно-проектор OBS).
-- ShellExecuteW через FFI: os.execute мигает консолью и не понимает кириллицу в путях.
-- quality.json читаем через obs_data_create_from_json_file: io.open не открывает пути с кириллицей.
obs = obslua
local ffi = require("ffi")
ffi.cdef[[
int MultiByteToWideChar(unsigned int cp, unsigned long flags, const char* src, int cb, wchar_t* dst, int cch);
void* ShellExecuteW(void* hwnd, const wchar_t* op, const wchar_t* file, const wchar_t* params, const wchar_t* dir, int show);
typedef struct { unsigned long Data1; unsigned short Data2, Data3; unsigned char Data4[8]; } SHIRMA_GUID;
typedef struct {
	unsigned long cbSize; void* hWnd; unsigned int uID; unsigned int uFlags; unsigned int uCallbackMessage;
	void* hIcon; wchar_t szTip[128]; unsigned long dwState; unsigned long dwStateMask; wchar_t szInfo[256];
	unsigned int uVersion; wchar_t szInfoTitle[64]; unsigned long dwInfoFlags; SHIRMA_GUID guidItem; void* hBalloonIcon;
} SHIRMA_NOTIFYICONDATAW;
int Shell_NotifyIconW(unsigned long msg, SHIRMA_NOTIFYICONDATAW* data);
void* FindWindowExW(void* parent, void* after, const wchar_t* cls, const wchar_t* title);
int GetClassNameW(void* hwnd, wchar_t* buf, int len);
unsigned long GetWindowThreadProcessId(void* hwnd, unsigned long* pid);
unsigned long GetCurrentProcessId(void);
void* OpenMutexW(unsigned long access, int inherit, const wchar_t* name);
int CloseHandle(void* h);
void* OpenFileMappingW(unsigned long access, int inherit, const wchar_t* name);
typedef struct { unsigned long Attributes, GrantedAccess, HandleCount, PointerCount, Reserved[10]; } SHIRMA_OBI;
long NtQueryObject(void* h, int cls, void* info, unsigned long len, unsigned long* ret);
int IsWindowVisible(void* hwnd);
int GetSystemMetrics(int index);
int SetWindowPos(void* hwnd, void* after, int x, int y, int cx, int cy, unsigned int flags);
typedef struct { long left, top, right, bottom; } SHIRMA_RECT;
int GetWindowRect(void* hwnd, SHIRMA_RECT* r);
int GetClientRect(void* hwnd, SHIRMA_RECT* r);
int SystemParametersInfoW(unsigned int action, unsigned int param, void* pv, unsigned int ini);
]]
local kernel32 = ffi.load("kernel32")
local shell32 = ffi.load("shell32")
local user32 = ffi.load("user32")
local ntdll = ffi.load("ntdll")

local function w(s)
	local n = kernel32.MultiByteToWideChar(65001, 0, s, -1, nil, 0)
	local buf = ffi.new("wchar_t[?]", n)
	kernel32.MultiByteToWideChar(65001, 0, s, -1, buf, n)
	return buf
end

local PYTHONW = [==[{pythonw}]==]
local SCRIPT = [==[{script}]==]
local DIR = [==[{dir}]==]
local QUALITY_FILE = DIR .. "\\quality.json"
local CAMERA_FILE = DIR .. "\\camera.json"
local PREVIEW_FILE = DIR .. "\\preview.json"
local MIRROR_FILE = DIR .. "\\mirror.json"
local RESOLUTION_FILE = DIR .. "\\resolution.json"
local RESOLUTIONS = {[720] = {1280, 720}, [1080] = {1920, 1080}}
local PROFILES = {profiles}
local applied = nil
-- Плагин читает эти ключи как целые; остальные числа — как дробные (mask_expansion тоже)
local INT_KEYS = {mask_every_x_frames = true, numThreads = true}

-- Тонкая настройка из трея: {"quality": "custom", "custom": {threshold, mask_expansion,
-- temporal_smooth_factor, feather, smooth_contour}} поверх «высокого»
local CUSTOM_KEYS = {"threshold", "mask_expansion", "temporal_smooth_factor", "feather", "smooth_contour"}

local function read_quality()
	local d = obs.obs_data_create_from_json_file(QUALITY_FILE)
	if d == nil then return "{default}", PROFILES["{default}"] end
	local q = obs.obs_data_get_string(d, "quality")
	if q == "fast" or q == "low" then q = "medium" end  -- старые имена режимов
	if q == "custom" then
		local profile = {}
		for k, v in pairs(PROFILES.high) do profile[k] = v end
		local c = obs.obs_data_get_obj(d, "custom")
		local sig = "custom"
		if c ~= nil then
			for _, k in ipairs(CUSTOM_KEYS) do
				if obs.obs_data_has_user_value(c, k) then profile[k] = obs.obs_data_get_double(c, k) end
				sig = sig .. ":" .. tostring(profile[k])
			end
			obs.obs_data_release(c)
		end
		obs.obs_data_release(d)
		return sig, profile
	end
	obs.obs_data_release(d)
	if PROFILES[q] == nil then q = "{default}" end
	return q, PROFILES[q]
end

local function apply_quality()
	local q, profile = read_quality()
	if q == applied then return end
	local src = obs.obs_get_source_by_name("Камера")
	if src == nil then return end
	local filter = obs.obs_source_get_filter_by_name(src, "Удаление фона")
	if filter ~= nil then
		local data = obs.obs_data_create()
		for key, value in pairs(profile) do
			if type(value) == "string" then
				obs.obs_data_set_string(data, key, value)
			elseif type(value) == "boolean" then
				obs.obs_data_set_bool(data, key, value)
			elseif INT_KEYS[key] then
				obs.obs_data_set_int(data, key, value)
			else
				obs.obs_data_set_double(data, key, value)
			end
		end
		obs.obs_source_update(filter, data)
		obs.obs_data_release(data)
		obs.obs_source_release(filter)
		applied = q
	end
	obs.obs_source_release(src)
end

local applied_cam = nil
local cam_switched_at = nil
local camera_shown = nil

local function canvas_size()
	local ovi = obs.obs_video_info()
	obs.obs_get_video_info(ovi)
	return ovi.base_width, ovi.base_height
end

local function canvas_res()
	local cw, ch = canvas_size()
	return cw .. "x" .. ch
end

local function apply_camera()
	local d = obs.obs_data_create_from_json_file(CAMERA_FILE)
	if d == nil then return end
	local id = obs.obs_data_get_string(d, "id")
	obs.obs_data_release(d)
	if id == "" then return end
	local src = obs.obs_get_source_by_name("Камера")
	if src == nil then return end
	if applied_cam == nil then
		local cur = obs.obs_source_get_settings(src)
		applied_cam = obs.obs_data_get_string(cur, "video_device_id")
		obs.obs_data_release(cur)
	end
	if id ~= applied_cam then
		local data = obs.obs_data_create()
		obs.obs_data_set_string(data, "video_device_id", id)
		obs.obs_data_set_string(data, "last_video_device_id", id)
		obs.obs_data_set_int(data, "res_type", 1)
		obs.obs_data_set_string(data, "resolution", canvas_res())
		obs.obs_source_update(src, data)
		obs.obs_data_release(data)
		applied_cam = id
		cam_switched_at = os.time()
	elseif cam_switched_at ~= nil and os.time() - cam_switched_at >= 4 then
		-- Камера не умеет разрешение холста и молчит — откатываемся на её родное
		-- (только если она сейчас показана: спрятанная по требованию тоже даёт 0)
		if camera_shown and obs.obs_source_get_width(src) == 0 then
			local data = obs.obs_data_create()
			obs.obs_data_set_int(data, "res_type", 0)
			obs.obs_source_update(src, data)
			obs.obs_data_release(data)
		end
		cam_switched_at = nil
	end
	obs.obs_source_release(src)
end

-- Значок OBS в трее. Без него OBS не прячет окно при старте (так устроен OBS:
-- скрытый запуск требует включённого значка), поэтому значок не выключаем
-- настройкой, а снимаем после старта. Qt держит его на скрытом верхнеуровневом
-- окне класса Qt<версия>TrayIconMessageWindowClass, uID = 0 (проверено на Qt 6.11).
-- Чужие значки не трогаем — сверяем PID.
local NIM_DELETE = 2

local function class_of(hwnd)
	local buf = ffi.new("wchar_t[128]")
	local n = user32.GetClassNameW(hwnd, buf, 128)
	local out = {}
	for i = 0, n - 1 do
		local c = buf[i]
		out[#out + 1] = c < 128 and string.char(c) or "?"
	end
	return table.concat(out)
end

local function hide_obs_tray_icon()
	local pid = kernel32.GetCurrentProcessId()
	local hwnd = nil
	local removed = false
	while true do
		hwnd = user32.FindWindowExW(nil, hwnd, nil, nil)  -- верхнеуровневые окна, включая скрытые
		if hwnd == nil then break end
		local owner = ffi.new("unsigned long[1]")
		user32.GetWindowThreadProcessId(hwnd, owner)
		if owner[0] == pid and class_of(hwnd):find("TrayIconMessageWindow", 1, true) then
			local nid = ffi.new("SHIRMA_NOTIFYICONDATAW")
			nid.cbSize = ffi.sizeof(nid)
			nid.hWnd = hwnd
			nid.uID = 0
			if shell32.Shell_NotifyIconW(NIM_DELETE, nid) ~= 0 then removed = true end
		end
	end
	return removed
end

-- Превью: трей пишет в preview.json новый номер запроса — открываем окно-проектор
local preview_seq = nil
local preview_resize_until = 0

-- Окно-проектор: внутренняя область = размер кадра в физических пикселях (1:1,
-- без масштабирования). Рамку и заголовок берём у самого окна (окно минус клиент).
-- Если не влезает в рабочую область экрана — уменьшаем с сохранением 16:9.
-- Ищем видимое окно нашего OBS (главное скрыто).
local SPI_GETWORKAREA = 0x0030
local SWP_NOZORDER_NOACTIVATE = 0x0014

local function resize_preview()
	if os.time() > preview_resize_until then return end
	local pid = kernel32.GetCurrentProcessId()
	local hwnd = nil
	while true do
		hwnd = user32.FindWindowExW(nil, hwnd, nil, nil)
		if hwnd == nil then return end
		local owner = ffi.new("unsigned long[1]")
		user32.GetWindowThreadProcessId(hwnd, owner)
		if owner[0] == pid and user32.IsWindowVisible(hwnd) ~= 0 then
			local wr, cr, wa = ffi.new("SHIRMA_RECT"), ffi.new("SHIRMA_RECT"), ffi.new("SHIRMA_RECT")
			user32.GetWindowRect(hwnd, wr)
			user32.GetClientRect(hwnd, cr)
			user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, wa, 0)
			local frame_w = (wr.right - wr.left) - (cr.right - cr.left)
			local frame_h = (wr.bottom - wr.top) - (cr.bottom - cr.top)
			local cw, ch = canvas_size()
			local area_w, area_h = wa.right - wa.left, wa.bottom - wa.top
			local k = math.min(1, (area_w - frame_w) / cw, (area_h - frame_h) / ch)
			local ww = math.floor(cw * k) + frame_w
			local wh = math.floor(ch * k) + frame_h
			user32.SetWindowPos(hwnd, nil, wa.left + math.floor((area_w - ww) / 2),
				wa.top + math.floor((area_h - wh) / 2), ww, wh, SWP_NOZORDER_NOACTIVATE)
			preview_resize_until = 0
			return
		end
	end
end

local function check_preview()
	local d = obs.obs_data_create_from_json_file(PREVIEW_FILE)
	if d == nil then return end
	local seq = obs.obs_data_get_int(d, "seq")
	obs.obs_data_release(d)
	if preview_seq ~= nil and seq ~= preview_seq then
		-- Проектор сцены, а не «Предпросмотр»: у скрытого OBS предпросмотр выключен,
		-- и окно «Предпросмотр» показывало картинку в низком разрешении
		obs.obs_frontend_open_projector("Scene", -1, "", "Shirma")
		preview_resize_until = os.time() + 5
	end
	preview_seq = seq
end

-- Значок Shirma — единственный способ выключить скрытый OBS, поэтому если он
-- закрылся (упал), поднимаем его снова. Жив ли он — по его мьютексу.
local TRAY_MUTEX = "Local\\ShirmaTray"
local SYNCHRONIZE = 0x00100000
local tray_started_at = 0

local function launch_tray()
	shell32.ShellExecuteW(nil, w("open"), w(PYTHONW), w('"' .. SCRIPT .. '"'), w(DIR), 1)
	tray_started_at = os.time()
end

local function ensure_tray()
	if os.time() - tray_started_at < 15 then return end
	local h = kernel32.OpenMutexW(SYNCHRONIZE, 0, w(TRAY_MUTEX))
	if h == nil then
		launch_tray()
	else
		kernel32.CloseHandle(h)
	end
end

-- Камера по требованию. Программа, которая берёт кадры виртуальной камеры, держит
-- открытым дескриптор общей памяти OBSVirtualCamVideo — только пока идёт показ
-- (модуль камеры, загруженный ради списка устройств, дескриптор не держит).
-- Число дескрипторов объекта = OBS + наш запрос + читатели; запрос ~10 мкс.
-- Окно превью — любое видимое окно нашего OBS (главное окно скрыто).
local VCAM_NAME = w("OBSVirtualCamVideo")
local FILE_MAP_READ = 4
local KEEP_ON = 5  -- с, чтобы камера не мигала при переподключении программы звонка
local wanted_at = 0

local function vcam_readers()
	local h = kernel32.OpenFileMappingW(FILE_MAP_READ, 0, VCAM_NAME)
	if h == nil then return 0 end
	local info = ffi.new("SHIRMA_OBI")
	local status = ntdll.NtQueryObject(h, 0, info, ffi.sizeof(info), nil)
	kernel32.CloseHandle(h)
	if status ~= 0 then return 0 end
	return math.max(0, info.HandleCount - 2)
end

local function own_window_visible()
	local pid = kernel32.GetCurrentProcessId()
	local hwnd = nil
	while true do
		hwnd = user32.FindWindowExW(nil, hwnd, nil, nil)
		if hwnd == nil then return false end
		local owner = ffi.new("unsigned long[1]")
		user32.GetWindowThreadProcessId(hwnd, owner)
		if owner[0] == pid and user32.IsWindowVisible(hwnd) ~= 0 then return true end
	end
end

local function set_camera_shown(show)
	local scene_src = obs.obs_get_source_by_name("Shirma")
	if scene_src == nil then return false end
	local scene = obs.obs_scene_from_source(scene_src)
	local item = scene and obs.obs_scene_find_source(scene, "Камера")
	if item ~= nil then obs.obs_sceneitem_set_visible(item, show) end
	obs.obs_source_release(scene_src)
	return item ~= nil
end

local function camera_on_demand()
	apply_quality()  -- раз в 0.5 с: ползунки тонкой настройки откликаются быстро
	local now = os.time()
	if vcam_readers() > 0 or own_window_visible() then wanted_at = now end
	local show = now - wanted_at < KEEP_ON
	if show ~= camera_shown and set_camera_shown(show) then
		camera_shown = show
		print(show and "camera on" or "camera off")
	end
end

-- Отражение по горизонтали из mirror.json: {"background": bool, "camera": bool}
local MIRROR_ITEMS = {background = "Фон", camera = "Камера"}
local mirrored = {}

local function apply_mirror()
	local d = obs.obs_data_create_from_json_file(MIRROR_FILE)
	local want = {background = false, camera = false}
	if d ~= nil then
		want.background = obs.obs_data_get_bool(d, "background")
		want.camera = obs.obs_data_get_bool(d, "camera")
		obs.obs_data_release(d)
	end
	local scene_src = nil
	for key, name in pairs(MIRROR_ITEMS) do
		if mirrored[key] ~= want[key] then
			scene_src = scene_src or obs.obs_get_source_by_name("Shirma")
			local scene = scene_src and obs.obs_scene_from_source(scene_src)
			local item = scene and obs.obs_scene_find_source(scene, name)
			if item ~= nil then
				local scale = obs.vec2()
				obs.obs_sceneitem_get_scale(item, scale)
				local mag = math.abs(scale.x)
				scale.x = want[key] and -mag or mag
				obs.obs_sceneitem_set_scale(item, scale)
				mirrored[key] = want[key]
			end
		end
	end
	if scene_src ~= nil then obs.obs_source_release(scene_src) end
end

-- Разрешение из resolution.json ({"height": 720|1080}).
-- Таймеры Lua выполняются в графическом потоке OBS, а obs_frontend_reset_video
-- пересоздаёт графику — из таймера это роняло OBS (краш в obs_scene_find_source
-- сразу после reset). Поэтому таймер только просит остановить виртуальную камеру,
-- а размер меняется в обработчике события VIRTUALCAM_STOPPED — он в потоке интерфейса.
-- Виртуальная камера у нас всегда запущена; если вдруг нет — сначала запускаем её,
-- чтобы получить пару событий STARTED/STOPPED.
local res_pending = nil

local function wanted_height()
	local d = obs.obs_data_create_from_json_file(RESOLUTION_FILE)
	if d == nil then return nil end
	local hgt = obs.obs_data_get_int(d, "height")
	obs.obs_data_release(d)
	return RESOLUTIONS[hgt] and hgt or nil
end

-- Слои фона и камеры по размеру кадра. Вызывается и после смены разрешения, и на
-- каждом тике как страховка: если кадр поменялся без нас (или OBS упал между
-- записью профиля и подгонкой), слои всё равно встанут правильно.
local function fit_scene()
	local cw, ch = canvas_size()
	local scene_src = obs.obs_get_source_by_name("Shirma")
	if scene_src == nil then return end
	local scene = obs.obs_scene_from_source(scene_src)
	local changed = false
	if scene ~= nil then
		for _, name in ipairs({"Фон", "Камера"}) do
			local item = obs.obs_scene_find_source(scene, name)
			if item ~= nil then
				local b = obs.vec2()
				obs.obs_sceneitem_get_bounds(item, b)
				if b.x ~= cw or b.y ~= ch then
					b.x = cw; b.y = ch
					obs.obs_sceneitem_set_bounds(item, b)
					local pos = obs.vec2(); pos.x = cw / 2; pos.y = ch / 2
					obs.obs_sceneitem_set_pos(item, pos)
					changed = true
				end
			end
		end
	end
	obs.obs_source_release(scene_src)
	if changed then
		local cam = obs.obs_get_source_by_name("Камера")
		if cam ~= nil then
			local data = obs.obs_data_create()
			obs.obs_data_set_int(data, "res_type", 1)
			obs.obs_data_set_string(data, "resolution", cw .. "x" .. ch)
			obs.obs_source_update(cam, data)
			obs.obs_data_release(data)
			obs.obs_source_release(cam)
			cam_switched_at = os.time()  -- не умеет такое разрешение — откат на родное
		end
		print("scene fitted to " .. cw .. "x" .. ch)
	end
end

local function apply_resolution()
	if res_pending ~= nil then return end
	local hgt = wanted_height()
	if hgt == nil then return end
	local _, ch = canvas_size()
	if ch == hgt then return end
	res_pending = hgt
	if obs.obs_frontend_virtualcam_active() then
		obs.obs_frontend_stop_virtualcam()
	else
		obs.obs_frontend_start_virtualcam()
	end
end

local function on_frontend_event(event)
	if res_pending == nil then return end
	if event == obs.OBS_FRONTEND_EVENT_VIRTUALCAM_STARTED then
		obs.obs_frontend_stop_virtualcam()
	elseif event == obs.OBS_FRONTEND_EVENT_VIRTUALCAM_STOPPED then
		local rw, rh = RESOLUTIONS[res_pending][1], RESOLUTIONS[res_pending][2]
		local cfg = obs.obs_frontend_get_profile_config()
		obs.config_set_uint(cfg, "Video", "BaseCX", rw)
		obs.config_set_uint(cfg, "Video", "BaseCY", rh)
		obs.config_set_uint(cfg, "Video", "OutputCX", rw)
		obs.config_set_uint(cfg, "Video", "OutputCY", rh)
		obs.config_save(cfg)
		obs.obs_frontend_reset_video()
		res_pending = nil
		obs.obs_frontend_start_virtualcam()  -- слои подгонит fit_scene на ближайшем тике
		print("resolution " .. rw .. "x" .. rh)
	end
end

local function tick()
	apply_resolution()
	if res_pending == nil then fit_scene() end
	apply_mirror()
	apply_camera()
	check_preview()
	resize_preview()
	hide_obs_tray_icon()  -- снова, если Проводник перезапустился и Qt вернул значок
	ensure_tray()
end

local function hide_fast()
	-- Первые секунды после старта проверяем часто, чтобы значок OBS не успел мелькнуть
	if hide_obs_tray_icon() then obs.remove_current_callback() end
end

function script_description()
	return "Shirma: значок в трее, качество, камера и превью."
end

function script_load(settings)
	launch_tray()
	obs.timer_add(hide_fast, 250)
	obs.timer_add(tick, 2000)
	obs.timer_add(camera_on_demand, 500)
	obs.obs_frontend_add_event_callback(on_frontend_event)
end

function script_unload()
	obs.timer_remove(hide_fast)
	obs.timer_remove(tick)
	obs.timer_remove(camera_on_demand)
end
"""


def lua_profiles():
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return f'"{v}"' if isinstance(v, str) else repr(v)
    rows = [f'{k} = {{{", ".join(f"{a} = {val(b)}" for a, b in p.items())}}}' for k, p in QUALITY.items()]
    return "{\n\t" + ",\n\t".join(rows) + "\n}"


def obs_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True).stdout
    return b"obs64.exe" in out


def pick_camera(wanted):
    cams = list_cameras()
    if not cams:
        sys.exit(t("Не нашёл ни одной подключённой камеры. Подключите камеру и запустите установку ещё раз.",
                   "No connected camera found. Connect a camera and run the installer again."))
    if wanted:
        if wanted.isdigit() and 1 <= int(wanted) <= len(cams):
            return cams[int(wanted) - 1]
        hit = [c for c in cams if wanted.lower() in c.name.lower()]
        if hit:
            return hit[0]
        print(t(f"Камера «{wanted}» не найдена, выберу сам.", f"Camera '{wanted}' not found, picking one."))
    if len(cams) == 1 or not sys.stdin.isatty():
        return cams[0]
    print(t("Найдено несколько камер:", "Several cameras found:"))
    for i, c in enumerate(cams, 1):
        print(f"  {i}. {c.name}{'' if c.is_usb else t('  (встроенная)', '  (built-in)')}")
    while True:
        ans = input(t(f"Какую использовать? [1-{len(cams)}, Enter = 1]: ",
                      f"Which one to use? [1-{len(cams)}, Enter = 1]: ")).strip() or "1"
        if ans.isdigit() and 1 <= int(ans) <= len(cams):
            return cams[int(ans) - 1]


def source(sid, name, settings, filters=None):
    return {"name": name, "uuid": str(uuid.uuid4()), "id": sid, "versioned_id": sid, "settings": settings,
            "mixers": 0, "sync": 0, "flags": 0, "volume": 1.0, "balance": 0.5, "enabled": True, "muted": False,
            "push-to-mute": False, "push-to-mute-delay": 0, "push-to-talk": False, "push-to-talk-delay": 0,
            "hotkeys": {}, "deinterlace_mode": 0, "deinterlace_field_order": 0, "monitoring_type": 0,
            "private_settings": {}, "filters": filters or []}


def item(name, src_uuid, idx, bounds_type, visible=True):
    # Выравнивание по центру кадра (align 0 = центр): тогда отражение по горизонтали
    # (scale.x < 0) идёт вокруг центра, и слой не уезжает за край
    return {"name": name, "source_uuid": src_uuid, "visible": visible, "locked": True, "rot": 0.0, "align": 0,
            "bounds_type": bounds_type, "bounds_align": 0, "bounds_crop": True,
            "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "id": idx,
            "group_item_backup": False, "pos": {"x": W / 2, "y": H / 2}, "scale": {"x": 1.0, "y": 1.0},
            "bounds": {"x": float(W), "y": float(H)}, "scale_filter": "disable", "blend_method": "default",
            "blend_type": "normal", "show_transition": {"duration": 0}, "hide_transition": {"duration": 0},
            "private_settings": {}}


def write_scene(camera, active_jpg, lua_path):
    bg = source("image_source", "Фон", {"file": active_jpg.as_posix(), "unload": False})
    removal = {"name": "Удаление фона", "uuid": str(uuid.uuid4()), "id": "background_removal",
               "versioned_id": "background_removal", "enabled": True, "settings": FILTER_SETTINGS,
               "mixers": 0, "sync": 0, "flags": 0, "volume": 1.0, "balance": 0.5, "muted": False,
               "hotkeys": {}, "private_settings": {}, "filters": []}
    cam = source("dshow_input", "Камера", {
        "video_device_id": camera.obs_id, "last_video_device_id": camera.obs_id,
        "res_type": 1, "resolution": f"{W}x{H}", "frame_interval": 333333, "video_format": 0,
        "active": True,
        # Камера включается только когда нужна: Lua показывает её в сцене на время звонка
        # или превью, а спрятанную OBS отпускает сам (лампочка гаснет)
        "deactivate_when_not_showing": True}, [removal])
    scene = source("scene", NAME, {"id_counter": 2, "custom_size": False,
                                   "items": [item("Фон", bg["uuid"], 1, 3), item("Камера", cam["uuid"], 2, 2, visible=False)]})
    coll = {"current_scene": NAME, "current_program_scene": NAME, "scene_order": [{"name": NAME}], "name": NAME,
            "sources": [scene, bg, cam], "groups": [], "quick_transitions": [], "transitions": [],
            "saved_projectors": [], "current_transition": "Cut", "transition_duration": 0,
            "preview_locked": True, "scaling_enabled": False, "version": 2,
            "modules": {"scripts-tool": [{"path": lua_path.as_posix(), "settings": {}}]}}
    path = OBS_CFG / "basic" / "scenes" / f"{NAME}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(coll, ensure_ascii=False, indent=2), encoding="utf-8")


def write_profile():
    path = OBS_CFG / "basic" / "profiles" / NAME / "basic.ini"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[General]\nName={NAME}\n\n[Video]\nBaseCX={W}\nBaseCY={H}\nOutputCX={W}\nOutputCY={H}\n"
                    f"FPSType=0\nFPSCommon={FPS}\n\n[Output]\nMode=Simple\n", encoding="utf-8")


def skip_first_run_wizard():
    """Если OBS ни разу не запускали, он откроет мастер автонастройки — помечаем, что первый запуск был."""
    for ini in (OBS_CFG / "user.ini", OBS_CFG / "global.ini"):
        text = ini.read_text(encoding="utf-8-sig") if ini.exists() else ""
        if "FirstRun=" in text:
            continue
        if "[General]" in text:
            text = text.replace("[General]", "[General]\nFirstRun=true", 1)
        else:
            text = "[General]\nFirstRun=true\n\n" + text
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_text(text, encoding="utf-8")


IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# Прежние имена папок -> нынешние (переезд при обновлении)
LEGACY_FOLDERS = {"Корпоративные": "Корп", "Пользовательские": "Свои"}


def _sha1(path: Path) -> str:
    import hashlib
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _hash_list(path: Path) -> set:
    if not path.exists():
        return set()
    lines = (l.strip() for l in path.read_text(encoding="utf-8").splitlines())
    return {l for l in lines if l and not l.startswith("#")}


def sync_backgrounds(bundled: Path, dst: Path):
    """Фоны из репозитория -> папка пользователя, не трогая его собственные картинки.

    - старые папки («Корпоративные», «Пользовательские») переезжают в нынешние;
    - недостающие фоны набора копируем, плоские копии старой установки перекладываем;
    - фон набора с тем же именем заменяем новой версией, только если файл
      пользователя совпадает с ранее выпущенной (.superseded) — значит, он его не менял;
    - снятые с набора (.retired) удаляем;
    - картинки пользователя из корня — в «Свои».
    """
    dst.mkdir(parents=True, exist_ok=True)
    for old, new in LEGACY_FOLDERS.items():
        old_dir, new_dir = dst / old, dst / new
        if not old_dir.is_dir():
            continue
        new_dir.mkdir(exist_ok=True)
        for f in list(old_dir.rglob("*")):
            if f.is_file():
                target = new_dir / f.relative_to(old_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    f.unlink()
                else:
                    f.replace(target)
        for d in sorted((d for d in old_dir.rglob("*") if d.is_dir()), reverse=True):
            d.rmdir()
        old_dir.rmdir()

    for name in _hash_list(bundled / ".retired"):
        for f in dst.rglob(name):
            f.unlink()

    superseded = _hash_list(bundled / ".superseded")
    bundled_names = set()
    for src in bundled.rglob("*"):
        if not src.is_file() or src.suffix.lower() not in IMG_EXT:
            continue
        bundled_names.add(src.name)
        target = dst / src.relative_to(bundled)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _sha1(target) in superseded and _sha1(target) != _sha1(src):
                shutil.copy2(src, target)
            continue
        # Плоская копия старой установки — только если это действительно наш файл;
        # своя картинка пользователя с тем же именем уедет в «Свои» ниже
        flat = dst / src.name
        if flat.is_file() and flat != target and _sha1(flat) in superseded | {_sha1(src)}:
            flat.replace(target)
        else:
            shutil.copy2(src, target)

    custom = dst / t("Свои", "Custom")
    bundled_hashes = {_sha1(f) for f in bundled.rglob("*") if f.is_file() and f.suffix.lower() in IMG_EXT}
    for img in [f for f in dst.iterdir() if f.is_file() and f.suffix.lower() in IMG_EXT]:
        if img.name in bundled_names and _sha1(img) in bundled_hashes | superseded:
            img.unlink()  # лишняя плоская копия нашего фона — он уже лежит в своей папке
            continue
        custom.mkdir(exist_ok=True)
        target, n = custom / img.name, 2
        while target.exists():
            target, n = custom / f"{img.stem} ({n}){img.suffix}", n + 1
        img.replace(target)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundled", default="", help="папка backgrounds из репозитория")
    ap.add_argument("--obs", required=True, help="путь к obs64.exe")
    ap.add_argument("--data", required=True, help="папка данных Shirma")
    ap.add_argument("--camera", default="", help="номер или часть имени камеры")
    ap.add_argument("--default-background", default="Библиотека")
    args = ap.parse_args()

    if obs_running():
        sys.exit(t("OBS сейчас запущен. Закройте Shirma (меню значка → «Выключить Shirma») и запустите "
                   "установку ещё раз: при выходе OBS перезапишет сцену своим состоянием.",
                   "OBS is running. Quit Shirma (tray menu -> Quit Shirma) and run the installer again: "
                   "on exit OBS would overwrite the scene with its own state."))

    data = Path(args.data).resolve()
    app = Path(__file__).resolve().parent
    global W, H
    try:
        hgt = json.loads((data / "resolution.json").read_text(encoding="utf-8")).get("height", 720)
    except (OSError, ValueError):
        hgt = 720
    W, H = RESOLUTIONS.get(hgt, RESOLUTIONS[720])
    if args.bundled:
        sync_backgrounds(Path(args.bundled), data / "backgrounds")
    pythonw = data / "venv" / "Scripts" / "pythonw.exe"
    camera = pick_camera(args.camera)
    print(t(f"Камера: {camera.name}", f"Camera: {camera.name}"))

    lua = data / "shirma.lua"
    lua.write_text(LUA.replace("{pythonw}", str(pythonw)).replace("{script}", str(app / "shirma.py"))
                   .replace("{dir}", str(data)).replace("{profiles}", lua_profiles())
                   .replace("{default}", DEFAULT_QUALITY), encoding="utf-8")
    (data / "camera.json").write_text(json.dumps({"id": camera.obs_id, "name": camera.name}, ensure_ascii=False),
                                      encoding="utf-8")
    quality = data / "quality.json"
    if not quality.exists():
        quality.write_text(json.dumps({"quality": DEFAULT_QUALITY}), encoding="utf-8")
    write_profile()
    write_scene(camera, data / "active.jpg", lua)
    skip_first_run_wizard()

    cfg_path = data / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    # Фон по умолчанию сохраняем, если он ещё существует (его могли снять с набора)
    default_bg = cfg.get("default_background", args.default_background)
    if not any(p.stem == default_bg for p in (data / "backgrounds").rglob("*") if p.is_file()):
        default_bg = args.default_background
    cfg.update({"obs": str(Path(args.obs)), "profile": NAME, "collection": NAME,
                "camera": camera.name, "default_background": default_bg})
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

    import shirma  # после config.json: модуль читает его при импорте
    shirma.save_icon_file()
    shirma.ensure_active()
    print(t("OBS настроен.", "OBS is set up."))


if __name__ == "__main__":
    main()
