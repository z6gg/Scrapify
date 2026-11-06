"""
how alike an arabic name and a latin spelling of it are, learned from anghami's names in both scripts
(bench/train_model.py). a small edit model: each arabic letter is written as nothing, one latin letter
or two ("sh", "ou", "ll"), latin letters can be added (the short vowels arabic doesn't write), and how
likely each of those is was counted from thousands of real pairs ("Galo Tara" = قالوا ترى,
"Mohammed Abdu" = محمد عبده). the score is how much likelier the pair is under that model than as two
unrelated names, per letter: around 1 or more for a spelling of the same words, 0 or below for others.

pure python and a few thousand numbers, so it runs anywhere scrapify does
"""

import math
import re
import unicodedata
from functools import lru_cache

try:
    from matchmodel_weights import translit as _weights
except ImportError:  # not trained yet: the matcher works without it
    _weights = None

_arabic_fold = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي",
    "ک": "ك", "ی": "ي", "ـ": None, "پ": "ب", "چ": "ج", "ڤ": "ف", "گ": "ك", "ﻻ": "لا",
})
_arabic_letters = re.compile("[ءابتثجحخدذرزسشصضطظعغفقكلمنهوي ]")
max_len = 48


def arabic_form(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch))
    text = text.translate(_arabic_fold)
    text = "".join(ch if _arabic_letters.match(ch) else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def latin_form(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub("['’ʼ`´]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def available():
    return _weights is not None


def _tables():
    w = _weights
    return w["sub"], w["sub2"], w["dele"], w["ins"], w["null_ar"], w["null_la"], w["floor"]


@lru_cache(maxsize=200000)
def similarity(arabic, latin):
    """per letter log-likelihood ratio of the best alignment, about -2 to 2. None without a model
    or if either side has no letters"""
    if _weights is None:
        return None
    a, l = arabic_form(arabic), latin_form(latin)
    if not a or not l:
        return None
    a, l = a[:max_len], l[:max_len]
    sub, sub2, dele, ins, null_ar, null_la, floor = _tables()
    m, n = len(a), len(l)
    neg = -1e18
    prev = [neg] * (n + 1)
    prev[0] = 0.0
    for j in range(1, n + 1):
        prev[j] = prev[j - 1] + ins.get(l[j - 1], floor)
    prev2 = None
    for i in range(1, m + 1):
        ca = a[i - 1]
        cur = [neg] * (n + 1)
        d = dele.get(ca, floor)
        cur[0] = prev[0] + d
        sub_a, sub2_a = sub.get(ca, {}), sub2.get(ca, {})
        for j in range(1, n + 1):
            cl = l[j - 1]
            best = prev[j] + d
            v = cur[j - 1] + ins.get(cl, floor)
            if v > best:
                best = v
            v = prev[j - 1] + sub_a.get(cl, floor)
            if v > best:
                best = v
            if j >= 2 and sub2_a:
                p2 = sub2_a.get(l[j - 2:j])
                if p2 is not None:
                    v = prev[j - 2] + p2
                    if v > best:
                        best = v
            cur[j] = best
        prev2, prev = prev, cur
    joint = prev[n]
    null = sum(null_ar.get(c, floor) for c in a) + sum(null_la.get(c, floor) for c in l)
    return (joint - null) / (m + n)
