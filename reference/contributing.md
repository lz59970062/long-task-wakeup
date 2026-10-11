# 参与文档维护

文档源文件和代码保存在同一个仓库。修改对应 Markdown 文件，构建后就可以预览页面。

## 本地预览

在仓库根目录中运行：

```bash
python3 -m venv .venv-docs
source .venv-docs/bin/activate
python -m pip install -r docs/requirements.txt
mkdocs serve
```

打开终端给出的本地地址。修改文件后，页面会自动更新。

## 添加一篇教程

按内容选择位置：

| 目录 | 内容 |
| --- | --- |
| `docs/guides/` | 完成一项具体工作的操作步骤 |
| `docs/integrations/` | 各 Agent 的配置与使用 |
| `docs/reference/` | 参数、配置和开发资料 |

中文文件使用 `topic.md`，英文使用同目录的 `topic.en.md`。在 `mkdocs.yml` 的 `nav` 中加入基础文件名，再在 `nav_translations` 中填写英文标题。两种语言中的链接都写基础文件名，例如 `../installation.md`，构建时会自动指向对应语言。

从相关教程链接到新页面。中文沿用站点根路径，英文位于 `/en/`；顶部语言菜单可以切换到当前页面的另一种语言。

一篇教程从读者要做的事开始，给出可执行的命令，说明预期输出和下一步。参数限制写成具体的配置要求，遇到问题给出处理步骤。

## 检查与发布

提交前运行：

```bash
python scripts/docs_sync.py check
mkdocs build --strict
python scripts/docs_sync.py export
```

构建会检查配置和文档链接。合并到 `main` 后，GitHub Actions 构建并发布 GitHub Pages。

检查命令要求导航中的每一页都有中英文版本。导出命令生成 GitBook 的双语 Markdown、目录和配置。各平台的首次接入与自动同步步骤见[文档发布与同步](publishing.md)。
