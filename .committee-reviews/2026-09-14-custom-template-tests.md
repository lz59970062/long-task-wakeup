# Independent custom-template test handoff

Status: authored. Tests have NOT been run. Only tests/test_custom_templates.py and this handoff were added.

Parent command from /data3/lizhi_2024/program/long-task-wakeup:

    PYTHONPATH=src python -m unittest discover -s tests -p test_custom_templates.py -v

Ten tests cover named/explicit templates and suffixes; XDG lookup; source/version/handoff persistence; worker-specific defaults and CLI precedence; builtin fallback and user override; ambiguous names/selectors; malformed, duplicate-key, unknown-key and unsafe YAML rejection; dry-run without queue mutation; and a real fake-child worker lifecycle after the source file changes. The latter verifies the persisted snapshot reaches both child and original parent callback.

Expected behavior comes from the parent-provided custom-template contract. GNU screen availability is mocked at submission, while CLI parsing, filesystem loading, persistence, worker execution and callback construction remain real. The worker fixture requires POSIX executable scripts, matching the existing LTC test environment. Environment and queue paths are temporary. Tests accept either nonzero CLI return or nonzero SystemExit for rejected input.

Risks outside this focused suite: no exhaustive YAML parser fuzzing, no Windows execution, no live model invocation, and no proof that an agent obeys prompt instructions. Default home-directory lookup is not exercised because this suite avoids changing HOME; XDG and LTC_TEMPLATE_DIR cover controlled lookup paths. Parent should run existing compatibility tests and installed-wheel checks.

Follow-up: isolated tests/test_templates.py from real user templates via a temporary LTC_TEMPLATE_DIR. Extended existing custom cases to assert an explicit Claude profile default and relative --template-file resolution from the submitting process directory when --cwd differs. These follow-up edits have NOT been executed by the child author.

## Parent execution and validation

Parent executed all 128 tests successfully, including the 10 custom-template tests
and existing worker/callback regressions. After strengthening test isolation and
adding Claude default/relative-file cases, all 19 template tests passed again.
The final wheel was built and installed into a temporary target; from outside the
source tree, the installed CLI loaded the example through LTC_TEMPLATE_DIR and
rendered the resolved prompt, source, model, effort and handoff without queuing work.
Version remains 0.6.5 as requested. No real model call was needed for configuration
and worker/callback verification.
