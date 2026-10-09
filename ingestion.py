import os
import zipfile
from pathlib import Path
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from pypdf import PdfReader
import docx
from roles import detect_role, detect_kind

def parse_pdf(file_path: Path) -> str:
    """Извлечение текста из PDF файлов."""
    try:
        reader = PdfReader(file_path)
        text = "\n".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        return f"[Ошибка чтения PDF {file_path.name}: {e}]"

def parse_docx(file_path: Path) -> str:
    """Извлечение текста из Word (DOCX) документов."""
    try:
        doc = docx.Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except Exception as e:
        return f"[Ошибка чтения DOCX {file_path.name}: {e}]"

def parse_html(file_path: Path) -> str:
    """Извлечение чистого текста из HTML страниц без тегов."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(content, "html.parser")
        return soup.get_text(separator="\n", strip=True)
    except Exception as e:
        return f"[Ошибка чтения HTML {file_path.name}: {e}]"

def process_file(file_path: Path, relative_path: str) -> Dict[str, Any]:
    """Универсальный парсер под все типы файлов."""
    ext = file_path.suffix.lower()
    
    if ext == ".pdf":
        text = parse_pdf(file_path)
    elif ext in [".docx", ".doc"]:
        text = parse_docx(file_path)
    elif ext in [".html", ".htm"]:
        text = parse_html(file_path)
    else:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        
    return {
        "path": relative_path,
        "content": text,
        "role": detect_role(relative_path),
        "kind": detect_kind(relative_path)
    }

def extract_and_process_zip(zip_path: Path, extract_to: Path) -> List[Dict[str, Any]]:
    """Распаковывает ZIP-архив и обрабатывает всё внутреннее содержимое."""
    processed_files = []
    
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(extract_to)
        
    for root, _, files in os.walk(extract_to):
        for file in files:
            full_path = Path(root) / file
            # Игнорируем скрытые и системные файлы
            if file.startswith(".") or "__pycache__" in str(full_path):
                continue
                
            rel_path = str(full_path.relative_to(extract_to)).replace("\\", "/")
            try:
                processed_files.append(process_file(full_path, rel_path))
            except Exception:
                continue
                
    return processed_files