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
DOCS_DIR = pathlib.Path("docs")  # GitHub Pages: a bio-link oldala (docs/index.html)
ARTICLES_FILE = DOCS_DIR / "articles.json"
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


HU_MONTHS = ["január", "február", "március", "április", "május", "június", "július",
             "augusztus", "szeptember", "október", "november", "december"]


def load_articles() -> list[dict]:
    return json.loads(ARTICLES_FILE.read_text()) if ARTICLES_FILE.exists() else []


def save_articles(articles: list[dict]) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    ARTICLES_FILE.write_text(json.dumps(articles, indent=2, ensure_ascii=False) + "\n")


def backfill_articles(posted: list[str], articles: list[dict]) -> None:
    """A korábban posztolt, de az oldalon még nem szereplő cikkek címét utólag lekéri."""
    known = {a["url"] for a in articles}
    for url in posted:
        if url in known:
            continue
        try:
            meta = get_meta(url)
        except Exception as e:  # noqa: BLE001
            print("Cím lekérése sikertelen:", url, e, file=sys.stderr)
            continue
        if meta["title"]:
            articles.append({"url": url, "title": meta["title"],
                             "description": meta["description"], "date": article_date(url)})


def build_page(articles: list[dict]) -> None:
    """Egyszerű, mobilbarát lista a posztolt cikkekről, napok szerint, legfrissebb elöl."""
    from html import escape

    by_day: dict[str, list[dict]] = {}
    for a in sorted(articles, key=lambda a: a["date"], reverse=True):
        by_day.setdefault(a["date"], []).append(a)
    sections = []
    for day, items in list(by_day.items())[:60]:
        y, m, d = map(int, day.split("-"))
        lis = "\n".join(
            f'<li><a href="{escape(a["url"])}" target="_blank" rel="noopener">{escape(a["title"])}</a>'
            + (f'<p>{escape(a["description"])}</p>' if a.get("description") else "") + "</li>"
            for a in reversed(items)  # egy napon belül a legutóbb posztolt elöl
        )
        sections.append(f"<h2>{y}. {HU_MONTHS[m - 1]} {d}.</h2>\n<ul>\n{lis}\n</ul>")
    page = f"""<!doctype html>
<html lang="hu">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Napi EU-s hírek</title>
<style>
:root {{ --bg:#f6f7f5; --card:#fff; --text:#1a1d1a; --muted:#5b625b; --accent:#00a844; --line:#e3e7e2; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#111412; --card:#1a1e1b; --text:#eef1ee; --muted:#a3aba4; --accent:#2fd36f; --line:#2a302b; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width:640px; margin:0 auto; padding:24px 16px 48px; }}
header {{ border-left:6px solid var(--accent); padding-left:12px; margin-bottom:24px; }}
h1 {{ font-size:1.6rem; margin:0; }}
header p {{ margin:4px 0 0; color:var(--muted); }}
h2 {{ font-size:1rem; color:var(--muted); margin:28px 0 8px; text-transform:uppercase; letter-spacing:.04em; }}
ul {{ list-style:none; margin:0; padding:0; }}
li {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:10px; }}
li a {{ color:var(--text); font-weight:600; text-decoration:none; }}
li a:hover {{ color:var(--accent); }}
li p {{ margin:6px 0 0; color:var(--muted); font-size:.92rem; }}
footer {{ margin-top:32px; color:var(--muted); font-size:.85rem; text-align:center; }}
</style>
</head>
<body>
<main>
<header><h1>Napi EU-s hírek</h1><p>Az Instagramon megosztott cikkek, napok szerint</p></header>
{chr(10).join(sections) or "<p>Még nincs cikk.</p>"}
<footer>Forrás: Telex.hu</footer>
</main>
</body>
</html>
"""
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "index.html").write_text(page)


def main() -> None:
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

    articles = load_articles()
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
            (card_url,) = push_images(card)
            media_id = publish(image_url=card_url, caption=caption)
            posted.append(url)
            save_state(posted)
            articles.append({"url": url, "title": meta["title"],
                             "description": meta["description"], "date": article_date(url)})
            save_articles(articles)
            print("Posztolva:", url, "media id:", media_id)
            try:  # a cikk linkje kommentként is a poszt alá kerül; hibája nem állítja meg a futást
                ig_call("POST", f"{media_id}/comments", message=url)
                print("Link kommentelve.")
            except Exception as e:  # noqa: BLE001
                print("Komment hiba:", e, file=sys.stderr)
        done += 1

    if not DRY_RUN:
        backfill_articles(posted, articles)
        save_articles(articles)
        build_page(articles)

    if slot and not DRY_RUN:
        LAST_SLOT_FILE.write_text(slot + "\n")  # csak sikeres futás után: hiba esetén a következő indítás újrapróbálja


if __name__ == "__main__":
    main()
