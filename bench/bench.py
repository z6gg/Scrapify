"""Scrapify matcher test bench. Never writes to either account: it only
searches and reads artist pages.

    python bench.py check              everything below, offline, with a summary
    python bench.py cases              hand-written match / no-match cases
    python bench.py replay [-v]        real songs, both directions, from the recordings
    python bench.py falsematch [-v]    different songs by the same artist, same length
    python bench.py parity             batched lookups (what a sync runs) pick what one-by-one does
    python bench.py record             re-fetch the tuning set's searches (needs sessions)

The small learned model the matcher consults for unsure Arabic songs is retrained with
"python train_model.py" (writes matchmodel_weights.py); run "check" after that too.

Add --holdout to replay/falsematch/record for the held-out set: 45 artists
never used while tuning the matcher, so it shows how it does on songs it
was not fitted to. "check" runs both sets.

Run "check" after any change to matcher.py (or the alias/search
code in the clients). The recordings make it take seconds instead of the
~30 minutes and thousands of requests a live run needs; re-record only when
the clients start asking different search queries.

Answer key for replay, deliberately independent of how the matcher reads
titles: a pick is right when it is by the same artist, within 3 seconds of
the source song's length, and its title overlaps loosely in some script.
"Wrong" = picked something else while a song like that was in the results;
"miss" = picked nothing although one was there; "unverified" = picked a
song with no same-length match anywhere (usually the only cut available);
"absent" = nothing suitable to pick.
"""
import importlib
import itertools
import json
import os
import re
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
ui_dir = os.path.dirname(here)
sys.path.insert(0, ui_dir)
os.chdir(here)  # anything relative lands here, never in scrapify_ui
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # arabic titles on a cp1252 console

tuning_artists = [
    # Egyptian
    "Amr Diab", "Tamer Hosny", "Sherine", "Mohamed Hamaki", "Angham", "Mohamed Mounir", "Cairokee",
    "Wegz", "Marwan Pablo", "Ahmed Saad", "Hamza Namira", "Massar Egbari", "Hassan Shakosh", "Ruby",
    "Abdel Halim Hafez", "Umm Kulthum", "Mohamed Abdel Wahab",
    # Levant
    "Fairuz", "Elissa", "Nancy Ajram", "Wael Kfoury", "Ragheb Alama", "Majida El Roumi", "Haifa Wehbe",
    "Nassif Zeytoun", "Mashrou' Leila", "Adonis", "Elyanna", "Saint Levant", "Adham Nabulsi",
    # Gulf / Iraq
    "Kadim Al Sahir", "Hussain Al Jassmi", "Balqees", "Mohammed Abdu", "Rashed Al Majed",
    "Abdul Majeed Abdullah", "Majid Al Mohandis", "Aseel Hameem",
    # Maghreb
    "Saad Lamjarred", "Cheb Khaled", "Saber Rebai", "Latifa",
    # control (English)
    "Taylor Swift", "The Weeknd", "Lana Del Rey", "Coldplay", "Billie Eilish",
]
tuning_arabic_queries = ["عمرو دياب", "فيروز", "ام كلثوم", "اليسا", "كاظم الساهر", "محمد عبده", "شيرين",
                         "نانسي عجرم", "عبد الحليم حافظ", "حسين الجسمي", "انغام", "محمد منير"]
holdout_artists = [
    "Hany Shaker", "Mohamed Fouad", "Hakim", "Abyusif", "Marwan Moussa", "Mohamed Mohsen", "Ramy Sabry",
    "Bahaa Sultan", "Tamer Ashour", "Mohamed Ramadan", "Ahmed Kamel", "Shereen Abdel Wahab",
    "Ziad Rahbani", "Marcel Khalife", "Melhem Zein", "Assi El Hallani", "Ramy Ayach", "Myriam Fares",
    "Carole Samaha", "Najwa Karam", "Joseph Attieh", "Sabah Fakhri", "Mohammed Assaf", "Shabjdeed",
    "Nawal Al Kuwaitia", "Ahlam", "Abdallah Al Rowaished", "Rabeh Saqer", "Waleed Al Shami",
    "Mohammed Al Salem", "Shamma Hamdan", "Saif Nabeel", "Hatem Al Iraqi",
    "Cheb Mami", "Douzi", "Hatim Ammor", "Asma Lmnawar", "Cheb Hasni", "Souad Massi", "Ihab Amir",
    "Adele", "Ed Sheeran", "Dua Lipa", "Arctic Monkeys", "Bruno Mars",
]
holdout_arabic_queries = ["هاني شاكر", "نجوى كرم", "راشد الماجد", "احلام", "تامر عاشور", "مروان موسى",
                          "وائل جسار", "ديانا حداد", "عبدالله الرويشد", "محمد فؤاد", "صابر الرباعي", "حاتم العراقي"]
