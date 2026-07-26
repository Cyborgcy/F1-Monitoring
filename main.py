#!/usr/bin/env python3
"""
Sepang / Malaysia F1 ticket monitor - v3
Watches the F1 ticket store AND sepangcircuit.com.

Per-source config, because the two sites need opposite keywords:
  tickets.formula1.com -> "malaysia"/"sepang" are clean, "bahrain" is dirty
                          (already present in the 2027 block)
  sepangcircuit.com    -> "malaysia" is dirty (it's a Malaysian circuit),
                          "formula 1"/"formula one" are clean

Primary signal everywhere: a NEW event link appears.
Everything is baseline-aware and persisted, so restarts don't re-alert.
"""

import html as htmllib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta

import requests

# ==========================================================================
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
if not NTFY_TOPIC:
    raise RuntimeError("Set the NTFY_TOPIC environment variable before running this script.")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

STATE_FILE = "f1_state.json"
POLL_SECONDS = 90
REQUEST_TIMEOUT = 25
HEARTBEAT_HOURS = 12       # low-priority "still alive" ping

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SOURCES = [
    {
        "name": "F1 Store calendar",
        "url": "https://tickets.formula1.com/en",
        "link_re": r"/en/(f1-\d+-[a-z0-9\-]+)",
        "link_base": "https://tickets.formula1.com/en/",
        "keywords": [r"\bmalaysia\w*\b", r"\bsepang\b", r"\bkuala\s+lumpur\b"],
        "check_queue": True,
        "hash_diff": False,
    },
    {
        "name": "Sepang ticketing",
        "url": "https://www.sepangcircuit.com/ticketing",
        "link_re": r"sepangcircuit\.com/events/([a-z0-9\-\.]+?)(?:\.html)?[\"'/\s]",
        "link_base": "https://www.sepangcircuit.com/events/",
        "keywords": [r"\bformula\s*1\b", r"\bformula\s*one\b", r"\bfia\s+formula\b",
                     r"\bgrand\s+prix\s+of\s+bahrain\b", r"\bbahrain\b"],
        "check_queue": False,
        "hash_diff": False,
    },
    {
        "name": "Sepang events listing",
        "url": "https://www.sepangcircuit.com/events-listing",
        "link_re": r"sepangcircuit\.com/events/([a-z0-9\-\.]+?)(?:\.html)?[\"'/\s]",
        "link_base": "https://www.sepangcircuit.com/events/",
        "keywords": [r"\bformula\s*1\b", r"\bformula\s*one\b", r"\bbahrain\b"],
        "check_queue": False,
        "hash_diff": False,
    },
    {
        "name": "Sepang event calendar",
        "url": "https://www.sepangcircuit.com/event-calendar",
        "link_re": r"sepangcircuit\.com/events/([a-z0-9\-\.]+?)(?:\.html)?[\"'/\s]",
        "link_base": "https://www.sepangcircuit.com/events/",
        "keywords": [r"\bformula\s*1\b", r"\bformula\s*one\b", r"\bbahrain\b"],
        "check_queue": False,
        "hash_diff": False,
    },
    {
        "name": "Sepang news archive",
        "url": "https://www.sepangcircuit.com/news-archive/",
        "link_re": None,
        "link_base": "",
        "keywords": [r"\bformula\s*1\b", r"\bformula\s*one\b", r"\bbahrain\b"],
        "check_queue": False,
        "hash_diff": True,     # news page, changes are meaningful
    },
]
# ==========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler("f1_monitor.log"), logging.StreamHandler()],
)
log = logging.getLogger("f1")

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,ms;q=0.8",
})

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style|noscript).*?</\1>", re.S | re.I)


def notify(title, message, priority="urgent", tags="checkered_flag"):
    try:
        requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
        log.info("NOTIFY [%s] %s", title, message.replace("\n", " | ")[:200])
    except Exception as exc:
        log.error("notify failed: %s", exc)


def to_text(raw: str) -> str:
    """Strip scripts/styles/tags so keyword matching doesn't hit CSS or JSON."""
    s = SCRIPT_RE.sub(" ", raw)
    s = TAG_RE.sub(" ", s)
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).lower()


