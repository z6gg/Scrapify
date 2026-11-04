"""matches songs across spotify and anghami by title, artist and length, in any script"""

import re
import unicodedata
from functools import lru_cache

from rapidfuzz import fuzz

# arabic letter variants catalogs mix up, persian letters, arabic and persian digits
_arabic_fold = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي",
    "ک": "ك", "ی": "ي", "ـ": None,
    "پ": "ب", "چ": "ج", "ڤ": "ف", "گ": "ك",
    **{chr(0x0660 + i): str(i) for i in range(10)},
    **{chr(0x06F0 + i): str(i) for i in range(10)},
})


def _fold(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.translate(_arabic_fold)


# arabic songs show up in arabic script on one platform and franco ("3met 3en") on the other.
# anghami's names in both scripts come in as track["aliases"], without those both spellings are cut
# down to consonants (skeleton): arabic doesn't write short vowels and franco spells them any way

_arabic_consonants = {
    "ب": "b", "ت": "t", "ث": "t", "ج": "J", "ح": "h", "خ": "K", "د": "d", "ذ": "z",
    "ر": "r", "ز": "z", "س": "s", "ش": "S", "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
    "غ": "J", "ف": "f", "ق": "k", "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h",
}
# spoken readings franco follows: qaf dropped ("Ana Wel Amar" = انا والقمر), tha/thal/zha as s/d/d
_arabic_consonants_soft = {**_arabic_consonants, "ق": "", "ث": "s", "ذ": "d", "ظ": "d"}
# gulf songs say qaf as g ("Galo Tara" = قالوا ترى)
_arabic_consonants_gulf = {**_arabic_consonants, "ق": "J"}
_arabic_readings = (False, True, "gulf")
_digraphs = (("kh", "K"), ("sh", "S"), ("ch", "S"), ("gh", "J"), ("th", "t"), ("dh", "d"), ("ph", "f"))
_latin_consonants = {
    "b": "b", "p": "b", "t": "t", "d": "d", "z": "z", "s": "s", "c": "k", "k": "k", "q": "k",
    "g": "J", "j": "J", "f": "f", "v": "f", "h": "h", "l": "l", "m": "m", "n": "n", "r": "r", "x": "ks",
}
_franco_digits = {"5": "K", "7": "h", "8": "k", "9": "s", "6": "t"}  # 2 and 3 are hamza/ain, dropped
_arabic_letter = re.compile("[؀-ۿ]")
_franco_word = re.compile(r"\b(?=\w*[a-z])(?=\w*[235789])\w+")


# cached because match_pairs compares every song with every other
@lru_cache(maxsize=65536)
def has_arabic(text):
    return bool(_arabic_letter.search(text or ""))


@lru_cache(maxsize=65536)
def _looks_franco(text):
    return bool(_franco_word.search(_fold(text or "")))


@lru_cache(maxsize=65536)
def skeleton(text, soft=False):
    # "Tamally Maak" and "تملي معاك" both give "tml mk". franco digits only count inside a word
    # so "7 Rings" keeps its 7
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub("['’ʼ`´]", "", text)  # "Ta'abt" is one word
    arabic_map = (_arabic_consonants_gulf if soft == "gulf" else
                  _arabic_consonants_soft if soft else _arabic_consonants)
    words = []
    for word in re.findall(r"\w+", text):
        franco = bool(re.search(r"[a-z]", word))
        for pair, symbol in _digraphs:  # text is lowercase, so the symbols can't clash
            word = word.replace(pair, symbol)
        out = []
        for ch in word:
            if ch in arabic_map:
                out.append(arabic_map[ch])
            elif ch in _latin_consonants:
                out.append(_latin_consonants[ch])
            elif ch in "KSJ":
                out.append(ch)
            elif ch.isdigit():
                out.append(_franco_digits.get(ch, "") if franco else ch)
        collapsed = "".join(c for i, c in enumerate(out) if i == 0 or c != out[i - 1])  # doubled letters say nothing
        if collapsed:
            words.append(collapsed)
    return " ".join(words)


@lru_cache(maxsize=65536)
def normalize(text):
    text = _fold(text)
    text = re.sub(r"\(.*?remaster.*?\)", "", text)
    text = re.sub(r"[\-–—]\s*(\d{4}\s*)?remaster(ed)?(\s*\d{4})?\b", "", text)
    text = re.sub(r"\b(\d{4}\s*)?(digitally\s*)?re-?master\w*(\s*\d{4})?\b", "", text)  # "Remasterisé" too
    text = re.sub(r"\bfeat\.?\b|\bft\.?\b", "feat", text)
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


_version_tag = re.compile(
    r"\s*[\-–—]\s*\(?(\d{4}\s*)?re-?master(ed)?(\s*\d{4})?\)?\s*$"
    r"|\s*\((\d{4}\s*)?re-?master(ed)?[^)]*\)\s*$",
    re.IGNORECASE,
)


def strip_version_tags(title):
    # only remaster tags go, live or slowed means another recording
    stripped = _version_tag.sub("", title).strip()
    return stripped or title


def track_key(title, artist):
    return f"{normalize(title)}||{normalize(artist)}"


# words that mark another recording of a song; both titles must carry the same set
_version_words = re.compile(
    r"\b(slowed|reverb|sped|nightcore|ultra|super|remix|rmx|live|acoustic|instrumental|karaoke|session|sessions|unplugged|concert|festival)\b")
# a different track made out of the song rather than another performance of it
_derivative_tags = frozenset({"remix", "rmx", "slowed", "reverb", "sped", "nightcore", "ultra", "super",
                              "karaoke", "instrumental"})
_arabic_version_words = re.compile("ريمكس|ريميكس|ريمكيس|لايف|مباشر|حفلة")
_arabic_remix = re.compile("ريمكس|ريميكس|ريمكيس")
_title_feat = re.compile(r"[(\[]?\s*\b(?:feat|ft|featuring)\b.*$", re.IGNORECASE)
_artist_split = re.compile(r"\s*(?:,|&|\band\b|\bfeat\b|\bft\b|\bx\b|\bwith\b|،)\s*|\sو\s", re.IGNORECASE)


def _version_tags(normalized_title):
    return frozenset(_version_words.findall(normalized_title))


def _adds_derivative_tags(target, candidate):
    # falling back from a remix to the original is fine, the other way round isn't
    added = _version_tags(normalize(candidate["title"])) - _version_tags(normalize(target["title"]))
    arabic_remix = bool(_arabic_remix.search(candidate["title"])) and not _arabic_remix.search(target["title"])
    return bool(added & _derivative_tags) or arabic_remix


# same artist, same length to the second and every title word present counts as a title match
_same_length_title_score = 90


def match_score(a, b, versions=True):
    """0 to 100. versions=False ignores live and remix tags"""
    # title and artist are scored apart and the lower wins, so a strong artist can't carry a
    # different title ("Cinnamon Girl" vs "Sad Girl"). a blank artist scores on the title alone
    a_title, b_title = normalize(a["title"]), normalize(b["title"])
    arabic_song = _is_arabic_song(a) or _is_arabic_song(b)
    title_score = _names_score(_names(a, "title"), _names(b, "title"),
                               lambda x, y: _title_pair_score(x, y, arabic_song))
    if not normalize(a["artist"]) or not normalize(b["artist"]):
        if versions and _version_tags(a_title) != _version_tags(b_title):
            title_score = min(title_score, 60)
        return title_score
    artist_score = _names_score(_names(a, "artist"), _names(b, "artist"),
                                lambda x, y: _artist_pair_score(x, y, arabic_song))
    if title_score < _same_length_title_score and artist_score >= 87 and _same_length(a, b) \
            and (_title_overlap(a, b) or _close_spelling_match(a, b)):
        title_score = _same_length_title_score
    elif title_score >= 87 and _far_apart(a, b) and not _same_title(a, b):
        title_score = 80  # one title only contains the other and they run far apart ("Habibi" / "Bes Ellak Habibi")
    if versions and _version_tags(a_title) != _version_tags(b_title):
        title_score = min(title_score, 60)
    return min(title_score, artist_score)


def is_same_artist(x, y):
    return bool(x and y) and _artist_pair_score(x, y, True) >= 90


def duration_gap(a, b):
    x, y = a.get("duration"), b.get("duration")
    return abs(x - y) if x and y else None


def _same_length(a, b):
    gap = duration_gap(a, b)
    return gap is not None and gap <= 2.5  # anghami's copy is often a second or two off


def _far_apart(a, b):
    gap = duration_gap(a, b)
    return gap is not None and gap > 10


def _same_title(a, b):
    # same once feat credits and version words are left out, not just one inside the other
    for x in _names(a, "title"):
        for y in _names(b, "title"):
            if not x or not y:
                continue
            nx, ny = normalize(_overlap_text(x)), normalize(_overlap_text(y))
            if nx and fuzz.ratio(nx, ny) >= 90:
                return True
            if _skeleton_ratio(_overlap_text(x), _overlap_text(y)) == 100:
                return True
    return False


def _skeleton_words(text, soft=False):
    # {skeleton word: untrimmed length}. the article goes so "Al Sobh", "Elsobh" and "الصبح" all give
    # "sbh". any word starting with l loses it on both sides alike, hence the untrimmed length
    out = {}
    for w in skeleton(text, soft).split():
        if w == "l":
            continue
        key = w[1:] if w.startswith("l") and len(w) > 2 else w
        out[key] = max(out.get(key, 0), len(w))
    return out


def _title_overlap(a, b):
    # every word of the shorter title is in the longer one by consonants: "Seta Sobeh" in
    # "Seta Al Sobh". whole words, so "Habayeb" (hbb) isn't "Habibty" (hbbt)
    own = {w for t in (a, b) for n in _names(t, "artist") for w in _skeleton_words(n)}  # artist names in titles
    for x in _names(a, "title"):
        for y in _names(b, "title"):
            if not x or not y:
                continue
            sx, sy = _overlap_text(x), _overlap_text(y)
            for soft_x in (_arabic_readings if has_arabic(sx) else (False,)):
                for soft_y in (_arabic_readings if has_arabic(sy) else (False,)):
                    wx, wy = _skeleton_words(sx, soft_x), _skeleton_words(sy, soft_y)
                    wx = {k: n for k, n in wx.items() if k not in own} or wx
                    wy = {k: n for k, n in wy.items() if k not in own} or wy
                    short, long_ = (wx, wy) if sum(wx.values()) <= sum(wy.values()) else (wy, wx)
                    # "Hawina Hawina" is only {"hn"}, which half of all titles contain
                    substantial = max(short.values(), default=0) >= 3 or (len(short) >= 2 and sum(short.values()) >= 3)
                    if substantial and _words_within(short, long_):
                        return True
    return False


def _close_spelling_match(a, b):
    # same artist, same length to the second, and nearly the same letters: "Akhedni Maak" and
    # "Akendni Maak" (0.3s apart) cut down to different consonants, "El Layaly" and "Ellayali" to
    # almost none. thin titles get a looser bar, "Wala Ala Balo" vs "Balak" (87) must stay apart.
    # latin spellings only: in arabic one letter is another word (باله / بالك). thin titles within
    # a second may also agree on their vowel shape, in any script ("Eyoon Alail" / "Oyoun Al Liel")
    gap = duration_gap(a, b)
    if gap is None or gap > 1.5:
        return False
    for x in _names(a, "title"):
        for y in _names(b, "title"):
            if not x or not y:
                continue
            thin = min(len(skeleton(x).replace(" ", "")), len(skeleton(y).replace(" ", ""))) < 4
            if thin and gap <= 1.0:
                shape = _vowel_shape(_overlap_text(x))
                if len(shape) >= 5 and shape == _vowel_shape(_overlap_text(y)):
                    return True
            if has_arabic(x) or has_arabic(y):
                continue
            nx = normalize(_overlap_text(x)).replace(" ", "")
            ny = normalize(_overlap_text(y)).replace(" ", "")
            if min(len(nx), len(ny)) < 4:
                continue
            ratio = fuzz.ratio(nx, ny)
            if (thin and ratio >= 80) or (gap <= 1.0 and min(len(nx), len(ny)) >= 6 and ratio >= 90):
                return True
    return False


def _words_within(short, long_):
    # every short word is in the long title. a long word may differ by a letter, franco writes a
    # dropped qaf as 2 or leaves it out where the gulf says g ("Bnftere2" / "Bneftreg" = بنفترق)
    return all(w in long_ or (len(w) >= 5 and any(len(v) >= 5 and fuzz.ratio(w, v) >= 90 for v in long_))
               for w in short)


_shape_vowels = set("aeiouyw23") | set("اويىعءؤئةأإآ")


@lru_cache(maxsize=65536)
def _vowel_shape(text):
    # consonants with every vowel run as one V, in either script: "Eyoon Alail", "Oyoun Al Liel" and
    # "عيون الليل" all give VnVlVl. for titles too thin for skeletons
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w]", "", text)
    for pair, symbol in _digraphs:
        text = text.replace(pair, symbol)
    out = []
    for ch in text:
        if ch in _shape_vowels:
            out.append("V")
        elif ch in _arabic_consonants:
            out.append(_arabic_consonants[ch])
        elif ch in _latin_consonants:
            out.append(_latin_consonants[ch])
        elif ch in "KSJ":
            out.append(ch)
        elif ch.isdigit():
            out.append(_franco_digits.get(ch, ch))
    return "".join(c for i, c in enumerate(out) if i == 0 or c != out[i - 1])


