"""Offline tests for the real audio engines: Kokoro TTS (mlx-audio) and parakeet-mlx captions.

The heavy libraries are replaced with fakes shaped like the verified APIs
(mlx-audio 0.5.9 KokoroPipeline results, parakeet-mlx 0.5.3 AlignedResult), so these run
without models, Metal or the local-audio extra.
"""

import builtins
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import Beat, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.stages import captions, tts
from studio.stages.base import EngineUnavailable, StageContext, StageError

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)
RATE = 24_000


@pytest.fixture(autouse=True)
def _reset_models():
    tts.unload()
    captions.unload()
    yield
    tts.unload()
    captions.unload()


@pytest.mark.parametrize("module", [tts, captions])
def test_cache_cleanup_does_not_import_mlx(module, monkeypatch):
    monkeypatch.delitem(sys.modules, "mlx.core", raising=False)
    real_import = builtins.__import__

    def no_mlx(name, *args, **kwargs):
        assert not name.startswith("mlx"), "cleanup must not initialise MLX/Metal"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mlx)
    module.unload()
    calls = []
    monkeypatch.setitem(sys.modules, "mlx.core", SimpleNamespace(clear_cache=lambda: calls.append(1)))
    module.unload()
    assert calls == [1]


# ---------------------------------------------------------------- fakes


@dataclass
class Tok:  # misaki MToken: text, whitespace after it, start_ts/end_ts in seconds
    text: str
    whitespace: str = " "
    start_ts: float | None = None
    end_ts: float | None = None


@dataclass
class Res:  # KokoroPipeline.Result
    audio: object
    tokens: list = field(default_factory=list)


class FakePipeline:
    """Callable like KokoroPipeline: one chunk per sentence, 0.5 s of audio per word."""

    def __init__(self):
        self.model = SimpleNamespace(sample_rate=RATE)
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0):
        import numpy as np

        self.calls.append((text, voice, speed))
        for sentence in [s for s in text.split(". ") if s]:
            words = sentence.rstrip(".").split()
            toks = []
            for i, w in enumerate(words):
                toks.append(Tok(w, " " if i < len(words) - 1 else "", i * 0.5, i * 0.5 + 0.4))
            toks.append(Tok(".", "", len(words) * 0.5 - 0.1, len(words) * 0.5))
            yield Res(np.zeros(int(len(words) * 0.5 * RATE), dtype=np.float32), toks)


def make_job(tmp_path: Path, hook="Hi there. Go now.", beats=("One two three.",), cta=""):
    script = Script(title="T", hook=hook, beats=[Beat(text=b, visual_prompt="x") for b in beats],
                    cta=cta, description="", sources=[])
    workdir = tmp_path / "job"
    workdir.mkdir()
    return VideoJob(job_id="j", channel="gravity-what-if", format="short", topic="t",
                    workdir=workdir, script=script)


def ctx(tmp_path, **engines):
    return StageContext(settings=Settings(output_dir=tmp_path, brand_dir=DEFAULT_BRAND_DIR),
                        chat=MockChatClient(), engines=engines)


def brand():
    return load_brand("gravity-what-if", DEFAULT_BRAND_DIR)


# ---------------------------------------------------------------- Kokoro TTS


def test_brand_pack_voices_are_real_kokoro_voices():
    for path in sorted(DEFAULT_BRAND_DIR.glob("*.yaml")):
        b = load_brand(path.stem, DEFAULT_BRAND_DIR)
        tts.check_voice(b.voice.voice)  # raises on an unknown voice


def test_unknown_voice_is_a_stage_error(monkeypatch):
    with pytest.raises(StageError, match="unknown Kokoro"):
        tts.check_voice("am_nobody")
    tts.check_voice("af_heart,af_bella")  # blends are fine
    monkeypatch.setenv("STUDIO_KOKORO_ANY_VOICE", "1")
    tts.check_voice("am_nobody")


