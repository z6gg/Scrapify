"""
the user's answers about songs scrapify wasn't sure of (semi-automatic matching), and the
questions still waiting for one. kept in match_choices.json in the data folder, so the background
sync and the window share them. an answer is used by every later sync, in either mode:
a yes adds that exact song without searching, a no keeps that candidate from being picked again
"""

import json
import os
import time
import uuid

from matcher import choice_key

choices_file = "match_choices.json"
_kept_fields = ("title", "artist", "duration", "id", "uri", "aliases")


def _slim(track):
    return {k: track[k] for k in _kept_fields if track.get(k) not in (None, "", {})}


def load(path=None):
    path = path or choices_file
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("pending", [])
            data.setdefault("answers", [])
            return data
    except (OSError, ValueError):
        pass
    return {"pending": [], "answers": []}


def save(data, path=None):
    path = path or choices_file
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    for attempt in range(5):  # the other process may be reading it this instant
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    os.replace(tmp, path)


def _candidate_id(track):
    return track.get("uri") or track.get("id")


def known(service, data=None):
    """{source song's choice_key: the song the user said it is} on service"""
    data = data or load()
    return {a["key"]: a["candidate"] for a in data["answers"] if a["service"] == service and a["same"]}


def rejected(service, data=None):
    """{source song's choice_key: ids of songs the user said it isn't} on service"""
    data = data or load()
    out = {}
    for a in data["answers"]:
        if a["service"] == service and not a["same"]:
            out.setdefault(a["key"], set()).add(_candidate_id(a["candidate"]))
    return out


def add_pending(pair, service, source, candidate, score=None, path=None, weak=False):
    """asks about source -> candidate unless it's already asked or answered. True if it's new.
    weak: scrapify found no match and this is only the closest song. These are offered together
    ("add the closest matches") and never need an answer, see clear_weak"""
    data = load(path)
    key, cid = choice_key(source), _candidate_id(candidate)
    for item in data["pending"] + data["answers"]:
        if item["service"] == service and item["key"] == key and _candidate_id(item["candidate"]) == cid:
            return False
    data["pending"].append({"id": uuid.uuid4().hex[:12], "pair": pair, "service": service, "key": key,
                            "source": _slim(source), "candidate": _slim(candidate),
                            "score": round(score) if score is not None else None, "asked": time.time(),
                            **({"weak": True} if weak else {})})
    save(data, path)
    return True


def clear_weak(pair, service, path=None):
    """forgets the closest-match offers for a pair, so a sync offers only what it still can't find.
    Offers nobody took simply lapse: nothing waits on them and nothing is remembered about them"""
    data = load(path)
    kept = [p for p in data["pending"] if not (p.get("weak") and p["pair"] == pair and p["service"] == service)]
    if len(kept) != len(data["pending"]):
        data["pending"] = kept
        save(data, path)



def answer(pending_id, same, path=None):
    """files the answer to one question. a yes also closes any other question about that song"""
    data = load(path)
    item = next((p for p in data["pending"] if p["id"] == pending_id), None)
    if item is None:
        return None
    data["pending"] = [p for p in data["pending"] if p["id"] != pending_id
                       and not (same and p["service"] == item["service"] and p["key"] == item["key"])]
    data["answers"].append({**{k: item[k] for k in ("pair", "service", "key", "source", "candidate")},
                            "same": bool(same), "answered": time.time()})
    save(data, path)
    return item


def pending(path=None):
    return load(path)["pending"]
