# -*- coding: utf-8 -*-
"""本地资料库测试：文件解析、去重、检索和解题器优先级。"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from vocabgo.bank import Bank
from vocabgo.llm import LlmHelper
from vocabgo.materials import MaterialImportError, MaterialLibrary
from vocabgo.solver import Solver


class MaterialLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.library = MaterialLibrary(self.folder / "materials", logger=lambda _m: None)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.folder / name
        path.write_text(text, encoding=encoding)
        return path

    def test_imports_glossary_phrase_and_context(self) -> None:
        path = self.write(
            "cet4.txt",
            "ubiquitous adj. 普遍存在的；无处不在的\n"
            "许多例子 = many examples\n\n"
            "Ubiquitous technology appears everywhere in modern cities.",
        )
        result = self.library.import_file(path)

        self.assertEqual(result.entries, 1)
        self.assertEqual(result.chunks, 1)
        self.assertEqual(self.library.lookup("ubiquitous"), ["adj. 普遍存在的；无处不在的"])
        self.assertEqual(
            self.library.direct_choice(
                {
                    "topic_mode": 31,
                    "stem": {"content": "ubiquitous"},
                    "options": [
                        {"content": "n. 天气", "answer_tag": 4},
                        {"content": "adj. 普遍存在的", "answer_tag": 7},
                    ],
                }
            ),
            7,
        )
        self.assertEqual(
            self.library.direct_phrase(
                {
                    "topic_mode": 32,
                    "stem": {"content": "___ ___", "remark": "许多例子"},
                    "options": [
                        {"content": "many", "answer_tag": 1},
                        {"content": "examples", "answer_tag": 2},
                    ],
                }
            ),
            "many,examples",
        )
        hint = self.library.context_for_topic(
            {
                "topic_mode": 31,
                "stem": {"content": "ubiquitous"},
                "options": [{"content": "adj. 普遍存在的"}],
            }
        )
        self.assertIn("Ubiquitous technology", hint)

    def test_gb18030_and_duplicate_import(self) -> None:
        path = self.write("词表.txt", "abandon v. 放弃；抛弃", encoding="gb18030")
        first = self.library.import_file(path)
        second = self.library.import_file(path)

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.library.lookup("abandon"), ["v. 放弃；抛弃"])
        self.assertEqual(self.library.stats()["sources"], 1)

    def test_json_mapping(self) -> None:
        path = self.write(
            "dictionary.json",
            json.dumps({"concise": ["adj. 简洁的", "adj. 简明的"]}, ensure_ascii=False),
        )
        self.library.import_file(path)
        self.assertEqual(
            self.library.lookup("concise"),
            ["adj. 简洁的", "adj. 简明的"],
        )

    def test_common_inflected_form_falls_back_to_base_word(self) -> None:
        path = self.write("verbs.txt", "twist v. 使弯曲；扭曲")
        self.library.import_file(path)
        self.assertEqual(self.library.lookup("twisted"), ["v. 使弯曲；扭曲"])

    def test_docx_paragraphs_are_extracted_without_external_dependency(self) -> None:
        path = self.folder / "book.docx"
        xml = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>diligent adj. 勤奋的</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Diligent students review their notes.</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        self.library.import_file(path)
        self.assertEqual(self.library.lookup("diligent"), ["adj. 勤奋的"])

    def test_text_pdf_is_extracted(self) -> None:
        path = self.folder / "book.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(
            b"BT /F1 12 Tf 72 720 Td (diligent means showing careful effort) Tj ET"
        )
        page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
        with path.open("wb") as handle:
            writer.write(handle)

        result = self.library.import_file(path)
        self.assertGreaterEqual(result.chunks, 1)

    def test_remove_source_rebuilds_indexes(self) -> None:
        path = self.write("words.txt", "fragile adj. 易碎的")
        result = self.library.import_file(path)
        self.assertEqual(self.library.lookup("fragile"), ["adj. 易碎的"])
        self.assertTrue(self.library.remove_source(result.source_id))
        self.assertEqual(self.library.lookup("fragile"), [])
        self.assertEqual(self.library.stats()["sources"], 0)

    def test_empty_or_unsupported_file_is_rejected(self) -> None:
        empty = self.write("empty.txt", "\n\n")
        with self.assertRaises(MaterialImportError):
            self.library.import_file(empty)
        unsupported = self.write("book.mobi", "content")
        with self.assertRaises(MaterialImportError):
            self.library.import_file(unsupported)


class MaterialSolverIntegrationTest(unittest.TestCase):
    class FakeClient:
        def __init__(self) -> None:
            self.verified: list = []
            self.submitted = 0

        def verify(self, code, answer):
            self.verified.append(answer)
            return {"data": {"answer_result": 1, "topic_code": "next"}}

        def submit(self, code, spent):
            self.submitted += 1
            return {"data": {"topic_done_num": 1, "topic_total": 1}}

        def study_verify(self, code, answer):
            return self.verify(code, answer)

        def study_submit(self, code, spent):
            return self.submit(code, spent)

    def topic(self, word: str = "ubiquitous") -> dict:
        return {
            "topic_mode": 31,
            "stem": {"content": word},
            "topic_code": "c1",
            "options": [
                {"content": "n. 天气", "answer_tag": 5},
                {"content": "adj. 普遍存在的", "answer_tag": 9},
            ],
        }

    def test_local_material_answers_before_llm(self) -> None:
        folder = Path(tempfile.mkdtemp())
        material = MaterialLibrary(folder / "materials", logger=lambda _m: None)
        material.import_file(self._write(folder, "cet4.txt", "ubiquitous adj. 普遍存在的"))

        class FailLlm:
            enabled = True
            auth_failed = False

            def ask_choice(self, *_args, **_kwargs):
                raise AssertionError("资料明确命中时不应调用模型")

        client = self.FakeClient()
        solver = Solver(
            client,
            Bank(folder / "bank.json"),
            materials=material,
            llm=FailLlm(),
            probe=False,
            logger=lambda _m: None,
        )
        solver._solve_one(self.topic())  # noqa: SLF001

        self.assertEqual(client.verified, [9])
        self.assertEqual(solver.stats.materials, 1)
        self.assertEqual(solver.stats.llm, 0)

    def test_unclear_material_is_sent_to_llm_as_short_hint(self) -> None:
        folder = Path(tempfile.mkdtemp())
        material = MaterialLibrary(folder / "materials", logger=lambda _m: None)
        material.import_file(
            self._write(
                folder,
                "book.txt",
                "In this chapter, obfuscate means to make an idea deliberately unclear.",
            )
        )
        seen: dict[str, str] = {}

        class FakeLlm:
            enabled = True
            auth_failed = False

            def ask_choice(
                self,
                _topic,
                _defs,
                _hint="",
                material_hint="",
                *,
                votes=1,
                thinking=None,
            ):
                del votes
                del thinking
                seen["material_hint"] = material_hint
                return 9

        client = self.FakeClient()
        solver = Solver(
            client,
            Bank(folder / "bank.json"),
            materials=material,
            llm=FakeLlm(),
            probe=False,
            logger=lambda _m: None,
        )
        solver._solve_one(self.topic("obfuscate"))  # noqa: SLF001

        self.assertIn("obfuscate means", seen["material_hint"])
        self.assertEqual(client.verified, [9])
        self.assertEqual(solver.stats.llm, 1)

    def test_prompt_contains_only_selected_material_context(self) -> None:
        prompt = LlmHelper.build_prompt(
            self.topic(),
            {},
            material_hint="《四级精讲》：ubiquitous 表示普遍存在的",
        )
        self.assertIn("用户本地资料检索", prompt)
        self.assertIn("ubiquitous 表示普遍存在的", prompt)

    @staticmethod
    def _write(folder: Path, name: str, text: str) -> Path:
        path = folder / name
        path.write_text(text, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main(verbosity=2)
