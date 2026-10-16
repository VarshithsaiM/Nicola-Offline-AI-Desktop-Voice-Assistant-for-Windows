"""
Nicola - an offline-first voice assistant for Windows (tuned audio & wake-word edition).
"""
from __future__ import annotations

import os
import sys

# Must be set before anything that can load an OpenMP runtime (numpy / torch / ctranslate2).
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# pythonw.exe has no console: stdout/stderr are None and print() would raise.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import ctypes
import difflib
import json
import logging
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional

import numpy as np
import psutil
import pystray
import pythoncom
import requests
import sounddevice as sd
import win32clipboard
import win32com.client
import win32con
from PIL import Image, ImageDraw, ImageGrab
from pystray import MenuItem

try:
    import screen_brightness_control as sbc
except ImportError:
    sbc = None


# =============================================================================
# CONFIG  (every value can be overridden with an environment variable)
# =============================================================================
def _env(name, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


if getattr(sys, "frozen", False):          # PyInstaller: keep data next to the .exe
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

MEMORY_FILE = os.path.join(APP_DIR, "nicola_memory.json")
NOTES_FILE = os.path.join(APP_DIR, "nicola_notes.txt")
ALIASES_FILE = os.path.join(APP_DIR, "nicola_aliases.json")
REMINDERS_FILE = os.path.join(APP_DIR, "nicola_reminders.json")
LOG_FILE = os.path.join(APP_DIR, "nicola.log")

LM_STUDIO_BASE = _env("NICOLA_LM_BASE", "http://localhost:1234")
LM_STUDIO_URL = LM_STUDIO_BASE + "/v1/chat/completions"
MODEL_ID = _env("NICOLA_MODEL_ID", "google/gemma-3-4b")
WHISPER_MODEL = _env("NICOLA_WHISPER_MODEL", "base.en")

# Full natural sentence primes Whisper for "Nicola" and command words without noise hallucination.
WHISPER_INITIAL_PROMPT = _env(
    "NICOLA_WHISPER_PROMPT",
    "Hey Nicola, open Chrome, close window, show desktop, volume up, volume down, set a timer, remind me."
)
TTS_RATE = _env("NICOLA_TTS_RATE", 3, int)
TTS_TAIL_S = 0.4                 # keep the mic muted briefly after speech ends (room echo)

SESSION_TIMEOUT = 25             # seconds of silence before returning to standby
CONFIRM_TIMEOUT = 15             # seconds the user has to answer a yes/no question
MAX_MEMORY_FACTS = 15
STALE_REMINDER_S = 3600          # reminders missed by more than this are dropped on restart

WAKE_WORDS = [
    "nicola", "nikola", "nicole", "nicolas", "nicholas", "nicol", "micola", "nukola",
    "necola", "nikla", "nickla", "nick ola", "knee cola", "echo la", "in cola",
    "the cola", "hey cola", "hi cola", "ni cola", "nick a la", "nickel a"
]

STANDBY_RE = re.compile(r"\b(?:goodbye|stop listening|standby mode|go to sleep)\b", re.I)

SYSTEM_PROMPT = (
    "You are Nicola, a snappy desktop voice assistant. Keep answers brief: one to three "
    "conversational sentences. Never use asterisks, lists or markdown. "
    "If the user is casually asking to open or close an app, set a timer or reminder, adjust "
    "volume or brightness, take a screenshot, or lock/sleep the computer, output ONLY "
    "[CMD: <canonical command>] (for example: [CMD: open calculator] or [CMD: close chrome] "
    "or [CMD: timer for 5 minutes]). For all normal questions or conversation, answer directly."
)

# --- audio (Tuned to ignore background noise, clicks, and breaths) ---
SAMPLE_RATE = 16000
BLOCK_SIZE = 1600                                   # 100 ms
BLOCKS_PER_SEC = SAMPLE_RATE / BLOCK_SIZE
MIN_THRESH = _env("NICOLA_MIN_THRESH", 0.018, float)  # Raised from 0.006 so room noise won't trip it
MAX_THRESH = _env("NICOLA_MAX_THRESH", 0.065, float)
FLOOR_MULT = 3.5                                    # Voice must be 3.5x louder than ambient noise floor
PREROLL_BLOCKS = 4                                  # 400ms pre-roll so the "N" in Nicola is never clipped
WAIT_FOR_SPEECH_S = 2.0
STANDBY_SILENCE_S, STANDBY_MAX_S = 0.7, 3.5
AWAKE_SILENCE_S, AWAKE_MAX_S = 1.5, 25.0
MIN_SPEECH_S = 0.4                                  # Require 400ms of real speech (ignores clicks/coughs)

CLOSE_GRACE_S = 3.0

# Things Whisper invents on near-silence.
HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you", "bye",
    "so", "the end", "okay", "oh", "uh", "um", "hmm", "ah"
}


# =============================================================================
# LOGGING / SMALL HELPERS
# =============================================================================
log = logging.getLogger("nicola")


def setup_logging():
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    handler = RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    log.addHandler(handler)


def safe_print(*args):
    """print() that can never raise (legacy consoles choke on some characters)."""
    try:
        print(*args)
    except Exception:
        pass


shutdown = threading.Event()
_io_lock = threading.RLock()


@dataclass
class Confirm:
    """Returned by an intent handler that needs a spoken yes/no before acting."""
    prompt: str
    action: Callable[[], str]


class AppState:
    """Thread-safe state shared by the assistant thread, the TTS thread and the overlay."""

    def __init__(self):
        self._lock = threading.Lock()
        self.phase = "idle"              # idle | listening | thinking
        self.speaking = False
        self.user_text = ""
        self.nicola_text = ""
        self.last_alert = None           # {"task": str, "time": float}

    def update(self, **fields):
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def snapshot(self):
        with self._lock:
            return self.phase, self.speaking, self.user_text, self.nicola_text

    def get_last_alert(self):
        with self._lock:
            return self.last_alert


state = AppState()


# =============================================================================
# WINDOWS API  (typed so 64-bit handles are not truncated)
# =============================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
powrprof = ctypes.WinDLL("powrprof", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None
user32.LockWorkStation.restype = wintypes.BOOL
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT

shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
powrprof.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN]
powrprof.SetSuspendState.restype = wintypes.BOOLEAN

