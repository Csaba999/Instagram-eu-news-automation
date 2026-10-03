"""
Telex EU-hírek -> Instagram autoposzt.

Lépések:
1. Lekéri a Telex "Európai Unió" címkeoldalát, kiszedi a cikk-URL-eket
   (ha a címkeoldal nem elérhető, a telex.hu/rss feedből szűr EU-s cikkekre).
2. Minden új cikknél beolvassa az og:title / og:description / og:image meta címkéket.
3. A poszt képe: a cikk borítóképe zöld kerettel. A cím és a leírás a caption szövegébe kerül.
4. A képet a publikus repóba pusholja (raw.githubusercontent.com URL), majd
   posztolja az Instagram API with Instagram Login-on keresztül.

DRY_RUN=1: lekér, képet generál, kiírja a captiont, de nem pushol és nem posztol.
"""
import email.utils
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

TAG_URL = "https://telex.hu/cimke/europai-unio"
RSS_URL = "https://telex.hu/rss"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 EUHirekBot/1.0",
    "Accept-Language": "hu-HU,hu;q=0.9",
}
STATE_FILE = pathlib.Path("posted.json")
IMG_DIR = pathlib.Path("images")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "1"))  # ennyi új cikket posztol egy futásnál

START_DATE = os.environ.get("START_DATE", "2026-10-02")  # ennél régebbi cikket nem posztol
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "2"))  # naponta (budapesti nap) legfeljebb ennyi poszt
MAX_AGE_DAYS = 2  # ennél régebbi cikk már nem kerül ki (a napi limit miatt ne torlódjon fel a sor)
DAILY_FILE = pathlib.Path("daily_count.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
IG_USER_ID = os.environ.get("IG_USER_ID", "").strip()  # a secretbe véletlenül bekerült szóköz/újsor ne zavarjon
IG_TOKEN = os.environ.get("IG_TOKEN", "").strip()
RAW_BASE = os.environ.get("RAW_BASE", "")  # pl. https://raw.githubusercontent.com/USER/REPO/main
GRAPH = "https://graph.instagram.com/v21.0"  # Instagram Login API (nem kell Facebook-oldal)

ARTICLE_RE = re.compile(r"^(?:https?://telex\.hu)?(/[a-z\-]+)?/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/?$")


def load_state() -> list[str]:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []


def save_state(urls: list[str]) -> None:
    STATE_FILE.write_text(json.dumps(urls[-500:], indent=2, ensure_ascii=False) + "\n")


def normalize(href: str) -> str:
    href = href.split("?")[0].split("#")[0].rstrip("/")
    return href if href.startswith("http") else "https://telex.hu" + href


def get_article_urls() -> list[str]:
    r = requests.get(TAG_URL, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"Címkeoldal: HTTP {r.status_code}, RSS-re váltok", file=sys.stderr)
        return get_article_urls_rss()
    soup = BeautifulSoup(r.text, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        if ARTICLE_RE.match(a["href"].split("?")[0]):
            url = normalize(a["href"])
            if url not in seen:
                seen.add(url)
                out.append(url)
    if not out:
        print("Címkeoldal: nem találtam cikket, RSS-re váltok", file=sys.stderr)
        return get_article_urls_rss()
    return out  # legfrissebb elöl


def get_article_urls_rss() -> list[str]:
    r = requests.get(RSS_URL, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"Telex RSS: HTTP {r.status_code}, Google News RSS-re váltok", file=sys.stderr)
        return get_article_urls_gnews()
    soup = BeautifulSoup(r.content, "xml")
    out = []
    for item in soup.find_all("item"):
        text = " ".join(t.get_text(" ") for t in item.find_all(["title", "description", "category"]))
        if re.search(r"\bEU\b|Európai Unió|Európai Bizottság|Európai Parlament|uniós", text):
            out.append(normalize(item.link.get_text(strip=True)))
    return out


GNEWS_RSS = (
    "https://news.google.com/rss/search?q=site:telex.hu+%22Eur%C3%B3pai+Uni%C3%B3%22+when:7d"
    "&hl=hu&gl=HU&ceid=HU:hu"
)


def decode_gnews_url(gn_url: str) -> str:
    """A Google News átirányító linkjéből kinyeri az eredeti cikk-URL-t."""
    article_id = gn_url.split("/articles/")[1].split("?")[0]
    page = requests.get(f"https://news.google.com/rss/articles/{article_id}", headers=HEADERS, timeout=30)
    page.raise_for_status()
    div = BeautifulSoup(page.text, "html.parser").select_one("c-wiz > div[jscontroller]")
    payload = [
        "Fbv4je",
        f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
        f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{article_id}",'
        f'{div["data-n-a-ts"]},"{div["data-n-a-sg"]}"]',
    ]
    r = requests.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data={"f.req": json.dumps([[payload]])},
        timeout=30,
    )
    r.raise_for_status()
    return json.loads(json.loads(r.text.split("\n\n")[1])[0][2])[1]


def get_article_urls_gnews() -> list[str]:
    r = requests.get(GNEWS_RSS, headers=HEADERS, timeout=30)
    r.raise_for_status()
    items = BeautifulSoup(r.content, "xml").find_all("item")
    items.sort(key=lambda i: email.utils.parsedate_to_datetime(i.pubDate.get_text()), reverse=True)
    out = []
    for item in items[:10]:
        try:
            url = normalize(decode_gnews_url(item.link.get_text(strip=True)))
        except Exception as e:  # noqa: BLE001 – egy rossz link ne állítsa meg a futást
            print("Google News link feloldása sikertelen:", e, file=sys.stderr)
            continue
        if ARTICLE_RE.match(url):
            out.append(url)
    print(f"Google News: {len(items)} találat, {len(out)} Telex-cikk feloldva", file=sys.stderr)
    return out


def article_date(url: str) -> str:
    y, m, d = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url).groups()
    return f"{y}-{m}-{d}"


