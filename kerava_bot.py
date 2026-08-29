#!/usr/bin/env python3
"""
Kerava Fast-Track bot
---------------------
Watches Oikotie (solid JSON API) AND Etuovi (best-effort) for new
apartments-for-sale in Kerava that match your criteria, scores each against
real central-Kerava market prices, and pushes only the NEW matches to your
WhatsApp — each with a one-tap link that opens your dashboard pre-loaded with
that listing's numbers.

Runs every ~15 min on GitHub Actions (free). Never notifies the same listing
twice: seen IDs live in seen.json, committed back by the workflow each run.
If Etuovi ever fails, Oikotie keeps working — the two sources are independent.
"""

import os
import re
import sys
import json
import time
import urllib.parse
import requests

# ==========================================================================
# 1. YOUR CRITERIA
# ==========================================================================
# --- HARD musts: a listing is dropped if it clearly fails one of these ---
PRICE_MAX   = 145_000     # € ceiling (velaton / debt-free price)
PRICE_MIN   = 80_000
SIZE_MIN    = 52          # m²
SIZE_MAX    = 67          # m²
MONTHLY_MAX = 360         # € — max total monthly charges (vastike etc.)
REQUIRE_OWN_PLOT = True   # drop listings on a rented plot (vuokratontti)

# Summary listings often DON'T include the fee or plot type — in that case the
# bot keeps the listing and flags it "❓ verify" rather than dropping it, so you
# never miss one. It only drops a listing when the data explicitly fails a must.

# --- NICE-TO-HAVE: never filters, only shown as flags in the message ---
#     sauna, balcony (parveke), pipe renovation (putkiremontti) done.

# Only push listings scored "good" or better? Default False = push every match.
ONLY_GOOD_OR_BETTER = False

# Optional district filter (case-insensitive). Empty = all of Kerava.
DISTRICT_KEYWORDS = []     # e.g. ["Keskusta"]

# Your dashboard — each alert links here pre-filled with the listing's numbers.
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL",
    "https://claude.ai/code/artifact/dbf525b6-ca69-487e-8741-73e15c5f06f6")

# Oikotie location selector — VERIFY once (README step 4).
LOCATIONS = os.environ.get("OIKOTIE_LOCATIONS", '[[1701,4,"Kerava"]]')

ENABLE_OIKOTIE = True
ENABLE_ETUOVI  = os.environ.get("ENABLE_ETUOVI", "1") != "0"

# ==========================================================================
# 2. MARKET BENCHMARK  (keep in sync with the dashboard)
# ==========================================================================
MARKET_PPM = 2022
Z_SUPER, Z_GOOD, Z_MARKET = 1750, 1950, 2150

def classify(ppm):
    if ppm <= Z_SUPER:  return ("SUPER", "\U0001F7E2", "Super deal")
    if ppm <= Z_GOOD:   return ("GOOD",  "\U0001F7E1", "Good value")
    if ppm <= Z_MARKET: return ("MARKET","\U0001F7E0", "At market")
    return ("OVER", "\U0001F534", "Above market")
RANK = {"SUPER": 3, "GOOD": 2, "MARKET": 1, "OVER": 0}

# ==========================================================================
# 3. NOTIFICATION CHANNEL (swappable — WhatsApp default)
# ==========================================================================
def notify_whatsapp_callmebot(text):
    phone  = os.environ.get("WHATSAPP_PHONE", "").strip()
    apikey = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not phone or not apikey:
        print("!! WhatsApp not configured (WHATSAPP_PHONE / CALLMEBOT_APIKEY)")
        return False
    try:
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": phone, "text": text, "apikey": apikey},
                         timeout=30)
        print(f"   whatsapp -> {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"!! whatsapp send failed: {e}")
        return False

def notify_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=30)
        return r.status_code == 200
    except Exception as e:
        print(f"!! telegram send failed: {e}")
        return False

def notify(text):
    sent = notify_whatsapp_callmebot(text)
    sent = notify_telegram(text) or sent
    return sent

# ==========================================================================
# 4. HELPERS
# ==========================================================================
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

def num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "price"):
            if k in v: return num(v[k])
        return None
    s = re.sub(r"[^\d,.\-]", "", str(v)).replace(" ", "").replace(",", ".")
    s = re.sub(r"\.(?=\d{3}\b)", "", s)   # strip thousands dots
    try: return float(s)
    except ValueError: return None

SAUNA_RE = re.compile(r"\bsauna\w*", re.I)
BALC_RE  = re.compile(r"\b(parvek\w*|balcony)", re.I)
PIPE_RE  = re.compile(r"\b(putkiremont\w*|linjasaneeraus|putkisaneeraus)", re.I)
RENT_PLOT_RE = re.compile(r"vuokratont\w*|vuokrattu\s+tontti|tontti\W+vuokra", re.I)
OWN_PLOT_RE  = re.compile(r"oma\s+tontti|omistustont\w*", re.I)

