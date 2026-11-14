"""合并上游后的静态守卫：防止品牌替换误伤标识符、包缺 __init__。"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "BlablaBot"


class MergeGuardTest(unittest.TestCase):
    def test_python_packages_have_init(self):
        missing = []
        for d in ROOT.rglob("*"):
            if not d.is_dir() or "__pycache__" in d.parts:
                continue
            if not any(p.suffix == ".py" for p in d.iterdir() if p.is_file()):
                continue
            if not (d / "__init__.py").exists():
                missing.append(str(d.relative_to(ROOT)))
        self.assertEqual(missing, [], f"缺少 __init__.py: {missing}")

    def test_no_renamed_config_class(self):
        hits = []
        for p in ROOT.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if "BlablaBotConfig" in text and p.name != "config.py":
                hits.append(str(p.relative_to(ROOT)))
        self.assertEqual(hits, [], f"误用 BlablaBotConfig: {hits}")


    def test_no_binop_decorators(self):
        """装饰器被重复拼接成 a@b 时，Python 会当成矩阵乘法，运行期才报 TypeError。"""
        import ast
        bad = []
        for p in ROOT.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.BinOp):
                            bad.append(f"{p.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(bad, [], f"重复拼接的装饰器: {bad}")

    def test_config_class_name_exists(self):
        text = (ROOT / "nikke_config" / "config.py").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(text, r"(?m)^AliceUIDConfig\s*=")


if __name__ == "__main__":
    unittest.main()
