#!/usr/bin/env node
// claude-agents-config:managed

/*
 * SessionStart is deliberately ordered through this one hook:
 * fetch/validate provider catalog -> persist scoped cache -> drift proposal ->
 * fleet-reconcile (shared lock) -> one bounded hook response.  The Python
 * provider_catalog.py and fleet-reconcile.py modules own identity and settings
 * semantics; this file only supplies the Node runtime boundary for discovery.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const homeArgIndex = process.argv.indexOf("--home");
const configArgIndex = process.argv.indexOf("--config-home");
const home = homeArgIndex >= 0 && process.argv[homeArgIndex + 1]
  ? path.resolve(process.argv[homeArgIndex + 1])
  : process.env.CLAUDE_FLEET_HOME
    ? path.resolve(process.env.CLAUDE_FLEET_HOME)
    : path.dirname(path.dirname(path.resolve(process.argv[1]))) || os.homedir();
const configHome = configArgIndex >= 0 && process.argv[configArgIndex + 1]
  ? path.resolve(process.argv[configArgIndex + 1])
  : process.env.CLAUDE_FLEET_CONFIG_HOME
    ? path.resolve(process.env.CLAUDE_FLEET_CONFIG_HOME)
    : process.env.XDG_CONFIG_HOME
      ? path.resolve(process.env.XDG_CONFIG_HOME)
      : path.join(home, ".config");
const CLAUDE_DIR = path.join(home, ".claude");
const SETTINGS_PATH = path.join(CLAUDE_DIR, "settings.json");
const CACHE_PATH = path.join(CLAUDE_DIR, "cache", "omniroute-models-cache.json");
const POLICY_PATH = path.join(configHome, "delegate-skills", "provider-policy.json");
const DRIFT_SCRIPT_PATH = path.join(CLAUDE_DIR, "fleet-model-drift.py");
const RECONCILE_SCRIPT_PATH = path.join(CLAUDE_DIR, "fleet-reconcile.py");
const CATALOG_HELPER = path.join(CLAUDE_DIR, "provider_catalog.py");
const isQuiet = process.argv.includes("--quiet") || process.argv.includes("-q");
const runDrift = process.argv.includes("--drift");
const pythonArgIndex = process.argv.indexOf("--python");
const pythonRuntime = pythonArgIndex >= 0 && process.argv[pythonArgIndex + 1]
  ? process.argv[pythonArgIndex + 1]
  : process.env.CLAUDE_FLEET_PYTHON || "python3";

function log(message) {
  if (!isQuiet) process.stderr.write(`[omniroute-sync] ${message}\n`);
}

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return null; }
}

function ensureNoSymlinkParent(file) {
  let current = path.dirname(path.resolve(file));
  while (true) {
    try {
      if (fs.lstatSync(current).isSymbolicLink()) throw new Error(`symlinked parent: ${current}`);
      break;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      const parent = path.dirname(current);
      if (parent === current) break;
      current = parent;
    }
  }
}

function writeJsonAtomic(file, value, mode = 0o600) {
  const temp = `${file}.tmp.${process.pid}.${crypto.randomBytes(6).toString("hex")}`;
  let fd;
  try {
    ensureNoSymlinkParent(file);
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    fd = fs.openSync(temp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC, mode);
    const data = Buffer.from(JSON.stringify(value, null, 2) + "\n", "utf8");
    let offset = 0;
    while (offset < data.length) offset += fs.writeSync(fd, data, offset, data.length - offset);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.chmodSync(temp, mode);
    fs.renameSync(temp, file);
    return true;
  } catch {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch {} }
    try { fs.unlinkSync(temp); } catch {}
    return false;
  }
}

function callCatalog(args, input) {
  if (!fs.existsSync(CATALOG_HELPER)) return null;
  try {
    const result = spawnSync(pythonRuntime, [CATALOG_HELPER, ...args], {
      input: JSON.stringify(input), encoding: "utf8", timeout: 4000,
    });
    if (result.error || !result.stdout) return null;
    return JSON.parse(result.stdout);
  } catch { return null; }
}

function canonicalHome() {
  try { return fs.realpathSync.native(home); } catch { return path.resolve(home); }
}

function sharedLockPath() {
  const digest = crypto.createHash("sha256").update(canonicalHome()).digest("hex");
  return path.join(os.tmpdir(), `claude-agents-config-${digest}.lock`);
}

function acquireSharedLock() {
  const lockPath = sharedLockPath();
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const fd = fs.openSync(lockPath, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
      fs.writeSync(fd, String(process.pid));
      return { fd, lockPath };
    } catch (error) {
      if (error?.code !== "EEXIST" || attempt > 0) return undefined;
      try {
        if (Date.now() - fs.statSync(lockPath).mtimeMs < 60_000) return undefined;
        fs.unlinkSync(lockPath);
      } catch { return undefined; }
    }
  }
  return undefined;
}

function releaseSharedLock(lock) {
  if (!lock) return;
  try { fs.closeSync(lock.fd); } catch {}
  try { fs.unlinkSync(lock.lockPath); } catch {}
}

function fetchCatalog(settings) {
  const endpoint = settings?.env?.ANTHROPIC_BASE_URL;
  const token = settings?.env?.ANTHROPIC_AUTH_TOKEN;
  if (typeof endpoint !== "string" || !endpoint || typeof token !== "string" || !token) return null;
  const response = callCatalog(["--fetch", "--endpoint", endpoint, "--timeout", "2.5"], {
    endpoint, token,
  });
  if (response?.complete === true && Array.isArray(response.rows)) {
    return { endpoint, token, rows: response.rows, network: true };
  }
  const cached = readJson(CACHE_PATH);
  const cachedResponse = callCatalog(["--cache-read", "--endpoint", endpoint], {
    cache: cached, endpoint, token,
  });
  if (cachedResponse?.valid === true && Array.isArray(cachedResponse.rows)) {
    return { endpoint, token, rows: cachedResponse.rows, network: false };
  }
  return { endpoint, token, rows: null, network: false };
}

function runDriftDetector() {
  if (!runDrift || !fs.existsSync(DRIFT_SCRIPT_PATH)) return null;
  try {
    const result = spawnSync(pythonRuntime, [
      DRIFT_SCRIPT_PATH, "--home", home, "--config-home", configHome,
    ], { timeout: 3000, encoding: "utf8" });
    return (result.stdout || "").trim() || null;
  } catch { return null; }
}

function runReconcile() {
  // A missing script or cache is an expected not-yet-configured state, not a
  // failure worth reporting. Once the child actually runs, though, a bounded
  // failure (nonzero exit, timeout/signal, or unparseable output) must be
  // surfaced to the caller rather than silently collapsed to null: a poisoned
  // reconciliation is otherwise invisible to the user and to `--check`.
  if (!fs.existsSync(RECONCILE_SCRIPT_PATH) || !fs.existsSync(CACHE_PATH)) return null;
  let result;
  try {
    result = spawnSync(pythonRuntime, [
      RECONCILE_SCRIPT_PATH,
      "--home", home,
      "--config-home", configHome,
      "--policy", POLICY_PATH,
      "--catalog", CACHE_PATH,
      "--session-start",
      "--lock-held",
    ], { timeout: 5000, encoding: "utf8" });
  } catch (error) {
    return { error: `could not start (${error?.code || error?.name || "spawn error"})` };
  }
  if (result.error) {
    return { error: `could not start (${result.error.code || result.error.message || "spawn error"})` };
  }
  if (result.signal) {
    return { error: `terminated by signal ${result.signal}` };
  }
  if (typeof result.status === "number" && result.status !== 0) {
    return { error: `exited with status ${result.status}` };
  }
  if (!result.stdout) {
    return { error: "produced no output" };
  }
  try {
    return JSON.parse(result.stdout);
  } catch {
    return { error: "produced malformed output" };
  }
}

function response(driftNotice, reconcileResult, extraMessage) {
  const messages = [];
  if (extraMessage) messages.push(extraMessage);
  if (reconcileResult?.error) messages.push(`Fleet reconciliation failed and was skipped: ${reconcileResult.error}.`);
  if (reconcileResult?.summary) messages.push(reconcileResult.summary);
  if (reconcileResult?.systemMessage) messages.push(reconcileResult.systemMessage);
  const output = {};
  if (messages.length) output.systemMessage = messages.join(" ");
  if (driftNotice) {
    output.hookSpecificOutput = {
      hookEventName: "SessionStart",
      additionalContext: driftNotice,
    };
  }
  return Object.keys(output).length ? output : null;
}

async function run() {
  const settings = readJson(SETTINGS_PATH);
  const policy = readJson(POLICY_PATH);
  if (!settings || !policy || policy.version !== "provider-policy.v1" || policy.provider !== "omniroute") return null;
  if (settings.env?.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY !== "1") return null;

  // Network work stays outside the writer lock. Only cache persistence,
  // proposal generation, and settings/fleet reconciliation are serialized.
  const discovery = fetchCatalog(settings);
  const lock = acquireSharedLock();
  if (!lock) return response(null, null, "Provider reconciliation was deferred because another fleet writer holds the lock.");
  try {
    let extraMessage = null;
    if (discovery?.network === true && Array.isArray(discovery.rows)) {
      const cacheResult = callCatalog(["--cache-payload", "--endpoint", discovery.endpoint], {
        rows: discovery.rows, endpoint: discovery.endpoint, token: discovery.token,
      });
      if (!cacheResult || !writeJsonAtomic(CACHE_PATH, cacheResult, 0o600)) {
        extraMessage = "Live provider data was received but could not be cached; existing fleet settings were preserved.";
      }
    } else if (!discovery || !Array.isArray(discovery.rows)) {
      extraMessage = "Provider discovery is unavailable or stale; existing fleet and picker were preserved.";
    }
    const driftNotice = discovery && Array.isArray(discovery.rows) ? runDriftDetector() : null;
    const reconcileResult = runReconcile();
    return response(driftNotice, reconcileResult, extraMessage);
  } finally {
    releaseSharedLock(lock);
  }
}

async function main() {
  const output = await run().catch(error => {
    log(`reconciliation failed: ${error?.name || "error"}`);
    return null;
  });
  if (output) process.stdout.write(JSON.stringify(output) + "\n");
}

main().catch(() => {});