# ==========================================================================
# 5. SOURCE: OIKOTIE  (internal cards JSON API)
# ==========================================================================
OIKO = "https://asunnot.oikotie.fi"

def oikotie_tokens(session):
    r = session.get(OIKO + "/", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    body = r.text
    def meta(name):
        m = re.search(r'<meta[^>]+name=["\']%s["\'][^>]+content=["\']([^"\']+)["\']' % name, body) \
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']%s["\']' % name, body)
        return m.group(1) if m else None
    t = {"OTA-token": meta("api-token"), "OTA-loaded": meta("loaded"), "OTA-cuid": meta("cuid")}
    if not all(t.values()):
        raise RuntimeError("Oikotie tokens not found — their front-page markup may have changed.")
    return t

def fetch_oikotie(session):
    t = oikotie_tokens(session)
    params = {
        "cardType": 100, "locations": LOCATIONS, "buildingType[]": 1,
        "price[min]": PRICE_MIN, "price[max]": PRICE_MAX,
        "size[min]": SIZE_MIN, "size[max]": SIZE_MAX,
        "sortBy": "published_sort_desc", "limit": 50, "offset": 0,
    }
    r = session.get(OIKO + "/api/cards", headers={"User-Agent": UA, "Accept": "application/json", **t},
                    params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    cards = data.get("cards", []) if isinstance(data, dict) else []
    out = []
    for c in cards:
        loc = c.get("location") or {}
        cid = str(c.get("id") or c.get("cardId") or "")
        text_parts = [str(c.get(k, "")) for k in ("description", "roomConfiguration", "buildingData", "title")]
        if isinstance(loc, dict):
            text_parts += [str(loc.get(k, "")) for k in ("address", "district", "city")]
        out.append({
            "source": "Oikotie",
            "id": "oikotie:" + cid,
            "price": num(c.get("price")),
            "size": num(c.get("size") or c.get("area")),
            "address": (loc.get("address") if isinstance(loc, dict) else None) or c.get("roomConfiguration") or "Kerava",
            "monthly": num(c.get("maintenanceFee") or c.get("totalHousingCharge")),
            "text": " ".join(text_parts),
            "url": c.get("url") or (f"{OIKO}/myytavat-asunnot/kerava/{cid}" if cid else OIKO),
        })
    return out

# ==========================================================================
# 6. SOURCE: ETUOVI  (best-effort; parses the SSR data in the page)
# ==========================================================================
ETUOVI = "https://www.etuovi.com"

def _walk_listings(node, found):
    """Recursively collect dicts that look like property listings."""
    if isinstance(node, dict):
        keys = set(node.keys())
        has_price = keys & {"price", "sellingPrice", "debtFreePrice", "unencumberedPrice"}
        has_area  = keys & {"area", "livingArea", "size", "totalArea"}
        has_id    = keys & {"friendlyId", "id", "itemId", "announcementId"}
        if has_price and has_area and has_id:
            found.append(node)
        for v in node.values():
            _walk_listings(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_listings(v, found)

def fetch_etuovi(session):
    url = ETUOVI + "/myytavat-asunnot/kerava"
    r = session.get(url, headers={"User-Agent": UA, "Accept-Language": "fi"}, timeout=30)
    r.raise_for_status()
    body = r.text
    blob = None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
    if m:
        blob = m.group(1)
    if not blob:
        # try a generic embedded-state assignment
        m = re.search(r'window\.__(?:NUXT|INITIAL_STATE|APOLLO_STATE)__\s*=\s*(\{.*?\});', body, re.S)
        blob = m.group(1) if m else None
    if not blob:
        print("   etuovi: no embedded listing data found (site may be fully "
              "client-rendered now). Falling back to your Etuovi email alert.")
        return []
    try:
        data = json.loads(blob)
    except Exception as e:
        print(f"   etuovi: could not parse embedded JSON ({e}).")
        return []

    found = []
    _walk_listings(data, found)
    out = []
    seen_ids = set()
    for c in found:
        fid = str(c.get("friendlyId") or c.get("id") or c.get("itemId") or c.get("announcementId") or "")
        if not fid or fid in seen_ids:
            continue
        seen_ids.add(fid)
        price = num(c.get("debtFreePrice") or c.get("unencumberedPrice") or c.get("sellingPrice") or c.get("price"))
        size  = num(c.get("livingArea") or c.get("area") or c.get("size") or c.get("totalArea"))
        addr  = (c.get("address") or c.get("streetAddress") or {})
        if isinstance(addr, dict):
            addr = addr.get("street") or addr.get("streetAddress") or "Kerava"
        text = json.dumps(c, ensure_ascii=False)
        out.append({
            "source": "Etuovi",
            "id": "etuovi:" + fid,
            "price": price, "size": size,
            "address": addr or "Kerava",
            "monthly": num(c.get("maintenanceCharge") or c.get("financialCharge") or c.get("totalCharge")),
            "text": text,
            "url": f"{ETUOVI}/kohde/{fid}",
        })
    print(f"   etuovi: parsed {len(out)} listing(s) from embedded data.")
    return out

# ==========================================================================
# 7. EVALUATE / MESSAGE
# ==========================================================================
def plot_status(text):
    if RENT_PLOT_RE.search(text): return "rent"
    if OWN_PLOT_RE.search(text):  return "own"
    return "unknown"

def evaluate(item):
    price, size = item.get("price"), item.get("size")
    if not price or not size or size <= 0:
        return None
    if not (PRICE_MIN <= price <= PRICE_MAX):
        return None
    if not (SIZE_MIN <= size <= SIZE_MAX):
        return None
    text = item.get("text", "")
    if DISTRICT_KEYWORDS and not any(k.lower() in text.lower() for k in DISTRICT_KEYWORDS):
        return None

    # MUST: own plot — drop only if explicitly rented
    plot = plot_status(text)
    if REQUIRE_OWN_PLOT and plot == "rent":
        return None
    # MUST: monthly charges — drop only if known and over the cap
    monthly = item.get("monthly")
    if monthly is not None and monthly > MONTHLY_MAX:
        return None

    ppm = price / size
    key, emoji, label = classify(ppm)
    return {
        **item, "ppm": ppm, "key": key, "emoji": emoji, "label": label,
        "plot": plot, "monthly": monthly,
        "sauna": bool(SAUNA_RE.search(text)),
        "balcony": bool(BALC_RE.search(text)),
        "pipes": bool(PIPE_RE.search(text)),
    }

def dashboard_link(m):
    q = {"price": int(round(m["price"])), "size": round(m["size"], 1),
         "addr": m["address"][:60]}
    if m.get("monthly"):
        q["fee"] = int(round(m["monthly"]))
    return DASHBOARD_URL + "?" + urllib.parse.urlencode(q)

def yn(flag, yes, unknown="❓"):
    return yes + (" ✅" if flag else " " + unknown)

def build_message(m):
    vs = (m["ppm"] - MARKET_PPM) / MARKET_PPM * 100
    vs_txt = f"{abs(vs):.0f}% under market" if vs <= 0 else f"{vs:.0f}% over market"
    plot_txt = {"own": "oma tontti ✅", "rent": "vuokratontti ❌", "unknown": "tontti ❓"}[m["plot"]]
    fee_txt = (f"vastike €{m['monthly']:.0f}/mo ✅" if m.get("monthly") is not None
               else "vastike ❓ verify")
    nice = "  ".join([yn(m["sauna"], "sauna"), yn(m["balcony"], "parveke"), yn(m["pipes"], "putki")])
    lines = [
        f"{m['emoji']} {m['label']} — Kerava ({m['source']})",
        f"{m['address']}",
        f"€{m['price']:,.0f} · {m['size']:.0f} m² · €{m['ppm']:,.0f}/m² ({vs_txt})",
        f"{plot_txt}  ·  {fee_txt}",
        f"nice-to-have: {nice}",
        f"Listing: {m['url']}",
        f"Score it: {dashboard_link(m)}",
    ]
    return "\n".join(lines).replace(",", " ")

# ==========================================================================
# 8. STATE
# ==========================================================================
SEEN_FILE = os.environ.get("SEEN_FILE", "seen.json")

def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids)[-3000:], f, ensure_ascii=False, indent=0)

