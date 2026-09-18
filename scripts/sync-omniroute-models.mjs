#!/usr/bin/env node
// claude-agents-config:managed

import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const homeArgIndex = process.argv.indexOf("--home");
const home = homeArgIndex >= 0 && process.argv[homeArgIndex + 1]
  ? path.resolve(process.argv[homeArgIndex + 1])
  : process.env.CLAUDE_FLEET_HOME
    ? path.resolve(process.env.CLAUDE_FLEET_HOME)
    : path.dirname(path.dirname(path.resolve(process.argv[1]))) || os.homedir();
const SETTINGS_PATH = path.join(home, ".claude", "settings.json");
const FLEET_PATH = path.join(home, ".config", "delegate-skills", "config.json");
const CACHE_PATH = path.join(home, ".claude", "cache", "omniroute-models-cache.json");
const isQuiet = process.argv.includes("--quiet") || process.argv.includes("-q");
const isForce = process.argv.includes("--force") || process.argv.includes("-f");

function log(msg) { if (!isQuiet) process.stdout.write("[omniroute-sync] " + msg + "\n"); }
function readJson(file) { try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return null; } }
function modelKey(row) { return row?.model || ""; }

async function main() {
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
          fs.mkdirSync(path.dirname(CACHE_PATH), { recursive: true });
          fs.writeFileSync(CACHE_PATH, JSON.stringify(modelsData, null, 2));
          fs.chmodSync(CACHE_PATH, 0o600);
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

  // Do not let discovery silently invalidate a fleet lane. Preserve its existing picker row,
  // or add a clearly labelled fallback row when a gateway does not advertise it.
  const oldOptions = Array.isArray(settings.modelPicker?.options) ? settings.modelPicker.options : [];
  const oldByModel = new Map(oldOptions.map(row => [modelKey(row), row]));
  const fleet = readJson(FLEET_PATH);
  const required = fleet?.lanes && typeof fleet.lanes === "object"
    ? [...new Set(Object.values(fleet.lanes).map(row => row?.model).filter(Boolean))]
    : [];
  const discovered = new Set(newOptions.map(modelKey));
  for (const requiredModel of required) {
    if (!discovered.has(requiredModel)) {
      newOptions.push(oldByModel.get(requiredModel) || { model: requiredModel, label: requiredModel, description: "Required by delegate fleet" });
    }
  }
  if (!newOptions.length) return;
  const changed = JSON.stringify(settings.modelPicker?.options || []) !== JSON.stringify(newOptions) || settings.modelPicker?.replaceBuiltInOptions !== true;
  if (!changed && !isForce) return;
  settings.modelPicker = { replaceBuiltInOptions: true, options: newOptions };
  if (!settings.model || !newOptions.some(row => row.model === settings.model)) {
    const preferred = newOptions.find(row => row.model.includes("sonnet") || row.model.includes("sol")) || newOptions[0];
    if (preferred) settings.model = preferred.model;
  }
  const temp = SETTINGS_PATH + ".tmp." + process.pid;
  try {
    fs.writeFileSync(temp, JSON.stringify(settings, null, 2) + "\n", "utf8");
    fs.chmodSync(temp, 0o600);
    fs.renameSync(temp, SETTINGS_PATH);
  } catch (err) { try { if (fs.existsSync(temp)) fs.unlinkSync(temp); } catch {} }
}
main().catch(() => {});
