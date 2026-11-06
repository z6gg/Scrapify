"""Trains Scrapify's two small matching models from the bench recordings and writes them to
scrapify_ui/matchmodel_weights.py. Offline, a few minutes, numpy only (not needed by the app).

    python bench/train_model.py          train both, report, write the weights
    python bench/train_model.py --dry    train and report, write nothing

1. translit: how arabic letters get written in latin letters, learned from Anghami's names in both
   scripts (every song in the recordings carries them). A hard-EM trained edit model: an arabic letter
   becomes nothing, one latin letter or two; latin letters can be added. Its score per pair is a
   log-likelihood ratio against "two unrelated names".
2. judge: a logistic regression over the matcher's own title and artist scores, the translit scores,
   the length gap and a few flags, for pairs in the grey zone the rules can't settle.

Trained on the tuning recording only; the report is on the held-out one. Run bench.py check after.
"""
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict

here = os.path.dirname(os.path.abspath(__file__))
ui_dir = os.path.dirname(here)
sys.path.insert(0, ui_dir)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

import matcher
import translit

random.seed(7)
weights_file = os.path.join(ui_dir, "matchmodel_weights.py")


# ----------------------------------------------------------------- data

def load(name):
    return json.load(open(os.path.join(here, f"recording_{name}.json"), encoding="utf-8"))


def anghami_songs(rec):
    seen, out = set(), []
    for bucket in ("anghami_search", "anghami_artist"):
        for songs in rec[bucket].values():
            for s in songs:
                if s.get("id") in seen:
                    continue
                seen.add(s.get("id"))
                out.append(s)
    return out


def script_pairs(songs):
    """(arabic, latin) title and artist spellings of the same song or artist"""
    pairs = set()
    for s in songs:
        al = s.get("aliases") or {}
        for field, alias_key in (("title", "titles"), ("artist", "artists")):
            for alias in al.get(alias_key) or []:
                own = s[field]
                if matcher.has_arabic(alias) == matcher.has_arabic(own):
                    continue
                ar, la = (alias, own) if matcher.has_arabic(alias) else (own, alias)
                a, l = translit.arabic_form(ar), translit.latin_form(la)
                if a and l and len(a) <= translit.max_len and len(l) <= translit.max_len:
                    pairs.add((a, l))
    return sorted(pairs)


# ----------------------------------------------------------------- 1. translit, hard EM

def viterbi_path(a, l, tables):
    sub, sub2, dele, ins, floor = tables
    m, n = len(a), len(l)
    neg = -1e18
    score = [[neg] * (n + 1) for _ in range(m + 1)]
    back = [[None] * (n + 1) for _ in range(m + 1)]
    score[0][0] = 0.0
    for i in range(m + 1):
        for j in range(n + 1):
            if i == 0 and j == 0:
                continue
            best, op = neg, None
            if i:
                v = score[i - 1][j] + dele.get(a[i - 1], floor)
                if v > best:
                    best, op = v, ("d", a[i - 1])
            if j:
                v = score[i][j - 1] + ins.get(l[j - 1], floor)
                if v > best:
                    best, op = v, ("i", l[j - 1])
            if i and j:
                v = score[i - 1][j - 1] + sub.get(a[i - 1], {}).get(l[j - 1], floor)
                if v > best:
                    best, op = v, ("s", a[i - 1], l[j - 1])
            if i and j >= 2:
                p = sub2.get(a[i - 1], {}).get(l[j - 2:j])
                if p is not None:
                    v = score[i - 1][j - 2] + p
                    if v > best:
                        best, op = v, ("s2", a[i - 1], l[j - 2:j])
            score[i][j], back[i][j] = best, op
    ops, i, j = [], m, n
    while i or j:
        op = back[i][j]
        ops.append(op)
        if op[0] == "d":
            i -= 1
        elif op[0] == "i":
            j -= 1
        elif op[0] == "s":
            i, j = i - 1, j - 1
        else:
            i, j = i - 1, j - 2
    return score[m][n], ops


def tables_from_counts(counts, alphabet_ar, alphabet_la, bigrams, smooth=0.5):
    # one joint distribution over every operation, add-smooth for the ones never seen
    total = sum(counts.values())
    n_ops = len(alphabet_ar) * (2 + len(alphabet_la) + len(bigrams)) + len(alphabet_la)
    denom = total + smooth * n_ops
    lp = lambda c: math.log((c + smooth) / denom)
    sub = {a: {l: lp(counts.get(("s", a, l), 0)) for l in alphabet_la} for a in alphabet_ar}
    sub2 = {a: {b: lp(counts[("s2", a, b)]) for b in bigrams if counts.get(("s2", a, b), 0) >= 3}
            for a in alphabet_ar}
    dele = {a: lp(counts.get(("d", a), 0)) for a in alphabet_ar}
    ins = {l: lp(counts.get(("i", l), 0)) for l in alphabet_la}
    return sub, sub2, dele, ins, math.log(smooth / denom)


