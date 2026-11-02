"""PDF, Word, Excel ve ZIP belgelerinden metin çıkarma."""

import shutil
import tempfile
import zipfile
from pathlib import Path

import pandas as pd

MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def extract_zip_text(path):
    """ZIP içindeki desteklenen belgelerden metin çıkarır."""
    supported = {
        ".pdf",
        ".doc",
        ".docx",
        ".xlsx",
        ".txt",
        ".csv",
        ".htm",
        ".html",
        ".json",
    }
    # Çok büyük arşivlerin işlemi kontrolsüz büyütmesini önle.
    max_files = 200
    max_total_bytes = 200 * 1024 * 1024
    blocks = []
    with zipfile.ZipFile(path) as archive:
        members = [
            item
            for item in archive.infolist()
            if not item.is_dir()
            and not item.filename.startswith("__MACOSX/")
            and not Path(item.filename).name.startswith("._")
        ]
        if not members:
            return "[BOS ZIP - arsivde dosya bulunamadi]"
        if len(members) > max_files:
            return f"[ZIP LIMITI - {len(members)} dosya var; sinir {max_files}]"
        total_bytes = sum(item.file_size for item in members)
        if total_bytes > max_total_bytes:
            return "[ZIP LIMITI - acilmis toplam boyut 200 MB sinirini asiyor]"
        with tempfile.TemporaryDirectory(prefix="nato_zip_") as folder:
            for index, item in enumerate(members, start=1):
                extension = Path(item.filename).suffix.lower()
                if extension not in supported:
                    body = f"[DESTEKLENMEYEN FORMAT: {extension or 'uzantisiz'}]"
                elif item.flag_bits & 1:
                    body = "[SIFRELI DOSYA - metin okunamadi]"
                else:
                    # Arşivdeki klasör yollarını kullanmadan benzersiz ad ver.
                    target = Path(folder) / f"document_{index}{extension}"
                    try:
                        with archive.open(item) as source:
                            with target.open("wb") as destination:
                                shutil.copyfileobj(source, destination)
                        body = (
                            extract_text(target).strip()
                            or "[BOS METIN - dosyadan metin cikarilamadi]"
                        )
                    except Exception as ex:
                        body = f"[ZIP UYESI OKUNAMADI: {ex}]"
                blocks.append(f"--- ZIP ICINDEKI DOSYA: {item.filename} ---\n{body}")
    return "\n\n".join(blocks)


def extract_text(p: Path) -> str:
    try:
        e = p.suffix.lower()
        if e == ".zip":
            return extract_zip_text(p)
        if e == ".pdf":
            try:
                import pymupdf
            except ImportError:  # eski sürümlerde paket "fitz" adıyla geliyor
                import fitz as pymupdf
            with pymupdf.open(p) as doc:
                return "\n".join(pg.get_text() for pg in doc)
        if e == ".doc":
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            word = None
            document = None
            try:
                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                word.DisplayAlerts = 0
                word.AutomationSecurity = 3  # Makroları çalıştırma
                document = word.Documents.Open(
                    FileName=str(p.resolve()),
                    ConfirmConversions=False,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    Visible=False,
                )
                return document.Content.Text.replace("\r", "\n").replace("\x07", "\t")
            finally:
                try:
                    if document is not None:
                        document.Close(SaveChanges=0)
                finally:
                    try:
                        if word is not None:
                            word.Quit(SaveChanges=0)
                    finally:
                        pythoncom.CoUninitialize()
        if e == ".docx":
            import docx
            from docx.oxml.ns import qn

            d = docx.Document(p)
            roots = [d.element.body]
            for sec in d.sections:
                roots += [sec.header._element, sec.footer._element]
            lines = []
            for root in roots:
                for para in root.iter(qn("w:p")):
                    if any(a.tag == MC_FALLBACK for a in para.iterancestors()):
                        continue  # metin kutularının yedek kopyası (çift okumayı önler)
                    line = "".join(t.text or "" for t in para.iter(qn("w:t")))
                    if line.strip():
                        lines.append(line)
            return "\n".join(lines)
        if e == ".xlsx":
            return "\n".join(
                x.to_csv(index=False, header=False)
                for x in pd.read_excel(p, sheet_name=None, header=None).values()
            )
        if e in (".txt", ".csv", ".htm", ".html", ".json"):
            return p.read_text(errors="ignore")
    except Exception as ex:
        return f"[METIN CIKARMA HATASI: {ex}]"
    return f"[DESTEKLENMEYEN FORMAT: {p.suffix.lower() or 'uzantisiz'}]"