def oldest_first(urls: list[str]) -> list[str]:
    """A lista legfrissebb elöl jön: dátum szerint növekvő, egy napon belül fordított listasorrend."""
    order = {u: i for i, u in enumerate(urls)}
    return sorted(
        (u for u in urls if article_date(u) >= START_DATE),
        key=lambda u: (article_date(u), -order[u]),
    )


def get_meta(url: str) -> dict:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def og(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"].strip() if tag and tag.get("content") else ""

    # a cikk elejének szövege a mesterséges intelligenciás témaszűréshez
    body = " ".join(p.get_text(" ", strip=True) for p in soup.select("article p, .article-html-content p"))
    return {
        "url": url,
        "title": og("og:title"),
        "description": og("og:description"),
        "image": og("og:image"),
        "body": body[:3000],
    }


# Témaszűrés: csak uniós döntések, jogszabályok, bővítés, uniós pénzek stb.; belpolitika és
# pártpolitika nem. A címben, a leírásban és az URL-ben keresünk (kisbetűsen, részszóra).
TOPICS = {
    "Döntés, jogszabály": ["rendelet", "irányelv", "jogszabály", "szabályoz", "elfogad", "megszavaz",
                           "jóváhagy", "döntött", "döntés", "határozat", "betilt", "tilalom", "kötelező lesz",
                           "új szabály", "javaslat", "európai bizottság", "uniós tanács", "az eu tanácsa"],
    "Bővítés, csatlakozás": ["csatlakoz", "bővítés", "tagjelölt", "tagság", "uniós tag", "schengen",
                             "euró bevezet", "eurózóna", "csatlakozási tárgyal"],
    "Uniós pénzek": ["uniós forrás", "uniós pénz", "eu-s támogatás", "uniós támogatás", "helyreállítási",
                     "kohéziós", "uniós költségvetés", "milliárd eurós", "befagyasztott"],
    "Szankciók, kereskedelem": ["szankció", "kereskedelmi megállapodás", "vámtarifa", "vámok", "embargó"],
    "Jog, bíróság": ["európai bíróság", "kötelezettségszegési", "jogállamisági", "bírság", "luxembourgi bíróság"],
}
BLOCKED = ["fidesz", "kdnp", "tisza", "orbán", "magyar péter", "mi hazánk", "momentum", "párt ",
           "pártok", "kampány", "választás", "ellenzék",
           "interjú", "botrány"]


def plain(text: str) -> str:
    """Kisbetűs, ékezet nélküli alak (az URL-ek is ékezet nélküliek)."""
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def classify(meta: dict) -> str | None:
    """A cikk témája (kategória neve), vagy None, ha nem illik a profilba."""
    slug = meta["url"].rsplit("/", 1)[-1].replace("-", " ")
    text = plain(f" {meta['title']} {meta['description']} {slug} ")
    if any(plain(b) in text for b in BLOCKED):
        return None
    for topic, words in TOPICS.items():
        if any(plain(w) in text for w in words):
            return topic
    return None


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")  # üresen: a legújabb elérhető flash-lite modell
_resolved_model: str | None = None


def gemini_model() -> str:
    """A használt modell neve; alapból a Google listájából a legújabb "flash-lite" (a legkevésbé terhelt)."""
    global _resolved_model
    if GEMINI_MODEL:
        return GEMINI_MODEL
    if _resolved_model is None:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": GEMINI_API_KEY}, params={"pageSize": 1000}, timeout=30)
        r.raise_for_status()
        names = [
            m["name"].split("/", 1)[1] for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
            and re.fullmatch(r"gemini-[\d.]+-flash-lite", m["name"].split("/", 1)[1])
        ]
        if not names:
            raise RuntimeError("Nem találtam flash-lite Gemini modellt.")
        _resolved_model = max(names, key=lambda n: [int(x) for x in re.findall(r"\d+", n)])
        print("Gemini modell:", _resolved_model)
    return _resolved_model
AI_PROMPT = """Egy magyar Instagram-oldalnak válogatsz híreket, amely az Európai Unió működéséről szól.
Döntsd el a cikkről, hogy kikerülhet-e az oldalra.

KIKERÜLHET: uniós döntések, rendeletek, irányelvek, jogszabályok és javaslatok; bővítés, csatlakozás,
tagjelölt országok (és pl. egy ország vissza- vagy becsatlakozásáról szóló hírek, felmérések is);
uniós pénzek, támogatások, költségvetés; szankciók, kereskedelmi megállapodások; az Európai Bíróság
ítéletei, kötelezettségszegési eljárások; az uniós intézmények (Bizottság, Parlament, Tanács) döntései.

NEM KERÜLHET KI: aktuálpolitika és pártpolitika (magyar vagy más ország pártjai, politikusok egymás
elleni vitái, nyilatkozatai, levelei, kampány, választás), interjúk, botrányok, személyes ügyek,
és minden, ami nem egy uniós döntésről vagy folyamatról szól.
INTERJÚ SOHA NEM KERÜLHET KI, akkor sem, ha uniós jogszabályról vagy uniós témáról szól
(pl. egy EP-képviselővel készült beszélgetés egy irányelvről): ilyenkor kikerulhet = false.

Kategóriák (ha kikerülhet): "Döntés, jogszabály", "Bővítés, csatlakozás", "Uniós pénzek",
"Szankciók, kereskedelem", "Jog, bíróság", "Egyéb uniós ügy".
Az indoklás egy rövid magyar mondat legyen."""


def classify_ai(meta: dict) -> tuple[bool, str, str]:
    """Gemini dönt a cikk témájáról: (kikerülhet-e, kategória, indoklás). Hibánál kivételt dob."""
    article = f"Cím: {meta['title']}\nLeírás: {meta['description']}\nSzöveg eleje: {meta.get('body', '')}"
    for wait in (10, 30, 60, None):  # túlterhelés (503) / kvóta (429) esetén újrapróbáljuk
        r = _gemini_request(article)
        if r.status_code not in (429, 500, 503) or wait is None:
            break
        print(f"Gemini foglalt (HTTP {r.status_code}), {wait} mp múlva újra...", file=sys.stderr)
        time.sleep(wait)
    if not r.ok:
        raise RuntimeError(f"Gemini API hiba (HTTP {r.status_code}): {r.text[:500]}")
    data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    return bool(data["kikerulhet"]), data["kategoria"], data["indoklas"]


def _gemini_request(article: str) -> requests.Response:
    return requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": AI_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": article}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "kikerulhet": {"type": "BOOLEAN"},
                        "kategoria": {"type": "STRING"},
                        "indoklas": {"type": "STRING"},
                    },
                    "required": ["kikerulhet", "kategoria", "indoklas"],
                },
            },
        },
        timeout=60,
    )


