// Generic Pi callback integration. No user templates or model configuration.
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import * as crypto from "node:crypto";
import { execFileSync } from "node:child_process";

const SLOT = Symbol.for("long-task-callback.pi-extension.v1");
const norm = (value) => process.platform === "win32" ? value.toLowerCase() : value;
const digest = (value) => crypto.createHash("sha256").update(value).digest("hex");

function canonical(value) {
  if (typeof value !== "string" || !path.isAbsolute(value)) throw new Error("An absolute session file is required");
  return norm(fs.existsSync(value) ? fs.realpathSync(value) : path.resolve(value));
}

function privateDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(directory);
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Unsafe Pi callback directory");
  if (process.platform !== "win32" && (stat.uid !== process.getuid() || (stat.mode & 0o077))) {
    throw new Error("Pi callback directory must be private");
  }
  if (process.platform === "win32") {
    // Read-only ACL inspection; do not trust an installer marker after ACLs
    // may have changed. Failure means no mailbox publication on this path.
    const script = "$ErrorActionPreference='Stop'; $a=Get-Acl -LiteralPath $env:LTC_PI_ACL_PATH; " +
      "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; " +
      "$allowed=@($sid,'S-1-5-18','S-1-5-32-544'); " +
      "$rules=$a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]); " +
      "foreach($r in $rules){if($r.AccessControlType -eq 'Allow' -and $allowed -notcontains $r.IdentityReference.Value){exit 2}}";
    execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], {
      env: { ...process.env, LTC_PI_ACL_PATH: directory }, timeout: 5000, windowsHide: true, stdio: "pipe" });
  }
}

function readRecord(file, versions = [1]) {
  const stat = fs.lstatSync(file);
  if (stat.isSymbolicLink() || !stat.isFile() || stat.size > 2 * 1024 * 1024) throw new Error("Unsafe Pi callback record");
  const value = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!value || !versions.includes(value.version)) throw new Error("Invalid Pi callback record");
  return value;
}

function writeRecord(file, value) {
  if (fs.existsSync(file) && fs.lstatSync(file).isSymbolicLink()) throw new Error("Preserving a record symlink");
  const temporary = `${file}.${crypto.randomUUID()}.tmp`;
  let fd;
  try {
    fd = fs.openSync(temporary, "wx", 0o600);
    fs.writeFileSync(fd, JSON.stringify(value) + "\n", "utf8");
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temporary, file);
    if (process.platform !== "win32") {
      const directory = fs.openSync(path.dirname(file), "r");
      try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    }
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

function alive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error.code !== "ESRCH"; }
}

