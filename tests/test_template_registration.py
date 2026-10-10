"""Public registration contracts, independently authored and run by the parent."""

import contextlib
import errno
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from long_task_callback import cli, storage
from long_task_callback.templates import load_template, user_template_dir


class TemplateRegistrationTests(unittest.TestCase):
    NAME = "contract-review"
    DESCRIPTION = "Review manuscript evidence and revise requested files."
    SYSTEM = "SYSTEM_FILE_SENTINEL\nPreserve evidence and the literal ${AUTHOR}.\n"
    PROMPT = "TASK_TEMPLATE_SENTINEL: carry out only the author's requested work."
    INDEX_BEGIN = "<!-- ltc:registered-templates:begin -->"
    INDEX_END = "<!-- ltc:registered-templates:end -->"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex = self.root / "codex-profile"
        self.claude = self.root / "claude-profile"
        self.templates = self.root / "registered templates"
        self.source_dir = self.root / "source templates"
        self.source_dir.mkdir()
        self.source = self.source_dir / "external-profile.yml"
        self.system = self.source_dir / "support files" / "system prompt.md"
        self.system.parent.mkdir()
        self.system.write_text(self.SYSTEM, encoding="utf-8")
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "CODEX_HOME": str(self.codex),
            "CLAUDE_CONFIG_DIR": str(self.claude),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "LTC_TEMPLATE_DIR": str(self.templates),
            "PI_CODING_AGENT_DIR": str(self.root / "pi-profile"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.write_source()

    def write_source(self, *, version=1, worker="pi", description=None, prompt=None):
        self.source.write_text(yaml.safe_dump({
            "version": version,
            "worker": worker,
            "description": self.DESCRIPTION if description is None else description,
            "prompt": self.PROMPT if prompt is None else prompt,
            "pi": {
                "system_prompt_file": "support files/system prompt.md",
                "model": "provider/model-id",
                "reasoning_effort": "high",
            },
            "handoff": "Inspect the actual edits and report unsupported claims separately.",
        }, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return self.source

    def invoke(self, *arguments):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["ltc", *map(str, arguments)]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                status = cli.main()
            except SystemExit as exc:
                status = exc.code
        return status, output.getvalue()

    def register(self, *, name=None, options=()):
        return self.invoke("template", "register", name or self.NAME,
                           "--file", self.source, *options)

    def skill(self, target="codex", name=None):
        profile = self.codex if target == "codex" else self.claude
        return profile / "skills" / ("ltc-" + (name or self.NAME))

    def base_skill(self, target="codex"):
        profile = self.codex if target == "codex" else self.claude
        return profile / "skills" / "long-task-callback"

    def snapshot(self, *, metadata=False, directory=None):
        """Include directories so a failed request cannot leave an empty partial tree."""
        state = {}
        directory = self.root if directory is None else directory
        if not directory.exists() and not directory.is_symlink():
            return state
        for path in [directory, *sorted(directory.rglob("*"))]:
            key = path.relative_to(directory).as_posix()
            if path.is_symlink():
                value = ("link", os.readlink(path))
            elif path.is_dir():
                value = ("directory",)
            else:
                value = ("file", path.read_bytes())
            if metadata:
                value += (path.lstat().st_mtime_ns,)
            state[key] = value
        return state

    def seed_custom_base(self, target="codex", *, newline="\n"):
        directory = self.base_skill(target)
        (directory / "agents").mkdir(parents=True)
        text = ("---\nname: long-task-callback\ndescription: User's existing workflow.\n---\n"
                "# Private LTC instructions\n\nPreserve this 用户维护段落 verbatim.\n")
        text = text.replace("\n", newline)
        (directory / "SKILL.md").write_bytes(text.encode("utf-8"))
        (directory / "agents" / "openai.yaml").write_text("private_metadata: keep\n", encoding="utf-8")
        (directory / "user-notes.md").write_text("Private notes; never remove.\n", encoding="utf-8")
        return text

    def index(self, target="codex"):
        text = (self.base_skill(target) / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(self.INDEX_BEGIN), 1)
        self.assertEqual(text.count(self.INDEX_END), 1)
        return text.split(self.INDEX_BEGIN, 1)[1].split(self.INDEX_END, 1)[0]

    def assert_discoverable(self, directory, *, name=None, worker="pi", description=None, codex=False):
        name = name or self.NAME
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
        body = "\n".join(lines[end + 1:])
        self.assertEqual(frontmatter["name"], "ltc-" + name)
        self.assertIn(description or self.DESCRIPTION, frontmatter["description"])
        self.assertRegex(body, rf"\bltc\s+agent\s+{worker}\b")
        self.assertIn("--cwd", body)
        self.assertTrue(f"--template {name}" in body or (
            "--template-file" in body and str(self.templates / f"{name}.yaml") in body
        ), body)
        self.assertNotIn(self.SYSTEM, body)
        if codex:
            metadata = yaml.safe_load((directory / "agents" / "openai.yaml").read_text(encoding="utf-8"))
            interface = metadata["interface"]
            self.assertTrue(interface["display_name"].strip())
            self.assertTrue(interface["short_description"].strip())
            self.assertIn("$ltc-" + name, interface["default_prompt"])

    def listed(self):
        status, output = self.invoke("template", "list", "--json")
        self.assertEqual(status, 0, output)
        result = json.loads(output)
        # Check the public collection without relying on private registry fields.
        self.assertIsInstance(result, list)
        return result

    def test_registration_copies_template_dependencies_for_other_workspaces(self):
        status, output = self.register()
        self.assertEqual(status, 0, output)
        self.assertEqual(user_template_dir(), self.templates.resolve())
        installed = self.templates / f"{self.NAME}.yaml"
        template = load_template(self.NAME)
        self.assertEqual(template.worker, "pi")
        self.assertEqual(template.description, self.DESCRIPTION)
        system_copy = template.system_prompt_file_for("pi")
        self.assertIsNotNone(system_copy)
        self.assertEqual(system_copy.read_text(encoding="utf-8"), self.SYSTEM)
        self.assertNotEqual(system_copy.resolve(), self.system.resolve())
        self.assertTrue(system_copy.resolve().is_relative_to(self.templates.resolve()))
        self.source.unlink()
        self.system.unlink()
        manuscript = self.root / "another manuscript workspace"
        manuscript.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(manuscript)
            for template in (load_template(self.NAME), load_template(template_file=str(installed))):
                self.assertEqual(template.prompt, self.PROMPT)
                self.assertEqual(template.model_for("pi"), "provider/model-id")
                self.assertEqual(template.reasoning_effort_for("pi"), "high")
                self.assertEqual(template.system_prompt_file_for("pi").read_text(encoding="utf-8"), self.SYSTEM)
        finally:
            os.chdir(previous)

    def test_both_targets_receive_discoverable_skills_without_replacing_user_ltc(self):
        originals = {target: self.seed_custom_base(target, newline="\r\n" if target == "claude" else "\n")
                     for target in ("codex", "claude")}
        status, output = self.register()
        self.assertEqual(status, 0, output)
        for target, original in originals.items():
            with self.subTest(target=target):
                self.assert_discoverable(self.skill(target), codex=target == "codex")
                base = self.base_skill(target)
                self.assertTrue((base / "SKILL.md").read_bytes().startswith(original.encode("utf-8")))
                self.assertIn(self.NAME, self.index(target))
                self.assertEqual((base / "agents" / "openai.yaml").read_text(encoding="utf-8"), "private_metadata: keep\n")
                self.assertEqual((base / "user-notes.md").read_text(encoding="utf-8"), "Private notes; never remove.\n")

    def test_generated_bash_recipe_preserves_cli_arguments_without_extra_tokens(self):
        status, output = self.register()
        self.assertEqual(status, 0, output)
        template_file = self.templates / f"{self.NAME}.yaml"
        self.assertIn(" ", str(template_file), "The fixture must exercise a path containing spaces.")
        for target in ("codex", "claude"):
            with self.subTest(target=target):
                text = (self.skill(target) / "SKILL.md").read_text(encoding="utf-8")
                blocks = text.split("```bash\n")
                self.assertEqual(len(blocks), 2)
                recipe = blocks[1].split("```", 1)[0].strip()
                argv = shlex.split(recipe)
                delimiter = argv.index("--")
                self.assertEqual(argv[:delimiter], [
                    "ltc", "agent", "pi", "--template-file", str(template_file),
                    "--cwd", "/absolute/path/to/workspace",
                ])
                self.assertEqual(argv[delimiter + 1:], ["The user's concrete task requirements."])

    def test_cli_metadata_and_target_overrides_respect_custom_and_default_profiles(self):
        description = "Inspect public API documentation."
        status, output = self.register(options=("--worker", "codex", "--description", description, "--target", "codex"))
        self.assertEqual(status, 0, output)
        self.assert_discoverable(self.skill(), worker="codex", description=description, codex=True)
        self.assertFalse(self.skill("claude").exists())
        self.assertFalse((self.home / ".agents" / "skills").exists())
        row = next(row for row in self.listed() if row["name"] == self.NAME)
        self.assertEqual(row["worker"], "codex")
        self.assertEqual(row["description"], description)
        default_name = "default-profile-review"
        with mock.patch.dict(os.environ):
            os.environ.pop("CODEX_HOME", None)
            status, output = self.register(name=default_name, options=("--target", "codex"))
        self.assertEqual(status, 0, output)
        standard = self.home / ".agents" / "skills" / ("ltc-" + default_name)
        if os.name == "nt":
            self.assert_discoverable(standard, name=default_name, codex=True)
        else:
            primary = self.home / ".codex" / "skills" / ("ltc-" + default_name)
            self.assert_discoverable(primary, name=default_name, codex=True)
            self.assertTrue(standard.is_symlink())
            self.assertEqual(standard.resolve(), primary.resolve())

    def test_repeat_registration_is_idempotent_and_listing_contains_effective_metadata(self):
        self.seed_custom_base()
        self.assertEqual(self.register()[0], 0)
        notes = self.skill() / "user-notes.md"
        notes.write_text("An unrelated user-owned supporting file.\n", encoding="utf-8")
        before = self.snapshot()
        status, output = self.register()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.snapshot(), before)
        rows = [row for row in self.listed() if row["name"] == self.NAME]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["worker"], "pi")
        self.assertEqual(rows[0]["description"], self.DESCRIPTION)
        self.assertIn(self.NAME, self.index())
        status, output = self.invoke("template", "list")
        self.assertEqual(status, 0, output)
        self.assertIn(self.NAME, output)
        self.assertIn(self.DESCRIPTION, output)

    def test_named_repeat_reuses_installed_files_after_original_source_is_moved(self):
        status, output = self.register()
        self.assertEqual(status, 0, output)
        self.source_dir.rename(self.root / "detached source directory")
        self.assertFalse(self.source.exists())
        self.assertFalse(self.system.exists())
        status, output = self.invoke("template", "register", self.NAME)
        self.assertEqual(status, 0, output)
        template = load_template(self.NAME)
        self.assertEqual(template.worker, "pi")
        self.assertEqual(template.description, self.DESCRIPTION)
        self.assertEqual(template.prompt, self.PROMPT)
        self.assertEqual(template.model_for("pi"), "provider/model-id")
        self.assertEqual(template.reasoning_effort_for("pi"), "high")
        self.assertEqual(template.system_prompt_file_for("pi").read_text(encoding="utf-8"), self.SYSTEM)
        self.assert_discoverable(self.skill(), codex=True)
        self.assert_discoverable(self.skill("claude"))
        rows = [row for row in self.listed() if row["name"] == self.NAME]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["worker"], "pi")
        self.assertEqual(rows[0]["description"], self.DESCRIPTION)

    def test_clean_updates_work_without_force_but_user_modified_copies_require_force(self):
        self.assertEqual(self.register()[0], 0)
        notes = self.skill() / "user-notes.md"
        notes.write_text("Keep this unknown file across managed updates.\n", encoding="utf-8")
        updated_system = "UPDATED_SYSTEM\nPreserve the author's reported measurements.\n"
        self.system.write_text(updated_system, encoding="utf-8")
        self.write_source(version=2, description="Updated manuscript review entry.")
        status, output = self.register()
        self.assertEqual(status, 0, output)
        template = load_template(self.NAME)
        self.assertEqual(template.version, 2)
        self.assertEqual(template.system_prompt_file_for("pi").read_text(encoding="utf-8"), updated_system)
        self.assert_discoverable(self.skill(), description="Updated manuscript review entry.", codex=True)
        installed = self.templates / f"{self.NAME}.yaml"
        installed.write_text(installed.read_text(encoding="utf-8") + "\n# User modification; require explicit overwrite.\n", encoding="utf-8")
        template.system_prompt_file_for("pi").write_text("User's custom system instructions.\n", encoding="utf-8")
        self.write_source(version=3, description="Third manuscript review entry.")
        self.system.write_text("THIRD_SYSTEM\n", encoding="utf-8")
        before = self.snapshot()
        status, _ = self.register()
        self.assertNotEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        status, output = self.register(options=("--force",))
        self.assertEqual(status, 0, output)
        self.assertEqual(load_template(self.NAME).version, 3)
        self.assertEqual(load_template(self.NAME).system_prompt_file_for("pi").read_text(encoding="utf-8"), "THIRD_SYSTEM\n")
        self.assertEqual(notes.read_text(encoding="utf-8"), "Keep this unknown file across managed updates.\n")

    def test_unregister_removes_its_discovery_entry_but_keeps_templates_and_user_files(self):
        for target in ("codex", "claude"):
            self.seed_custom_base(target)
        self.assertEqual(self.register()[0], 0)
        other = "other-review"
        self.assertEqual(self.register(name=other)[0], 0)
        unknown = self.skill() / "private-notes.txt"
        unknown.write_text("Never delete this user file.\n", encoding="utf-8")
        installed = self.templates / f"{self.NAME}.yaml"
        system_copy = load_template(self.NAME).system_prompt_file_for("pi")
        template_bytes, system_bytes = installed.read_bytes(), system_copy.read_bytes()
        status, output = self.invoke("template", "unregister", self.NAME)
        self.assertEqual(status, 0, output)
        for target in ("codex", "claude"):
            self.assertFalse((self.skill(target) / "SKILL.md").exists())
            self.assertTrue((self.skill(target, other) / "SKILL.md").is_file())
            self.assertNotIn(self.NAME, self.index(target))
            self.assertIn(other, self.index(target))
        self.assertFalse((self.skill() / "agents" / "openai.yaml").exists())
        self.assertEqual(unknown.read_text(encoding="utf-8"), "Never delete this user file.\n")
        self.assertEqual(installed.read_bytes(), template_bytes)
        self.assertEqual(system_copy.read_bytes(), system_bytes)
        self.assertEqual(load_template(self.NAME).system_prompt_file_for("pi").read_bytes(), system_bytes)
        names = {row["name"] for row in self.listed()}
        self.assertNotIn(self.NAME, names)
        self.assertIn(other, names)

    def test_register_and_unregister_dry_runs_make_no_filesystem_changes(self):
        before = self.snapshot(metadata=True)
        status, output = self.register(options=("--dry-run",))
        self.assertEqual(status, 0, output)
        self.assertIn(self.NAME, output)
        self.assertEqual(self.snapshot(metadata=True), before)
        self.assertEqual(self.register()[0], 0)
        before = self.snapshot(metadata=True)
        status, output = self.invoke("template", "unregister", self.NAME, "--dry-run")
        self.assertEqual(status, 0, output)
        self.assertIn(self.NAME, output)
        self.assertEqual(self.snapshot(metadata=True), before)

    def test_foreign_skill_collision_refuses_both_targets_even_with_force(self):
        self.seed_custom_base("codex")
        foreign = self.skill("claude")
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("---\nname: ltc-contract-review\ndescription: User-owned unrelated skill.\n---\nNever overwrite.\n", encoding="utf-8")
        (foreign / "user-script.py").write_text("# User's unrelated program\n", encoding="utf-8")
        before = self.snapshot()
        for options in ((), ("--force",)):
            with self.subTest(options=options):
                status, _ = self.register(options=options)
                self.assertNotEqual(status, 0)
                self.assertEqual(self.snapshot(), before)

    def test_second_template_namespace_cannot_claim_same_skill_even_with_force(self):
        status, output = self.register()
        self.assertEqual(status, 0, output)
        other_templates = self.root / "another template namespace"
        before = self.snapshot(metadata=True)
        with mock.patch.dict(os.environ, {"LTC_TEMPLATE_DIR": str(other_templates)}):
            for options in ((), ("--force",)):
                with self.subTest(options=options):
                    status, _ = self.register(options=options)
                    self.assertNotEqual(status, 0)
                    self.assertEqual(self.snapshot(metadata=True), before)
        template = load_template(self.NAME)
        self.assertEqual(template.prompt, self.PROMPT)
        self.assertEqual(template.system_prompt_file_for("pi").read_text(encoding="utf-8"), self.SYSTEM)
        self.assert_discoverable(self.skill(), codex=True)
        self.assert_discoverable(self.skill("claude"))

    def test_invalid_names_and_sources_are_rejected_before_configuration_changes(self):
        for name in ("../escape", "_invalid", "name with spaces"):
            with self.subTest(name=name):
                before = self.snapshot()
                status, _ = self.register(name=name)
                self.assertNotEqual(status, 0)
                self.assertEqual(self.snapshot(), before)
        bodies = [b"[]", b"version: 1\nprompt: x\nprompt: duplicate\nworker: pi\n",
                  b"version: 1\nprompt: ' '\nworker: pi\n",
                  b"version: 1\nprompt: x\nworker: unsupported-agent\n",
                  b"version: 1\nprompt: x\nworker: pi\ndescription: []\n",
                  b"version: 1\nprompt: \xff\n"]
        for body in bodies:
            with self.subTest(body=body):
                self.source.write_bytes(body)
                before = self.snapshot()
                status, _ = self.register()
                self.assertNotEqual(status, 0)
                self.assertEqual(self.snapshot(), before)
        before = self.snapshot()
        status, _ = self.invoke("template", "register", self.NAME, "--file", self.source_dir / "missing.yaml")
        self.assertNotEqual(status, 0)
        self.assertEqual(self.snapshot(), before)

    def test_unusable_system_prompt_dependencies_fail_without_partial_registration(self):
        for body in (None, b"", b" \n\t", b"\xef\xbb\xbf", b"\xff\xfe"):
            with self.subTest(body=body):
                if self.system.exists():
                    self.system.unlink()
                if body is not None:
                    self.system.write_bytes(body)
                before = self.snapshot()
                status, _ = self.register()
                self.assertNotEqual(status, 0)
                self.assertEqual(self.snapshot(), before)
        self.system.unlink()
        self.system.mkdir()
        before = self.snapshot()
        status, _ = self.register()
        self.assertNotEqual(status, 0)
        self.assertEqual(self.snapshot(), before)

    def test_existing_template_conflict_requires_force_and_preserves_other_configuration(self):
        self.templates.mkdir()
        installed = self.templates / f"{self.NAME}.yaml"
        installed.write_text("version: 1\nprompt: Existing user-managed template.\n", encoding="utf-8")
        unrelated = self.templates / "unrelated.yml"
        unrelated.write_text("version: 1\nprompt: Preserve this independent profile.\n", encoding="utf-8")
        before = self.snapshot()
        status, _ = self.register()
        self.assertNotEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        status, output = self.register(options=("--force",))
        self.assertEqual(status, 0, output)
        self.assertEqual(load_template(self.NAME).prompt, self.PROMPT)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "version: 1\nprompt: Preserve this independent profile.\n")

    def test_keep_existing_repair_preserves_registered_indexes_and_all_customizations(self):
        for target in ("codex", "claude"):
            self.seed_custom_base(target)
        self.assertEqual(self.register()[0], 0)
        before = self.snapshot()
        for target in ("codex", "claude"):
            with contextlib.redirect_stdout(io.StringIO()):
                status = cli.install_skill_tree(self.base_skill(target),
                                                include_codex_plugin=target == "codex",
                                                force=True, keep_existing=True)
            self.assertEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(self.NAME, self.index("codex"))
        self.assertIn(self.NAME, self.index("claude"))

    def test_switching_codex_profile_does_not_rewrite_the_original_global_skills(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CODEX_HOME", None)
            status, output = self.register(options=("--target", "codex"))
        self.assertEqual(status, 0, output)
        original_home = self.snapshot(directory=self.home)
        self.write_source(version=2, description="Review under the newly selected profile.")
        other_profile = self.root / "another-codex-profile"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(other_profile)}):
            status, output = self.register(options=("--target", "codex"))
        self.assertEqual(self.snapshot(directory=self.home), original_home)
        if status == 0:
            self.assert_discoverable(other_profile / "skills" / ("ltc-" + self.NAME),
                                     description="Review under the newly selected profile.", codex=True)
        else:
            # Refusing an ambiguous cross-profile update is also safe; partial admission is not.
            self.assertNotEqual(status, 0, output)
            self.assertEqual(self.snapshot(directory=other_profile), {})

    def test_unregister_refuses_symlinked_ancestor_and_preserves_the_user_backup(self):
        self.assertEqual(self.register(options=("--target", "codex"))[0], 0)
        original_skills = self.codex / "skills"
        backup = self.root / "user-skill-backup"
        original_skills.rename(backup)
        try:
            original_skills.symlink_to(backup, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Creating test directory symlinks is unavailable: {exc}")
            raise
        before = self.snapshot()
        owned_entry = backup / ("ltc-" + self.NAME) / "SKILL.md"
        saved_entry = owned_entry.read_bytes()
        status, _ = self.invoke("template", "unregister", self.NAME)
        self.assertNotEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(owned_entry.read_bytes(), saved_entry)

    def test_single_publication_failure_restores_files_and_allows_a_clean_retry(self):
        self.assertEqual(self.register()[0], 0)
        self.write_source(version=2, description="Updated entry after a publication failure.")
        self.system.write_text("UPDATED SYSTEM FOR PUBLICATION FAILURE\n", encoding="utf-8")
        before = self.snapshot()
        actual_write = storage.write_private_text
        calls = 0
        failed = False

        def fail_once(path, text, **kwargs):
            nonlocal calls, failed
            calls += 1
            if calls == 3:
                failed = True
                raise OSError(errno.EIO, "Simulated one-time publication failure")
            return actual_write(path, text, **kwargs)

        with mock.patch.object(storage, "write_private_text", side_effect=fail_once):
            status, _ = self.register()
        self.assertTrue(failed, "The injected failure must occur after earlier publication writes.")
        self.assertNotEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(load_template(self.NAME).version, 1)
        status, output = self.register()
        self.assertEqual(status, 0, output)
        self.assertEqual(load_template(self.NAME).version, 2)


if __name__ == "__main__":
    unittest.main()
