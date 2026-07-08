// Backend access: the one fetch wrapper every module calls, and the admin
// password cookie it authenticates with. app.js registers what happens on a
// 401 (show the login screen) so this module stays dependency-free.

let unauthorizedHandler = () => {};

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler;
}

export function getPassword() {
  const match = document.cookie.match(/(?:^|; )trustyclaw_admin=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : null;
}

export async function api(method, path, body) {
  const headers = { "Authorization": "Bearer " + getPassword() };
  if (method !== "GET") headers["Idempotency-Key"] = crypto.randomUUID();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (response.status === 401) { unauthorizedHandler(); throw new Error("unauthorized"); }
  if (!response.ok) throw new Error(data.error ? data.error.message : response.statusText);
  return data;
}
