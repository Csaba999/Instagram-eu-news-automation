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
from datetime import date

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

    return {
        "url": url,
        "title": og("og:title"),
        "description": og("og:description"),
        "image": og("og:image"),
    }


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


def push_image(path: pathlib.Path) -> str:
    """Az Instagramnak publikus URL kell: a képet feltoljuk a (publikus) repóba."""
    subprocess.run(["git", "add", str(path)], check=True)
    subprocess.run(["git", "commit", "-m", f"card {path.name}"], check=True)
    subprocess.run(["git", "pull", "--rebase", "--quiet"], check=True)
    subprocess.run(["git", "push"], check=True)
    url = f"{RAW_BASE}/{path.as_posix()}"
    for _ in range(20):  # várjuk meg, míg elérhető lesz
        if requests.head(url, timeout=15).status_code == 200:
            return url
        time.sleep(3)
    raise RuntimeError("A kép nem érhető el publikusan: " + url)


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


def post_to_instagram(image_url: str, caption: str) -> str:
    container_id = ig_call("POST", f"{IG_USER_ID}/media", image_url=image_url, caption=caption)["id"]
    for _ in range(30):  # megvárjuk, míg az Instagram feldolgozza a képet
        status = ig_call("GET", container_id, fields="status_code,status").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Konténer feldolgozási hiba: {status}")
        time.sleep(5)
    return ig_call("POST", f"{IG_USER_ID}/media_publish", creation_id=container_id)["id"]


def build_caption(meta: dict) -> str:
    caption = (
        f"{meta['title']}\n\n{meta['description']}\n\nForrás: Telex.hu\n{meta['url']}"
        "\n\n#európaiunió #eu #hírek"
    )
    return caption[:2200]  # Instagram caption-limit


def main() -> None:
    if not DRY_RUN:
        missing = [k for k in ("IG_USER_ID", "IG_TOKEN", "RAW_BASE") if not os.environ.get(k)]
        if missing:
            sys.exit("Hiányzó környezeti változó(k): " + ", ".join(missing))

    IMG_DIR.mkdir(exist_ok=True)
    posted = load_state()
    candidates = oldest_first([u for u in get_article_urls() if u not in posted])
    print(f"{len(candidates)} új cikk a listában, max. {MAX_PER_RUN} posztolva", "(DRY_RUN)" if DRY_RUN else "")

    done = 0
    for url in candidates:  # legrégebbi elöl
        if done >= MAX_PER_RUN:
            break
        meta = get_meta(url)
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
            print(f"--- [DRY_RUN] kép: {card}\n{caption}\n---")
        else:
            image_url = push_image(card)
            media_id = post_to_instagram(image_url, caption)
            posted.append(url)
            save_state(posted)
            print("Posztolva:", url, "media id:", media_id)
        done += 1


if __name__ == "__main__":
    main()
