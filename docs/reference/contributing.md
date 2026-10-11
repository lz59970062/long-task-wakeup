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

在 `mkdocs.yml` 的 `nav` 中加入页面，再从相关教程链接到它。

一篇教程从读者要做的事开始，给出可执行的命令，说明预期输出和下一步。参数限制写成具体的配置要求，遇到问题给出处理步骤。

## 检查与发布

提交前运行：

```bash
mkdocs build --strict
```

构建会检查配置和文档链接。合并到 `main` 后，GitHub Actions 构建并发布 GitHub Pages。

仓库中的 `.readthedocs.yaml` 使用相同的配置和依赖。在 Read the Docs 导入这个公开仓库后，选择 `main` 构建，即可发布同一套文档。设置 `LTC_DOCS_SITE_URL` 可以指定构建站点的完整网址。