def _overlap_text(title):
    title = _title_feat.sub("", title) or title
    title = _arabic_version_words.sub(" ", title)
    return _version_words.sub(" ", _fold(title))


def _names(track, field):
    return [track[field]] + list((track.get("aliases") or {}).get(f"{field}s") or [])


def _is_arabic_song(track):
    return any(has_arabic(n) for field in ("title", "artist") for n in _names(track, field))


def _names_score(a_names, b_names, pair_score):
    return max(pair_score(x, y) for x in a_names for y in b_names if x and y) if any(a_names) and any(b_names) else 0


def _cross_script(x, y, arabic_song=False):
    # franco spellings of one word vary too much for plain fuzzy matching
    return arabic_song or has_arabic(x) != has_arabic(y) or _looks_franco(x) or _looks_franco(y)


def _skeleton_ratio(x, y):
    xs = {skeleton(x, r) for r in _arabic_readings} if has_arabic(x) else {skeleton(x)}
    ys = {skeleton(y, r) for r in _arabic_readings} if has_arabic(y) else {skeleton(y)}
    return max(_one_skeleton_ratio(x, y, a.replace(" ", ""), b.replace(" ", "")) for a in xs for b in ys)


def _one_skeleton_ratio(x, y, sx, sy):
    if len(sx) < 3 or len(sy) < 3:
        # too short to fuzzy match ("hb" is half the love songs): exact only, similar length
        # ("Habibi" is حبيبي not حبي), and two latin spellings must agree ("Lelly" vs "Wely")
        lx, ly = len(re.sub(r"\W", "", x)), len(re.sub(r"\W", "", y))
        if not (sx and sx == sy and min(lx, ly) >= 0.6 * max(lx, ly)):
            return 0
        if not has_arabic(x) and not has_arabic(y) and fuzz.ratio(normalize(x), normalize(y)) < 75:
            return 0
        return 100
    if sx == sy:
        return 100
    if min(len(sx), len(sy)) < 8:
        # a letter still differing in a short skeleton is usually another word ("Balo"/"Balak")
        return min(fuzz.ratio(sx, sy), 80)
    return fuzz.ratio(sx, sy)


