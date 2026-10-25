"""Forced alignment of known lyrics to CTC posteriors (numpy only, no torch).

The posteriors come from facebook/wav2vec2-base-960h on the Demucs vocal stem (computed in
the ACE-Step venv by ace_worker.py analyze). This module runs a standard CTC Viterbi over
the blank-extended label sequence (blank, c1, blank, c2, ..., blank) and returns word start
and end times plus a per-word confidence: the mean posterior of the frames assigned to
that word's characters.

wav2vec2 was trained on read LibriSpeech speech, not singing, so scores on sung vocals are
a rough intelligibility proxy rather than a measurement.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import asdict, dataclass

import numpy as np

WORD_SEP = "|"
CONFIDENT = 0.5  # mean char posterior at or above this counts as "aligned with confidence"


@dataclass
class Word:
    word: str
    start: float
    end: float
    score: float


def normalise_words(text: str) -> list[str]:
    """Lyric text -> uppercase words in the wav2vec2-960h alphabet (A-Z and ')."""
    text = text.replace("’", "'").replace("-", " ")
    text = re.sub(r"\[[^\]]*\]", " ", text)  # structure tags such as [Chorus]
    words = []
    for raw in text.upper().split():
        w = re.sub(r"[^A-Z']", "", raw).strip("'")
        if w:
            words.append(w)
    return words


def tokens_for(words: list[str], vocab: dict[str, int]) -> tuple[list[int], list[int]]:
    """Character ids for the words joined by the word separator, plus each char's word index.

    Separator tokens carry word index -1. Characters missing from the vocab are dropped.
    """
    ids: list[int] = []
    owner: list[int] = []
    sep = vocab.get(WORD_SEP)
    for wi, word in enumerate(words):
        if wi and sep is not None:
            ids.append(sep)
            owner.append(-1)
        for ch in word:
            if ch in vocab:
                ids.append(vocab[ch])
                owner.append(wi)
    return ids, owner


def viterbi(logp: np.ndarray, tokens: list[int], blank: int = 0) -> np.ndarray | None:
    """Best CTC path. Returns the extended-state index per frame, or None if infeasible.

    State s is blank when s is even, and tokens[(s - 1) // 2] when s is odd.
    """
    T = logp.shape[0]
    L = len(tokens)
    if L == 0:
        return None
    S = 2 * L + 1
    ext = np.full(S, blank, dtype=np.int64)
    ext[1::2] = tokens
    # A skip s-2 -> s is allowed into a label that differs from the label two states back.
    can_skip = np.zeros(S, dtype=bool)
    can_skip[3::2] = ext[3::2] != ext[1:-2:2]
    neg = -np.inf
    alpha = np.full(S, neg)
    alpha[0] = logp[0, ext[0]]
    alpha[1] = logp[0, ext[1]]
    back = np.zeros((T, S), dtype=np.int8)
    for t in range(1, T):
        stay = alpha
        step = np.concatenate(([neg], alpha[:-1]))
        skip = np.where(can_skip, np.concatenate(([neg, neg], alpha[:-2])), neg)
        cand = np.stack([stay, step, skip])
        choice = np.argmax(cand, axis=0)
        alpha = cand[choice, np.arange(S)] + logp[t, ext]
        back[t] = choice
    ends = [S - 1, S - 2] if S > 1 else [S - 1]
    last = max(ends, key=lambda s: alpha[s])
    if not np.isfinite(alpha[last]):
        return None
    path = np.empty(T, dtype=np.int64)
    s = last
    for t in range(T - 1, -1, -1):
        path[t] = s
        s -= int(back[t, s])
    return path


def align(logp: np.ndarray, vocab: dict[str, int], text: str, frame_s: float = 0.02,
          blank_token: str = "<pad>") -> dict:
    """Align `text` to log-posteriors `logp` (T, V). Returns words, score and coverage."""
    words = normalise_words(text)
    tokens, owner = tokens_for(words, vocab)
    blank = vocab.get(blank_token, 0)
    path = viterbi(logp, tokens, blank)
    if path is None:
        return {"ok": False, "words": [], "score": 0.0, "confident_fraction": 0.0,
                "n_words": len(words), "reason": "alignment infeasible (audio too short?)"}
    probs = np.exp(logp)
    frames_for: dict[int, list[int]] = {}
    for t, s in enumerate(path):
        if s % 2 == 1:
            frames_for.setdefault((s - 1) // 2, []).append(t)
    out: list[Word] = []
    for wi, word in enumerate(words):
        char_idx = [j for j, o in enumerate(owner) if o == wi]
        frames = [t for j in char_idx for t in frames_for.get(j, [])]
        if not frames:
            out.append(Word(word, 0.0, 0.0, 0.0))
            continue
        tok = {t: tokens[j] for j in char_idx for t in frames_for.get(j, [])}
        score = float(np.mean([probs[t, tok[t]] for t in frames]))
        out.append(Word(word, round(min(frames) * frame_s, 3),
                        round((max(frames) + 1) * frame_s, 3), round(score, 4)))
    scores = np.array([w.score for w in out])
    return {
        "ok": True,
        "words": [asdict(w) for w in out],
        "score": round(float(scores.mean()), 4),
        "confident_fraction": round(float(np.mean(scores >= CONFIDENT)), 4),
        "n_words": len(out),
        "first_word_s": out[0].start if out else None,
        "last_word_s": out[-1].end if out else None,
    }


def monotonic(words: list[dict]) -> bool:
    return all(a["start"] <= b["start"] for a, b in itertools.pairwise(words))
