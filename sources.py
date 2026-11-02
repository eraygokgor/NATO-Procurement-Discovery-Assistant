"""Liste ve detay erişimi, tarayıcı oturumları ve ek indirme koordinasyonu."""

import atexit
import json
import re
import time
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from attachments import extract_text
from config import (
    ACT_BASE,
    ATT_DIR,
    BROWSER_DIR,
    DAYS_BACK,
    DETAIL_DB,
    LIMIT,
    LLM_DB,
    NCIA_BASE,
    NSPA_BASE,
    SOURCES,
    URL,
)
from storage import read_jsonl

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch_tender_list():
    api_start_date = None

    if DAYS_BACK is not None:
        if (
            isinstance(DAYS_BACK, bool)
            or not isinstance(DAYS_BACK, int)
            or DAYS_BACK <= 0
        ):
            raise ValueError("DAYS_BACK pozitif bir tam sayı veya None olmalı.")

        today = pd.Timestamp.now(tz="Europe/Istanbul").normalize()
        api_start_date = (today - pd.Timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")

    rows, page = [], 1
    while True:
        params = {
            "sortBy": "nopoDate",
            "sortOrder": "asc",
            "page": page,
            "pageSize": 100,
            "includeFilterOptions": "false",
        }

        if api_start_date is not None:
            params["nopoDate"] = api_start_date

        response = requests.get(
            URL,
            params=params,
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        rows += data["items"]
        if page >= data["pagination"]["totalPages"]:
            break
        page += 1
    df = pd.DataFrame(rows)
    if df.empty:
        return df, df.copy()
    for col in [
        "nopoDate",
        "plannedSolicitationRelease",
        "plannedSolicitationClosing",
        "plannedContractAward",
    ]:
        df[col] = pd.to_datetime(df[col], unit="ms")
    df["source"] = df["originator"].str.lower()
    print(
        f"{len(df)} kayıt çekildi. Kaynak dağılımı (originator'a göre): {df['source'].value_counts().to_dict()}"
    )
    filtered = df.copy()
    if DAYS_BACK is None:
        print("Tarih filtresi yok: tüm ilanlar değerlendirilecek.")

    else:
        if (
            isinstance(DAYS_BACK, bool)
            or not isinstance(DAYS_BACK, int)
            or DAYS_BACK <= 0
        ):
            raise ValueError("DAYS_BACK pozitif bir tam sayı veya None olmalı.")

        today = pd.Timestamp.now(tz="Europe/Istanbul").normalize()
        start_date = (today - pd.Timedelta(days=DAYS_BACK)).date()
        end_date = today.date()  # Bugün dahil değil.
        last_date = (today - pd.Timedelta(days=1)).date()

        publication_dates = filtered["nopoDate"].dt.date
        missing_dates = publication_dates.isna().sum()

        filtered = filtered.loc[
            (publication_dates >= start_date) & (publication_dates < end_date)
        ].copy()

        print(
            f"Tarih filtresi: {start_date:%d.%m.%Y} – "
            f"{last_date:%d.%m.%Y} "
            f"(bugün hariç son {DAYS_BACK} gün). "
            f"{len(filtered)} kayıt bu aralıkta."
        )

        if missing_dates:
            print(
                f"UYARI: Yayın tarihi bulunmayan {missing_dates} kayıt "
                "tarih filtresine dahil edilmedi."
            )

    parts = []
    for s in SOURCES:
        sub = filtered[filtered["source"] == s]
        parts.append(sub.head(LIMIT) if LIMIT else sub)
    targets = pd.concat(parts, ignore_index=True)
    print(f"{len(targets)} ihale işlenecek ({', '.join(SOURCES)}).")
    return df, targets


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nato.int/",
}


S = requests.Session()


S.headers.update(BROWSER_HEADERS)


S.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504]
        )
    ),
)


def get(url, timeout=30, headers=None):
    resp = S.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp


def safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", str(name))


