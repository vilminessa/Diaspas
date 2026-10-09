/* Diaspas — фронтенд.
 *
 * Мост с Python (ui/webapp.py):
 *   JS  → Python : window.pywebview.api.<method>()  (promise)
 *   Python → JS  : window.diaspas.emit(kind, payload) — вызывается из
 *                  evaluate_js; подписки через window.diaspas.on(kind, fn)
 *
 * Правило: любой долгий вызов (пробы, A/B, перебор) — в api-потоке
 * pywebview, UI остаётся отзывчивым; прогресс приходит emit'ом.
 */
"use strict";

window.diaspas = (() => {
  const handlers = {};
  return {
    on(kind, fn) { (handlers[kind] ||= []).push(fn); },
    emit(kind, payload) {
      (handlers[kind] || []).forEach(fn => {
        try { fn(payload); } catch (e) { console.error(e); }
      });
    },
  };
})();

const $ = (sel) => document.querySelector(sel);
const api = () => window.pywebview.api;

/* ── состояние фронтенда ─────────────────────────────────────── */
const S = {
  tab: "overview",
  state: null,
  presets: [],
  probes: null,      // последний отчёт пробы
  busy: false,
  logs: [],          // строки журнала (объекты {ts, level, text})
  logFilter: "all",
};

/* ── мелкие утилиты ─────────────────────────────────────────── */
function toast(text, kind = "") {
  const el = $("#toast");
  el.textContent = text;
  el.className = `toast ${kind}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 4200);
}

function confirmBox(title, body) {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    $("#modal-body").textContent = body;
    $("#modal").classList.remove("hidden");
    const ok = $("#modal-ok"), cancel = $("#modal-cancel");
    const done = (val) => {
      $("#modal").classList.add("hidden");
      ok.onclick = cancel.onclick = null;
      resolve(val);
    };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
  });
}

function setBusy(busy, label) {
  S.busy = busy;
  $("#busy-ind").classList.toggle("hidden", !busy);
  if (label !== undefined) $("#statusbar-text").textContent = label;
  // блокировка действий: пока занято, кнопки не дёргают
  document.querySelectorAll(".tab, #preset-grid button, .card .btn, .card select")
    .forEach((b) => { b.disabled = busy; });
}

/* ── вкладки ─────────────────────────────────────────────────── */
function switchTab(name) {
  S.tab = name;
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === `tab-${name}`));
}

/* ── состояние ───────────────────────────────────────────────── */
function renderState(st) {
  S.state = st;
  const run = st.running;
  // Четыре разных состояния вместо двух: «скачал релиз» ≠ «служба стоит».
  // Раньше отсутствие службы показывалось как «Установка не найдена»,
  // и после успешного скачивания экран выглядел сломанным.
  let cls, headline, sub;
  if (run) {
    cls = "ok"; headline = "Обход работает";
    sub = st.zapret_dir || "";
  } else if (st.installed) {
    cls = "warn"; headline = "Служба остановлена";
    sub = st.zapret_dir || "";
  } else if (st.dir_ok) {
    cls = "warn"; headline = "Служба не установлена";
    sub = `Папка релиза на месте: ${st.zapret_dir} — пресет ещё не применён`;
  } else {
    cls = "unknown"; headline = "Папка zapret не найдена";
    sub = "Скачай последний релиз или укажи папку вручную";
  }

  const pill = $("#top-status");
  pill.className = `status-pill status-${cls}`;
  pill.querySelector(".status-text").textContent =
    run ? "работает" : (st.installed ? "остановлена" : "нет службы");

  $("#state-dot").className = `big-dot ${cls}`;
  $("#state-headline").textContent = headline;
  $("#state-sub").textContent = sub;

  $("#v-service").textContent = st.installed
    ? (run ? "работает" : "остановлена") : "не установлена";
  $("#v-strategy").textContent = st.strategy || "—";
  $("#v-preset").textContent = st.preset_title || "не распознан";
  $("#v-pid").textContent = st.pid ? String(st.pid) : "—";
  $("#zapret-path").textContent = st.zapret_dir || "папка не найдена";
  $("#zapret-path").title = st.zapret_dir || "";

  // кнопка «Создать службу»: папка есть, а службы нет
  const need = st.dir_ok && !st.installed;
  $("#btn-create-service").classList.toggle("hidden", !need);
  if (need) {
    const key = st.suggested_preset;
    const name = (S.presets.find((p) => p.key === key) || {}).title;
    $("#btn-create-service").textContent = name
      ? `Создать службу — пресет «${name}»` : "Создать службу (выберите пресет)";
    $("#btn-create-service").onclick = () => createService(key);
  }

  // подсветка активного пресета
  document.querySelectorAll(".preset-card").forEach((c) => {
    c.classList.toggle("active", !!st.preset_key && c.dataset.key === st.preset_key);
    const mark = c.querySelector(".mark");
    if (mark) mark.textContent = (st.preset_key && c.dataset.key === st.preset_key) ? "▸" : "";
  });

  if (st.release_note) $("#release-note").textContent = st.release_note;
}

async function createService(key) {
  const p = S.presets.find((x) => x.key === key);
  if (!p || p.off || !key) {
    toast("Выберите пресет в разделе «Пресеты»", "");
    return;
  }
  const okBox = await confirmBox("Создать службу",
    `Папка релиза на месте, но служба zapret не установлена.\n\n` +
    `Применить пресет «${p.title}»? Он создаст службу и включит обход.`);
  if (!okBox) return;
  await applyPreset(p);
}

async function refreshState() {
  try {
    renderState(await api().state());
  } catch (e) {
    toast("не удалось прочитать состояние: " + e, "err");
  }
}

/* ── пресеты ─────────────────────────────────────────────────── */
function renderPresets(presets) {
  S.presets = presets;
  const grid = $("#preset-grid");
  grid.innerHTML = "";
  presets.forEach((p) => {
    const b = document.createElement("button");
    b.className = "preset-card";
    b.dataset.key = p.key;
    b.innerHTML = `<span class="name">${escapeHtml(p.title)}<span class="mark"></span></span>
                   <span class="desc">${escapeHtml(p.description || "")}</span>`;
    b.onmouseenter = () => { $("#preset-hint").textContent = p.description || ""; };
    b.onclick = () => applyPreset(p);
    grid.appendChild(b);
  });
}

async function applyPreset(p) {
  if (S.busy) return;
  const what = p.off
    ? "Служба zapret будет удалена, winws остановлен."
    : `Стратегия: ${p.strategy}${p.game_filter ? `, Game Filter: ${p.game_filter}` : ""}` +
      `${p.ipset ? `, IPSet: ${p.ipset}` : ""}` +
      `${p.exclude_domains && p.exclude_domains.length ? ", исключения: " + p.exclude_domains.join(", ") : ""}`;
  const ok = await confirmBox(`Пресет «${p.title}»`, what + "\n\nПродолжить?");
  if (!ok) return;

  setBusy(true, `применяю пресет «${p.title}»…`);
  try {
    const res = await api().apply_preset(p.key);
    if (res && res.ok) {
      toast(`Пресет «${p.title}» применён`, "ok");
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
  }
}

/* ── пробы ───────────────────────────────────────────────────── */
function chipHtml(state, label, count, total) {
  return `<span class="chip ${state}"><span class="dot"></span>${escapeHtml(label)}` +
         `<span class="count">${count}/${total}</span></span>`;
}

function renderProbes(report) {
  S.probes = report;
  const groups = report.groups || {};
  const names = Object.keys(groups);
  const html = names.map((g) => {
    const r = groups[g];
    return chipHtml(r.state === "full" ? "ok" : (r.state === "partial" ? "warn" : "err"),
      g, r.count, r.total);
  }).join("");
  const summary = `<span class="chip ${report.state === "full" ? "ok" : (report.state === "none" ? "err" : "warn")}">
      <span class="dot"></span>итого<span class="count">${report.count}/${report.total}</span></span>`;
  $("#probe-chips").innerHTML = html || "<span class='muted'>нет данных</span>";
  $("#overview-probes").innerHTML = summary + " " + html;

  // таблица целей
  const rows = [];
  names.forEach((g) => {
    const t = groups[g].targets || {};
    Object.keys(t).forEach((name) => {
      const x = t[name];
      rows.push(`<tr>
        <td>${escapeHtml(g)}</td><td class="mono">${escapeHtml(name)}</td>
        <td class="${x.ok ? "ok" : "err"}">${x.ok ? "✓" : "✗"}</td>
        <td class="mono">${x.ms ?? ""}</td>
        <td class="muted">${escapeHtml(x.why || "")}</td></tr>`);
    });
  });
  $("#probe-table tbody").innerHTML = rows.length
    ? rows.join("") : "<tr><td colspan='5' class='muted'>нет данных</td></tr>";
}

async function runProbes() {
  if (S.busy) return;
  setBusy(true, "проверяю целевые сервисы…");
  try {
    const report = await api().probes_run();
    renderProbes(report);
    $("#statusbar-text").textContent =
      `пробы: ${report.count}/${report.total}`;
  } catch (e) {
    toast("Ошибка проб: " + e, "err");
  } finally {
    setBusy(false, "готов");
  }
}

/* ── A/B ─────────────────────────────────────────────────────── */
const VERDICT_MARK = {
  zapret_needed: ["good", "✓ работает только с обходом — DPI блокирует"],
  zapret_breaks: ["bad", "✗ ломает сам обход — домен в исключения"],
  works_always: ["mixed", "~ отвечает в обоих случаях"],
  works_without: ["mixed", "~ обход не требуется"],
  blocked_anyway: ["bad", "✗ не отвечает и без обхода — нужна другая стратегия"],
  blocked_always: ["bad", "✗ не отвечает в обоих случаях — блокировка выше обхода"],
};

async function runAb() {
  if (S.busy) return;
  const group = $("#ab-group").value;
  const ok = await confirmBox("A/B-тест",
    `Группа «${group}»: служба zapret будет остановлена и запущена заново.\nПродолжить?`);
  if (!ok) return;

  setBusy(true, `A/B-тест группы ${group}…`);
  const out = $("#ab-result");
  out.classList.remove("hidden");
  out.className = "verdict";
  out.textContent = "замер с обходом → остановка → замер без обхода…";
  try {
    const res = await api().ab_run(group);
    if (!res.ok) {
      out.classList.add("bad");
      out.textContent = "Ошибка: " + (res.error || "неизвестно");
    } else {
      const withOk = res.with && res.with.ok;
      const withoutOk = res.without && res.without.ok;
      const [kind, mark] = VERDICT_MARK[res.verdict] || ["", res.verdict || ""];
      out.classList.add(kind);
      out.textContent =
        `${mark}\n` +
        `с обходом: ${res.with ? res.with.count + "/" + res.with.total : "—"} · ` +
        `без обхода: ${res.without ? res.without.count + "/" + res.without.total : "—"}\n` +
        `${res.hint || ""}`;
    }
  } catch (e) {
    out.classList.add("bad");
    out.textContent = "Ошибка: " + e;
  } finally {
    setBusy(false, "готов");
    await refreshState();
  }
}

/* ── автоподбор ──────────────────────────────────────────────── */
function renderSelectProgress(p) {
  const total = p.total || 0, done = p.done || 0;
  $("#select-progress").textContent = `${done}/${total}` +
    (p.best ? ` · лучшая ${p.best}` : "");
  $("#select-bar").style.width = total ? `${done * 100 / total}%` : "0%";
  const rows = (p.results || []).map(rowHtml).join("");
  if (rows) $("#select-table tbody").innerHTML = rows;
}

function rowHtml(r) {
  const cell = (v) => v ? `<span class="ok">+</span>` : `<span class="err">−</span>`;
  const targets = {};
  (r.targets || []).forEach((t) => {
    if (!t.ok) targets[t.group] = false;
    else if (targets[t.group] === undefined) targets[t.group] = true;
  });
  const stateCls = r.state === "full" ? "ok" : (r.state === "partial" ? "warn" : "err");
  return `<tr>
    <td class="mono">${escapeHtml((r.name || "").replace(/^general ?\(?|\)?\.bat$/g, ""))}</td>
    <td class="${stateCls}">${escapeHtml(r.state || "")}</td>
    <td class="mono">${r.count}/${r.total}</td>
    <td>${cell(targets.discord)}</td><td>${cell(targets.youtube)}</td>
    <td>${cell(targets.ea)}</td><td>${cell(targets.ubisoft)}</td></tr>`;
}

async function runSelect() {
  if (S.busy) return;
  const ok = await confirmBox("Автоподбор стратегий",
    "Служба zapret будет пересоздаваться для каждой стратегии; Discord и YouTube " +
    "могут кратковременно прерываться. Продолжить?");
  if (!ok) return;

  setBusy(true, "перебор стратегий…");
  $("#btn-select-cancel").disabled = false;
  $("#select-best").classList.add("hidden");
  try {
    const res = await api().select_run();
    if (!res || !res.ok) {
      toast("Перебор: " + ((res && res.error) || "не удалось запустить"), "err");
    } else {
      $("#select-table tbody").innerHTML =
        (res.results || []).map(rowHtml).join("");
      $("#select-bar").style.width = "100%";
      const best = res.best ? labelOf(res.best) : "не подобрана";
      $("#select-best").classList.remove("hidden");
      $("#select-best").textContent = `★ Рекомендуемая стратегия: ${best}`;
      toast(`Лучшая стратегия: ${best}`, "ok");
    }
  } catch (e) {
    toast("Ошибка перебора: " + e, "err");
  } finally {
    setBusy(false, "готов");
    $("#btn-select-cancel").disabled = true;
    await refreshState();
  }
}

function labelOf(file) {
  return (file || "").replace(/^general ?\(?|\)?\.bat$/g, "") || "general";
}

/* ── журнал ──────────────────────────────────────────────────── */
function appendLog(entry) {
  // entry: строка формата "YYYY-MM-DD HH:MM:SS [level] text"
  const m = entry.match(/^\S+ \S+ \[(\w+)\] ([\s\S]*)$/);
  const level = m ? m[1] : "info";
  const text = m ? m[2] : entry;
  S.logs.push({ level, text, raw: entry });
  if (S.logs.length > 3000) S.logs.shift();
  renderLog();
}

function renderLog() {
  const view = $("#log-view");
  const f = S.logFilter;
  const lines = S.logs.filter((l) => f === "all" || l.level === f);
  view.innerHTML = lines.map((l) => {
    const raw = l.raw || l.text;
    const m = raw.match(/^(\S+ \S+) \[(\w+)\] ([\s\S]*)$/);
    if (!m) return `<span class="log-line lvl-old">${escapeHtml(raw)}</span>`;
    return `<span class="log-line"><span class="ts">${escapeHtml(m[1])}</span> ` +
      `<span class="lvl-${m[2]}">[${m[2]}]</span> ${escapeHtml(m[3])}</span>`;
  }).join("");
  view.scrollTop = view.scrollHeight;
  const last = lines[lines.length - 1];
  if (last) $("#statusbar-last").textContent = last.raw || last.text;
}

/* ── установка релиза ────────────────────────────────────────── */
async function installRelease() {
  if (S.busy) return;
  const ok = await confirmBox("Скачать последний релиз",
    "zip будет скачан с GitHub (Flowseal/zapret-discord-youtube) и распакован " +
    "в %LOCALAPPDATA%\\Diaspas\\zapret. LICENSE.txt из архива сохраняется.\n\nПродолжить?");
  if (!ok) return;
  setBusy(true, "скачиваю релиз…");
  try {
    const res = await api().install_release();
    if (res && res.ok) {
      toast("Релиз установлен: " + (res.tag || ""), "ok");
      // замыкаем цикл: релиз даёт только папку, службу создаёт пресет
      if (res.needs_preset) {
        if (res.active_preset) {
          await createService(res.active_preset);
        } else {
          toast("Теперь примените пресет — он создаст службу", "");
        }
      }
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
  }
}

async function pickDir() {
  try {
    const res = await api().pick_dir();
    if (res && res.ok) toast("Папка zapret: " + res.path, "ok");
    if (res && res.error) toast(res.error, "err");
  } catch (e) {
    toast("Ошибка: " + e, "err");
  }
  await refreshState();
}

/* ── helpers ─────────────────────────────────────────────────── */
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── инициализация ───────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  // вкладки
  document.querySelectorAll(".tab").forEach((t) =>
    t.onclick = () => switchTab(t.dataset.tab));
  document.querySelectorAll("[data-goto]").forEach((b) =>
    b.onclick = () => switchTab(b.dataset.goto));

  // кнопки
  $("#btn-refresh").onclick = refreshState;
  $("#btn-pick-dir").onclick = pickDir;
  $("#btn-install").onclick = installRelease;
  $("#btn-quick-probe").onclick = () => { switchTab("diagnostics"); runProbes(); };
  $("#btn-probe").onclick = runProbes;
  $("#btn-ab").onclick = runAb;
  $("#btn-select").onclick = runSelect;
  $("#btn-select-cancel").onclick = () => api().select_cancel();
  $("#log-filter").onchange = (e) => { S.logFilter = e.target.value; renderLog(); };
  $("#btn-log-copy").onclick = async () => {
    const text = S.logs.map((l) => l.raw || l.text).join("\n");
    try { await navigator.clipboard.writeText(text); toast("Скопировано", "ok"); }
    catch { toast("Не удалось скопировать", "err"); }
  };
  $("#btn-log-open").onclick = () => api().open_log();

  // подписки на события Python
  window.diaspas.on("log", (line) => appendLog(line));
  window.diaspas.on("state", (st) => renderState(st));
  window.diaspas.on("probes", (rep) => renderProbes(rep));
  window.diaspas.on("select_progress", (p) => renderSelectProgress(p));
  window.diaspas.on("busy", (b) => setBusy(!!b.busy, b.label));
  window.diaspas.on("toast", (t) => toast(t.text, t.kind));

  // pywebview сообщает о готовности
  window.addEventListener("pywebviewready", init, { once: true });
  // если событие уже прошло (скрипт в конце body)
  if (window.pywebview) setTimeout(init, 0);
});

let _inited = false;
async function init() {
  if (_inited) return;
  _inited = true;
  try {
    // доказательство живости моста: Python получит вызов и запишет в журнал
    await api().hello();
    // хвост журнала прошлого запуска + текущие строки
    const lines = await api().log_tail(400);
    (lines || []).forEach(appendLog);
    renderPresets(await api().presets());
    await refreshState();
    $("#statusbar-text").textContent = "готов";
  } catch (e) {
    toast("Ошибка инициализации: " + e, "err");
  }
}
