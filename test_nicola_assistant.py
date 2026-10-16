"""
Tests for the pure-logic parts of nicola_assistant.py (no microphone, speakers or Windows calls).
Run from the same folder with:   python -m unittest test_nicola_assistant -v
"""
import os
import tempfile
import unittest
from unittest import mock

import nicola_assistant as na


class Dispatch(unittest.TestCase):
    """Every handler is replaced by a mock, so we only test which one gets chosen."""

    def route(self, text, **overrides):
        names = ["launch_dynamic_application", "get_system_vitals", "control_media", "control_volume",
                 "window_control", "power_control", "perform_web_search", "get_weather", "get_definition",
                 "close_application", "save_memory_fact", "save_custom_alias", "schedule_alert",
                 "adjust_brightness", "take_screenshot", "read_clipboard", "snooze_last_alert",
                 "save_note"]
        patches = {n: mock.patch.object(na, n, return_value=f"<{n}>") for n in names}
        patches["aliases"] = mock.patch.object(na, "load_custom_aliases", return_value=overrides.get("aliases", {}))
        mocks = {k: p.start() for k, p in patches.items()}
        self.addCleanup(mock.patch.stopall)
        handled, response = na.dispatch(text)
        return handled, response, mocks

    def test_open_beats_keyword_intents(self):
        # These all used to be hijacked by substring keywords (ram / desktop / resume / battery).
        for text, expected in [("open telegram", "telegram"),
                               ("Open WhatsApp desktop.", "WhatsApp desktop"),
                               ("open my resume", "my resume"),
                               ("open battery saver", "battery saver"),
                               ("Open Chrome.", "Chrome")]:
            handled, _, m = self.route(text)
            self.assertTrue(handled, text)
            m["launch_dynamic_application"].assert_called_once_with(expected)
            m["get_system_vitals"].assert_not_called()
            m["control_media"].assert_not_called()
            m["window_control"].assert_not_called()

    def test_no_false_positives_for_chat(self):
        for text in ["how long is my commute", "what time is it in Tokyo", "tell me a joke",
                     "what is the capital of France", "explain quantum computing"]:
            handled, _, _ = self.route(text)
            self.assertFalse(handled, text)

    def test_keyword_intents_still_work(self):
        handled, _, m = self.route("what's my RAM usage")
        self.assertTrue(handled); m["get_system_vitals"].assert_called_once()
        handled, _, m = self.route("battery")
        self.assertTrue(handled); m["get_system_vitals"].assert_called_once()
        handled, _, m = self.route("mute")
        m["control_media"].assert_called_once_with("mute")
        handled, _, m = self.route("resume")
        m["control_media"].assert_called_once_with("play_pause")
        handled, _, m = self.route("skip this song")
        m["control_media"].assert_called_once_with("next")
        handled, _, m = self.route("volume up")
        m["control_volume"].assert_called_once_with("up")
        handled, _, m = self.route("lock my computer")
        m["power_control"].assert_called_once_with("lock")
        handled, _, m = self.route("put the computer to sleep")
        m["power_control"].assert_called_once_with("sleep")
        handled, _, m = self.route("minimize all windows")
        m["window_control"].assert_called_once_with("show_desktop")
        handled, _, m = self.route("close this window")
        m["window_control"].assert_called_once_with("close_window")
        handled, _, m = self.route("take a screenshot")
        m["take_screenshot"].assert_called_once()

    def test_time_and_date(self):
        handled, response, _ = self.route("what time is it")
        self.assertTrue(handled); self.assertTrue(response.startswith("It is "))
        handled, response, _ = self.route("What's the date today?")
        self.assertTrue(handled); self.assertTrue(response.startswith("Today is "))

    def test_weather(self):
        _, _, m = self.route("what's the weather like in Paris?")
        m["get_weather"].assert_called_once_with("Paris")
        _, _, m = self.route("weather in Hyderabad today")
        m["get_weather"].assert_called_once_with("Hyderabad")
        _, _, m = self.route("how's the weather")
        m["get_weather"].assert_called_once_with("")

    def test_search_define_and_memory_keep_case(self):
        _, _, m = self.route("search for Python tutorials")
        m["perform_web_search"].assert_called_once_with("Python tutorials")
        _, _, m = self.route("define serendipity")
        m["get_definition"].assert_called_once_with("serendipity")
        _, _, m = self.route("Remember that my name is Sai.")
        m["save_memory_fact"].assert_called_once_with("my name is Sai")
        _, _, m = self.route("take a note that Buy Milk")
        m["save_note"].assert_called_once_with("Buy Milk")

    def test_reminders_and_timers(self):
        _, _, m = self.route("remind me to call mom in 10 minutes")
        m["schedule_alert"].assert_called_once_with("Reminder to call mom", 600)
        _, _, m = self.route("remind me to check the oven in the kitchen in five minutes")
        m["schedule_alert"].assert_called_once_with("Reminder to check the oven in the kitchen", 300)
        _, _, m = self.route("remind me in half an hour to stretch")
        m["schedule_alert"].assert_called_once_with("Reminder to stretch", 1800)
        _, _, m = self.route("set a timer for 90 seconds")
        m["schedule_alert"].assert_called_once_with("Your 1 minute and 30 seconds timer is up.", 90)
        _, _, m = self.route("set a ten minute timer")
        m["schedule_alert"].assert_called_once_with("Your 10 minutes timer is up.", 600)
        handled, response, m = self.route("remind me to sleep in a while")
        self.assertIn("did not catch", response)
        m["schedule_alert"].assert_not_called()

    def test_close_and_kill(self):
        _, _, m = self.route("close chrome")
        m["close_application"].assert_called_once_with("chrome")
        handled, response, m = self.route("kill chrome")
        self.assertIsInstance(response, na.Confirm)
        m["close_application"].assert_not_called()           # nothing happens until the user says yes
        self.assertEqual(response.action(), "<close_application>")
        m["close_application"].assert_called_once_with("chrome", force=True)

    def test_destructive_actions_need_confirmation(self):
        for text in ["forget everything", "clear my memory", "empty the recycle bin"]:
            handled, response, _ = self.route(text)
            self.assertTrue(handled, text)
            self.assertIsInstance(response, na.Confirm, text)

    def test_custom_alias(self):
        handled, _, m = self.route("my editor", aliases={"my editor": "notepad"})
        self.assertTrue(handled)
        m["launch_dynamic_application"].assert_called_once_with("notepad")
        _, _, m = self.route("when I say browser open Chrome")
        m["save_custom_alias"].assert_called_once_with("browser", "Chrome")


