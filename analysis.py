"""LLM girdisi, yanıt şeması, değerlendirme ve alıntı kontrolü."""

import json
import re
import time

from openai import BadRequestError, RateLimitError

from config import (
    DETAIL_DB,
    LLM_DB,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TIMEOUT,
    MAX_CHARS,
    RECHECK_REFS,
)
from storage import read_jsonl


def build_response_format(system_prompt):
    ALLOWED_AREAS = sorted(
        set(re.findall(r'^- "([^"]+)"\s*$', system_prompt, re.MULTILINE))
    )
    if not ALLOWED_AREAS:
        raise ValueError("Promptta en az bir kategori tanımlanmalı.")
    properties = {
        name: {"type": "string"} for name in ["notice_id", "reason", "eligibility_note"]
    }
    properties.update(
        {
            "relevance": {
                "type": "string",
                "enum": ["HIGH", "MEDIUM", "LOW", "NONE"],
            },
            "area": {
                "type": ["string", "null"],
                "enum": ALLOWED_AREAS + [None],
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "evidence": {"type": ["string", "null"]},
        }
    )
    LLM_RESPONSE_FORMAT = {
        "type": "json_schema",
        "json_schema": {
            "name": "tender_assessment",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "tenders": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "items": {
                            "type": "object",
                            "properties": properties,
                            "required": list(properties),
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["tenders"],
                "additionalProperties": False,
            },
        },
    }
    return LLM_RESPONSE_FORMAT


BOILER = re.compile(
    r"general provisions|terms and conditions|termination for convenience|"
    r"code of conduct|misr form|cover page",
    re.IGNORECASE,
)


def shorten_section(text, limit):
    """Uzun bölümün başını ve sonunu korur; atlanan kısmı açıkça belirtir."""
    if len(text) <= limit:
        return text
    marker = "\n[PROCESSING NOTE: middle content omitted due to input limit]\n"
    if limit <= len(marker):
        return marker[:limit]
    available = limit - len(marker)
    head = (available * 2) // 3
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def fit_tender_text(text, budget):
    """Sayfa bilgilerine ve her eke yer ayırarak karakter sınırına uyar."""
    if budget <= 0:
        raise ValueError("LLM metin bütçesi pozitif olmalı.")
    if len(text) <= budget:
        return text
    chunks = re.split(r"\n\n=== (.+?) ===\n", text)
    header = chunks[0]
    attachments = list(zip(chunks[1::2], chunks[2::2]))
    if not attachments:
        return shorten_section(text, budget)
    # Sayfa/liste bilgileri bütçenin en fazla dörtte birini kullanır.
    header = shorten_section(header, max(1, budget // 4))
    labels = [f"\n\n=== {name} ===\n" for name, _ in attachments]
    remaining = budget - len(header) - sum(map(len, labels))
    minimum = 100
    if remaining < minimum * len(attachments):
        raise ValueError("Metin bütçesi her eke asgari yer ayırmak için yetersiz.")
    # Kısa ekler ihtiyaçları kadar yer alır; kalan bütçe uzunlara dağıtılır.
    lengths = [len(body) for _, body in attachments]
    allocations = [0] * len(attachments)
    pending = set(range(len(attachments)))
    while pending:
        share = remaining // len(pending)
        small = [i for i in pending if lengths[i] <= share]
        if small:
            for i in small:
                allocations[i] = lengths[i]
                remaining -= lengths[i]
                pending.remove(i)
        else:
            extra = remaining % len(pending)
            for position, i in enumerate(sorted(pending)):
                allocations[i] = share + (position < extra)
            break
    return header + "".join(
        label + shorten_section(body, allocation)
        for label, (_, body), allocation in zip(labels, attachments, allocations)
    )


def build_text(rec, lr):
    """Liste ve sayfa bilgilerini eklerle birleştirir; uzun içerikte bütçeyi bölümler arasında dağıtır."""
    chunks = re.split(r"\n=== (.+?) ===\n", rec["text"])
    atts = sorted(
        zip(chunks[1::2], chunks[2::2]), key=lambda a: 1 if BOILER.search(a[0]) else 0
    )
    listinfo = (
        f"Notice ID: {rec['reference']}\n"
        f"Source agency: {lr.originator} ({lr.sourceId})\n"
        f"List title: {lr.projectTitle}\n"
        f"Opportunity type: {lr.opportunityTypeFull}\n"
        f"Estimated value: {lr.estimatedValue} {lr.currency}\n"
        f"Planned solicitation closing: {lr.plannedSolicitationClosing}\n"
    )
    text = (
        listinfo
        + "\nTENDER PAGE FIELDS:\n"
        + chunks[0]
        + "".join(f"\n\n=== {n} ===\n{b}" for n, b in atts)
    )
    prepared = fit_tender_text(text, MAX_CHARS)
    return prepared, len(text) > MAX_CHARS, len(text)


def parse_json(s):
    s = re.sub(r"^```(?:json)?|```$", "", (s or "").strip(), flags=re.MULTILINE).strip()
    obj = json.loads(re.search(r"\{.*\}", s, re.DOTALL).group(0))
    return obj["tenders"][0] if "tenders" in obj else obj


last_llm_request = 0


def classify(text, client, system_prompt, response_format):
    """Dönüş: (sonuç, gerçekten gönderilen ihale metni)."""
    global last_llm_request
    used = len(text)
    for _ in range(3):
        sent_text = fit_tender_text(text, used)
        try:
            time.sleep(max(0, 5.1 - (time.monotonic() - last_llm_request)))
            last_llm_request = time.monotonic()
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": f"Analyze the following tender.\n\n{sent_text}",
                    },
                ],
                response_format=response_format,
                max_tokens=LLM_MAX_TOKENS,
                timeout=LLM_TIMEOUT,
            )
            break
        except BadRequestError as ex:
            m = re.search(
                r"Input length \((\d+)\) exceeds model's maximum context length \((\d+)\)",
                str(ex),
            )
            if not m:
                raise
            n_in, n_max = int(m.group(1)), int(m.group(2))
            budget = n_max - LLM_MAX_TOKENS - 1000  # çıktı token'ları da bağlamdan yer
            used = int(used * budget / n_in * 0.95)
            print(
                f"  bağlam aşıldı ({n_in} > {n_max}), metin {used:,} karaktere kısaltılıp tekrar deneniyor"
            )
    else:
        raise RuntimeError("bağlam sınırı kısaltmayla da aşılıyor")
    ch = resp.choices[0]
    if ch.finish_reason == "length":
        raise RuntimeError("max_tokens yetmedi (cevap kesildi)")
    result = parse_json(ch.message.content)
    relevance = result.get("relevance")
    area = result.get("area")
    allowed_areas = set(re.findall(r'^- "([^"]+)"\s*$', system_prompt, re.MULTILINE))
    if relevance in {"HIGH", "MEDIUM", "LOW"}:
        if area not in allowed_areas:
            raise ValueError(
                f"Geçersiz kategori: relevance={relevance}, area={area!r}. "
                "İlgili ihalelerde izin verilen bir kategori zorunludur."
            )
    elif relevance == "NONE":
        if area is not None:
            raise ValueError(
                f"Geçersiz kategori: relevance=NONE iken area null olmalı; "
                f"gelen={area!r}."
            )
    else:
        raise ValueError(f"Geçersiz relevance: {relevance!r}")
    return result, sent_text


