# -*- coding: utf-8 -*-
"""用户资料库：本地导入词表、教材和电子书，优先用本地资料答题。

设计原则：

1. 上传、解析、切分、检索全部在本地完成，不调用大模型。
2. 结构化词条可以保守地直接匹配选项；普通教材正文只用于检索片段。
3. 只有资料无法确定答案时，才把当前题目和少量相关片段交给大模型。
4. 个人资料保存在 LOCALAPPDATA，不进入分享包。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from .glossary import match_option, meaning_hint
from .matcher import (
    target_word,
    topic_mode,
    topic_options,
    topic_remark,
    topic_stem,
)

DATA_VERSION = 1
DATA_NAME = "materials.json"
SUPPORTED_SUFFIXES = {".txt", ".md", ".csv", ".json", ".docx", ".epub", ".pdf"}
_CJK = re.compile(r"[\u3400-\u9fff]")
_ENGLISH_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]{1,}")
_POS = r"(?:n|v|vt|vi|adj|adv|prep|conj|pron|num|art|aux|phr)\."
_GLOSSARY_POS_LINE = re.compile(
    rf"^\s*([A-Za-z][A-Za-z'’.-]*(?:\s+[A-Za-z][A-Za-z'’.-]*){{0,3}}?)"
    rf"\s+({_POS})\s*([^\n]{{1,300}})\s*$",
    re.IGNORECASE,
)
_GLOSSARY_DELIM_LINE = re.compile(
    r"^\s*([A-Za-z][A-Za-z'’ -]{0,60}?)\s*(?:\t+|\s*\|\s*|\s*=\s*|\s*:\s*)([^\n]{1,300})\s*$"
)
_GLOSSARY_SPACE_LINE = re.compile(
    r"^\s*([A-Za-z][A-Za-z'’.-]{0,60})\s+([\u3400-\u9fff][^\n]{0,299})\s*$"
)
_PHRASE_LINE = re.compile(
    r"^\s*([^=|→]{1,80}(?:[\u3400-\u9fff][^=|→]{0,78})?)\s*(?:=|→|->|\|)\s*"
    r"([A-Za-z][A-Za-z'’ -]{1,100})\s*$"
)


class MaterialImportError(RuntimeError):
    """资料文件无法读取或没有可索引文字。"""


@dataclass(frozen=True)
class ImportResult:
    source_id: str
    name: str
    entries: int
    chunks: int
    chars: int
    duplicate: bool = False

    @property
    def message(self) -> str:
        if self.duplicate:
            return f"资料已存在，跳过重复导入：{self.name}"
        return (
            f"已导入 {self.name}：词条 {self.entries} 条，"
            f"正文片段 {self.chunks} 段，共 {self.chars} 个字符"
        )


@dataclass(frozen=True)
class _Extracted:
    text: str
    glossary: dict[str, list[str]]
    phrases: dict[str, list[str]]


class _HTMLText(HTMLParser):
    """把 EPUB/XHTML 转成可索引的纯文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MaterialImportError("无法识别文本编码，请另存为 UTF-8 后重试")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _clean_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _add_meaning(target: dict[str, list[str]], word: str, meaning: str) -> None:
    key = re.sub(r"\s+", " ", str(word or "")).strip().lower()
    value = re.sub(r"\s+", " ", str(meaning or "")).strip()
    if not key or not value or not _CJK.search(value):
        return
    bucket = target.setdefault(key, [])
    if value not in bucket:
        bucket.append(value[:500])


def _add_phrase(target: dict[str, list[str]], chinese: str, english: str) -> None:
    key = re.sub(r"\s+", "", str(chinese or "")).strip().lower()
    value = re.sub(r"\s+", " ", str(english or "")).strip().lower()
    if not key or not value or not _CJK.search(key):
        return
    bucket = target.setdefault(key, [])
    if value not in bucket:
        bucket.append(value[:200])


