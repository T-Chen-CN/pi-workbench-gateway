"""工具注册覆盖：schema 与 shim 的 handler 表必须一一对应。

`shim._register_tools` 是手写的 `name -> 方法` 映射，外面套了 `except Exception`，
所以「schema 加了、映射忘加」不会报错，只会变成一条 warning，模型那边就是**看不见
这个工具**（0.3.1 真踩过：doctor 报 5 个工具而 schemas 有 6 个）。

`shim.py` 用的是相对 import（不能单独 import），所以这里直接读源码里的映射表。
"""

import re
import unittest
from pathlib import Path

from tools import tool_schemas

SHIM = Path(__file__).resolve().parent.parent / "shim.py"


class HandlerCoverageTests(unittest.TestCase):
    def test_shim_maps_every_schema_to_a_handler(self) -> None:
        source = SHIM.read_text(encoding="utf-8")
        block = source[source.index("handlers = {") : source.index("}", source.index("handlers = {"))]
        mapped = set(re.findall(r'"([a-z_]+)":', block))
        names = {schema["name"] for schema in tool_schemas()}
        self.assertTrue(names)
        self.assertEqual(names - mapped, set(), "这些工具没有 handler，模型看不见它们")
        self.assertEqual(mapped - names, set(), "handler 表里有 schema 之外的残留")

    def test_shim_maps_to_real_methods(self) -> None:
        source = SHIM.read_text(encoding="utf-8")
        block = source[source.index("handlers = {") : source.index("}", source.index("handlers = {"))]
        from tools import DispatchTools

        for method in re.findall(r"tools\.([a-z_]+)", block):
            self.assertTrue(
                callable(getattr(DispatchTools, method, None)), f"DispatchTools.{method} 不存在"
            )
