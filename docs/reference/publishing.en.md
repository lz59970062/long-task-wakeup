# Publishing and synchronization

All tutorials are maintained in `docs/` in the GitHub repository. Chinese files use `.md`; English files use `.en.md`. Each update to `main` checks translation coverage, builds the site, and updates the GitBook export branches.

## Publishing destinations

| Platform | Repository configuration | Updates after connection |
| --- | --- | --- |
| [GitHub Pages](https://lz59970062.github.io/long-task-wakeup/en/) | `.github/workflows/docs.yml` | Enabled; updates automatically after a push to `main` |
| Read the Docs | `.readthedocs.yaml` | A webhook triggers builds after importing the repository |
| Cloudflare Pages | `wrangler.toml` and the settings below | Builds automatically after connecting the GitHub repository |
| GitLab Pages | `.gitlab-ci.yml` and optional GitHub sync job | GitHub pushes source to GitLab, then GitLab CI publishes it |
| GitBook | Generated `gitbook-zh` and `gitbook-en` branches | Two Spaces sync their branches and publish as language variants |

GitHub Pages is live. Configuration and content exports are ready for the other four platforms; their first publication requires creating or linking a project in the corresponding account.

## GitHub Pages

After pushing documentation to `main`, open the repository's [Documentation workflow](https://github.com/lz59970062/long-task-wakeup/actions/workflows/docs.yml). It builds the site, updates `gh-pages`, and requests a Pages build.

Chinese uses the site root; English uses `en/`. The language menu opens the other-language version of the current page.

## Read the Docs

1. Sign into [Read the Docs](https://readthedocs.org/) and import `lz59970062/long-task-wakeup`.
2. Select `main`, use the root `.readthedocs.yaml`, and choose Simplified Chinese as the project language.
3. Trigger the first build, open the assigned public URL, and check the language menu.
4. Confirm the GitHub webhook is connected in the project's integration settings so future pushes rebuild it.

Each build contains both languages. The build hook reads `READTHEDOCS_CANONICAL_URL` so language links and the sitemap use the hosted site's full path. See the [Read the Docs MkDocs guide](https://docs.readthedocs.com/platform/stable/intro/mkdocs.html).

## Cloudflare Pages

Create a Pages project under Workers & Pages, connect the same GitHub repository, and enter:

| Setting | Value |
| --- | --- |
| Production branch | `main` |
| Root directory | Repository root |
| Build command | `pip install -r docs/requirements.txt && python scripts/docs_sync.py check && mkdocs build --strict` |
| Build output directory | `site` |
| Environment variable | `PYTHON_VERSION=3.12` |

`wrangler.toml` also sets `site` as the output directory. The site URL comes from `CF_PAGES_URL`. For a custom domain, set `LTC_DOCS_SITE_URL` to the full public URL. See [Cloudflare's MkDocs deployment guide](https://developers.cloudflare.com/pages/framework-guides/deploy-an-mkdocs-site/).

## GitLab Pages

Create an empty GitLab project with `main` as the default branch. In the GitHub repository, open Settings → Secrets and variables → Actions and add:

| Type | Name | Value |
| --- | --- | --- |
| Repository variable | `DOCS_GITLAB_REPOSITORY` | The GitLab project's HTTPS clone URL |
| Repository secret | `DOCS_GITLAB_TOKEN` | An access token with `write_repository` permission for that project |

Keep the token in GitHub Secrets. Run the `Documentation` workflow manually once. Its `sync-gitlab` job pushes the current commit to GitLab's `main`; subsequent documentation updates repeat the sync. GitLab builds and publishes `site` using `.gitlab-ci.yml`, with `CI_PAGES_URL` as the site address.

Find the public URL under Deploy → Pages in GitLab and enable public Pages access. The configuration uses `pages.publish` from GitLab 17.10 or later. See the [GitLab Pages guide](https://docs.gitlab.com/user/project/pages/getting_started/pages_from_scratch/).

## GitBook

GitHub Actions generates two content branches. Each contains Markdown, `SUMMARY.md`, `.gitbook.yaml`, and a `sync-manifest.json` recording the source commit.

1. Create a Chinese Space and an English Space in GitBook.
2. Enable Git Sync for each Space. Connect Chinese to this repository's `gitbook-zh` branch and English to `gitbook-en`.
3. Choose to import content from GitHub for the first sync. `SUMMARY.md` supplies the table of contents.
4. Add both Spaces to the same documentation site as language variants, named “简体中文” and “English,” then publish.

Continue editing source files in `docs/` on `main`. The workflow updates both export branches, which GitBook syncs. During export, MkDocs buttons become ordinary links and installation tabs become section headings. Follow the [GitBook language variants tutorial](https://gitbook.com/docs/guides/content-organization-and-localization/localize-your-docs-with-variants-in-gitbook) for site configuration.

Inspect the exports locally with:

```bash
python scripts/docs_sync.py export
```

The files are written to `build/gitbook/zh/` and `build/gitbook/en/`.

## Check a sync

Confirm that GitHub's `Documentation` build passed, then open a newly updated page on the destination platform. Switch languages and check code blocks, internal links, and search. Compare the source commit in GitBook's `sync-manifest.json` with the GitHub commit you published.

Use `LTC_DOCS_SITE_URL` to override automatic URL detection for a custom domain. Set it to the complete site-root URL, including the trailing `/`.