sets = {
    "tuning": ("recording_tuning.json", tuning_artists, tuning_arabic_queries),
    "holdout": ("recording_holdout.json", holdout_artists, holdout_arabic_queries),
}
per_artist = 8


def load_recording(name):
    try:
        return json.load(open(os.path.join(here, sets[name][0]), encoding="utf-8"))
    except (OSError, ValueError):
        return {"anghami_search": {}, "spotify_search": {}, "anghami_artist": {}}


def save_recording(name, data):
    json.dump(data, open(os.path.join(here, sets[name][0]), "w", encoding="utf-8"), ensure_ascii=False)


class Recorder:
    """Answers searches from the recording; with live clients (record mode)
    fetches and stores whatever the recording doesn't have yet."""

    def __init__(self, data, ag=None, sp=None):
        self.data, self.ag, self.sp = data, ag, sp
        self.unrecorded = 0

    def _get(self, bucket, key, live):
        store = self.data[bucket]
        if key not in store:
            if live is None:
                self.unrecorded += 1  # replay: a query the recording never made
                return []
            for _attempt in range(3):
                try:
                    store[key] = live(key)
                    break
                except Exception as e:
                    print(f"   ! {bucket} {key!r}: {e}")
                    time.sleep(3)
            else:
                store[key] = []
            time.sleep(0.15)
        if bucket.startswith("anghami"):
            # Re-apply the alias reader's artist-name rule to songs recorded
            # before it existed (see anghamiclient._with_aliases).
            import matcher
            for t in store[key]:
                al = t.get("aliases")
                if al and al.get("titles"):
                    moved = [k for k in al["titles"] if matcher.is_same_artist(k, t["artist"])]
                    if moved:
                        al["titles"] = [k for k in al["titles"] if k not in moved]
                        al["artists"] = list(dict.fromkeys((al.get("artists") or []) + moved))
        return store[key]

    def anghami_search(self, q):
        return self._get("anghami_search", q, self.ag._search_api if self.ag else None)

    def spotify_search(self, q):
        return self._get("spotify_search", q, self.sp._api_search_candidates if self.sp else None)

    def anghami_artist(self, artist):
        # per credited name, same as AnghamiClient.artist_songs_many
        songs = []
        for name in [p for p in re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?)\s*", artist) if p.strip()]:
            songs += self._get("anghami_artist", name, self.ag._read_artist_songs if self.ag else None)
        return songs


def sources(rec, artists, arabic_queries):
    """Source songs for each direction: each artist's top songs on one
    platform, plus Spotify songs listed in Arabic script."""
    from matcher import normalize
    s2a, a2s, seen_s, seen_a = [], [], set(), set()
    for artist in artists:
        n = 0
        for h in rec.spotify_search(artist):
            if normalize(artist) in normalize(h["artist"]) and h["uri"] not in seen_s and h.get("duration"):
                seen_s.add(h["uri"]); s2a.append(h); n += 1
                if n >= per_artist:
                    break
        n = 0
        for t in rec.anghami_artist(artist):
            if t["id"] not in seen_a and t.get("duration"):
                seen_a.add(t["id"]); a2s.append(t); n += 1
                if n >= per_artist:
                    break
    for q in arabic_queries:
        for h in rec.spotify_search(q)[:6]:
            if h["uri"] not in seen_s and h.get("duration"):
                seen_s.add(h["uri"]); s2a.append(h)
    return s2a, a2s


def _artist_ok(src, cand):
    import matcher
    return max(matcher._artist_pair_score(x, y, True)
               for x in matcher._names(src, "artist") for y in matcher._names(cand, "artist") if x and y) >= 80