WM_CLOSE = 0x0010
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
VK_ESCAPE = 0x1B
VK_MEDIA_PLAY_PAUSE, VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK = 0xB3, 0xB0, 0xB1
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_LWIN, VK_MENU, VK_F4, VK_D = 0x5B, 0x12, 0x73, 0x44
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# =============================================================================
# TEXT TO SPEECH  (one worker thread owns the COM voice object; Esc interrupts)
# =============================================================================
class TTS:
    def __init__(self, app_state: AppState):
        self._state = app_state
        self._q: "queue.Queue" = queue.Queue()
        self._cv = threading.Condition()
        self._pending = 0
        self._mute_until = 0.0
        self._abort_flag = threading.Event()
        self._started = False
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)

    def start(self):
        if not self._started:
            self._started = True
            self._thread.start()

    def stop(self):
        self._abort_flag.set()
        self._q.put(None)

    def interrupt(self):
        """Immediately silence active speech and flush queued sentences."""
        self._abort_flag.set()

    def is_busy(self) -> bool:
        """True while speaking, queued to speak, or in the short echo tail afterwards."""
        return self._pending > 0 or time.time() < self._mute_until

    def say(self, text, *, block=True, display=None, timeout=None):
        if not text or not str(text).strip():
            return
        clean = re.sub(r"[*#_`]", "", str(text)).strip()
        if not self._started:
            safe_print(f"\n[Nicola - TTS not running]: {clean}")
            return
        with self._cv:
            self._pending += 1
        self._q.put((clean, clean if display is None else display))
        if block:
            self.wait_idle(timeout)

    def wait_idle(self, timeout=None) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0, timeout)

    def _run(self):
        com_ready = False
        try:
            pythoncom.CoInitialize()
            com_ready = True
        except Exception:
            log.exception("CoInitialize failed")
        voice = None
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = TTS_RATE
        except Exception:
            log.exception("Could not create the SAPI voice")

        SVSFlagsAsync = 1
        SVSFPurgeBeforeSpeak = 2

        while True:
            item = self._q.get()
            if item is None:
                break
            text, display = item
            self._abort_flag.clear()
            self._state.update(speaking=True, nicola_text=display)
            safe_print(f"\n[Nicola]: {text}")
            try:
                if voice is not None:
                    voice.Speak(text, SVSFlagsAsync)
                    while not voice.WaitUntilDone(50):
                        esc_pressed = bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
                        if shutdown.is_set() or self._abort_flag.is_set() or esc_pressed:
                            voice.Speak("", SVSFlagsAsync | SVSFPurgeBeforeSpeak)
                            with self._cv:
                                while True:
                                    try:
                                        dropped = self._q.get_nowait()
                                        if dropped is None:
                                            self._q.put(None)
                                            break
                                        self._pending = max(1, self._pending - 1)
                                    except queue.Empty:
                                        break
                            break
            except Exception:
                log.exception("TTS speak failed")
            finally:
                self._mute_until = time.time() + TTS_TAIL_S
                with self._cv:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._state.update(speaking=False)
                    self._cv.notify_all()
        if com_ready:
            pythoncom.CoUninitialize()


tts = TTS(state)


def speak(text, block=True, display=None):
    tts.say(text, block=block, display=display)


# =============================================================================
# TEXT & FUZZY WAKE-WORD UTILITIES
# =============================================================================
def normalize(text: str) -> str:
    """Collapse whitespace and strip the punctuation / politeness Whisper adds."""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = t.strip(" ,.!?;:")
    t = re.sub(r"^(?:hey|hi|hello|okay|ok|yo)\s+", "", t, flags=re.I).strip(" ,.!?;:")
    t = re.sub(r"[\s,]*\bplease$", "", t, flags=re.I).strip(" ,.!?;:")
    return t


_WAKE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(WAKE_WORDS, key=len, reverse=True)) + r")\b",
    re.I,
)


def _fuzzy_wake_span(text: str) -> Optional[tuple[int, int]]:
    """
    Find the character span (start, end) of a word or 2-word pair that sounds like 'nicola'
    even if Whisper slightly misspelled it (e.g. 'nickle a', 'necola', 'ni cola').
    """
    m = _WAKE_RE.search(text)
    if m:
        return m.span()

    tokens = list(re.finditer(r"[a-zA-Z]+", text))
    # Only check the first 4 words of an utterance for fuzzy wake words
    limit = min(len(tokens), 4)
    for i in range(limit):
        w1 = tokens[i].group(0).lower()
        if len(w1) >= 5:
            score = max(
                difflib.SequenceMatcher(None, w1, "nicola").ratio(),
                difflib.SequenceMatcher(None, w1, "nikola").ratio(),
                difflib.SequenceMatcher(None, w1, "nicole").ratio(),
            )
            if score >= 0.75:
                return tokens[i].start(), tokens[i].end()
        if i + 1 < limit:
            combined = w1 + tokens[i + 1].group(0).lower()
            if 5 <= len(combined) <= 9:
                score = max(
                    difflib.SequenceMatcher(None, combined, "nicola").ratio(),
                    difflib.SequenceMatcher(None, combined, "nikola").ratio(),
                )
                if score >= 0.75:
                    return tokens[i].start(), tokens[i + 1].end()
    return None


def contains_wake_word(text: str) -> bool:
    return _fuzzy_wake_span(text) is not None


def strip_wake_word(text: str) -> str:
    cleaned = _WAKE_RE.sub(" ", text)
    span = _fuzzy_wake_span(cleaned)
    if span:
        cleaned = cleaned[:span[0]] + " " + cleaned[span[1]:]
    return normalize(cleaned)


_YES_RE = re.compile(r"^(?:yes|yeah|yep|yup|sure|confirm|do it|go ahead|proceed|affirmative)\b", re.I)
_NO_RE = re.compile(r"^(?:no|nope|nah|cancel|stop|never ?mind|don['’]?t)\b", re.I)


def classify_yes_no(text: str) -> Optional[bool]:
    t = normalize(text)
    if _YES_RE.search(t):
        return True
    if _NO_RE.search(t):
        return False
    return None


# --- spoken durations: "5 minutes", "five minutes", "an hour and a half", "half an hour" ---
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
         "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
         "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}


def _alt(words):
    return "|".join(sorted(words, key=len, reverse=True))


_NUM_WORD = (rf"(?:(?:{_alt(_TENS)})(?:[ -](?:{_alt(w for w, v in _ONES.items() if 1 <= v <= 9)}))?"
             rf"|{_alt(_ONES)})")
_NUM = rf"(?:\d+(?:\.\d+)?|{_NUM_WORD}|an?)"
_DUR_PART = re.compile(rf"\b({_NUM})\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", re.I)


def _num_value(token: str) -> float:
    token = token.lower().replace("-", " ").strip()
    if token in ("a", "an"):
        return 1.0
    try:
        return float(token)
    except ValueError:
        pass
    return float(sum(_TENS.get(w, _ONES.get(w, 0)) for w in token.split()))


def parse_duration(text: str) -> Optional[int]:
    """Return seconds, or None if no duration is found."""
    s = text.lower()
    s = re.sub(r"\bhalf an hour\b|\bhalf hour\b|\ba half hour\b", "30 minutes", s)
    s = re.sub(r"\ban hour and a half\b", "90 minutes", s)
    s = re.sub(rf"\b({_NUM}) and a half (hours?|minutes?)\b",
               lambda m: f"{_num_value(m.group(1)) + 0.5} {m.group(2)}", s)
    total = 0.0
    for num, unit in _DUR_PART.findall(s):
        mult = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        total += _num_value(num) * mult
    return int(round(total)) if total > 0 else None


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs or not parts:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " and ".join(parts)


