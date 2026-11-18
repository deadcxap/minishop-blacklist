import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { test } from "node:test";
import { Window } from "happy-dom";
import { mountView, unmountView, updateView } from "../../frontend/dist/admin/index.js";

function fixture(t, respond) {
  const window = new Window({ url: "https://minishop.test/shop/admin/blacklist" });
  const globals = {
    document: window.document,
    HTMLButtonElement: window.HTMLButtonElement,
    fetch: respond,
  };
  const original = Object.fromEntries(Object.keys(globals).map((key) => [key, globalThis[key]]));
  Object.assign(globalThis, globals);
  window.document.documentElement.lang = "ru";
  window.document.cookie = "rw_webapp_csrf=fixture-token; path=/";
  const target = window.document.createElement("div");
  window.document.body.append(target);
  t.after(async () => {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
    await window.close();
  });
  return { target, event: (name) => new window.Event(name, { bubbles: true, cancelable: true }) };
}

const source = "https://list.test/blacklist.txt";
const settings = {
  sources: [source],
  refreshHours: 24,
  display: { users: true, userCard: true, ticket: true },
  chatId: null,
  topicId: null,
  events: [],
};

const sourceStatuses = [{
  url: source,
  lastSuccessAt: "2026-11-17T13:05:00+00:00",
  lastAttemptAt: "2026-11-17T13:05:00+00:00",
  lastError: null,
  count: 5,
  invalidLines: 2,
}];

test("settings keep the draft through validation failures and save all selected events", async (t) => {
  let submitted;
  let submissions = 0;
  const { target, event } = fixture(t, async (path, options) => {
    assert.equal(path, "/api/admin/minishop-blacklist/settings");
    assert.equal(options.credentials, "same-origin");
    if (options.method === "PUT") {
      assert.equal(options.headers["X-CSRF-Token"], "fixture-token");
      submitted = JSON.parse(options.body);
      if (++submissions === 1) {
        return Response.json({ ok: false, error: "chat_required" }, { status: 400 });
      }
      return Response.json({ ok: true, settings: submitted, sourceStatuses, refreshPending: false });
    }
    return Response.json({ ok: true, settings, sourceStatuses, refreshPending: false });
  });
  const translatedKeys = [];
  const instance = mountView("settings", target, {
    at: (key, _values, fallback) => { translatedKeys.push(key); return fallback; },
  });
  await setImmediate();

  assert.ok(translatedKeys.includes("minishop_blacklist_refresh_hours"));
  assert.equal(target.querySelector('[name="refreshHours"]').value, "24");
  assert.match(target.textContent, /Пропущено некорректных строк — 2/u);
  const interval = target.querySelector('[name="refreshHours"]');
  interval.value = "12";
  interval.dispatchEvent(event("input"));
  const sources = target.querySelector('[name="sources"]');
  sources.value = `${source}\n\n https://other.test/list.txt `;
  sources.dispatchEvent(event("input"));
  const all = target.querySelector('[name="allEvents"]');
  all.click();
  for (const name of ["register", "trial", "payment", "expired"]) {
    assert.equal(target.querySelector(`[name="${name}"]`).checked, true);
  }
  target.querySelector("form").dispatchEvent(event("submit"));
  await setImmediate();

  assert.deepEqual(submitted.sources, [source, "https://other.test/list.txt"]);
  assert.equal(submitted.refreshHours, 12);
  assert.match(target.querySelector('[role="alert"]').textContent, /Укажите ID чата/u);
  assert.equal(target.querySelector('[name="refreshHours"]').value, "12");
  assert.equal(target.querySelector('[name="allEvents"]').checked, true);

  const chat = target.querySelector('[name="chatId"]');
  chat.value = "-1001234567890";
  chat.dispatchEvent(event("input"));
  const topic = target.querySelector('[name="topicId"]');
  topic.value = "42";
  topic.dispatchEvent(event("input"));
  target.querySelector('[name="ticket"]').click();
  target.querySelector("form").dispatchEvent(event("submit"));
  await setImmediate();

  assert.deepEqual(submitted, {
    ...settings,
    sources: [source, "https://other.test/list.txt"],
    refreshHours: 12,
    chatId: "-1001234567890",
    topicId: "42",
    display: { users: true, userCard: true, ticket: false },
    events: ["register", "trial", "payment", "expired"],
  });
  assert.match(target.textContent, /Настройки сохранены/u);
  unmountView(instance);
  assert.equal(target.childElementCount, 0);
});