def _word_forms(word: str) -> Iterable[str]:
    """给出常见屈折形式的基础词候选，不依赖联网词形还原。"""
    value = re.sub(r"\s+", " ", str(word or "")).strip().lower()
    if not value:
        return
    yield value
    candidates: list[str] = []
    if value.endswith("ies") and len(value) > 4:
        candidates.append(f"{value[:-3]}y")
    if value.endswith("ied") and len(value) > 4:
        candidates.append(f"{value[:-3]}y")
    if value.endswith("ing") and len(value) > 5:
        stem = value[:-3]
        candidates.append(stem)
        if len(stem) > 2 and stem[-1] == stem[-2]:
            candidates.append(stem[:-1])
        candidates.append(f"{stem}e")
    if value.endswith("ed") and len(value) > 4:
        stem = value[:-2]
        candidates.append(stem)
        if len(stem) > 2 and stem[-1] == stem[-2]:
            candidates.append(stem[:-1])
        candidates.append(f"{stem}e")
    if value.endswith("es") and len(value) > 4:
        candidates.append(value[:-2])
    if value.endswith("s") and len(value) > 3:
        candidates.append(value[:-1])
    seen = {value}
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _parse_loose_entries(text: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    glossary: dict[str, list[str]] = {}
    phrases: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        phrase = _PHRASE_LINE.match(line)
        if phrase:
            left, right = phrase.groups()
            if _CJK.search(left) and not _CJK.search(right):
                _add_phrase(phrases, left, right)
                continue
            if _CJK.search(right) and not _CJK.search(left):
                _add_phrase(phrases, right, left)
                continue
        match = _GLOSSARY_POS_LINE.match(line)
        if match:
            word, pos, meaning = match.groups()
            _add_meaning(glossary, word, f"{pos} {meaning}".strip())
            continue
        match = _GLOSSARY_DELIM_LINE.match(line)
        if match:
            word, meaning = match.groups()
            _add_meaning(glossary, word, meaning)
            continue
        match = _GLOSSARY_SPACE_LINE.match(line)
        if match:
            word, meaning = match.groups()
            _add_meaning(glossary, word, meaning)
    return glossary, phrases


def _csv_text(text: str) -> _Extracted:
    glossary: dict[str, list[str]] = {}
    phrases: dict[str, list[str]] = {}
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return _Extracted("", {}, {})

    headers = [str(cell or "").strip().lower() for cell in rows[0]]
    word_keys = {"word", "term", "单词", "词汇", "英文"}
    meaning_keys = {"meaning", "definition", "释义", "中文", "解释", "answer", "答案"}
    chinese_keys = {"chinese", "中文", "汉语", "短语中文"}
    english_keys = {"english", "英文", "短语英文"}
    word_col = next((i for i, value in enumerate(headers) if value in word_keys), None)
    meaning_col = next((i for i, value in enumerate(headers) if value in meaning_keys), None)
    chinese_col = next((i for i, value in enumerate(headers) if value in chinese_keys), None)
    english_col = next((i for i, value in enumerate(headers) if value in english_keys), None)

    start = 1 if word_col is not None or chinese_col is not None else 0
    for row in rows[start:]:
        if not row:
            continue
        if word_col is not None and word_col < len(row) and meaning_col is not None and meaning_col < len(row):
            _add_meaning(glossary, row[word_col], row[meaning_col])
        if chinese_col is not None and chinese_col < len(row) and english_col is not None and english_col < len(row):
            _add_phrase(phrases, row[chinese_col], row[english_col])
        if word_col is None and len(row) >= 2:
            _add_meaning(glossary, row[0], row[-1])
    text_value = "\n".join(" | ".join(cell for cell in row if cell) for row in rows)
    fallback_glossary, fallback_phrases = _parse_loose_entries(text_value)
    for key, values in fallback_glossary.items():
        for value in values:
            _add_meaning(glossary, key, value)
    for key, values in fallback_phrases.items():
        for value in values:
            _add_phrase(phrases, key, value)
    return _Extracted(text_value, glossary, phrases)


def _json_text(text: str) -> _Extracted:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MaterialImportError(f"JSON 格式错误：{exc}") from exc
    glossary: dict[str, list[str]] = {}
    phrases: dict[str, list[str]] = {}

    def consume(item: Any) -> None:
        if isinstance(item, dict):
            word = item.get("word") or item.get("term") or item.get("单词") or item.get("英文")
            meaning = (
                item.get("meaning")
                or item.get("definition")
                or item.get("释义")
                or item.get("中文")
                or item.get("answer")
                or item.get("答案")
            )
            if word and meaning:
                values = meaning if isinstance(meaning, list) else [meaning]
                for value in values:
                    _add_meaning(glossary, str(word), str(value))
            chinese = item.get("chinese") or item.get("中文") or item.get("短语中文")
            english = item.get("english") or item.get("英文") or item.get("短语英文")
            if chinese and english:
                values = english if isinstance(english, list) else [english]
                for value in values:
                    _add_phrase(phrases, str(chinese), str(value))

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (str, list, tuple)):
                values = value if isinstance(value, (list, tuple)) else [value]
                for item in values:
                    _add_meaning(glossary, str(key), str(item))
            else:
                consume(value)
    elif isinstance(data, list):
        for item in data:
            consume(item)

    rendered = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
    if not glossary and not phrases:
        glossary, phrases = _parse_loose_entries(str(rendered))
    return _Extracted(str(rendered), glossary, phrases)


