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
  versions: null,    // реестр установленных версий zapret
  strategies: null,  // стратегии активной установки
  releaseRepos: null, // доступные версии обоих источников
  lastSelectBest: null, // лучшая стратегия последнего перебора
  busy: false,
  logs: [],          // строки журнала (объекты {ts, level, text})
  logFilter: "all",
  version: null,     // {version, label, beta} - подпись сборки в статус-баре
  autoRunning: false,   // один автоцикл за раз (на фронте и в Python)
  autoStarted: false,   // авто при старте не повторяем после первой проверки
  lastSelect: null,     // итог ручного перебора — для «Применить лучшую»
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

function promptBox(title, body, value = "") {
  return new Promise((resolve) => {
    $("#modal-input-title").textContent = title;
    $("#modal-input-body").textContent = body;
    const field = $("#modal-input-field");
    field.value = value;
    $("#modal-input").classList.remove("hidden");
    const ok = $("#modal-input-ok"), cancel = $("#modal-input-cancel");
    const done = (val) => {
      $("#modal-input").classList.add("hidden");
      ok.onclick = cancel.onclick = null;
      resolve(val);
    };
    ok.onclick = () => done(field.value);
    cancel.onclick = () => done(null);
    field.focus();
  });
}

function setBusy(busy, label) {
  S.busy = busy;
  $("#busy-ind").classList.toggle("hidden", !busy);
  // «Отменить» доступна пока операция идёт: отказ от долгой закачки -
  // нормальное действие, а не ошибка
  $("#btn-cancel-op").classList.toggle("hidden", !busy);
  if (label !== undefined) $("#statusbar-text").textContent = label;
  // блокировка действий: пока занято, кнопки не дёргают
  document.querySelectorAll(".tab, #preset-grid button, .card .btn, .card select")
    .forEach((b) => { b.disabled = busy; });
  $("#auto-strategy").disabled = busy;
  syncStrategyControls();
}

async function cancelOperation() {
  try {
    const res = await api().cancel_operation();
    toast(res && res.ok ? "отмена запрошена — дождитесь точки остановки"
                        : "сейчас нет операции, которую можно отменить", "");
  } catch (e) {
    toast("Ошибка отмены: " + e, "err");
  }
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
    const src = st.dir_source === "settings"
      ? "Папка релиза" : "Папка из автопоиска (старая установка)";
    sub = `${src}: ${st.zapret_dir} — пресет ещё не применён`;
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
  $("#v-strategy").textContent = (st.strategy_label || st.strategy || "—")
    + (st.strategy_mode === "auto" ? " · авто" : "");
  $("#v-preset").textContent = st.preset_title || "не распознан";
  $("#v-pid").textContent = st.pid ? String(st.pid) : "—";
  $("#zapret-path").textContent = st.zapret_dir || "папка не найдена";
  $("#zapret-path").title = st.zapret_dir || "";

  renderStrategy(st);
  renderTray(st);

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

  // кнопка «Восстановить после перезапуска»: служба запущена, но пресет
  // не распознан - правки вне Diaspas (service.bat) или автозапуск
  const needRestore = st.needs_restore;
  if (needRestore) {
    $("#btn-restore-after-restart").classList.remove("hidden");
    $("#btn-restore-after-restart").textContent = "Восстановить после перезапуска";
    $("#btn-restore-after-restart").onclick = () => restoreAfterRestart(st.suggested_preset);
  } else {
    $("#btn-restore-after-restart").classList.add("hidden");
  }

  // подсветка активного пресета
  document.querySelectorAll(".preset-card").forEach((c) => {
    c.classList.toggle("active", !!st.preset_key && c.dataset.key === st.preset_key);
    const mark = c.querySelector(".mark");
    if (mark) mark.textContent = (st.preset_key && c.dataset.key === st.preset_key) ? "▸" : "";
  });

  if (st.release_note) $("#release-note").textContent = st.release_note;
}

/* ── смена стратегии ────────────────────────────────────────── */
const AUTO_REASON = {
  off: "режим выключен",
  full: "всё отвечает — менять нечего",
  cooldown: "подбор был недавно — пауза",
  preset_off: "пресет «Выкл»: обход выключен намеренно",
  none: "полной стратегии не найдено — оставляю текущую",
  already: "лучшая стратегия уже активна",
};

function fmtMinutes(sec) {
  const m = Math.max(1, Math.round(sec / 60));
  return m >= 60 ? `${Math.round(m / 60)} ч` : `${m} мин`;
}

function agoText(epoch) {
  const min = Math.max(0, Math.round((Date.now() / 1000 - epoch) / 60));
  if (min < 1) return "только что";
  if (min < 60) return `${min} мин назад`;
  const hours = Math.round(min / 60);
  return hours < 48 ? `${hours} ч назад` : `${Math.round(hours / 24)} дн назад`;
}

function renderStrategy(st) {
  const sel = $("#strategy-select");
  const list = st.strategies || [];
  sel.innerHTML = list.length
    ? list.map((s) => `<option value="${escapeHtml(s.file)}">` +
                      `${escapeHtml(s.label)}</option>`).join("")
    : `<option value="">нет стратегий</option>`;
  const active = list.find((s) => s.active);
  sel.value = active ? active.file : (st.strategy_file || "");

  $("#auto-strategy").checked = st.strategy_mode === "auto";
  syncStrategyControls();

  const note = [];
  if (st.strategy_mode === "auto") {
    note.push("авто: подбор при запуске и после проб с сбоями");
    if (st.auto_cooldown_left > 0) {
      note.push(`следующий подбор через ${fmtMinutes(st.auto_cooldown_left)}`);
    }
  } else {
    note.push("выбор вручную: стратегия сохраняется при смене пресета");
  }
  if (st.auto_last_strategy) {
    note.push(`авто применяло ${labelOf(st.auto_last_strategy)}` +
              (st.auto_last_at ? ` · ${agoText(st.auto_last_at)}` : ""));
  }
  $("#strategy-note").textContent = note.join(" · ");
}

/** Доступность контролов стратегии: занято / авто-режим / нет папки. */
function syncStrategyControls() {
  const busy = S.busy;
  const auto = !!S.state && S.state.strategy_mode === "auto";
  const noDir = !S.state || !S.state.dir_ok;
  const sel = $("#strategy-select");
  const btn = $("#btn-apply-strategy");
  const box = $("#auto-strategy");
  sel.disabled = busy || auto || noDir;
  btn.disabled = busy || auto || noDir;
  box.disabled = busy || noDir;
  box.closest(".check").classList.toggle("off", box.disabled);
}