def test_segment_words_glues_punctuation_and_offsets_chunks():
    np = pytest.importorskip("numpy")
    results = [
        Res(np.zeros(RATE), [Tok("Hi", " ", 0.1, 0.3), Tok("there", "", 0.3, 0.8),
                             Tok("!", " ", 0.8, 0.9)]),
        Res(np.zeros(RATE // 2), [Tok("Go", "", 0.0, 0.2), Tok("?", "", None, None)]),
    ]
    words = tts._segment_words(results, RATE)
    assert [w["text"] for w in words] == ["Hi", "there!", "Go?"]
    assert words[1] == {"text": "there!", "start": 0.3, "end": 0.9}
    assert words[2]["start"] == 1.0  # second chunk starts after the first chunk's 1 s
    assert words[2]["end"] == 1.2  # untimed "?" does not move the end backwards past 1.2


def test_missing_mlx_audio_raises_engine_unavailable(monkeypatch):
    real_import = builtins.__import__

    def no_mlx(name, *a, **kw):
        if name.startswith("mlx_audio"):
            raise ImportError("No module named 'mlx_audio'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_mlx)
    with pytest.raises(EngineUnavailable, match="local-audio"):
        tts._load_kokoro()


def test_load_kokoro_uses_verified_api_once(monkeypatch):
    calls = []
    model = SimpleNamespace(sample_rate=RATE)

    class Pipe:
        def __init__(self, lang_code, model, repo_id):
            calls.append(("pipe", lang_code, repo_id))
            self.model = model

    utils = SimpleNamespace(load=lambda repo: calls.append(("load", repo)) or model)
    kp = SimpleNamespace(KokoroPipeline=Pipe)
    for name, mod in {"mlx_audio": SimpleNamespace(), "mlx_audio.tts": SimpleNamespace(),
                      "mlx_audio.tts.utils": utils, "mlx_audio.tts.models": SimpleNamespace(),
                      "mlx_audio.tts.models.kokoro": SimpleNamespace(),
                      "mlx_audio.tts.models.kokoro.pipeline": kp}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    p1, p2 = tts._load_kokoro(), tts._load_kokoro()
    assert p1 is p2 and p1.model is model
    assert calls == [("load", tts.KOKORO_MODEL), ("pipe", "a", tts.KOKORO_MODEL)]
    tts.unload()
    assert tts._kokoro is None


@needs_ffmpeg
def test_kokoro_run_writes_audio_timings_and_words(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    fake = FakePipeline()
    monkeypatch.setattr(tts, "_load_kokoro", lambda *_a: fake)
    job = make_job(tmp_path)
    job = tts.run(job, brand(), ctx(tmp_path, tts="kokoro"))
    wd = Path(job.workdir)
    timings = json.loads((wd / "timings.json").read_text())
    words = json.loads((wd / "tts_words.json").read_text())
    # hook = 2 sentences (2 + 2 words) = 2.0 s, gap 0.08 s, beat = 3 words = 1.5 s
    assert timings["engine"] == "kokoro"
    assert [s["kind"] for s in timings["segments"]] == ["hook", "beat"]
    assert timings["segments"][0]["end"] == 2.0 and timings["segments"][1]["start"] == 2.08
    assert timings["duration"] == pytest.approx(3.58, abs=0.01)
    assert [w["text"] for w in words] == ["Hi", "there.", "Go", "now.", "One", "two", "three."]
    assert words[2]["start"] == 1.0  # second pipeline chunk of the hook
    assert words[4]["start"] == 2.08  # beat words are offset by hook + gap
    assert fake.calls[0][1:] == (brand().voice.voice, tts.ENERGY_SPEED)  # 1.0 raised to 1.07
    # cached second run does not synthesise again
    tts.run(job, brand(), ctx(tmp_path, tts="kokoro"))
    assert len(fake.calls) == 2


@needs_ffmpeg
def test_mock_tts_removes_stale_kokoro_words(tmp_path):
    job = make_job(tmp_path)
    (Path(job.workdir) / "tts_words.json").write_text("[]")
    tts.run(job, brand(), ctx(tmp_path, tts="mock"))
    assert not (Path(job.workdir) / "tts_words.json").exists()


# ---------------------------------------------------------------- parakeet captions


def tok(text, start, dur=0.1):
    return SimpleNamespace(text=text, start=start, end=start + dur)


class FakeParakeet:
    def __init__(self, sentences):
        self.sentences = sentences
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append((path, kw))
        return SimpleNamespace(sentences=[SimpleNamespace(tokens=t) for t in self.sentences])


def test_parakeet_words_merges_subwords_and_bare_space_tokens(monkeypatch, tmp_path):
    # real token stream seen on parakeet-tdt-0.6b-v2: ' Now' ',' ' ' 'z' 'o' 'om' ...
    fake = FakeParakeet([
        [tok(" Now", 0.0), tok(",", 0.1), tok(" ", 0.2), tok("z", 0.3), tok("o", 0.4),
         tok("om", 0.5), tok(" in", 0.6), tok(".", 0.7)],
        [tok("Then", 1.0), tok(" mo", 1.1), tok("on", 1.2)],
    ])
    monkeypatch.setattr(captions, "_load_parakeet", lambda: fake)
    monkeypatch.setattr(captions, "media_duration", lambda p: 30.0)
    words = captions.parakeet_words(tmp_path / "a.wav")
    assert [w["text"] for w in words] == ["Now,", "zoom", "in.", "Then", "moon"]
    assert words[1] == {"text": "zoom", "start": 0.3, "end": 0.6}
    assert fake.calls[0][1] == {}  # short audio: one pass


def test_parakeet_chunks_long_audio(monkeypatch, tmp_path):
    fake = FakeParakeet([[tok(" hi", 0.0)]])
    monkeypatch.setattr(captions, "_load_parakeet", lambda: fake)
    monkeypatch.setattr(captions, "media_duration", lambda p: 600.0)
    captions.parakeet_words(tmp_path / "a.wav")
    assert fake.calls[0][1] == {"chunk_duration": captions.CHUNK_S, "overlap_duration": 15.0}


def test_missing_parakeet_raises_engine_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "parakeet_mlx", None)
    with pytest.raises(EngineUnavailable, match="parakeet-mlx"):
        captions._load_parakeet()


def test_load_parakeet_once_and_unload(monkeypatch):
    loaded = []
    mod = SimpleNamespace(from_pretrained=lambda repo: loaded.append(repo) or object())
    monkeypatch.setitem(sys.modules, "parakeet_mlx", mod)
    a, b = captions._load_parakeet(), captions._load_parakeet()
    assert a is b and loaded == [captions.PARAKEET_MODEL]
    captions.unload()
    assert captions._parakeet is None


@needs_ffmpeg
def test_parakeet_run_unloads_tts_first(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    job = tts.run(job, brand(), ctx(tmp_path, tts="mock"))
    tts._kokoro = object()  # pretend Kokoro is still resident
    seen = {}

    def fake_words(audio):
        seen["kokoro_at_asr"] = tts._kokoro
        return [{"text": "Hi", "start": 0.0, "end": 0.5}, {"text": "there.", "start": 0.5,
                                                            "end": 1.0}]

    monkeypatch.setattr(captions, "parakeet_words", fake_words)
    job = captions.run(job, brand(), ctx(tmp_path, captions="parakeet"))
    assert seen["kokoro_at_asr"] is None
    assert "HI" in Path(job.captions_path).read_text()


# ---------------------------------------------------------------- kokoro-timestamps captions


@needs_ffmpeg
def test_kokoro_timestamps_captions(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    monkeypatch.setattr(tts, "_load_kokoro", lambda *_a: FakePipeline())
    job = make_job(tmp_path)
    job = tts.run(job, brand(), ctx(tmp_path, tts="kokoro"))
    job = captions.run(job, brand(), ctx(tmp_path, captions="kokoro-timestamps"))
    words = json.loads((Path(job.workdir) / "words.json").read_text())
    assert words == json.loads((Path(job.workdir) / "tts_words.json").read_text())
    assert "THERE." in Path(job.captions_path).read_text()


@needs_ffmpeg
def test_kokoro_timestamps_needs_kokoro_tts(tmp_path):
    job = make_job(tmp_path)
    job = tts.run(job, brand(), ctx(tmp_path, tts="mock"))
    with pytest.raises(StageError, match="tts_words.json"):
        captions.run(job, brand(), ctx(tmp_path, captions="kokoro-timestamps"))


# ---------------------------------------------------------------- delivery (voice + speed)


def test_delivery_energy_floor_and_overrides(monkeypatch):
    monkeypatch.delenv("STUDIO_TTS_VOICE", raising=False)
    monkeypatch.delenv("STUDIO_TTS_SPEED", raising=False)
    b = brand()  # gravity-what-if: af_bella at 1.0
    assert tts.delivery(b) == ("af_bella", tts.ENERGY_SPEED)
    fast = load_brand("software-shortcut", DEFAULT_BRAND_DIR)  # af_heart at 1.1 stays 1.1
    assert tts.delivery(fast) == ("af_heart", 1.1)
    monkeypatch.setenv("STUDIO_TTS_SPEED", "0.95")
    monkeypatch.setenv("STUDIO_TTS_VOICE", "am_michael")
    assert tts.delivery(b) == ("am_michael", 0.95)  # an explicit override is not floored
    monkeypatch.setenv("STUDIO_TTS_SPEED", "fast")
    with pytest.raises(StageError, match="STUDIO_TTS_SPEED"):
        tts.delivery(b)


def test_voice_grades_cover_every_us_voice():
    assert set(tts.VOICE_GRADES) == set(tts.KOKORO_US_VOICES)
    assert tts.voice_grade("af_heart") == "A" and tts.voice_grade("am_michael") == "C+"
    assert tts.voice_grade("af_heart,am_onyx") == "D"  # a blend is as weak as its weakest
    assert all(v in tts.KOKORO_US_VOICES for v in tts.RECOMMENDED_VOICES)


@needs_ffmpeg
def test_kokoro_logs_a_weak_voice_and_speed_changes_the_cache_key(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    fake = FakePipeline()
    monkeypatch.setattr(tts, "_load_kokoro", lambda *_a: fake)
    monkeypatch.setenv("STUDIO_TTS_VOICE", "am_onyx")
    monkeypatch.delenv("STUDIO_TTS_SPEED", raising=False)
    job = tts.run(make_job(tmp_path), brand(), ctx(tmp_path, tts="kokoro"))
    assert any("grade D" in line for line in job.log)
    assert fake.calls[0][1:] == ("am_onyx", tts.ENERGY_SPEED)
    monkeypatch.setenv("STUDIO_TTS_SPEED", "1.15")
    tts.run(job, brand(), ctx(tmp_path, tts="kokoro"))
    assert fake.calls[-1][2] == 1.15  # new speed -> re-synthesised, not cached


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_delivery_rejects_nonfinite_speed(monkeypatch, raw):
    monkeypatch.setenv("STUDIO_TTS_SPEED", raw)
    with pytest.raises(StageError, match="finite"):
        tts.delivery(brand())


def test_kokoro_streams_chunks_without_retaining_audio(tmp_path, monkeypatch):
    import gc
    import wave
    import weakref

    np = pytest.importorskip("numpy")
    cleared = []

    class StreamingPipeline:
        model = SimpleNamespace(sample_rate=RATE)

        def __call__(self, text, **kwargs):
            for _ in range(12):
                result = Res(np.zeros(RATE // 2, dtype=np.float32),
                             [Tok("word", " ", 0.0, 0.4)])
                ref = weakref.ref(result.audio)
                yield result
                del result
                gc.collect()
                assert ref() is None, "TTS retained a previous chunk's audio"

    monkeypatch.setattr(tts, "_load_kokoro", lambda *_a: StreamingPipeline())
    monkeypatch.setattr(tts, "_clear_mlx_cache", lambda: cleared.append(1))
    path = tmp_path / "voice.wav"
    durations, words = tts._kokoro_tts([{"text": "one"}, {"text": "two"}], path,
                                        "af_heart", 1.0)
    assert durations == [6.0, 6.0]
    assert words[12]["start"] == 6.08
    assert len(cleared) >= 24
    with wave.open(str(path)) as wav:
        assert wav.getnframes() == round(12.08 * RATE)


def test_kokoro_failure_cleans_partial_output_and_cache(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    cleared = []

    class BrokenPipeline:
        model = SimpleNamespace(sample_rate=RATE)

        def __call__(self, text, **kwargs):
            yield Res(np.zeros(20, dtype=np.float32))
            raise RuntimeError("synthesis failed")

    monkeypatch.setattr(tts, "_load_kokoro", lambda *_a: BrokenPipeline())
    monkeypatch.setattr(tts, "_clear_mlx_cache", lambda: cleared.append(1))
    path = tmp_path / "voice.wav"
    path.write_bytes(b"old audio")
    with pytest.raises(StageError, match="synthesis failed"):
        tts._kokoro_tts([{"text": "one"}], path, "af_heart", 1.0)
    assert path.read_bytes() == b"old audio"
    assert not path.with_suffix(".tmp.wav").exists()
    assert len(cleared) >= 2


def test_parakeet_failure_still_clears_cache(tmp_path, monkeypatch):
    calls = []

    def broken(*args, **kwargs):
        raise RuntimeError("transcription failed")

    monkeypatch.setattr(captions, "_load_parakeet", lambda: SimpleNamespace(transcribe=broken))
    monkeypatch.setattr(captions, "media_duration", lambda _: 1.0)
    monkeypatch.setattr(captions, "_clear_mlx_cache", lambda: calls.append(1))
    with pytest.raises(RuntimeError, match="transcription failed"):
        captions.parakeet_words(tmp_path / "a.wav")
    assert calls == [1]


def test_british_voices_select_lang_b_and_us_stays_a():
    assert tts.lang_code("bf_emma") == "b" and tts.lang_code("af_heart") == "a"
    tts.check_voice("bf_emma")
    tts.check_voice("bm_george,bf_isabella")
    with pytest.raises(Exception, match="blend"):
        tts.check_voice("af_heart,bf_emma")