# --- sentence splitting for streamed LLM output & long clipboards ---
_SENT_END = re.compile(r"([.!?]+[\"')\]]*)(\s+)")
_ABBREV = {"dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "e.g", "i.e", "no", "sr", "jr"}


def split_sentences(buf: str, min_len: int = 20):
    """Split off complete sentences; return (sentences, unfinished_remainder)."""
    out, start = [], 0
    for m in _SENT_END.finditer(buf):
        chunk = buf[start:m.end(1)].strip()
        words = chunk.rstrip(".!?\"')]").split()
        if words and words[-1].lower() in _ABBREV:
            continue
        if len(chunk) < min_len:
            continue
        out.append(chunk)
        start = m.end()
    return out, buf[start:]


# =============================================================================
# PERSISTENCE  (atomic writes, UTF-8, corrupt files are set aside not overwritten)
# =============================================================================
def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        log.exception("Could not read %s; keeping a copy as .corrupt", path)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return default


def load_memory() -> list:
    with _io_lock:
        data = _read_json(MEMORY_FILE, [])
    return [str(x) for x in data] if isinstance(data, list) else []


def save_memory_fact(fact: str):
    with _io_lock:
        facts = load_memory()
        facts.append(fact.strip())
        _atomic_write_json(MEMORY_FILE, facts[-MAX_MEMORY_FACTS:])


def clear_memory() -> str:
    with _io_lock:
        try:
            os.remove(MEMORY_FILE)
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("Could not delete memory file")
            return "I could not wipe my memory."
    return "My memory has been wiped clean."


def save_note(note: str):
    with _io_lock, open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {note.strip()}\n")


def read_notes() -> str:
    if not os.path.exists(NOTES_FILE):
        return "You do not have any saved notes yet."
    try:
        with _io_lock, open(NOTES_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if not lines:
            return "Your notes file is empty."
        recent = [re.sub(r"^\[.*?\]\s*", "", ln) for ln in lines[-3:]]
        return "Your latest notes are: " + ". ".join(recent) + "."
    except Exception:
        log.exception("Could not read notes file")
        return "I had trouble reading your notes."


def load_custom_aliases() -> dict:
    with _io_lock:
        data = _read_json(ALIASES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_custom_alias(trigger: str, target: str):
    with _io_lock:
        aliases = load_custom_aliases()
        aliases[normalize(trigger).lower()] = target.strip()
        _atomic_write_json(ALIASES_FILE, aliases)


# =============================================================================
# REMINDERS / TIMERS  (daemon timers, persisted & cancellable)
# =============================================================================
_active_timers: dict[str, threading.Timer] = {}


def _load_reminders() -> list:
    data = _read_json(REMINDERS_FILE, [])
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and {"id", "task", "due"} <= r.keys()]


def _start_timer(rid: str, task: str, delay: float):
    timer = threading.Timer(max(0.0, delay), fire_alert, args=(rid, task))
    timer.daemon = True            # a pending reminder must not keep the process alive after quit
    timer.name = "reminder"
    with _io_lock:
        _active_timers[rid] = timer
    timer.start()


def schedule_alert(task: str, seconds: float):
    due = time.time() + seconds
    rid = f"{int(due * 1000)}-{random.randint(0, 9999)}"
    with _io_lock:
        items = _load_reminders()
        items.append({"id": rid, "task": task, "due": due})
        _atomic_write_json(REMINDERS_FILE, items)
    _start_timer(rid, task, seconds)


def fire_alert(rid: str, task: str):
    try:
        with _io_lock:
            _active_timers.pop(rid, None)
            _atomic_write_json(REMINDERS_FILE, [r for r in _load_reminders() if r["id"] != rid])
        state.update(last_alert={"task": task, "time": time.time()})
        tts.say(f"Alert! {task}")
    except Exception:
        log.exception("fire_alert failed")


def cancel_all_timers() -> str:
    with _io_lock:
        count = len(_active_timers)
        for timer in _active_timers.values():
            timer.cancel()
        _active_timers.clear()
        _atomic_write_json(REMINDERS_FILE, [])
    if count == 0:
        return "You have no active timers or reminders to cancel."
    return f"Cancelled {count} active {'timer' if count == 1 else 'timers'}."


def list_active_reminders() -> str:
    with _io_lock:
        items = sorted(_load_reminders(), key=lambda x: x["due"])
    if not items:
        return "You have no active timers or reminders."
    now = time.time()
    parts = [f"{r['task']} in {format_duration(max(1, int(r['due'] - now)))}" for r in items[:3]]
    return "Upcoming: " + "; ".join(parts) + "."


def restore_reminders():
    now = time.time()
    with _io_lock:
        items = _load_reminders()
        keep = [r for r in items if r["due"] - now > -STALE_REMINDER_S]
        if len(keep) != len(items):
            _atomic_write_json(REMINDERS_FILE, keep)
    for r in keep:
        delay = r["due"] - now
        if delay < 0:                                  # missed while Nicola was off
            _start_timer(r["id"], f"Missed reminder. {r['task']}", 8)
        else:
            _start_timer(r["id"], r["task"], delay)


def snooze_last_alert() -> str:
    last = state.get_last_alert()
    if last and time.time() - last["time"] < 300:
        schedule_alert(last["task"], 300)
        return "Snoozed for five minutes."
    return "There is no recent alert to snooze."


# =============================================================================
# LOW-LEVEL WINDOW & KEYBOARD HELPERS
# =============================================================================
def _key_event(vk, extended=False, up=False):
    flags = (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)
    user32.keybd_event(vk, 0, flags, 0)


def _tap_media_key(vk):
    _key_event(vk, extended=True)
    _key_event(vk, extended=True, up=True)


def _windows_snapshot():
    """Visible top-level windows as (hwnd, title, class_name, pid)."""
    found = []

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((hwnd, title, cls.value, pid.value))
        except Exception:
            log.exception("window enumeration callback failed")
        return True

    callback = WNDENUMPROC(_cb)        # keep a reference alive during the call
    user32.EnumWindows(callback, 0)
    return found


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


# =============================================================================
# SIMPLE SYSTEM CONTROLS
# =============================================================================
def control_media(action: str) -> str:
    keys = {"play_pause": (VK_MEDIA_PLAY_PAUSE, "Toggled playback."),
            "next": (VK_MEDIA_NEXT_TRACK, "Skipping track."),
            "previous": (VK_MEDIA_PREV_TRACK, "Previous track."),
            "mute": (VK_VOLUME_MUTE, "Toggled mute.")}
    vk, message = keys[action]
    _tap_media_key(vk)
    return message


def control_volume(direction: str) -> str:
    vk = VK_VOLUME_UP if direction == "up" else VK_VOLUME_DOWN
    for _ in range(3):
        _tap_media_key(vk)
        time.sleep(0.04)
    return f"Volume {direction}."


def set_exact_volume(level: int) -> str:
    target = max(0, min(100, int(level)))
    for _ in range(50):
        _tap_media_key(VK_VOLUME_DOWN)
    for _ in range(round(target / 2)):
        _tap_media_key(VK_VOLUME_UP)
    return f"Volume set to {target} percent."


def window_control(action: str) -> str:
    if action == "show_desktop":
        _key_event(VK_LWIN); _key_event(VK_D)
        _key_event(VK_D, up=True); _key_event(VK_LWIN, up=True)
        return "Showing the desktop."
    if action == "close_window":
        _key_event(VK_MENU); _key_event(VK_F4)
        _key_event(VK_F4, up=True); _key_event(VK_MENU, up=True)
        return "Closing the current window."
    return "I do not know that window command."


def power_control(action: str) -> str:
    if action == "lock":
        user32.LockWorkStation()
        return "Workstation locked."
    if action == "sleep":
        def _suspend():
            time.sleep(1.5)                  # let the spoken reply get queued first
            tts.wait_idle(timeout=8)
            powrprof.SetSuspendState(False, True, False)
        threading.Thread(target=_suspend, daemon=True, name="suspend").start()
        return "Putting the computer to sleep. Goodnight."
    return "I do not know that power command."


def empty_recycle_bin() -> str:
    # 7 = no confirmation dialog, no progress UI, no sound (we already confirmed by voice).
    result = shell32.SHEmptyRecycleBinW(None, None, 7)
    return "Recycle bin emptied." if result == 0 else "The recycle bin is already empty, or I could not empty it."


def adjust_brightness(command: str) -> str:
    if sbc is None:
        return "The screen brightness library is not installed."
    try:
        match = re.search(r"\b(\d{1,3})\b", command)
        if match:
            level = max(0, min(100, int(match.group(1))))
            sbc.set_brightness(level)
            return f"Brightness set to {level} percent."
        lowered = command.lower()
        if re.search(r"\b(?:up|increase|raise|brighter)\b", lowered):
            delta = 20
        elif re.search(r"\b(?:down|decrease|lower|dim|dimmer)\b", lowered):
            delta = -20
        else:
            return "Tell me a brightness level, or say up or down."
        current = sbc.get_brightness(display=0)
        current = current[0] if isinstance(current, list) else current
        level = max(10, min(100, current + delta))
        sbc.set_brightness(level)
        return f"Brightness {'increased' if delta > 0 else 'decreased'} to {level} percent."
    except Exception:
        log.exception("adjust_brightness failed")
        return "I could not adjust the brightness on this display."


def take_screenshot() -> str:
    try:
        pictures = os.path.join(os.path.expanduser("~"), "Pictures")
        os.makedirs(pictures, exist_ok=True)
        path = os.path.join(pictures, f"Nicola_Screenshot_{datetime.now():%Y%m%d_%H%M%S}.png")
        ImageGrab.grab(all_screens=True).save(path)
        return "Screenshot saved to your Pictures folder."
    except Exception:
        log.exception("screenshot failed")
        return "I could not take the screenshot."


def get_system_vitals() -> str:
    try:
        ram = psutil.virtual_memory().percent
        battery = psutil.sensors_battery()
        if battery:
            plugged = "plugged in" if battery.power_plugged else "on battery"
            return f"RAM usage is {ram} percent. Battery is at {int(battery.percent)} percent and {plugged}."
        return f"RAM usage is {ram} percent. No battery detected."
    except Exception:
        log.exception("vitals failed")
        return "I could not read the system vitals."


def read_clipboard() -> str:
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                clip = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            else:
                clip = ""
        finally:
            win32clipboard.CloseClipboard()
        clip = re.sub(r"[\*#_`<>{}|]", "", (clip or "")).strip()
        clip = re.sub(r"\s+", " ", clip)
        if not clip:
            return "Your clipboard is empty or does not contain text."

        capped = clip[:2500]
        sentences, tail = split_sentences(capped, min_len=10)
        tts.say("Your clipboard says:", block=False)
        for s in sentences:
            tts.say(s, block=False)
        if tail.strip():
            tts.say(tail.strip(), block=False)
        if len(clip) > 2500:
            tts.say("End of clipboard preview.", block=False)
        tts.wait_idle()
        return ""
    except Exception:
        log.exception("clipboard read failed")
        return "I could not read the clipboard."


# =============================================================================
# WEB HELPERS
# =============================================================================
def perform_web_search(query: str) -> str:
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
    return f"Searching for {query}."


def get_weather(location: str = "") -> str:
    try:
        loc = urllib.parse.quote(location.strip())
        res = requests.get(f"https://wttr.in/{loc}?format=%C+and+%t", timeout=4)
        text = res.text.strip()
        if res.ok and text and "<html" not in text.lower() and "unknown" not in text.lower():
            where = location.strip().title() or "your area"
            return f"Currently in {where}, it is {text}."
        return "I could not find the weather for that location."
    except requests.exceptions.RequestException:
        return "I could not reach the weather service. Are you online?"
    except Exception:
        log.exception("weather failed")
        return "I could not get the weather."


def get_definition(word: str) -> str:
    word = word.strip()
    try:
        res = requests.get("https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(word), timeout=4)
        if res.status_code == 200:
            data = res.json()[0]["meanings"][0]
            return f"{word}, {data['partOfSpeech']}: {data['definitions'][0]['definition']}"
        return f"I could not find a definition for {word}."
    except requests.exceptions.RequestException:
        return "I could not reach the dictionary service. Are you online?"
    except Exception:
        log.exception("definition failed")
        return "I could not look that word up."


# =============================================================================
# APP DISCOVERY / LAUNCHING
# =============================================================================
cached_apps: dict = {}          # lowercase name -> AppID (Start Menu / UWP)
cached_shortcuts: dict = {}     # lowercase name -> .lnk path
_cache_ready = threading.Event()

# kind: "exe" = resolve on PATH / App Paths, "uri" = os.startfile, "shell" = explorer shell: path
_STATIC_APPS = {
    "calculator": ("exe", "calc.exe"), "calc": ("exe", "calc.exe"),
    "notepad": ("exe", "notepad.exe"),
    "command prompt": ("exe", "cmd.exe"), "cmd": ("exe", "cmd.exe"),
    "terminal": ("exe", "wt.exe"), "windows terminal": ("exe", "wt.exe"),
    "task manager": ("exe", "taskmgr.exe"),
    "explorer": ("exe", "explorer.exe"), "file explorer": ("exe", "explorer.exe"),
    "chrome": ("exe", "chrome.exe"), "google chrome": ("exe", "chrome.exe"),
    "edge": ("exe", "msedge.exe"), "microsoft edge": ("exe", "msedge.exe"),
    "firefox": ("exe", "firefox.exe"),
    "vs code": ("exe", "code"), "vscode": ("exe", "code"), "visual studio code": ("exe", "code"),
    "code": ("exe", "code"),
    "matlab": ("exe", "matlab.exe"),
    "vlc": ("exe", "vlc.exe"),
    "steam": ("exe", "steam.exe"),
    "settings": ("uri", "ms-settings:"),
    "whatsapp": ("shell", r"shell:appsFolder\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"),
}


def _clean_app_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"^(?:the|my)\s+", "", name)
    name = re.sub(r"\s+(?:app|application|program)$", "", name)
    return name.strip()


def _find_executable(name: str) -> Optional[str]:
    """Locate an executable on PATH or via the registry's App Paths (what the Start button uses)."""
    path = shutil.which(name)
    if path:
        return path
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, None)
            value = os.path.expandvars(str(value).strip('"'))
            if os.path.exists(value):
                return value
        except OSError:
            continue
    return None


def _launch(kind: str, value: str) -> bool:
    try:
        if kind == "uri":
            os.startfile(value)
            return True
        if kind == "shell":
            subprocess.Popen(["explorer.exe", value])
            return True
        exe = _find_executable(value)
        if exe:
            subprocess.Popen([exe])
            return True
    except Exception:
        log.exception("launch failed for %s", value)
    return False


def _best_match(target: str, names) -> Optional[str]:
    """Rank: exact > startswith > whole word > substring (> fuzzy). Short targets must match exactly."""
    t = target.lower().strip()
    names = list(names)
    if not t:
        return None
    best, best_score = None, 0.0
    for name in names:
        n = name.lower()
        if "uninstall" in n and "uninstall" not in t:
            continue
        if n == t:
            score = 100.0
        elif len(t) < 3:
            continue
        elif n.startswith(t):
            score = 80.0
        elif re.search(rf"\b{re.escape(t)}\b", n):
            score = 60.0
        elif t in n:
            score = 40.0
        else:
            continue
        score -= min(len(n) - len(t), 40) * 0.1          # prefer the shorter, closer name
        if score > best_score:
            best, best_score = name, score
    if best is None and len(t) >= 4:                      # speech-recognition near misses
        lowered = {n.lower(): n for n in names}
        close = difflib.get_close_matches(t, list(lowered), n=1, cutoff=0.8)
        if close:
            best = lowered[close[0]]
    return best


def populate_app_cache():
    """Build the Start Menu app + shortcut caches (runs in a background thread)."""
    global cached_apps, cached_shortcuts
    try:
        ps_cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }")
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                             capture_output=True, text=True, encoding="utf-8", errors="ignore",
                             creationflags=NO_WINDOW, timeout=30)
        apps = {}
        for line in res.stdout.splitlines():
            if "|" in line:
                name, appid = line.rsplit("|", 1)
                apps[name.strip().lower()] = appid.strip()
        cached_apps = apps
    except Exception:
        log.exception("Get-StartApps failed")
    try:
        shortcuts = {}
        roots = [os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
                 os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
                 os.path.join(os.path.expanduser("~"), "Desktop")]
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for folder, _dirs, files in os.walk(root):
                for fname in files:
                    if fname.lower().endswith(".lnk"):
                        shortcuts.setdefault(fname[:-4].lower(), os.path.join(folder, fname))
        cached_shortcuts = shortcuts
    except Exception:
        log.exception("shortcut scan failed")
    finally:
        _cache_ready.set()
        log.info("App cache ready: %d apps, %d shortcuts", len(cached_apps), len(cached_shortcuts))


