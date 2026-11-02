"""JSONL kayıtları, Excel çıktısı ve terminal/dosya loglama."""

import json
import re
import shutil

import pandas as pd

from config import ATT_DIR, CACHE_DIR, DATA, DETAIL_DB, FRESH, LLM_DB


def prepare_run():
    """Çıktı klasörünü hazırlar; FRESH açıksa önceki çalışma verilerini siler."""
    DATA.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if FRESH:
        for path in (DETAIL_DB, LLM_DB):
            path.unlink(missing_ok=True)
        if ATT_DIR.exists():
            shutil.rmtree(ATT_DIR)


def read_jsonl(path):
    """JSONL kayıtlarını okur; boş satırları atlar, bozuk kaydı açıkça bildirir."""
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as ex:
                raise ValueError(
                    f"{path.name}, satır {line_number}: "
                    f"geçersiz veya yarım kalmış JSON kaydı. {ex.msg}"
                ) from ex
            if not isinstance(record, dict):
                raise ValueError(
                    f"{path.name}, satır {line_number}: kayıt bir JSON nesnesi olmalı."
                )
            records.append(record)
    return records


ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_cell(v):
    return ILLEGAL.sub("", v)[:32000] if isinstance(v, str) else v


def export_excel(targets, ref2url, output_path):
    detail_rows = read_jsonl(DETAIL_DB)
    if detail_rows:
        det = (
            pd.DataFrame(detail_rows)
            .drop(
                columns=["text", "reference", "sourceId", "source"],
                errors="ignore",
            )
            .drop_duplicates(subset="detailsUrl", keep="last")
        )
    else:
        det = pd.DataFrame(columns=["detailsUrl"])
    llm_rows = read_jsonl(LLM_DB)
    # Eski kayıtlarla uyumluluk: URL bulunmayan NSPA kayıtlarını eşleştir.
    for record in llm_rows:
        if not record.get("detailsUrl"):
            record["detailsUrl"] = ref2url.get(record.get("reference"))
    if llm_rows:
        llm = (
            pd.DataFrame(llm_rows)
            .drop(columns=["reference"], errors="ignore")
            .dropna(subset=["detailsUrl"])
            .drop_duplicates(subset="detailsUrl", keep="last")
        )
    else:
        llm = pd.DataFrame(columns=["detailsUrl"])
    final = targets.merge(
        det, on="detailsUrl", how="left", validate="many_to_one"
    ).merge(llm, on="detailsUrl", how="left", validate="many_to_one")
    # Excel'in kabul etmediği kontrol karakterlerini temizle, hücre sınırını (32767) aşma
    # Excel incelemesinde kullanılmayan alanlar.
    excluded_columns = [
        "estimatedValue",
        "currency",
        "plannedContractAward",
        "Collective Number",
        "Title",
        "Closing Time (CET)",
        "Level Distribution",
        "Buyer Email Address",
        "Buyer Backup Email",
        "Last Updated",
        "Estimated Value",
        "Method",
        "Reference",
        "Description",
        "Opportunity Release",
        "Planned Solicitation Release",
        "Planned Closing Date",
        "Planned Contract Award",
        "detail_note",
        "NOI/RFI/RFQ Release",
        "Planned Solicitation Closing",
        "Published",
        "Last modified",
        "Number",
        "Available Date",
        "Clarification Deadline",
        "Due Date",
        "Contact Information",
        "Bid Type",
        "Portal Access Information",
        "document_note",
        "originator",
        "originatorFull",
        "opportunityTypeFull",
        "source",
        "page_title",
        "Type",
        "resolved_detail_url",
        "Portal access information",
        "attachments",
        "tender_title_en",
        "sourceRecordKey",
        "Product Name",
        "Opportunity Id",
        "Publication Date",
        "Tentative RFP Date",
        "plannedSolicitationClosing",
        "detail_fetch_method",
        "attachment_results",
        "evidence",
        "evidence_original",
        "text_truncated",
        "evidence_status",
        "input_chars",
        "sent_chars",
        "confidence",
    ]
    final = final.drop(columns=excluded_columns, errors="ignore")
    column_labels = {
        "sourceId": "Kaynak",
        "reference": "İhale Referansı",
        "projectTitle": "İhale Başlığı",
        "opportunityType": "İhale Türü",
        "nopoDate": "NATO Portalı Yayın Tarihi",
        "plannedSolicitationRelease": "Planlanan Teklif Çağrısı Tarihi",
        "detailsUrl": "İhale Bağlantısı",
        "Purchasing Organisation": "Satın Alan Organizasyon",
        "relevance": "İlgililik Düzeyi",
        "area": "Önerilen Kategori",
        "reason": "Değerlendirme Gerekçesi",
        "eligibility_note": "Katılım Koşulları",
        "detail_status": "Detay Erişim Durumu",
        "attachment_count": "Bulunan Ek Belge Sayısı",
        "attachment_read_ok": "Başarıyla Okunan Ek Belge Sayısı",
        "attachment_needs_review": "Kontrol Gerektiren Ek Belge Sayısı",
    }
    final = final.rename(columns=column_labels)
    final = (
        final.map(clean_cell) if hasattr(final, "map") else final.applymap(clean_cell)
    )
    final.to_excel(output_path, index=False)
    print(f"{len(final)} kayıt Excel'e yazıldı: {output_path}")
    return output_path


class Tee:
    """Çıktıyı hem terminale hem log dosyasına yazar."""

    def __init__(self, terminal, log_file):
        self.terminal = terminal
        self.log_file = log_file

    def write(self, text):
        self.terminal.write(text)
        self.log_file.write(text)
        self.flush()
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()