class WakeWords(unittest.TestCase):
    def test_boundaries(self):
        self.assertTrue(na.contains_wake_word("Nicola, what time is it"))
        self.assertTrue(na.contains_wake_word("hey Nikola"))
        self.assertFalse(na.contains_wake_word("I would love a Coca-Cola"))
        self.assertFalse(na.contains_wake_word("pina colada"))

    def test_strip(self):
        self.assertEqual(na.strip_wake_word("Nicola, open Chrome."), "open Chrome")
        self.assertEqual(na.strip_wake_word("Nicola"), "")


class Utilities(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(na.normalize("  Open   Chrome. "), "Open Chrome")
        self.assertEqual(na.normalize("open chrome, please!"), "open chrome")

    def test_parse_duration(self):
        cases = {"5 minutes": 300, "five minutes": 300, "twenty five minutes": 1500,
                 "an hour": 3600, "half an hour": 1800, "an hour and a half": 5400,
                 "1 hour and 30 minutes": 5400, "two and a half hours": 9000, "90 seconds": 90,
                 "a minute": 60, "1.5 hours": 5400, "seventeen minutes": 1020}
        for text, seconds in cases.items():
            self.assertEqual(na.parse_duration(text), seconds, text)
        self.assertIsNone(na.parse_duration("in a while"))

    def test_format_duration(self):
        self.assertEqual(na.format_duration(300), "5 minutes")
        self.assertEqual(na.format_duration(5400), "1 hour and 30 minutes")
        self.assertEqual(na.format_duration(1), "1 second")

    def test_yes_no(self):
        self.assertTrue(na.classify_yes_no("Yes."))
        self.assertTrue(na.classify_yes_no("go ahead"))
        self.assertFalse(na.classify_yes_no("No, cancel that"))
        self.assertIsNone(na.classify_yes_no("what time is it"))

    def test_split_sentences(self):
        done, rest = na.split_sentences("The answer is 3.5 degrees outside. It feels warm and Dr. Smith agrees. And so")
        self.assertEqual(done, ["The answer is 3.5 degrees outside.", "It feels warm and Dr. Smith agrees."])
        self.assertEqual(rest, "And so")
        done, rest = na.split_sentences("Short. Hi.")
        self.assertEqual(done, [])                                # too short to speak on its own yet

    def test_best_match(self):
        names = ["microsoft word", "wordpad", "word", "visual studio code", "git uninstall"]
        self.assertEqual(na._best_match("word", names), "word")
        self.assertEqual(na._best_match("visual studio", names), "visual studio code")
        self.assertIsNone(na._best_match("a", names))              # too short for substring matches
        self.assertEqual(na._best_match("wrodpad", names), "wordpad")   # speech near miss


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, name in [("MEMORY_FILE", "m.json"), ("REMINDERS_FILE", "r.json"),
                           ("ALIASES_FILE", "a.json"), ("NOTES_FILE", "n.txt")]:
            p = mock.patch.object(na, attr, os.path.join(self.tmp.name, name))
            p.start()
            self.addCleanup(p.stop)

    def test_memory_roundtrip_and_cap(self):
        for i in range(20):
            na.save_memory_fact(f"fact {i}")
        facts = na.load_memory()
        self.assertEqual(len(facts), na.MAX_MEMORY_FACTS)
        self.assertEqual(facts[-1], "fact 19")

    def test_corrupt_file_is_kept_not_overwritten(self):
        with open(na.MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(na.load_memory(), [])
        self.assertTrue(os.path.exists(na.MEMORY_FILE + ".corrupt"))

    def test_notes_are_utf8(self):
        na.save_note("café – naïve ’quote’")
        with open(na.NOTES_FILE, encoding="utf-8") as f:
            self.assertIn("café", f.read())

    def test_reminders_persist_and_restore(self):
        with mock.patch.object(na, "_start_timer") as start:
            na.schedule_alert("Reminder to test", 120)
            self.assertEqual(len(na._load_reminders()), 1)
            na.restore_reminders()
            self.assertEqual(start.call_count, 2)               # once when scheduled, once on restore

    def test_stale_reminders_are_dropped(self):
        na._atomic_write_json(na.REMINDERS_FILE, [{"id": "x", "task": "old", "due": 1.0}])
        with mock.patch.object(na, "_start_timer") as start:
            na.restore_reminders()
            start.assert_not_called()
        self.assertEqual(na._load_reminders(), [])


class FakeMic:
    """Feeds a fixed list of audio blocks to the Listener."""
    def __init__(self, blocks):
        self.blocks = list(blocks)

    def get(self, timeout=0.5):
        return self.blocks.pop(0) if self.blocks else None

    def drain(self):
        self.blocks.clear()

    def restart(self):
        pass


def _blocks(level, count):
    import numpy as np
    return [np.full(na.BLOCK_SIZE, level, dtype="float32") for _ in range(count)]


class ListenerTests(unittest.TestCase):
    def setUp(self):
        na.shutdown.clear()
        self.addCleanup(na.shutdown.clear)

    def listener(self, blocks):
        lst = na.Listener(FakeMic(blocks))
        lst.floor = 0.002                      # threshold = max(MIN_THRESH, 0.005) = MIN_THRESH
        return lst

    def test_utterance_includes_preroll_and_trailing_silence(self):
        quiet, loud = 0.001, 0.1
        audio = self.listener(_blocks(quiet, 10) + _blocks(loud, 8) + _blocks(quiet, 10)).listen(awake=False)
        self.assertIsNotNone(audio)
        # 3 pre-roll blocks + 8 speech blocks + 6 blocks of silence that ended the utterance
        self.assertEqual(len(audio), (na.PREROLL_BLOCKS + 8 + 6) * na.BLOCK_SIZE)

    def test_pure_noise_returns_none(self):
        self.assertIsNone(self.listener(_blocks(0.001, 40)).listen(awake=False))

    def test_single_click_is_ignored(self):
        blocks = _blocks(0.001, 5) + _blocks(0.2, 1) + _blocks(0.001, 12)
        self.assertIsNone(self.listener(blocks).listen(awake=False))

    def test_threshold_is_clamped(self):
        lst = na.Listener(FakeMic([]))
        lst.floor = 1.0
        self.assertEqual(lst.threshold(), na.MAX_THRESH)
        lst.floor = 0.0
        self.assertEqual(lst.threshold(), na.MIN_THRESH)


class FakeStream:
    def __init__(self, lines, error=None):
        self.lines, self.error = lines, error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for line in self.lines:
            yield line.encode("utf-8")


def sse(*pieces, finish="stop"):
    import json
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': p}, 'finish_reason': None}]})}" for p in pieces]
    lines.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}")
    lines.append("data: [DONE]")
    return lines


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.fake_tts = mock.MagicMock()
        for p in (mock.patch.object(na, "tts", self.fake_tts),
                  mock.patch.object(na, "load_memory", return_value=["my name is Sai"])):
            p.start()
            self.addCleanup(p.stop)

    def spoken(self):
        return [c.args[0] for c in self.fake_tts.say.call_args_list]

    def test_streams_sentence_by_sentence(self):
        pieces = ["The weather is lovely today", ". You might want a light jacket", " later on. Enjoy", " it!"]
        with mock.patch.object(na.requests, "post", return_value=FakeStream(sse(*pieces))):
            client = na.LLMClient()
            reply = client.respond("how is the weather")
        self.assertEqual(self.spoken(), ["The weather is lovely today.",
                                         "You might want a light jacket later on.", "Enjoy it!"])
        self.assertEqual(reply, " ".join(self.spoken()))
        self.assertEqual([m["role"] for m in client.history], ["user", "assistant"])
        self.fake_tts.wait_idle.assert_called()

    def test_truncated_reply_drops_the_half_sentence(self):
        pieces = ["This sentence is complete and fine. And this one is cut o"]
        with mock.patch.object(na.requests, "post", return_value=FakeStream(sse(*pieces, finish="length"))):
            na.LLMClient().respond("hi")
        self.assertEqual(self.spoken(), ["This sentence is complete and fine."])

    def test_memory_goes_into_the_system_prompt(self):
        client = na.LLMClient()
        system = client._build_messages("hello")[0]["content"]
        self.assertIn("my name is Sai", system)
        self.assertIn("cannot control the computer", system)

    def test_error_messages_are_specific(self):
        cases = [(na.requests.exceptions.ConnectionError(), "could not reach LM Studio"),
                 (na.requests.exceptions.Timeout(), "taking too long")]
        for error, expected in cases:
            self.fake_tts.reset_mock()
            with mock.patch.object(na.requests, "post", side_effect=error):
                reply = na.LLMClient().respond("hi")
            self.assertEqual(reply, "")
            self.assertIn(expected, self.spoken()[0])


class TTSTests(unittest.TestCase):
    def test_worker_speaks_in_order_and_updates_state(self):
        voice = mock.MagicMock()
        app_state = na.AppState()
        engine = na.TTS(app_state)
        with mock.patch.object(na.win32com.client, "Dispatch", return_value=voice):
            engine.start()
            engine.say("first sentence here", block=False)
            engine.say("second sentence here", block=True, timeout=5)
        self.assertTrue(engine.wait_idle(timeout=5))
        self.assertEqual([c.args[0] for c in voice.Speak.call_args_list],
                         ["first sentence here", "second sentence here"])
        self.assertFalse(app_state.snapshot()[1])            # no longer "speaking"
        self.assertTrue(engine.is_busy())                    # still inside the echo tail
        engine.stop()

    def test_say_before_start_does_not_block(self):
        na.TTS(na.AppState()).say("nobody is listening")


class ConversationFlow(unittest.TestCase):
    """Scripted run through assistant_loop with fake audio, speech-to-text and LLM."""

    def test_wake_confirm_cancel_llm_and_standby(self):
        import numpy as np
        na.shutdown.clear()
        self.addCleanup(na.shutdown.clear)
        script = ["hello there", "goodbye", "Nicola, kill chrome", "yes", "Nicola", "forget everything",
                  "no", "what is the capital of France", "open chrome", "Goodbye."]
        events = []

        class FakeTranscriber:
            def transcribe(self, audio):
                if not script:
                    na.shutdown.set()
                    return ""
                return script.pop(0)

        class FakeListener:
            def __init__(self, mic): pass
            def calibrate(self): pass
            def listen(self, awake): return np.zeros(1600, dtype="float32")

        class FakeMic:
            def start(self): pass
            def drain(self): pass

        class FakeTTS:
            def say(self, text, **kw): events.append(("SAY", text))
            def is_busy(self): return False
            def wait_idle(self, timeout=None): return True

        class FakeLLM:
            def __init__(self): self.history = []
            def respond(self, prompt):
                events.append(("LLM", prompt))
                return "Paris."

        with mock.patch.object(na, "Transcriber", FakeTranscriber), \
             mock.patch.object(na, "Listener", FakeListener), \
             mock.patch.object(na, "MicStream", FakeMic), \
             mock.patch.object(na, "tts", FakeTTS()), \
             mock.patch.object(na, "LLMClient", FakeLLM), \
             mock.patch.object(na, "close_application", side_effect=lambda a, force=False: f"closed {a} force={force}"), \
             mock.patch.object(na, "clear_memory", return_value="memory wiped"), \
             mock.patch.object(na, "launch_dynamic_application", side_effect=lambda a: f"opening {a}"), \
             mock.patch.object(na, "load_custom_aliases", return_value={}):
            na.assistant_loop()

        said = [text for kind, text in events if kind == "SAY"][2:]     # skip the two start-up lines
        self.assertEqual(said, [
            "This will force close chrome and unsaved work may be lost. Say yes to continue.",
            "closed chrome force=True",            # only after "yes"
            "Yes?",                                # her name on its own
            "This will erase everything I remember about you. Say yes to confirm.",
            "Okay, cancelled.",                    # "no" is only two letters but must still count
            "opening chrome",
            "Standby mode enabled. Say my name to wake me.",
        ])
        self.assertIn(("LLM", "what is the capital of France"), events)
        # "hello there" and "goodbye" were said in standby: no reaction at all
        self.assertNotIn("Standby mode enabled. Say my name to wake me.", said[:1])


if __name__ == "__main__":
    unittest.main()
