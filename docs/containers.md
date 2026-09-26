# Linux containers and AutoDL

LTC 0.7 uses GNU screen when a systemd user manager is unavailable. A container
does not need systemd, privileged mode, a host service socket or the Docker socket
to run this backend. Keep LTC, the selected Agent CLI, its session files and the
workload in the **same running container**, under the same user account.

There are two independent choices:

| Setting | What it controls | Container choice |
| --- | --- | --- |
| `--backend` on `run`, `agent` or `setup` | The owner of each workload | `screen` |
| `--service` on `setup` | How the callback coordinator starts | `standalone`, an explicitly selected existing `supervisor`, or run `ltc daemon` in the foreground yourself |

`setup --service auto` uses an available systemd user manager, otherwise standalone
mode. It does not automatically modify a platform-managed Supervisor installation.
The selected workload backend is saved in each task; recovery keeps that owner.

## Docker image

From the repository root:

```bash
docker build -f examples/docker/Dockerfile -t ltc:0.7.0a1 .
docker run --rm ltc:0.7.0a1 ltc --version
docker run --rm ltc:0.7.0a1 screen --version
```

The [example Dockerfile](../examples/docker/Dockerfile) installs Python-based LTC
and screen. It contains **no Agent CLI, model runtime, GPU stack or workload
dependencies**. Extend this image with the Codex or Claude Code CLI and the
dependencies required by your commands before expecting a real callback. The
following example uses `ltc-agent:0.7.0a1` as the name of that prepared image.
Its selected Agent executable must be on the `ltc` user's `PATH`.

An existing Debian/Ubuntu Python image can supply the base without changing the
Dockerfile:

```bash
docker build -f examples/docker/Dockerfile \
  --build-arg BASE_IMAGE=your-local-python-image:tag \
  -t ltc:0.7.0a1 .
```

The override requires Python 3.9+ and `apt-get`; Alpine images use different
package tooling and cannot be substituted into this Dockerfile unchanged.
The build context allowlist excludes local profiles, task records, Git history
and committee discussions. Do not add credentials through `COPY`, build arguments
or image environment values.

### Start the coordinator and agent

Choose an existing absolute workspace path on the Docker host. The image uses UID
1000; that user must be able to write the mounted workspace. Adjust the image user
or workspace permissions for your deployment if the host uses a different UID.

```bash
export LTC_WORKSPACE=/absolute/path/to/your/project
docker volume create ltc-preview-state
docker run -d --init --name ltc-preview \
  --mount type=volume,src=ltc-preview-state,dst=/state \
  --mount "type=bind,src=$LTC_WORKSPACE,dst=/workspace" \
  ltc-agent:0.7.0a1
docker exec ltc-preview ltc install-skill --target both
docker logs ltc-preview
```