def train_translit(pairs, rounds=6):
    alphabet_ar = sorted({c for a, _ in pairs for c in a})
    alphabet_la = sorted({c for _, l in pairs for c in l})
    bigram_counts = Counter(l[k:k + 2] for _, l in pairs for k in range(len(l) - 1))
    bigrams = [b for b, c in bigram_counts.most_common(400) if " " not in b]
    # a weak start: every substitution alike, a bit likelier than deleting or inserting
    counts = Counter()
    for a in alphabet_ar:
        counts[("d", a)] = 5
        for l in alphabet_la:
            counts[("s", a, l)] = 2
        for b in bigrams:
            counts[("s2", a, b)] = 3
    for l in alphabet_la:
        counts[("i", l)] = 5
    counts[("s", " ", " ")] = 500
    tables = tables_from_counts(counts, alphabet_ar, alphabet_la, bigrams)
    kept = pairs
    for r in range(rounds):
        counts = Counter()
        scored = []
        for a, l in kept:
            s, ops = viterbi_path(a, l, tables)
            scored.append((s / (len(a) + len(l)), a, l))
            counts.update(ops)
        tables = tables_from_counts(counts, alphabet_ar, alphabet_la, bigrams)
        if r >= 1:
            # translations ("Love Humiliation" = يا من هواه) fit no spelling: the worst tenth is left out
            scored.sort()
            cut = len(scored) // 10
            kept = [(a, l) for _s, a, l in scored[cut:]] if r == 1 else kept
        print(f"  translit round {r + 1}: mean per-letter score {sum(s for s, _a, _l in scored) / len(scored):.3f}"
              f" on {len(scored)} pairs")
    null_ar = Counter(c for a, _ in kept for c in a)
    null_la = Counter(c for _, l in kept for c in l)
    tot_ar, tot_la = sum(null_ar.values()), sum(null_la.values())
    sub, sub2, dele, ins, floor = tables
    return {"sub": sub, "sub2": {a: v for a, v in sub2.items() if v}, "dele": dele, "ins": ins,
            "null_ar": {c: math.log(n / tot_ar) for c, n in null_ar.items()},
            "null_la": {c: math.log(n / tot_la) for c, n in null_la.items()}, "floor": floor}


def round_tables(w, digits=3):
    r = lambda x: round(x, digits)
    return {"sub": {a: {l: r(v) for l, v in d.items() if v > w["floor"] + 0.5} for a, d in w["sub"].items()},
            "sub2": {a: {b: r(v) for b, v in d.items()} for a, d in w["sub2"].items()},
            "dele": {a: r(v) for a, v in w["dele"].items()}, "ins": {l: r(v) for l, v in w["ins"].items()},
            "null_ar": {c: r(v) for c, v in w["null_ar"].items()}, "null_la": {c: r(v) for c, v in w["null_la"].items()},
            "floor": r(w["floor"])}


def auc(pos, neg):
    pos, neg = sorted(pos), sorted(neg)
    wins, j = 0, 0
    for p in pos:
        while j < len(neg) and neg[j] < p:
            j += 1
        wins += j
    return wins / (len(pos) * len(neg))


# ----------------------------------------------------------------- 2. judge

features = matcher.judge_features  # the app's own, so training and use can't drift apart


feature_names = ["title", "artist", "title_translit", "title_cross", "artist_translit", "artist_cross",
                 "same_length", "gap", "gap_unknown", "title_overlap", "version_differs"]


def other_script(song):
    """the song as the other platform may list it: its names in the other script, when it has them"""
    al = song.get("aliases") or {}
    titles, artists = al.get("titles") or [], al.get("artists") or []
    if not titles:
        return None
    return {"title": titles[0], "artist": artists[0] if artists else song["artist"],
            "duration": song.get("duration")}


