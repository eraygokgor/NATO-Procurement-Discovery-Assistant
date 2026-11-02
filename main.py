"""Programın başlangıç noktası; python main.py ile çalıştırın."""

import os
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import OpenAI

import analysis
from analysis import build_response_format, evaluate_tenders
from config import (
    DATA,
    DAYS_BACK,
    FRESH,
    LIMIT,
    LLM_URL,
    RECHECK_REFS,
    RUNS_DIR,
    validate_settings,
)
from notifications import send_run_notification
from sources import S, close_browsers, collect_details, fetch_tender_list
from storage import Tee, export_excel, prepare_run


def main(report):
    """İhaleleri toplar, değerlendirir ve çalışma kimliğiyle Excel'e yazar."""
    program_started = time.monotonic()
    analysis.last_llm_request = 0

    try:
        system_prompt = validate_settings()
        response_format = build_response_format(system_prompt)

        df, targets = fetch_tender_list()
        if targets.empty:
            print("Seçilen kaynaklarda işlenecek ihale bulunamadı.")
            report["status"] = "empty"
            return

        prepare_run()

        nspa_rows = df.loc[df["source"] == "nspa"]
        ref2url = dict(zip(nspa_rows["reference"], nspa_rows["detailsUrl"]))

        try:
            collect_details(targets)
        finally:
            close_browsers()

        with OpenAI(
            base_url=LLM_URL,
            api_key=os.environ["LLM_API_KEY"],
            max_retries=0,
        ) as client:
            evaluate_tenders(
                targets,
                ref2url,
                client,
                system_prompt,
                response_format,
            )

        output_path = RUNS_DIR / (f"nato_ihaleleri_{report['run_id']}.xlsx")
        report["output_path"] = export_excel(targets, ref2url, output_path)
        report["status"] = "completed"

    finally:
        S.close()
        elapsed = int(time.monotonic() - program_started)
        print(
            f"Toplam çalışma süresi: "
            f"{elapsed // 3600} h "
            f"{(elapsed % 3600) // 60} m "
            f"{elapsed % 60} s"
        )


if __name__ == "__main__":
    load_dotenv()

    started = time.monotonic()
    now = datetime.now(timezone(timedelta(hours=3)))
    today = now.date()

    if DAYS_BACK is None:
        period = "Dönem: tüm tarihler"
    elif (
        isinstance(DAYS_BACK, int) and not isinstance(DAYS_BACK, bool) and DAYS_BACK > 0
    ):
        period = (
            f"Dönem: {today - timedelta(days=DAYS_BACK):%d.%m.%Y}"
            f" – {today - timedelta(days=1):%d.%m.%Y}"
        )
    else:
        period = "Dönem: geçersiz DAYS_BACK ayarı"

    # Mikrosaniye aynı saniyede başlayan çalışmaların çakışmasını önler.
    run_id = now.strftime("%Y%m%d_%H%M%S_%f")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "status": "failed",
        "period": period,
        "run_id": run_id,
    }

    log_dir = DATA / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{run_id}.log"
    exit_code = 0

    with log_path.open("w", encoding="utf-8") as log_file:
        with (
            redirect_stdout(Tee(sys.stdout, log_file)),
            redirect_stderr(Tee(sys.stderr, log_file)),
        ):
            print(f"Başlangıç: {now:%Y-%m-%d %H:%M:%S}")
            print(f"Log dosyası: {log_path}")
            print(
                f"Ayarlar: FRESH={FRESH}, DAYS_BACK={DAYS_BACK}, "
                f"LIMIT={LIMIT}, RECHECK_REFS={RECHECK_REFS}"
            )

            try:
                main(report)
            except KeyboardInterrupt:
                report["status"] = "cancelled"
                exit_code = 130
                print("\nÇalışma kullanıcı tarafından durduruldu.")
            except Exception as ex:
                report["status"] = "failed"
                report["error_type"] = type(ex).__name__
                exit_code = 1
                traceback.print_exc()
            finally:
                elapsed = int(time.monotonic() - started)
                print(f"Bitiş: {datetime.now():%Y-%m-%d %H:%M:%S}")
                log_file.flush()

                try:
                    send_run_notification(report, elapsed, log_path)
                except Exception as ex:
                    print(f"TEAMS BİLDİRİM HATASI: {ex}")
                    if exit_code == 0:
                        exit_code = 2

    raise SystemExit(exit_code)
