"""路径白名单：`os.pathsep` 分隔、realpath 包含、符号链接逃逸。"""

from __future__ import annotations

import os
import tempfile
import unittest

from hub import fsguard
from hub.errors import HubError


class ResolveRootsTests(unittest.TestCase):
    def test_explicit_roots_use_pathsep(self) -> None:
        roots = fsguard.resolve_roots(os.pathsep.join(["/a", "/b"]), "/home/u")
        self.assertEqual(len(roots), 2)
        self.assertTrue(all(os.path.isabs(p) for p in roots))

    def test_windows_style_paths_are_not_split_by_colon(self) -> None:
        """`C:\\a;C:\\b` 用 os.pathsep 切；`:` 会把盘符切碎。"""
        raw = os.path.join("C:\\one", "C:\\two") if False else "C:\\one;C:\\two"
        roots = fsguard.resolve_roots(raw, "/home/u", separator=";")
        self.assertEqual(len(roots), 2)
        self.assertTrue(roots[0].endswith("one"))
        self.assertTrue(roots[1].endswith("two"))

    def test_defaults_come_from_profile_home(self) -> None:
        roots = fsguard.resolve_roots(None, "/srv/hermes")
        self.assertEqual(len(roots), 2)
        self.assertTrue(any(p.endswith("uploads") for p in roots))
        self.assertTrue(any(p.endswith("cache") for p in roots))

    def test_no_profile_no_roots(self) -> None:
        self.assertEqual(fsguard.resolve_roots(None, ""), ())


class ContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fsguard-")
        self.root = os.path.join(self.tmp, "media")
        os.makedirs(self.root)
        self.roots = (os.path.realpath(self.root),)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inside_is_allowed(self) -> None:
        target = os.path.join(self.root, "a.png")
        with open(target, "wb") as handle:
            handle.write(b"x")
        self.assertTrue(fsguard.is_allowed(target, self.roots))
        self.assertEqual(fsguard.ensure_allowed(target, self.roots), os.path.realpath(target))

    def test_traversal_is_rejected(self) -> None:
        evil = os.path.join(self.root, "..", "etc", "passwd")
        self.assertFalse(fsguard.is_allowed(evil, self.roots))
        with self.assertRaises(HubError) as caught:
            fsguard.ensure_allowed(evil, self.roots)
        self.assertEqual(caught.exception.code, "path_not_allowed")

    def test_symlink_escape_is_rejected(self) -> None:
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret")
        link = os.path.join(self.root, "link.txt")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台不支持符号链接")
        self.assertFalse(fsguard.is_allowed(link, self.roots))

    def test_empty_inputs_are_rejected(self) -> None:
        self.assertFalse(fsguard.is_allowed("", self.roots))
        self.assertFalse(fsguard.is_allowed(os.path.join(self.root, "a"), ()))
        with self.assertRaises(HubError):
            fsguard.ensure_allowed("", self.roots)


if __name__ == "__main__":
    unittest.main()