def judge_examples(rec):
    songs = [s for s in anghami_songs(rec) if s.get("duration")]
    by_artist, by_title = defaultdict(list), defaultdict(list)
    for s in songs:
        by_artist[matcher.normalize(s["artist"])].append(s)
        by_title[matcher.skeleton(s["title"]).replace(" ", "")].append(s)
    X, y = [], []
    plain = lambda s: {k: s[k] for k in ("title", "artist", "duration")}
    for s in songs:
        twin = other_script(s)
        if twin is None:
            continue
        jitter = random.choice([0.0, 0.4, 1.1, 2.0])
        # the same song in the other script, with its length and without
        for dur in (s["duration"] + jitter, None):
            X.append(features(plain(s), dict(twin, duration=dur))); y.append(1)
        # another song by the same artist, in the other script where it has one: as long, its own
        # length, or no length known
        others = [o for o in by_artist[matcher.normalize(s["artist"])] if o["id"] != s["id"]
                  and matcher.fuzz.token_set_ratio(matcher.normalize(o["title"]), matcher.normalize(s["title"])) < 70]
        for o in random.sample(others, min(3, len(others))):
            o2 = other_script(o) or o
            for dur in (s["duration"] + random.uniform(0, 2), o["duration"], None):
                X.append(features(plain(s), dict(o2, duration=dur))); y.append(0)
        # the same title by someone else (a cover, or another song of that name)
        namesakes = [o for o in by_title[matcher.skeleton(s["title"]).replace(" ", "")]
                     if not matcher.is_same_artist(o["artist"], s["artist"])]
        for o in random.sample(namesakes, min(2, len(namesakes))):
            o2 = other_script(o) or o
            for dur in (o["duration"], None):
                X.append(features(plain(s), dict(o2, duration=dur))); y.append(0)
    return np.array(X), np.array(y)


def train_logreg(X, y, l2=1e-3, steps=3000, lr=0.5):
    Xb = np.hstack([X, np.ones((len(X), 1))])
    w = np.zeros(Xb.shape[1])
    pos_w = (len(y) - y.sum()) / max(1, y.sum())  # balance the classes
    sw = np.where(y == 1, pos_w, 1.0)
    for _ in range(steps):
        p = 1 / (1 + np.exp(-Xb @ w))
        grad = Xb.T @ ((p - y) * sw) / sw.sum() + l2 * np.r_[w[:-1], 0]
        w -= lr * grad
    return w


def main():
    dry = "--dry" in sys.argv
    tuning, holdout = load("tuning"), load("holdout")
    pairs = script_pairs(anghami_songs(tuning))
    print(f"translit: {len(pairs)} spelling pairs from the tuning recording")
    w = round_tables(train_translit(pairs))
    translit._weights = w
    translit.similarity.cache_clear()
    test = script_pairs(anghami_songs(holdout))
    test = [p for p in test if p not in set(pairs)]
    shuffled = [(a, test[(k + 17) % len(test)][1]) for k, (a, _l) in enumerate(test)]
    pos = [translit.similarity(a, l) for a, l in test]
    neg = [translit.similarity(a, l) for a, l in shuffled]
    print(f"translit on {len(test)} held-out pairs: AUC same vs shuffled {auc(pos, neg):.3f}, "
          f"median same {sorted(pos)[len(pos) // 2]:.2f}, median other {sorted(neg)[len(neg) // 2]:.2f}")
    for ar, la in (("محمد عبده", "Mohammed Abdu"), ("قالوا نسيته", "Qalou Naseto"), ("قالوا ترى", "Galo Tara"),
                   ("الدمعة", "Al Waad Qedam"), ("ولا على بالك", "Wala Ala Balo")):
        print(f"    {ar} / {la}: {translit.similarity(ar, la):.2f}")

    X, y = judge_examples(tuning)
    Xh, yh = judge_examples(holdout)
    coef = train_logreg(X, y)
    ph = 1 / (1 + np.exp(-np.hstack([Xh, np.ones((len(Xh), 1))]) @ coef))
    print(f"judge: {len(y)} training pairs ({int(y.sum())} same), held-out AUC "
          f"{auc(ph[yh == 1].tolist(), ph[yh == 0].tolist()):.3f}")
    for name, c in zip(feature_names + ["bias"], coef):
        print(f"    {name:16s} {c:+.3f}")
    if dry:
        return
    with open(weights_file, "w", encoding="utf-8") as f:
        f.write('"""written by bench/train_model.py, don\'t edit by hand"""\n\n')
        f.write(f"translit = {json.dumps(w, ensure_ascii=False, separators=(',', ':'))}\n\n")
        f.write(f"judge = {json.dumps({'features': feature_names, 'coef': [round(float(c), 4) for c in coef]})}\n")
    print(f"wrote {weights_file} ({os.path.getsize(weights_file) // 1024} KB)")


if __name__ == "__main__":
    main()