def _title_plausible(src, cand):
    """Loose, deliberately simpler than the matcher: some real overlap
    between any of the names, in spelling or in consonants."""
    import matcher
    from rapidfuzz import fuzz
    for x in matcher._names(src, "title"):
        for y in matcher._names(cand, "title"):
            if not x or not y:
                continue
            if fuzz.token_set_ratio(matcher.normalize(x), matcher.normalize(y)) >= 60:
                return True
            sx, sy = matcher.skeleton(x).replace(" ", ""), matcher.skeleton(y).replace(" ", "")
            if min(len(sx), len(sy)) >= 3 and fuzz.partial_ratio(sx, sy) >= 80:
                return True
    return False


def evaluate(rec, direction, srcs):
    import matcher
    out = {"right": [], "wrong": [], "miss": [], "absent": [], "unverified": []}
    for s in srcs:
        if direction == "s2a":  # same fallbacks the clients use
            search, artist_songs, svc = rec.anghami_search, rec.anghami_artist, "Anghami"
        else:
            search, artist_songs, svc = rec.spotify_search, rec.spotify_search, "Spotify"
        pool = list(search(s["artist"]))
        if direction == "s2a":
            pool += artist_songs(s["artist"])
        picked = matcher.find_in_catalog(search, s["title"], s["artist"], 87, False, None, svc,
                                         s.get("aliases"), artist_songs, s.get("duration"))
        for q in (f"{s['title']} {s['artist']}", s["title"]):
            pool += search(q)
        same = lambda c: (c.get("duration") and abs(c["duration"] - s["duration"]) <= 3
                          and _artist_ok(s, c) and _title_plausible(s, c))
        oracle = sorted([c for c in pool if same(c)], key=lambda c: abs(c["duration"] - s["duration"]))
        if picked:
            if same(picked):
                out["right"].append((s, picked))
            elif oracle:
                out["wrong"].append((s, picked, oracle[0]))
            else:
                out["unverified"].append((s, picked))
        else:
            (out["miss"] if oracle else out["absent"]).append((s, oracle[0] if oracle else None))
    return out


def _fmt(t):
    return f"{t['title']} / {t['artist']} [{t.get('duration', 0):.0f}s]" if t else "-"


def run_replay(set_name, verbose):
    filename, artists, arabic_queries = sets[set_name]
    rec = Recorder(load_recording(set_name))
    s2a, a2s = sources(rec, artists, arabic_queries)
    results = {}
    for direction, label, srcs in (("s2a", "Spotify -> Anghami", s2a), ("a2s", "Anghami -> Spotify", a2s)):
        out = results[direction] = evaluate(rec, direction, srcs)
        print(f"[{set_name}] {label}: {sum(len(v) for v in out.values())} songs | right {len(out['right'])} | "
              f"WRONG {len(out['wrong'])} | MISS {len(out['miss'])} | unverified {len(out['unverified'])} | "
              f"absent {len(out['absent'])}")
        if verbose:
            for s, p, o in out["wrong"]:
                print(f"    WRONG  {_fmt(s)}\n             picked {_fmt(p)}\n             should {_fmt(o)}")
            for s, o in out["miss"]:
                print(f"    MISS   {_fmt(s)}\n             exists {_fmt(o)}")
            for s, p in out["unverified"]:
                print(f"    UNVER  {_fmt(s)}\n             picked {_fmt(p)}")
    if rec.unrecorded:
        print(f"[{set_name}] note: {rec.unrecorded} searches the recording doesn't have (answered empty); "
              f"run 'record' to fill them in")
    return results


def run_falsematch(set_name, verbose):
    """Pairs of different songs (clearly different titles) by the same artist,
    within 2.5s in length -- the case the same-length rule could get wrong.
    Anything judged "the same" here should be one song listed twice (e.g. in
    two scripts); verbose lists them for checking by eye."""
    import matcher
    from rapidfuzz import fuzz
    rec = Recorder(load_recording(set_name))
    groups = [rec.anghami_artist(name) for name in list(rec.data["anghami_artist"])]
    groups += [[h for h in hits] for hits in rec.data["spotify_search"].values()]
    seen, judged_same, total = set(), set(), 0
    for songs in groups:
        songs = [s for s in songs if s.get("duration")]
        for a, b in itertools.combinations(songs, 2):
            ka, kb = a.get("id") or a.get("uri"), b.get("id") or b.get("uri")
            if ka == kb or a["artist"] != b["artist"] or abs(a["duration"] - b["duration"]) > 2.5:
                continue
            key = tuple(sorted((ka, kb)))
            if key in seen:
                continue
            seen.add(key)
            if fuzz.token_set_ratio(matcher.normalize(a["title"]), matcher.normalize(b["title"])) >= 70:
                continue  # same title: most likely one song listed twice
            total += 1
            if matcher.match_score(a, b) >= 87:
                judged_same.add(f"{a['title']} [{a['duration']:.0f}s]  ==  {b['title']} [{b['duration']:.0f}s]  ({a['artist']})")
    print(f"[{set_name}] false-match test: {total} same-artist, same-length, different-title pairs | "
          f"judged the same song: {len(judged_same)} (check these by eye with -v)")
    if verbose:
        for line in sorted(judged_same):
            print("    " + line)
    return len(judged_same)