def launch_dynamic_application(app_request: str) -> str:
    try:
        target = _clean_app_name(app_request)
        if not target:
            return "Which application should I open?"
        if target in _STATIC_APPS:
            kind, value = _STATIC_APPS[target]
            if _launch(kind, value):
                return f"Opening {target}."
        _cache_ready.wait(timeout=10)
        name = _best_match(target, cached_apps)
        if name:
            appid = cached_apps[name]
            if "\\" in appid and not appid.lower().endswith(".exe") and target in cached_shortcuts:
                os.startfile(cached_shortcuts[target])
                return f"Opening {target.title()}."
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{appid}"])
            return f"Opening {name.title()}."
        name = _best_match(target, cached_shortcuts)
        if name:
            os.startfile(cached_shortcuts[name])
            return f"Opening {name.title()}."
        if re.fullmatch(r"[\w .+-]{1,60}", target):
            exe = _find_executable(target)
            if exe:
                subprocess.Popen([exe])
                return f"Opening {target}."
        return f"I could not find an app called {target}."
    except Exception:
        log.exception("launch_dynamic_application failed")
        return "I had trouble launching that application."


# =============================================================================
# CLOSING APPS  (exact match, graceful first, force only when told to)
# =============================================================================
_CLOSE_ALIASES = {
    "chrome": ["chrome.exe"], "google chrome": ["chrome.exe"],
    "edge": ["msedge.exe"], "microsoft edge": ["msedge.exe"], "firefox": ["firefox.exe"],
    "vs code": ["code.exe"], "vscode": ["code.exe"], "visual studio code": ["code.exe"], "code": ["code.exe"],
    "word": ["winword.exe"], "excel": ["excel.exe"], "powerpoint": ["powerpnt.exe"],
    "outlook": ["outlook.exe"], "notepad": ["notepad.exe"], "calculator": ["calculatorapp.exe", "calc.exe"],
    "spotify": ["spotify.exe"], "discord": ["discord.exe"], "teams": ["ms-teams.exe", "teams.exe"],
    "whatsapp": ["whatsapp.exe", "whatsapp.root.exe"], "matlab": ["matlab.exe"],
    "steam": ["steam.exe"], "vlc": ["vlc.exe"],
    "amazon music": ["amazon music.exe", "amazonmusic.exe"],
    "settings": ["systemsettings.exe"],
    "terminal": ["windowsterminal.exe", "wt.exe"], "command prompt": ["cmd.exe"], "cmd": ["cmd.exe"],
}
_EXPLORER_NAMES = {"explorer", "file explorer", "files", "folder", "folders", "explorer windows"}
_PROTECTED = {"explorer.exe", "taskmgr.exe", "svchost.exe", "winlogon.exe", "csrss.exe", "wininit.exe",
              "lsass.exe", "services.exe", "smss.exe", "dwm.exe", "system", "registry",
              "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "applicationframehost.exe"}


def close_application(app_request: str, force: bool = False) -> str:
    try:
        target = _clean_app_name(app_request)
        if len(target) < 3:
            return "Which application should I close?"
        exes = {e.lower() for e in _CLOSE_ALIASES.get(target, [target.replace(" ", "") + ".exe", target + ".exe"])}
        exes -= _PROTECTED
        explorer_windows = target in _EXPLORER_NAMES
        own_pid = os.getpid()
        procs = [] if explorer_windows else [
            p for p in psutil.process_iter(["pid", "name"])
            if (p.info["name"] or "").lower() in exes and p.pid != own_pid]
        pids = {p.pid for p in procs}
        title_rx = re.compile(rf"\b{re.escape(target)}\b", re.I)

        def matching_windows():
            hits = []
            for hwnd, title, cls, pid in _windows_snapshot():
                if explorer_windows:
                    if cls == "CabinetWClass":
                        hits.append(hwnd)
                elif pid in pids:
                    hits.append(hwnd)
                elif title and title_rx.search(title) and _process_name(pid) == "applicationframehost.exe":
                    hits.append(hwnd)                  # Store/UWP apps live inside ApplicationFrameHost
            return hits

        windows = matching_windows()
        if not windows and not procs:
            return f"I could not find {target} running."

        for hwnd in windows:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        deadline = time.time() + (1.0 if force else CLOSE_GRACE_S)
        while windows and time.time() < deadline:
            time.sleep(0.25)
            windows = matching_windows()

        if not force:
            if windows:
                return f"{target.title()} is still open. It may be asking to save. Say kill {target} to force it."
            return f"Closed {target}."

        denied = False
        for proc in procs:
            try:
                for child in proc.children(recursive=True):
                    child.kill()
                proc.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                denied = True
            except Exception:
                log.exception("kill failed for pid %s", proc.pid)
        time.sleep(0.3)
        if denied:
            return f"Windows would not let me close {target}. It may be running as administrator."
        if matching_windows():
            return f"I could not force {target} to close."
        return f"Force closed {target}."
    except Exception:
        log.exception("close_application failed")
        return "I had trouble closing that application."


# =============================================================================
# INTENTS  -  ordered registry; first match wins, so order is priority.
# =============================================================================
INTENTS: list = []
APOS = "['’]"


def intent(pattern: str):
    rx = re.compile(pattern, re.IGNORECASE)

    def decorator(fn):
        INTENTS.append((rx, fn))
        return fn
    return decorator


def dispatch(text: str):
    """Return (handled, response). response is a str, or a Confirm that needs a spoken yes/no."""
    cmd = normalize(text)
    if not cmd:
        return False, ""
    alias_target = load_custom_aliases().get(cmd.lower())
    if alias_target:
        return True, launch_dynamic_application(alias_target)
    for rx, handler in INTENTS:
        match = rx.search(cmd)
        if match:
            return True, handler(match)
    return False, ""


# ----- A. structured -----------------------------------------------------------
@intent(r"^when i say (?P<trigger>.+?) open (?P<target>.+)$")
def _intent_alias(m):
    trigger, target = m["trigger"].strip(), m["target"].strip()
    save_custom_alias(trigger, target)
    return f"Got it. When you say {trigger.lower()}, I will open {target.title()}."


@intent(r"^remember (?:that )?(?P<fact>.+)$")
def _intent_remember(m):
    save_memory_fact(m["fact"])
    return "Got it, I will remember that."


@intent(r"\b(?:forget everything|(?:clear|wipe|erase) (?:my |your )?memory)\b")
def _intent_forget(_m):
    return Confirm("This will erase everything I remember about you. Say yes to confirm.", clear_memory)


@intent(r"^(?:take a note|make a note|note that)(?: that)?\s+(?P<note>.+)$")
def _intent_note(m):
    save_note(m["note"])
    return "Note saved."


@intent(rf"\b(?:read (?:my )?notes|what(?:{APOS}s| is) in my notes)\b")
def _intent_read_notes(_m):
    return read_notes()


@intent(r"^remind me to (?P<task>.+)\s+in\s+(?P<dur>(?:(?!\bin\b).)+)$")
@intent(r"^remind me in (?P<dur>.+?) to (?P<task>.+)$")
def _intent_remind(m):
    seconds = parse_duration(m["dur"])
    if not seconds:
        return "Sorry, I did not catch how long to wait."
    schedule_alert(f"Reminder to {m['task'].strip()}", seconds)
    return f"Reminder set for {format_duration(seconds)}."


@intent(r"^(?:set (?:a |an )?)?timer (?:for )?(?P<dur>.+)$")
@intent(r"^set (?:a |an )(?P<dur>.+?) timer$")
def _intent_timer(m):
    seconds = parse_duration(m["dur"])
    if not seconds:
        return "Sorry, I did not catch how long the timer should be."
    schedule_alert(f"Your {format_duration(seconds)} timer is up.", seconds)
    return f"Timer set for {format_duration(seconds)}."


@intent(r"\b(?:cancel|stop|clear|delete) (?:all )?(?:my )?(?:timers?|reminders?|alarms?)\b")
def _intent_cancel_timers(_m):
    return cancel_all_timers()


@intent(rf"\b(?:what are my reminders|list (?:my )?(?:reminders|timers)|any (?:reminders|timers))\b")
def _intent_list_reminders(_m):
    return list_active_reminders()


@intent(r"^define\s+(?P<word>.+)$")
def _intent_define(m):
    return get_definition(m["word"])


# ----- B. anchored verbs -------------------------------------------------------
@intent(r"^close (?:this |the |that |current |the current )?window$")
def _intent_close_window(_m):
    return window_control("close_window")


@intent(r"^(?P<verb>close|quit|exit|kill)\s+(?P<app>.+)$")
def _intent_close_app(m):
    app = m["app"].strip()
    if m["verb"].lower() == "kill":
        return Confirm(f"This will force close {app} and unsaved work may be lost. Say yes to continue.",
                       lambda: close_application(app, force=True))
    return close_application(app)


@intent(r"^(?:search for|search|google)\s+(?P<query>.+)$")
def _intent_search(m):
    return perform_web_search(m["query"])


@intent(r"^(?:open|launch|start|run)\s+(?P<app>.+)$")
def _intent_open(m):
    return launch_dynamic_application(m["app"])


# ----- C. keyword intents ------------------------------------------------------
@intent(r"\bsnooze\b")
def _intent_snooze(_m):
    return snooze_last_alert()


@intent(rf"\b(?:read (?:my |the )?clipboard|what(?:{APOS}s| is) (?:on|in) my clipboard)\b")
def _intent_clipboard(_m):
    return read_clipboard()


@intent(r"\bempty (?:the |my )?(?:recycle bin|trash)\b")
def _intent_recycle(_m):
    return Confirm("This will permanently empty the recycle bin. Say yes to confirm.", empty_recycle_bin)


@intent(r"\bweather(?: like)?(?: (?:right now|today|outside|currently))? in (?P<loc>.+)$")
def _intent_weather_in(m):
    loc = re.sub(r"\s+(?:today|right now|now|currently)$", "", m["loc"].strip(), flags=re.I)
    return get_weather(loc)


@intent(r"\b(?:weather|temperature)\b")
def _intent_weather(_m):
    return get_weather("")


@intent(r"\b(?:flip|toss) a coin\b")
def _intent_coin(_m):
    return f"It is {random.choice(['heads', 'tails'])}."


@intent(r"\broll (?:a )?(?:die|dice)\b")
def _intent_dice(_m):
    return f"You rolled a {random.randint(1, 6)}."


@intent(rf"\b(?:what time is it|what(?:{APOS}s| is) the time|current time|time now)\b(?! in )")
def _intent_time(_m):
    return f"It is {datetime.now():%I:%M %p}."


@intent(rf"\b(?:what(?:{APOS}s| is) (?:the )?date|today{APOS}?s date|what day is it)\b")
def _intent_date(_m):
    return f"Today is {datetime.now():%A, %B %d, %Y}."


@intent(r"\block\b.*\b(?:pc|computer|screen|system|workstation|laptop)\b")
def _intent_lock(_m):
    return power_control("lock")


@intent(r"\bsleep\b.*\b(?:pc|computer|system|laptop)\b|\b(?:pc|computer|system|laptop)\b.*\bsleep\b")
def _intent_sleep_pc(_m):
    return power_control("sleep")


@intent(r"\b(?:show (?:my |the )?desktop|minimi[sz]e all(?: windows)?|hide (?:all )?windows)\b")
def _intent_desktop(_m):
    return window_control("show_desktop")


@intent(r"^(?:(?:pause|resume|play)(?: the)?(?: music| song| playback| video| media)?"
        r"|stop(?: the)? (?:music|song|playback|video|media))$")
def _intent_play_pause(_m):
    return control_media("play_pause")


@intent(r"\b(?:next (?:song|track)|skip(?: this)?(?: song| track)?)\b")
def _intent_next(_m):
    return control_media("next")


@intent(r"\b(?:previous|last) (?:song|track)\b")
def _intent_previous(_m):
    return control_media("previous")


@intent(r"^(?:un)?mute(?: the| my)?(?: volume| sound| audio| music)?$")
def _intent_mute(_m):
    return control_media("mute")


@intent(r"\b(?:set (?:the )?)?volume (?:to )?(?P<level>\d{1,3})\s*(?:percent|%)?\b")
def _intent_volume_exact(m):
    return set_exact_volume(int(m["level"]))


@intent(r"\b(?:volume up|louder|(?:increase|raise|turn up) (?:the )?volume)\b")
def _intent_volume_up(_m):
    return control_volume("up")


@intent(r"\b(?:volume down|quieter|(?:lower|decrease|reduce|turn down) (?:the )?volume)\b")
def _intent_volume_down(_m):
    return control_volume("down")


@intent(r"\bbrightness\b")
def _intent_brightness(m):
    return adjust_brightness(m.string)


@intent(r"\b(?:screenshot|screen capture)\b")
def _intent_screenshot(_m):
    return take_screenshot()


@intent(r"\b(?:battery|ram|system status|vitals|memory usage)\b")
def _intent_vitals(_m):
    return get_system_vitals()


# =============================================================================
# AUDIO INPUT  (one persistent stream; frames are dropped while Nicola is speaking)
# =============================================================================
class MicStream:
    def __init__(self):
        self.q: "queue.Queue" = queue.Queue(maxsize=300)       # ~30 s of audio at most
        self._stream = None
        self._overflowed = False

    def _callback(self, indata, _frames, _time_info, status):
        if status:
            self._overflowed = True                            # logged outside the audio thread
        if tts.is_busy():
            return                                             # never listen to ourselves
        block = indata[:, 0].copy()
        try:
            self.q.put_nowait(block)
        except queue.Full:
            try:
                self.q.get_nowait()                            # drop the oldest block
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(block)
            except queue.Full:
                pass

    def start(self):
        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                      blocksize=BLOCK_SIZE, callback=self._callback)
        self._stream.start()

    def restart(self):
        log.warning("No audio for several seconds; restarting the microphone stream")
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            log.exception("error closing the microphone stream")
        self.start()

    def get(self, timeout=0.5):
        if self._overflowed:
            self._overflowed = False
            log.warning("Audio input overflow reported by sounddevice")
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return


