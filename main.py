"""PULSEPAD – hangpad a mikrofonodra (pywebview webes felülettel).

Hangok lejátszása a mikrofonodon (virtuális kábelen) keresztül.

Szükséges csomagok: pywebview miniaudio sounddevice numpy keyboard pystray pillow
Indítás:  python main.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
import webbrowser
import time
import zlib
from pathlib import Path

import miniaudio
import numpy as np
import sounddevice as sd
import webview

try:
    import keyboard as _kb
except Exception:
    _kb = None

SR = 44100
OLD_CONFIG = Path(__file__).with_name("soundboard_config.json")  # régi, titkosítatlan fájl (egyszeri átvétel)
APP_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "soundb"
CONFIG = APP_DIR / "config.dat"
MAGIC_DPAPI = b"SBX1"
MAGIC_FERNET = b"SBX2"
DEBOUNCE = 0.25
MIN_LIT = 0.18
EXTS = {".mp3", ".wav", ".ogg", ".flac"}
OUT_HINTS = ("voicemeeter input", "voicemeeter aux", "cable input", "vb-audio")
MAX_NAME = 60
PEAKS = 128      # hullámforma-minták száma hangonként (a pad előnézethez)
FADE_MS = 70     # leállításkor ennyi ms alatt halkul el (nincs kattogás)
PALETTE = 8      # a felületen elérhető padszínek száma
VERSION = "1.0"       # minden kiadásnál írd át, és egyezzen a GitHub-kiadás címkéjével (pl. v4.1)
UPDATE_REPO = "FrenzyRL/soundboard"      # "felhasznalo/repo" – a GitHub tároló, ahová a kiadásokat teszed; üresen az ellenőrzés ki van kapcsolva
PREVIEW_ID = 0   # a vágó előnézetének hangazonosítója (a valódi hangok 1-től indulnak)
WAVE_PEAKS = 800 # hullámforma-minták száma a vágó nézetben
WATCH_EVERY = 2.0  # ennyi másodpercenként nézi a figyelt mappákat
DEFAULT_PROFILE = {"hu": "Általános", "en": "General"}
NEW_PROFILE = {"hu": "Új profil", "en": "New profile"}
TRAY_TEXT = {
    "hu": {"show": "Pulsepad megnyitása", "stop": "Mindent leállít", "quit": "Kilépés"},
    "en": {"show": "Open Pulsepad", "stop": "Stop everything", "quit": "Quit"},
}


# ----------------------------------------------------------------------------
# Titkosított beállítások (%APPDATA%\soundb\config.dat)
# ----------------------------------------------------------------------------
def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI: csak ugyanaz a Windows-felhasználó tudja visszafejteni, külön kulcs nélkül."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def make(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    inb, keep1 = make(data)
    ent, keep2 = make(b"soundb")
    out = Blob()
    if not fn(ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI hiba")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _fernet():
    """Nem Windows rendszeren: Fernet, a kulcs a mappában lévő key.bin fájlban."""
    from cryptography.fernet import Fernet

    key_path = APP_DIR / "key.bin"
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key = Fernet.generate_key()
        key_path.write_bytes(key)
    return Fernet(key)


def encrypt_bytes(raw: bytes) -> bytes:
    if sys.platform == "win32":
        return MAGIC_DPAPI + _dpapi(raw, True)
    return MAGIC_FERNET + _fernet().encrypt(raw)


def decrypt_bytes(blob: bytes) -> bytes:
    if blob.startswith(MAGIC_DPAPI):
        return _dpapi(blob[len(MAGIC_DPAPI):], False)
    if blob.startswith(MAGIC_FERNET):
        return _fernet().decrypt(blob[len(MAGIC_FERNET):])
    raise ValueError("ismeretlen formátum")


def read_config() -> dict:
    try:
        cfg = json.loads(decrypt_bytes(CONFIG.read_bytes()).decode("utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        pass
    except Exception:
        return {}
    # egyszeri átvétel a régi JSON-ból
    try:
        cfg = json.loads(OLD_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(cfg, dict):
        write_config(cfg)
        return cfg
    return {}


def write_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = encrypt_bytes(json.dumps(cfg, ensure_ascii=False).encode("utf-8"))
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, CONFIG)


# ----------------------------------------------------------------------------
# Hangmotor
# ----------------------------------------------------------------------------
def load_sound(path: str) -> np.ndarray:
    d = miniaudio.decode_file(
        path,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=2,
        sample_rate=SR,
    )
    arr = np.frombuffer(d.samples, dtype=np.int16).astype(np.float32) / 32768.0
    return arr.reshape(-1, 2)


def make_peaks(data: np.ndarray, n: int = PEAKS) -> list[float]:
    """Hullámforma-előnézet: n darab 0..1 csúcsérték (a leghangosabb = 1)."""
    if len(data) == 0:
        return [0.0] * n
    mono = np.abs(data).max(axis=1)
    edges = np.linspace(0, len(mono), n + 1).astype(int)[:-1]
    out = np.maximum.reduceat(mono, edges).astype(np.float32)
    top = float(out.max()) or 1.0
    out = (out / top) ** 0.75
    return [round(float(v), 2) for v in out]


def parse_trim(t) -> tuple[float, float, int, int]:
    """(kezdet mp, vég mp [0 = a végéig], fade-in ms, fade-out ms), biztonságosan."""
    try:
        t0, t1, fi, fo = [float(v) for v in t]
    except Exception:
        return 0.0, 0.0, 0, 0
    return max(0.0, t0), max(0.0, t1), int(max(0, min(5000, fi))), int(max(0, min(5000, fo)))


def process_trim(full: np.ndarray, t0: float, t1: float, fi: int, fo: int) -> np.ndarray:
    """A teljes hangból kivágja a kijelölt részt, és ráteszi a fade-in/out-ot (az eredeti nem változik)."""
    n = len(full)
    if n == 0:
        return full
    a = min(max(0, int(t0 * SR)), n - 1)
    b = int(t1 * SR) if t1 > 0 else n
    b = max(a + 1, min(n, b))
    if a == 0 and b == n and not fi and not fo:
        return full
    seg = full[a:b].copy()
    k = len(seg)
    ki = min(int(fi * SR / 1000), k)
    ko = min(int(fo * SR / 1000), k - ki)
    if ki > 0:
        seg[:ki] *= np.linspace(0.0, 1.0, ki, dtype=np.float32)[:, None]
    if ko > 0:
        seg[k - ko:] *= np.linspace(1.0, 0.0, ko, dtype=np.float32)[:, None]
    return seg


class Mixer:
    # egy hang: [adat, pozíció, id, erősítés, loop, fade_hátra, fade_összes]
    def __init__(self) -> None:
        self.voices: list[list] = []
        self.volume = 0.8
        self.lock = threading.Lock()
        self._pk = np.zeros(2, dtype=np.float32)

    def add(self, data: np.ndarray, sid: int, gain: float = 1.0, loop: bool = False) -> None:
        with self.lock:
            self.voices.append([data, 0, sid, gain, loop, None, 0])

    def set_gain(self, sid: int, gain: float) -> None:
        with self.lock:
            for v in self.voices:
                if v[2] == sid:
                    v[3] = gain

    def set_loop(self, sid: int, loop: bool) -> None:
        with self.lock:
            for v in self.voices:
                if v[2] == sid:
                    v[4] = loop

    def seek(self, sid: int, frac: float) -> None:
        """Tekerés: a szóló hang a hossz adott arányú pontjától megy tovább."""
        with self.lock:
            for v in self.voices:
                if v[2] == sid and v[5] is None:
                    n = len(v[0])
                    v[1] = min(max(0, int(frac * n)), max(0, n - 1))

    def clear(self) -> None:
        with self.lock:
            self.voices.clear()

    def fade(self, sid: int | None = None, ms: int = FADE_MS) -> None:
        """Puha leállítás: a hang(ok) pár ms alatt elhalkulnak, és kiesnek."""
        n = max(1, int(SR * ms / 1000))
        with self.lock:
            for v in self.voices:
                if (sid is None or v[2] == sid) and v[5] is None:
                    v[5], v[6] = n, n

    def active_ids(self) -> set[int]:
        with self.lock:
            return {v[2] for v in self.voices if v[5] is None}

    def progress(self) -> dict[int, float]:
        with self.lock:
            return {v[2]: (v[1] / len(v[0]) if len(v[0]) else 1.0) for v in self.voices if v[5] is None}

    def take_peak(self) -> np.ndarray:
        """A legutóbbi lekérdezés óta mért csúcs (bal, jobb), majd nullázás."""
        with self.lock:
            pk = self._pk.copy()
            self._pk[:] = 0.0
        return pk

    def mix(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), dtype=np.float32)
        with self.lock:
            alive = []
            for v in self.voices:
                data, pos, loop = v[0], v[1], v[4]
                n = len(data)
                buf = np.zeros((frames, 2), dtype=np.float32)
                o, ended = 0, n == 0
                while o < frames and not ended:
                    take = min(frames - o, n - pos)
                    buf[o:o + take] = data[pos:pos + take]
                    o += take
                    pos += take
                    if pos >= n:
                        if loop:
                            pos = 0
                        else:
                            ended = True
                v[1] = pos
                if v[5] is not None:
                    left, total = v[5], v[6]
                    ramp = np.clip(np.arange(left, left - frames, -1, dtype=np.float32) / total, 0.0, 1.0)
                    buf *= ramp[:, None]
                    v[5] = left - frames
                    if v[5] <= 0:
                        ended = True
                out += buf * v[3]
                if not ended:
                    alive.append(v)
            self.voices = alive
            out *= self.volume
            np.maximum(self._pk, np.abs(out).max(axis=0), out=self._pk)
        return out


def clamp_color(c):
    try:
        c = int(c)
    except (TypeError, ValueError):
        return None
    return c if 0 <= c < PALETTE else None


def clamp_vol(v) -> float:
    try:
        return min(1.5, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 1.0


def clean_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_NAME]


def list_devices(kind: str) -> list[tuple[int, str]]:
    key = "max_output_channels" if kind == "out" else "max_input_channels"
    try:
        default_api = sd.query_devices(sd.default.device[1 if kind == "out" else 0])["hostapi"]
    except Exception:
        default_api = None
    res = []
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and (default_api is None or d["hostapi"] == default_api):
            res.append((i, d["name"]))
    return res


def pick_default(devs: list[tuple[int, str]], saved, hints=()) -> str:
    names = [n for _, n in devs]
    if saved in names:
        return saved
    for h in hints:
        for n in names:
            if h in n.lower():
                return n
    return names[0] if names else ""


def dev_index(devs: list[tuple[int, str]], name: str):
    for i, n in devs:
        if n == name:
            return i
    return None


def path_key(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


class Api:
    """A JavaScript ezeket a metódusokat hívja: window.pywebview.api.<név>()

    Minden belső adat '_' kezdetű, hogy a pywebview ne próbálja kiajánlani.
    """

    def __init__(self) -> None:
        self._window = None
        self._main = Mixer()
        self._mon = Mixer()
        self._streams: list = []
        self._main_active = False
        self._mon_active = False
        self._audio_lock = threading.Lock()
        self._outs = list_devices("out")
        self._ins = list_devices("in")
        self._sounds: list[dict] = []
        self._next_id = 1
        self._lk = threading.RLock()          # hangok listája / azonosítók
        self._save_lock = threading.Lock()
        self._lit_until: dict[int, float] = {}
        self._hk: dict[int, object] = {}      # hang-gyorsbillentyűk (csak az aktív profilé él)
        self._last_toggle: dict[int, float] = {}
        self._binding = False
        self._bind_done: threading.Event | None = None
        self._tray = None
        self._tray_ok = False
        self._quitting = False
        self._status = ("ok", "")
        # profilok
        self._profiles: list[dict] = []       # {id, name, hotkey, folder}
        self._active = 1
        self._next_pid = 1
        self._phk: dict[int, object] = {}     # profilváltó gyorsbillentyűk
        self._rev = 0                         # nő, ha a lista a háttérben változott (figyelt mappa, profilváltás)
        # figyelt mappák
        self._seen: set[str] = set()
        self._wsizes: dict[str, tuple] = {}
        self._wfails: dict[str, int] = {}
        self._scan_lock = threading.Lock()
        # frissítés
        self._update: dict | None = None
        self._upd = {"state": "idle", "p": 0.0, "error": ""}
        self._upd_path = ""
        self._stopev = threading.Event()
        self._s = {
            "out": "", "mon": "", "mic": "",
            "use_mon": True, "use_mic": False, "volume": 0.8,
            "lang": "hu", "theme": "dark", "tray_close": True, "stack": False, "skip_update": "",
        }
        self._init()

    # ---------- indulás / mentés ----------
    def _init(self) -> None:
        cfg = read_config()
        s = self._s
        s["out"] = pick_default(self._outs, cfg.get("out"), OUT_HINTS)
        s["mon"] = pick_default(self._outs, cfg.get("mon"))
        s["mic"] = pick_default(self._ins, cfg.get("mic"))
        s["use_mon"] = bool(cfg.get("use_mon", True))
        s["use_mic"] = bool(cfg.get("use_mic", False))
        try:
            s["volume"] = min(1.0, max(0.0, float(cfg.get("volume", 0.8))))
        except (TypeError, ValueError):
            pass
        s["tray_close"] = bool(cfg.get("tray_close", True))
        s["stack"] = bool(cfg.get("stack", False))
        s["skip_update"] = str(cfg.get("skip_update") or "")
        if cfg.get("lang") in ("hu", "en"):
            s["lang"] = cfg["lang"]
        if cfg.get("theme") in ("dark", "light"):
            s["theme"] = cfg["theme"]
        self._apply_volume()

        # profilok
        profs: list[dict] = []
        raw = cfg.get("profiles")
        for p in raw if isinstance(raw, list) else []:
            if not isinstance(p, dict) or not isinstance(p.get("id"), int):
                continue
            if any(q["id"] == p["id"] for q in profs):
                continue
            profs.append({
                "id": p["id"],
                "name": clean_name(p.get("name")) or NEW_PROFILE[s["lang"]],
                "hotkey": str(p.get("hotkey") or ""),
                "folder": str(p.get("folder") or ""),
            })
        if not profs:
            profs = [{"id": 1, "name": DEFAULT_PROFILE[s["lang"]], "hotkey": "", "folder": ""}]
        self._profiles = profs
        self._next_pid = max(p["id"] for p in profs) + 1
        act = cfg.get("active")
        self._active = act if any(p["id"] == act for p in profs) else profs[0]["id"]
        for p in profs:
            hk, p["hotkey"] = p["hotkey"], ""
            if hk:
                self._set_phk(p, hk)
        seen = cfg.get("seen")
        self._seen = {path_key(x) for x in seen if isinstance(x, str)} if isinstance(seen, list) else set()

        def d(key):
            v = cfg.get(key)
            return v if isinstance(v, dict) else {}

        binds, names, vols = d("binds"), d("names"), d("vols")
        loops, colors, favs, pmap = d("loops"), d("colors"), d("favs"), d("profs")
        trims = d("trims")
        valid = {p["id"] for p in profs}
        for p in cfg.get("sounds", []):
            try:
                if Path(p).exists():
                    pid = pmap.get(p)
                    if pid not in valid:
                        pid = profs[0]["id"]
                    self._add_sound(p, binds.get(p, ""), names.get(p, ""), clamp_vol(vols.get(p, 1.0)),
                                    bool(loops.get(p, False)), clamp_color(colors.get(p)),
                                    pid=pid, fav=bool(favs.get(p, False)), trim=trims.get(p))
            except Exception:
                pass
        self._restart_audio()

    def _save(self) -> None:
        with self._save_lock:
            cfg = dict(self._s)
            snd = list(self._sounds)
            cfg["sounds"] = [x["path"] for x in snd]
            cfg["binds"] = {x["path"]: x["hotkey"] for x in snd if x["hotkey"]}
            # csak a programon belüli nevek – a fájl neve sosem változik
            cfg["names"] = {x["path"]: x["name"] for x in snd if x["name"] != x["default"]}
            cfg["vols"] = {x["path"]: round(x["vol"], 2) for x in snd if abs(x["vol"] - 1.0) > 0.004}
            cfg["loops"] = {x["path"]: True for x in snd if x["loop"]}
            cfg["colors"] = {x["path"]: x["color"] for x in snd if x["color"] is not None}
            cfg["favs"] = {x["path"]: True for x in snd if x["fav"]}
            cfg["trims"] = {x["path"]: [x["t0"], x["t1"], x["fi"], x["fo"]]
                            for x in snd if x["t0"] or x["t1"] or x["fi"] or x["fo"]}
            cfg["profs"] = {x["path"]: x["pid"] for x in snd}
            cfg["profiles"] = [dict(p) for p in self._profiles]
            cfg["active"] = self._active
            cfg["seen"] = sorted(self._seen)
            try:
                write_config(cfg)
            except Exception:
                pass

    def _state(self) -> dict:
        counts: dict[int, int] = {}
        for x in self._sounds:
            counts[x["pid"]] = counts.get(x["pid"], 0) + 1
        return {
            "outs": [n for _, n in self._outs],
            "ins": [n for _, n in self._ins],
            "settings": self._s,
            "profiles": [dict(p, count=counts.get(p["id"], 0)) for p in self._profiles],
            "active": self._active,
            "rev": self._rev,
            "version": VERSION,
            "update": None if self._update is None else {
                k: self._update[k] for k in ("version", "notes", "can_install")},
            "sounds": [
                {
                    "id": x["id"], "name": x["name"], "hotkey": x["hotkey"], "vol": x["vol"],
                    "hue": zlib.crc32(x["path"].encode("utf-8")) % 360,
                    "loop": x["loop"], "color": x["color"], "peaks": x["peaks"], "fav": x["fav"],
                    "t0": x["t0"], "t1": x["t1"], "fi": x["fi"], "fo": x["fo"],
                    "trimmed": bool(x["t0"] or x["t1"] or x["fi"] or x["fo"]),
                    "dur": round(len(x["data"]) / SR, 2),
                }
                for x in list(self._sounds) if x["pid"] == self._active
            ],
            "hotkeys": _kb is not None,
            "tray": self._tray_ok,
            "status": self._status[0],
            "detail": self._status[1],
        }

    # ---------- audio ----------
    def _apply_volume(self) -> None:
        self._main.volume = self._mon.volume = self._s["volume"]

    def _restart_audio(self) -> None:
        with self._audio_lock:
            for st in self._streams:
                try:
                    st.stop()
                    st.close()
                except Exception:
                    pass
            self._streams = []
            self._main.clear()
            self._mon.clear()
            self._main_active = self._mon_active = False

            s = self._s
            out = dev_index(self._outs, s["out"])
            mon = dev_index(self._outs, s["mon"])
            mic = dev_index(self._ins, s["mic"])
            main, monm = self._main, self._mon

            try:
                if out is not None:
                    if s["use_mic"] and mic is not None:
                        def cb(indata, outdata, frames, t, status):
                            outdata[:] = np.clip(main.mix(frames) + indata, -1, 1)
                        st = sd.Stream(samplerate=SR, device=(mic, out), channels=(1, 2),
                                       dtype="float32", callback=cb)
                    else:
                        def cb(outdata, frames, t, status):
                            outdata[:] = np.clip(main.mix(frames), -1, 1)
                        st = sd.OutputStream(samplerate=SR, device=out, channels=2,
                                             dtype="float32", callback=cb)
                    st.start()
                    self._streams.append(st)
                    self._main_active = True
                if s["use_mon"] and mon is not None:
                    def cb2(outdata, frames, t, status):
                        outdata[:] = np.clip(monm.mix(frames), -1, 1)
                    st2 = sd.OutputStream(samplerate=SR, device=mon, channels=2,
                                          dtype="float32", callback=cb2)
                    st2.start()
                    self._streams.append(st2)
                    self._mon_active = True
                self._status = ("ok", "") if self._streams else ("no_out", "")
            except Exception as e:
                self._status = ("error", str(e))

    def _add_sound(self, path: str, hotkey: str = "", name: str = "", vol: float = 1.0,
                   loop: bool = False, color=None, pid: int | None = None, fav: bool = False,
                   trim=None) -> None:
        full = load_sound(path)          # lassú rész: zár nélkül
        t0, t1, fi, fo = parse_trim(trim)
        data = process_trim(full, t0, t1, fi, fo)
        peaks = make_peaks(data)
        default = Path(path).stem
        with self._lk:
            x = {
                "id": self._next_id, "path": path, "default": default,
                "name": (name or default)[:MAX_NAME], "data": data, "hotkey": "", "vol": vol,
                "full": full, "t0": t0, "t1": t1, "fi": fi, "fo": fo, "wpeaks": None,
                "loop": loop, "color": color, "peaks": peaks, "fav": fav,
                "pid": pid if pid is not None else self._active,
            }
            self._next_id += 1
            self._sounds.append(x)
        if hotkey:
            self._register(x, hotkey)

    # ---------- profilok ----------
    def _profile(self, pid):
        return next((p for p in self._profiles if p["id"] == pid), None)

    def _switch_profile(self, pid: int) -> None:
        if self._profile(pid) is None or pid == self._active:
            return
        self._stop()
        self._active = pid
        self._rehook()
        self._rev += 1
        self._save()

    def _phk_fire(self, pid: int) -> None:
        if self._binding:
            return
        # a billentyűfigyelő szálát nem terheljük: külön szálon váltunk
        threading.Thread(target=self._switch_profile, args=(pid,), daemon=True).start()

    def _clear_phk(self, p: dict) -> None:
        h = self._phk.pop(p["id"], None)
        if h is not None and _kb is not None:
            try:
                _kb.remove_hotkey(h)
            except Exception:
                pass
        p["hotkey"] = ""

    def _set_phk(self, p: dict, hotkey: str) -> bool:
        self._clear_phk(p)
        if not hotkey or _kb is None:
            return False
        for o in self._profiles:
            if o is not p and o["hotkey"] == hotkey:
                self._clear_phk(o)
        for x in self._sounds:
            if x["hotkey"] == hotkey:
                self._unregister(x)
        try:
            self._phk[p["id"]] = _kb.add_hotkey(hotkey, self._phk_fire, args=(p["id"],))
        except Exception:
            return False
        p["hotkey"] = hotkey
        return True

    def _rehook(self) -> None:
        """Csak az aktív profil hangjainak gyorsbillentyűi élnek."""
        for x in list(self._sounds):
            if x["pid"] != self._active:
                self._drop_handle(x)
            elif x["hotkey"] and x["id"] not in self._hk and _kb is not None:
                try:
                    self._hk[x["id"]] = _kb.add_hotkey(x["hotkey"], self._toggle, args=(x["id"],))
                except Exception:
                    x["hotkey"] = ""

    # ---------- gyorsbillentyűk ----------
    def _drop_handle(self, x: dict) -> None:
        h = self._hk.pop(x["id"], None)
        if h is not None and _kb is not None:
            try:
                _kb.remove_hotkey(h)
            except Exception:
                pass

    def _unregister(self, x: dict) -> None:
        self._drop_handle(x)
        x["hotkey"] = ""

    def _register(self, x: dict, hotkey: str) -> bool:
        """Hozzárendel egy gyorsbillentyűt a hanghoz.

        Ugyanabban a profilban másnál már meglévő billentyűt elvesszük tőle, így több
        profilban ugyanaz a billentyű is használható. Profilváltó billentyűvel ütközve az veszít.
        """
        self._unregister(x)
        if not hotkey or _kb is None:
            return False
        for o in self._sounds:
            if o is not x and o["pid"] == x["pid"] and o["hotkey"] == hotkey:
                self._unregister(o)
        for p in self._profiles:
            if p["hotkey"] == hotkey:
                self._clear_phk(p)
        if x["pid"] == self._active:
            try:
                self._hk[x["id"]] = _kb.add_hotkey(hotkey, self._toggle, args=(x["id"],))
            except Exception:
                return False
        x["hotkey"] = hotkey
        return True

    def _toggle(self, sid: int) -> None:
        """Egyszer megnyomva elindítja a hangot, még egyszer megnyomva leállítja.

        Új hang indításakor az előzőleg szóló hang(ok) leállnak.
        """
        if self._binding:
            return
        now = time.monotonic()
        if now - self._last_toggle.get(sid, 0.0) < DEBOUNCE:
            return
        self._last_toggle[sid] = now
        x = next((s for s in self._sounds if s["id"] == sid), None)
        if x is None:
            return
        if sid in (self._main.active_ids() | self._mon.active_ids()):
            self._main.fade(sid)
            self._mon.fade(sid)
            self._lit_until.pop(sid, None)
            return
        if not self._s["stack"]:
            # az előző hang(ok) puha leállítása
            self._main.fade()
            self._mon.fade()
            self._lit_until.clear()
        # csak olyan mixerbe tesszük, amihez tényleg fut stream
        if self._main_active:
            self._main.add(x["data"], sid, x["vol"], x["loop"])
        if self._mon_active:
            self._mon.add(x["data"], sid, x["vol"], x["loop"])
        self._lit_until[sid] = now + MIN_LIT

    def _stop(self) -> None:
        self._main.fade()
        self._mon.fade()
        self._lit_until.clear()

    def _load_many(self, paths) -> list[str]:
        errors = []
        for p in paths or []:
            if Path(p).suffix.lower() not in EXTS:
                continue
            try:
                self._add_sound(p)
            except Exception as e:
                errors.append(f"{Path(p).name}: {e}")
        self._save()
        return errors

    def _capture_key(self) -> tuple[str, str]:
        """Megvárja a következő billentyű(kombináció)t.

        Visszatérés: ("ok", név) | ("cancelled", "") | ("clear", "") | ("invalid", "").
        Esc = mégse, Delete/Backspace = törlés.
        """
        done = threading.Event()
        pressed: list[str] = []
        got: list[list[str]] = []

        def on_event(e) -> None:
            if e.event_type == "down":
                if e.name not in pressed:
                    pressed.append(e.name)
            elif pressed and not done.is_set():
                got.append(list(pressed))
                done.set()

        self._binding = True
        self._bind_done = done
        handle = _kb.hook(on_event)
        try:
            done.wait(15)
        finally:
            _kb.unhook(handle)
            self._bind_done = None
            time.sleep(0.15)  # hogy a felengedett billentyű ne indítson el semmit
            self._binding = False

        if not got:
            return "cancelled", ""
        try:
            name = _kb.get_hotkey_name(got[0])
        except Exception:
            return "invalid", ""
        if name == "esc":
            return "cancelled", ""
        if name in ("delete", "backspace"):
            return "clear", ""
        return "ok", name

    # ---------- figyelt mappák ----------
    def _scan_profile(self, p: dict, immediate: bool = False) -> int:
        """A profil mappájában lévő új hangokat felveszi. Visszatér: hány hang jött."""
        folder = p.get("folder")
        if not folder:
            return 0
        try:
            files = sorted(f for f in Path(folder).iterdir() if f.is_file() and f.suffix.lower() in EXTS)
        except Exception:
            return 0
        added = 0
        with self._scan_lock:
            known = {path_key(x["path"]) for x in self._sounds}
            for f in files:
                path = str(f)
                k = path_key(path)
                if k in self._seen or k in known:
                    continue
                if not immediate:
                    # csak akkor vesszük fel, ha két körben ugyanakkora (a másolás befejeződött)
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    sig = (st.st_size, st.st_mtime_ns)
                    if st.st_size == 0 or self._wsizes.get(k) != sig:
                        self._wsizes[k] = sig
                        continue
                try:
                    self._add_sound(path, pid=p["id"])
                except Exception:
                    n = self._wfails.get(k, 0) + 1
                    self._wfails[k] = n
                    if n >= 3:
                        self._seen.add(k)
                    continue
                self._seen.add(k)
                self._wsizes.pop(k, None)
                added += 1
        return added

    def _watch_loop(self) -> None:
        while not self._stopev.wait(WATCH_EVERY):
            added = 0
            for p in list(self._profiles):
                try:
                    added += self._scan_profile(p)
                except Exception:
                    pass
            if added:
                self._rev += 1
                self._save()

    def _start_watch(self) -> None:
        threading.Thread(target=self._watch_loop, daemon=True).start()

    # ---------- tálcaikon ----------
    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return  # pystray / pillow nélkül tálca nélkül fut a program

        def make_icon(size: int = 64):
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size * 0.18, fill=(255, 122, 61, 255))
            k = size / 40
            pts = [(5, 21), (12, 21), (15, 10), (20, 31), (24, 15), (27, 21), (35, 21)]
            d.line([(x * k, y * k) for x, y in pts], fill=(23, 24, 26, 255), width=max(2, round(2.6 * k)), joint="curve")
            return img

        def tr(key: str) -> str:
            return TRAY_TEXT[self._s["lang"]][key]

        def show(*_):
            w = self._window
            if w is None:
                return
            try:
                w.show()
                w.restore()
            except Exception:
                pass

        def stop_sounds(*_):
            self._stop()

        def quit_app(*_):
            self._quitting = True
            try:
                self._tray.stop()
            except Exception:
                pass
            if self._window is not None:
                try:
                    self._window.destroy()
                except Exception:
                    pass

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: tr("show"), show, default=True),
            pystray.MenuItem(lambda item: tr("stop"), stop_sounds),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: tr("quit"), quit_app),
        )
        try:
            self._tray = pystray.Icon("pulsepad", make_icon(), "Pulsepad", menu)
            self._tray.run_detached()
            self._tray_ok = True
        except Exception:
            self._tray = None
            self._tray_ok = False

    def _on_closing(self) -> bool:
        """Bezáráskor a tálcára kerül (ha be van kapcsolva), a Kilépés a tálcamenüből van."""
        if self._quitting or not self._tray_ok or not self._s["tray_close"]:
            return True
        try:
            self._window.hide()
        except Exception:
            return True
        return False

    def _shutdown(self) -> None:
        self._stopev.set()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        if _kb is not None:
            try:
                _kb.unhook_all_hotkeys()
            except Exception:
                pass
        for st in self._streams:
            try:
                st.close()
            except Exception:
                pass

    # ---------- JS-ből hívható műveletek ----------
    def get_state(self) -> dict:
        return self._state()

    def set_settings(self, d: dict) -> dict:
        changed = False
        for k in ("out", "mon", "mic"):
            if k in d and d[k] != self._s[k]:
                self._s[k] = d[k]
                changed = True
        for k in ("use_mon", "use_mic"):
            if k in d and bool(d[k]) != self._s[k]:
                self._s[k] = bool(d[k])
                changed = True
        if "tray_close" in d:
            self._s["tray_close"] = bool(d["tray_close"])
        if "stack" in d:
            self._s["stack"] = bool(d["stack"])
        if changed:
            self._restart_audio()
        self._save()
        return self._state()

    def set_sound_volume(self, sid: int, v: float) -> dict:
        """Hangonkénti hangerő (0–1.5), azonnal érvényes a most szóló hangra is."""
        vol = clamp_vol(v)
        for x in self._sounds:
            if x["id"] == sid:
                x["vol"] = vol
                self._main.set_gain(sid, vol)
                self._mon.set_gain(sid, vol)
        self._save()
        return {"ok": True}

    def set_volume(self, v: float) -> dict:
        try:
            self._s["volume"] = min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return {"ok": False}
        self._apply_volume()
        self._save()
        return {"ok": True}

    def set_ui(self, lang: str, theme: str) -> dict:
        if lang in ("hu", "en"):
            self._s["lang"] = lang
        if theme in ("dark", "light"):
            self._s["theme"] = theme
        self._save()
        return {"ok": True}

    def toggle(self, sid: int) -> dict:
        """Kattintás a padra: ugyanaz, mint a gyorsbillentyű (indít / leállít)."""
        if not any(x["id"] == sid for x in self._sounds):
            return {"ok": False, "error": "not_found"}
        self._toggle(sid)
        return {"ok": True}

    def seek(self, sid: int, frac) -> dict:
        """Tekerés a szóló hangban (frac: 0..1)."""
        try:
            f = min(1.0, max(0.0, float(frac)))
        except (TypeError, ValueError):
            return {"ok": False}
        self._main.seek(sid, f)
        self._mon.seek(sid, f)
        return {"ok": True}

    def bind(self, sid: int) -> dict:
        """Megvárja a következő billentyű(kombináció)t, és a hanghoz rendeli."""
        if _kb is None:
            return {"ok": False, "error": "no_keyboard"}
        x = next((s for s in self._sounds if s["id"] == sid), None)
        if x is None:
            return {"ok": False, "error": "not_found"}
        status, name = self._capture_key()
        if status == "cancelled":
            return {"ok": True, "cancelled": True, **self._state()}
        if status == "invalid":
            return {"ok": False, "error": "invalid_key", **self._state()}
        if status == "clear":
            self._unregister(x)
        elif not self._register(x, name):
            return {"ok": False, "error": "invalid_key", **self._state()}
        self._save()
        return {"ok": True, **self._state()}

    def cancel_bind(self) -> dict:
        """A felugró ablak „Mégse” gombja: megszakítja a billentyűre várakozást."""
        done = self._bind_done
        if done is not None:
            done.set()
        return {"ok": True}

    def clear_hotkey(self, sid: int) -> dict:
        for x in self._sounds:
            if x["id"] == sid:
                self._unregister(x)
        self._save()
        return self._state()

    def rename(self, sid: int, name: str) -> dict:
        """Átnevezés csak a programban – a fájl neve nem változik. Üres név = eredeti név."""
        name = clean_name(name)
        for x in self._sounds:
            if x["id"] == sid:
                x["name"] = name or x["default"]
        self._save()
        return self._state()

    def stop_all(self) -> dict:
        self._stop()
        return {"ok": True}

    def set_loop(self, sid: int, on: bool) -> dict:
        """Ismétlés (loop): a hang addig szól, amíg le nem állítod."""
        for x in self._sounds:
            if x["id"] == sid:
                x["loop"] = bool(on)
                self._main.set_loop(sid, x["loop"])
                self._mon.set_loop(sid, x["loop"])
        self._save()
        return self._state()

    def set_color(self, sid: int, idx) -> dict:
        for x in self._sounds:
            if x["id"] == sid:
                x["color"] = clamp_color(idx)
        self._save()
        return self._state()

    def set_fav(self, sid: int, on) -> dict:
        for x in self._sounds:
            if x["id"] == sid:
                x["fav"] = bool(on)
        self._save()
        return {"ok": True}

    def move(self, sid: int, index) -> dict:
        """Pad áthelyezése (húzással): a hang a megadott pozícióba kerül az aktív profilon belül."""
        try:
            index = int(index)
        except (TypeError, ValueError):
            return self._state()
        with self._lk:
            x = next((s for s in self._sounds if s["id"] == sid), None)
            if x is not None:
                self._sounds.remove(x)
                ps = [s for s in self._sounds if s["pid"] == x["pid"]]
                index = max(0, min(len(ps), index))
                if index >= len(ps):
                    at = (self._sounds.index(ps[-1]) + 1) if ps else len(self._sounds)
                else:
                    at = self._sounds.index(ps[index])
                self._sounds.insert(at, x)
        self._save()
        return self._state()

    def move_to_profile(self, sid: int, pid: int) -> dict:
        """A hang átkerül egy másik profilba (a gyorsbillentyűje törlődik, hogy ne ütközzön)."""
        if self._profile(pid) is not None:
            for x in self._sounds:
                if x["id"] == sid and x["pid"] != pid:
                    self._unregister(x)
                    self._main.fade(sid)
                    self._mon.fade(sid)
                    self._lit_until.pop(sid, None)
                    x["pid"] = pid
        self._save()
        return self._state()

    # ----- profilok -----
    def set_profile(self, pid: int) -> dict:
        self._switch_profile(pid)
        return self._state()

    def add_profile(self, name: str) -> dict:
        name = clean_name(name) or NEW_PROFILE[self._s["lang"]]
        with self._lk:
            pid = self._next_pid
            self._next_pid += 1
            self._profiles.append({"id": pid, "name": name, "hotkey": "", "folder": ""})
        self._switch_profile(pid)
        self._save()
        return self._state()

    def rename_profile(self, pid: int, name: str) -> dict:
        p = self._profile(pid)
        name = clean_name(name)
        if p is not None and name:
            p["name"] = name
        self._save()
        return self._state()

    def delete_profile(self, pid: int) -> dict:
        p = self._profile(pid)
        if p is None or len(self._profiles) <= 1:
            return self._state()
        self._stop()
        with self._lk:
            for x in [x for x in self._sounds if x["pid"] == pid]:
                self._unregister(x)
            self._sounds = [x for x in self._sounds if x["pid"] != pid]
            self._clear_phk(p)
            self._profiles = [q for q in self._profiles if q["id"] != pid]
            if self._active == pid:
                self._active = self._profiles[0]["id"]
                self._rehook()
        self._save()
        return self._state()

    def bind_profile(self, pid: int) -> dict:
        """A profilhoz váltó gyorsbillentyű beállítása (Esc = mégse, Del = törlés)."""
        if _kb is None:
            return {"ok": False, "error": "no_keyboard"}
        p = self._profile(pid)
        if p is None:
            return {"ok": False, "error": "not_found"}
        status, name = self._capture_key()
        if status == "cancelled":
            return {"ok": True, "cancelled": True, **self._state()}
        if status == "invalid":
            return {"ok": False, "error": "invalid_key", **self._state()}
        if status == "clear":
            self._clear_phk(p)
        elif not self._set_phk(p, name):
            return {"ok": False, "error": "invalid_key", **self._state()}
        self._save()
        return {"ok": True, **self._state()}

    def clear_profile_hotkey(self, pid: int) -> dict:
        p = self._profile(pid)
        if p is not None:
            self._clear_phk(p)
        self._save()
        return self._state()

    def set_folder(self, pid: int) -> dict:
        """Figyelt mappa kijelölése a profilhoz: a benne lévő és a későbbi hangok megjelennek."""
        p = self._profile(pid)
        if p is None or self._window is None:
            return {"ok": False, **self._state()}
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind)
        if not res:
            return {"ok": True, "added": 0, **self._state()}
        folder = res[0] if isinstance(res, (list, tuple)) else res
        p["folder"] = str(folder)
        added = self._scan_profile(p, immediate=True)
        self._save()
        return {"ok": True, "added": added, **self._state()}

    def clear_folder(self, pid: int) -> dict:
        p = self._profile(pid)
        if p is not None:
            p["folder"] = ""
        self._save()
        return self._state()

    # ----- frissítés (GitHub Releases) -----
    def _check_update(self) -> str:
        """Megnézi a legújabb kiadást. Visszatér: "ok" | "error" | "off"."""
        if not UPDATE_REPO:
            return "off"
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                headers={"User-Agent": "pulsepad", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=6) as r:
                rel = json.load(r)

            def ver(s):
                return tuple(int(p) for p in re.findall(r"\d+", str(s))) or (0,)

            latest = str(rel.get("tag_name", ""))
            if ver(latest) <= ver(VERSION):
                self._update = None
                return "ok"
            asset = next((a for a in rel.get("assets", [])
                          if str(a.get("name", "")).lower().endswith(".exe")), None)
            body = str(rel.get("body") or "")
            sha = ""
            if asset:
                dg = str(asset.get("digest") or "")
                if dg.startswith("sha256:"):
                    sha = dg[7:]
            if not sha:
                m = re.search(r"\b[0-9a-fA-F]{64}\b", body)
                sha = m.group(0) if m else ""
            self._update = {
                "version": latest.lstrip("vV"), "notes": body[:2000],
                "url": asset["browser_download_url"] if asset else "",
                "page": str(rel.get("html_url") or ""), "sha256": sha,
                "can_install": bool(asset) and bool(getattr(sys, "frozen", False)) and sys.platform == "win32",
            }
            return "ok"
        except Exception:
            return "error"

    def _start_update_check(self) -> None:
        def run() -> None:
            time.sleep(4)               # ne lassítsa az indulást
            self._check_update()
            if self._update is not None:
                self._rev += 1          # a felület a következő lekérdezéskor megjeleníti
        threading.Thread(target=run, daemon=True).start()

    def check_update_now(self) -> dict:
        """„Frissítések keresése” gomb: kihagyott verziót is újra felkínál."""
        self._s["skip_update"] = ""
        status = self._check_update()
        self._save()
        return {"status": status, **self._state()}

    def skip_update(self, ver: str) -> dict:
        self._s["skip_update"] = str(ver)
        self._save()
        return {"ok": True}

    def update_status(self) -> dict:
        return dict(self._upd)

    def start_update(self) -> dict:
        u = self._update
        if not u:
            return {"ok": False}
        if not u["can_install"]:
            webbrowser.open(u["page"] or u["url"])   # nem exe-ből futunk: csak megnyitjuk a letöltési oldalt
            return {"ok": True}
        if self._upd["state"] == "downloading":
            return {"ok": True}
        self._upd = {"state": "downloading", "p": 0.0, "error": ""}
        threading.Thread(target=self._download_update, args=(dict(u),), daemon=True).start()
        return {"ok": True}

    def _download_update(self, u: dict) -> None:
        try:
            folder = APP_DIR / "update"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "pulsepad-new.exe"
            h = hashlib.sha256()
            req = urllib.request.Request(u["url"], headers={"User-Agent": "pulsepad"})
            with urllib.request.urlopen(req, timeout=20) as r, open(path, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self._upd["p"] = (got / total) if total else 0.0
            if u["sha256"] and h.hexdigest().lower() != u["sha256"].lower():
                path.unlink(missing_ok=True)
                raise ValueError("SHA-256 mismatch")
            with open(path, "rb") as f:
                if f.read(2) != b"MZ":
                    path.unlink(missing_ok=True)
                    raise ValueError("not an exe")
            self._upd_path = str(path)
            self._upd = {"state": "ready", "p": 1.0, "error": ""}
        except Exception as e:
            self._upd = {"state": "error", "p": 0.0, "error": str(e)}

    def apply_update(self) -> dict:
        """A letöltött exe-t a kilépés után lecseréli, majd újraindítja a programot."""
        if self._upd["state"] != "ready" or not self._upd_path or sys.platform != "win32":
            return {"ok": False}
        exe, new = sys.executable, self._upd_path
        bat = APP_DIR / "update" / "apply.bat"
        bat.write_text(
            "@echo off\r\nchcp 65001 >nul\r\n"
            f'set "OLD={exe}"\r\nset "NEW={new}"\r\n'
            "for /l %%i in (1,1,40) do (\r\n"
            '  move /y "%NEW%" "%OLD%" >nul 2>&1 && goto done\r\n'
            "  timeout /t 1 /nobreak >nul\r\n)\r\n"
            "exit /b 1\r\n:done\r\n"
            'start "" "%OLD%"\r\n(goto) 2>nul & del "%~f0"\r\n',
            encoding="utf-8")
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True)
        threading.Timer(0.4, self._quit_now).start()
        return {"ok": True}

    def _quit_now(self) -> None:
        self._quitting = True
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass

    # ----- vágás (nem destruktív: az eredeti fájl és a teljes hang érintetlen marad) -----
    def _find(self, sid):
        return next((x for x in self._sounds if x["id"] == sid), None)

    @staticmethod
    def _norm_trim(x: dict, t0, t1, fi, fo) -> tuple[float, float, int, int]:
        dur = len(x["full"]) / SR
        t0, t1, fi, fo = parse_trim((t0, t1, fi, fo))
        t0 = min(t0, max(0.0, dur - 0.01))
        if t0 < 0.005:
            t0 = 0.0
        if t1 <= 0 or t1 >= dur - 0.005:
            t1 = 0.0                      # 0 = a hang végéig
        else:
            t1 = max(t1, t0 + 0.01)
        return round(t0, 3), round(t1, 3), fi, fo

    def get_wave(self, sid: int) -> dict:
        """A vágó nézethez: a teljes (nem vágott) hang nagy felbontású hullámformája."""
        x = self._find(sid)
        if x is None:
            return {"ok": False}
        if x["wpeaks"] is None:
            x["wpeaks"] = make_peaks(x["full"], WAVE_PEAKS)
        return {"ok": True, "peaks": x["wpeaks"], "dur": round(len(x["full"]) / SR, 3)}

    def set_trim(self, sid: int, t0, t1, fi, fo) -> dict:
        x = self._find(sid)
        if x is not None:
            x["t0"], x["t1"], x["fi"], x["fo"] = self._norm_trim(x, t0, t1, fi, fo)
            x["data"] = process_trim(x["full"], x["t0"], x["t1"], x["fi"], x["fo"])
            x["peaks"] = make_peaks(x["data"])
            self._main.fade(sid)          # a most szóló példány a régi adatot használja
            self._mon.fade(sid)
            self._lit_until.pop(sid, None)
            self._save()
        return self._state()

    def preview(self, sid: int, t0, t1, fi, fo) -> dict:
        """Mentés nélküli előnézet – csak a fejhallgatón/hangszórón, a mikrofonra nem megy."""
        x = self._find(sid)
        if x is None:
            return {"ok": False, "error": "not_found"}
        if not self._mon_active:
            return {"ok": False, "error": "no_monitor"}
        t0, t1, fi, fo = self._norm_trim(x, t0, t1, fi, fo)
        data = process_trim(x["full"], t0, t1, fi, fo)
        self._mon.fade(PREVIEW_ID)
        self._mon.add(data, PREVIEW_ID, x["vol"], False)
        return {"ok": True}

    def preview_stop(self) -> dict:
        self._mon.fade(PREVIEW_ID)
        return {"ok": True}

    def playing(self) -> dict:
        """Szóló hangok {id: arány 0..1}, a kimenet csúcsszintje [bal, jobb], az aktív profil és a változásszámláló."""
        now = time.monotonic()
        res: dict[int, float] = {}
        mp = self._mon.progress()
        pv = mp.pop(PREVIEW_ID, None)     # a vágó előnézete nem pad
        res.update(mp)
        res.update(self._main.progress())
        for i, t in list(self._lit_until.items()):
            if t > now:
                res.setdefault(i, 0.0)
        pm, pn = self._main.take_peak(), self._mon.take_peak()
        pk = pm if self._main_active else pn
        return {
            "p": {str(k): round(v, 3) for k, v in res.items()},
            "lv": [round(min(1.5, float(pk[0])), 3), round(min(1.5, float(pk[1])), 3)],
            "ap": self._active,
            "rev": self._rev,
            "pv": None if pv is None else round(pv, 3),
        }

    def add_dialog(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "no_window"}
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.OPEN if dialog else webview.OPEN_DIALOG
        paths = self._window.create_file_dialog(
            kind, allow_multiple=True,
            file_types=("Hang (*.mp3;*.wav;*.ogg;*.flac)", "Minden fájl (*.*)"),
        )
        errors = self._load_many(paths)
        return {"ok": True, "errors": errors, **self._state()}

    def add_paths(self, paths: list[str]) -> dict:
        errors = self._load_many(paths)
        return {"ok": True, "errors": errors, **self._state()}

    def remove(self, sid: int) -> dict:
        self._stop()
        for x in self._sounds:
            if x["id"] == sid:
                self._unregister(x)
                self._seen.add(path_key(x["path"]))  # a figyelt mappa ne hozza vissza
        with self._lk:
            self._sounds = [x for x in self._sounds if x["id"] != sid]
        self._save()
        return self._state()


# ----------------------------------------------------------------------------
# Felület
# ----------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="hu" data-theme="dark"><head><meta charset="utf-8"><title>Pulsepad</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0c0c0a;--panel:#12120f;--raised:#1a1a16;--line:#25251f;--line2:#3a3a31;--text:#ece8dc;--muted:#97948a;--dim:#55544b;--wave:rgba(236,232,220,.24);--accent:#d9ff3f;--on-accent:#0c0c0a;--ok:#9bd36a;--warn:#f0b23c;--bad:#ff5a3c;--grain:.07;
--c0:#d9ff3f;--c1:#ff5a3c;--c2:#3fe0d0;--c3:#ffb938;--c4:#ff7ab8;--c5:#6aa8ff;--c6:#b394ff;--c7:#7dffa0;
--font:"Bahnschrift","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;--display:"Bahnschrift SemiCondensed","Bahnschrift","Arial Narrow","Segoe UI",sans-serif;--mono:"Cascadia Mono","Cascadia Code",Consolas,"SF Mono",Menlo,monospace}
[data-theme=light]{--bg:#e8e4d6;--panel:#dfdaca;--raised:#f3f0e5;--line:#c9c4b2;--line2:#a8a391;--text:#15150e;--muted:#5a594d;--dim:#a7a290;--wave:rgba(21,21,14,.27);--accent:#ff4a1c;--on-accent:#fff6e8;--ok:#2e8a3e;--warn:#a86a00;--bad:#d0361a;--grain:.12;
--c0:#7a9700;--c1:#e0391a;--c2:#0d8f84;--c3:#d48100;--c4:#d12f7e;--c5:#2260d4;--c6:#6a46d6;--c7:#1b9a4a}
*{box-sizing:border-box;margin:0}
html,body{height:100%;background:var(--bg);color:var(--text);font:14px/1.45 var(--font);transition:background-color .3s,color .3s}
body{overflow:hidden;user-select:none;-webkit-user-select:none}
input{user-select:text;-webkit-user-select:text}
button{font-family:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line2);border:3px solid transparent;border-radius:6px;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:var(--muted);background-clip:padding-box;border:3px solid transparent}
.hide{display:none!important}
.ico{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.app{height:100vh;display:flex;flex-direction:column}
.app::after{content:"";position:fixed;inset:0;z-index:100;pointer-events:none;opacity:var(--grain);mix-blend-mode:overlay;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 .9 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

/* ---------- felső sáv ---------- */
.bar{height:54px;flex:none;display:flex;align-items:center;gap:22px;padding:0 18px;border-bottom:1px solid var(--line)}
.logo{display:flex;align-items:center;gap:11px;min-width:200px}
.mark{width:28px;height:28px;flex:none}
.mark circle{fill:var(--accent)}
.mark path{fill:none;stroke:var(--on-accent);stroke-width:2.3;stroke-linecap:round}
.wm{font:700 19px/1 var(--display);letter-spacing:.16em}
.wm b{color:var(--accent);font-weight:400;margin:0 .1em}
.ver{font:500 9.5px var(--mono);color:var(--muted);letter-spacing:.08em;border:1px solid var(--line2);padding:1px 5px;border-radius:2px}
.search{position:relative;flex:1;max-width:460px;margin:0 auto}
.search input{width:100%;height:32px;border:1px solid var(--line);border-radius:2px;background:var(--panel);color:var(--text);font:13px var(--mono);padding:0 64px 0 34px;transition:border-color .15s}
.search input::placeholder{color:var(--dim)}
.search input:focus{outline:0;border-color:var(--accent)}
.search svg{position:absolute;left:11px;top:50%;translate:0 -50%;width:14px;height:14px;fill:none;stroke:var(--muted);stroke-width:1.8;stroke-linecap:round;pointer-events:none}
.search kbd{position:absolute;right:7px;top:50%;translate:0 -50%;font:500 10px var(--mono);color:var(--muted);border:1px solid var(--line2);border-bottom-width:2px;border-radius:3px;padding:0 5px;pointer-events:none}
.bar-r{display:flex;align-items:center;gap:12px}
.status{display:flex;align-items:center;gap:8px;font:500 10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
.status::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.status.warn::before{background:var(--warn);box-shadow:0 0 8px var(--warn)}
.status.bad::before{background:var(--bad);box-shadow:0 0 8px var(--bad)}.status.bad{color:var(--bad)}
.ibtn{width:32px;height:32px;border:1px solid var(--line);border-radius:2px;background:transparent;cursor:pointer;display:grid;place-items:center;font:600 13px var(--mono);transition:background-color .12s,border-color .12s}
.ibtn:hover{background:var(--raised);border-color:var(--line2)}
#theme svg{width:16px;height:16px;grid-area:1/1;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;transition:transform .35s,opacity .2s}
.sun{opacity:0;transform:rotate(-60deg)}
[data-theme=light] .sun{opacity:1;transform:none}[data-theme=light] .moon{opacity:0;transform:rotate(60deg)}
.seg{position:relative;display:flex;border:1px solid var(--line);border-radius:2px;padding:2px}
.seg button{position:relative;z-index:1;border:0;background:none;color:var(--muted);font:600 11px var(--mono);letter-spacing:.06em;padding:4px 9px;cursor:pointer;transition:color .2s}
.seg button.on{color:var(--on-accent)}
.seg i{position:absolute;top:2px;bottom:2px;left:2px;width:calc(50% - 2px);border-radius:1px;background:var(--accent);transition:transform .25s cubic-bezier(.5,0,.2,1)}
.seg[data-v=en] i{transform:translateX(100%)}

/* ---------- oldalsáv ---------- */
.body{flex:1;min-height:0;display:flex}
.view{flex:1;min-width:0;overflow:auto;padding:22px}
.views{display:flex;gap:2px;margin-left:6px}
.vbtn{height:32px;padding:0 14px;border:1px solid transparent;border-radius:2px;background:transparent;color:var(--muted);cursor:pointer;font:700 12px var(--display);letter-spacing:.14em;text-transform:uppercase;display:inline-flex;align-items:center;transition:color .12s,background-color .12s,border-color .12s}
.vbtn:hover{color:var(--text);background:var(--panel)}
.vbtn.on{color:var(--on-accent);background:var(--accent);border-color:var(--accent)}
body:not(.v-board) .search{visibility:hidden}
.setgrid{max-width:1000px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px;align-items:start}
.setgrid .mod{border:1px solid var(--line);background:var(--panel);border-radius:3px}

/* ---------- vágó ---------- */
.trim{display:flex;gap:18px;align-items:flex-start;max-width:1200px;margin:0 auto}
.tlist{width:260px;flex:none;border:1px solid var(--line);background:var(--panel);border-radius:3px}
.tlist .mh{padding:14px 14px 0;margin-bottom:10px}
.tscroll{overflow:auto;max-height:calc(100vh - 210px)}
.tli{display:flex;align-items:center;gap:10px;width:100%;border:0;border-top:1px solid var(--line);background:none;padding:9px 14px;cursor:pointer;text-align:left;font:600 13.5px var(--font);--ink:var(--c0)}
.tli:hover{background:var(--raised)}
.tli.on{background:color-mix(in srgb,var(--ink) 12%,var(--panel));box-shadow:inset 3px 0 0 var(--ink)}
.tli i{width:8px;height:8px;border-radius:50%;background:var(--ink);flex:none}
.tli .nm2{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tli small{font:500 10.5px var(--mono);color:var(--muted)}
.tli .ico{width:12px;height:12px;color:var(--muted)}
.te{flex:1;min-width:0;display:flex;flex-direction:column;gap:16px}
.teh{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap}
.teh h2{font:700 25px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;overflow-wrap:anywhere}
.teh small{display:block;margin-top:7px;font:500 11px var(--mono);color:var(--muted);letter-spacing:.06em}
.tdirty{font:600 10.5px var(--mono);color:var(--warn);letter-spacing:.08em;text-transform:uppercase}
.twave{position:relative;height:270px;border:1px solid var(--line);background:var(--panel);border-radius:3px;overflow:hidden}
.twave canvas{position:absolute;inset:0;width:100%;height:100%;cursor:ew-resize;touch-action:none}
.tgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.tf{border:1px solid var(--line);background:var(--panel);border-radius:3px;padding:12px 14px}
.tf label{display:flex;justify-content:space-between;align-items:baseline;font:500 10px var(--mono);letter-spacing:.14em;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
.tf label b{font:500 12px var(--mono);color:var(--text);letter-spacing:.04em;text-transform:none}
.tf .minput{font:600 17px var(--mono);height:40px}
.tf .ro{font:600 17px/40px var(--mono)}
.tbtns{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tnote{color:var(--muted);font-size:12.5px;line-height:1.5}
.temp{border:1px dashed var(--line2);border-radius:3px;padding:70px 20px;text-align:center;color:var(--muted);font:13px var(--mono)}
.btn:disabled{opacity:.4;pointer-events:none}
.mvol{display:flex;align-items:center;gap:10px;margin-bottom:2px}
.mvol>span{font:500 10px var(--mono);letter-spacing:.14em;color:var(--muted);text-transform:uppercase}
.mvol input{width:130px}
.mvol b{font:600 12px var(--mono);min-width:46px;text-align:right}
.ic{display:flex;align-items:center;gap:6px}
.mod{padding:16px 18px 18px}
.mod+.mod{border-top:1px solid var(--line)}
.mh{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.mh .no{font:500 10.5px var(--mono);color:var(--accent);letter-spacing:.06em}
.mh h3{font:700 12.5px var(--display);letter-spacing:.18em;text-transform:uppercase}
.mh::after{content:"";flex:1;height:1px;background:var(--line)}
.path{display:grid;gap:14px}
.port{position:relative;padding-left:26px}
.port::before{content:"";position:absolute;left:4px;top:3px;width:11px;height:11px;border-radius:50%;border:1.5px solid var(--dim);background:var(--bg);transition:all .2s}
.port.on::before{border-color:var(--accent);background:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
.port::after{content:"";position:absolute;left:9.5px;top:19px;bottom:-14px;width:1px;background:var(--line2)}
.port:last-child::after{display:none}
.path.live .port::after{background:linear-gradient(180deg,var(--line2) 0 35%,var(--accent) 50%,var(--line2) 65% 100%);background-size:100% 300%;animation:flow 1.1s linear infinite}
.port .lab{font:500 10px var(--mono);letter-spacing:.12em;color:var(--muted);text-transform:uppercase;margin-bottom:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.port.off select{opacity:.4}
.port.core::before{border-radius:2px;rotate:45deg;width:10px;height:10px;left:5px}
.port.core.on::before{animation:pulse 1.1s ease-in-out infinite}
.core .row{display:flex;align-items:center;justify-content:space-between;font:500 12px var(--mono);color:var(--muted);height:20px}
.core .row b{color:var(--text);font-weight:500;letter-spacing:.06em;text-transform:uppercase}
select{width:100%;height:32px;font:13px var(--font);color:var(--text);background:var(--panel) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='none' stroke='%2397948a' stroke-width='1.6'%3E%3Cpath d='M1 1l4 4 4-4'/%3E%3C/svg%3E") no-repeat right 10px center;border:1px solid var(--line);border-radius:2px;padding:0 28px 0 10px;cursor:pointer;appearance:none;-webkit-appearance:none;transition:border-color .15s;text-overflow:ellipsis}
select:hover{border-color:var(--line2)}
select:focus{outline:0;border-color:var(--accent)}
select option{background:var(--panel);color:var(--text)}
.switch{display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer;font-size:13.5px}
.switch+.switch{margin-top:13px}
.switch input{appearance:none;-webkit-appearance:none;width:34px;height:18px;border:1px solid var(--line2);border-radius:2px;background:var(--panel);position:relative;margin:0;cursor:pointer;flex:none;transition:border-color .15s,background-color .15s}
.switch input::after{content:"";position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:1px;background:var(--dim);transition:transform .15s,background-color .15s}
.switch input:checked{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 14%,var(--panel))}
.switch input:checked::after{transform:translateX(16px);background:var(--accent)}
.mrow{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px}
.mrow span{font:500 10px var(--mono);letter-spacing:.14em;color:var(--muted);text-transform:uppercase}
.mrow b{font:700 30px/1 var(--display);font-variant-numeric:tabular-nums}
.mrow b small{font-size:13px;color:var(--muted);margin-left:2px;font-weight:400}
input[type=range]{appearance:none;-webkit-appearance:none;width:100%;height:24px;margin:0;background:transparent;cursor:pointer;display:block}
input[type=range]::-webkit-slider-runnable-track{height:3px;border-radius:2px;background:linear-gradient(90deg,var(--accent) var(--v,80%),var(--line2) var(--v,80%))}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:24px;margin-top:-10.5px;border-radius:2px;border:1px solid rgba(0,0,0,.55);background:linear-gradient(90deg,transparent 44%,var(--accent) 44% 56%,transparent 56%),var(--text)}
.fader::after{content:"";display:block;height:5px;margin:3px 7px 0;background:repeating-linear-gradient(90deg,var(--line2) 0 1px,transparent 1px 10%);background-size:calc(100% + 1px) 100%}

/* ---------- fő terület ---------- */
.main{flex:1;min-width:0;display:flex;flex-direction:column;padding:18px 22px 0}
.deck{flex:none;height:136px;display:flex;border:1px solid var(--line);background:
 linear-gradient(var(--accent),var(--accent)) 0 0/10px 2px no-repeat,linear-gradient(var(--accent),var(--accent)) 0 0/2px 10px no-repeat,
 linear-gradient(var(--accent),var(--accent)) 100% 0/10px 2px no-repeat,linear-gradient(var(--accent),var(--accent)) 100% 0/2px 10px no-repeat,
 linear-gradient(var(--accent),var(--accent)) 0 100%/10px 2px no-repeat,linear-gradient(var(--accent),var(--accent)) 0 100%/2px 10px no-repeat,
 linear-gradient(var(--accent),var(--accent)) 100% 100%/10px 2px no-repeat,linear-gradient(var(--accent),var(--accent)) 100% 100%/2px 10px no-repeat,var(--panel)}
.dl{width:262px;flex:none;padding:13px 16px 12px;display:flex;flex-direction:column;justify-content:space-between;border-right:1px solid var(--line);min-width:0}
.dlab{font:500 10px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:8px}
.dlab .dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.dlab .tags{margin-left:auto;display:flex;gap:5px}
.tag{font:600 9.5px/1.5 var(--mono);letter-spacing:.08em;padding:0 5px;border:1px solid var(--line2);border-radius:2px;color:var(--text)}
.deck.live .dot{background:var(--ink);box-shadow:0 0 9px var(--ink)}
.ttl{font:700 23px/1.05 var(--display);text-transform:uppercase;letter-spacing:.02em;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.deck:not(.live) .ttl{color:var(--dim)}
.deck:not(.live) .ttl::after{content:"_";color:var(--accent);animation:caret 1s steps(1) infinite}
.tm{font:500 12px var(--mono);color:var(--muted);font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:8px}
.tm b{font:500 21px var(--mono);color:var(--text)}
.deck:not(.live) .tm b{color:var(--dim)}
.dw{flex:1;min-width:0;position:relative}
.dw canvas,.dm canvas{position:absolute;width:100%;height:100%;left:0;top:0}
.dw canvas{touch-action:none}
.deck.live .dw canvas{cursor:ew-resize}
.dm{width:64px;flex:none;border-left:1px solid var(--line);position:relative}
.dm canvas{left:11px;top:11px;width:calc(100% - 22px);height:calc(100% - 30px)}
.dm .ml{position:absolute;left:11px;right:11px;bottom:5px;display:flex;justify-content:space-around;font:500 9px var(--mono);color:var(--muted);letter-spacing:.1em}

/* ---------- fülek (profilok) ---------- */
.tabs{flex:none;display:flex;align-items:flex-end;gap:2px;margin-top:16px;border-bottom:1px solid var(--line);overflow-x:auto;overflow-y:hidden;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{flex:none;display:flex;align-items:center;gap:8px;height:34px;padding:0 14px;border:1px solid transparent;border-bottom:0;border-radius:3px 3px 0 0;background:transparent;color:var(--muted);cursor:pointer;font:700 12.5px var(--display);letter-spacing:.1em;text-transform:uppercase;white-space:nowrap;position:relative;top:1px;transition:color .12s,background-color .12s}
.tab:hover{color:var(--text);background:var(--panel)}
.tab.on{color:var(--text);background:var(--bg);border-color:var(--line);border-bottom:1px solid var(--bg);box-shadow:inset 0 2px 0 var(--accent)}
.tab>span:first-child{max-width:170px;overflow:hidden;text-overflow:ellipsis}
.tab .n{font:500 10px var(--mono);color:var(--dim);letter-spacing:.04em}
.tab .key{font-size:9.5px;letter-spacing:.02em;text-transform:none}
.tab.menu-open{outline:2px solid var(--accent);outline-offset:-2px}
.tab.add{padding:0 12px;font-size:17px;font-weight:400;letter-spacing:0}

.mhead{flex:none;display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:16px 0 14px}
.mhead h1{font:700 25px/1 var(--display);letter-spacing:.08em;text-transform:uppercase;display:flex;align-items:center;gap:12px}
.count{font:600 11.5px/1.6 var(--mono);color:var(--bg);background:var(--text);padding:0 7px;border-radius:2px;letter-spacing:.04em}
.filters{display:flex;align-items:center;gap:7px;margin-left:auto;margin-bottom:4px}
.fbtn{width:28px;height:28px;border:1px solid var(--line);border-radius:2px;background:transparent;color:var(--muted);cursor:pointer;display:grid;place-items:center;transition:color .12s,border-color .12s,background-color .12s}
.fbtn:hover{color:var(--text);border-color:var(--line2)}
.fbtn.on{color:var(--on-accent);background:var(--accent);border-color:var(--accent)}
.fbtn.on .ico{fill:currentColor}
.fsep{width:1px;height:18px;background:var(--line);margin:0 3px}
.filters .sw8.dim{opacity:.28}
.actions{display:flex;gap:8px}
.btn{font:700 12px var(--display);letter-spacing:.12em;text-transform:uppercase;cursor:pointer;border:1px solid var(--line2);background:transparent;color:var(--text);height:34px;padding:0 15px;border-radius:2px;display:inline-flex;align-items:center;gap:9px;transition:background-color .12s,border-color .12s,color .12s}
.btn:hover{background:var(--raised);border-color:var(--muted)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn.primary:hover{filter:brightness(1.07);background:var(--accent)}
.btn.red{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 50%,var(--line))}
.btn.red:hover{background:var(--bad);border-color:var(--bad);color:var(--on-accent)}
.btn .sq{width:8px;height:8px;background:currentColor}
.padwrap{flex:1;min-height:0;overflow:auto;margin:0 -22px;padding:2px 22px 24px}

/* ---------- padek ---------- */
.pads{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:10px}
.pad{--ink:var(--c0);--p:0;position:relative;display:flex;flex-direction:column;min-height:150px;padding:10px 12px 11px 16px;background:var(--panel);border:1px solid var(--line);border-radius:3px;cursor:pointer;overflow:hidden;transition:border-color .12s,background-color .15s,transform .08s}
.pad::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ink);opacity:.5;transition:opacity .12s}
.pad:hover{background:var(--raised);border-color:var(--line2)}
.pad:hover::before{opacity:1}
.pad:active{transform:scale(.988)}
.pad.dragging{opacity:.3}
.pad.over{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.pad.menu-open{outline:2px solid var(--accent);outline-offset:2px}
.top{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:20px}
.tr{display:flex;align-items:center;gap:5px}
.idx{display:flex;align-items:center;gap:8px;font:500 10.5px var(--mono);color:var(--muted);letter-spacing:.1em}
.led{width:7px;height:7px;border-radius:50%;background:var(--dim);transition:background-color .1s,box-shadow .1s}
.key{font:600 10.5px/1.5 var(--mono);padding:0 6px;border:1px solid var(--line2);border-bottom-width:2px;border-radius:3px;background:var(--bg);color:var(--text);white-space:nowrap}
.fav{border:0;background:none;color:var(--muted);cursor:pointer;display:grid;place-items:center;padding:2px;border-radius:2px;margin-right:-3px}
.fav:hover{color:var(--text)}
.fav .ico{width:14px;height:14px}
.fav.on{color:var(--ink)}
.fav.on .ico{fill:currentColor}
.pad:not(:hover):not(:focus-within) .fav:not(.on){opacity:0}
.wv{position:relative;height:38px;margin:10px 0 9px;flex:none}
.wv i{position:absolute;inset:0;-webkit-mask:var(--m) center/100% 100% no-repeat;mask:var(--m) center/100% 100% no-repeat}
.wv .b{background:var(--wave);transition:background-color .15s}
.wv .f{background:var(--ink);clip-path:inset(0 calc((1 - var(--p)) * 100%) 0 0);opacity:0}
.wv u{position:absolute;top:-3px;bottom:-3px;left:calc(var(--p) * 100%);width:1.5px;background:var(--text);opacity:0;text-decoration:none}
.nm{flex:1;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere;font:700 16px/1.12 var(--display);letter-spacing:.015em}
.meta{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:7px;font:500 10.5px var(--mono);color:var(--muted);font-variant-numeric:tabular-nums;letter-spacing:.03em}
.meta .ico{width:12px;height:12px;color:var(--ink)}
.pad.playing{border-color:var(--ink);background:color-mix(in srgb,var(--ink) 9%,var(--panel))}
.pad.playing::before{opacity:1}
.pad.playing .led{background:var(--ink);box-shadow:0 0 8px var(--ink)}
.pad.playing .wv .b{background:color-mix(in srgb,var(--ink) 26%,transparent)}
.pad.playing .wv .f{opacity:1}
.pad.playing .wv u{opacity:.9}
.pad.playing .key{border-color:var(--ink)}
.pad.addtile{align-items:center;justify-content:center;gap:4px;border:1px dashed var(--line2);background:transparent;color:var(--muted)}
.pad.addtile::before{display:none}
.pad.addtile:hover{color:var(--text);border-color:var(--muted);background:var(--raised)}
.addtile .plus{font:300 38px/1 var(--display)}
.addtile .l{font:600 11px var(--mono);letter-spacing:.1em;text-transform:uppercase}
.nomatch{grid-column:1/-1;color:var(--muted);padding:34px 0;font:13px var(--mono)}
.empty{position:relative;min-height:330px}
.ghosts{position:absolute;inset:0;display:grid;grid-template-columns:repeat(4,1fr);grid-template-rows:repeat(2,1fr);gap:10px;-webkit-mask-image:linear-gradient(90deg,transparent 15%,#000 85%);mask-image:linear-gradient(90deg,transparent 15%,#000 85%)}
.ghosts i{position:relative;border:1px dashed var(--line2);border-radius:3px}
.ghosts b{position:absolute;left:16px;right:14px;top:50%;height:40px;translate:0 -50%;background:var(--wave);-webkit-mask:var(--m) center/100% 100% no-repeat;mask:var(--m) center/100% 100% no-repeat}
.emptybox{position:relative;max-width:440px;padding:18px 0 0 2px}
.emptybox h2{font:700 66px/.92 var(--display);letter-spacing:.015em;text-transform:uppercase;margin-bottom:16px}
.emptybox h2 em{display:block;font-style:normal;color:var(--accent)}
.emptybox p{color:var(--muted);margin-bottom:20px;max-width:340px}
#drop{position:fixed;inset:10px;z-index:50;display:none;align-items:center;justify-content:center;font:700 28px var(--display);letter-spacing:.1em;text-transform:uppercase;border:2px dashed var(--accent);border-radius:3px;background:color-mix(in srgb,var(--bg) 90%,transparent);pointer-events:none}
body.dragging #drop{display:flex}

/* ---------- lábléc ---------- */
.foot{height:28px;flex:none;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:0 18px;border-top:1px solid var(--line);font:500 10.5px var(--mono);color:var(--muted);letter-spacing:.05em;white-space:nowrap;overflow:hidden}
.hints{display:flex;gap:18px}
.hints span{display:flex;align-items:center;gap:7px}
.foot kbd{font:600 9.5px var(--mono);color:var(--text);border:1px solid var(--line2);border-bottom-width:2px;border-radius:3px;padding:0 5px}
.foot .out{overflow:hidden;text-overflow:ellipsis}

/* ---------- jobb klikk menü ---------- */
.ctx{position:fixed;z-index:60;min-width:244px;max-width:320px;padding:5px;border:1px solid var(--line2);border-radius:3px;background:var(--raised);box-shadow:0 10px 30px rgba(0,0,0,.45);opacity:0;transform:translateY(-3px);pointer-events:none;transition:opacity .1s,transform .12s}
.ctx.on{opacity:1;transform:none;pointer-events:auto}
.ctx>button{display:flex;align-items:center;gap:11px;width:100%;border:0;background:none;font:500 13.5px var(--font);text-align:left;padding:7px 10px;border-radius:2px;cursor:pointer}
.ctx>button>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ctx>button:hover,.ctx>button:focus-visible{background:var(--line);outline:0}
.ctx>button.red{color:var(--bad)}
.ctx .ico{color:var(--muted)}.ctx button.red .ico{color:inherit}
.ctx .sep{height:1px;background:var(--line);margin:5px 3px}
.ctxinfo{padding:2px 10px 6px 37px;font:500 10.5px var(--mono);color:var(--muted);overflow-wrap:anywhere}
.vrow{display:flex;align-items:center;gap:10px;padding:6px 10px 4px}
.vrow input{flex:1}
.vrow b{width:42px;text-align:right;font:500 11.5px var(--mono);color:var(--muted)}
.mini{margin-left:auto;width:26px;height:14px;border:1px solid var(--line2);border-radius:2px;position:relative;flex:none}
.mini::after{content:"";position:absolute;top:2px;left:2px;width:8px;height:8px;background:var(--dim);transition:transform .15s,background-color .15s}
.mini.on{border-color:var(--accent)}.mini.on::after{transform:translateX(12px);background:var(--accent)}
.crow{display:flex;align-items:center;gap:8px;padding:7px 10px 8px}
.crow .lbl{font:500 10px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-right:auto}
.sw8{width:15px;height:15px;border-radius:50%;border:0;padding:0;cursor:pointer;background:var(--ink);transition:transform .1s;flex:none}
.sw8:hover{transform:scale(1.2)}
.sw8.on{box-shadow:0 0 0 2px var(--bg),0 0 0 3.5px var(--text)}
.crow .sw8.on{box-shadow:0 0 0 2px var(--raised),0 0 0 3.5px var(--text)}

/* ---------- modal ---------- */
.overlay{position:fixed;inset:0;z-index:70;display:grid;place-items:center;padding:20px;background:rgba(6,6,5,.62);opacity:0;pointer-events:none;transition:opacity .15s}
.overlay.on{opacity:1;pointer-events:auto}
.modal{width:min(410px,100%);border:1px solid var(--line2);border-radius:3px;background:var(--raised);padding:22px;box-shadow:0 18px 50px rgba(0,0,0,.5);transform:translateY(8px);transition:transform .18s}
.modal.wide{width:min(560px,100%)}
.overlay.on .modal{transform:none}
.modal h3{font:700 20px/1.1 var(--display);letter-spacing:.1em;text-transform:uppercase}
.mbody{margin-top:14px;color:var(--muted)}
.mbody p{line-height:1.55}
.mbody .how{max-height:62vh;overflow:auto}
.mbody .how p{padding:10px 0;border-top:1px solid var(--line)}
.mbody .how p:first-child{border-top:0;padding-top:0}
.minput{width:100%;height:38px;font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line2);border-radius:2px;padding:0 11px;transition:border-color .15s}
.minput:focus{outline:0;border-color:var(--accent)}
.mbtns{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}
.keyhint{display:grid;justify-items:center;gap:12px;padding:6px 0 0;color:var(--text);text-align:center}
.keyhint small{color:var(--muted);font:12px var(--mono)}
.cap{width:78px;height:78px;display:grid;place-items:center;border:2px solid var(--line2);border-bottom-width:7px;border-radius:9px;background:var(--panel)}
.cap::after{content:"";width:2px;height:28px;background:var(--accent);animation:caret 1s steps(1) infinite}
.swap .t{opacity:0}.t{transition:opacity .15s}
.toast{position:fixed;bottom:42px;left:50%;transform:translate(-50%,16px);opacity:0;max-width:80vw;white-space:pre-line;background:var(--raised);border:1px solid var(--line2);border-left:3px solid var(--bad);color:var(--text);padding:10px 16px;border-radius:2px;z-index:90;font:12.5px var(--mono);transition:opacity .2s,transform .2s;pointer-events:none}
.toast.info{border-left-color:var(--accent)}
.toast.on{opacity:1;transform:translate(-50%,0)}
@keyframes caret{50%{opacity:0}}
@keyframes flow{from{background-position:0 100%}to{background-position:0 -100%}}
@keyframes pulse{50%{scale:1.35}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media (max-width:1100px){.tlist{width:210px}.search{max-width:300px}.logo{min-width:0}.ver,.hints span:nth-child(n+4){display:none}.dl{width:220px}}
</style></head><body>
<div class="app">
<header class="bar">
 <div class="logo"><svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="16"/><path d="M8.5 14v4M12.5 10v12M16.5 7.5v17M20.5 11v10M24 13.5v5"/></svg><div class="wm">PULSE<b>/</b>PAD</div><span class="ver">v4</span></div>
 <nav class="views" id="views"><button class="vbtn on" data-view="board"><span class="t" data-t="vBoard"></span></button><button class="vbtn" data-view="trim"><span class="t" data-t="vTrim"></span></button><button class="vbtn" data-view="settings"><span class="t" data-t="vSet"></span></button></nav>
 <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg><input id="q" type="text" autocomplete="off" spellcheck="false"><kbd>Ctrl K</kbd></div>
 <div class="bar-r">
  <div class="status" id="chip"></div>
  <div class="seg" id="lang" data-v="hu"><i></i><button data-l="hu" class="on">HU</button><button data-l="en">EN</button></div>
  <button class="ibtn" id="theme" aria-label="theme"><svg class="moon" viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg><svg class="sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></button>
  <button class="ibtn" id="help" aria-label="help">?</button>
 </div>
</header>
<div class="body">
 <section class="view hide" id="vSettings"><div class="setgrid">
  <section class="mod"><div class="mh"><span class="no">01</span><h3 class="t" data-t="route"></h3></div>
   <div class="path" id="path">
    <div class="port" id="p_mic"><div class="lab t" data-t="mic"></div><select id="mic"></select></div>
    <div class="port core" id="p_core"><div class="lab t" data-t="mix"></div><div class="row"><span>PULSE/PAD</span><b id="layers"></b></div></div>
    <div class="port" id="p_out"><div class="lab t" data-t="out"></div><select id="out"></select></div>
    <div class="port" id="p_mon"><div class="lab t" data-t="mon"></div><select id="mon"></select></div>
   </div>
  </section>
  <section class="mod"><div class="mh"><span class="no">02</span><h3 class="t" data-t="opts"></h3></div>
   <label class="switch"><span class="t" data-t="useMon"></span><input type="checkbox" id="use_mon"></label>
   <label class="switch"><span class="t" data-t="useMic"></span><input type="checkbox" id="use_mic"></label>
   <label class="switch"><span class="t" data-t="stack"></span><input type="checkbox" id="stack"></label>
   <label class="switch hide" id="trayrow"><span class="t" data-t="trayClose"></span><input type="checkbox" id="tray_close"></label>
  </section>
  <section class="mod"><div class="mh"><span class="no">03</span><h3 class="t" data-t="about"></h3></div>
   <div class="mvol" style="justify-content:space-between"><span id="verTxt"></span><button class="btn" id="chkUpd"><span class="t" data-t="updCheck"></span></button></div>
   <p class="tnote" id="updMsg" style="margin-top:10px"></p>
  </section>
 </div></section>
 <section class="view hide" id="vTrim"><div class="trim"><div class="tlist" id="tlist"></div><div class="te" id="te"></div></div></section>
 <main class="main" id="vBoard">
  <section class="deck" id="deck">
   <div class="dl">
    <div class="dlab"><i class="dot"></i><span class="t" data-t="nowPlaying"></span><span class="tags" id="dTags"></span></div>
    <div class="ttl" id="dTitle"></div>
    <div class="tm"><b id="dTime">00:00.00</b><span id="dTotal">/ 00:00.00</span></div>
   </div>
   <div class="dw"><canvas id="wave"></canvas></div>
   <div class="dm"><canvas id="meter"></canvas><div class="ml"><span>L</span><span>R</span></div></div>
  </section>
  <nav class="tabs" id="tabs"></nav>
  <div class="mhead">
   <h1><span class="t" data-t="sounds"></span><span class="count" id="count"></span></h1>
   <div class="filters" id="filters"></div>
   <div class="mvol"><span class="t" data-t="vol"></span><input type="range" id="vol" min="0" max="100" value="80"><b><span id="volv">80</span>%</b></div>
   <div class="actions"><button class="btn red" id="stop"><i class="sq"></i><span class="t" data-t="stop"></span></button><button class="btn primary" id="add"><span class="t" data-t="add"></span></button></div>
  </div>
  <div class="padwrap"><div id="pads" class="pads"></div></div>
 </main>
</div>
<footer class="foot"><div class="hints" id="hints"></div><div class="out" id="footOut"></div></footer>
</div>
<div id="drop" class="t" data-t="dropHere"></div>
<div class="ctx" id="ctx" role="menu"></div>
<div class="overlay" id="modal"><div class="modal" id="mbox" role="dialog" aria-modal="true" aria-labelledby="mTitle"><h3 id="mTitle"></h3><div class="mbody" id="mBody"></div><div class="mbtns" id="mBtns"></div></div></div>
<div class="toast" id="toast"></div>
<script>
(() => {
'use strict';
const T = {
 hu:{tag:"Nyomd. Halld.",trayClose:"Bezáráskor a tálcára kerüljön",search:"Hang keresése, Enter = lejátszás…",chipOk:"Audio fut",chipNo:"Nincs kimenet",chipErr:"Audio hiba",
  route:"Jelút",opts:"Beállítások",master:"Master",out:"Kimenet (virtuális kábel)",mon:"Fejhallgató / hangszóró",mic:"Mikrofon",mix:"Keverő",
  useMon:"Én is halljam",useMic:"Valódi mikrofon is menjen át",stack:"Hangok egymásra játszása",vol:"Hangerő",
  nowPlaying:"Most szól",standby:"Készenlét",layers:n=>n?n+" réteg":"üresjárat",
  stop:"Mindent leállít",noKb:"A gyorsbillentyűkhöz telepítsd: pip install keyboard (Linuxon rendszergazdai jog kell).",badKey:"Ez a billentyű nem használható.",
  sounds:"Hangok",add:"Hangok hozzáadása",count:n=>String(n),addTile:"Hang hozzáadása",noMatch:"Nincs találat erre a keresésre vagy szűrésre.",
  empty:"Húzz ide hangfájlokat (mp3, wav, ogg, flac), vagy add hozzá őket a gombbal.",dropHere:"Engedd el a hangfájlokat a hozzáadáshoz",
  sErr:"Hiba az audio indításakor:",loadErr:"Nem sikerült beolvasni:",
  ctxPlay:"Lejátszás",ctxStop:"Leállítás",ctxLoop:"Ismétlés (loop)",ctxFav:"Kedvenc",ctxColor:"Szín",ctxBind:"Gyorsbillentyű beállítása…",ctxUnbind:"Gyorsbillentyű törlése",ctxRename:"Átnevezés…",ctxDelete:"Eltávolítás a listából…",ctxMove:"Áthelyezés ide:",
  renameTitle:"Hang átnevezése",renameNote:"Csak a programban változik meg a név, a fájl neve marad. Üresen hagyva visszaáll az eredeti.",renamePh:"Hang neve",
  save:"Mentés",cancel:"Mégse",close:"Bezárás",delTitle:"Hang eltávolítása",delText:n=>`Eltávolítod ezt a listából: „${n}”? A fájl a gépeden megmarad.`,delBtn:"Eltávolítás",
  bindTitle:"Gyorsbillentyű",profBindTitle:"Profilváltó billentyű",bindText:"Nyomd meg a kívánt billentyűt vagy kombinációt.",bindHint:"Esc: mégse. Del: gyorsbillentyű törlése.",
  about:"Névjegy",verLabel:v=>"Verzió "+v,updCheck:"Frissítések keresése",updNone:"A program naprakész.",updFail:"A frissítések ellenőrzése nem sikerült (nincs net, vagy nincs beállítva a tároló).",
  updTitle:"Új verzió érhető el",updText:(v,c)=>`Elérhető a(z) ${v} verzió (jelenleg: ${c}). Letöltöd most?`,updDl:"Letöltés",updLater:"Később",updSkip:"Ezt a verziót kihagyom",
  updGet:"Letöltés folyamatban…",updReady:"A frissítés letöltve. A program újraindul, hogy telepítse.",updRestart:"Újraindítás és telepítés",updErr:"A letöltés nem sikerült:",updOpen:"Megnyitás böngészőben",
  vBoard:"Soundboard",vTrim:"Vágás",vSet:"Beállítások",trimList:"Hangok",trimPick:"Válassz egy hangot a vágáshoz.",trimNone:"Ebben a profilban még nincs hang. Adj hozzá a Soundboard fülön.",
  tStart:"Kezdet",tEnd:"Vége",tLen:"Hossz",tFadeIn:"Fade in",tFadeOut:"Fade out",tOrig:"Eredeti hossz:",tPrev:"Előnézet",tPrevStop:"Leállítás",tReset:"Visszaállítás",tApply:"Alkalmaz",
  tNote:"Nem destruktív: az eredeti fájl nem változik, a vágás bármikor visszaállítható. Az előnézet csak a fejhallgatón szól, a mikrofonra nem megy. Fogd meg a két sárga fogantyút a hullámformán, vagy írd be az időket.",
  tDirty:"Nem mentett változás",tSaved:"A vágás elmentve.",noMon:"Az előnézethez kapcsold be az „Én is halljam” opciót a Beállításokban.",ctxTrim:"Vágás…",
  how8:"Vágás: a Vágás fülön válassz hangot, húzd a két fogantyút (vagy írd be az időket) az eleje és a vége levágásához, állíts fade in/out-ot, hallgasd meg előnézetben, majd nyomd meg az Alkalmaz gombot. Az eredeti fájl nem változik.",
  tabAdd:"Új profil",profAddTitle:"Új profil",profAddNote:"Külön hangkészlet, például játékhoz, streamhez vagy csevegéshez.",profPh:"Profil neve",pRenameTitle:"Profil átnevezése",
  pRename:"Profil átnevezése…",pBind:"Váltás gyorsbillentyűvel…",pUnbind:"Váltó billentyű törlése",pFolder:"Figyelt mappa kijelölése…",pFolderClear:"Figyelt mappa megszüntetése",pDelete:"Profil törlése…",
  pDelTitle:"Profil törlése",pDelText:(n,c)=>`Törlöd a(z) „${n}” profilt${c?` és a benne lévő ${c} hangot a listából`:''}? A fájlok a gépeden megmaradnak.`,
  folderNow:"Figyelt mappa:",folderAdded:n=>n+" hang került a listára a mappából.",
  favOnly:"Csak a kedvencek",colorFilter:"Szűrés színre",clearFilters:"Szűrők törlése",
  hints:[["Esc","leállít"],["1–9","gyors indítás"],["Ctrl K","keresés"],["jobb klikk","menü"],["húzás","átrendezés"],["hullám","tekerés"]],
  howTitle:"Hogyan működik",how1:"A kimenetnek válassz egy virtuális kábelt (pl. VB-Cable vagy Voicemeeter), a chat programban pedig ennek a kábelnek a bemenetét mikrofonnak.",
  how2:"Jobb klikk egy padon: gyorsbillentyű, átnevezés, hangerő, ismétlés (loop), kedvenc, szín, áthelyezés másik profilba, eltávolítás. A gyorsbillentyű akkor is működik, ha másik ablak van előtérben, de csak az aktív profilban él, így több profilban is használhatod ugyanazt a billentyűt. Alapból egyetlen hang sincs billentyűhöz kötve.",
  how3:"Alapból egy új hang indításakor az előző elhalkul. A „Hangok egymásra játszása” bekapcsolásával több hang szólhat egyszerre. Az Esc mindent leállít.",
  how4:"A padokat megfogva átrendezheted őket. Az ablakban az 1–9 gombok az első kilenc látható padot indítják, a keresőben az Enter az első találatot. A pad hullámformája lejátszás közben töltődik. A felső kijelző hullámformájára kattintva vagy ráhúzva tekerhetsz a szóló hangban.",
  how5:"Profilok: a fülek külön hangkészletek (játék, stream, csevegés). A + gombbal újat hozhatsz létre, a fülön jobb klikkel átnevezheted, törölheted, váltóbillentyűt vagy figyelt mappát adhatsz neki. A figyelt mappába bedobott hangok pár másodpercen belül megjelennek az adott profilban.",
  how6:"A pad sarkában lévő csillaggal kedvencet jelölhetsz. A lista feletti csillag gombbal csak a kedvenceket, a színpöttyökkel pedig egy adott színű padokat láthatod.",
  how7:"A „Valódi mikrofon is menjen át” bekapcsolásával a saját hangod is hallható marad. A hangok a gépeden maradnak, a program semmit sem küld el."},
 en:{tag:"Hit it. Hear it.",trayClose:"Close to the system tray",search:"Find a sound, Enter = play…",chipOk:"Audio running",chipNo:"No output",chipErr:"Audio error",
  route:"Signal path",opts:"Options",master:"Master",out:"Output (virtual cable)",mon:"Headphones / speakers",mic:"Microphone",mix:"Mixer",
  useMon:"Let me hear it too",useMic:"Pass the real microphone through",stack:"Layer sounds on top of each other",vol:"Volume",
  nowPlaying:"Now playing",standby:"Standby",layers:n=>n?n+(n>1?" layers":" layer"):"idle",
  stop:"Stop everything",noKb:"Hotkeys need the keyboard package: pip install keyboard (on Linux it needs root).",badKey:"This key can’t be used.",
  sounds:"Sounds",add:"Add sounds",count:n=>String(n),addTile:"Add a sound",noMatch:"Nothing matches this search or filter.",
  empty:"Drop audio files here (mp3, wav, ogg, flac), or add them with the button.",dropHere:"Drop audio files to add them",
  sErr:"Audio failed to start:",loadErr:"Couldn't read:",
  ctxPlay:"Play",ctxStop:"Stop",ctxLoop:"Loop",ctxFav:"Favorite",ctxColor:"Color",ctxBind:"Set hotkey…",ctxUnbind:"Clear hotkey",ctxRename:"Rename…",ctxDelete:"Remove from list…",ctxMove:"Move to:",
  renameTitle:"Rename sound",renameNote:"Only the name inside the app changes, the file keeps its name. Leave empty to restore the original.",renamePh:"Sound name",
  save:"Save",cancel:"Cancel",close:"Close",delTitle:"Remove sound",delText:n=>`Remove “${n}” from the list? The file stays on your computer.`,delBtn:"Remove",
  bindTitle:"Hotkey",profBindTitle:"Profile hotkey",bindText:"Press the key or combination you want.",bindHint:"Esc: cancel. Del: clear hotkey.",
  about:"About",verLabel:v=>"Version "+v,updCheck:"Check for updates",updNone:"You're up to date.",updFail:"Couldn't check for updates (offline, or no repository is configured).",
  updTitle:"A new version is available",updText:(v,c)=>`Version ${v} is available (you have ${c}). Download it now?`,updDl:"Download",updLater:"Later",updSkip:"Skip this version",
  updGet:"Downloading…",updReady:"The update is downloaded. The app will restart to install it.",updRestart:"Restart and install",updErr:"Download failed:",updOpen:"Open in browser",
  vBoard:"Soundboard",vTrim:"Trim",vSet:"Settings",trimList:"Sounds",trimPick:"Pick a sound to trim.",trimNone:"This profile has no sounds yet. Add some on the Soundboard tab.",
  tStart:"Start",tEnd:"End",tLen:"Length",tFadeIn:"Fade in",tFadeOut:"Fade out",tOrig:"Original length:",tPrev:"Preview",tPrevStop:"Stop",tReset:"Reset",tApply:"Apply",
  tNote:"Non-destructive: the original file never changes and the trim can be undone any time. Preview plays on your headphones only, not on the microphone. Drag the two yellow handles on the waveform, or type the times.",
  tDirty:"Unsaved changes",tSaved:"Trim saved.",noMon:"Turn on “Let me hear it too” in Settings to use the preview.",ctxTrim:"Trim…",
  how8:"Trim: pick a sound on the Trim tab, drag the two handles (or type the times) to cut the start and the end, set a fade in/out, listen to the preview, then press Apply. The original file is never changed.",
  tabAdd:"New profile",profAddTitle:"New profile",profAddNote:"A separate sound set, for example for gaming, streaming or chatting.",profPh:"Profile name",pRenameTitle:"Rename profile",
  pRename:"Rename profile…",pBind:"Switch with a hotkey…",pUnbind:"Clear switch hotkey",pFolder:"Choose watched folder…",pFolderClear:"Stop watching folder",pDelete:"Delete profile…",
  pDelTitle:"Delete profile",pDelText:(n,c)=>`Delete the “${n}” profile${c?` and remove its ${c} sound${c>1?'s':''} from the list`:''}? The files stay on your computer.`,
  folderNow:"Watched folder:",folderAdded:n=>n+(n>1?" sounds":" sound")+" added from the folder.",
  favOnly:"Favorites only",colorFilter:"Filter by color",clearFilters:"Clear filters",
  hints:[["Esc","stop"],["1–9","quick play"],["Ctrl K","search"],["right click","menu"],["drag","reorder"],["wave","seek"]],
  howTitle:"How it works",how1:"Choose a virtual cable (e.g. VB-Cable or Voicemeeter) as the output, then pick that cable's input as your microphone in your chat app.",
  how2:"Right-click a pad: hotkey, rename, volume, loop, favorite, color, move to another profile, remove. Hotkeys work even when another window is in front, but only in the active profile, so you can reuse the same key in several profiles. No sound is bound to any key by default.",
  how3:"Normally a new sound fades out the previous one. Turn on “Layer sounds” to let several play at once. Esc stops everything.",
  how4:"Drag pads to reorder them. In the window, keys 1–9 play the first nine visible pads, and Enter in the search box plays the first match. A pad's waveform fills up as it plays. Click or drag on the waveform in the top display to seek within the playing sound.",
  how5:"Profiles: the tabs are separate sound sets (gaming, streaming, chatting). Use + to add one; right-click a tab to rename or delete it, or give it a switch hotkey or a watched folder. Sounds dropped into a watched folder show up in that profile within a few seconds.",
  how6:"Use the star in a pad's corner to mark a favorite. The star button above the list shows favorites only, and the color dots show only pads of that color.",
  how7:"Turn on “Pass the real microphone through” to keep your own voice audible. Your sounds stay on your computer and nothing is uploaded."}
};
const ICON={
 play:'<path d="M5 3.2v9.6l7.6-4.8z"/>',stop:'<rect x="3.8" y="3.8" width="8.4" height="8.4" rx="1"/>',
 key:'<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="M4.2 6.5h.6M7.7 6.5h.6M11.2 6.5h.6M5 9.6h6"/>',
 x:'<path d="M4 4l8 8M12 4l-8 8"/>',pen:'<path d="M2.8 13.2l.7-3.1L11 2.6l2.4 2.4-7.5 7.5zM9.4 4.2l2.4 2.4"/>',
 trash:'<path d="M2.8 4.5h10.4M6.4 4.5V3h3.2v1.5M4.3 4.5l.6 8.5h6.2l.6-8.5M7 7v3.6M9 7v3.6"/>',
 vol:'<path d="M2.2 6.3h2.6L8.4 3.4v9.2L4.8 9.7H2.2zM11 5.8a3.1 3.1 0 010 4.4"/>',
 loop:'<path d="M3 8a5 5 0 019-3M12.2 2.4V5H9.6M13 8a5 5 0 01-9 3M3.8 13.6V11h2.6"/>',
 star:'<path d="M8 1.8l1.9 4 4.4.5-3.3 3 .9 4.3L8 11.4l-3.9 2.2.9-4.3-3.3-3 4.4-.5z"/>',
 folder:'<path d="M1.8 4.2c0-.7.5-1.2 1.2-1.2h3l1.5 1.7H13c.7 0 1.2.5 1.2 1.2v6c0 .7-.5 1.2-1.2 1.2H3c-.7 0-1.2-.5-1.2-1.2z"/>',
 move:'<path d="M2.5 8h10M9 4.5L12.5 8 9 11.5"/>',
 cut:'<circle cx="4" cy="4" r="1.8"/><circle cx="4" cy="12" r="1.8"/><path d="M5.5 5.2L14 12M5.5 10.8L14 4"/>'};
const ico=n=>{const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('viewBox','0 0 16 16');s.setAttribute('class','ico');s.setAttribute('aria-hidden','true');s.innerHTML=ICON[n];return s};
const $=s=>document.querySelector(s);
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
const api=()=>window.pywebview.api;
let lang='hu',theme='dark',ST=null,sounds=[],volT=0,toastT=0,drag=0,listening=0,modalCancel=null,ctxPad=null,dragId=0,cur=undefined;
let fFav=false,fColor=null,syncing=false,hold=0,holdId=0;
let view='board',TR={id:null,dur:0,peaks:[],t0:0,t1:0,fi:0,fo:0,pv:null,playing:false,ps:0},TE=null,trc=null,trx=null,tdrag=null;
const live=new Map(),started=new Map(),padRefs=new Map(),masks=new Map();
const lv={t:[0,0],d:[0,0]};
let COL={c:[]};
const NCOL=8;
const pig=s=>(s.color!==null&&s.color!==undefined)?s.color:(s.hue%NCOL);
const fmtKey=k=>k.split('+').map(p=>p.length===1?p.toUpperCase():p[0].toUpperCase()+p.slice(1)).join('+');
const fmtDur=s=>{s=+s||0;if(s<60)return s.toFixed(1)+' s';const m=Math.floor(s/60),r=Math.round(s%60);return m+':'+String(r).padStart(2,'0')};
const clock=s=>{s=Math.max(0,+s||0);const m=Math.floor(s/60);return String(m).padStart(2,'0')+':'+(s-m*60).toFixed(2).padStart(5,'0')};
const byId=id=>sounds.find(s=>s.id===id);
const metaText=s=>fmtDur(s.dur)+(Math.abs(s.vol-1)>.004?' · '+Math.round(s.vol*100)+'%':'');
const query=()=>($('#q').value||'').trim().toLowerCase();
const filtered=()=>!!query()||fFav||fColor!==null;
const match=s=>{const q=query();return (!q||s.name.toLowerCase().includes(q))&&(!fFav||s.fav)&&(fColor===null||pig(s)===fColor)};
const visible=()=>sounds.filter(match);

function toast(msg,info){const t=$('#toast');t.textContent=msg;t.classList.toggle('info',!!info);t.classList.add('on');clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('on'),4500)}
function fillSelect(sel,names,value){sel.textContent='';names.forEach(n=>{const o=document.createElement('option');o.value=o.textContent=n;sel.appendChild(o)});if(names.includes(value))sel.value=value}
function setVol(v){$('#vol').style.setProperty('--v',v+'%');$('#volv').textContent=v}
function readColors(){const c=getComputedStyle(document.documentElement),g=n=>c.getPropertyValue(n).trim();
 COL={bg:g('--bg'),muted:g('--muted'),text:g('--text'),wave:g('--wave'),dim:g('--dim'),line:g('--line2'),accent:g('--accent'),ok:g('--ok'),warn:g('--warn'),bad:g('--bad'),c:[...Array(NCOL).keys()].map(i=>g('--c'+i))}}

/* ---------- hullámforma maszk (SVG) ---------- */
function maskFromPeaks(pk,n){
 const per=pk.length/n;let d='';
 for(let i=0;i<n;i++){
  let m=0;const a=Math.floor(i*per),b=Math.max(a+1,Math.floor((i+1)*per));
  for(let j=a;j<b&&j<pk.length;j++)if(pk[j]>m)m=pk[j];
  const h=Math.max(1.3,m*15);
  d+='M'+(i*2+1)+' '+(16-h).toFixed(1)+'V'+(16+h).toFixed(1);
 }
 const svg="<svg xmlns='http://www.w3.org/2000/svg' width='"+n*2+"' height='32' viewBox='0 0 "+n*2+" 32' preserveAspectRatio='none'><path d='"+d+"' stroke='#000' stroke-width='1.1' stroke-linecap='round' fill='none'/></svg>";
 return 'url("data:image/svg+xml,'+encodeURIComponent(svg)+'")';
}
function maskOf(s){
 let m=masks.get(s.id);
 if(!m){m=maskFromPeaks(s.peaks&&s.peaks.length?s.peaks:new Array(48).fill(.12),48);masks.set(s.id,m)}
 return m;
}

/* ---------- megjelenítés ---------- */
function renderText(){
 const t=T[lang];
 document.querySelectorAll('[data-t]').forEach(e=>e.textContent=t[e.dataset.t]);
 $('#q').placeholder=t.search;if(ST)$('#verTxt').textContent=t.verLabel(ST.version);
 $('#count').textContent=t.count(sounds.length);
 const h=$('#hints');h.textContent='';
 t.hints.forEach(([k,l])=>{const s=el('span');s.appendChild(el('kbd','',k));s.appendChild(document.createTextNode(l));h.appendChild(s)});
 cur=undefined;
 renderStatus();renderTabs();renderFilters();renderPads();updatePorts();renderTrim();
}
function renderStatus(){
 if(!ST)return;const t=T[lang],c=$('#chip');
 c.className='status'+(ST.status==='ok'?'':ST.status==='no_out'?' warn':' bad');
 c.textContent=ST.status==='ok'?t.chipOk:ST.status==='no_out'?t.chipNo:t.chipErr;
 c.title=ST.status==='error'?`${t.sErr} ${ST.detail}`:'';
 $('#footOut').textContent=ST.settings.out?'OUT ▸ '+ST.settings.out+' · 44.1 kHz':'';
}
function updatePorts(){
 if(!ST)return;const s=ST.settings,t=T[lang],n=live.size;
 $('#p_mic').classList.toggle('on',s.use_mic);$('#p_mic').classList.toggle('off',!s.use_mic);
 $('#p_mon').classList.toggle('on',s.use_mon);$('#p_mon').classList.toggle('off',!s.use_mon);
 $('#p_out').classList.toggle('on',ST.status==='ok');
 $('#p_core').classList.toggle('on',n>0);
 $('#path').classList.toggle('live',n>0);
 $('#layers').textContent=t.layers(n);
}
function renderTabs(){
 const t=T[lang],box=$('#tabs');box.textContent='';
 if(!ST||!ST.profiles)return;
 ST.profiles.forEach(p=>{
  const b=el('button','tab'+(p.id===ST.active?' on':''));b.dataset.pid=p.id;b.title=p.name;
  b.appendChild(el('span','',p.name));
  if(p.hotkey)b.appendChild(el('span','key',fmtKey(p.hotkey)));
  b.appendChild(el('span','n',String(p.count)));
  b.onclick=()=>switchProfile(p.id);
  b.addEventListener('contextmenu',e=>{e.preventDefault();e.stopPropagation();openProfileMenu(p.id,e.clientX,e.clientY,b)});
  box.appendChild(b);
 });
 const a=el('button','tab add','+');a.title=t.tabAdd;a.setAttribute('aria-label',t.tabAdd);a.onclick=addProfileModal;box.appendChild(a);
}
function renderFilters(){
 const t=T[lang],f=$('#filters');f.textContent='';
 if(!sounds.length&&!fFav&&fColor===null){f.classList.add('hide');return}
 f.classList.remove('hide');
 const fb=el('button','fbtn'+(fFav?' on':''));fb.appendChild(ico('star'));fb.title=t.favOnly;fb.setAttribute('aria-label',t.favOnly);fb.setAttribute('aria-pressed',String(fFav));
 fb.onclick=()=>{fFav=!fFav;renderFilters();renderPads()};f.appendChild(fb);
 f.appendChild(el('i','fsep'));
 const used=new Set(sounds.map(pig));
 for(let k=0;k<NCOL;k++){
  const b=el('button','sw8'+(fColor===k?' on':'')+(used.has(k)||fColor===k?'':' dim'));
  b.style.setProperty('--ink','var(--c'+k+')');b.title=t.colorFilter;b.setAttribute('aria-label',t.colorFilter+' '+(k+1));
  b.onclick=()=>{fColor=fColor===k?null:k;renderFilters();renderPads()};f.appendChild(b);
 }
 if(fFav||fColor!==null){
  const x=el('button','fbtn');x.appendChild(ico('x'));x.title=t.clearFilters;x.setAttribute('aria-label',t.clearFilters);
  x.onclick=()=>{fFav=false;fColor=null;renderFilters();renderPads()};f.appendChild(x);
 }
}
function buildPad(s,i,canDrag){
 const root=el('div','pad');root.dataset.id=s.id;root.title=s.name;root.tabIndex=0;root.setAttribute('role','button');
 root.style.setProperty('--ink','var(--c'+pig(s)+')');
 root.style.setProperty('--m',maskOf(s));
 const top=el('div','top'),idx=el('div','idx');idx.appendChild(el('i','led'));idx.appendChild(el('span','',String(i+1).padStart(2,'0')));top.appendChild(idx);
 const tr=el('div','tr');
 if(s.hotkey)tr.appendChild(el('span','key',fmtKey(s.hotkey)));
 const fv=el('button','fav'+(s.fav?' on':''));fv.appendChild(ico('star'));fv.title=T[lang].ctxFav;fv.setAttribute('aria-label',T[lang].ctxFav);fv.setAttribute('aria-pressed',String(!!s.fav));
 fv.onclick=e=>{e.stopPropagation();toggleFav(s.id)};
 fv.addEventListener('keydown',e=>e.stopPropagation());
 fv.addEventListener('dblclick',e=>e.stopPropagation());
 tr.appendChild(fv);top.appendChild(tr);
 root.appendChild(top);
 const wv=el('div','wv');wv.appendChild(el('i','b'));wv.appendChild(el('i','f'));wv.appendChild(el('u'));root.appendChild(wv);
 root.appendChild(el('div','nm',s.name));
 const meta=el('div','meta'),mt=el('span','mt',metaText(s)),ic=el('span','ic');meta.appendChild(mt);
 if(s.trimmed)ic.appendChild(ico('cut'));if(s.loop)ic.appendChild(ico('loop'));
 if(ic.firstChild)meta.appendChild(ic);root.appendChild(meta);
 root.onclick=()=>toggle(s.id);
 root.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle(s.id)}});
 root.addEventListener('contextmenu',e=>{e.preventDefault();e.stopPropagation();openMenu(s.id,e.clientX,e.clientY,root)});
 if(canDrag){
  root.draggable=true;
  root.addEventListener('dragstart',e=>{dragId=s.id;e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain','pulsepad:'+s.id);root.classList.add('dragging');closeMenu()});
  root.addEventListener('dragend',()=>{dragId=0;document.querySelectorAll('.pad.over,.pad.dragging').forEach(x=>x.classList.remove('over','dragging'))});
  root.addEventListener('dragover',e=>{if(!dragId||dragId===s.id)return;e.preventDefault();e.dataTransfer.dropEffect='move';root.classList.add('over')});
  root.addEventListener('dragleave',()=>root.classList.remove('over'));
  root.addEventListener('drop',async e=>{if(!dragId)return;e.preventDefault();e.stopPropagation();root.classList.remove('over');
   const to=sounds.findIndex(x=>x.id===s.id),id=dragId;dragId=0;apply(await api().move(id,to))});
 }
 padRefs.set(s.id,{root,fav:fv});
 if(live.has(s.id))root.classList.add('playing');
 return root;
}
function renderPads(){
 const t=T[lang],g=$('#pads');g.textContent='';padRefs.clear();
 if(!sounds.length){
  g.className='';
  const e=el('div','empty'),gh=el('div','ghosts');
  for(let j=0;j<8;j++){const c=el('i'),b=el('b');
   const pk=Array.from({length:48},(_,k)=>.12+.88*Math.abs(Math.sin(k*.55+j*1.9))*Math.abs(Math.cos(k*.19+j*.7)));
   b.style.setProperty('--m',maskFromPeaks(pk,48));c.appendChild(b);gh.appendChild(c)}
  e.appendChild(gh);
  const box=el('div','emptybox'),h=el('h2'),w=t.tag.split(' ');
  h.appendChild(document.createTextNode(w[0]));h.appendChild(el('em','',w.slice(1).join(' ')));
  box.appendChild(h);box.appendChild(el('p','',t.empty));
  const b=el('button','btn primary',t.add);b.onclick=addDialog;box.appendChild(b);e.appendChild(box);g.appendChild(e);return}
 g.className='pads';
 const flt=filtered();
 const list=sounds.map((s,i)=>[s,i]).filter(([s])=>match(s));
 if(!list.length){g.appendChild(el('div','nomatch',t.noMatch));return}
 list.forEach(([s,i])=>g.appendChild(buildPad(s,i,!flt)));
 if(!flt){
  const a=el('button','pad addtile');a.appendChild(el('span','plus','+'));a.appendChild(el('span','l',t.addTile));a.onclick=addDialog;g.appendChild(a)}
}
/* ---------- frissítés ---------- */
let updShown='',updBusy=false;
function offerUpdate(force){
 const u=ST&&ST.update;if(!u||updBusy||modalCancel&&!force)return;
 if(!force&&(updShown===u.version||ST.settings.skip_update===u.version))return;
 updShown=u.version;const t=T[lang];
 const body=el('div');body.appendChild(el('p','',t.updText(u.version,ST.version)));
 if(u.notes){const n=el('p','',u.notes);n.style.cssText='margin-top:10px;max-height:180px;overflow:auto;white-space:pre-wrap;font:12px var(--mono);color:var(--muted)';body.appendChild(n)}
 openModal({title:t.updTitle,body,buttons:[
  {label:t.updSkip,onClick:()=>{closeModal();ST.settings.skip_update=u.version;api().skip_update(u.version)}},
  {label:t.updLater,onClick:closeModal},
  {label:u.can_install?t.updDl:t.updOpen,cls:'primary',focus:true,onClick:()=>downloadUpdate(u)}]});
}
async function downloadUpdate(u){
 const t=T[lang];updBusy=true;
 if(!u.can_install){closeModal();updBusy=false;api().start_update();return}
 const body=el('div'),msg=el('p','',t.updGet),bar=el('div');
 bar.style.cssText='height:6px;margin-top:14px;background:var(--line);border-radius:2px;overflow:hidden';
 const fill=el('i');fill.style.cssText='display:block;height:100%;width:0;background:var(--accent);transition:width .2s';bar.appendChild(fill);
 body.appendChild(msg);body.appendChild(bar);
 openModal({title:t.updTitle,body,buttons:[]});
 await api().start_update();
 const iv=setInterval(async()=>{
  const s=await api().update_status();
  fill.style.width=Math.round((s.p||0)*100)+'%';
  if(s.state==='ready'){
   clearInterval(iv);msg.textContent=t.updReady;fill.style.width='100%';
   const bt=$('#mBtns');bt.textContent='';
   const go=el('button','btn primary',t.updRestart);go.onclick=()=>api().apply_update();bt.appendChild(go);go.focus();
  }else if(s.state==='error'){
   clearInterval(iv);updBusy=false;msg.textContent=t.updErr+' '+s.error;
   const bt=$('#mBtns');bt.textContent='';
   const c=el('button','btn',t.close);c.onclick=closeModal;bt.appendChild(c);
  }
 },300);
}
async function checkUpdateNow(){
 const t=T[lang],m=$('#updMsg');m.textContent='…';
 const r=await api().check_update_now();ST=r;
 if(r.update){m.textContent='';offerUpdate(true)}
 else m.textContent=r.status==='ok'?t.updNone:t.updFail;
}
function apply(st){ST=st;sounds=st.sounds;cur=undefined;offerUpdate();$('#verTxt').textContent=T[lang].verLabel(st.version);$('#count').textContent=T[lang].count(sounds.length);renderStatus();renderTabs();renderFilters();renderPads();updatePorts();trimAfterApply()}
window.refresh=async errors=>{
 drag=0;document.body.classList.remove('dragging');
 apply(await api().get_state());
 if(errors&&errors.length)toast(T[lang].loadErr+'\n'+errors.join('\n'));
};
async function addDialog(){
 const r=await api().add_dialog();
 if(r&&r.ok){apply(r);if(r.errors&&r.errors.length)toast(T[lang].loadErr+'\n'+r.errors.join('\n'))}
}

/* ---------- profilok, kedvencek ---------- */
async function switchProfile(id){
 if(!ST||id===ST.active)return;
 [...live.keys()].forEach(dropLive);
 apply(await api().set_profile(id));
}
function toggleFav(id){
 const s=byId(id);if(!s)return;
 s.fav=!s.fav;api().set_fav(id,s.fav);
 const r=padRefs.get(id);
 if(r){r.fav.classList.toggle('on',s.fav);r.fav.setAttribute('aria-pressed',String(s.fav))}
 if(fFav&&!s.fav)renderPads();
}

/* ---------- lejátszás ---------- */
function setPlaying(id,on){
 const r=padRefs.get(id);if(!r)return;
 r.root.classList.toggle('playing',on);
 if(!on)r.root.style.setProperty('--p',0);
}
function dropLive(id){live.delete(id);started.delete(id);setPlaying(id,false)}
async function toggle(id){
 if(listening)return;
 const now=performance.now();
 if(live.has(id))dropLive(id);
 else{
  if(!ST.settings.stack)[...live.keys()].forEach(dropLive);
  live.set(id,{p:0,t:now});started.set(id,now);setPlaying(id,true);
 }
 updatePorts();
 try{await api().toggle(id)}catch(e){}
}
async function poll(){
 if(document.hidden||!window.pywebview||!ST||syncing)return;
 try{
  const r=await api().playing(),now=performance.now(),ids=new Set(Object.keys(r.p).map(Number));
  ids.forEach(id=>{
   const p=r.p[id],o=live.get(id);
   if(!o){live.set(id,{p,t:now});started.set(id,now);setPlaying(id,true)}
   else if(!(id===holdId&&now<hold)){o.p=p;o.t=now}
  });
  [...live.keys()].forEach(id=>{if(!ids.has(id))dropLive(id)});
  lv.t=r.lv;TR.pv=r.pv;
  if(TR.playing&&r.pv==null&&now-TR.ps>500){TR.playing=false;updatePrev()}
  if(r.ap!==ST.active||r.rev!==ST.rev){
   syncing=true;
   try{apply(await api().get_state())}finally{syncing=false}
  }
  updatePorts();
 }catch(e){syncing=false}
}

/* ---------- kijelző (deck) + mérő + tekerés ---------- */
const wc=$('#wave'),wx=wc.getContext('2d'),mc=$('#meter'),mx=mc.getContext('2d');
const geom={x0:0,n:1,step:1,d:1};
let scrubId=0,scrubT=0;
function fit(c){const r=c.getBoundingClientRect(),d=window.devicePixelRatio||1,w=Math.max(1,Math.round(r.width*d)),h=Math.max(1,Math.round(r.height*d));if(c.width!==w||c.height!==h){c.width=w;c.height=h}return d}
function fracAt(e){const r=wc.getBoundingClientRect(),x=(e.clientX-r.left)*geom.d;return Math.max(0,Math.min(1,(x-geom.x0)/(geom.n*geom.step)))}
function scrubTo(e,force){
 const o=live.get(scrubId);if(!o)return;
 const f=fracAt(e),now=performance.now();
 o.p=f;o.t=now;
 if(force||now-scrubT>40){scrubT=now;api().seek(scrubId,f)}
}
wc.addEventListener('pointerdown',e=>{
 if(e.button!==0||cur===null||cur===undefined||!live.has(cur))return;
 scrubId=cur;holdId=cur;hold=Infinity;
 try{wc.setPointerCapture(e.pointerId)}catch(_){}
 scrubTo(e,true);
});
wc.addEventListener('pointermove',e=>{if(scrubId)scrubTo(e,false)});
const endScrub=e=>{if(!scrubId)return;scrubTo(e,true);hold=performance.now()+250;scrubId=0};
wc.addEventListener('pointerup',endScrub);
wc.addEventListener('pointercancel',endScrub);
function setCurrent(id){
 if(id===cur)return;cur=id;
 const t=T[lang],deck=$('#deck'),tags=$('#dTags');tags.textContent='';
 const s=id!==null?byId(id):null;
 deck.classList.toggle('live',!!s);
 if(s){
  deck.style.setProperty('--ink','var(--c'+pig(s)+')');
  $('#dTitle').textContent=s.name;$('#dTotal').textContent='/ '+clock(s.dur);
  if(s.loop)tags.appendChild(el('span','tag','LOOP'));
  if(s.hotkey)tags.appendChild(el('span','tag',fmtKey(s.hotkey)));
 }else{$('#dTitle').textContent=t.standby;$('#dTotal').textContent='/ 00:00.00';$('#dTime').textContent='00:00.00'}
}
function drawWave(now,s,p){
 const d=fit(wc),W=wc.width,H=wc.height,mid=H/2;wx.clearRect(0,0,W,H);
 const step=5*d,bw=3*d,n=Math.max(8,Math.floor((W-36*d)/step)),x0=(W-n*step)/2+(step-bw)/2;
 if(!s){
  const pos=((now/2200)%1.25-.1)*n;
  for(let i=0;i<n;i++){
   const k=Math.exp(-((i-pos)*(i-pos))/7),h=(1.2+k*H*.2)*(i%2?1:.85);
   wx.globalAlpha=.35+k*.65;wx.fillStyle=k>.15?COL.accent:COL.dim;
   wx.fillRect(x0+i*step,mid-h,bw,h*2);
  }
  wx.globalAlpha=1;return;
 }
 geom.x0=x0;geom.n=n;geom.step=step;geom.d=d;
 const pk=s.peaks&&s.peaks.length?s.peaks:[.2],L=pk.length,ink=COL.c[pig(s)];
 for(let i=0;i<n;i++){
  const a=Math.floor(i/n*L),b=Math.max(a+1,Math.floor((i+1)/n*L));let m=0;
  for(let j=a;j<b&&j<L;j++)if(pk[j]>m)m=pk[j];
  const h=Math.max(1.5*d,m*H*.4);
  wx.fillStyle=(i+.5)/n<=p?ink:COL.wave;
  wx.fillRect(x0+i*step,mid-h,bw,h*2);
 }
 const px=x0+p*n*step;
 wx.fillStyle=COL.text;wx.fillRect(px-d*.75,6*d,1.5*d,H-12*d);
 wx.beginPath();wx.moveTo(px-4*d,6*d);wx.lineTo(px+4*d,6*d);wx.lineTo(px,11*d);wx.fill();
}
const lvl=v=>v<=.0006?0:Math.max(0,Math.min(1,(20*Math.log10(v)+54)/54));
function drawMeter(){
 const d=fit(mc),W=mc.width,H=mc.height,segs=22,gap=2*d,sh=(H-gap*(segs-1))/segs,cw=(W-6*d)/2;mx.clearRect(0,0,W,H);
 for(let c=0;c<2;c++){
  const lit=Math.round(lvl(lv.d[c])*segs);
  for(let i=0;i<segs;i++){
   const y=H-(i+1)*sh-i*gap;
   mx.fillStyle=i<lit?(i>=segs-2?COL.bad:i>=segs-7?COL.warn:COL.ok):COL.line;
   mx.globalAlpha=i<lit?1:.45;
   mx.fillRect(c*(cw+6*d),y,cw,sh);
  }
 }
 mx.globalAlpha=1;
}
function frame(now){
 requestAnimationFrame(frame);
 if(document.hidden||!ST)return;
 let best=null,bt=-1;
 live.forEach((o,id)=>{
  const s=byId(id),dur=s&&s.dur>0?s.dur:1;
  let p=o.p+(now-o.t)/1000/dur;p=s&&s.loop?p%1:Math.min(p,.999);o.cp=p;
  const r=padRefs.get(id);if(r)r.root.style.setProperty('--p',p.toFixed(4));
  const st=started.get(id)||0;if(st>=bt){bt=st;best=id}
 });
 for(let c=0;c<2;c++){const t=lv.t[c]||0;lv.d[c]+=(t-lv.d[c])*(t>lv.d[c]?.7:.14)}
 if(view==='trim'){drawTrim();return}
 if(view!=='board')return;
 setCurrent(best);
 const s=best!==null?byId(best):null,o=best!==null?live.get(best):null;
 drawWave(now,s,o?o.cp||0:0);
 if(s&&o)$('#dTime').textContent=clock((o.cp||0)*s.dur);
 drawMeter();
}

/* ---------- jobb klikk menü ---------- */
function closeMenu(){
 $('#ctx').classList.remove('on');
 if(ctxPad){ctxPad.classList.remove('menu-open');ctxPad=null}
}
function menuNew(){closeMenu();const m=$('#ctx');m.textContent='';return m}
function menuAdd(m,icon,label,fn,cls){
 const b=el('button',cls||'');b.setAttribute('role','menuitem');
 b.appendChild(ico(icon));b.appendChild(el('span','',label));
 b.onclick=()=>{closeMenu();fn()};m.appendChild(b);return b}
function menuShow(m,x,y,host){
 ctxPad=host;host.classList.add('menu-open');
 m.classList.add('on');
 const w=m.offsetWidth,h=m.offsetHeight;
 m.style.left=Math.max(8,Math.min(x,innerWidth-w-8))+'px';
 m.style.top=Math.max(8,Math.min(y,innerHeight-h-8))+'px';
 const f=m.querySelector('button');if(f)f.focus({preventScroll:true});
}
function openMenu(id,x,y,padEl){
 const s=byId(id);if(!s)return;
 const t=T[lang],m=menuNew(),playing=live.has(id);
 menuAdd(m,playing?'stop':'play',playing?t.ctxStop:t.ctxPlay,()=>toggle(id));
 const vr=el('div','vrow');vr.appendChild(ico('vol'));
 const sl=document.createElement('input');sl.type='range';sl.min=0;sl.max=150;sl.value=Math.round(s.vol*100);sl.setAttribute('aria-label',t.vol);sl.title=t.vol;
 const pc=el('b','');
 const paint=()=>{sl.style.setProperty('--v',(sl.value/150*100)+'%');pc.textContent=sl.value+'%'};paint();
 let vt=0;
 sl.addEventListener('input',()=>{paint();s.vol=sl.value/100;
  const mt=document.querySelector('.pad[data-id="'+id+'"] .mt');if(mt)mt.textContent=metaText(s);
  clearTimeout(vt);vt=setTimeout(()=>api().set_sound_volume(id,s.vol),50)});
 sl.addEventListener('dblclick',()=>{sl.value=100;sl.dispatchEvent(new Event('input'))});
 vr.appendChild(sl);vr.appendChild(pc);m.appendChild(vr);
 const lb=menuAdd(m,'loop',t.ctxLoop,async()=>apply(await api().set_loop(id,!s.loop)));
 lb.appendChild(el('i','mini'+(s.loop?' on':'')));
 const fb=menuAdd(m,'star',t.ctxFav,()=>toggleFav(id));
 fb.appendChild(el('i','mini'+(s.fav?' on':'')));
 const cr=el('div','crow');cr.appendChild(el('span','lbl',t.ctxColor));
 for(let k=0;k<NCOL;k++){const b=el('button','sw8'+(pig(s)===k?' on':''));b.style.setProperty('--ink','var(--c'+k+')');b.setAttribute('aria-label',t.ctxColor+' '+(k+1));
  b.onclick=async()=>{closeMenu();apply(await api().set_color(id,k))};cr.appendChild(b)}
 m.appendChild(cr);
 m.appendChild(el('div','sep'));
 menuAdd(m,'key',t.ctxBind,()=>startBind(id,'pad'));
 if(s.hotkey)menuAdd(m,'x',t.ctxUnbind,async()=>apply(await api().clear_hotkey(id)));
 menuAdd(m,'pen',t.ctxRename,()=>renameModal(id));
 menuAdd(m,'cut',t.ctxTrim,()=>{showView('trim',true);selectTrim(id)});
 const others=(ST.profiles||[]).filter(p=>p.id!==ST.active);
 if(others.length){
  m.appendChild(el('div','sep'));
  others.forEach(p=>menuAdd(m,'move',t.ctxMove+' '+p.name,async()=>{dropLive(id);apply(await api().move_to_profile(id,p.id))}));
 }
 m.appendChild(el('div','sep'));
 menuAdd(m,'trash',t.ctxDelete,()=>deleteModal(id),'red');
 menuShow(m,x,y,padEl);
}
function openProfileMenu(pid,x,y,host){
 const p=(ST.profiles||[]).find(q=>q.id===pid);if(!p)return;
 const t=T[lang],m=menuNew();
 menuAdd(m,'pen',t.pRename,()=>textModal({title:t.pRenameTitle,value:p.name,ph:t.profPh,onSave:async v=>apply(await api().rename_profile(pid,v))}));
 menuAdd(m,'key',t.pBind,()=>startBind(pid,'profile'));
 if(p.hotkey)menuAdd(m,'x',t.pUnbind,async()=>apply(await api().clear_profile_hotkey(pid)));
 m.appendChild(el('div','sep'));
 menuAdd(m,'folder',t.pFolder,async()=>{
  const r=await api().set_folder(pid);
  if(r&&r.sounds){apply(r);if(r.added)toast(t.folderAdded(r.added),true)}
 });
 if(p.folder){
  m.appendChild(el('div','ctxinfo',t.folderNow+' '+p.folder));
  menuAdd(m,'x',t.pFolderClear,async()=>apply(await api().clear_folder(pid)));
 }
 if(ST.profiles.length>1){
  m.appendChild(el('div','sep'));
  menuAdd(m,'trash',t.pDelete,()=>deleteProfileModal(pid),'red');
 }
 menuShow(m,x,y,host);
}

/* ---------- modal ---------- */
function closeModal(){modalCancel=null;$('#modal').classList.remove('on')}
function openModal({title,body,buttons,onCancel,wide}){
 closeMenu();
 $('#mbox').classList.toggle('wide',!!wide);
 $('#mTitle').textContent=title;
 const mb=$('#mBody');mb.textContent='';mb.appendChild(body);
 const bt=$('#mBtns');bt.textContent='';
 buttons.forEach(b=>{const x=el('button','btn '+(b.cls||''),b.label);x.onclick=b.onClick;bt.appendChild(x);if(b.focus)setTimeout(()=>x.focus(),40)});
 modalCancel=()=>{closeModal();if(onCancel)onCancel()};
 $('#modal').classList.add('on');
}
function helpModal(){
 const t=T[lang],d=el('div','how');
 ['how1','how2','how3','how4','how5','how6','how8','how7'].forEach(k=>d.appendChild(el('p','',t[k])));
 openModal({title:t.howTitle,body:d,wide:true,buttons:[{label:t.close,cls:'primary',onClick:closeModal,focus:true}]});
}
function textModal({title,note,value,ph,onSave}){
 const t=T[lang],wrap=document.createElement('div');
 const inp=el('input','minput');inp.value=value||'';inp.maxLength=60;inp.placeholder=ph;inp.setAttribute('aria-label',ph);
 wrap.appendChild(inp);
 if(note){const n=el('p','',note);n.style.cssText='margin-top:10px;font-size:12.5px';wrap.appendChild(n)}
 const go=async()=>{const v=inp.value;closeModal();await onSave(v)};
 inp.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();go()}});
 openModal({title,body:wrap,buttons:[{label:t.cancel,onClick:closeModal},{label:t.save,cls:'primary',onClick:go}]});
 setTimeout(()=>{inp.focus();inp.select()},60);
}
function renameModal(id){
 const s=byId(id);if(!s)return;const t=T[lang];
 textModal({title:t.renameTitle,note:t.renameNote,value:s.name,ph:t.renamePh,onSave:async v=>apply(await api().rename(id,v))});
}
function addProfileModal(){
 const t=T[lang];
 textModal({title:t.profAddTitle,note:t.profAddNote,value:'',ph:t.profPh,onSave:async v=>{[...live.keys()].forEach(dropLive);apply(await api().add_profile(v))}});
}
function deleteModal(id){
 const s=byId(id);if(!s)return;const t=T[lang];
 openModal({title:t.delTitle,body:el('p','',t.delText(s.name)),buttons:[
  {label:t.cancel,onClick:closeModal,focus:true},
  {label:t.delBtn,cls:'red',onClick:async()=>{closeModal();dropLive(id);apply(await api().remove(id))}}]});
}
function deleteProfileModal(pid){
 const p=(ST.profiles||[]).find(q=>q.id===pid);if(!p)return;const t=T[lang];
 openModal({title:t.pDelTitle,body:el('p','',t.pDelText(p.name,p.count)),buttons:[
  {label:t.cancel,onClick:closeModal,focus:true},
  {label:t.delBtn,cls:'red',onClick:async()=>{closeModal();[...live.keys()].forEach(dropLive);apply(await api().delete_profile(pid))}}]});
}
async function startBind(id,kind){
 if(listening)return;
 if(ST&&!ST.hotkeys){toast(T[lang].noKb);return}
 const t=T[lang];
 const body=el('div','keyhint');body.appendChild(el('div','cap'));body.appendChild(el('div','',t.bindText));body.appendChild(el('small','',t.bindHint));
 listening=id;
 openModal({title:kind==='profile'?t.profBindTitle:t.bindTitle,body,buttons:[{label:t.cancel,onClick:()=>{closeModal();api().cancel_bind()}}],onCancel:()=>api().cancel_bind()});
 let r;
 try{r=await (kind==='profile'?api().bind_profile(id):api().bind(id))}catch(e){r={ok:false}}
 listening=0;closeModal();
 if(r&&r.sounds)apply(r);else renderPads();
 if(r&&!r.ok)toast(r.error==='no_keyboard'?T[lang].noKb:T[lang].badKey);
}

/* ---------- nézetek + vágó ---------- */
function showView(v,keep){
 if(view==='trim'&&v!=='trim'&&TR.playing){TR.playing=false;api().preview_stop()}
 view=v;closeMenu();
 ['board','trim','settings'].forEach(k=>document.body.classList.toggle('v-'+k,k===v));
 $('#vBoard').classList.toggle('hide',v!=='board');
 $('#vTrim').classList.toggle('hide',v!=='trim');
 $('#vSettings').classList.toggle('hide',v!=='settings');
 document.querySelectorAll('#views .vbtn').forEach(b=>b.classList.toggle('on',b.dataset.view===v));
 if(v==='trim'){
  if(keep)renderTrimList();
  else if(TR.id&&byId(TR.id))renderTrim();
  else if(sounds.length)selectTrim(sounds[0].id);
  else{TR.id=null;renderTrim()}
 }else if(v==='board')cur=undefined;
}
async function selectTrim(id){
 const s=byId(id);if(!s)return;
 if(TR.playing){TR.playing=false;api().preview_stop()}
 const r=await api().get_wave(id);if(!r||!r.ok)return;
 TR={id,dur:r.dur,peaks:r.peaks,t0:s.t0||0,t1:s.t1>0?s.t1:r.dur,fi:s.fi||0,fo:s.fo||0,pv:null,playing:false,ps:0};
 renderTrim();
}
function trimAfterApply(){
 if(TR.id&&!byId(TR.id)){
  if(TR.playing){TR.playing=false;api().preview_stop()}
  TR.id=null;
  if(view==='trim'){if(sounds.length)selectTrim(sounds[0].id);else renderTrim()}
  return;
 }
 if(view==='trim'){renderTrimList();if(TE)syncTrim()}
}
function renderTrim(){renderTrimList();renderEditor()}
function renderTrimList(){
 const t=T[lang],box=$('#tlist');box.textContent='';
 const mh=el('div','mh');mh.appendChild(el('span','no','01'));mh.appendChild(el('h3','',t.trimList));box.appendChild(mh);
 const sc=el('div','tscroll');
 sounds.forEach(s=>{
  const b=el('button','tli'+(s.id===TR.id?' on':''));b.style.setProperty('--ink','var(--c'+pig(s)+')');
  b.appendChild(el('i'));b.appendChild(el('span','nm2',s.name));
  if(s.trimmed)b.appendChild(ico('cut'));
  b.appendChild(el('small','',fmtDur(s.dur)));
  b.onclick=()=>{if(s.id!==TR.id)selectTrim(s.id)};
  sc.appendChild(b);
 });
 box.appendChild(sc);
}
const parseTime=v=>{v=String(v).trim().replace(',','.');if(!v)return NaN;let sec=0;for(const x of v.split(':')){const n=parseFloat(x);if(!isFinite(n))return NaN;sec=sec*60+n}return sec};
const clampN=(v,a,b)=>Math.max(a,Math.min(b,v));
function trimDirty(){
 const s=byId(TR.id);if(!s)return false;
 const st1=s.t1>0?s.t1:TR.dur;
 return Math.abs(TR.t0-(s.t0||0))>.004||Math.abs(TR.t1-st1)>.004||TR.fi!==(s.fi||0)||TR.fo!==(s.fo||0);
}
function updatePrev(){
 if(!TE)return;const t=T[lang];
 TE.prev.textContent='';TE.prev.appendChild(ico(TR.playing?'stop':'play'));TE.prev.appendChild(el('span','',TR.playing?t.tPrevStop:t.tPrev));
}
function syncTrim(){
 if(!TE)return;
 const dirty=trimDirty();
 if(document.activeElement!==TE.a)TE.a.value=clock(TR.t0);
 if(document.activeElement!==TE.b)TE.b.value=clock(TR.t1);
 TE.len.textContent=clock(TR.t1-TR.t0);
 TE.fi.value=TR.fi;TE.fo.value=TR.fo;
 TE.fi.style.setProperty('--v',(TR.fi/30)+'%');TE.fo.style.setProperty('--v',(TR.fo/30)+'%');
 TE.fiv.textContent=TR.fi+' ms';TE.fov.textContent=TR.fo+' ms';
 TE.dirty.style.visibility=dirty?'visible':'hidden';
 TE.apply.disabled=!dirty;
}
function renderEditor(){
 const t=T[lang],box=$('#te');box.textContent='';TE=null;trc=null;trx=null;tdrag=null;
 if(!sounds.length){box.appendChild(el('div','temp',t.trimNone));return}
 const s=byId(TR.id);
 if(!s){box.appendChild(el('div','temp',t.trimPick));return}
 const h=el('div','teh'),tt=el('div');tt.appendChild(el('h2','',s.name));tt.appendChild(el('small','',t.tOrig+' '+clock(TR.dur)));
 h.appendChild(tt);const dirty=el('span','tdirty',t.tDirty);h.appendChild(dirty);box.appendChild(h);

 const wv=el('div','twave');trc=document.createElement('canvas');wv.appendChild(trc);trx=trc.getContext('2d');box.appendChild(wv);
 const hx=e=>{const r=trc.getBoundingClientRect();return{x:e.clientX-r.left,gw:r.width-24}};
 const tAt=(x,gw)=>clampN((x-12)/gw*TR.dur,0,TR.dur);
 const move=e=>{
  const {x,gw}=hx(e),v=tAt(x,gw);
  if(tdrag==='a')TR.t0=Math.min(v,TR.t1-.01);else TR.t1=Math.max(v,TR.t0+.01);
  TR.t0=Math.max(0,TR.t0);TR.t1=Math.min(TR.dur,TR.t1);syncTrim();
 };
 trc.addEventListener('pointerdown',e=>{
  if(e.button!==0)return;
  const {x,gw}=hx(e),xa=12+gw*TR.t0/TR.dur,xb=12+gw*TR.t1/TR.dur;
  tdrag=x>=xb?'b':x<=xa?'a':(Math.abs(x-xa)<=Math.abs(x-xb)?'a':'b');
  try{trc.setPointerCapture(e.pointerId)}catch(_){}
  move(e);
 });
 trc.addEventListener('pointermove',e=>{if(tdrag)move(e)});
 const up=()=>{tdrag=null};trc.addEventListener('pointerup',up);trc.addEventListener('pointercancel',up);

 const g=el('div','tgrid');box.appendChild(g);
 const field=label=>{const f=el('div','tf'),l=el('label');l.appendChild(el('span','',label));f.appendChild(l);g.appendChild(f);return{f,l}};
 const timeIn=(label,get,set)=>{
  const {f}=field(label),i=el('input','minput');i.type='text';i.spellcheck=false;f.appendChild(i);
  const commit=()=>{const v=parseTime(i.value);if(!isNaN(v))set(v);i.blur();syncTrim();i.value=clock(get())};
  i.addEventListener('change',commit);
  i.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();i.blur()}else if(e.key==='Escape'){e.stopPropagation();i.value=clock(get());i.blur()}});
  return i;
 };
 const a=timeIn(t.tStart,()=>TR.t0,v=>{TR.t0=clampN(v,0,TR.t1-.01)});
 const b=timeIn(t.tEnd,()=>TR.t1,v=>{TR.t1=clampN(v,TR.t0+.01,TR.dur)});
 const lf=field(t.tLen),len=el('div','ro');lf.f.appendChild(len);
 const fade=(label,key)=>{
  const {f,l}=field(label),v=el('b'),r=document.createElement('input');
  l.appendChild(v);r.type='range';r.min=0;r.max=3000;r.step=10;r.value=TR[key];r.setAttribute('aria-label',label);
  r.addEventListener('input',()=>{TR[key]=+r.value;syncTrim()});
  r.addEventListener('dblclick',()=>{TR[key]=0;syncTrim()});
  f.appendChild(r);return{r,v};
 };
 const fi=fade(t.tFadeIn,'fi'),fo=fade(t.tFadeOut,'fo');

 const bt=el('div','tbtns');box.appendChild(bt);
 const prev=el('button','btn');prev.onclick=previewToggle;bt.appendChild(prev);
 const reset=el('button','btn',t.tReset);reset.onclick=()=>{TR.t0=0;TR.t1=TR.dur;TR.fi=0;TR.fo=0;syncTrim()};bt.appendChild(reset);
 const apply2=el('button','btn primary',t.tApply);apply2.onclick=applyTrim;bt.appendChild(apply2);
 box.appendChild(el('p','tnote',t.tNote));
 TE={a,b,len,fi:fi.r,fo:fo.r,fiv:fi.v,fov:fo.v,prev,apply:apply2,dirty};
 updatePrev();syncTrim();
}
async function previewToggle(){
 if(TR.playing){TR.playing=false;updatePrev();api().preview_stop();return}
 let r;try{r=await api().preview(TR.id,TR.t0,TR.t1,TR.fi,TR.fo)}catch(e){r={ok:false}}
 if(!r||!r.ok){toast(r&&r.error==='no_monitor'?T[lang].noMon:T[lang].badKey);return}
 TR.playing=true;TR.ps=performance.now();updatePrev();
}
async function applyTrim(){
 const id=TR.id;if(!id)return;
 if(TR.playing){TR.playing=false;updatePrev();api().preview_stop()}
 dropLive(id);
 const st=await api().set_trim(id,TR.t0,TR.t1,TR.fi,TR.fo);
 masks.delete(id);apply(st);toast(T[lang].tSaved,true);
}
function drawTrim(){
 if(!trc||!TR.id)return;const s=byId(TR.id);if(!s)return;
 const d=fit(trc),W=trc.width,H=trc.height,x=trx;x.clearRect(0,0,W,H);
 const gx0=12*d,gw=W-24*d,top=24*d,bot=H-10*d,mid=(top+bot)/2,hh=(bot-top)/2*.94;
 const X=v=>gx0+gw*v/TR.dur,xa=X(TR.t0),xb=X(TR.t1);
 const step=3*d,bw=2*d,n=Math.max(8,Math.floor(gw/step)),pk=TR.peaks,L=pk.length,ink=COL.c[pig(s)];
 for(let i=0;i<n;i++){
  const a=Math.floor(i/n*L),b=Math.max(a+1,Math.floor((i+1)/n*L));let m=0;
  for(let j=a;j<b&&j<L;j++)if(pk[j]>m)m=pk[j];
  const h=Math.max(1*d,m*hh),bx=gx0+i*step,c=bx+bw/2;
  x.fillStyle=(c>=xa&&c<=xb)?ink:COL.wave;
  x.fillRect(bx,mid-h,bw,h*2);
 }
 x.globalAlpha=.55;x.fillStyle=COL.bg;
 x.fillRect(0,top-4*d,xa,H);x.fillRect(xb,top-4*d,W-xb,H);
 const fw=gw*(TR.fi/1000)/TR.dur,ow=gw*(TR.fo/1000)/TR.dur,y0=top-4*d,y1=bot+4*d;
 if(fw>0){x.beginPath();x.moveTo(xa,y0);x.lineTo(Math.min(xa+fw,xb),y0);x.lineTo(xa,y1);x.closePath();x.fill()}
 if(ow>0){x.beginPath();x.moveTo(xb,y0);x.lineTo(Math.max(xb-ow,xa),y0);x.lineTo(xb,y1);x.closePath();x.fill()}
 x.globalAlpha=1;
 const steps=[.05,.1,.25,.5,1,2,5,10,15,30,60,120,300,600],pps=gw/TR.dur;
 const st=steps.find(v=>pps*v>=80*d)||600;
 x.font=(10*d)+'px Consolas,monospace';x.textBaseline='top';
 for(let v=0;v<=TR.dur+1e-6;v+=st){
  const px=X(v);x.fillStyle=COL.line;x.fillRect(px,0,d,7*d);
  x.fillStyle=COL.muted;x.fillText(st<1?clock(v):clock(v).slice(0,5),px+4*d,3*d);
 }
 x.fillStyle=COL.accent;
 [xa,xb].forEach((hx,k)=>{
  x.fillRect(hx-d,top-8*d,2*d,bot-top+16*d);
  x.fillRect(k?hx:hx-10*d,mid-15*d,10*d,30*d);
 });
 if(TR.pv!=null&&TR.playing){
  const px=xa+(xb-xa)*TR.pv;x.fillStyle=COL.text;x.fillRect(px-d*.75,top-6*d,1.5*d,bot-top+12*d);
 }
}
const stopAll=()=>{[...live.keys()].forEach(dropLive);updatePorts();TR.playing=false;updatePrev();api().stop_all()};
function settings(){return{out:$('#out').value,mon:$('#mon').value,mic:$('#mic').value,use_mon:$('#use_mon').checked,use_mic:$('#use_mic').checked,stack:$('#stack').checked,tray_close:$('#tray_close').checked}}
async function send(){apply(await api().set_settings(settings()))}

function setLang(l,anim){lang=l;document.documentElement.lang=l;$('#lang').dataset.v=l;
 document.querySelectorAll('#lang button').forEach(b=>b.classList.toggle('on',b.dataset.l===l));
 if(anim){document.body.classList.add('swap');setTimeout(()=>{renderText();document.body.classList.remove('swap')},150)}else renderText()}
function setTheme(x){theme=x;document.documentElement.dataset.theme=x;readColors()}
const saveUi=()=>api().set_ui(lang,theme);

async function init(){
 const st=await api().get_state(),s=st.settings;
 fillSelect($('#out'),st.outs,s.out);fillSelect($('#mon'),st.outs,s.mon);fillSelect($('#mic'),st.ins,s.mic);
 $('#use_mon').checked=s.use_mon;$('#use_mic').checked=s.use_mic;$('#stack').checked=s.stack;$('#tray_close').checked=s.tray_close;
 $('#trayrow').classList.toggle('hide',!st.tray);
 const v=Math.round(s.volume*100);$('#vol').value=v;setVol(v);
 setTheme(s.theme);ST=st;sounds=st.sounds;setLang(s.lang);showView('board');
 document.querySelectorAll('#views .vbtn').forEach(b=>b.onclick=()=>showView(b.dataset.view));

 ['out','mon','mic','use_mon','use_mic','stack','tray_close'].forEach(id=>$('#'+id).addEventListener('change',send));
 $('#vol').addEventListener('input',()=>{const v=+$('#vol').value;setVol(v);clearTimeout(volT);volT=setTimeout(()=>api().set_volume(v/100),60)});
 $('#stop').onclick=stopAll;
 $('#chkUpd').onclick=checkUpdateNow;$('#verTxt').textContent=T[lang].verLabel(st.version);offerUpdate();
 $('#add').onclick=addDialog;
 $('#help').onclick=helpModal;
 $('#q').addEventListener('input',renderPads);
 $('#q').addEventListener('keydown',e=>{if(e.key==='Enter'){const l=visible();if(l.length){e.preventDefault();toggle(l[0].id)}}});
 document.querySelectorAll('#lang button').forEach(b=>b.onclick=()=>{setLang(b.dataset.l,true);saveUi()});
 $('#theme').onclick=()=>{setTheme(theme==='dark'?'light':'dark');saveUi()};

 addEventListener('contextmenu',e=>{if(e.target.tagName!=='INPUT')e.preventDefault();closeMenu()});
 addEventListener('mousedown',e=>{if(!e.target.closest('#ctx'))closeMenu()});
 addEventListener('blur',closeMenu);addEventListener('resize',closeMenu);addEventListener('scroll',closeMenu,true);
 $('#modal').addEventListener('mousedown',e=>{if(e.target.id==='modal'&&modalCancel)modalCancel()});

 addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&['f','k'].includes(e.key.toLowerCase())){e.preventDefault();if(!modalCancel){showView('board');$('#q').focus();$('#q').select()}return}
  if(e.key==='Escape'){
   if($('#ctx').classList.contains('on')){closeMenu();return}
   if(modalCancel){modalCancel();return}
   if(e.target.id==='q'&&$('#q').value){$('#q').value='';renderPads();return}
   stopAll();return}
  if(view==='board'&&/^[1-9]$/.test(e.key)&&!e.ctrlKey&&!e.altKey&&!e.metaKey&&!modalCancel&&!listening&&!['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)){
   const l=visible(),sd=l[+e.key-1];if(sd)toggle(sd.id)}
 });

 addEventListener('dragenter',e=>{if(e.dataTransfer&&[...e.dataTransfer.types].includes('Files')){drag++;document.body.classList.add('dragging')}});
 addEventListener('dragleave',()=>{if(--drag<=0){drag=0;document.body.classList.remove('dragging')}});
 addEventListener('dragover',e=>e.preventDefault());
 addEventListener('drop',e=>e.preventDefault());

 setInterval(poll,60);
 requestAnimationFrame(frame);
}
window.addEventListener('pywebviewready',init);
})();
</script></body></html>
"""


def main() -> None:
    api = Api()
    window = webview.create_window(
        "Pulsepad",
        html=HTML,
        js_api=api,
        width=1320,
        height=860,
        min_size=(980, 660),
        background_color="#0c0c0a" if api._s["theme"] == "dark" else "#e8e4d6",
    )
    api._window = window
    api._start_tray()
    api._start_watch()
    api._start_update_check()

    def bind(win) -> None:
        """Fájlok behúzása az ablakba (a pywebview adja meg a teljes elérési utat)."""
        try:
            from webview.dom import DOMEventHandler

            def on_drop(e) -> None:
                files = (e.get("dataTransfer") or {}).get("files") or []
                paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
                errors = api._load_many(paths) if paths else []
                win.evaluate_js(f"window.refresh && window.refresh({json.dumps(errors)})")

            win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            pass  # régebbi pywebview: a „Hangok hozzáadása” gomb ettől még működik

    window.events.closing += api._on_closing
    window.events.closed += lambda: api._shutdown()
    webview.start(bind, window, debug=False)


if __name__ == "__main__":
    main()