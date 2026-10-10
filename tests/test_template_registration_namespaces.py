"""Check discovery isolation when independent template directories share a parent home."""

import argparse
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from long_task_callback import cli, template_registration as registration


class TemplateRegistrationNamespaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.first_directory = self.base / "aa-first-templates"
        self.second_directory = self.base / "ab-second-templates"
        self.parent_home = self.base / "zz-claude-home"
        self.skill_root = self.parent_home / "skills"
        self.workflow = self.skill_root / "long-task-callback" / "SKILL.md"
        self.workflow.parent.mkdir(parents=True)
        self.user_text = (
            "---\nname: long-task-callback\n"
            "description: The user's own LTC description.\n"
            "custom-field: preserved\n---\n\nUser workflow instructions.\n"
        )
        self.workflow.write_text(self.user_text, encoding="utf-8")
        self.source = self.base / "source.yaml"
        self.source.write_text(
            "version: 1\nworker: pi\ndescription: Independent task review.\n"
            "prompt: Review the supplied task within its requested scope.\n",
            encoding="utf-8",
        )
        environment = mock.patch.dict(os.environ, {
            "LTC_TEMPLATE_DIR": str(self.first_directory),
            "CODEX_HOME": str(self.base / "isolated-codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.parent_home),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def register(self, name, directory, *, dry_run=False):
        args = argparse.Namespace(
            name=name, file=str(self.source), worker="pi",
            description=f"Delegate independent {name} review.", target="claude",
            force=False, dry_run=dry_run,
        )
        with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(directory)}):
            return registration.register(args)

    def unregister(self, name, directory):
        args = argparse.Namespace(name=name, dry_run=False)
        with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(directory)}):
            return registration.unregister(args)

    def test_two_registries_keep_both_discoverable_entries_and_user_text(self):
        self.register("alpha", self.first_directory)
        alpha_skill = self.skill_root / "ltc-alpha" / "SKILL.md"
        alpha_before = alpha_skill.read_bytes()
        self.register("beta", self.second_directory)
        combined = self.workflow.read_bytes()
        self.assertTrue(combined.startswith(self.user_text.encode("utf-8")))
        self.assertIn(b"`alpha`", combined)
        self.assertIn(b"`beta`", combined)
        self.assertEqual(alpha_skill.read_bytes(), alpha_before)
        self.register("alpha", self.first_directory)
        self.assertEqual(self.workflow.read_bytes(), combined)
        for directory, expected in [(self.first_directory, "alpha"), (self.second_directory, "beta")]:
            with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(directory)}):
                self.assertEqual([entry["name"] for entry in registration.list_templates()], [expected])

    def test_unregister_keeps_the_other_registry_index_and_entrypoint(self):
        self.register("alpha", self.first_directory)
        self.register("beta", self.second_directory)
        beta_skill = self.skill_root / "ltc-beta" / "SKILL.md"
        beta_before = beta_skill.read_bytes()
        self.unregister("alpha", self.first_directory)
        remaining = self.workflow.read_text(encoding="utf-8")
        self.assertTrue(remaining.startswith(self.user_text))
        self.assertNotIn("`alpha`", remaining)
        self.assertIn("`beta`", remaining)
        self.assertEqual(beta_skill.read_bytes(), beta_before)
        self.assertFalse((self.skill_root / "ltc-alpha" / "SKILL.md").exists())
        self.assertTrue((self.first_directory / "alpha.yaml").is_file())
        with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(self.second_directory)}):
            self.assertEqual([entry["name"] for entry in registration.list_templates()], ["beta"])

    def test_force_reinstall_keeps_every_registry_namespace_and_entrypoint(self):
        self.register("alpha", self.first_directory)
        self.register("beta", self.second_directory)
        entrypoints = {
            name: (self.skill_root / f"ltc-{name}" / "SKILL.md").read_bytes()
            for name in ("alpha", "beta")
        }
        old = self.workflow.read_text(encoding="utf-8")
        inner = old[old.index(registration.INDEX_BEGIN) + len(registration.INDEX_BEGIN):
                    old.index(registration.INDEX_END)]
        with contextlib.redirect_stdout(io.StringIO()):
            status = cli.install_skill_tree(self.workflow.parent, include_codex_plugin=False, force=True)
        self.assertEqual(status, 0)
        installed = self.workflow.read_text(encoding="utf-8")
        self.assertNotIn("User workflow instructions.", installed)
        self.assertEqual(installed.count("`alpha`"), 1)
        self.assertEqual(installed.count("`beta`"), 1)
        self.assertIn(registration.INDEX_BEGIN + inner + registration.INDEX_END, installed)
        for name, original in entrypoints.items():
            self.assertEqual((self.skill_root / f"ltc-{name}" / "SKILL.md").read_bytes(), original)
        for directory, expected in [(self.first_directory, "alpha"), (self.second_directory, "beta")]:
            with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(directory)}):
                self.assertEqual([entry["name"] for entry in registration.list_templates()], [expected])

    def test_shared_root_contention_releases_partially_acquired_registry_lock(self):
        original = self.workflow.read_bytes()
        with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(self.first_directory)}):
            with registration._lock([self.skill_root]):
                with self.assertRaisesRegex(ValueError, "another template registration"):
                    self.register("beta", self.second_directory)
                # This registry sorts before the held shared root. Its own lease
                # must be released when acquisition of the shared lease fails.
                with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(self.second_directory)}):
                    with registration._lock():
                        pass
                self.assertEqual(self.workflow.read_bytes(), original)
                self.assertFalse((self.second_directory / "beta.yaml").exists())
                self.assertFalse((self.skill_root / "ltc-beta" / "SKILL.md").exists())
        self.register("beta", self.second_directory)
        self.assertIn("`beta`", self.workflow.read_text(encoding="utf-8"))

    def test_dry_run_and_failed_publication_preserve_the_foreign_namespace(self):
        self.register("alpha", self.first_directory)
        original = self.workflow.read_bytes()
        existing_locks = set(self.base.rglob(".ltc-registration.lock"))
        self.register("beta", self.second_directory, dry_run=True)
        self.assertFalse(self.second_directory.exists())
        self.assertEqual(set(self.base.rglob(".ltc-registration.lock")), existing_locks)
        self.assertEqual(self.workflow.read_bytes(), original)

        write = registration.storage.write_private_text
        failed = False

        def fail_registry_publication(path, text, **kwargs):
            nonlocal failed
            if Path(path) == self.second_directory / registration.REGISTRY_NAME and not failed:
                failed = True
                raise OSError("injected registry publication failure")
            return write(path, text, **kwargs)

        with mock.patch.object(registration.storage, "write_private_text", side_effect=fail_registry_publication):
            with self.assertRaisesRegex(OSError, "injected registry publication failure"):
                self.register("beta", self.second_directory)
        self.assertTrue(failed)
        self.assertEqual(self.workflow.read_bytes(), original)
        self.assertFalse((self.second_directory / registration.REGISTRY_NAME).exists())
        self.assertFalse((self.second_directory / "beta.yaml").exists())
        self.assertFalse((self.skill_root / "ltc-beta" / "SKILL.md").exists())
        self.register("beta", self.second_directory)
        restored = self.workflow.read_text(encoding="utf-8")
        self.assertIn("`alpha`", restored)
        self.assertIn("`beta`", restored)


if __name__ == "__main__":
    unittest.main()