def parse_nspa(html):
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    h2 = soup.select_one("div.page-header h2")
    fields["page_title"] = " ".join(h2.get_text(" ", strip=True).split()) if h2 else ""
    for g in soup.select("div.form-group"):
        lab = g.find("label")
        if lab:
            key = lab.get_text(strip=True)
            lab.extract()
            fields[key] = " ".join(g.get_text(" ", strip=True).split())
    files = []
    for t in soup.find_all(attrs={"documents": True}):
        for fl in json.loads(t["documents"])["Files"]:
            files.append(
                {
                    "FileName": fl["FileName"],
                    "url": NSPA_BASE + fl["DownloadFileActionUrl"],
                }
            )
    return fields, files


def parse_ncia(html):
    """Sayfa 404 ise (None, [])."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.get_text(strip=True).endswith("404"):
        return None, []
    h1 = soup.select_one("h1")
    fields = {"page_title": h1.get_text(strip=True) if h1 else ""}
    for p in soup.find_all("p"):
        t = " ".join(p.get_text(" ", strip=True).split())
        m = re.match(
            r"(Method|Reference|Estimated Value|Last Updated|Description):\s*(.*)", t
        )
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if key == "Description" and not val:  # değer bir sonraki <p>'de
            nxt = p.find_next_sibling("p")
            val = " ".join(nxt.get_text(" ", strip=True).split()) if nxt else ""
        if val.strip() in ("", "€"):
            continue
        fields[key] = val
    for card in soup.select("div.card-item"):  # Key Dates
        head, val = card.select_one(".date-header"), card.find("p")
        if head and val:
            fields[head.get_text(strip=True)] = val.get_text(strip=True)
    files = []
    for a in soup.select("#download1page a[href]"):
        href = a["href"].strip()
        if href:
            u = urljoin(NCIA_BASE, href)
            name = a.get_text(strip=True) or unquote(u.split("/")[-1].split("?")[0])
            files.append({"FileName": name, "url": u})
    return fields, files


ACT_FILE_EXT = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
    ".txt",
    ".csv",
)


def parse_act(html):
    """ACT (act.nato.int/opportunities/contracting/...) WordPress sayfası.

    Not: sayfanın meta description alanı bazı sayfalarda eski/yanlış, bu yüzden kullanılmıyor."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one(".banner-inner h1") or soup.find("h1")
    fields = {"page_title": h1.get_text(strip=True) if h1 else ""}
    # Yoast JSON-LD'den yayın ve güncelleme tarihi
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data_ld = json.loads(sc.string or "")
        except ValueError:
            continue
        for item in data_ld.get("@graph", []):
            if item.get("@type") == "WebPage":
                if item.get("datePublished"):
                    fields["Published"] = item["datePublished"][:10]
                if item.get("dateModified"):
                    fields["Last modified"] = item["dateModified"][:10]
    body = soup.select_one("div.only-desc")
    files, blocks = [], []
    if body:
        container = body.select_one(".col-lg-12") or body
        for el in container.find_all(recursive=False):
            if el.name == "ul":
                blocks += [
                    "- " + ln
                    for ln in el.get_text("\n", strip=True).splitlines()
                    if ln.strip()
                ]
            else:
                t = " ".join(el.get_text(" ", strip=True).split())
                if not t or t.lower().startswith("click here"):
                    continue  # sadece dosya linki olan satırlar bilgi taşımaz
                blocks.append(re.sub(r"\s+([:.,;])", r"\1", t))
        if blocks:
            fields["Description"] = "\n".join(blocks)
        seen = set()
        for a in body.find_all("a", href=True):
            href = a["href"].strip()
            if href.split("?")[0].lower().endswith(ACT_FILE_EXT):
                u = urljoin(ACT_BASE, href)
                if u not in seen:
                    seen.add(u)
                    files.append(
                        {
                            "FileName": unquote(u.split("?")[0].rsplit("/", 1)[-1]),
                            "url": u,
                        }
                    )
    return fields, files


class AccessBlocked(Exception):
    """Detay erişimi başarısız; çağıran kod liste bilgisiyle devam edebilir."""


