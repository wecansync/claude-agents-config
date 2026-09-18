#!/usr/bin/env node
// claude-agents-config:managed

import fs from "node:fs";
import path from "node:path";
import os from "node:os";

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
const isQuiet = process.argv.includes("--quiet") || process.argv.includes("-q");
const isForce = process.argv.includes("--force") || process.argv.includes("-f");

function log(msg) { if (!isQuiet) process.stdout.write("[omniroute-sync] " + msg + "\n"); }
function readJson(file) { try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return null; } }
function modelKey(row) { return row?.model || ""; }

async function main() {
  if (process.env.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY !== "1") return;
  const settings = readJson(SETTINGS_PATH);
  if (!settings) return;
  const baseUrl = settings.env?.ANTHROPIC_BASE_URL?.replace(/\/+$/, "");
  const apiKey = settings.env?.ANTHROPIC_AUTH_TOKEN;
  if (!baseUrl || !apiKey) return;

  let modelsData = null;
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2500);
    const res = await fetch(baseUrl + "/v1/models?limit=1000", {
      headers: { "x-api-key": apiKey, "authorization": "Bearer " + apiKey, "anthropic-version": "2023-06-01" },
      signal: controller.signal,
    });
    clearTimeout(timeout);
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
        } catch {}
      }
    } else log("Gateway returned HTTP " + res.status + "; using offline cache if available.");
  } catch (err) { log("Network unavailable; using offline cache if available."); }
  if (!modelsData) modelsData = readJson(CACHE_PATH);
  if (!Array.isArray(modelsData)) return;

  const newOptions = [];
  for (const model of modelsData) {
    const rawId = model?.id || "";
    if (!rawId || rawId.startsWith("speech-") || rawId.startsWith("tts-")) continue;
    const cleanId = rawId.replace(/\[\d+[kmKM]?\]\s*$/, "");
    if (!cleanId) continue;
    const ctx = model.context_length || model.max_input_tokens || 200000;
    const modelId = ctx >= 872000 ? cleanId + "[1m]" : cleanId;
    const label = model.display_name || cleanId.replace(/^(claude|omniroute)-/, "").split("-").map(s => s.charAt(0).toUpperCase() + s.slice(1)).join(" ");
    const description = model.description || ((ctx >= 1000000 ? "1M" : Math.round(ctx / 1000) + "K") + " gateway context");
    newOptions.push({ model: modelId, label, description });
  }
  if (!newOptions.length) return;

  // Do not let discovery silently invalidate a fleet lane. Preserve its existing picker row,
  // or add a clearly labelled fallback row when a gateway does not advertise it.
  const fleet = readJson(FLEET_PATH);
  const required = fleet?.lanes && typeof fleet.lanes === "object"
    ? [...new Set(Object.values(fleet.lanes).map(row => row?.model).filter(Boolean))]
    : [];
  function optionsFor(baseOptions) {
    const discoveredByModel = new Map(newOptions.map(row => [modelKey(row), row]));
    const baseByModel = new Map(baseOptions.map(row => [modelKey(row), row]));
    const requiredByModel = new Map();
    for (const requiredModel of required) {
      if (!discoveredByModel.has(requiredModel)) {
        requiredByModel.set(requiredModel, baseByModel.get(requiredModel) || {
          model: requiredModel,
          label: requiredModel,
          description: "Required by delegate fleet",
        });
      }
    }
    const desiredByModel = new Map([...discoveredByModel, ...requiredByModel]);
    const options = [];
    const seen = new Set();
    // Reapply only rows affected by discovery. Any unrelated picker row,
    // including one added while the network request was in flight, survives.
    for (const row of baseOptions) {
      const id = modelKey(row);
      const replacement = desiredByModel.get(id);
      if (replacement) {
        options.push({ ...replacement });
        seen.add(id);
      } else {
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
  if (!initialOptions.length) return;
  const initialActiveModel = settings.model;
  const initialModel = initialActiveModel && initialOptions.some(row => row.model === initialActiveModel)
    ? initialActiveModel
    : (initialOptions.find(row => row.model.includes("sonnet") || row.model.includes("sol")) || initialOptions[0])?.model;
  const initiallyChanged = JSON.stringify(oldOptions) !== JSON.stringify(initialOptions)
    || settings.modelPicker?.replaceBuiltInOptions !== true
    || initialModel !== initialActiveModel;
  if (!initiallyChanged && !isForce) return;

  const lockPath = SETTINGS_PATH + ".lock";
  const temp = SETTINGS_PATH + ".tmp." + process.pid;
  let lockFd;
  let lockCreated = false;
  try {
    lockFd = fs.openSync(lockPath, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
    lockCreated = true;
    // Network discovery happens before the lock. Re-read after locking and
    // mutate only the discovered picker/active-model fields on the fresh
    // object, preserving unrelated concurrent settings edits.
    const latest = readJson(SETTINGS_PATH);
    if (!latest) return;
    const latestOptions = Array.isArray(latest.modelPicker?.options) ? latest.modelPicker.options : [];
    const options = optionsFor(latestOptions);
    if (!options.length) return;
    const latestModel = latest.model;
    const model = latestModel && options.some(row => row.model === latestModel)
      ? latestModel
      : (options.find(row => row.model.includes("sonnet") || row.model.includes("sol")) || options[0])?.model;
    const latestPicker = latest.modelPicker && typeof latest.modelPicker === "object" ? latest.modelPicker : {};
    const pickerChanged = JSON.stringify(latestOptions) !== JSON.stringify(options) || latestPicker.replaceBuiltInOptions !== true;
    const activeChanged = model !== latestModel;
    if (!pickerChanged && !activeChanged && !isForce) return;
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
  } catch (err) { try { if (fs.existsSync(temp)) fs.unlinkSync(temp); } catch {} }
  finally {
    try { if (lockFd !== undefined) fs.closeSync(lockFd); } catch {}
    if (lockCreated) { try { fs.unlinkSync(lockPath); } catch {} }
  }
}
main().catch(() => {});