def _rms(block) -> float:
    return float(np.sqrt(np.mean(np.square(block))))


class Listener:
    """Turns the block stream into utterances using a calibrated energy threshold + pre-roll."""

    def __init__(self, mic: MicStream):
        self.mic = mic
        self.floor = 0.006

    def threshold(self) -> float:
        return min(MAX_THRESH, max(MIN_THRESH, self.floor * FLOOR_MULT))

    def calibrate(self, seconds: float = 1.2):
        self.mic.drain()
        values = []
        wanted = int(seconds * BLOCKS_PER_SEC)
        while len(values) < wanted and not shutdown.is_set():
            block = self.mic.get(timeout=0.5)
            if block is None:
                break
            values.append(_rms(block))
        if values:
            # Use 75th percentile of ambient noise so minor fan/room hums are absorbed into the floor
            self.floor = float(np.percentile(values, 75))
        log.info("Noise floor %.5f -> threshold %.5f", self.floor, self.threshold())

    def listen(self, awake: bool):
        """Return a normalized float32 array holding one utterance, or None if nothing was said."""
        silence_blocks = int((AWAKE_SILENCE_S if awake else STANDBY_SILENCE_S) * BLOCKS_PER_SEC)
        max_blocks = int((AWAKE_MAX_S if awake else STANDBY_MAX_S) * BLOCKS_PER_SEC)
        wait_blocks = int(WAIT_FOR_SPEECH_S * BLOCKS_PER_SEC)
        min_blocks = int(MIN_SPEECH_S * BLOCKS_PER_SEC)

        preroll = deque(maxlen=PREROLL_BLOCKS)
        recorded = []
        speaking = False
        silent_run = 0
        loud = 0                                               # blocks that were actually above threshold
        waited = 0
        empty_polls = 0

        while not shutdown.is_set():
            block = self.mic.get(timeout=0.5)
            if block is None:
                empty_polls += 1
                if empty_polls >= 10:                          # ~5 s without audio: device went away?
                    empty_polls = 0
                    try:
                        self.mic.restart()
                    except Exception:
                        log.exception("could not restart the microphone")
                continue
            empty_polls = 0
            level = _rms(block)
            thr = self.threshold()

            if not speaking:
                if level >= thr:
                    speaking = True
                    loud = 1
                    recorded.extend(preroll)                   # keep the soft start of "Nicola"
                    recorded.append(block)
                    preroll.clear()
                    if awake:
                        state.update(nicola_text="Listening to you...")
                else:
                    # Slowly adapt noise floor upward or downward when idle
                    self.floor = 0.96 * self.floor + 0.04 * level
                    preroll.append(block)
                    waited += 1
                    if waited >= wait_blocks:
                        return None
            else:
                recorded.append(block)
                # Hysteresis: once speaking, allow energy down to 75% of trigger threshold before counting silence
                if level < (thr * 0.75):
                    silent_run += 1
                else:
                    silent_run = 0
                    loud += 1
                if silent_run >= silence_blocks or len(recorded) >= max_blocks:
                    break
        else:
            return None                                        # shutting down

        if loud < min_blocks:                                  # a click, desk bump, or breath, not speech
            return None

        audio = np.concatenate(recorded)
        peak = float(np.max(np.abs(audio)))
        if peak < MIN_THRESH:
            return None
        # Normalize peak amplitude to 0.90 so Whisper always receives clean, consistent volume
        return (audio / peak * 0.90).astype(np.float32)


