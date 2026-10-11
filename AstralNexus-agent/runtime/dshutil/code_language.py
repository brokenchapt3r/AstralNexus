"""Lowercase extension to canonical language id.

Port of ``@deepseek-ai/dsh-util-code-language`` (commit 5badb15). Contract:
- ``language_for_path``: the extension is the text after the last dot of the
  final path segment (both separators recognized, case-insensitive). A leading
  dot is still the separator (``.env`` → ``dotenv``); an unlisted dotfile
  (``.gitignore``) and an extensionless name return ``None``.
- ``read_lang_hint_for_path``: the persisted read-card ``lang`` hint — the
  language's short name, overridden per suffix where the suffix names itself
  better (``tsx``, ``jsx``, ``tf``, ``tfvars``, ``gradle``). Persisted values
  are exact strings recorded sessions already hold and must not change.
- ``CODE_HIGHLIGHT_EXTENSIONS``: every recognized suffix, one entry each.

Upstream uses a Map so prototype keys (``foo.constructor``) miss; Python dict
lookup has the same property natively.
"""
from __future__ import annotations

from typing import Dict, List, Optional

_LANGUAGE_EXTENSIONS: Dict[str, List[str]] = {
    "typescript": ["ts", "tsx", "mts", "cts"],
    "javascript": ["js", "jsx", "mjs", "cjs"],
    "shellscript": ["sh", "bash", "zsh"],
    "fish": ["fish"],
    "json": ["json", "jsonc", "jsonl", "ndjson", "ipynb"],
    "csv": ["csv"],
    "python": ["py", "pyw", "pyi"],
    "ruby": ["rb", "rake", "gemspec"],
    "go": ["go"],
    "rust": ["rs"],
    "java": ["java"],
    "c": ["c", "h"],
    "cpp": ["cc", "cpp", "cxx", "hh", "hpp", "hxx"],
    "csharp": ["cs"],
    "kotlin": ["kt", "kts"],
    "swift": ["swift"],
    "php": ["php"],
    "yaml": ["yaml", "yml"],
    "toml": ["toml"],
    "ini": ["ini", "conf", "cfg", "properties"],
    "dotenv": ["env"],
    "log": ["log"],
    "diff": ["diff", "patch"],
    "http": ["http"],
    "markdown": ["md", "markdown"],
    "mdx": ["mdx"],
    "rst": ["rst"],
    "latex": ["tex", "sty", "cls"],
    "bibtex": ["bib"],
    "asciidoc": ["adoc"],
    "html": ["html", "htm", "xhtml"],
    "css": ["css"],
    "scss": ["scss"],
    "less": ["less"],
    "sql": ["sql"],
    "xml": ["xml", "xsd", "xsl", "xslt", "plist", "svg"],
    "lua": ["lua"],
    "bat": ["bat", "cmd"],
    "powershell": ["ps1", "psm1", "psd1"],
    "r": ["r"],
    "julia": ["jl"],
    "dart": ["dart"],
    "scala": ["scala"],
    "clojure": ["clj", "cljs", "edn"],
    "erlang": ["erl", "hrl"],
    "elixir": ["ex", "exs"],
    "haskell": ["hs"],
    "fsharp": ["fs", "fsi", "fsx"],
    "vb": ["vb"],
    "perl": ["pl", "pm"],
    "verilog": ["v"],
    "system-verilog": ["sv", "svh"],
    "graphql": ["graphql", "gql"],
    "proto": ["proto"],
    "hcl": ["tf", "tfvars", "hcl"],
    "nix": ["nix"],
    "vue": ["vue"],
    "svelte": ["svelte"],
    "make": ["makefile", "mk"],
    "cmake": ["cmake"],
    "groovy": ["gradle", "groovy"],
}

LANGUAGES: Dict[str, str] = {
    extension: language
    for language, extensions in _LANGUAGE_EXTENSIONS.items()
    for extension in extensions
}

CODE_HIGHLIGHT_EXTENSIONS: List[str] = list(LANGUAGES.keys())

READ_LANG_BY_EXTENSION: Dict[str, str] = {
    "tsx": "tsx", "jsx": "jsx", "tf": "tf", "tfvars": "tfvars", "gradle": "gradle",
}

SHORT_BY_LANGUAGE: Dict[str, str] = {
    "typescript": "ts", "javascript": "js", "shellscript": "sh", "fish": "fish",
    "json": "json", "csv": "csv", "python": "py", "ruby": "rb", "go": "go",
    "rust": "rs", "java": "java", "c": "c", "cpp": "cpp", "csharp": "cs",
    "kotlin": "kotlin", "swift": "swift", "php": "php", "yaml": "yaml",
    "toml": "toml", "ini": "ini", "dotenv": "env", "log": "log", "diff": "diff",
    "http": "http", "markdown": "md", "mdx": "mdx", "rst": "rst", "latex": "tex",
    "bibtex": "bib", "asciidoc": "adoc", "html": "html", "css": "css",
    "scss": "scss", "less": "less", "sql": "sql", "xml": "xml", "lua": "lua",
    "bat": "bat", "powershell": "ps1", "r": "r", "julia": "jl", "dart": "dart",
    "scala": "scala", "clojure": "clj", "erlang": "erl", "elixir": "ex",
    "haskell": "hs", "fsharp": "fs", "vb": "vb", "perl": "pl", "verilog": "v",
    "system-verilog": "sv", "graphql": "graphql", "proto": "proto", "hcl": "hcl",
    "nix": "nix", "vue": "vue", "svelte": "svelte", "make": "make",
    "cmake": "cmake", "groovy": "groovy",
}


def extension_for_path(path: str) -> Optional[str]:
    """Lowercase text after the last dot of the final segment; None when absent."""
    slash = max(path.rfind("/"), path.rfind("\\"))
    base = path[slash + 1:]
    dot = base.rfind(".")
    return None if dot < 0 else base[dot + 1:].lower()


def language_for_path(path: str) -> Optional[str]:
    """Canonical language id for a filename or path, case-insensitively."""
    extension = extension_for_path(path)
    return None if extension is None else LANGUAGES.get(extension)


def read_lang_hint_for_path(path: str) -> Optional[str]:
    """Persisted read-card ``lang`` hint; None when the extension maps to none."""
    extension = extension_for_path(path)
    if extension is None:
        return None
    canonical = LANGUAGES.get(extension)
    if canonical is None:
        return None
    return READ_LANG_BY_EXTENSION.get(extension, SHORT_BY_LANGUAGE[canonical])
