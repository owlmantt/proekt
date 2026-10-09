from pathlib import Path

# Карта расширений под ключевые роли
ROLES_MAP = {
    "frontend": ["js", "jsx", "ts", "tsx", "vue", "svelte", "html", "css", "scss", "less"],
    "flutter": ["dart"],
    "backend": ["py", "go", "java", "rs", "cpp", "c", "cs", "php", "rb", "sql"],
    "devops": ["dockerfile", "yaml", "yml", "tf", "sh", "env", "ini", "conf"],
    "docs": ["pdf", "docx", "doc", "md", "txt", "rtf"]
}

def detect_role(file_path: str) -> str:
    """Автоматически определяет роль на основе пути и расширения файла."""
    path = Path(file_path)
    ext = path.suffix.lower().lstrip(".")
    path_str = str(path).lower()

    # Специфичные проверки по путям
    if any(k in path_str for k in ["docker", "k8s", "terraform", ".github", "ci-cd"]):
        return "devops"
    
    for role, exts in ROLES_MAP.items():
        if ext in exts:
            return role
            
    return "general"

def detect_kind(file_path: str) -> str:
    """Определяет смысловой тип содержимого."""
    ext = Path(file_path).suffix.lower().lstrip(".")
    file_name = Path(file_path).name.lower()

    if ext in ["pdf", "docx", "doc", "md", "txt"]:
        return "documentation"
    elif ext in ["json", "yaml", "yml"] and ("openapi" in file_name or "swagger" in file_name):
        return "openapi"
    elif ext in ["py", "ts", "js", "dart", "go", "java", "cs", "cpp"]:
        return "source_code"
    
    return "file_chunk"


# Что роль имеет право видеть. Это виды чанков, а не расширения файлов.
API_KINDS = frozenset({"openapi_info", "openapi_endpoint", "openapi_schema"})
CODE_KINDS = frozenset({"function", "class", "method", "constant", "module"})

ROLE_PACKS: dict[str, dict[str, frozenset[str]]] = {
    "frontend": {"kinds": API_KINDS},
    "flutter": {"kinds": API_KINDS},
    "backend": {
        "kinds": API_KINDS | CODE_KINDS,
        "source_kinds": frozenset({"source_code"}),
    },
    "devops": {
        "file_roles": frozenset({"devops"}),
        "suffixes": frozenset({".yml", ".yaml", ".tf", ".sh", ".toml", ".ini", ".cfg"}),
        "names": frozenset({"dockerfile", "makefile", "procfile", ".dockerignore", ".gitignore"}),
    },
}


def allows(meta: dict, role: str) -> bool:
    """Чанк входит в пакет роли. all и пустая роль видят всё."""
    normalized = (role or "all").strip().lower()
    if normalized == "all":
        return True
    pack = ROLE_PACKS.get(normalized)
    if pack is None:
        return False

    kind = str(meta.get("kind") or "")
    if kind in pack.get("kinds", ()):
        return True

    source_kind = str(meta.get("source_kind") or "")
    if source_kind and source_kind in pack.get("source_kinds", ()):
        return True

    file_role = str(meta.get("role") or "")
    if file_role and file_role in pack.get("file_roles", ()):
        return True

    path = Path(str(meta.get("path") or ""))
    if path.suffix.lower() in pack.get("suffixes", ()):
        return True
    if path.name.lower() in pack.get("names", ()):
        return True
    return False


def chroma_kind_where(role: str) -> dict | None:
    """Фильтр Chroma, если пакет роли задан только списком kind."""
    normalized = (role or "").strip().lower()
    pack = ROLE_PACKS.get(normalized)
    if not pack or set(pack) != {"kinds"}:
        return None
    kinds = pack.get("kinds") or frozenset()
    if not kinds:
        return None
    return {"kind": {"$in": sorted(kinds)}}