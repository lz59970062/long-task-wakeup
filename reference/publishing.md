# 文档发布与同步

所有教程在 GitHub 仓库的 `docs/` 中维护。中文文件使用 `.md`，英文使用 `.en.md`。每次更新 `main`，发布流程检查双语覆盖、构建站点，并更新 GitBook 导出分支。

## 发布入口

| 平台 | 仓库中的配置 | 接入后的更新方式 |
| --- | --- | --- |
| [GitHub Pages](https://lz59970062.github.io/long-task-wakeup/) | `.github/workflows/docs.yml` | 已启用；提交到 `main` 后自动更新 |
| Read the Docs | `.readthedocs.yaml` | 导入仓库后，由 webhook 触发构建 |
| Cloudflare Pages | `wrangler.toml` 和下方构建参数 | 连接 GitHub 仓库后自动构建 |
| GitLab Pages | `.gitlab-ci.yml`、可选 GitHub 同步任务 | GitHub 推送源文件到 GitLab，再由 GitLab CI 发布 |
| GitBook | 自动生成的 `gitbook-zh`、`gitbook-en` 分支 | 两个 Space 分别连接对应分支，通过语言 variants 发布 |

GitHub Pages 已上线。其他四个平台的配置和内容导出已准备好，首次发布需要在对应账号中创建或关联项目。

## GitHub Pages

推送文档到 `main` 后，在仓库 [Actions](https://github.com/lz59970062/long-task-wakeup/actions/workflows/docs.yml) 查看 `Documentation` 工作流。它先构建，再更新 `gh-pages` 分支并请求 Pages 发布。

中文首页位于站点根路径，英文位于 `en/`。顶部语言菜单切换到当前页面的另一种语言。

## Read the Docs

1. 登录 [Read the Docs](https://readthedocs.org/)，导入 `lz59970062/long-task-wakeup`。
2. 使用 `main` 分支和仓库根目录的 `.readthedocs.yaml`，项目语言选择简体中文。
3. 触发第一次构建，打开项目给出的公开地址，检查语言菜单。
4. 在项目的集成设置中确认 GitHub webhook 已连接，后续提交即可自动更新。

一个构建包含两种语言。构建钩子读取平台提供的 `READTHEDOCS_CANONICAL_URL`，语言链接和 sitemap 使用当前站点的完整路径。配置方式见 [Read the Docs 的 MkDocs 指南](https://docs.readthedocs.com/platform/stable/intro/mkdocs.html)。

## Cloudflare Pages

在 Cloudflare 的 Workers & Pages 中创建 Pages 项目，连接同一个 GitHub 仓库，填写：

| 设置 | 值 |
| --- | --- |
| Production branch | `main` |
| Root directory | 仓库根目录 |
| Build command | `pip install -r docs/requirements.txt && python scripts/docs_sync.py check && mkdocs build --strict` |
| Build output directory | `site` |
| 环境变量 | `PYTHON_VERSION=3.12` |

仓库中的 `wrangler.toml` 也指定了 `site` 输出目录。站点地址从 `CF_PAGES_URL` 读取。使用自定义域名时，将 `LTC_DOCS_SITE_URL` 设置为完整公开地址。流程见 [Cloudflare 的 MkDocs 部署指南](https://developers.cloudflare.com/pages/framework-guides/deploy-an-mkdocs-site/)。

## GitLab Pages

在 GitLab 创建一个空项目，默认分支使用 `main`。在 GitHub 仓库的 Settings → Secrets and variables → Actions 中添加：

| 类型 | 名称 | 值 |
| --- | --- | --- |
| Repository variable | `DOCS_GITLAB_REPOSITORY` | GitLab 项目的 HTTPS 克隆地址 |
| Repository secret | `DOCS_GITLAB_TOKEN` | 有该项目 `write_repository` 权限的访问令牌 |

令牌保存在 GitHub Secrets 中。手动运行一次 `Documentation` 工作流，`sync-gitlab` 会将当前提交推送到 GitLab 的 `main`。之后每次文档更新都会同步。GitLab 使用 `.gitlab-ci.yml` 构建并发布 `site`，站点地址从 `CI_PAGES_URL` 读取。

在 GitLab 的 Deploy → Pages 查看公开地址，项目的 Pages 访问设置选择公开。配置使用 GitLab 17.10 及更新版本的 `pages.publish`，参阅 [GitLab Pages 指南](https://docs.gitlab.com/user/project/pages/getting_started/pages_from_scratch/)。

## GitBook

GitHub Actions 生成两个内容分支，每个分支都有 Markdown、`SUMMARY.md`、`.gitbook.yaml` 和记录源提交的 `sync-manifest.json`。

1. 在 GitBook 建立中文与英文两个 Space。
2. 为中文 Space 开启 Git Sync，连接本仓库的 `gitbook-zh` 分支；英文连接 `gitbook-en`。
3. 首次同步选择从 GitHub 导入内容，目录由 `SUMMARY.md` 提供。
4. 在同一个 GitBook 文档站中把两个 Space 配成语言 variants，名称分别为“简体中文”和“English”，然后发布。

源文件继续在 `main` 的 `docs/` 中修改。同步流程会更新两个导出分支，GitBook 随之同步。导出时，MkDocs 按钮会转成普通链接，安装步骤的页签会转成小节标题。GitBook 的语言 variants 配置见[官方教程](https://gitbook.com/docs/guides/content-organization-and-localization/localize-your-docs-with-variants-in-gitbook)。

本地检查导出内容：

```bash
python scripts/docs_sync.py export
```

文件位于 `build/gitbook/zh/` 和 `build/gitbook/en/`。

## 检查一次同步

先确认 GitHub `Documentation` 构建成功，再打开目标平台的新页面。切换语言，检查代码块、站内链接和搜索。GitBook 的 `sync-manifest.json` 中记录源提交，可以和这次 GitHub 提交核对。

`LTC_DOCS_SITE_URL` 可覆盖自动识别的地址，适合自定义域名。值使用站点根目录的完整 URL，并保留末尾的 `/`。
