const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const age = ts => {
  if (!ts) return "unknown";
  const h = (Date.now() - new Date(String(ts).replace(" ", "T")).getTime()) / 36e5;
  if (!isFinite(h) || h < 0) return "unknown";
  if (h < 24) return `${Math.max(1, Math.round(h))}h`;
  const d = Math.round(h / 24);
  return d < 30 ? `${d}d` : `${Math.round(d / 30)}mo`;
};
const ageCls = ts => {
  if (!ts) return "";
  const h = (Date.now() - new Date(String(ts).replace(" ", "T")).getTime()) / 36e5;
  if (!isFinite(h) || h < 0) return "";
  return h < 48 ? "fresh" : h > 24 * 21 ? "stale" : "";
};
const safeUrl = u => /^https?:\/\//i.test(String(u ?? "")) ? String(u) : "#";

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error((await res.json()).error || res.statusText);
  return res.json();
}

async function postJSON(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