def _spelling_score(x, y, arabic_song):
    # for an arabic song only exact or word subset spellings count here, near misses are left to
    # the skeleton ("Wala Ala Balo" vs "Wala Ala Balak")
    score = fuzz.token_set_ratio(normalize(x), normalize(y))
    return score if score == 100 or not arabic_song else min(score, 80)


def _title_pair_score(x, y, arabic_song=False):
    score = _spelling_score(x, y, arabic_song)
    if _cross_script(x, y, arabic_song):
        score = max(score, _skeleton_ratio(_title_feat.sub("", x) or x, _title_feat.sub("", y) or y))
    return score


def _artist_pair_score(x, y, arabic_song=False):
    score = _spelling_score(x, y, arabic_song)
    if _cross_script(x, y, arabic_song):
        # per credited artist, so a duet still matches one of its singers
        parts_x = [x] + [p for p in _artist_split.split(x) if p.strip()]
        parts_y = [y] + [p for p in _artist_split.split(y) if p.strip()]
        score = max(score, max(_skeleton_ratio(px, py) for px in parts_x for py in parts_y))
    return score


def best_match(track, candidates, threshold=87):
    best, best_score = closest_match(track, candidates)
    return best if best_score >= threshold else None


def _tie_break(track, candidate):
    # closest length first (same recording is within a second or two), then spelling
    a, b = track.get("duration"), candidate.get("duration")
    gap = abs(a - b) if a and b else 0
    spelling = fuzz.ratio(normalize(f"{track['title']} {track['artist']}"),
                          normalize(f"{candidate['title']} {candidate['artist']}"))
    return (-gap, spelling)