/* ── трей ─────────────────────────────────────────────────────── */
/** Трей: чекбокс показывает включённость, кнопка — скрытие окна.
    «Иконка не поднялась» отличается от «трей выключен»: мягкий отказ
    (нет интерактивного десктопа) должен быть виден, а не молчать. */
function renderTray(st) {
  const box = $("#tray-enabled");
  box.checked = !!st.tray;
  box.disabled = S.busy;
  box.closest(".check").classList.toggle("off", box.disabled);

  const hidden = !!st.window_hidden;
  const btn = $("#btn-hide-window");
  btn.disabled = !st.tray_running || hidden || S.busy;
  btn.textContent = hidden ? "Окно скрыто" : "Скрыть окно";

  $("#tray-note").textContent = st.tray
    ? (st.tray_running
        ? "иконка в трее: меню меняет пресет и стратегию, обход не выключается"
        : "трей включён, но иконка не поднялась (нет интерактивного десктопа)")
    : "трей выключен: закрытие окна выходит из приложения";
}

async function setTray(on) {
  if (S.busy) return;
  setBusy(true, on ? "включаю трей…" : "выключаю трей…");
  try {
    const res = await api().set_tray(!!on);
    if (res && res.ok) {
      toast(on ? "Трей включён" : "Трей выключен", "ok");
    } else {
      toast("Трей: " + ((res && res.error) || "не удалось"), "err");
      $("#tray-enabled").checked = false;   // отказ - чекбокс не врёт
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
    $("#tray-enabled").checked = false;
  } finally {
    setBusy(false, "готов");
    await refreshState();
  }
}

async function hideWindow() {
  if (S.busy) return;
  try {
    const res = await api().hide_window();
    if (res && res.ok) {
      toast("Окно скрыто — открывай двойным кликом по иконке трея", "ok");
    } else {
      toast("Скрыть не удалось: " + ((res && res.error) || "нет трея"), "err");
    }
    await refreshState();
  } catch (e) {
    toast("Ошибка: " + e, "err");
  }
}

async function applyStrategy(file) {
  if (S.busy) return;
  const target = file || $("#strategy-select").value;
  if (!target) {
    toast("Стратегия не выбрана", "err");
    return;
  }
  const label = labelOf(target);
  const ok = await confirmBox("Сменить стратегию",
    `Служба zapret будет пересоздана со стратегией «${label}».\n` +
    `Game Filter, IPSet и списки доменов останутся как сейчас.\n` +
    `Discord и YouTube могут кратковременно прерываться.\n\nПродолжить?`);
  if (!ok) return;

  setBusy(true, `ставлю стратегию ${label}…`);
  try {
    const res = await api().apply_strategy(target);
    if (res && res.ok) toast(`Стратегия: ${label}`, "ok");
    else toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
  }
}

async function toggleAutoMode() {
  const box = $("#auto-strategy");
  const want = box.checked ? "auto" : "manual";
  if (want === "auto") {
    const ok = await confirmBox("Автоподбор стратегий",
      "Diaspas будет проверять доступность сервисов при запуске и после проб\n" +
      "с сбоями, перебирать стратегии и сам применит лучшую. Служба будет\n" +
      "пересоздаваться. Подбор — не чаще одного раза в 30 минут.\n\nВключить?");
    if (!ok) {
      box.checked = false;
      return;
    }
  }
  try {
    const res = await api().set_strategy_mode(want);
    if (!res || !res.ok) {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  }
  await refreshState();
}

async function runAuto(label = "автоподбор стратегии…") {
  if (S.busy || S.autoRunning) return null;
  S.autoRunning = true;
  setBusy(true, label);
  try {
    const res = await api().auto_run();
    if (res && res.probes) renderProbes(res.probes);
    if (res && res.results && res.results.length) {
      $("#select-table tbody").innerHTML = res.results.map(rowHtml).join("");
    }
    if (!res || !res.ok) {
      toast("Авто: " + ((res && res.error) || "не удалось запустить"), "err");
    } else if (res.applied) {
      toast(`Авто: применена ${res.label || labelOf(res.best)}`, "ok");
    } else {
      toast("Авто: " + (AUTO_REASON[res.reason] || res.reason || "без изменений"), "");
    }
    return res;
  } catch (e) {
    toast("Ошибка автоподбора: " + e, "err");
  } finally {
    S.autoRunning = false;
    setBusy(false, "готов");
    await refreshState();
  }
}

async function createService(key) {
  const p = S.presets.find((x) => x.key === key);
  if (!p || p.off || !key) {
    toast("Выберите пресет в разделе «Пресеты»", "");
    return;
  }
  const strat = p.strategy_label || labelOf(p.strategy);
  const okBox = await confirmBox("Создать службу",
    `Папка релиза на месте, но служба zapret не установлена.\n\n` +
    `Применить пресет «${p.title}» со стратегией ${strat}? ` +
    `Он создаст службу и включит обход.`);
  if (!okBox) return;
  // confirmed=true: вопрос уже задан - applyPreset не должен спрашивать снова
  await applyPreset(p, true);
}

async function restoreAfterRestart(key) {
  const p = S.presets.find((x) => x.key === key);
  if (!p || p.off || !key) {
    toast("Выберите пресет в разделе «Пресеты»", "");
    return;
  }
  const okBox = await confirmBox("Восстановить после перезапуска",
    `Служба запущена, но Diaspas не распознал текущий пресет\n` +
    `(правки через service.bat или автозапуск после перезагрузки).\n\n` +
    `Применить пресет «${p.title}» для синхронизации состояния?`);
  if (!okBox) return;
  await applyPreset(p, true);
}

/* ── версии zapret: несколько установленных, одна активная ─────────── */

async function refreshVersions() {
  try {
    const v = await api().versions();
    renderVersions(v);
    return v;
  } catch (e) {
    toast("Ошибка: " + e, "err");
    return null;
  }
}

/* ── стратегии активной установки: выбор и применение ─────────────── */

async function refreshStrategies() {
  try {
    const st = await api().strategies();
    S.strategies = st;
    renderStrategies(st);
    return st;
  } catch (e) {
    toast("Ошибка: " + e, "err");
    return null;
  }
}

function renderStrategies(st) {
  const sel = $("#strategy-select");
  renderPresetSelect($("#strategy-preset"), st && st.target_preset);
  sel.innerHTML = "";
  if (!st || !st.ok || !st.items || !st.items.length) {
    const opt = document.createElement("option");
    opt.textContent = st && st.ok ? "нет стратегий" : "папка zapret не найдена";
    opt.value = "";
    sel.appendChild(opt);
    $("#btn-apply-strategy").classList.add("hidden");
    $("#btn-save-strategy").classList.add("hidden");
    $("#strategy-layout").textContent = "";
    $("#strategy-current").textContent = "";
    return;
  }
  st.items.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = labelOf(name);
    sel.appendChild(opt);
  });
  // подсвечиваем ФАКТИЧЕСКУЮ стратегию службы, а не обещание пресета:
  // иначе кнопка «Применить» висит на пункте, который уже стоит, и
  // «текущая стратегия» выглядит врём
  sel.value = st.current_strategy || st.preset_strategy || st.items[0];
  const layout = st.layout === "winws"
    ? "win-bundle (preset*.cmd)" : "Flowseal (general*.bat)";
  $("#strategy-layout").textContent = layout;
  $("#strategy-current").textContent = st.current_strategy
    ? `сейчас: ${labelOf(st.current_strategy)}` : "";
  updateStrategyButtons();
}

