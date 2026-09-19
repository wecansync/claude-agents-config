#!/usr/bin/env node
// claude-agents-config:managed

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
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
const SETTINGS_PATH = path.join(home, ".claude", "settings.json");
const FLEET_PATH = path.join(configHome, "delegate-skills", "config.json");
const CACHE_PATH = path.join(home, ".claude", "cache", "omniroute-models-cache.json");
const DRIFT_SCRIPT_PATH = path.join(home, ".claude", "fleet-model-drift.py");
const REQUIRED_FLEET_DESCRIPTION = "Required by delegate fleet";
const isQuiet = process.argv.includes("--quiet") || process.argv.includes("-q");
const isForce = process.argv.includes("--force") || process.argv.includes("-f");
// Only the installer-managed SessionStart hook passes --drift. Other
// invocations of this script (for example a shell wrapper that some local
// setups add to run it once before launching Claude Code) must not run the
// detector: it dedups by models_hash, so a flagless run right before the
// hook would consume the one drift notice the hook needed to surface via
// additionalContext, and the SessionStart run would then emit nothing.
const runDrift = process.argv.includes("--drift");
const pythonArgIndex = process.argv.indexOf("--python");
const pythonRuntime = pythonArgIndex >= 0 && process.argv[pythonArgIndex + 1]
  ? process.argv[pythonArgIndex + 1]
  : process.env.CLAUDE_FLEET_PYTHON || "python3";

function log(msg) { if (!isQuiet) process.stderr.write("[omniroute-sync] " + msg + "\n"); }
function readJson(file) { try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return null; } }
function modelKey(row) { return row?.model || ""; }
function effectiveModelBase(rawId) {
  const withoutSuffix = rawId.replace(/\[\d+[kmKM]?\]\s*$/, "");
  const parts = withoutSuffix.split("/");
  return (parts.length > 1 ? parts.at(-1) : withoutSuffix).trim();
}

// Runs the drift detector synchronously, in-process with this script, right
// after the cache below is known fresh (just written, or the most recent
// offline cache). SessionStart hooks registered for the same event run
// independently of each other with no ordering guarantee, so a *separate*
// SessionStart hook for drift detection could read the cache before this
// script finishes writing it. Shelling out to the detector from here instead
// serializes "write cache" before "read cache" by construction and removes
// that race entirely.
function runDriftDetector() {
  if (!runDrift) return null;
  if (!fs.existsSync(DRIFT_SCRIPT_PATH)) return null;
  try {
    const result = spawnSync(pythonRuntime, [
      DRIFT_SCRIPT_PATH, "--home", home, "--config-home", configHome,
    ], { timeout: 3000, encoding: "utf8" });
    // fleet-model-drift.py prints its (already-flushed) notice to stdout
    // before it persists the "seen" proposal state, specifically so a
    // subprocess that errors or is killed (timeout, signal) *after*
    // printing but before finishing its own write still delivers the
    // notice here. So: trust any captured stdout text regardless of
    // result.error/status -- discard only when there is truly nothing.
    const text = (result.stdout || "").trim();
    return text || null;
  } catch {
    return null;
  }
}

function finish(driftNotice, removalMessage) {
  if (!driftNotice && !removalMessage) return null;
  const payload = {};
  if (removalMessage) payload.systemMessage = removalMessage;
  if (driftNotice) payload.hookSpecificOutput = { hookEventName: "SessionStart", additionalContext: driftNotice };
  return payload;
}

function acquireSettingsLock(lockPath) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return fs.openSync(lockPath, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
    } catch (error) {
      if (error?.code !== "EEXIST" || attempt > 0) return undefined;
      try {
        const age = Date.now() - fs.statSync(lockPath).mtimeMs;
        // A model-sync hook is bounded to seconds. A much older lock can only
        // be an orphan left by a killed process, so recover it fail-open.
        if (age < 60_000) return undefined;
        fs.unlinkSync(lockPath);
      } catch {
        return undefined;
      }
    }
  }
  return undefined;
}

