# 运行命令

用 `ltc run` 提交训练、构建、测试或数据处理任务。请让 Agent 在当前会话中执行命令，LTC 会记录这次任务的回调目标。

## 提交任务

```bash
ltc run --cwd "$PWD" --task "训练 ResNet 基线" -- python train.py --config configs/resnet.yaml
```

`--cwd` 设置工作目录。配置文件、输入数据和输出目录中的相对路径，都以这个目录为起点。`--task` 写一个容易辨认的名称，方便之后在回调里找到这次运行。

LTC 把 `--` 后面的内容作为要执行的程序及其参数。任务会继承提交时的环境，所以先激活训练环境，再让 Agent 提交：

```bash
source .venv/bin/activate
ltc run --cwd "$PWD" --task "运行测试" -- python -m pytest tests/
```

## 查看日志

提交输出中会给出 `task log` 路径。复制这个路径，用编辑器打开，或在终端中查看最近的输出：

```bash
tail -n 60 /实际日志路径/attempt-1.log
```

任务结束后，回调也会带上这个路径。让 Agent 结合退出码、日志和项目产物判断执行结果，例如读取测试汇总或检查训练输出的 checkpoint。

## 执行一段脚本

命令较长时，把步骤写进脚本更容易维护：

```bash
ltc run --cwd "$PWD" --task "构建安装包" -- bash scripts/build.sh
```

如果需要 shell 管道或重定向，也可以显式调用 shell：

```bash
ltc run --cwd "$PWD" --task "生成报告" -- bash -lc 'python analyse.py && python report.py'
```

在 Windows 上，可用 `powershell.exe -File scripts/build.ps1` 作为实际命令。

## 指定执行后端

默认的 `--backend auto` 根据操作系统选择后端。在使用 GNU screen 的服务器或容器中，可以明确选择：

```bash
ltc run --backend screen --cwd "$PWD" --task "训练模型" -- python train.py
```

安装与后端配置见[安装指南](../installation.md)。完成后的处理步骤见[接收与确认回调](callbacks.md)。