def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise MaterialImportError("DOCX 文件损坏或格式不受支持") from exc
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = [node.text or "" for node in paragraph.iter(f"{namespace}t")]
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _epub_text(path: Path) -> str:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise MaterialImportError("EPUB 文件损坏或受 DRM 保护") from exc
    chunks: list[str] = []
    with archive:
        names = sorted(
            name
            for name in archive.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm"))
        )
        for name in names:
            parser = _HTMLText()
            try:
                parser.feed(_decode(archive.read(name)))
            except Exception:
                continue
            value = _clean_text(parser.text())
            if value:
                chunks.append(value)
    return "\n\n".join(chunks)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError as exc:
            raise MaterialImportError(
                "当前运行环境没有 PDF 解析组件，请先重新运行 prepare.bat 安装依赖"
            ) from exc
    try:
        reader = PdfReader(str(path))
        pages: list[str] = []
        for page in reader.pages:
            try:
                value = page.extract_text() or ""
            except Exception:
                value = ""
            if value.strip():
                pages.append(value)
    except Exception as exc:
        raise MaterialImportError(f"PDF 读取失败：{exc}") from exc
    if not pages:
        raise MaterialImportError(
            "PDF 没有可提取文字，可能是扫描版；请先用 OCR 转成可搜索 PDF 或文本文件"
        )
    return "\n\n".join(pages)


def _extract(path: Path) -> _Extracted:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise MaterialImportError(
            f"不支持 {suffix or '无扩展名'} 文件；可用 TXT、MD、CSV、JSON、DOCX、EPUB、PDF"
        )
    if suffix in {".txt", ".md"}:
        text = _decode(path.read_bytes())
        glossary, phrases = _parse_loose_entries(text)
        return _Extracted(text, glossary, phrases)
    if suffix == ".csv":
        return _csv_text(_decode(path.read_bytes()))
    if suffix == ".json":
        return _json_text(_decode(path.read_bytes()))
    if suffix == ".docx":
        text = _docx_text(path)
        glossary, phrases = _parse_loose_entries(text)
        return _Extracted(text, glossary, phrases)
    if suffix == ".epub":
        text = _epub_text(path)
        glossary, phrases = _parse_loose_entries(text)
        return _Extracted(text, glossary, phrases)
    text = _pdf_text(path)
    glossary, phrases = _parse_loose_entries(text)
    return _Extracted(text, glossary, phrases)


def _chunks(text: str, *, max_chars: int = 1200, overlap: int = 120) -> list[str]:
    text = _clean_text(text)
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [part.strip() for part in text.splitlines() if part.strip()]
    out: list[str] = []
    current = ""
    for paragraph in paragraphs:
        paragraph = re.sub(r"\n+", " ", paragraph)
        if len(paragraph) > max_chars:
            sentences = re.split(r"(?<=[。！？.!?])\s+", paragraph)
            pieces: list[str] = []
            buffer = ""
            for sentence in sentences:
                if not sentence:
                    continue
                if buffer and len(buffer) + len(sentence) + 1 > max_chars:
                    pieces.append(buffer)
                    buffer = sentence
                else:
                    buffer = f"{buffer} {sentence}".strip()
            if buffer:
                pieces.append(buffer)
            paragraphs_to_add = pieces
        else:
            paragraphs_to_add = [paragraph]
        for piece in paragraphs_to_add:
            if current and len(current) + len(piece) + 1 > max_chars:
                out.append(current)
                tail = current[-overlap:] if overlap and len(current) > overlap else ""
                current = f"{tail} {piece}".strip()
            else:
                current = f"{current}\n{piece}".strip()
    if current:
        out.append(current)
    return [chunk for chunk in out if len(chunk) >= 20]