def closest_match(track, candidates, versions=True):
    best, best_key = None, None
    for c in candidates:
        key = (match_score(track, c, versions), _tie_break(track, c))
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best, (best_key[0] if best_key else 0)


def _length_rank(target, candidate):
    # 0 same length (3s) or unknown, 1 a bit off, 2 clearly another cut
    gap = duration_gap(target, candidate)
    return 0 if gap is None or gap <= 3 else 1 if gap <= 10 else 2


def good_length(target, candidate):
    return _length_rank(target, candidate) == 0


def _best_passing(target, candidates, threshold, versions):
    # over the threshold the right length beats a higher score
    best, best_key = None, None
    for c in candidates:
        score = match_score(target, c, versions)
        if score < threshold:
            continue
        if not versions and _adds_derivative_tags(target, c):
            continue
        key = (-_length_rank(target, c), score, _tie_break(target, c))
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best, (best_key[1] if best_key else 0)


def pick_best(target, candidates, threshold=87, exact_version=False):
    """(hit, score, same_version). the same version, else unless exact_version the closest other"""
    best, best_score = _best_passing(target, candidates, threshold, True)
    if best is not None:
        return best, best_score, True
    if not exact_version:
        loose, loose_score = _best_passing(target, candidates, threshold, False)
        if loose is not None:
            return loose, loose_score, False
    return None, closest_match(target, candidates)[1], False