def run_parity(set_name):
    """matcher.find_many (the batched lookups a sync uses) must pick exactly
    what find_in_catalog picks one song at a time."""
    import matcher
    filename, artists, arabic_queries = sets[set_name]
    rec = Recorder(load_recording(set_name))
    s2a, a2s = sources(rec, artists, arabic_queries)
    key = lambda h: h and (h.get("id") or h.get("uri"))
    differing = 0
    for direction, srcs in (("s2a", s2a), ("a2s", a2s)):
        search, artist = (rec.anghami_search, rec.anghami_artist) if direction == "s2a" else \
                         (rec.spotify_search, rec.spotify_search)
        one = [matcher.find_in_catalog(search, t["title"], t["artist"], 87, False, None, "", t.get("aliases"),
                                       artist, t.get("duration")) for t in srcs]
        many = matcher.find_many(srcs, lambda qs: {q: search(q) for q in qs}, 87, False, None, "",
                                 lambda names: {n: artist(n) for n in names})
        differing += sum(1 for a, b in zip(one, many) if key(a) != key(b))
    print(f"[{set_name}] batched lookups pick the same as one-at-a-time: "
          f"{'yes' if not differing else f'NO, {differing} differ'}")
    return differing


def run_cases(verbose):
    from matcher import match_score
    from anghamiclient import _with_aliases
    T = lambda t, a, kw=None, d=None: dict(_with_aliases({"title": t, "artist": a}, kw), **({"duration": d} if d else {}))
    amr = ['Amr Diab', 'عمرو دياب']
    cases = [
        # cross-script, no help from Anghami's names
        (T("Tamally Maak", "Amr Diab"), T("تملي معاك", "عمرو دياب"), True),
        (T("Ana Lak Ala Toul", "Abdel Halim Hafez"), T("أنا لك على طول", "عبد الحليم حافظ"), True),
        (T("3met 3en", "Natasha"), T("عمت عين", "ناتاشا"), True),
        (T("Kol Ma Aghani", "Shireen Abdel Wahab & Hossam Habib"), T("كل ما أغني", "شيرين عبدالوهاب"), True),
        (T("Khalik Ma'aya", "Amr Diab"), T("خليك معايا", "عمرو دياب"), True),
        (T("Ya Habibi", "Mohamed Mounir"), T("يا حبيبي", "محمد منير"), True),
        (T("انا لحبيبي", "فيروز"), T("Ana La Habibi", "Fairuz"), True),
        (T("Ana Lak 3ala Toul", "Nader Kassir"), T("Ana Lak Ala Tool", "Nader Kassir"), True),
        # with Anghami's names (keywords)
        (T("Tamally Maak", "Amr Diab"), T("Tamally Maak", "Amr Diab", ['Tamally Maak', 'تملى معاك'] + amr), True),
        (T("تملي معاك", "عمرو دياب"), T("Tamally Maak", "Amr Diab", ['Tamally Maak', 'تملى معاك'] + amr), True),
        (T("Ahwak", "عبد الحليم حافظ"), T("Ahwak", "Abdel Halim Hafez", ['Ahwak', 'أهواك', 'Abdel Halim Hafez', 'عبد الحليم حافظ']), True),
        (T("3 Daqat", "Abu"), T("3 Dakkat (Feat. Yousra)", "Abu", ['3 Dakkat (Feat. Yousra)', '3 دقات', 'Abu', 'أبو']), True),
        (T("Leilet Hob", "Amr Diab"), T("Leilt 7ob", "Amr Diab", ['Leilt 7ob', 'ليلة حب'] + amr), True),
        (T("Ya Mn Hawah", "Breaking the Boundaries, Abdulrahman Mohammed"),
         T("Love Humiliation", "Breaking the Boundaries & Abdulrahman Mohammed",
           ['Love Humiliation', 'يا من هواه', 'Breaking the Boundaries & Abdulrahman Mohammed', 'بريكينج ذي بوندريز & عبدالرحمن محمد']), True),
        # same artist + same length rescues a looser title ...
        (T("Teabet Menak", "Elissa", None, 304), T("Ta'abt Minnak", "Elissa", None, 304), True),
        (T("Seta Sobeh", "Hussain Aljassmi", None, 267), T("Seta Al Sobh", "Hussain Al Jassmi", None, 267), True),
        (T("Enta omri", "Umm Kulthum", None, 3537), T("Anta Oumri Remasterisé", "Umm Kulthum", None, 3538), True),
        # titles too thin for consonants ("l l") lean on spelling, at the same length only
        (T("El Layaly", "Wael Jassar", None, 362.96), T("Ellayali", "Wael Jassar", ['Ellayali', 'الليالي', 'Wael Jassar', 'وائل جسار'], 363.0), True),
        (T("El Layaly", "Wael Jassar", None, 362.96), T("Ellayali", "Wael Jassar", None, 367.0), False),
        (T("Leila", "Cairokee", None, 200), T("Lola", "Cairokee", None, 200.5), False),
        # nearly the same letters at the same length, though the consonants differ
        (T("Akhedni Maak", "Fadel Chaker", ['Akhedni Maak', 'خدني معاك', 'Fadel Chaker', 'فضل شاكر'], 314.25),
         T("Akendni Maak", "Fadel Chaker", None, 313.94), True),
        # gulf qaf as g, any length
        (T("قالوا ترى", "عبادي الجوهر"), T("Galo Tara", "Abadi Al Johar"), True),
        (T("قالوا ترى", "عبادي الجوهر"), T("Galo Ensa", "Abadi Al Johar"), False),
        # vowel heavy titles share a vowel shape, within a second only
        (T("Eyoon Alail", "Mohamed Abdo", ['Eyoon Alail', 'عيون الليل', 'Mohamed Abdo', 'محمد عبده'], 565.0),
         T("Oyoun Al Liel", "Mohammed Abdu", None, 564.636), True),
        (T("Eyoon Alail", "Mohamed Abdo", ['Eyoon Alail', 'عيون الليل', 'Mohamed Abdo', 'محمد عبده'], 565.0),
         T("Eyoun Al Lail", "Mohammed Abdu", None, 540.6), False),
        (T("Laila", "Cairokee", None, 200), T("Lola", "Cairokee", None, 200.3), False),
        # franco "2" for a dropped qaf vs gulf g, inside a title Spotify shortened
        (T("Tadrin W Adri Bnftere2", "Abade Al Johar", ['Tadrin W Adri Bnftere2', 'تدرين وادري', 'Abade Al Johar', 'عبادي الجوهر'], 620.11),
         T("Bneftreg", "Abadi Al Johar", None, 618.951), True),
        (T("Tadrin W Adri Bnftere2", "Abade Al Johar", ['Tadrin W Adri Bnftere2', 'تدرين وادري', 'Abade Al Johar', 'عبادي الجوهر'], 620.11),
         T("Bneftreg", "Abadi Al Johar", None, 650.0), False),
        (T("Wala Ala Balo", "Amr Diab", ['Wala Ala Balo', 'ولا على باله'] + amr, 240.0),
         T("Wala Ala Balak", "Amr Diab", ['Wala Ala Balak', 'ولا على بالك'] + amr, 240.5), False),
        (T("Akhedni Maak", "Fadel Chaker", ['Akhedni Maak', 'خدني معاك', 'Fadel Chaker', 'فضل شاكر'], 314.25),
         T("Akendni Maak", "Fadel Chaker", None, 318.0), False),
        # ... but not a different song that just runs as long
        (T("Tamally Maak", "Amr Diab", None, 269), T("Amarain", "Amr Diab", None, 269), False),
        (T("Habayeb Eh", "Amr Diab", None, 253), T("Habibty Malak", "Amr Diab", None, 252), False),
        (T("Hawina Hawina", "Latifa", None, 213), T("Kada Aliya El Haneen", "Latifa", None, 212), False),
        (T("Mashaer (Hekayet Hayat Series)", "Sherine", None, 292), T("Mafish Manea", "Sherine", None, 293), False),
        # contained title, far-off length: different songs
        (T("Habibi", "Majida El Roumi", None, 291), T("Bes Ellak Habibi", "Majida El Roumi", None, 248), False),
        # same artist, other song / same title, other artist
        (T("Tamally Maak", "Amr Diab"), T("خليك معايا", "عمرو دياب"), False),
        (T("Tamally Maak", "Amr Diab"), T("تملي معاك", "محمد منير"), False),
        (T("Habibi", "Amr Diab"), T("حبي", "عمرو دياب"), False),
        (T("Ya Habibi", "Mohamed Mounir"), T("يا حياتي", "محمد منير"), False),
        (T("Nour El Ein", "Amr Diab"), T("Tamally Maak", "Amr Diab", ['Tamally Maak', 'تملى معاك'] + amr), False),
        (T("Habibi", "Amr Diab"), T("Habiby Ya Nour El Ein", "Amr Diab", ['Habiby Ya Nour El Ein', 'حبيبي يا نور العين'] + amr), False),
        (T("Wala Ala Balo", "Amr Diab"), T("Wala Ala Balak", "Amr Diab", ['Wala Ala Balak', 'ولا على بالك'] + amr), False),
        (T("Lelly", "Mohamed Mounir"), T("Wely", "Mohamed Mounir"), False),
        (T("Kol Ma Aghani", "Shireen Abdel Wahab"), T("Kol Ma Aftkr", "Mohammed Saeed"), False),
        # an artist's Arabic name listed before the Latin one is not a title
        (T("IDGAF", "Dua Lipa", ['IDGAF', 'دوا ليبا', 'Dua Lipa']), T("No Goodbyes", "Dua Lipa", ['No Goodbyes', 'دوا ليبا', 'Dua Lipa']), False),
        # read off the page (arabic names only, no length): a final ه is said as a vowel, عبده is
        # "Abdu", نسيته is "Naseto". each with a control that must stay a different song
        (T("وينك يا درب المحبة", "محمد عبده"), T("Wienak Ya Darb Al Mahaba", "Mohammed Abdu", None, 409), True),
        (T("وينك يا درب المحبة", "محمد عبده", None, 410),
         T("Wienak Ya Darb Al Mahaba - Live Concert", "Mohammed Abdu", None, 681), False),
        (T("قالوا نسيته", "محمد عبده"), T("Qalou Naseto", "Mohammed Abdu"), True),
        (T("قالوا نسيته", "محمد عبده"), T("Qalou Naseto (Live, Diamond jubilee)", "Mohammed Abdu"), False),
        (T("مذهلة", "محمد عبده"), T("Mozhela", "Mohammed Abdu"), True),
        (T("مذهلة", "محمد عبده"), T("يا مدوّر الهيّن", "Mohammed Abdu"), False),
        (T("الأماكن", "محمد عبده"), T("Al Amaken", "Mohammed Abdu"), True),
        (T("الأماكن", "محمد عبده"), T("Al Amaken", "Mohammad Al Hueimani"), False),
        (T("مجموعة انسان", "محمد عبده"), T("Magmoua Ensan", "Mohammed Abdu"), True),
        (T("مجموعة انسان", "محمد عبده", None, 443), T("Magmoua Ensan - February Kuwait 2022", "Mohammed Abdu", None, 728), False),
        (T("ودعتك الله", "محمد عبده"), T("Waadatak Allah", "Mohammed Abdu"), True),
        (T("سلام الله", "محمد عبده"), T("Nehmed Allah Salam Al Fahed", "Mohammed Abdu"), False),
        (T("وين احب الليلة", "محمد عبده", None, 900), T("وين أحب الليلة - لندن 97", "Muhammad Abduh", None, 1187), False),
        (T("ولا على باله", "عمرو دياب"), T("Wala Ala Balo", "Amr Diab"), True),
        (T("ولا على بالك", "عمرو دياب"), T("Wala Ala Balo", "Amr Diab"), False),
        # one platform has the full title ("ليالي الشمال الحزينة"), the other a short one, 3s apart;
        # the long cut and another song of a similar name stay apart
        (T("Layali El Shimal", "Fayrouz", ['Layali El Shimal', 'ليالي الشّمال', 'Fayrouz', 'فيروز'], 199),
         T("Laialy El Shemal El Hazeena", "Fairuz", None, 196), True),
        (T("Layali El Shimal", "Fayrouz", ['Layali El Shimal', 'ليالي الشّمال', 'Fayrouz', 'فيروز'], 199),
         T("Layali El Shimal Al Hazeneh", "Fairuz", None, 616), False),
        (T("Be3dou El Habayeb", "Fayrouz", ['Be3dou El Habayeb', 'بعدو الحبابيب', 'Fayrouz', 'فيروز'], 257),
         T("Ya Gabal Elly Baeed - Beado El Habayeb", "Fairuz", None, 322), False),
        (T("بعدو الحبايب", "فيروز"), T("Ya Gabal Elly Baeed - Beado El Habayeb", "Fairuz"), False),
        (T("Habbeytak", "Fayrouz", ['Habbeytak', 'حبيتك', 'Fayrouz', 'فيروز'], 324),
         T("Habbaitak Be El Saif", "Fairuz", None, 212), False),
        (T("Habbeytak", "Fayrouz", ['Habbeytak', 'حبيتك', 'Fayrouz', 'فيروز'], 324),
         T("Saaltak Habiby", "Fairuz", None, 327), False),
        # versions and English controls
        (T("Fakra", "Massar Egbari"), T("Fakra  (Anghami Sessions)", "Massar Egbari"), False),
        (T("worry - Slowed", "LONOWN"), T("worry - ultra slowed", "LONOWN"), False),
        (T("Cinnamon Girl", "Lana Del Rey"), T("Sad Girl", "Lana Del Rey"), False),
        (T("Let Go", "Ark Patrol"), T("Let Go (feat. Veronika Redd)", "Ark Patrol"), True),
        (T("Army Dreamers - 2018 Remaster", "Kate Bush"), T("Army Dreamers", "Kate Bush"), True),
    ]
    failures = 0
    for a, b, want in cases:
        score = match_score(a, b)
        good = (score >= 87) == want
        failures += not good
        if verbose or not good:
            print(f"    {'ok ' if good else 'BAD'} {'same' if want else 'diff'} {score:5.1f}  "
                  f"{a['title']} / {a['artist']}  <->  {b['title']} / {b['artist']}")
    print(f"[cases] {len(cases) - failures}/{len(cases)} hand-written cases pass")
    return failures


