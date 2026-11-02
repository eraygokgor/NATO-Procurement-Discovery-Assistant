"""Çalışma ayarları, yollar ve başlangıç doğrulaması."""

import os
from pathlib import Path

import truststore
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent

load_dotenv(PROJECT_DIR / ".env")

truststore.inject_into_ssl()

# None: tarih sınırı olmadan tüm ilanlar.
DAYS_BACK = 7

# True: detay/LLM kayıtlarını ve indirilen ekleri silerek yeniden başlar.
FRESH = False

# Kaynak başına kayıt sınırı; None bütün kayıtlar.
LIMIT = None

# Yalnızca seçilen referansların LLM sonucunu yeniler; normal kullanımda boş.
RECHECK_REFS = set()

SOURCES = ["nspa", "ncia", "act", "is", "ims", "aco"]

# LLM girdisi karakter bütçesi; uzun metin bölümler arasında paylaştırılır.
MAX_CHARS = 300_000

URL = (
    "https://www.nato.int/content/nato/en/work-with-us/business-and-project-opportunities/"
    "procurement-opportunities/jcr:content/root/container/procurement-listing.search.json"
)

NSPA_BASE = "https://eportal.nspa.nato.int"

NCIA_BASE = "https://www.ncia.nato.int/"

ACT_BASE = "https://www.act.nato.int/"

LLM_URL = os.getenv("LLM_URL", "").strip()

LLM_MODEL = os.getenv("LLM_MODEL", "").strip()

LLM_MAX_TOKENS = 8000

LLM_TIMEOUT = 300

DATA = PROJECT_DIR / "data"

BROWSER_DIR = DATA / "browser"

ATT_DIR = DATA / "ekler"

CACHE_DIR = DATA / "cache"

RUNS_DIR = DATA / "runs"

DETAIL_DB = CACHE_DIR / "veri_detay.jsonl"

LLM_DB = CACHE_DIR / "llm_sonuc.jsonl"

PROMPT_PATH = PROJECT_DIR / "prompts" / "system_prompt_current.txt"


def validate_settings():
    """Veri indirmeden veya mevcut kayıtları silmeden ayarları kontrol eder."""
    if not isinstance(FRESH, bool):
        raise ValueError("FRESH yalnızca True veya False olabilir.")
    if LIMIT is not None and (
        isinstance(LIMIT, bool) or not isinstance(LIMIT, int) or LIMIT <= 0
    ):
        raise ValueError("LIMIT pozitif bir tam sayı veya None olmalı.")
    supported_sources = {"nspa", "ncia", "act", "is", "ims", "aco"}
    if not SOURCES:
        raise ValueError("SOURCES boş olamaz.")
    unknown_sources = set(SOURCES) - supported_sources
    if unknown_sources:
        raise ValueError(f"Desteklenmeyen kaynaklar: {sorted(unknown_sources)}")
    if len(SOURCES) != len(set(SOURCES)):
        raise ValueError("SOURCES içinde tekrarlanan kaynak var.")
    if not isinstance(RECHECK_REFS, set):
        raise ValueError("RECHECK_REFS bir küme olmalı: set() veya {'referans'}.")
    if FRESH and RECHECK_REFS:
        raise ValueError(
            "FRESH=True ile RECHECK_REFS birlikte kullanılamaz. "
            "Tam yenileme için RECHECK_REFS boş olmalı."
        )
    if not LLM_URL or not LLM_MODEL:
        raise ValueError("LLM_URL ve LLM_MODEL ortam değişkenlerini tanımlayın.")
    if not os.getenv("LLM_API_KEY", "").strip():
        raise ValueError(
            f"LLM_API_KEY tanımlı değil. "
            f"{PROJECT_DIR / '.env'} dosyasını veya ortam değişkenini kontrol et."
        )
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Prompt dosyası bulunamadı: {PROMPT_PATH}")
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"Prompt dosyası boş: {PROMPT_PATH}")
    return prompt
