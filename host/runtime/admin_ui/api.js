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

export async function api(method, path, body, extraHeaders) {
  const headers = { "Authorization": "Bearer " + getPassword() };
  for (const [name, value] of Object.entries(extraHeaders || {})) headers[name] = value;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (response.status === 401) { unauthorizedHandler(); throw new Error("unauthorized"); }
  if (!response.ok) throw new Error(data.error ? data.error.message : response.statusText);
  return data;
}

export async function apiBlob(path) {
  const response = await fetch(path, {
    method: "GET",
    headers: { "Authorization": "Bearer " + getPassword() },
  });
  if (response.status === 401) { unauthorizedHandler(); throw new Error("unauthorized"); }
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = data.error ? data.error.message : message;
    } catch (_) {}
    throw new Error(message);
  }
  return response.blob();
}