def decide_topic(meta: dict) -> str | None:
    """Ha van Gemini-kulcs, a modell dönt; hiba esetén (vagy kulcs nélkül) a kulcsszavas szűrő."""
    if GEMINI_API_KEY:
        try:
            ok, topic, reason = classify_ai(meta)
            print(f"AI döntés: {'IGEN' if ok else 'NEM'} ({topic}) – {reason}")
            return topic if ok else None
        except Exception as e:  # noqa: BLE001
            print("AI szűrés nem sikerült, kulcsszavas szűrőre váltok:", e, file=sys.stderr)
    return classify(meta)


def load_daily(today: str) -> int:
    data = json.loads(DAILY_FILE.read_text()) if DAILY_FILE.exists() else {}
    return data.get("count", 0) if data.get("date") == today else 0


def save_daily(today: str, count: int) -> None:
    DAILY_FILE.write_text(json.dumps({"date": today, "count": count}) + "\n")


GREEN = "#00C853"   # a kép keretének színe
FRAME = 24          # keret vastagsága pixelben (0 = nincs keret)
WIDTH = 1080        # a poszt képének szélessége
MIN_RATIO, MAX_RATIO = 0.8, 1.91  # Instagram: 4:5 ... 1.91:1 között fogad el képet


def make_card(meta: dict, path: pathlib.Path) -> None:
    r = requests.get(meta["image"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    photo = Image.open(io.BytesIO(r.content)).convert("RGB")

    inner_w = WIDTH - 2 * FRAME
    inner_h = round(inner_w * photo.height / photo.width)

    # az Instagram által elfogadott képarányon kívül eső képnél a vásznat igazítjuk a határhoz
    total_w, total_h = WIDTH, inner_h + 2 * FRAME
    ratio = total_w / total_h
    if ratio > MAX_RATIO:
        total_h = round(total_w / MAX_RATIO)
    elif ratio < MIN_RATIO:
        total_h = round(total_w / MIN_RATIO)
    inner_h = total_h - 2 * FRAME

    # a teljes kép mindig látszik: nem vágjuk le, hanem a zöld háttérre középre igazítjuk
    photo = ImageOps.contain(photo, (inner_w, inner_h), method=Image.LANCZOS)
    card = Image.new("RGB", (total_w, total_h), GREEN)
    card.paste(photo, ((total_w - photo.width) // 2, (total_h - photo.height) // 2))
    card.save(path, "JPEG", quality=92)


def push_images(*paths: pathlib.Path) -> list[str]:
    """Az Instagramnak publikus URL kell: a képeket feltoljuk a (publikus) repóba."""
    subprocess.run(["git", "add", *map(str, paths)], check=True)
    # ha ugyanez a kép már fent van (pl. egy korábbi, elakadt futásból), nincs mit commitolni
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        subprocess.run(["git", "commit", "-m", f"card {paths[0].name}"], check=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "--quiet"], check=True)
        subprocess.run(["git", "push"], check=True)
    urls = []
    for path in paths:
        url = f"{RAW_BASE}/{path.as_posix()}"
        for _ in range(20):  # várjuk meg, míg elérhető lesz
            if requests.head(url, timeout=15).status_code == 200:
                break
            time.sleep(3)
        else:
            raise RuntimeError("A kép nem érhető el publikusan: " + url)
        urls.append(url)
    return urls


def ig_call(method: str, path: str, **params) -> dict:
    params["access_token"] = IG_TOKEN
    if method == "GET":
        r = requests.get(f"{GRAPH}/{path}", params=params, timeout=60)
    else:
        r = requests.post(f"{GRAPH}/{path}", data=params, timeout=60)
    if not r.ok:
        # a hibaüzenet nem tartalmazza a tokent; az URL-t (ami GET-nél igen) nem írjuk ki
        raise RuntimeError(f"Instagram API hiba ({path.split('/')[-1] or path}, HTTP {r.status_code}): {r.text}")
    return r.json()


def check_account() -> None:
    # a tokenhez tartozó fiókot a /me adja meg; ha az IG_USER_ID nem egyezik vele, azt jelezzük
    me = ig_call("GET", "me", fields="user_id,username,account_type")
    print(f"Instagram-fiók: @{me.get('username')} ({me.get('account_type')})")
    if IG_USER_ID and IG_USER_ID not in (me.get("user_id"), me.get("id")):
        print("Figyelem: az IG_USER_ID secret nem ennek a fióknak az azonosítója; a /me fiókot használom.")


def publish(**params) -> str:
    container_id = ig_call("POST", "me/media", **params)["id"]
    for _ in range(30):  # megvárjuk, míg az Instagram feldolgozza a képet
        status = ig_call("GET", container_id, fields="status_code,status").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Konténer feldolgozási hiba: {status}")
        time.sleep(5)
    return ig_call("POST", "me/media_publish", creation_id=container_id)["id"]


def build_caption(meta: dict) -> str:
    caption = (
        f"{meta['title']}\n\n{meta['description']}\n\nForrás: Telex.hu\n{meta['url']}"
        "\n\n#európaiunió #eu #hírek"
    )
    return caption[:2200]  # Instagram caption-limit


# helyi (Europe/Budapest) idő szerinti futási időpontok. A cron mindkét (nyári/téli) UTC-eltolással
# elindítja a workflow-t; ha a Mac épp aludt, a GitHub sorban tartja a futást, és ébredéskor lefut.
# A bot a legutóbb esedékes időpontot a last_slot.txt-ben jegyzi meg, így minden időpont egyszer
# dolgozik: a kimaradtat ébredéskor pótolja, a dupla (másik eltolású) indítás pedig azonnal kilép.
SLOTS = ["06:00", "11:00", "13:45", "15:35", "18:00", "20:00", "22:00"]
LAST_SLOT_FILE = pathlib.Path("last_slot.txt")


def due_slot() -> str | None:
    tz = ZoneInfo("Europe/Budapest")
    now = datetime.now(tz)
    slots = [
        datetime.combine(day, datetime.strptime(t, "%H:%M").time(), tz)
        for day in (now.date() - timedelta(days=1), now.date())
        for t in SLOTS
    ]
    latest = max(s for s in slots if s <= now).strftime("%Y-%m-%d %H:%M")
    last = LAST_SLOT_FILE.read_text().strip() if LAST_SLOT_FILE.exists() else ""
    if last >= latest:
        print(f"Most {now:%H:%M} van (Budapest); a {latest} időpont már lefutott, kilépek.")
        return None
    print(f"Esedékes időpont: {latest} (most {now:%H:%M}, Budapest)")
    return latest


def ai_test() -> None:
    """Próbaüzem: a legutóbb feldolgozott cikkeket újra elbírálja (nem posztol, nem ír állapotot)."""
    for url in load_state()[-12:]:
        try:
            meta = get_meta(url)
        except Exception as e:  # noqa: BLE001
            print("Nem sikerült lekérni:", url, e)
            continue
        print(f"\n{meta['title']}")
        topic = decide_topic(meta)
        print("  =>", f"KIKERÜLNE ({topic})" if topic else "KIMARADNA")


def main() -> None:
    if os.environ.get("AI_TEST"):
        return ai_test()
    slot = due_slot() if os.environ.get("SCHEDULED") else None
    if os.environ.get("SCHEDULED") and not slot:
        return
    if not DRY_RUN:
        missing = [k for k in ("IG_TOKEN", "RAW_BASE") if not os.environ.get(k)]
        if missing:
            sys.exit("Hiányzó környezeti változó(k): " + ", ".join(missing))

    IMG_DIR.mkdir(exist_ok=True)
    posted = load_state()
    candidates = oldest_first([u for u in get_article_urls() if u not in posted])
    print(f"{len(candidates)} új cikk a listában, max. {MAX_PER_RUN} posztolva", "(DRY_RUN)" if DRY_RUN else "")

    if not DRY_RUN and candidates:
        check_account()

    today_dt = datetime.now(ZoneInfo("Europe/Budapest")).date()
    today = today_dt.isoformat()
    oldest_ok = (today_dt - timedelta(days=MAX_AGE_DAYS)).isoformat()
    posted_today = load_daily(today)
    print(f"Ma eddig {posted_today}/{DAILY_LIMIT} poszt.")

    done = 0
    for url in candidates:  # legrégebbi elöl
        if done >= MAX_PER_RUN or (posted_today >= DAILY_LIMIT and not DRY_RUN):
            if posted_today >= DAILY_LIMIT:
                print("Elérte a napi limitet, a többi cikk később jöhet.")
            break
        if article_date(url) < oldest_ok:
            print("Kihagyva (túl régi):", url)
            if not DRY_RUN:
                posted.append(url)
                save_state(posted)
            continue
        meta = get_meta(url)
        topic = decide_topic(meta)
        if not topic:
            print("Kihagyva (téma nem illik, pl. belpolitika):", meta["title"] or url)
            if not DRY_RUN:
                posted.append(url)
                save_state(posted)
            continue
        print(f"Téma: {topic} – {meta['title']}")
        if not meta["title"] or not meta["image"]:
            print("Kihagyva (hiányzó og:title/og:image):", url)
            if not DRY_RUN:
                posted.append(url)
                save_state(posted)
            continue
        slug = hashlib.sha1(url.encode()).hexdigest()[:8]
        card = IMG_DIR / f"{date.today().isoformat()}-{slug}.jpg"
        make_card(meta, card)
        caption = build_caption(meta)
        if DRY_RUN:
            limit_note = "" if posted_today < DAILY_LIMIT else " (a napi limit miatt élesben ma már nem kerülne ki)"
            print(f"--- [DRY_RUN] kép: {card}{limit_note}\n{caption}\n---")
            posted_today += 1
        else:
            (card_url,) = push_images(card)
            media_id = publish(image_url=card_url, caption=caption)
            posted.append(url)
            save_state(posted)
            posted_today += 1
            save_daily(today, posted_today)
            print("Posztolva:", url, "media id:", media_id)
            try:  # a cikk linkje kommentként is a poszt alá kerül; hibája nem állítja meg a futást
                ig_call("POST", f"{media_id}/comments", message=url)
                print("Link kommentelve.")
            except Exception as e:  # noqa: BLE001
                print("Komment hiba:", e, file=sys.stderr)
        done += 1

    if slot and not DRY_RUN:
        LAST_SLOT_FILE.write_text(slot + "\n")  # csak sikeres futás után: hiba esetén a következő indítás újrapróbálja


if __name__ == "__main__":
    main()