# ==========================================================================
# 9. MAIN
# ==========================================================================
def main():
    session = requests.Session()
    items = []
    if ENABLE_OIKOTIE:
        try:
            items += fetch_oikotie(session)
        except Exception as e:
            print(f"!! Oikotie failed: {e}")
    if ENABLE_ETUOVI:
        try:
            items += fetch_etuovi(session)
        except Exception as e:
            print(f"!! Etuovi failed (Oikotie still ran): {e}")

    print(f"Fetched {len(items)} raw listing(s) across sources.")
    if not items:
        print("Nothing fetched. If this repeats, re-check the Oikotie location code (README step 4).")

    seen = load_seen()
    first_run = len(seen) == 0

    matches = []
    for it in items:
        m = evaluate(it)
        if not m or not m["id"] or m["id"] in seen:
            continue
        if ONLY_GOOD_OR_BETTER and RANK[m["key"]] < RANK["GOOD"]:
            seen.add(m["id"]); continue
        matches.append(m)

    matches.sort(key=lambda x: (RANK[x["key"]], -x["ppm"]), reverse=True)

    if first_run:
        for m in matches:
            seen.add(m["id"])
        save_seen(seen)
        print(f"First run: silently recorded {len(matches)} current match(es). "
              "You'll be pinged on the NEXT new one.")
        return

    for m in matches:
        msg = build_message(m)
        print("NEW MATCH:\n" + msg + "\n")
        notify(msg)
        seen.add(m["id"])
        time.sleep(2)

    save_seen(seen)
    print(f"Done. {len(matches)} new match(es) notified.")

if __name__ == "__main__":
    main()
    