# =============================================================================
# SPEECH TO TEXT  (beam_size=5 + temperature=0.0 to eliminate word mix-ups)
# =============================================================================
class Transcriber:
    def __init__(self):
        self.backend = None
        try:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            self.backend = "faster-whisper"
        except ImportError:
            try:
                import whisper
            except ImportError as exc:
                raise RuntimeError("Install faster-whisper (recommended) or openai-whisper") from exc
            self.model = whisper.load_model(WHISPER_MODEL, device="cpu")
            self.backend = "openai-whisper"
        log.info("Speech backend: %s (%s)", self.backend, WHISPER_MODEL)

    def transcribe(self, audio) -> str:
        prompt = WHISPER_INITIAL_PROMPT or None
        parts = []
        if self.backend == "faster-whisper":
            segments, _info = self.model.transcribe(
                audio,
                language="en",
                beam_size=5,
                best_of=5,
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=True,
                initial_prompt=prompt,
            )
            for seg in segments:
                if seg.no_speech_prob > 0.55 or seg.avg_logprob < -1.0:
                    continue                                    # filter out mumbles / background noise
                parts.append(seg.text)
        else:
            result = self.model.transcribe(
                audio,
                language="en",
                fp16=False,
                beam_size=5,
                temperature=0.0,
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            for seg in result.get("segments", []):
                if seg.get("no_speech_prob", 0) > 0.55 or seg.get("avg_logprob", 0) < -1.0:
                    continue
                parts.append(seg["text"])
        text = " ".join(p.strip() for p in parts).strip()
        bare = re.sub(r"[^\w\s']", "", text.lower()).strip()
        if bare in HALLUCINATIONS or len(bare) < 2:
            return ""
        return text


# =============================================================================
# LM STUDIO
# =============================================================================
lms_started_by_us = False


def lmstudio_alive() -> bool:
    try:
        return requests.get(LM_STUDIO_BASE + "/v1/models", timeout=1.5).ok
    except requests.exceptions.RequestException:
        return False


def preload_lmstudio_model():
    """Start the LM Studio server if it is not already running, then warm the model up."""
    global lms_started_by_us
    try:
        if not lmstudio_alive():
            try:
                subprocess.Popen(["lms", "server", "start"], creationflags=NO_WINDOW)
                lms_started_by_us = True
            except OSError:
                log.warning("The 'lms' CLI was not found; start the LM Studio server manually")
        payload = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
        for _ in range(36):
            if shutdown.is_set():
                return
            try:
                res = requests.post(LM_STUDIO_URL, json=payload, timeout=120)
                if res.ok:
                    log.info("LM Studio model is warm")
                    return
                log.warning("Warm-up got HTTP %s: %s", res.status_code, res.text[:200])
            except requests.exceptions.RequestException:
                pass
            time.sleep(5)
    except Exception:
        log.exception("preload_lmstudio_model failed")


def stop_lmstudio_if_ours():
    if lms_started_by_us:
        try:
            subprocess.Popen(["lms", "server", "stop"], creationflags=NO_WINDOW)
        except OSError:
            pass


_CMD_RESCUE_RE = re.compile(r"^\[CMD:\s*(.+?)\]$", re.I)


class LLMClient:
    def __init__(self):
        self.history: list = []

    def _build_messages(self, prompt: str) -> list:
        system = SYSTEM_PROMPT
        facts = load_memory()
        if facts:
            system += "\n\nThings you know about the user: " + "; ".join(facts)
        return [{"role": "system", "content": system}] + self.history[-6:] + [{"role": "user", "content": prompt}]

    def respond(self, prompt: str) -> Optional[str]:
        """
        Stream the answer and speak it sentence by sentence.
        If the LLM outputs [CMD: <canonical>], returns the rescued command string without speaking.
        Otherwise speaks the reply and returns None.
        """
        payload = {"model": MODEL_ID, "messages": self._build_messages(prompt),
                   "temperature": 0.2, "max_tokens": 150, "stream": True}
        spoken, buf, finish = [], "", None
        error_message = None
        maybe_cmd = True
        try:
            with requests.post(LM_STUDIO_URL, json=payload, stream=True, timeout=(5, 45)) as res:
                res.raise_for_status()
                for raw in res.iter_lines():
                    if shutdown.is_set():
                        break
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not chunk.get("choices"):
                        continue
                    choice = chunk["choices"][0]
                    buf += (choice.get("delta") or {}).get("content") or ""
                    finish = choice.get("finish_reason") or finish

                    stripped = buf.lstrip()
                    if maybe_cmd:
                        if len(stripped) < 6 and "[CMD:".startswith(stripped.upper()):
                            continue
                        if stripped.upper().startswith("[CMD:"):
                            continue
                        maybe_cmd = False

                    sentences, buf = split_sentences(buf)
                    for sentence in sentences:
                        tts.say(sentence, block=False)
                        spoken.append(sentence)
        except requests.exceptions.ConnectionError:
            error_message = "I could not reach LM Studio. Is the server running?"
        except requests.exceptions.Timeout:
            error_message = "LM Studio is taking too long to answer. The model may still be loading."
        except requests.exceptions.HTTPError as exc:
            log.exception("LM Studio returned an error")
            error_message = f"LM Studio returned an error, status {exc.response.status_code}."
        except Exception:
            log.exception("LLM request failed")
            error_message = "Something went wrong while I was thinking."

        tail = buf.strip()
        cmd_match = _CMD_RESCUE_RE.match(tail) if not spoken else None
        if cmd_match:
            rescued = cmd_match.group(1).strip()
            log.info("LLM rescued command: %s", rescued)
            return rescued

        if tail and not error_message:
            cut_off = finish == "length" and not re.search(r"[.!?][\"')\]]*$", tail)
            if not (cut_off and spoken):                       # drop a half sentence only if we said something
                tts.say(tail, block=False)
                spoken.append(tail)
        if error_message and not spoken:
            tts.say(error_message, block=False)
        tts.wait_idle()

        reply = " ".join(spoken).strip()
        if reply:
            self.history.append({"role": "user", "content": prompt})
            self.history.append({"role": "assistant", "content": reply})
            del self.history[:-20]
        return None


# =============================================================================
# MAIN ASSISTANT LOOP
# =============================================================================
class Session:
    def __init__(self):
        self.awake = False
        self.last = 0.0                       # time of the last finished interaction
        self.pending: Optional[Confirm] = None
        self.pending_deadline = 0.0

    def touch(self):
        self.last = time.time()


def assistant_loop():
    log.info("Assistant thread started")
    tts.say("Loading offline transcription engine. This will take a moment.")
    try:
        transcriber = Transcriber()
    except Exception:
        log.exception("Failed to load the speech model")
        tts.say("I hit a critical error loading my speech model. Please check the log file.")
        shutdown.set()
        return

    mic = MicStream()
    try:
        mic.start()
    except Exception:
        log.exception("Could not open the microphone")
        tts.say("I could not open the microphone. Please check your audio settings.")
        shutdown.set()
        return

    listener = Listener(mic)
    listener.calibrate()
    llm = LLMClient()
    sess = Session()
    tts.say("Systems initialized. I am in standby mode. Say my name to wake me.")
    mic.drain()

    def go_standby(message: str):
        sess.awake = False
        sess.pending = None
        state.update(phase="idle", user_text="", nicola_text="")
        llm.history.clear()
        tts.say(message)

    def handle_intent_response(response):
        if isinstance(response, Confirm):
            sess.pending = response
            sess.pending_deadline = time.time() + CONFIRM_TIMEOUT
            tts.say(response.prompt)
        elif response:
            tts.say(response)
        sess.touch()

    while not shutdown.is_set():
        try:
            if sess.awake and time.time() - sess.last > SESSION_TIMEOUT:
                go_standby("Entering standby mode.")
                continue

            if sess.awake:
                state.update(phase="listening")
                if not tts.is_busy():
                    state.update(nicola_text="Awaiting command...")
            else:
                state.update(phase="idle", user_text="")

            audio = listener.listen(sess.awake)
            if audio is None:
                continue
            if sess.awake:
                state.update(nicola_text="Transcribing...")
            text = transcriber.transcribe(audio)
            if not text:
                continue
            safe_print(f"\n[You]: {text}")

            was_awake = sess.awake
            if was_awake and STANDBY_RE.search(text):         # only honoured while she is awake
                go_standby("Standby mode enabled. Say my name to wake me.")
                continue

            has_wake = contains_wake_word(text)
            if not was_awake:
                if not has_wake:
                    continue
                sess.awake = True
                sess.touch()
                state.update(phase="listening")
                command = strip_wake_word(text)
            else:
                command = strip_wake_word(text) if has_wake else normalize(text)

            min_len = 1 if sess.pending is not None else 3    # "no" is a valid answer to a question
            if len(command) < min_len:                        # just her name, or noise
                if has_wake:
                    tts.say("Yes?")
                    sess.touch()
                continue

            state.update(user_text=command)

            # Answer to a pending yes/no question. Anything else cancels it and is handled normally.
            if sess.pending is not None:
                pending, sess.pending = sess.pending, None
                if time.time() <= sess.pending_deadline:
                    answer = classify_yes_no(command)
                    if answer is True:
                        tts.say(pending.action())
                        sess.touch()
                        continue
                    if answer is False:
                        tts.say("Okay, cancelled.")
                        sess.touch()
                        continue

            handled, response = dispatch(command)
            if handled:
                handle_intent_response(response)
                continue

            state.update(phase="thinking", nicola_text="Thinking...")
            rescued_cmd = llm.respond(command)
            if rescued_cmd:
                handled_rescue, rescue_response = dispatch(rescued_cmd)
                if handled_rescue:
                    handle_intent_response(rescue_response)
                else:
                    tts.say(f"I tried to run {rescued_cmd}, but could not match that command.")
            state.update(phase="listening")
            sess.touch()

        except Exception:
            log.exception("Error in the assistant loop")
            time.sleep(1)

    log.info("Assistant thread finished")


# =============================================================================
# TRAY ICON + FLOATING OVERLAY
# =============================================================================
def create_tray_icon_image():
    image = Image.new("RGB", (64, 64), color=(0, 0, 0))
    ImageDraw.Draw(image).ellipse((4, 4, 60, 60), fill=(0, 120, 215))
    return image


def run_system_tray():
    def quit_app(icon, _item):
        shutdown.set()                       # everything else (goodbye, LM Studio) is handled in main()
        icon.stop()

    menu = pystray.Menu(
        MenuItem("Silence Speech (Esc)", lambda _icon, _item: tts.interrupt()),
        MenuItem("Quit Nicola", quit_app),
    )
    icon = pystray.Icon("Nicola", create_tray_icon_image(), "Nicola Assistant", menu)
    icon.run_detached()
    return icon


def main():
    setup_logging()
    log.info("Nicola starting")
    tts.start()
    restore_reminders()
    threading.Thread(target=populate_app_cache, daemon=True, name="app-cache").start()
    threading.Thread(target=preload_lmstudio_model, daemon=True, name="lm-warmup").start()
    threading.Thread(target=assistant_loop, daemon=True, name="assistant").start()
    run_system_tray()

    root = tk.Tk()
    root.overrideredirect(True)
    root.wm_attributes("-topmost", True)
    root.wm_attributes("-transparentcolor", "black")
    root.config(bg="black")

    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    window_width, window_height = 1000, 200
    root.geometry(f"{window_width}x{window_height}+{screen_width // 2 - window_width // 2}+{screen_height - 220}")
    root.update_idletasks()

    # Make the HUD 100% click-through and hide it from the Alt+Tab switcher
    try:
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            ex_style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        )
    except Exception:
        log.exception("Could not apply click-through window style")

    canvas = tk.Canvas(root, width=window_width, height=window_height, bg="black", highlightthickness=0)
    canvas.pack()

    last_drawn = [None]                      # redraw only when something actually changed

    def update_ui():
        if shutdown.is_set():
            root.destroy()
            return

        phase, speaking, user_text, nicola_text = state.snapshot()
        if phase == "idle" and not speaking:
            key = None
        else:
            disp_user = user_text if len(user_text) < 90 else user_text[:87] + "..."
            disp_nicola = nicola_text if len(nicola_text) < 130 else nicola_text[:127] + "..."
            key = (f"You: {disp_user}\n\nNicola: {disp_nicola}" if disp_user else f"Nicola: {disp_nicola}")

        if key != last_drawn[0]:
            canvas.delete("all")
            if key is None:
                root.withdraw()
            else:
                root.deiconify()
                font_config = ("Segoe UI", 16, "bold")
                offsets = [(2, 2), (-2, -2), (2, -2), (-2, 2), (0, 2), (2, 0), (-2, 0), (0, -2)]
                for ox, oy in offsets:
                    canvas.create_text(window_width // 2 + ox, window_height // 2 + oy, text=key,
                                       font=font_config, fill="#111111", justify="center",
                                       width=window_width - 20)
                canvas.create_text(window_width // 2, window_height // 2, text=key, font=font_config,
                                   fill="white", justify="center", width=window_width - 20)
            last_drawn[0] = key
        root.after(50, update_ui)

    root.after(50, update_ui)
    try:
        root.mainloop()
    finally:
        shutdown.set()
        try:
            tts.say("Shutting down Nicola.", timeout=5)
        except Exception:
            log.exception("goodbye failed")
        stop_lmstudio_if_ours()
        log.info("Nicola stopped")


if __name__ == "__main__":
    main()