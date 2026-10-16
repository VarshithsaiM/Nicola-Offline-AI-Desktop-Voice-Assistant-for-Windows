Nicola — Offline AI Desktop Voice Assistant for Windows

![Python](https://img.shields.io/badge/Python-3.10%2B%20(64--bit)-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)
![Whisper](https://img.shields.io/badge/STT-OpenAI%20Whisper%20(Local)-74aa9c?logo=openai&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-LM%20Studio%20%2F%20Gemma%203-orange)
![License](https://img.shields.io/badge/License-MIT-green)

"Nicola" is a high-performance, privacy-first, offline voice assistant built natively for Windows. Instead of relying on cloud voice APIs that stream your microphone data to remote servers, Nicola runs **100% locally**—combining **OpenAI Whisper** (`faster-whisper` / `openai-whisper`) on your CPU for speech-to-text, **LM Studio (`google/gemma-3-4b`)** on your GPU for conversational intelligence, and a zero-lag **Windows SAPI** worker thread for speech synthesis.


Key Features

* 100% Offline Core: Wake-word detection, voice transcription (Whisper), and LLM reasoning (LM Studio) run entirely on your machine.
* Zero-Latency Intent Registry: Everyday OS commands are intercepted by an ordered word-boundary regex registry and executed in milliseconds without waiting for the LLM.
* LLM Intent Rescue (`[CMD: ...]`): If you phrase a system command casually and it slips past the regex matcher, the local LLM translates your intent into a canonical command tag and routes it back to the OS dispatcher automatically.
* Sentence-by-Sentence SSE Streaming: LLM replies, and long clipboard texts are streamed and spoken sentence-by-sentence via an asynchronous SAPI worker thread. Press **`Esc`** at any time to cut speech off mid-word.
* Click-Through Stealth HUD: A borderless, high-contrast dual-line Tkinter overlay displays live `You:` and `Nicola:` transcripts at the bottom of your screen. Built with `WS_EX_TRANSPARENT` and `WS_EX_TOOLWINDOW` Win32 styles so mouse clicks pass straight through to your games/app,s and it never clutters `Alt+Tab`.
* Adaptive RMS Noise Gate: Continuously calibrates to your room's ambient noise floor with a 400ms pre-roll buffer and fuzzy phonetic wake-word matching so the start of *"Nicola"* is never clipped.
* Atomic Persistent Memory (RAG): Teach Nicola custom app aliases, personal facts, timestamped notes, and persistent timers/reminders that survive system restarts.

---

Voice Commands Cheat Sheet

Wake Nicola from standby by saying **"Nicola"** (e.g., *"Nicola, open calculator"*), or wake her first and speak naturally within the 25-second active session window.

| Category | Example Voice Commands |
| :--- | :--- |
| **App Control** | *"Open Chrome"*, *"Launch MATLAB"*, *"Close WhatsApp"*, *"Kill Spotify"* (asks for yes/no confirmation) |
| **Custom App Aliases** | *"When I say edit code open VS Code"* |
| **Window & Power** | *"Show my desktop"*, *"Close this window"*, *"Lock my PC"*, *"Put computer to sleep"* |
| **Audio & Media** | *"Volume up"*, *"Volume down"*, *"Set volume to 45 percent"*, *"Mute"*, *"Pause music"*, *"Next track"* |
| **Hardware & Vitals** | *"Set brightness to 70"*, *"Brightness down"*, *"Take a screenshot"*, *"System status"* / *"Battery"* |
| **Timers & Reminders** | *"Set a timer for 10 minutes"*, *"Remind me to check the circuit in an hour and a half"*, *"Snooze"*, *"List my reminders"*, *"Cancel all timers"* |
| **Memory & Notes** | *"Remember that I am building a pure sine wave inverter"*, *"Clear my memory"*, *"Take a note that..."*, *"Read my notes"* |
| **Utilities** | *"Read my clipboard"*, *"Empty the recycle bin"*, *"Define hysteresis"*, *"Weather in Hyderabad"*, *"Flip a coin"*, *"Roll a die"* |
| **Standby / Silence** | *"Goodbye"* / *"Go to sleep"* (returns to standby), or press **`Esc`** to silence speech immediately |


Prerequisites

1. **Windows 10 or Windows 11**
2. **Python 3.10+ (64-bit)** — *64-bit is required for PyTorch/Whisper.*
3. **[LM Studio](https://lmstudio.ai/)** installed with the **`google/gemma-3-4b`** model downloaded (or any OpenAI-compatible local model).
   * Enable the **Local Server** in LM Studio (default port `1234`), or ensure the `lms` CLI is in your PATH so Nicola can auto-start the server headlessly.


Installation & Setup

 1. Clone the Repository
    ```powershell
git clone [https://github.com/VarshithsaiM/nicola-assistant.git](https://github.com/VarshiithsaiM/Nicola-Offline-AI-Desktop-Voice-Assistant-for-Windows
.git)
cd nicola-assistant
```powershell
git clone [https://github.com/VarshithsaiM/nicola-assistant.git](https://github.com/VarshithsaiM/Nicola-Offline-AI-Desktop-Voice-Assistant-for-Windows
.git)
cd nicola-assistant