def normalise(raw: str) -> str:
    """For hash diffing - drop the volatile stuff Magento/CDNs inject."""
    s = SCRIPT_RE.sub("", raw)
    s = re.sub(r"form_key[\"'=:\s]+[\"']?\w+", "", s, flags=re.I)
    s = re.sub(r"nonce=[\"'][^\"']+[\"']", "", s, flags=re.I)
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "", s, flags=re.I)
    s = re.sub(r"\b\d{10,13}\b", "", s)
    s = re.sub(r"cdn-cgi/l/email-protection#\w+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as exc:
            log.warning("state file unreadable, starting fresh: %s", exc)
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def check_queue(url):
    try:
        r = session.get(url, allow_redirects=False, timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        log.warning("queue check failed: %s", exc)
        return None
    if 300 <= r.status_code < 400:
        loc = r.headers.get("Location", "")
        if "queue-fair.net" in loc.lower() or "queue-it.net" in loc.lower():
            return loc
    return None


def poll_source(src, state):
    name = src["name"]
    st = state.setdefault(name, {})

    if src["check_queue"]:
        loc = check_queue(src["url"])
        if loc and not st.get("queued"):
            notify("QUEUE IS LIVE",
                   f"{name} is redirecting to a Queue-Fair waiting room.\n"
                   f"Open {src['url']} in your browser NOW.")
        st["queued"] = bool(loc)

    try:
        r = session.get(src["url"], timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        fails = st.get("fails", 0) + 1
        st["fails"] = fails
        log.warning("%s fetch failed (%d in a row): %s", name, fails, exc)
        if fails == 5:
            notify("Monitor degraded",
                   f"{name} has failed {fails} polls in a row.\nLast error: {exc}",
                   priority="high", tags="warning")
        return
    st["fails"] = 0

    raw = r.text
    text = to_text(raw)

    links = sorted(set(m.lower() for m in re.findall(src["link_re"], raw, re.I))) \
        if src["link_re"] else []
    kws = sorted({k for k in src["keywords"] if re.search(k, text, re.I)})

    first = "links" not in st

    if first:
        log.info("BASELINE %-24s links=%d keywords=%s", name, len(links), kws)
        if links:
            log.info("   %s", links)
    else:
        new_links = sorted(set(links) - set(st["links"]))
        if new_links:
            urls = "\n".join(src["link_base"] + s for s in new_links)
            notify("NEW EVENT PAGE",
                   f"{name} has new entries:\n{urls}\n\nCheck it now.")

        new_kw = sorted(set(kws) - set(st["keywords"]))
        if new_kw:
            pretty = ", ".join(k.replace(r"\b", "").replace(r"\s*", " ")
                               .replace(r"\s+", " ").replace(r"\w*", "")
                               for k in new_kw)
            notify("KEYWORD HIT",
                   f"{name} now mentions: {pretty}\n{src['url']}",
                   priority="urgent", tags="rotating_light")

        if src["hash_diff"]:
            import hashlib
            h = hashlib.sha256(normalise(raw).encode("utf-8", "ignore")).hexdigest()
            if st.get("hash") and h != st["hash"]:
                notify("Page changed", f"{name}\n{src['url']}",
                       priority="default", tags="eyes")
            st["hash"] = h

    st["links"] = links
    st["keywords"] = kws
    log.info("%-24s ok links=%-3d kw=%d", name, len(links), len(kws))


def heartbeat(state):
    last = state.get("_heartbeat")
    now = datetime.now()
    if last:
        if now - datetime.fromisoformat(last) < timedelta(hours=HEARTBEAT_HOURS):
            return
    else:
        state["_heartbeat"] = now.isoformat()
        return
    notify("Monitor alive",
           f"Still watching {len(SOURCES)} pages. Nothing new yet.",
           priority="min", tags="heartbeat")
    state["_heartbeat"] = now.isoformat()


def main():
    if "--test-notify" in sys.argv:
        notify("Test notification",
               "This is a test message from the F1 monitor.",
               priority="default", tags="white_check_mark")
        return

    state = load_state()
    once = "--once" in sys.argv
    if not state:
        notify("Monitor armed",
               f"Baselining {len(SOURCES)} pages across tickets.formula1.com "
               f"and sepangcircuit.com.",
               priority="low", tags="white_check_mark")
    while True:
        for src in SOURCES:
            poll_source(src, state)
            time.sleep(3)
        heartbeat(state)
        save_state(state)
        if once:
            return
        time.sleep(POLL_SECONDS + random.randint(0, 30))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped")