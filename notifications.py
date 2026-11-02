"""Çalışma özetini ve varsa mevcut Excel raporunu Teams akışına gönderir."""

import base64
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


def text_block(text, **options):
    return {
        "type": "TextBlock",
        "text": str(text),
        "wrap": True,
        **options,
    }


def send_run_notification(report, elapsed, log_path):
    webhook = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
    if not webhook:
        print("Teams bildirimi atlandı: TEAMS_WEBHOOK_URL tanımlı değil.")
        return

    state = report["status"]
    body = [
        text_block(
            "İHALELER",
            weight="Bolder",
            size="Large",
        ),
        text_block(
            datetime.strptime(report["run_id"][:15], "%Y%m%d_%H%M%S").strftime(
                "%d.%m.%Y %H:%M:%S"
            ),
            isSubtle=True,
            size="Small",
        ),
        text_block("**Veri aralığı:** " + report["period"].removeprefix("Dönem: ")),
    ]

    file_name = ""
    file_content = ""

    if state == "completed":
        # Sadece bu çalışmada üretilen Excel kullanılır.
        output_path = Path(report["output_path"])
        output = pd.read_excel(output_path).fillna("")
        records = output.to_dict("records")

        counts = Counter(row.get("İlgililik Düzeyi") for row in records)
        missing_llm = sum(
            row.get("İlgililik Düzeyi") not in {"HIGH", "MEDIUM", "LOW", "NONE"}
            for row in records
        )
        missing_details = sum(
            row.get("Detay Erişim Durumu") not in {"ok", "list_only_policy"}
            for row in records
        )
        attachment_issues = sum(
            int(row.get("Kontrol Gerektiren Ek Belge Sayısı") or 0) for row in records
        )

        has_warnings = missing_llm or missing_details or attachment_issues
        body[0]["text"] = (
            "⚠️ İHALELER — UYARILARLA TAMAMLANDI"
            if has_warnings
            else "✅ İHALELER — TAMAMLANDI"
        )

        body.append(text_block(f"**İşlenen ihale:** {len(records)}"))
        body.append(
            text_block(
                "**İlgililik dağılımı:** "
                f"HIGH: {counts['HIGH']} · "
                f"MEDIUM: {counts['MEDIUM']} · "
                f"LOW: {counts['LOW']} · "
                f"NONE: {counts['NONE']}"
            )
        )

        warnings = []

        for row in records:
            reference = str(row.get("İhale Referansı", "")).strip()
            title = str(row.get("İhale Başlığı", "")).strip()
            label = reference or title or "Referansı bulunmayan ihale"
            problems = []

            if row.get("İlgililik Düzeyi") not in {"HIGH", "MEDIUM", "LOW", "NONE"}:
                problems.append("LLM değerlendirmesi eksik")

            detail_status = row.get("Detay Erişim Durumu")
            if detail_status in {"access_failed", "unavailable"}:
                problems.append(
                    "detay alınamadı; değerlendirme liste bilgisine dayanıyor"
                )
            elif detail_status not in {"ok", "list_only_policy"}:
                problems.append(
                    "detay kaydı eksik; değerlendirme kapsamını kontrol edin"
                )

            issue_count = int(row.get("Kontrol Gerektiren Ek Belge Sayısı") or 0)
            if issue_count:
                problems.append(
                    f"{issue_count} ek belge okunamadı veya kontrol gerektiriyor"
                )

            if problems:
                warnings.append(f"• {label}: {'; '.join(problems)}.")

        if warnings:
            body.append(
                text_block(
                    "Kontrol gereken ihaleler",
                    weight="Bolder",
                    color="Warning",
                )
            )

            # Uzun listeler kartı büyütmesin; tüm kayıtlar Excel'de bulunur.
            body.append(
                text_block(
                    "\n\n".join(warnings[:10]),
                    color="Warning",
                )
            )

            if len(warnings) > 10:
                body.append(
                    text_block(
                        f"Diğer {len(warnings) - 10} ihale için "
                        "Excel'deki Detay Erişim Durumu ve "
                        "Kontrol Gerektiren Ek Belge Sayısı "
                        "sütunlarını inceleyin.",
                        color="Warning",
                    )
                )

            body.append(
                text_block(
                    "Bu ihalelerin değerlendirmelerini, "
                    "ihale sayfası ve ilgili belgeler üzerinden "
                    "ayrıca kontrol edin."
                )
            )

        # Yereldeki dosyayı yeniden üretmeden aynen gönder.
        # OneDrive'da her çalışmanın ayrı dosyası olsun.
        file_name = output_path.name
        file_content = base64.b64encode(output_path.read_bytes()).decode("ascii")

    elif state == "empty":
        body[0]["text"] = "ℹ️ İHALELER — YENİ İHALE YOK"
        body.append(
            text_block(
                "Seçilen dönemde ve kaynaklarda ihale bulunamadı. "
                "Bu çalışma için Excel gönderilmedi."
            )
        )

    elif state == "cancelled":
        body[0]["text"] = "⏹️ İHALELER — DURDURULDU"
        body.append(
            text_block(
                "Çalışma kullanıcı tarafından durduruldu. Excel gönderilmedi.",
                color="Warning",
            )
        )

    else:
        body[0]["text"] = "❌ İHALELER — ÇALIŞMA BAŞARISIZ"
        body.append(
            text_block(
                f"Çalışma başarısız: "
                f"{report.get('error_type', 'Bilinmeyen hata')}.\n\n"
                "Sonuçlar tamamlanmadı; Excel gönderilmedi. "
                "Ayrıntılar yerel log dosyasında.",
                color="Attention",
            )
        )

    duration = (
        "1 saniyeden kısa" if elapsed < 1 else f"{elapsed // 60} dk {elapsed % 60} sn"
    )

    body.append(
        text_block(
            f"Çalışma süresi: {duration}\n\nÇalışma kimliği: {report['run_id']}",
            size="Small",
            isSubtle=True,
        )
    )

    payload = {
        "type": "message",
        "file_name": file_name,
        "file_content": file_content,
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": ("http://adaptivecards.io/schemas/adaptive-card.json"),
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": body,
                },
            }
        ],
    }

    try:
        response = requests.post(
            webhook,
            json=payload,
            timeout=(10, 60),
        )
    except requests.RequestException as ex:
        # Webhook adresini hata metniyle birlikte loga yazma.
        raise RuntimeError(
            f"Teams bağlantı hatası ({type(ex).__name__}); teslim durumu belirsiz."
        ) from None

    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Teams HTTP {response.status_code}")

    print(
        "Teams: Workflow isteği kabul edildi. "
        "Mesajın ve dosyanın teslimi akış geçmişinden doğrulanabilir."
    )