`--init` adds a small init process to handle child-process reaping. A full systemd
installation inside the container is unnecessary for this arrangement.
[Docker process guidance](https://docs.docker.com/engine/containers/multi-service_container/)

Authenticate your selected Agent interactively **inside this container**, using
its supported login method. Its configuration and session directories are runtime
volume paths: `/state/codex` for `CODEX_HOME`, `/state/claude` for
`CLAUDE_CONFIG_DIR`. Existing dedicated profiles can instead be mounted at those
locations when the Agent supports that arrangement. They need write access for
session updates. Supply API credentials at container startup through your runtime
credential mechanism if the Agent uses environment-based authentication; the
coordinator also needs access when it launches a callback. Credentials supplied
only to one `docker exec` shell do not become the coordinator's environment.

Open the installed CLI in the container, for example:

```bash
docker exec -it --workdir /workspace ltc-preview codex
# Or, when that is the CLI installed in your image:
# docker exec -it --workdir /workspace ltc-preview claude
```

From that Agent's shell/tool context, submit the actual workload:

```bash
ltc run --backend screen --cwd /workspace \
  --task "train experiment" -- python /workspace/train.py
```

The launching Agent normally supplies its session identifier. An unrelated
`docker exec` shell does not inherit the Agent's session; explicitly pass
`--agent codex --session <actual-container-session-id>` when submitting there.
Do not substitute a host desktop session ID: the container CLI must be able to
resume the bound session itself. The example disables Codex Desktop socket
delivery and uses the CLI callback path within the container.

### What persists

| Runtime path | Content |
| --- | --- |
| `/state/codex/long-task-wakeup/` | Queue, task metadata, logs, compact callback details, ACKs, goals and reminder settings |
| `/state/codex/` and `/state/claude/` | Agent profiles, credentials and session history stored by the installed CLI |
| `/workspace/` | Your code, outputs and checkpoints |

The state volume and workspace mount must remain available at the same container
paths while tasks are active. Keep only one coordinator per queue. A named volume
outlives a container unless you explicitly remove it; a bind mount exposes the
chosen host directory to the container.
[Docker volumes](https://docs.docker.com/engine/storage/volumes/),
[bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

The image runs `ltc daemon` in the foreground. **If that main service exits, the
container stops and its running tasks stop too.** Screen cannot outlive its
container. If coordinator restarts must leave workloads running, use your existing
Supervisor/process manager as the container's main service and let it manage
`ltc daemon` as a child. Restart only that coordinator child; stopping the whole
container still ends all workloads. Do not use `ltc setup --service standalone
--now` as a container's sole `CMD`: setup returns after starting its background
process, which ends the container's main command.

## AutoDL or an existing Linux development container

Use the environment already provided by the platform; a second Docker daemon is
not required. Install screen with the image's package manager and install LTC into
the Python environment you intend to keep available:

```bash
# Debian/Ubuntu-based image, as its administrator:
apt-get update
apt-get install -y screen

# From the LTC checkout, in the selected Python environment:
python -m pip install .
ltc --version
screen --version
```

Select a directory on storage that your platform retains. Replace the example
path below with that actual path; LTC does not determine the retention policy of
an AutoDL disk or rental instance. Choose the Agent profile locations **before
opening the session**. If you already have persistent profiles, keep their current
paths instead; exporting a new path does not copy old sessions or authentication.

```bash
export LTC_PERSIST_ROOT=/absolute/path/to/persistent-disk/ltc-preview
mkdir -p "$LTC_PERSIST_ROOT"
export CODEX_HOME="$LTC_PERSIST_ROOT/codex"
export CLAUDE_CONFIG_DIR="$LTC_PERSIST_ROOT/claude"
export LTC_EXECUTION_BACKEND=screen
export CODEX_LONG_TASK_WAKEUP_DESKTOP_APP_SERVER=0

# Install the selected Agent CLI and make its authentication available first.
ltc setup --backend screen --service standalone --now
ltc status
```

Keep these environment settings consistent in the Agent and coordinator startup
environments. A detached standalone daemon continues after the launching shell
closes while the platform keeps the container alive. It is **not** a boot service
or a crash supervisor. After restarting the platform/container, restore the same
environment and run setup again. Its log is
`$CODEX_HOME/long-task-wakeup/daemon.log`.

Standalone coordinator PID/runtime files belong to the selected `CODEX_HOME`.
Use a separate profile directory for an isolated preview or another standalone
coordinator, and start its Agent with that same profile. Merely changing
`--queue-dir` does not isolate those coordinator files.

If the container already has a Supervisor that you administer, use
`ltc setup --backend screen --service supervisor --now` to request that integration
explicitly. It needs permission to write and load that Supervisor's configuration.
For an externally managed startup command, use `ltc daemon` directly in the
foreground instead of launching another standalone coordinator.

## Stop, restart and callback boundaries

- Closing an interactive shell does not stop screen-owned tasks while their
  container and process manager stay alive.
- Stopping, replacing or deleting the container interrupts its processes. Saved
  files do not preserve a running process. Inspect the recorded result and
  checkpoints before submitting new work; LTC does not automatically rerun an
  interrupted business command.
- Recreating a container can change its host/container identity and the available
  Agent sessions, even with the same volume. LTC can classify its old task records
  as belonging to another host. Persistent files enable inspection, but do not
  establish cross-container or cross-host callback recovery.
- Keep the original bound Agent session and its files accessible. A new login or
  a new conversation is not permission to redirect an old callback. Do not use
  `--last` to work around missing session history.
- The Linux container path does not establish native macOS or Windows support.
  GPU access and checkpointing belong to your workload/container configuration.

The Dockerfile and these commands are deployment examples. A working image build
or CLI version check does not verify real Agent authentication and original-session
callback delivery; validate that complete path in the prepared container before
relying on it for unattended work.