def find_in_catalog(search, title, artist, threshold=87, exact_version=False, log=None, service="",
                    aliases=None, artist_songs=None, duration=None):
    """search(query) -> [{title, artist, ...}]. tries "title artist", without a remaster tag, the
    other script names, then the title alone, stopping at the first same version hit of the right length"""
    # artist_songs(artist) is the last resort, also when the best hit is the wrong length: it catches
    # names spelled too differently for search, translated titles ("Ya Mn Hawah" is "Love Humiliation")
    # and cuts search ranks low
    target = {"title": title, "artist": artist, "aliases": aliases, "duration": duration}
    queries = [f"{title} {artist}"]
    stripped = strip_version_tags(title)
    if stripped != title:
        queries.append(f"{stripped} {artist}")
    alias_titles = [t for t in (aliases or {}).get("titles") or [] if has_arabic(t) != has_arabic(title)]
    alias_artists = [a for a in (aliases or {}).get("artists") or [] if has_arabic(a) != has_arabic(artist)]
    if alias_titles:
        queries.append(f"{alias_titles[0]} {alias_artists[0] if alias_artists else artist}")
    queries.append(title)

    seen, hit, same_version, best_score = [], None, False, 0
    for q in queries:
        seen += search(q)
        hit, score, same_version = pick_best(target, seen, threshold, exact_version)
        best_score = max(best_score, score)
        if hit is not None and same_version and good_length(target, hit):
            return hit
    if artist_songs and artist.strip():  # a song with no artist has no artist page to read
        seen += artist_songs(artist)
        hit, score, same_version = pick_best(target, seen, threshold, exact_version)
        best_score = max(best_score, score)
    if hit and same_version:
        return hit
    if hit:
        if log:
            log.info("%s: no exact version of '%s - %s', using '%s - %s'",
                     service, title, artist, hit["title"], hit["artist"])
        return hit
    if log:
        closest, _ = closest_match(target, seen)
        log.warning("%s search for '%s - %s' found no match%s", service, title, artist,
                    f" (closest '{closest['title']} - {closest['artist']}', score {best_score:.0f})" if closest else "")
    return None