export default function ltcCallback(pi) {
  // Both the installed copy and explicitly supplied bundled copy can load.
  // A disposed generation clears this slot so /reload gets fresh handlers.
  if (globalThis[SLOT]?.active) return;
  const state = { active: true, context: undefined, owner: undefined, directory: undefined,
                  timer: undefined, polling: false, error: undefined, admitted: new Map() };
  globalThis[SLOT] = state;
  const managed = Boolean(process.env.LTC_PI_RESERVATION);
  const recovering = process.env.LTC_PI_RECOVERY === "1";

  function notice(message) {
    state.context?.ui?.notify?.(`LTC: ${message}`, "warning");
  }

  function receipt(file, envelope, status) {
    writeRecord(file, { version: 1, state: status, callback_id: envelope.callback_id,
      pi_delivery: envelope.pi_delivery ?? "follow-up",
      queue_dir: envelope.queue_dir, owner_nonce: envelope.owner_nonce,
      session_file: envelope.session_file, recorded_at: Date.now() / 1000 });
  }

  function observedMessages() {
    if (!state.context || state.admitted.size === 0) return;
    const users = state.context.sessionManager.getBranch()
      .filter((entry) => entry.type === "message" && entry.message?.role === "user")
      .map((entry) => typeof entry.message.content === "string" ? entry.message.content :
        entry.message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n"));
    for (const [file, envelope] of state.admitted) {
      if (fs.existsSync(envelope.ack_path)) {
        receipt(file, envelope, "acknowledged");
        state.admitted.delete(file);
      } else if (users.includes(envelope.prompt)) {
        receipt(file, envelope, "session_observed");
        state.admitted.delete(file);
      }
    }
  }

  function poll() {
    if (state.polling || !state.active || !state.owner || recovering) return;
    state.polling = true;
    try {
      if (canonical(state.context.sessionManager.getSessionFile()) !== state.owner.session_file) return;
      const owner = readRecord(path.join(state.directory, "owner.json"));
      if (owner.owner_nonce !== state.owner.owner_nonce || owner.owner_pid !== process.pid || owner.state !== "active") return;
      if (managed && (!alive(owner.lease_pid) || (process.platform !== "win32" && process.ppid !== owner.lease_pid))) return;
      observedMessages();
      const inbox = path.join(state.directory, "inbox");
      for (const filename of fs.readdirSync(inbox).filter((name) => /^[a-f0-9]{64}\.json$/.test(name)).sort()) {
        const envelope = readRecord(path.join(inbox, filename), [1, 2]);
        const delivery = envelope.pi_delivery ?? "follow-up";
        if ((envelope.version === 1 && delivery !== "follow-up") ||
            (envelope.version === 2 && delivery !== "steer")) continue;
        if (envelope.owner_nonce !== owner.owner_nonce || envelope.session_file !== owner.session_file) continue;
        if (typeof envelope.callback_id !== "string" || !/^[A-Za-z0-9_-]+$/.test(envelope.callback_id) ||
            typeof envelope.prompt !== "string" || !envelope.prompt || typeof envelope.queue_dir !== "string" ||
            !path.isAbsolute(envelope.queue_dir)) continue;
        const queue = canonical(envelope.queue_dir);
        if (canonical(envelope.ack_path) !== canonical(path.join(queue, "acks", `${envelope.callback_id}.json`)) ||
            canonical(envelope.canceled_path) !== canonical(path.join(queue, "canceled", `${envelope.callback_id}.json`)) ||
            filename !== `${digest(queue + "\0" + envelope.callback_id)}.json`) continue;
        const received = path.join(state.directory, "receipts", filename);
        if (fs.existsSync(received)) continue; // Never replay an ambiguous admission.
        if (fs.existsSync(envelope.ack_path)) { receipt(received, envelope, "acknowledged"); continue; }
        if (fs.existsSync(envelope.canceled_path)) { receipt(received, envelope, "canceled"); continue; }
        // Older callbacks retain their follow-up intent. New callbacks enter
        // Pi's native steer queue while busy, before the next model call, and
        // start a normal turn while idle. Legacy envelopes must not block them.
        if (delivery === "follow-up" && !state.context.isIdle()) continue;
        // Native API returns void. This marker records an attempt, not success.
        receipt(received, envelope, "admitted");
        if (fs.existsSync(envelope.canceled_path)) { receipt(received, envelope, "canceled"); continue; }
        if (managed && (!alive(owner.lease_pid) || (process.platform !== "win32" && process.ppid !== owner.lease_pid))) return;
        state.admitted.set(received, envelope);
        pi.sendUserMessage(envelope.prompt, { deliverAs: delivery === "steer" ? "steer" : "followUp" });
        break;
      }
    } catch (error) {
      // Leave every published/admitted envelope intact for ACK or recovery.
      // Mailbox failure must not freeze ordinary work under a valid writer.
      state.mailboxError = String(error.message ?? error);
    } finally { state.polling = false; }
  }

  pi.on("session_start", async (_event, context) => {
    state.active = true;
    globalThis[SLOT] = state;
    state.admitted.clear();
    if (state.timer) clearInterval(state.timer);
    state.context = context;
    try {
      const file = context.sessionManager.getSessionFile();
      if (!file) return; // --no-session children have no callback owner.
      const session = canonical(file);
      const profile = canonical(path.resolve(process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent")));
      const root = path.resolve(process.env.LTC_PI_CHANNEL_ROOT || path.join(profile, "long-task-callback", "channels"));
      if (process.platform === "win32" && !fs.existsSync(root)) {
        throw new Error("Install the extension with LTC to initialize private Windows channel permissions");
      }
      privateDirectory(root);
      const directory = path.join(root, digest(session));
      privateDirectory(directory);
      privateDirectory(path.join(directory, "inbox"));
      privateDirectory(path.join(directory, "receipts"));
      const ownerPath = path.join(directory, "owner.json");
      const previous = fs.existsSync(ownerPath) ? readRecord(ownerPath) : undefined;
      if (managed) {
        const checks = { reservation: previous?.managed === true, session: previous?.session_file === session,
          nonce: previous?.owner_nonce === process.env.LTC_PI_RESERVATION, lease: alive(previous?.lease_pid),
          unclaimed: previous?.state === "reserved" || previous?.owner_pid === process.pid,
          parent: previous?.state !== "reserved" || process.platform === "win32" || previous?.lease_pid === process.ppid,
          sessionId: previous?.session_id === context.sessionManager.getSessionId() };
        const failures = Object.entries(checks).filter(([, valid]) => !valid).map(([name]) => name);
        if (failures.length) throw new Error(`Managed Pi ownership handshake failed: ${failures.join(", ")}`);
      } else if (previous && previous.owner_pid !== process.pid && alive(previous.owner_pid)) {
        throw new Error("Another Pi process owns this callback mailbox");
      }
      const owner = { ...(managed ? previous : {}), version: 1, session_file: session,
        delivery_modes: ["follow-up", "steer"],
        session_id: context.sessionManager.getSessionId(), profile_dir: profile, channel_dir: directory,
        owner_nonce: managed ? process.env.LTC_PI_RESERVATION : crypto.randomUUID(),
        owner_pid: process.pid, managed, state: "active", recorded_at: Date.now() / 1000 };
      if (previous?.owner_pid !== process.pid) delete owner.owner_identity;
      const spawnPath = path.join(directory, `spawn-${owner.owner_nonce}.json`);
      if (managed && fs.existsSync(spawnPath)) {
        const spawn = readRecord(spawnPath);
        if (spawn.owner_nonce === owner.owner_nonce && spawn.owner_pid === process.pid) {
          owner.owner_identity = spawn.owner_identity;
        }
      }
      writeRecord(ownerPath, owner);
      state.owner = owner;
      state.directory = directory;
      state.error = undefined;
      state.timer = setInterval(poll, 200);
      state.timer.unref();
    } catch (error) {
      state.error = String(error.message ?? error);
      notice(state.error);
      if (managed) {
        // Built-in commands bypass input hooks. Do not enter Pi's input loop
        // with a failed ownership handshake, even briefly.
        process.stderr.write(`LTC: ${state.error}; exiting\n`);
        process.exit(2);
      }
    }
  });

  pi.on("input", async (_event, context) => {
    if (!managed || !context.sessionManager.getSessionFile()) return { action: "continue" };
    if (recovering && [process.env.LTC_PI_CALLBACK_ACK_PATH, process.env.LTC_PI_CALLBACK_CANCEL_PATH]
        .some((file) => file && fs.existsSync(file))) return { action: "handled" };
    try {
      if (state.error || !state.owner || canonical(context.sessionManager.getSessionFile()) !== state.owner.session_file ||
          !alive(state.owner.lease_pid) || (process.platform !== "win32" && process.ppid !== state.owner.lease_pid)) {
        throw new Error("Managed Pi session ownership is unavailable");
      }
      const current = readRecord(path.join(state.directory, "owner.json"));
      if (current.owner_nonce !== state.owner.owner_nonce || current.owner_pid !== process.pid) {
        throw new Error("Managed Pi owner changed");
      }
      return { action: "continue" };
    } catch (error) {
      notice(String(error.message ?? error));
      return { action: "handled" }; // Pi treats a thrown input hook as continue.
    }
  });

  pi.on("session_before_switch", async () => {
    if (!managed) return;
    // Even reopening this same file can race a settling native turn's append.
    // Keep the in-memory branch until the managed launcher exits.
    notice("Start ltc pi with the chosen session to switch managed files");
    return { cancel: true };
  });
  pi.on("session_before_fork", async () => {
    if (!managed) return;
    notice("Start a separate ltc pi session to fork managed work");
    return { cancel: true };
  });
  pi.on("agent_end", async () => { try { observedMessages(); } catch { /* Remain uncertain. */ } });
  pi.on("session_shutdown", async (event) => {
    state.active = false;
    if (state.timer) clearInterval(state.timer);
    try {
      if (state.owner) {
        const file = path.join(state.directory, "owner.json");
        const current = readRecord(file);
        if (current.owner_nonce === state.owner.owner_nonce && current.owner_pid === process.pid) {
          writeRecord(file, { ...current, state: "closed", recorded_at: Date.now() / 1000 });
        }
      }
    } catch { /* A failed tombstone never authorizes offline recovery. */ }
    if (globalThis[SLOT] === state) delete globalThis[SLOT];
    if (managed && event.reason === "reload") {
      // A failed extension reload would remove the session pin before native
      // commands run. Restart the managed launcher to reload resources safely.
      process.stderr.write("LTC: restart ltc pi to reload managed session resources\n");
      process.exit(0);
    }
  });
}