def record(set_name):
    from playwright.sync_api import sync_playwright
    from anghamiclient import AnghamiClient
    from spotifyscraper import SpotifyScraper
    import sysutil
    filename, artists, arabic_queries = sets[set_name]
    data = load_recording(set_name)
    with sync_playwright() as pw:
        with AnghamiClient(pw, storage_state_file=os.path.join(sysutil.data_dir(), "anghami_session.json"), debug_dumps=False) as ag, \
             SpotifyScraper(pw, storage_state_file=os.path.join(sysutil.data_dir(), "spotify_session.json"), debug_dumps=False) as sp:
            ag._open_api()
            rec = Recorder(data, ag, sp)
            try:
                s2a, a2s = sources(rec, artists, arabic_queries)
                print(f"sources: {len(s2a)} Spotify, {len(a2s)} Anghami")
                save_recording(set_name, data)
                for direction, srcs in (("s2a", s2a), ("a2s", a2s)):
                    for i in range(0, len(srcs), 25):
                        evaluate(rec, direction, srcs[i:i + 25])
                        save_recording(set_name, data)
                        print(f"  recorded {direction} {min(i + 25, len(srcs))}/{len(srcs)}", flush=True)
            finally:
                save_recording(set_name, data)


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "check"
    verbose = "-v" in args
    set_name = "holdout" if "--holdout" in args else "tuning"
    importlib.import_module("matcher")
    if mode == "cases":
        sys.exit(1 if run_cases(verbose) else 0)
    if mode == "replay":
        run_replay(set_name, verbose)
    elif mode == "falsematch":
        run_falsematch(set_name, verbose)
    elif mode == "parity":
        run_parity(set_name)
    elif mode == "record":
        record(set_name)
    elif mode == "check":
        bad = run_cases(verbose)
        for name in ("tuning", "holdout"):
            results = run_replay(name, verbose)
            bad += sum(len(r["wrong"]) for r in results.values())
            run_falsematch(name, verbose)
            bad += run_parity(name)
        print("\nWrong picks or failed cases:", bad, "(the tuning set's one 'wrong' is a known answer-key "
              "limit: 'Tayer' -> 'طاير' is right)" if bad else "")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