class _NeedsLookup(Exception):
    def __init__(self, kind, key):
        super().__init__(kind, key)
        self.kind, self.key = kind, key


def find_many(tracks, search_many, threshold=87, exact_version=False, log=None, service="",
              artist_songs_many=None, rounds=1):
    """find_in_catalog for many songs with the lookups batched, picking exactly what one by one would.
    rounds > 1 searches again from scratch for whatever is still unmatched (anghami's results vary
    from call to call); only the last round logs misses"""
    results, pending = [None] * len(tracks), list(range(len(tracks)))
    for r in range(rounds):
        last = r == rounds - 1
        found = _find_many_once([tracks[i] for i in pending], search_many, threshold, exact_version,
                                log if last else None, service, artist_songs_many)
        for i, hit in zip(pending, found):
            results[i] = hit
        pending = [i for i in pending if results[i] is None]
        if not pending:
            break
    return results


def _find_many_once(tracks, search_many, threshold, exact_version, log, service, artist_songs_many):
    # each round every unresolved song runs against what's fetched so far and raises at the first
    # lookup it lacks. those get fetched together and the round repeats
    searched, artists = {}, {}

    def search(q):
        if q not in searched:
            raise _NeedsLookup("search", q)
        return searched[q]

    def artist_songs(name):
        if name not in artists:
            raise _NeedsLookup("artist", name)
        return artists[name]

    results, pending = [None] * len(tracks), list(range(len(tracks)))
    while pending:
        need_search, need_artist, still = [], [], []
        for i in pending:
            t = tracks[i]
            try:
                results[i] = find_in_catalog(search, t["title"], t["artist"], threshold, exact_version, log,
                                             service, t.get("aliases"),
                                             artist_songs if artist_songs_many else None, t.get("duration"))
            except _NeedsLookup as need:
                (need_search if need.kind == "search" else need_artist).append(need.key)
                still.append(i)
        # anything a batch didn't return counts as no results, so nothing waits forever
        if need_search:
            keys = list(dict.fromkeys(need_search))
            got = search_many(keys)
            searched.update({k: got.get(k) or [] for k in keys})
        if need_artist:
            keys = list(dict.fromkeys(need_artist))
            got = artist_songs_many(keys)
            artists.update({k: got.get(k) or [] for k in keys})
        pending = still
    return results


def match_pairs(source, dest, threshold=87, versions=True):
    """(pairs, source_left, dest_left), one to one so duplicates count right"""
    dest_by_key = {}
    for i, d in enumerate(dest):
        dest_by_key.setdefault(track_key(d["title"], d["artist"]), []).append(i)

    used_dest, pending, pairs_out = set(), [], []
    for s in source:
        free = dest_by_key.get(track_key(s["title"], s["artist"]))
        if free:
            di = free.pop(0)
            used_dest.add(di)
            pairs_out.append((s, dest[di]))
        else:
            pending.append(s)

    rest = [i for i in range(len(dest)) if i not in used_dest]
    scored = []
    for si, s in enumerate(pending):
        for di in rest:
            score = match_score(s, dest[di], versions)
            if score >= threshold:
                scored.append((score, _tie_break(s, dest[di]), si, di))
    scored.sort(key=lambda p: (p[0], p[1]), reverse=True)

    matched_src, matched_dest = set(), set()
    for _score, _tie, si, di in scored:
        if si not in matched_src and di not in matched_dest:
            matched_src.add(si)
            matched_dest.add(di)
            pairs_out.append((pending[si], dest[di]))

    return (pairs_out,
            [s for si, s in enumerate(pending) if si not in matched_src],
            [dest[di] for di in rest if di not in matched_dest])


def pair_tracks(source, dest, threshold=87):
    """(missing from dest, missing from source)"""
    _pairs, src_left, dest_left = match_pairs(source, dest, threshold)
    return src_left, dest_left