class RateLimited(AccessBlocked):
    """HTTP 429 alındı; tarayıcıyla yeni istek gönderilmez."""


def parse_aco(html):
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    form = soup.select_one('[data-name="FormBidDetailVendor"]')
    if form:
        for group in form.select(".form-group"):
            label = group.select_one(".label-text")
            value = group.select_one(".field-display")
            if label and value:
                fields[label.get_text(" ", strip=True)] = value.get_text(
                    "\n", strip=True
                )
    if fields.get("Title"):
        fields["page_title"] = fields["Title"]
    alert = soup.select_one(".anonymous-info-alert .message-content")
    if alert:
        fields["Portal access information"] = alert.get_text("\n", strip=True)
    if fields:
        fields["document_note"] = (
            "Bid Documents tab has not been collected. "
            "Assessment uses the available description and page fields."
        )
    return fields, []


ADAPTERS = {
    "nspa": (parse_nspa, "div.form-group:has(label)"),
    "ncia": (parse_ncia, "p"),
    "act": (parse_act, "div.only-desc"),
    "aco": (
        parse_aco,
        '[data-name="FormBidDetailVendor"] '
        '.form-group:has(label[for="editor-Description"]) .field-display',
    ),
}


NCIA_FIELD = re.compile(r"^\s*(Method|Reference|Description)\s*:", re.IGNORECASE)


PW = None


BROWSERS = {}


for host in (
    "eportal.nspa.nato.int",
    "www.ncia.nato.int",
    "ncia.nato.int",
    "www.act.nato.int",
    "act.nato.int",
    # "nato.bonfirehub.com",
    "aco.procureware.com",
):
    S.mount(f"https://{host}/", HTTPAdapter(max_retries=0))


def close_browsers():
    global PW
    for context, _ in BROWSERS.values():
        try:
            context.close()
        except Exception as ex:
            print("Tarayıcı kapatma hatası:", ex)
    BROWSERS.clear()
    if PW is not None:
        try:
            PW.stop()
        finally:
            PW = None


atexit.register(close_browsers)


