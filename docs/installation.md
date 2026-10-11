# 安装

准备 Python 3.9 或更新版本、Git，以及已经完成登录的 Agent CLI。LTC 和 Agent 使用同一个用户账号运行。

## 安装正式版

以下命令安装 `0.7.0`。虚拟环境用来保存 LTC 及其依赖，安装完成后保留这个目录。

=== "Linux / macOS"

    ```bash
    git clone --branch v0.7.0 --depth 1 https://github.com/lz59970062/long-task-wakeup.git
    cd long-task-wakeup
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install .
    ltc setup --enable --now
    ltc --version
    ```

=== "Windows PowerShell"

    ```powershell
    git clone --branch v0.7.0 --depth 1 https://github.com/lz59970062/long-task-wakeup.git
    Set-Location long-task-wakeup
    py -3 -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install .
    & .\.venv\Scripts\ltc.exe setup --service windows-task --enable --now
    & .\.venv\Scripts\ltc.exe --version
    ```

`setup` 安装 Agent 技能，并配置后台协调器。最后一条命令显示版本号。Windows 后续调用可以继续使用 `.venv\Scripts\ltc.exe` 的完整路径。

## 选择运行环境

| 环境 | 任务由谁托管 | 使用方式 |
| --- | --- | --- |
| Linux 桌面或服务器 | systemd 用户服务 | 使用默认 `setup` |
| macOS 桌面登录会话 | launchd | 在当前用户的终端中运行 `setup` |
| Windows 10 及更新版本 | 计划任务与 Job Object | 保持该用户登录，使用本地 NTFS 目录 |
| Docker、AutoDL 或使用 screen 的 Linux | GNU screen | 按下面的容器步骤配置 |

在 Debian / Ubuntu 容器中，先安装 screen，再启动协调器：

```bash
sudo apt-get install screen
ltc setup --backend screen --service standalone --now
```

容器内的 LTC、Agent、工作目录和会话文件放在同一个运行环境中。把 `~/.ltc`、Agent 配置目录和项目目录放在持久卷里，可以在重启后继续读取任务记录。镜像和启动配置见[容器部署资料](https://github.com/lz59970062/long-task-wakeup/blob/main/docs/containers.md)。

## 安装预览版

`0.7.1` 预览版包含 Claude 实时回调、Pi 接入和 Desktop 启动器。使用一个新的目录安装当前 `main`：

```bash
git clone https://github.com/lz59970062/long-task-wakeup.git ltc-preview
cd ltc-preview
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
ltc setup --force --enable --now
ltc --version
```

Windows 使用上面的 PowerShell 安装方式，将克隆命令中的 `--branch v0.7.0 --depth 1` 去掉。

从已有版本升级时，先处理完队列中的任务和回调，再安装新版本并运行 `ltc setup --keep-skill --force --enable --now`。这会更新协调器并保留已有技能内容。

安装完成后，进入[第一个任务](quickstart.md)。