/* Целевой пресет выбирается явно: без этого стратегия привязывалась
   только к маркеру active_preset, и служба, поставленная service.bat,
   переключаться не давала вовсе. «Выкл» не принимает стратегию. */
function renderPresetSelect(sel, target) {
  sel.innerHTML = "";
  const list = S.presets.filter((p) => !p.off);
  list.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.title;
    sel.appendChild(opt);
  });
  if (!list.length) return;
  sel.value = list.some((p) => p.key === target) ? target : list[0].key;
}

function strategyTargetKey() {
  const sel = $("#strategy-preset");
  return sel.value || (S.strategies && S.strategies.target_preset) || null;
}

function updateStrategyButtons() {
  const chosen = $("#strategy-select").value;
  const running = S.strategies && S.strategies.current_strategy;
  const stored = S.strategies && S.strategies.preset_strategy;
  // «Применить» - только когда есть что менять по факту службы
  $("#btn-apply-strategy").classList.toggle(
    "hidden", !chosen || chosen === running);
  // «Запомнить» - выбор без перезапуска службы, если он отличается от
  // записанного для пресета (следующее применение пресета его использует)
  $("#btn-save-strategy").classList.toggle(
    "hidden", !chosen || chosen === stored);
  // «Попробовать» имеет смысл только против работающей службы: без неё
  // проверять нечего, и возвратить тоже нечего
  $("#btn-try-strategy").classList.toggle(
    "hidden", !chosen || !running || chosen === running);
  loadStrategyDetail(chosen);
}

/* Разбор аргументов winws: имя файла ничего не говорит пользователю,
   поэтому под селектором показываем флаги и diff к тому, что стоит. */
async function loadStrategyDetail(name) {
  const box = $("#strategy-detail");
  if (!name) { box.classList.add("hidden"); return; }
  try {
    const res = await api().strategy_detail(name);
    if (!res || !res.ok) {
      box.classList.remove("hidden");
      box.textContent = "разбор стратегии: " + ((res && res.error) || "нет ответа");
      return;
    }
    box.classList.remove("hidden");
    box.textContent = strategyDetailText(res);
  } catch (e) {
    box.classList.remove("hidden");
    box.textContent = "разбор стратегии не получился: " + e;
  }
}

function strategyDetailText(res) {
  const d = res.detail || {};
  const parts = [];
  if (!d.ok) return "не разбирается: " + (d.error || "ошибка");
  if (d.headline && d.headline.length) parts.push(d.headline.join(" "));
  const diff = res.diff || {};
  if (diff.unknown_baseline) {
    parts.push("служба не отвечает — сравнить не с чем");
  } else if (diff.same) {
    parts.push("отличий от текущей нет");
  } else {
    if (diff.added && diff.added.length) parts.push("+ " + diff.added.join(" "));
    if (diff.removed && diff.removed.length) parts.push("- " + diff.removed.join(" "));
    if (diff.changed && diff.changed.length) parts.push("~ " + diff.changed.join("  "));
  }
  if (res.best && res.best !== d.file) {
    parts.push(`подобранная перебором: ${labelOf(res.best)}`);
  }
  parts.push(`${d.size} байт, ${d.features.count} флагов`);
  return parts.join("\n");
}

