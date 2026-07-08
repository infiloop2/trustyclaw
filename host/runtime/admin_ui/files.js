// Agent workspace tab: read-only file explorer over the agent home.

import { api } from "./api.js";
import { $ } from "./helpers.js";

const FILE_LIST_ENTRY_LIMIT = 1000;

let currentFilePath = "/";
let fileEntries = [];

function fileMessage(message) {
  $("file-message").textContent = message || "";
}

function parentPath(path) {
  const normalized = path && path !== "/" ? path.replace(/\/+$/, "") : "/";
  if (normalized === "/") return "/";
  const index = normalized.lastIndexOf("/");
  return index <= 0 ? "/" : normalized.slice(0, index);
}

export async function loadAgentFiles(path = currentFilePath) {
  try {
    fileMessage("");
    const response = await api("GET", `/v1/agent-files?path=${encodeURIComponent(path || "/")}`);
    currentFilePath = response.path || "/";
    fileEntries = Array.isArray(response.entries) ? response.entries : [];
    $("file-path").value = currentFilePath;
    renderFileList(response);
  } catch (error) {
    fileMessage(error.message);
  }
}

export function refreshFiles() {
  return loadAgentFiles(currentFilePath);
}

export async function ensureFilesLoaded() {
  if (!fileEntries.length) await loadAgentFiles(currentFilePath);
}

export function loadParentDirectory() {
  return loadAgentFiles(parentPath(currentFilePath));
}

async function readAgentFile(path) {
  try {
    fileMessage("");
    const response = await api("GET", `/v1/agent-files/read?path=${encodeURIComponent(path)}`);
    renderFileContent(response);
  } catch (error) {
    fileMessage(error.message);
  }
}

export async function openAgentPath(path, type) {
  if (type === "directory") {
    await loadAgentFiles(path);
    return;
  }
  await readAgentFile(path);
}

function renderFileList(listing = {}) {
  if (listing.truncated) {
    fileMessage(`Showing first ${FILE_LIST_ENTRY_LIMIT} entries.`);
  }
  const table = $("file-list");
  table.textContent = "";
  const header = document.createElement("tr");
  for (const label of ["name", "type", "size"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    header.appendChild(cell);
  }
  table.appendChild(header);
  if (currentFilePath !== "/") {
    table.appendChild(fileRow("..", parentPath(currentFilePath), "directory", null));
  }
  if (!fileEntries.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "empty-state";
    cell.textContent = "Empty directory.";
    row.appendChild(cell);
    table.appendChild(row);
    return;
  }
  for (const entry of fileEntries) {
    table.appendChild(fileRow(entry.name, entry.path, entry.type, entry.size_bytes));
  }
}

function fileRow(name, path, type, sizeBytes) {
  const row = document.createElement("tr");
  const nameCell = document.createElement("td");
  const button = document.createElement("button");
  button.className = "file-entry";
  button.dataset.action = "open-file-path";
  button.dataset.path = path == null ? "" : String(path);
  button.dataset.fileType = type == null ? "" : String(type);
  button.textContent = name == null ? "" : String(name);
  nameCell.appendChild(button);
  row.appendChild(nameCell);

  const typeCell = document.createElement("td");
  typeCell.textContent = type == null ? "" : String(type);
  row.appendChild(typeCell);

  const sizeCell = document.createElement("td");
  sizeCell.className = "muted";
  sizeCell.textContent = sizeBytes == null ? "" : String(sizeBytes);
  row.appendChild(sizeCell);
  return row;
}

function renderFileContent(file) {
  const truncated = file.truncated ? " (truncated)" : "";
  $("file-viewer-title").textContent = `${file.path || ""}${truncated}`;
  $("file-content").textContent = file.content || "";
}

export function goToFilePath() {
  loadAgentFiles($("file-path").value.trim() || "/");
}