def get_browser(source):
    global PW
    if source not in BROWSERS:
        (BROWSER_DIR / source).mkdir(parents=True, exist_ok=True)
        if PW is None:
            PW = sync_playwright().start()
        context = PW.chromium.launch_persistent_context(
            user_data_dir=str((BROWSER_DIR / source / "profile").resolve()),
            channel="msedge",
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        BROWSERS[source] = (context, page)
    return BROWSERS[source]


def detail_ready(source, html):
    """HTTP 200 yeterli değil; beklenen ihale içeriği de bulunmalı."""
    soup = BeautifulSoup(html, "html.parser")
    _, selector = ADAPTERS[source]
    for element in soup.select(selector):
        text = element.get_text(" ", strip=True)
        if source == "ncia":
            if NCIA_FIELD.match(text):
                return True
        elif text:
            return True
    return False


def page_not_found(html):
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(
        r"^\s*404\b|"
        r"\bpage\s+(?:not found|cannot be found|could not be found|"
        r"does not exist|doesn't exist)\b|"
        r"^\s*not found\b",
        re.IGNORECASE,
    )
    return any(
        pattern.search(element.get_text(" ", strip=True))
        for element in soup.select("title, h1")
    )


def fetch_browser(source, url):
    parser, selector = ADAPTERS[source]
    _, page = get_browser(source)
    label = source.upper()
    try:
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
    except PlaywrightTimeoutError:
        # Sayfa kısmen açılmış olabilir; içeriği beklemeye devam et.
        response = None
    if response is not None:
        challenged = response.headers.get("cf-mitigated", "").lower() == "challenge"
        if response.status == 429:
            raise RateLimited(f"{label}: Tarayıcı HTTP 429")
        if response.status in (404, 410) and not challenged:
            return None, []
        if response.status >= 400 and not (challenged or response.status in (401, 403)):
            raise AccessBlocked(f"{label}: Tarayıcı HTTP {response.status} | {url}")
        if response is not None and response.status == 200:
            if not response.body().strip():
                return None, []
    html = page.content()
    if page_not_found(html):
        # print(f"{label}: Page not found; liste bilgileriyle devam ediliyor.")
        return None, []
    if not detail_ready(source, html):
        print(
            f"{label}: İhale içeriği bekleniyor. "
            "Doğrulama görünürse tamamla; en fazla 10 saniye beklenecek."
        )
    try:
        locator = page.locator(selector)
        if source == "ncia":
            locator = locator.filter(has_text=NCIA_FIELD)
        locator.first.wait_for(state="attached", timeout=10_000)
        html = page.content()
        if page_not_found(html):
            return None, []
        if not detail_ready(source, html):
            raise AccessBlocked("İhale içerik alanı boş")
    except (PlaywrightTimeoutError, AccessBlocked) as ex:
        if page_not_found(page.content()):
            return None, []
        debug_file = BROWSER_DIR / source / "response.html"
        debug_file.write_text(page.content(), encoding="utf-8")
        raise AccessBlocked(
            f"{label}: Tarayıcıdan detay alınamadı"
            f" | title={page.title()}"
            f" | url={page.url}"
            f" | response={debug_file}"
        ) from ex
    fields, files = parser(html)
    if fields is not None:
        fields["resolved_detail_url"] = page.url
        fields["detail_fetch_method"] = "browser"
    return fields, files


def fetch_detail(source, url):
    parser, _ = ADAPTERS[source]
    candidates = [url]
    # NCIA'nın eski .aspx adresi için mevcut alternatif denemeyi koru.
    parts = urlsplit(url)
    if source == "ncia" and parts.path.lower().endswith(".aspx"):
        candidates.append(urlunsplit(parts._replace(path=parts.path[:-5])))
    browser_candidates = []
    # 1) Önce HTTP.
    for candidate in candidates:
        try:
            r = S.get(candidate, timeout=30)
            # print(f"{source.upper()} HTTP {r.status_code}: {candidate}")
            if r.status_code == 429:
                raise RateLimited(
                    f"HTTP 429 | url={candidate}"
                    f" | Retry-After={r.headers.get('Retry-After', '-')}"
                )
            challenged = r.headers.get("cf-mitigated", "").lower() == "challenge"
            if challenged or r.status_code in (401, 403):
                browser_candidates.append(candidate)
                continue
            if r.status_code in (404, 410):
                continue
            r.raise_for_status()
            if page_not_found(r.text):
                continue
            fields, files = parser(r.text)
            if fields is None:
                continue
            if not detail_ready(source, r.text):
                # print(f"{source.upper()}: HTTP yanıtında ihale içeriği yok.")
                browser_candidates.append(candidate)
                continue
            fields["resolved_detail_url"] = r.url
            fields["detail_fetch_method"] = "http"
            return fields, files
        except (requests.RequestException, ValueError):
            # print(f"{source.upper()} HTTP HATA: {candidate} | {ex}")
            browser_candidates.append(candidate)
    # 2) HTTP ile alınamayan adresler için tarayıcı.
    failures = []
    for candidate in browser_candidates:
        print(f"{source.upper()}: Tarayıcı deneniyor -> {candidate}")
        try:
            fields, files = fetch_browser(source, candidate)
            if fields is not None:
                return fields, files
        except RateLimited:
            raise
        except (AccessBlocked, PlaywrightError) as ex:
            failures.append(str(ex))
    if failures:
        raise AccessBlocked("; ".join(failures))
    # Denenen sayfalar bulunamadı; mevcut döngü liste verisiyle devam eder.
    return None, []


def fetch_nspa(url):
    return fetch_detail("nspa", url)


def fetch_ncia(url):
    return fetch_detail("ncia", url)


def fetch_act(url):
    return fetch_detail("act", url)


def fetch_is(url):
    return {
        "page_title": "",
        "detail_note": (
            "Bonfire detail fetching is disabled. "
            "Evaluated using list data only; attachment contents unavailable."
        ),
    }, []


def fetch_ims(url):
    return fetch_is(url)


def fetch_aco(url):
    return fetch_detail("aco", url)


FETCHERS = {
    "nspa": fetch_nspa,
    "ncia": fetch_ncia,
    "act": fetch_act,
    "is": fetch_is,
    "ims": fetch_ims,
    "aco": fetch_aco,
}


def download_attachment(source, url, path, detail_url, headers=None):
    try:
        content = get(url, timeout=120, headers=headers).content
    except requests.HTTPError as ex:
        if ex.response is None or ex.response.status_code not in (401, 403):
            raise
        print(f"{source.upper()}: Ek için tarayıcı deneniyor -> {path.name}")
        _, page = get_browser(source)
        # Dosyanın ait olduğu sitedeki tarayıcı oturumunu hazırla.
        if urlsplit(page.url).netloc != urlsplit(detail_url).netloc:
            fetch_browser(source, detail_url)
        result = page.evaluate(
            """async (url) => {

                const controller = new AbortController();

                const timer = setTimeout(() => controller.abort(), 120000);

                try {

                    const response = await fetch(url, {

                        credentials: "include",

                        signal: controller.signal

                    });

                    if (!response.ok) {

                        throw new Error(`Ek indirme HTTP ${response.status}`);

                    }

                    return Array.from(

                        new Uint8Array(await response.arrayBuffer())

                    );

                } finally {

                    clearTimeout(timer);

                }

            }""",
            url,
        )
        content = bytes(result)
    if not content:
        raise ValueError("Ek dosyanın içeriği boş")
    if path.suffix.lower() == ".pdf" and b"%PDF-" not in content[:1024]:
        raise ValueError("PDF yerine farklı içerik döndü")
    path.write_bytes(content)


def collect_details(targets):
    target_urls = set(targets["detailsUrl"])
    detail_rows = read_jsonl(DETAIL_DB)
    retry_urls = {
        row["detailsUrl"]
        for row in detail_rows
        if row.get("detailsUrl") in target_urls
        and row.get("source") == "ncia"
        and re.search(
            r"\[EK INDIRILEMEDI: 403\b",
            row.get("text", ""),
        )
    }
    if retry_urls:
        for db in (DETAIL_DB, LLM_DB):
            rows = read_jsonl(db)
            with db.open("w", encoding="utf-8") as f:
                for row in rows:
                    if row.get("detailsUrl") not in retry_urls:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{len(retry_urls)} NCIA kaydı eksik ekler için yeniden işlenecek.")
    done = {r["detailsUrl"] for r in read_jsonl(DETAIL_DB)}
    errors = 0
    blocked_src = {}  # kaynak -> engellenen ihale sayısı
    with DETAIL_DB.open("a", encoding="utf-8") as f:
        for r in targets.itertuples():
            if r.detailsUrl in done:
                continue
            if blocked_src.get(r.source, 0) >= 3:
                continue  # aynı kaynakta art arda engel: siteyi zorlamadan atla
            try:
                detail_status = (
                    "list_only_policy" if r.source in {"is", "ims"} else "ok"
                )
                try:
                    fields, files = FETCHERS[r.source](r.detailsUrl)
                except RateLimited:
                    raise
                except AccessBlocked as ex:
                    detail_status = "access_failed"
                    print(
                        "DETAY ERISIM HATASI",
                        r.source,
                        r.reference,
                        "->",
                        ex,
                    )
                    fields = {
                        "page_title": "",
                        "detail_note": (
                            "Detail page could not be accessed. "
                            "Evaluated using list data only."
                        ),
                    }
                    files = []
                if fields is None:
                    detail_status = "unavailable"
                    fields = {
                        "page_title": "",
                        "detail_note": (
                            "Detail page unavailable or returned empty content. "
                            "Evaluated using list data only."
                        ),
                    }
                    files = []
                folder = ATT_DIR / r.source / safe(r.reference)
                # ACT ekleri için tarayıcıdaki gibi Referer = ihalenin kendi sayfası
                dl_headers = {"Referer": r.detailsUrl} if r.source == "act" else None
                texts = ["\n".join(f"{k}: {v}" for k, v in fields.items())]
                attachment_results = []
                for fl in files:
                    folder.mkdir(parents=True, exist_ok=True)
                    p = folder / safe(fl["FileName"])
                    download_status = "cached" if p.exists() else "pending"
                    extraction_status = "not_attempted"
                    try:
                        if not p.exists():
                            download_attachment(
                                r.source,
                                fl["url"],
                                p,
                                r.detailsUrl,
                                headers=dl_headers,
                            )
                            download_status = "downloaded"
                            time.sleep(1)
                    except Exception as ex:
                        download_status = "failed"
                        body = f"[EK INDIRILEMEDI: {ex}]"
                    else:
                        try:
                            body = extract_text(p).strip()
                            if not body:
                                extraction_status = "empty"
                                body = "[BOS METIN - dosyadan metin cikarilamadi]"
                            elif body.startswith("[DESTEKLENMEYEN FORMAT:"):
                                extraction_status = "unsupported"
                            elif body.startswith("[METIN CIKARMA HATASI:"):
                                extraction_status = "failed"
                            elif p.suffix.lower() == ".zip":
                                issue_markers = (
                                    "[BOS ZIP",
                                    "[ZIP LIMITI",
                                    "[DESTEKLENMEYEN FORMAT:",
                                    "[SIFRELI DOSYA",
                                    "[BOS METIN",
                                    "[METIN CIKARMA HATASI:",
                                    "[ZIP UYESI OKUNAMADI:",
                                )
                                extraction_status = (
                                    "needs_review"
                                    if any(marker in body for marker in issue_markers)
                                    else "ok"
                                )
                            else:
                                extraction_status = "ok"
                        except Exception as ex:
                            extraction_status = "failed"
                            body = f"[METIN CIKARMA HATASI: {ex}]"
                    attachment_results.append(
                        {
                            "filename": fl["FileName"],
                            "url": fl["url"],
                            "download_status": download_status,
                            "extraction_status": extraction_status,
                        }
                    )
                    if extraction_status != "ok":
                        print(
                            "EK KONTROL",
                            r.reference,
                            fl["FileName"],
                            f"| indirme={download_status}",
                            f"| okuma={extraction_status}",
                        )
                    texts.append(f"\n=== {fl['FileName']} ===\n{body}")
                rec = {
                    "source": r.source,
                    "reference": r.reference,
                    "detailsUrl": r.detailsUrl,
                    **fields,
                    "detail_status": detail_status,
                    "attachments": "; ".join(fl["FileName"] for fl in files),
                    "attachment_results": attachment_results,
                    "attachment_count": len(attachment_results),
                    "attachment_read_ok": sum(
                        item["extraction_status"] == "ok" for item in attachment_results
                    ),
                    "attachment_needs_review": sum(
                        item["extraction_status"] != "ok" for item in attachment_results
                    ),
                    "text": "\n".join(texts),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                done.add(r.detailsUrl)
                errors = 0
                status = "DETAY OK" if detail_status == "ok" else "SADECE LISTE"
                print(
                    status,
                    r.source,
                    r.reference,
                    f"| durum={detail_status}",
                    f"| bulunan ek={len(files)}",
                )
            except AccessBlocked as ex:
                blocked_src[r.source] = blocked_src.get(r.source, 0) + 1
                print("ENGELLENDI", r.source, r.reference, "->", ex)
                if blocked_src[r.source] == 3:
                    print(
                        f"  {r.source}: 3 engel, bu kaynağın kalanı atlanıyor (kayda geçmedi, sonra yeniden denenir)"
                    )
                continue
            except Exception as ex:
                errors += 1
                print("DETAY HATA", r.source, r.reference, ex)
                if errors >= 5:
                    print("Art arda 5 hata, duruyorum.")
                    break
            time.sleep(1)
    if blocked_src:
        print(
            f"UYARI: erişim engeli nedeniyle detayı alınamayan kaynaklar: {blocked_src}"
        )