async function tryChosenStrategy() {
  // оценка рекомендации до пересоздания службы: probe -> apply -> probe -> возврат
  const chosen = $("#strategy-select").value;
  const key = strategyTargetKey();
  if (!chosen || !key || key === "off") return;
  const okBox = await confirmBox("Проба стратегии",
    `Служба будет переустановлена на «${labelOf(chosen)}», пробы сравнят` +
    ` обход, затем вернётся прежнее состояние.\n\nПробовать?`);
  if (!okBox) return;
  setBusy(true, `пробую ${labelOf(chosen)}…`);
  try {
    const res = await api().try_strategy(key, chosen);
    if (!res || !res.ok) {
      toast("Проба: " + ((res && res.error) || "не удалось"), "err");
      if (res && res.restored === false) {
        toast("Прежнее состояние НЕ восстановлено — нажмите «Обновить»" +
              " и примените пресет", "err");
      }
    } else {
      const b = res.before || {}, a = res.after || {};
      toast(`${labelOf(chosen)}: ${b.count}/${b.total} → ${a.count}/${a.total}` +
            (res.better ? " — лучше" : " — не лучше"), res.better ? "ok" : "");
      $("#strategy-detail").textContent =
        `проба: было ${b.count}/${b.total} (${b.state}), стало ` +
        `${a.count}/${a.total} (${a.state}); прежнее восстановлено ` +
        (res.restored ? "да" : "НЕТ — примените пресет вручную");
    }
  } catch (e) {
    toast("Ошибка пробы: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshStrategies();
  }
}

/* ── перенос связки пресета ─────────────────────────────────────────── */

async function exportCurrentPreset() {
  const key = (S.state && S.state.preset_key)
    || (S.strategies && S.strategies.target_preset);
  if (!key) { toast("Нет текущего пресета для экспорта", "err"); return; }
  try {
    const res = await api().export_preset(key);
    if (!res || !res.ok) {
      toast("Экспорт: " + ((res && res.error) || "не удалось"), "err");
      return;
    }
    try {
      await navigator.clipboard.writeText(res.json);
      toast("Связка скопирована в буфер обмена", "ok");
    } catch {
      await promptBox("Экспорт связки", "Сохраните текст:", res.json);
    }
  } catch (e) {
    toast("Ошибка экспорта: " + e, "err");
  }
}

async function importPresetBundle() {
  const text = await promptBox("Импорт связки",
    "Вставьте json связки (title обязателен). Стратегия проверяется по " +
    "этой установке: чужой файл не запишется.", "");
  if (!text) return;
  try {
    const res = await api().import_preset(text);
    if (res && res.ok) {
      toast("Пресет добавлен: " + res.key, "ok");
      await refreshPresets();
      await refreshStrategies();
    } else {
      toast("Импорт: " + ((res && res.error) || "не удалось"), "err");
    }
  } catch (e) {
    toast("Ошибка импорта: " + e, "err");
  }
}

async function applyChosenStrategy() {
  const chosen = $("#strategy-select").value;
  if (!chosen) return;
  const key = strategyTargetKey();
  if (!key || key === "off") {
    toast("Выберите пресет — стратегия привязывается к нему", "");
    return;
  }
  const p = S.presets.find((x) => x.key === key);
  const title = p ? p.title : key;
  const okBox = await confirmBox("Применение стратегии",
    `Пресет «${title}» будет применён со стратегией «${labelOf(chosen)}».\n` +
    `Стратегия запомнится для этого пресета в этой установке.\n` +
    `После применения обход проверится пробами; если он пропадёт — ` +
    `автоматически вернётся прежнее состояние.\n\nПродолжить?`);
  if (!okBox) return;
  setBusy(true, `применяю ${labelOf(chosen)}…`);
  try {
    // verify=true: проверка пробами с откатом — смена стратегии опаснее,
    // чем применение пресета с его базовой стратегией
    const res = await api().apply_preset(key, chosen, true);
    if (res && res.ok) {
      const b = res.before || {}, a = res.after || {};
      const verdict = a.total ? ` обход: ${a.count}/${a.total}` : "";
      toast(`Пресет «${title}» со стратегией ${labelOf(chosen)} применён` + verdict, "ok");
    } else if (res && res.rolled_back) {
      const b = res.before || {}, a = res.after || {};
      toast(`Обход пропал (${b.count}/${b.total} → 0/${a.total}) — возвращено ` +
            `прежнее состояние${res.restore_ok ? "" : ", возврат НЕ удался"}`, "err");
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshStrategies();
    await refreshVersions();
    await refreshPresets();
  }
}

async function saveChosenStrategy() {
  // выбор без перезапуска службы: применится при следующем действии
  const chosen = $("#strategy-select").value;
  const key = strategyTargetKey();
  if (!chosen || !key || key === "off") return;
  setBusy(true, "запоминаю стратегию…");
  try {
    const res = await api().set_preset_strategy(key, chosen);
    if (res && res.ok) {
      toast(`Для пресета запомнена стратегия ${labelOf(chosen)} — ` +
            `применится при следующем применении пресета`, "ok");
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshPresets();
    await refreshStrategies();
  }
}

async function takeBestStrategy() {
  // «взять лучшую» = применить целевой пресет с рекомендованной стратегией
  const best = S.lastSelectBest;
  const key = strategyTargetKey();
  if (!best) return;
  if (!key || key === "off") {
    toast("Выберите пресет — стратегия привязывается к нему", "");
    return;
  }
  const p = S.presets.find((x) => x.key === key);
  const okBox = await confirmBox("Применение рекомендации",
    `Применить к пресету «${p ? p.title : key}» стратегию ` +
    `«${labelOf(best)}» из перебора?`);
  if (!okBox) return;
  setBusy(true, `применяю ${labelOf(best)}…`);
  try {
    const res = await api().apply_preset(key, best, true);
    if (res && res.ok) {
      toast(`Стратегия ${labelOf(best)} применена`, "ok");
      $("#btn-take-best").classList.add("hidden");
    } else if (res && res.rolled_back) {
      toast("Обход пропал после применения рекомендации — возвращено " +
            "прежнее состояние", "err");
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshStrategies();
    await refreshPresets();
  }
}

/* ── скачивание любой доступной версии ────────────────────────────── */

async function listReleases() {
  setBusy(true, "запрашиваю список версий у GitHub…");
  let res;
  try {
    res = await api().available_releases();
  } finally {
    setBusy(false, "готов");
  }
  if (!res || !res.ok) {
    toast("Ошибка: " + ((res && res.error) || "нет связи с GitHub"), "err");
    return;
  }
  S.releaseRepos = res.repos;
  renderRepoSelect(res.repos);
  renderReleaseTable(currentRepo());
}

function currentRepo() {
  if (!S.releaseRepos || !S.releaseRepos.length) return null;
  const name = $("#release-repo").value;
  return S.releaseRepos.find((r) => r.repo === name) || S.releaseRepos[0];
}

function renderRepoSelect(repos) {
  const sel = $("#release-repo");
  sel.innerHTML = "";
  repos.forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r.repo;
    const short = r.repo.split("/").pop();
    opt.textContent = `${short} (${r.entries.length})` +
      (r.stale ? " — офлайн-кэш" : "");
    sel.appendChild(opt);
  });
}

function renderReleaseTable(repoData) {
  const tbody = $("#release-table tbody");
  if (!repoData || !repoData.entries || !repoData.entries.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="muted">нет версий</td></tr>';
    return;
  }
  tbody.innerHTML = "";
  repoData.entries.forEach((e) => {
    const tr = document.createElement("tr");
    const size = e.branch ? "ветка"
      : (e.size ? `${(e.size / 1048576).toFixed(1)} МБ` : "—");
    const date = e.published ? e.published.slice(0, 10) : "—";
    tr.innerHTML =
      `<td class="mono">${escapeHtml(e.tag)}</td>` +
      `<td class="mono">${escapeHtml(date)}</td>` +
      `<td class="mono">${escapeHtml(size)}</td>` +
      `<td><button class="btn" data-repo="${escapeHtml(e.repo)}" ` +
      `data-tag="${escapeHtml(e.tag)}">Скачать</button></td>`;
    tr.querySelector("button").onclick = () =>
      downloadChosen(e.repo, e.tag);
    tbody.appendChild(tr);
  });
}

async function downloadChosen(repo, tag) {
  const short = repo.split("/").pop();
  const okBox = await confirmBox("Скачивание версии",
    `Скачать ${short} ${tag}?\n\nВерсия установится РЯДОМ с текущими, ` +
    `затем её можно сделать активной. Пресет будет применён к ней, ` +
    `а после проверки обхода можно переключиться обратно.`);
  if (!okBox) return;
  setBusy(true, `скачиваю ${short} ${tag}…`);
  try {
    const res = await api().download_release(repo, tag);
    if (res && res.ok) {
      toast(`Установлена версия ${tag}`, "ok");
      if (res.needs_preset && res.active_preset) {
        await createService(res.active_preset);
      }
      await offerStale(res.stale);
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshVersions();
    await refreshStrategies();
    await refreshPresets();
  }
}

function renderVersions(v) {
  S.versions = v;
  const sel = $("#version-select");
  sel.innerHTML = "";
  if (!v.items || !v.items.length) {
    const opt = document.createElement("option");
    opt.textContent = "нет установленных версий";
    opt.value = "";
    sel.appendChild(opt);
  } else {
    v.items.forEach((it) => {
      const opt = document.createElement("option");
      opt.value = it.path;
      const mark = it.path === v.active ? "● активная" : "";
      const broken = it.valid ? "" : " (повреждена)";
      const layout = it.layout === "winws" ? "win-bundle" :
        (it.layout === "flowseal" ? "flowseal" : "");
      opt.textContent = `${it.version}${mark ? " — " + mark : ""}` +
        `${layout ? " [" + layout + "]" : ""}${broken}`;
      opt.disabled = !it.valid;
      sel.appendChild(opt);
    });
    sel.value = v.active || (v.items[0] && v.items[0].path) || "";
  }

  // кнопки видны, только когда есть что делать
  const selected = sel.value;
  $("#btn-switch-version").classList.toggle(
    "hidden", !(selected && selected !== v.active));
  $("#btn-rollback").classList.toggle("hidden", !v.rollback);
  $("#btn-remove-version").classList.toggle(
    "hidden", !(selected && selected !== v.active));

  if (v.best_strategy) {
    $("#statusbar-last").textContent =
      `для этой версии подобрана стратегия: ${labelOf(v.best_strategy)}`;
  }
}

async function switchVersion() {
  const sel = $("#version-select");
  const path = sel.value;
  if (!path || !S.versions || path === S.versions.active) return;
  const name = path.split("\\").pop() || path;
  const okBox = await confirmBox("Переключение версии",
    `Активной станет версия «${name}».\n` +
    `Служба zapret будет переустановлена на её папку с текущим пресетом.\n` +
    `Прежняя версия сохранится для отката.\n\nПродолжить?`);
  if (!okBox) return;
  setBusy(true, `переключение на ${name}…`);
  try {
    const res = await api().switch_version(path);
    if (res && res.ok) {
      toast(`Активная версия: ${name}`, "ok");
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshVersions();
    // активная установка сменилась - список стратегий и дефолты пресетов
    // другие (переопределения хранятся по установке)
    await refreshStrategies();
    await refreshPresets();
  }
}

async function rollbackVersion() {
  if (!S.versions || !S.versions.rollback) return;
  const name = S.versions.rollback.split("\\").pop();
  const okBox = await confirmBox("Откат версии",
    `Вернуться на «${name}»? Служба будет переустановлена на её папку.`);
  if (!okBox) return;
  setBusy(true, `откат на ${name}…`);
  try {
    const res = await api().rollback_version();
    if (res && res.ok) toast(`Откат выполнен: ${name}`, "ok");
    else toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshVersions();
    await refreshStrategies();
    await refreshPresets();
  }
}

async function removeVersion() {
  const sel = $("#version-select");
  const path = sel.value;
  if (!path || !S.versions || path === S.versions.active) return;
  const name = path.split("\\").pop() || path;
  const okBox = await confirmBox("Удаление версии",
    `Удалить «${name}» и её файлы с диска? Действие необратимо.`);
  if (!okBox) return;
  try {
    const res = await api().remove_version(path);
    if (res && res.ok) toast(`Версия ${name} удалена`, "ok");
    else toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
  } catch (e) {
    toast("Ошибка: " + e, "err");
  }
  await refreshVersions();
}

async function offerStale(stale) {
  // версии сверх лимита после установки/обновления - предложить убрать
  if (!stale || !stale.length) return;
  const names = stale.map((s) => s.version).join(", ");
  const okBox = await confirmBox("Очистка старых версий",
    `Установлено версий больше лимита. Удалить старые: ${names}?\n` +
    `Активная и откатная версии не удаляются.`);
  if (!okBox) return;
  for (const s of stale) {
    const res = await api().remove_version(s.path);
    if (!(res && res.ok)) {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
      break;
    }
  }
  toast("Старые версии удалены", "ok");
  await refreshVersions();
}

async function checkUpdate() {
  setBusy(true, "проверяю обновления на GitHub…");
  let res;
  try {
    res = await api().check_update();
  } finally {
    setBusy(false, "готов");
  }
  if (!res || !res.ok) {
    toast("Ошибка: " + ((res && res.error) || "нет связи с GitHub"), "err");
    return;
  }
  if (!res.update_available) {
    toast(`Активная версия ${res.current || "?"} — актуальна`, "ok");
    return;
  }
  await updateLatest(res.latest);
}

async function updateLatest(tag) {
  const okBox = await confirmBox("Обновление zapret",
    `Доступна версия ${tag}.\n\nНовая версия установится РЯДОМ со старой, ` +
    `затем пресет применится к ней и обход будет проверен. ` +
    `Если проверка не пройдёт — произойдёт автоматический откат.\n\n` +
    `Обновить?`);
  if (!okBox) return;
  setBusy(true, `обновление до ${tag}…`);
  try {
    const res = await api().update_latest();
    if (res && res.ok && res.up_to_date) {
      toast("Уже актуальная версия", "ok");
    } else if (res && res.ok) {
      toast(`Обновлено до ${res.tag}: обход проверен`, "ok");
      await offerStale(res.stale);
    } else if (res && res.rolled_back) {
      toast(`Обновление отменено: ${res.error || "обход сломался"} — ` +
            `выполнено откат`, "err");
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshVersions();
    await refreshStrategies();
    await refreshPresets();
  }
}

async function checkInstall() {
  setBusy(true, "проверяю файлы установки…");
  let res;
  try {
    res = await api().check_install();
  } finally {
    setBusy(false, "готов");
  }
  if (!res || !res.ok) {
    toast("Ошибка: " + ((res && res.error) || "папка не найдена"), "err");
    return;
  }
  if (!res.missing || !res.missing.length) {
    toast(`Версия ${res.version} цела: все файлы на месте`, "ok");
    return;
  }
  const list = res.missing.slice(0, 6).join(", ") +
               (res.missing.length > 6 ? `… (+${res.missing.length - 6})` : "");
  const okBox = await confirmBox("Повреждённая установка",
    `Отсутствуют файлы (${res.missing.length}):\n${list}\n\n` +
    `Восстановить из архива этой же версии из кэша?`);
  if (!okBox) return;
  setBusy(true, "восстанавливаю файлы…");
  try {
    const r2 = await api().repair_install();
    if (r2 && r2.ok) toast(`Восстановлено файлов: ${r2.restored}`, "ok");
    else toast("Ошибка: " + ((r2 && r2.error) || "архив не найден"), "err");
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
  }
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
    // стратегия видна на карточке: без неё «везде ALT11» читалось как
    // неизменяемое, хотя переопределение существовало в настройках
    const strat = p.off ? "" :
      `<span class="strat${p.overridden ? " custom" : ""}">` +
      `${escapeHtml(p.strategy_label || labelOf(p.strategy))}` +
      `${p.overridden ? " · изменена" : ""}</span>`;
    b.innerHTML = `<span class="name">${escapeHtml(p.title)}<span class="mark"></span></span>
                   ${strat}
                   <span class="desc">${escapeHtml(p.description || "")}</span>`;
    b.onmouseenter = () => { $("#preset-hint").textContent = p.description || ""; };
    b.onclick = () => applyPreset(p);
    grid.appendChild(b);
  });
  // сетка пересобрана - подсветку активного пресета надо вернуть
  if (S.state) renderState(S.state);
}

async function refreshPresets() {
  try {
    renderPresets(await api().presets());
  } catch (e) {
    toast("Не удалось обновить пресеты: " + e, "err");
  }
}

async function applyPreset(p, confirmed = false) {
  if (S.busy) return;
  // один вопрос на операцию: createService уже спросил - второй раз не спрашиваем
  if (!confirmed) {
    const what = p.off
      ? "Служба zapret будет удалена, winws остановлен."
      : `Стратегия: ${p.strategy_label || p.strategy}` +
        `${p.game_filter ? `, Game Filter: ${p.game_filter}` : ""}` +
        `${p.ipset ? `, IPSet: ${p.ipset}` : ""}` +
        `${p.exclude_domains && p.exclude_domains.length ? ", исключения: " + p.exclude_domains.join(", ") : ""}` +
        `${p.exclude_ips && p.exclude_ips.length ? ", IP-исключения: " + p.exclude_ips.join(", ") : ""}`;
    const ok = await confirmBox(`Пресет «${p.title}»`, what + "\n\nПродолжить?");
    if (!ok) return;
  }

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
    await refreshStrategies();
    await refreshPresets();
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
  let report = null;
  try {
    report = await api().probes_run();
    renderProbes(report);
    $("#statusbar-text").textContent =
      `пробы: ${report.count}/${report.total}`;
  } catch (e) {
    toast("Ошибка проб: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshHistory();
  }
}

/* ── история замеров ───────────────────────────────────────────────── */
function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function refreshHistory() {
  try {
    const res = await api().probe_history(15);
    renderHistory((res && res.items) || []);
  } catch (e) {
    toast("История недоступна: " + e, "err");
  }
}

function renderHistory(items) {
  const body = $("#history-table tbody");
  if (!items.length) {
    body.innerHTML = "<tr><td colspan='5' class='muted'>нет замеров</td></tr>";
    return;
  }
  const rows = items.map((it, i) => {
    const prev = items[i - 1];
    let delta = "";
    if (prev && prev.total) {
      const d = (it.count || 0) - (prev.count || 0);
      delta = d > 0 ? `+${d}` : (d < 0 ? `${d}` : "0");
    }
    const cls = it.state === "full" ? "ok" : (it.state === "none" ? "err" : "warn");
    return `<tr>
      <td class="mono">${fmtTs(it.ts)}</td>
      <td class="mono">${escapeHtml(it.strategy || "—")}</td>
      <td>${escapeHtml(it.preset || "—")}</td>
      <td class="${cls}">${it.count || 0}/${it.total || 0}</td>
      <td class="mono muted">${delta}</td></tr>`;
  }).reverse().join("");
  body.innerHTML = rows;
}

/* ── свои цели пробы ───────────────────────────────────────────────── */
async function refreshUserGroups() {
  try {
    const res = await api().probe_groups();
    if (!res || !res.ok) return;
    const names = Object.keys(res.user || {});
    $("#user-groups").innerHTML = names.length
      ? names.map((n) => `<span class="chip"><span class="dot"></span>${escapeHtml(n)}
          <span class="count">${(res.user[n] || []).length}</span></span>`).join("")
      : "<span class='muted'>своих групп нет — встроенные: " +
        escapeHtml((res.builtin || []).join(", ")) + "</span>";
  } catch (e) {
    toast("Группы пробы не прочитаны: " + e, "err");
  }
}

async function addProbeTarget() {
  const name = $("#pg-name").value.trim();
  const url = $("#pg-url").value.trim();
  if (!name || !url) { toast("Нужны имя группы и адрес", "err"); return; }
  try {
    const res = await api().probe_groups_add(name, url);
    if (res && res.ok) {
      toast(`Цель добавлена в «${name}»`, "ok");
      $("#pg-url").value = "";
      await refreshUserGroups();
    } else {
      toast("Добавление: " + ((res && res.error) || "не удалось"), "err");
    }
  } catch (e) {
    toast("Ошибка добавления: " + e, "err");
  }
}

async function removeProbeGroup() {
  const name = $("#pg-name").value.trim();
  if (!name) { toast("Укажите имя группы", "err"); return; }
  const okBox = await confirmBox("Удаление группы пробы",
    `Убрать пользовательскую группу «${name}» со всеми её целями?`);
  if (!okBox) return;
  try {
    const res = await api().probe_groups_remove(name);
    if (res && res.ok) {
      toast(`Группа «${name}» удалена`, "ok");
      await refreshUserGroups();
    } else {
      toast("Удаление: " + ((res && res.error) || "не удалось"), "err");
    }
  } catch (e) {
    toast("Ошибка удаления: " + e, "err");
  }
  // В авто-режиме сбой проб - триггер: подбор и применение лучшей стратегии.
  if (report && S.state && S.state.strategy_mode === "auto"
      && report.state !== "full" && report.total > 0) {
    await runAuto("авто: подбираю стратегию после сбоя…");
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
  const kind = p.auto ? "авто" : "перебор";
  const text = `${kind} ${done}/${total}` +
    (p.best ? ` · лучшая ${labelOf(p.best)}` : "");
  $("#select-progress").textContent = text;
  $("#select-bar").style.width = total ? `${done * 100 / total}%` : "0%";
  const rows = (p.results || []).map(rowHtml).join("");
  if (rows) $("#select-table tbody").innerHTML = rows;
  if (S.busy) $("#statusbar-text").textContent = text;
}

function rowHtml(r) {
  const cell = (v) => v ? `<span class="ok">+</span>` : `<span class="err">−</span>`;
  const targets = {};
  (r.targets || []).forEach((t) => {
    if (!t.ok) targets[t.group] = false;
    else if (targets[t.group] === undefined) targets[t.group] = true;
  });
  const stateCls = r.state === "full" ? "ok" : (r.state === "partial" ? "warn" : "err");
  return `<tr class="${r.best ? "best-row" : ""}">
    <td class="mono">${escapeHtml(labelOf(r.name))}</td>
    <td class="${stateCls}">${escapeHtml(r.state || "")}</td>
    <td class="mono">${r.count}/${r.total}</td>
    <td>${cell(targets.discord)}</td><td>${cell(targets.youtube)}</td>
    <td>${cell(targets.ea)}</td><td>${cell(targets.ubisoft)}</td></tr>`;
}

async function runSelect() {
  if (S.busy) return;
  const ok = await confirmBox("Перебор стратегий",
    "Служба zapret будет пересоздаваться для каждой стратегии; Discord и YouTube " +
    "могут кратковременно прерываться. Продолжить?");
  if (!ok) return;

  setBusy(true, "перебор стратегий…");
  $("#btn-select-cancel").disabled = false;
  $("#select-best").classList.add("hidden");
  S.lastSelect = null;
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
      $("#select-best").textContent = `★ Рекомендуемая стратегия: ${best}` +
        (res.early ? "" : ` (перебрано ${res.done || 0}/${res.total || 0})`);
      // рекомендация должна применяться, а не только отображаться
      S.lastSelectBest = res.best || null;
      if (res.best) {
        $("#btn-take-best").classList.remove("hidden");
        $("#btn-take-best").textContent = `★ Взять ${best}`;
      }
      toast(`Лучшая стратегия: ${best}`, "ok");
      S.lastSelect = res;
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
  // general (ALT11).bat / general (ALT11) -> ALT11; general.bat -> general;
  // preset1_example.cmd -> preset1_example (win-bundle)
  const name = String(file || "");
  if (!name) return "";
  const m = name.match(/^general\s*\(([^)]+)\)/i);
  if (m) return m[1];
  return name.replace(/\.(bat|cmd)$/i, "") || "general";
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
      // замыкаем цикл: релиз даёт только папку, службу создаёт пресет.
      // Пресеты и стратегии перечитываются под НОВУЮ папку - у неё может
      // быть другой layout, и стратегия дефолта тоже другая
      await refreshPresets();
      await refreshStrategies();
      if (res.needs_preset) {
        if (res.active_preset) {
          await createService(res.active_preset);
        } else {
          toast("Теперь примените пресет — он создаст службу", "");
        }
      }
      await offerStale(res.stale);
    } else {
      toast("Ошибка: " + ((res && res.error) || "неизвестно"), "err");
    }
  } catch (e) {
    toast("Ошибка: " + e, "err");
  } finally {
    setBusy(false, "готов");
    await refreshState();
    await refreshVersions();
    await refreshStrategies();
    await refreshPresets();
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
  // папка сменилась - список стратегий и дефолты пресетов берутся из неё
  await refreshState();
  await refreshStrategies();
  await refreshPresets();
}

/* ── helpers ─────────────────────────────────────────────────── */
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Подпись сборки в статус-баре: тег релиза/беты или «unreleased». */
function renderVersion(v) {
  S.version = v || null;
  const el = $("#app-version");
  if (!v) {
    el.textContent = "";
    el.classList.remove("beta");
    return;
  }
  const label = v.label || "unreleased";
  el.textContent = v.beta ? `${label} · бета` : label;
  el.classList.toggle("beta", !!v.beta);
  el.title = v.built_from_tag
    ? `Собрано из тега ${label} (числовая версия ${v.version})`
    : `Собрано не из тега: числовая версия ${v.version}`;
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
  $("#btn-cancel-op").onclick = cancelOperation;
  $("#btn-pick-dir").onclick = pickDir;
  $("#btn-install").onclick = installRelease;
  $("#btn-quick-probe").onclick = () => { switchTab("diagnostics"); runProbes(); };
  $("#btn-probe").onclick = runProbes;
  $("#btn-history-refresh").onclick = refreshHistory;
  $("#btn-pg-add").onclick = addProbeTarget;
  $("#btn-pg-remove").onclick = removeProbeGroup;
  $("#btn-ab").onclick = runAb;
  $("#btn-select").onclick = runSelect;
  $("#btn-select-cancel").onclick = () => api().select_cancel();
  $("#btn-take-best").onclick = takeBestStrategy;
  // стратегии: выбор, применение и запоминание без перезапуска службы
  $("#btn-apply-strategy").onclick = applyChosenStrategy;
  $("#btn-save-strategy").onclick = saveChosenStrategy;
  $("#btn-try-strategy").onclick = tryChosenStrategy;
  $("#strategy-select").onchange = updateStrategyButtons;
  $("#strategy-preset").onchange = updateStrategyButtons;
  // авто-режим при запуске и трей
  $("#auto-strategy").onchange = toggleAutoMode;
  $("#tray-enabled").onchange = (e) => setTray(e.target.checked);
  $("#btn-hide-window").onclick = hideWindow;
  // перенос связки «пресет + стратегия + списки»
  $("#btn-export-preset").onclick = exportCurrentPreset;
  $("#btn-import-preset").onclick = importPresetBundle;
  // восстановление после перезапуска
  $("#btn-restore-after-restart").onclick = () => restoreAfterRestart();
  // скачивание версий
  $("#btn-list-releases").onclick = listReleases;
  $("#release-repo").onchange = () => renderReleaseTable(currentRepo());
  // версии zapret
  $("#btn-switch-version").onclick = switchVersion;
  $("#btn-rollback").onclick = rollbackVersion;
  $("#btn-remove-version").onclick = removeVersion;
  $("#btn-check-update").onclick = checkUpdate;
  $("#btn-check-install").onclick = checkInstall;
  $("#version-select").onchange = () => {
    // кнопки зависят от выбора: переключить/удалить - только неактивную
    const sel = $("#version-select");
    const act = S.versions && S.versions.active;
    const chosen = sel.value;
    $("#btn-switch-version").classList.toggle(
      "hidden", !chosen || chosen === act);
    $("#btn-remove-version").classList.toggle(
      "hidden", !chosen || chosen === act);
    // список стратегий зависит от установки: у другой версии - свои
    refreshStrategies();
  };
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
  // трей просит конкретную вкладку («Журнал») - окно может быть скрыто
  window.diaspas.on("tab", (t) => { if (t && t.tab) switchTab(t.tab); });

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
    renderVersion(await api().version());
    // хвост журнала прошлого запуска + текущие строки
    const lines = await api().log_tail(400);
    (lines || []).forEach(appendLog);
    renderPresets(await api().presets());
    await refreshState();
    await refreshVersions();
    await refreshStrategies();
    await refreshHistory();
    await refreshUserGroups();
    $("#statusbar-text").textContent = "готов";
    await warmupPrivileges();
    // Авто-режим при старте: проверка доступности и, если есть сбои,
    // подбор + применение лучшей стратегии. Один раз на сессию.
    if (S.state && S.state.strategy_mode === "auto" && S.state.running
        && !S.autoStarted) {
      S.autoStarted = true;
      setTimeout(() => {
        runAuto("авто: проверяю доступность при запуске…");
      }, 400);
    }
  } catch (e) {
    toast("Ошибка инициализации: " + e, "err");
  }
}

/* ── TgWsProxy: вкладка Telegram ─────────────────────────────────────
 * Карточка читает state.tg (событие state приходит каждые 5 с) и зовёт
 * api().tg_*; мутации проходят через oplock на стороне Python. */
(() => {
  const $ = (s) => document.querySelector(s);
  const api = () => window.pywebview.api;
  let T = null;

  function renderTg(tg) {
    if (!tg) return;
    T = tg;
    const dot = $("#tg-dot");
    if (!tg.installed) {
      dot.className = "big-dot unknown";
      $("#tg-headline").textContent = "не установлено";
      $("#tg-sub").textContent = "официальный релиз с проверкой sha256";
    } else if (tg.running) {
      dot.className = tg.listening === false ? "big-dot warn" : "big-dot ok";
      $("#tg-headline").textContent =
        tg.listening === false ? "запущен, но порт молчит" : "работает";
      $("#tg-sub").textContent = tg.host + ":" + tg.port +
        (tg.listening === false ? "" : " принимает соединения");
    } else {
      dot.className = "big-dot warn";
      $("#tg-headline").textContent = "установлено, не запущено";
      $("#tg-sub").textContent = tg.host + ":" + tg.port;
    }
    $("#tg-tag").textContent = tg.tag || "—";
    $("#tg-addr").textContent = tg.host + ":" + tg.port +
      (tg.link ? " · ссылка готова" : " · секрета нет");
    $("#tg-auto").textContent = tg.autostart ? "вкл" : "выкл";
    $("#tg-zlist").textContent = tg.zapret_list ? "добавлены" : "не добавлены";
    $("#tg-btn-autostart").textContent =
      "Автозапуск: " + (tg.autostart ? "вкл" : "выкл");
    $("#tg-btn-zapret").textContent =
      "Telegram-домены: " + (tg.zapret_list ? "вкл" : "выкл");
    $("#tg-btn-start").disabled = !tg.installed || tg.running;
    $("#tg-btn-stop").disabled = !tg.running;
    $("#tg-btn-link").disabled = !tg.link;
    $("#tg-status").textContent = tg.installed
      ? (tg.running ? "онлайн" : "офлайн") : "";
    const notes = [];
    if (tg.foreign && tg.foreign.length) {
      notes.push("чужая копия процесса: " + tg.foreign.join(", "));
    }
    if (!tg.config_found) {
      notes.push("конфиг не найден — запустите прокси один раз");
    }
    $("#tg-note").textContent = notes.join(" · ");
  }

  async function tgCall(name, ...args) {
    try {
      const res = await api()[name](...args);
      if (!res || !res.ok) {
        toast("TgWsProxy: " + ((res && res.error) || "не удалось"), "err");
      }
      return res;
    } catch (e) {
      toast("TgWsProxy: " + e, "err");
      return null;
    }
  }
  async function tgRefresh() {
    const res = await tgCall("tg_state");
    if (res && res.tg) renderTg(res.tg);
  }

  $("#tg-btn-install").onclick = async () => {
    setBusy(true, "устанавливаю TgWsProxy…");
    try {
      const res = await tgCall("tg_install");
      if (res && res.ok) {
        toast("TgWsProxy " + (res.updated ? "обновлён" : "установлен") +
              ": " + res.tag, "ok");
      }
      await tgRefresh();
    } finally {
      setBusy(false, "готов");
      await refreshState();
    }
  };
  $("#tg-btn-check").onclick = async () => {
    const res = await tgCall("tg_check_update");
    if (res && res.ok) {
      if (res.update_available) {
        toast("Доступна версия " + res.latest +
              " (стоит " + (res.current || "нет") + ")");
      } else {
        toast("Установлена последняя версия (" +
              (res.current || "?") + ")", "ok");
      }
    }
  };
  $("#tg-btn-start").onclick = async () => {
    await tgCall("tg_start");
    await tgRefresh();
  };
  $("#tg-btn-stop").onclick = async () => {
    await tgCall("tg_stop");
    await tgRefresh();
  };
  $("#tg-btn-link").onclick = async () => {
    const res = await tgCall("tg_link");
    if (!res || !res.ok) return;
    try {
      await navigator.clipboard.writeText(res.link);
      toast("Ссылка скопирована — вставьте в «Избранное» Telegram", "ok");
    } catch {
      window.prompt("Скопируйте ссылку:", res.link);
    }
  };
  $("#tg-btn-dir").onclick = () => tgCall("tg_open_dir");
  $("#tg-btn-autostart").onclick = async () => {
    const res = await tgCall("tg_autostart", !(T && T.autostart));
    if (res && res.ok) await tgRefresh();
  };
  $("#tg-btn-dc").onclick = async () => {
    const res = await tgCall("tg_fix_dc");
    if (res && res.ok) {
      toast(res.changed ? "dc_ip исправлен (оставлен DC 4)"
                        : "dc_ip уже в норме", "ok");
    }
    await tgRefresh();
  };
  $("#tg-btn-zapret").onclick = async () => {
    setBusy(true, "обновляю список zapret…");
    try {
      await tgCall("tg_zapret_list", !(T && T.zapret_list));
      await tgRefresh();
    } finally {
      setBusy(false, "готов");
      await refreshState();
    }
  };

  window.diaspas.on("state", (st) => {
    if (st && st.tg) renderTg(st.tg);
  });
  tgRefresh();
})();

async function warmupPrivileges() {
  // Единственный UAC за весь срок: задача планировщика регистрируется
  // при старте, а не посреди операции - иначе окно Windows легко
  // не заметить за окном Diaspas, и «ничего не произошло».
  const info = await api().prepare_privileges();
  if (!info || info.have_task) return;
  const ok = await confirmBox(info.title || "Права администратора",
                              info.body || "Подтвердить?");
  if (!ok) {
    toast(info.declined || "Отклонено: UAC потребуется при операциях", "err");
    return;
  }
  setBusy(true, "регистрирую служебную задачу…");
  try {
    const res = await api().register_task_now();
    if (res && res.registered) {
      toast("Готово: дальше операции без запросов прав", "ok");
    } else {
      toast("Не подтверждено: операции будут спрашивать права", "err");
    }
  } finally {
    setBusy(false, "готов");
    await refreshState();
  }
}