test("manual refresh uses the worker and updates statuses without replacing unsaved settings", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let refreshStarted = false;
  let statusRequests = 0;
  const { target, event } = fixture(t, async (path, options) => {
    if (options.method === "POST") {
      assert.equal(path, "/api/admin/minishop-blacklist/sources/refresh");
      assert.equal(options.headers["X-CSRF-Token"], "fixture-token");
      refreshStarted = true;
      return Response.json({ ok: true, settings, sourceStatuses, refreshPending: true });
    }
    assert.equal(path, "/api/admin/minishop-blacklist/settings");
    statusRequests += 1;
    const statuses = refreshStarted ? [{ ...sourceStatuses[0], lastError: "source_timeout" }] : sourceStatuses;
    return Response.json({ ok: true, settings, sourceStatuses: statuses, refreshPending: false });
  });

  const instance = mountView("settings", target, {});
  await setImmediate();
  const refresh = () => target.querySelector('[name="sourceRefresh"]');
  assert.equal(refresh().textContent, "Обновить списки");
  refresh().click();
  await setImmediate();
  assert.ok(refreshStarted);
  assert.equal(refresh().disabled, true);
  assert.equal(refresh().textContent, "Списки обновляются…");

  const draft = target.querySelector('[name="refreshHours"]');
  assert.equal(draft.disabled, false);
  draft.value = "6";
  draft.dispatchEvent(event("input"));
  t.mock.timers.tick(3_000);
  await setImmediate();

  assert.equal(statusRequests, 2);
  assert.equal(target.querySelector('[name="refreshHours"]'), draft);
  assert.equal(draft.value, "6");
  assert.match(target.textContent, /Обновление списков завершено/u);
  assert.match(target.textContent, /Не удалось обновить список/u);
  assert.equal(refresh().disabled, false);
  unmountView(instance);
});

test("a ticket shows every distinct comment and opens safe evidence links in new tabs", async (t) => {
  let finishOldRequest;
  let oldSignal;
  const { target } = fixture(t, async (path, options) => {
    assert.equal(path, "/api/admin/minishop-blacklist/status");
    const body = JSON.parse(options.body);
    assert.equal(body.view, "ticket");
    if (body.telegramIds[0] === "101") {
      oldSignal = options.signal;
      return new Promise((resolve) => { finishOldRequest = resolve; });
    }
    assert.deepEqual(body.telegramIds, ["102"]);
    return Response.json({ ok: true, enabled: true, statuses: {
      "102": {
        state: "match", stale: true, reasons: [
          { text: "Первый комментарий", links: ["https://evidence.test/a", "https://evidence.test/b"] },
          { text: "Другой <b>комментарий</b>", links: ["javascript:alert(1)"] },
        ],
      },
    } });
  });
  const props = (id) => ({ context: { ticket: { user: { telegram_id: id } } } });
  const instance = mountView("ticket-status", target, props(101));
  updateView(instance, props(102));
  await setImmediate();
  assert.equal(oldSignal.aborted, true);
  assert.match(target.textContent, /Первый комментарий/u);
  assert.match(target.textContent, /Другой <b>комментарий<\/b>/u);
  assert.match(target.textContent, /из предыдущей загрузки/u);
  assert.equal(target.querySelector("b"), null);
  const links = Array.from(target.querySelectorAll("a"));
  assert.deepEqual(links.map((link) => link.href), ["https://evidence.test/a", "https://evidence.test/b"]);
  assert.ok(links.every((link) => link.target === "_blank" && link.rel === "noopener noreferrer"));

  finishOldRequest(Response.json({ ok: true, enabled: true, statuses: {
    "101": { state: "match", stale: false, reasons: [{ text: "Устаревший ответ", links: [] }] },
  } }));
  await setImmediate();
  assert.doesNotMatch(target.textContent, /Устаревший ответ/u);
  unmountView(instance);
});

test("an inactive profile tab waits for selection and the API decides whether to show an unlinked account", async (t) => {
  const requests = [];
  const { target } = fixture(t, async (_, options) => {
    requests.push(JSON.parse(options.body));
    return Response.json({ ok: true, enabled: false, statuses: {} });
  });
  const instance = mountView("user-status", target, { active: false, user: { telegram_id: null } });
  await setImmediate();
  assert.equal(requests.length, 0);
  updateView(instance, { active: true, user: { telegram_id: null } });
  await setImmediate();
  assert.deepEqual(requests, [{ view: "userCard", telegramIds: [] }]);
  assert.match(target.textContent, /Показ в карточке пользователя отключён/u);
  unmountView(instance);
});

test("matched users paginate and link to the native prefixed user card", async (t) => {
  const row = (index) => ({
    userId: `ms_id${index}`,
    telegramId: String(200 + index),
    name: `Пользователь ${index}`,
    username: null,
    state: "match",
    stale: false,
    reasons: [{ text: "Причина", links: [] }],
  });
  const { target } = fixture(t, async (path) => {
    const url = new URL(path, "https://minishop.test");
    assert.equal(url.pathname, "/api/admin/minishop-blacklist/matches");
    assert.equal(url.searchParams.get("limit"), "25");
    const offset = Number(url.searchParams.get("offset"));
    const rows = offset ? [row(26)] : Array.from({ length: 25 }, (_, index) => row(index + 1));
    return Response.json({ ok: true, enabled: true, rows, total: 26 });
  });
  const instance = mountView("matched-users", target, { routePrefix: "/shop/" });
  await setImmediate();
  assert.equal(target.querySelectorAll("tbody tr").length, 25);
  assert.equal(target.querySelector("tbody a").getAttribute("href"), "/shop/admin/users/ms_id1");
  target.querySelector('[name="next"]').click();
  await setImmediate();
  assert.equal(target.querySelectorAll("tbody tr").length, 1);
  assert.match(target.textContent, /Показано 26–26 из 26/u);
  assert.equal(target.querySelector('[name="next"]').disabled, true);
  unmountView(instance);
});