def check_evidence(evidence, sent_text):
    """Alıntının gönderilen metinde geçtiğini, yalnızca boşlukları normalleştirerek kontrol eder."""
    if evidence is None:
        return "not_provided"
    normalized_evidence = " ".join(evidence.split())
    if not normalized_evidence:
        return "not_provided"
    normalized_text = " ".join(sent_text.split())
    return "matched" if normalized_evidence in normalized_text else "not_found"


def evaluate_tenders(
    targets,
    ref2url,
    client,
    system_prompt,
    response_format,
):
    details = {r["detailsUrl"]: r for r in read_jsonl(DETAIL_DB)}
    llm_rows = read_jsonl(LLM_DB)
    for r in llm_rows:
        if not r.get("detailsUrl"):
            r["detailsUrl"] = ref2url.get(r.get("reference"))
    if RECHECK_REFS:
        # Diğer sonuçları koru; seçilenlerin eski değerlendirmelerini kaldır.
        llm_rows = [r for r in llm_rows if r.get("reference") not in RECHECK_REFS]
        LLM_DB.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in llm_rows),
            encoding="utf-8",
        )
    done_llm = {r["detailsUrl"] for r in llm_rows if r.get("detailsUrl")}
    errors = 0
    with LLM_DB.open("a", encoding="utf-8") as f:
        for lr in targets.itertuples():
            if RECHECK_REFS and lr.reference not in RECHECK_REFS:
                continue
            url = lr.detailsUrl
            if url in done_llm or url not in details:
                continue
            try:
                text, truncated, n_chars = build_text(details[url], lr)
                for attempt in range(4):
                    try:
                        res, sent_text = classify(
                            text,
                            client,
                            system_prompt,
                            response_format,
                        )
                        break
                    except RateLimitError as ex:
                        if attempt == 3:
                            raise
                        match = re.search(
                            r"retry after approximately\s+(\d+)", str(ex), re.IGNORECASE
                        )
                        wait = int(match.group(1)) + 2 if match else 60
                        print(
                            f"LLM 429: {wait} sn bekleniyor; aynı ihale yeniden denenecek."
                        )
                        time.sleep(wait)
                evidence_original = res.get("evidence")
                evidence_status = check_evidence(
                    evidence_original,
                    sent_text,
                )
                res["evidence_original"] = evidence_original
                if evidence_status != "matched":
                    res["evidence"] = None
                if evidence_status == "not_found":
                    print(
                        "ALINTI KONTROL",
                        lr.source,
                        lr.reference,
                        "Alıntı doğrulanamadı; evidence boş bırakıldı. "
                        "Özgün metin evidence_original alanında saklandı.",
                    )
                res.pop("notice_id", None)
                res.update(
                    detailsUrl=url,
                    reference=lr.reference,
                    text_truncated=truncated or sent_text != text,
                    input_chars=n_chars,
                    sent_chars=len(sent_text),
                    evidence_status=evidence_status,
                )
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
                f.flush()
                done_llm.add(url)
                errors = 0
                print(
                    "LLM OK",
                    lr.source,
                    lr.reference,
                    res.get("relevance"),
                    "|",
                    res.get("area"),
                )
            except Exception as ex:
                errors += 1
                print("LLM HATA", lr.source, lr.reference, ex)
                if errors >= 5:
                    print("Art arda 5 hata, duruyorum.")
                    break
