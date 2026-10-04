import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from evaluation_judger.dataset import task_fingerprint
from evaluation_judger.isolation import enable_image_reads, restrict_reads
from evaluation_judger.judge import _prompt
from evaluation_judger.rubric import load_rubric
from evaluation_judger.scoring import validate_atom, calculate
from evaluation_judger.render import task_markdown
from test_dataset import fixture_rubric


class ImageInput(TestCase):
    def test_visual_evidence_remains_located_and_is_rendered_without_a_fabricated_quote(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text(fixture_rubric(), encoding="utf-8")
            rubric = load_rubric(path)
            evidence = {"file": "chart.png", "locator": "图中蓝色折线", "kind": "visual", "description": "蓝色折线连接四个标记点，横坐标按年份递增。"}
            records = []
            for atom in rubric.atoms:
                record = {"id": atom.id, "state": 1, "observation": "已实际查看图中折线及坐标轴标签。",
                          "reason": "图中可见的折线连接与坐标轴排列符合此评分项的明确要求。", "evidence": [evidence]}
                records.append(validate_atom(record, atom, {"chart.png"}))
            task = SimpleNamespace(id="1.1", name="chart", dimension="维度一", rubric=rubric)
            rendered = task_markdown(task, [{"subject": "x", "atoms": records, "errors": [], "scores": calculate(rubric, records, [])}], {"x": "Actor"})
            self.assertIn("视觉观察：蓝色折线连接四个标记点", rendered)
            for replacement in ({**evidence, "file": "delivery.txt"}, {**evidence, "description": ""}, {**evidence, "locator": ""}, {**evidence, "kind": "text"}):
                with self.assertRaises(ValueError):
                    validate_atom({**records[0], "evidence": [replacement]}, rubric.atoms[0], {"chart.png", "delivery.txt"})
            with self.assertRaises(ValueError):
                validate_atom(records[0], rubric.atoms[0], {"other.png"})

    def test_image_declaration_preserves_read_isolation_and_other_model_capabilities(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"provider": {"demo": {"models": {"vision": {"modalities": {"input": ["text", "audio"], "output": ["text"]}}}}},
                        "agent": {"judge": {"permission": {"edit": "deny", "bash": "deny", "external_directory": "deny"}}}}
            path = root / "opencode.jsonc"
            path.write_text(json.dumps(original), encoding="utf-8")
            restrict_reads(root)
            before = json.loads(path.read_text(encoding="utf-8"))["agent"]
            enable_image_reads(root, "demo/vision#high")
            enable_image_reads(root, "demo/vision#high")
            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(before, after["agent"])
            self.assertEqual(after["provider"]["demo"]["models"]["vision"]["modalities"],
                             {"input": ["text", "audio", "image"], "output": ["text"]})
            with self.assertRaises(ValueError):
                enable_image_reads(root, "demo/unknown")

    def test_vision_changes_cache_key_but_disabled_option_preserves_existing_results(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            question = root / "question.txt"; question.write_text("Draw the required chart", encoding="utf-8")
            rubric_file = root / "rubric.md"; rubric_file.write_text(fixture_rubric(), encoding="utf-8")
            delivery = root / "chart.png"; delivery.write_bytes(b"unchanged binary material")
            task = SimpleNamespace(id="1.1", name="chart", directory=root, question=question,
                                   rubric=load_rubric(rubric_file), sources=(), deliveries={"x": (delivery,)})
            baseline = task_fingerprint(task, "x", {})
            self.assertEqual(baseline, task_fingerprint(task, "x", {"enable_image_input": False}))
            self.assertNotEqual(baseline, task_fingerprint(task, "x", {"enable_image_input": True}))
            manifest = {"files": [{"file": "chart.png", "extracted": "chart.txt", "limitation": "no text"}],
                        "process_record": {}, "image_input": True}
            text = _prompt(task, "x", [], manifest)
            self.assertIn("empty extracted text does not mean an image delivery is missing", text)
            self.assertIn("Do not execute delivered plotting code", text)
            self.assertNotIn("configured for image input", _prompt(task, "x", [], {**manifest, "image_input": False}))