async function run() {
  if (process.env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY !== "1") return null;
  const settings = readJson(SETTINGS_PATH);
  if (!settings) return null;
  const baseUrl = settings.env?.ANTHROPIC_BASE_URL?.replace(/\/+$/, "");
  const apiKey = settings.env?.ANTHROPIC_AUTH_TOKEN;
  if (!baseUrl || !apiKey) return null;

  let modelsData = null;
  // Tracks whether the on-disk cache is known to be at least as fresh as
  // modelsData for this run: either we just wrote it (network path,
  // persisted below), or modelsData itself came from reading that same
  // file (offline fallback path further down). If the network returned
  // fresh data but persisting it to disk failed, this stays false and the
  // drift detector -- which only ever reads the on-disk cache, never
  // modelsData directly -- is skipped this cycle rather than silently
  // comparing against a stale or absent file.
  let cacheFresh = false;
  try {
    const controller = new AbortController();
    // Bound the *entire* fetch, including response-body read: the timer is
    // only cleared once json() resolves (success or throw), inside the
    // finally below. Clearing it right after headers arrive (the previous
    // behavior) left body parsing unbounded -- a slow/stalled body stream
    // could hang past the intended 2500ms budget.
    const timeout = setTimeout(() => controller.abort(), 2500);
    try {
      const res = await fetch(baseUrl + "/v1/models?limit=1000", {
        headers: { "x-api-key": apiKey, "authorization": "Bearer " + apiKey, "anthropic-version": "2023-06-01" },
        signal: controller.signal,
      });
      if (res.ok) {
        const json = await res.json();
        if (Array.isArray(json.data) && json.data.length > 0) {
          modelsData = json.data;
          try {
            fs.mkdirSync(path.dirname(CACHE_PATH), { recursive: true, mode: 0o700 });
            const cacheTemp = CACHE_PATH + ".tmp." + process.pid;
            const cacheFd = fs.openSync(cacheTemp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC, 0o600);
            try {
              const cacheData = Buffer.from(JSON.stringify(modelsData, null, 2) + "\n", "utf8");
              let offset = 0;
              while (offset < cacheData.length) offset += fs.writeSync(cacheFd, cacheData, offset, cacheData.length - offset);
              fs.fsyncSync(cacheFd);
            } finally { fs.closeSync(cacheFd); }
            fs.chmodSync(cacheTemp, 0o600);
            fs.renameSync(cacheTemp, CACHE_PATH);
            cacheFresh = true;
          } catch {}
        }
      } else log("Gateway returned HTTP " + res.status + "; using offline cache if available.");
    } finally {
      clearTimeout(timeout);
    }
  } catch (err) { log("Network unavailable; using offline cache if available."); }
  if (!modelsData) {
    modelsData = readJson(CACHE_PATH);
    if (Array.isArray(modelsData)) cacheFresh = true;
  }
  if (!Array.isArray(modelsData)) return null;

  // Only run drift detection when the on-disk cache is known to be at least
  // as fresh as this run (freshly written above, or the most recent offline
  // cache read back and reused as modelsData). If the network returned data
  // but the write to CACHE_PATH failed, cacheFresh is false: the detector
  // would otherwise read a stale or missing file while modelsData (used for
  // the picker below) reflects the live gateway response, so skip this
  // cycle and let the next run retry rather than reporting off a mismatch.
  const driftNotice = cacheFresh ? runDriftDetector() : null;

  const newOptions = [];
  for (const model of modelsData) {
    const rawId = model?.id || "";
    if (!rawId || rawId.startsWith("speech-") || rawId.startsWith("tts-")) continue;
    const cleanId = effectiveModelBase(rawId);
    if (!cleanId) continue;
    const ctx = model.context_length || model.max_input_tokens || 200000;
    const modelId = ctx >= 872000 ? cleanId + "[1m]" : cleanId;
    const label = model.display_name || cleanId.replace(/^(claude|omniroute)-/, "").split("-").map(s => s.charAt(0).toUpperCase() + s.slice(1)).join(" ");
    const description = model.description || ((ctx >= 1000000 ? "1M" : Math.round(ctx / 1000) + "K") + " gateway context");
    newOptions.push({ model: modelId, label, description });
  }
  if (!newOptions.length) return finish(driftNotice, null);

  // Do not let discovery silently invalidate a fleet lane. Preserve its existing picker row,
  // or add a clearly labelled fallback row when a gateway does not advertise it.
  const fleet = readJson(FLEET_PATH);
  const required = fleet?.lanes && typeof fleet.lanes === "object"
    ? [...new Set(Object.values(fleet.lanes).flatMap(row => [row?.model, ...(Array.isArray(row?.fallbacks) ? row.fallbacks : [])]).filter(Boolean))]
    : [];
  const lanesByModel = {};
  if (fleet?.lanes && typeof fleet.lanes === "object") {
    for (const [lane, row] of Object.entries(fleet.lanes)) {
      if (row?.model) (lanesByModel[row.model] = lanesByModel[row.model] || []).push(lane);
    }
  }
  let newFallbackModels = [];
  function optionsFor(baseOptions) {
    const discoveredByModel = new Map(newOptions.map(row => [modelKey(row), row]));
    const baseByModel = new Map(baseOptions.map(row => [modelKey(row), row]));
    const requiredByModel = new Map();
    const fallbackModels = [];
    for (const requiredModel of required) {
      if (!discoveredByModel.has(requiredModel)) {
        requiredByModel.set(requiredModel, baseByModel.get(requiredModel) || {
          model: requiredModel,
          label: requiredModel,
          description: REQUIRED_FLEET_DESCRIPTION,
        });
        // Report only a NEW loss: a row that is absent or was previously a
        // real gateway entry. Steady-state fallback rows stay silent.
        const existingRow = baseByModel.get(requiredModel);
        if (!existingRow || existingRow.description !== "Required by delegate fleet") {
          fallbackModels.push(requiredModel);
        }
      }
    }
    newFallbackModels = fallbackModels;
    const desiredByModel = new Map([...discoveredByModel, ...requiredByModel]);
    const options = [];
    const seen = new Set();
    // Reapply only rows affected by discovery. claude-* rows resolve inside
    // Claude Code itself and always survive. A gateway-spelling row the
    // gateway no longer advertises is a dead reference and is pruned; keeping
    // it made the picker accumulate stale IDs across gateway renames.
    for (const row of baseOptions) {
      const id = modelKey(row);
      const replacement = desiredByModel.get(id);
      if (replacement) {
        options.push({ ...replacement });
        seen.add(id);
      } else if (id.startsWith("claude-")) {
        options.push(row);
        seen.add(id);
      }
    }
    for (const [id, row] of desiredByModel) {
      if (!seen.has(id)) options.push({ ...row });
    }
    return options;
  }

  const oldOptions = Array.isArray(settings.modelPicker?.options) ? settings.modelPicker.options : [];
  const initialOptions = optionsFor(oldOptions);
  if (!initialOptions.length) return finish(driftNotice, null);
  const initialActiveModel = settings.model;
  const initialModel = initialActiveModel && initialOptions.some(row => row.model === initialActiveModel)
    ? initialActiveModel
    : (initialOptions.find(row => row.model.includes("sonnet") || row.model.includes("sol")) || initialOptions[0])?.model;
  const initiallyChanged = JSON.stringify(oldOptions) !== JSON.stringify(initialOptions)
    || settings.modelPicker?.replaceBuiltInOptions !== true
    || initialModel !== initialActiveModel;
  if (!initiallyChanged && !isForce) return finish(driftNotice, null);

  const lockPath = SETTINGS_PATH + ".lock";
  const temp = SETTINGS_PATH + ".tmp." + process.pid;
  let lockFd;
  let lockCreated = false;
  let wrote = false;
  try {
    lockFd = acquireSettingsLock(lockPath);
    if (lockFd === undefined) return finish(driftNotice, null);
    lockCreated = true;
    // Network discovery happens before the lock. Re-read after locking and
    // mutate only the discovered picker/active-model fields on the fresh
    // object, preserving unrelated concurrent settings edits.
    const latest = readJson(SETTINGS_PATH);
    if (!latest) return finish(driftNotice, null);
    const latestOptions = Array.isArray(latest.modelPicker?.options) ? latest.modelPicker.options : [];
    const options = optionsFor(latestOptions);
    if (!options.length) return finish(driftNotice, null);
    const latestModel = latest.model;
    const model = latestModel && options.some(row => row.model === latestModel)
      ? latestModel
      : (options.find(row => row.model.includes("sonnet") || row.model.includes("sol")) || options[0])?.model;
    const latestPicker = latest.modelPicker && typeof latest.modelPicker === "object" ? latest.modelPicker : {};
    const pickerChanged = JSON.stringify(latestOptions) !== JSON.stringify(options) || latestPicker.replaceBuiltInOptions !== true;
    const activeChanged = model !== latestModel;
    if (!pickerChanged && !activeChanged && !isForce) return finish(driftNotice, null);
    latest.modelPicker = { ...latestPicker, replaceBuiltInOptions: true, options };
    if (activeChanged && model) latest.model = model;
    const fd = fs.openSync(temp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC, 0o600);
    try {
      const data = Buffer.from(JSON.stringify(latest, null, 2) + "\n", "utf8");
      let offset = 0;
      while (offset < data.length) offset += fs.writeSync(fd, data, offset, data.length - offset);
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    fs.chmodSync(temp, 0o600);
    fs.renameSync(temp, SETTINGS_PATH);
    wrote = true;
  } catch (err) { try { if (fs.existsSync(temp)) fs.unlinkSync(temp); } catch {} }
  finally {
    try { if (lockFd !== undefined) fs.closeSync(lockFd); } catch {}
    if (lockCreated) { try { fs.unlinkSync(lockPath); } catch {} }
  }
  let removalMessage = null;
  if (wrote && newFallbackModels.length) {
    // Claude Code shows a SessionStart hook's systemMessage to the user, so a
    // newly lost fleet model is announced instead of silently falling back.
    const parts = newFallbackModels.map(id => {
      const lanes = lanesByModel[id] || [];
      return lanes.length ? `${id} (lane${lanes.length > 1 ? "s" : ""}: ${lanes.join(", ")})` : id;
    });
    removalMessage = "Gateway no longer offers fleet model(s): " + parts.join("; ")
      + ". Fallback picker rows were kept; remap the affected lanes and run claude-fleet-sync, then restart Claude Code.";
  }
  return finish(driftNotice, removalMessage);
}

async function main() {
  const output = await run().catch(() => null);
  if (output) process.stdout.write(JSON.stringify(output) + "\n");
}
main().catch(() => {});