class MaterialLibrary:
    """本地资料索引。一个实例对应一个目录中的 materials.json。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        logger: Any = None,
        enabled: bool = True,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / DATA_NAME
        self.log = logger or (lambda _message: None)
        self.enabled = enabled
        self._data = self._empty()
        self.load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": DATA_VERSION,
            "sources": [],
            "glossary": {},
            "phrases": {},
            "chunks": [],
        }

    def load(self) -> None:
        if not self.path.exists():
            self._data = self._empty()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("根节点不是对象")
            self._data = {
                "version": int(data.get("version") or DATA_VERSION),
                "sources": list(data.get("sources") or []),
                "glossary": dict(data.get("glossary") or {}),
                "phrases": dict(data.get("phrases") or {}),
                "chunks": list(data.get("chunks") or []),
            }
        except Exception as exc:
            self._data = self._empty()
            self.log(f"⚠️ 资料库索引损坏，已按空资料库启动：{exc}")

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def stats(self) -> dict[str, int]:
        return {
            "sources": len(self._data["sources"]),
            "entries": len(self._data["glossary"]),
            "phrases": len(self._data["phrases"]),
            "chunks": len(self._data["chunks"]),
        }

    def sources(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._data["sources"]]

    def import_file(self, file_path: str | os.PathLike[str]) -> ImportResult:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise MaterialImportError(f"文件不存在：{path}")
        digest = _file_digest(path)
        source_id = f"{path.stem[:40]}-{digest}"
        for source in self._data["sources"]:
            if source.get("id") == source_id:
                return ImportResult(
                    source_id=source_id,
                    name=str(source.get("name") or path.name),
                    entries=int(source.get("entries") or 0),
                    chunks=int(source.get("chunks") or 0),
                    chars=int(source.get("chars") or 0),
                    duplicate=True,
                )

        extracted = _extract(path)
        text = _clean_text(extracted.text)
        chunk_values = _chunks(text)
        if not extracted.glossary and not extracted.phrases and not chunk_values:
            raise MaterialImportError("文件中没有识别到可用的中文词条或正文")

        source_name = path.name
        for word, meanings in extracted.glossary.items():
            bucket = self._data["glossary"].setdefault(word, [])
            for meaning in meanings:
                item = {"meaning": meaning, "source": source_id}
                if item not in bucket:
                    bucket.append(item)
        for chinese, english_values in extracted.phrases.items():
            bucket = self._data["phrases"].setdefault(chinese, [])
            for english in english_values:
                item = {"english": english, "source": source_id}
                if item not in bucket:
                    bucket.append(item)
        for index, value in enumerate(chunk_values):
            self._data["chunks"].append(
                {
                    "id": f"{source_id}:{index}",
                    "source": source_id,
                    "source_name": source_name,
                    "text": value,
                }
            )
        self._data["sources"].append(
            {
                "id": source_id,
                "name": source_name,
                "path": str(path),
                "suffix": path.suffix.lower(),
                "imported_at": int(time.time()),
                "chars": len(text),
                "entries": len(extracted.glossary),
                "phrases": len(extracted.phrases),
                "chunks": len(chunk_values),
            }
        )
        self.save()
        return ImportResult(
            source_id=source_id,
            name=source_name,
            entries=len(extracted.glossary),
            chunks=len(chunk_values),
            chars=len(text),
        )

    def remove_source(self, source_id: str) -> bool:
        before = len(self._data["sources"])
        self._data["sources"] = [
            item for item in self._data["sources"] if item.get("id") != source_id
        ]
        if len(self._data["sources"]) == before:
            return False
        for section in ("glossary", "phrases"):
            for key in list(self._data[section]):
                values = [
                    item
                    for item in self._data[section][key]
                    if item.get("source") != source_id
                ]
                if values:
                    self._data[section][key] = values
                else:
                    del self._data[section][key]
        self._data["chunks"] = [
            item for item in self._data["chunks"] if item.get("source") != source_id
        ]
        self.save()
        return True

    def clear(self) -> None:
        self._data = self._empty()
        self.save()

    def lookup(self, word: str) -> list[str]:
        if not self.enabled:
            return []
        key = re.sub(r"\s+", " ", str(word or "")).strip().lower()
        if not key:
            return []
        for candidate in _word_forms(key):
            out: list[str] = []
            for item in self._data["glossary"].get(candidate, []):
                meaning = str(item.get("meaning") or "").strip()
                if meaning and meaning not in out:
                    out.append(meaning)
            if out:
                return out
        return []

    def direct_choice(self, topic: dict[str, Any]) -> Any | None:
        """结构化词条与选项都足够明确时直接返回 answer_tag。"""
        if not self.enabled or topic_mode(topic) == 32:
            return None
        word = target_word(topic)
        if not word:
            return None
        meanings = self.lookup(word)
        index = match_option(meanings, topic_options(topic))
        if index is None:
            return None
        options = topic_options(topic)
        return options[index].get("answer_tag", index)

    def direct_phrase(self, topic: dict[str, Any]) -> str | None:
        """匹配 `中文短语 = english phrase` 形式的本地资料。"""
        if not self.enabled or topic_mode(topic) != 32:
            return None
        remark = re.sub(r"\s+", "", str(topic_remark(topic) or "")).lower()
        if not remark:
            return None
        candidates = [
            str(item.get("english") or "").strip().lower()
            for item in self._data["phrases"].get(remark, [])
        ]
        options = [
            str(option.get("content") or "").strip().lower()
            for option in topic_options(topic)
        ]
        pool = {item for item in options if item}
        if not candidates or not pool:
            return None
        slots = len(re.findall(r"_+", topic_stem(topic)))
        answers: list[str] = []
        for candidate in candidates:
            tokens = [token.lower() for token in _ENGLISH_WORD.findall(candidate)]
            if not tokens or not all(token in pool for token in tokens):
                continue
            if slots and len(tokens) != slots:
                continue
            answer = ",".join(tokens)
            if answer not in answers:
                answers.append(answer)
        return answers[0] if len(answers) == 1 else None

    def _query_terms(self, topic: dict[str, Any]) -> list[str]:
        text = " ".join(
            [
                topic_stem(topic),
                str(topic_remark(topic) or ""),
                " ".join(str(item.get("content") or "") for item in topic_options(topic)),
            ]
        )
        terms: list[str] = []
        word = target_word(topic)
        if word:
            terms.append(word.lower())
        for item in _ENGLISH_WORD.findall(text):
            value = item.lower().strip("'’-")
            if len(value) >= 3 and value not in terms:
                terms.append(value)
        for item in re.findall(r"[\u3400-\u9fff]{2,8}", text):
            if item not in terms:
                terms.append(item)
        return terms[:24]

    def context_for_topic(
        self,
        topic: dict[str, Any],
        *,
        limit: int = 4,
        max_chars: int = 2400,
    ) -> str:
        """检索与当前题目最相关的词条和正文片段，供模型兜底。"""
        if not self.enabled:
            return ""
        parts: list[str] = []
        word_meanings = self.lookup(target_word(topic))
        glossary = meaning_hint(word_meanings, limit=5)
        if glossary:
            parts.append(f"本地词条：{glossary}")

        terms = self._query_terms(topic)
        if terms and self._data["chunks"]:
            scored: list[tuple[float, dict[str, Any]]] = []
            for chunk in self._data["chunks"]:
                value = str(chunk.get("text") or "")
                lower = value.lower()
                score = 0.0
                for term in terms:
                    count = lower.count(term)
                    if count:
                        weight = 1.5 if len(term) >= 8 else 1.0
                        if _CJK.search(term):
                            weight += 0.5
                        score += min(count, 3) * weight
                if score:
                    scored.append((score, chunk))
            scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
            selected_ids: set[str] = set()
            for score, chunk in scored:
                del score
                chunk_id = str(chunk.get("id") or "")
                if not chunk_id or chunk_id in selected_ids:
                    continue
                selected_ids.add(chunk_id)
                name = str(chunk.get("source_name") or "资料")
                value = str(chunk.get("text") or "").strip()
                parts.append(f"《{name}》：{value}")
                if len(selected_ids) >= max(1, int(limit)):
                    break

        if not parts:
            return ""
        result = "\n\n".join(parts)
        return result[: max(300, int(max_chars))].rstrip()


def iter_supported_files(folder: str | os.PathLike[str]) -> Iterable[Path]:
    """遍历资料目录中的受支持文件，供批量和测试使用。"""
    root = Path(folder)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path
