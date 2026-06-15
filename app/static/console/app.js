const state = {
  apiBase: localStorage.getItem("console.apiBase") || window.location.origin,
  callId: localStorage.getItem("console.callId") || "",
  lastResponse: null,
};

const demoImport = {
  rows: [
    {
      case_id: "CASE_TEST_001",
      debtor_name: "李四",
      debtor_gender: "男",
      debtor_phone: "13900000001",
      platform_name: "橘子分期",
      creditor_name: "辽宁友信资产管理",
      mediation_org: "XX民商事调解中心",
      total_amount: 9800,
      overdue_days: 42,
      official_verify_channel: "官方客服热线",
      notice_status: "已发送",
    },
  ],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function pretty(data) {
  if (typeof data === "string") return data;
  return JSON.stringify(data, null, 2);
}

function setText(selector, text) {
  const el = $(selector);
  if (el) el.textContent = text;
}

function setJson(selector, data) {
  setText(selector, pretty(data));
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function parseJsonInput(value, fallback = null) {
  const text = value.trim();
  if (!text) return fallback;
  return JSON.parse(text);
}

function endpoint(path) {
  const base = state.apiBase.endsWith("/") ? state.apiBase : `${state.apiBase}/`;
  return new URL(path.replace(/^\//, ""), base).toString();
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const init = { ...options, headers };
  if (options.body !== undefined && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(endpoint(path), init);
  const text = await res.text();
  let data = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : `${res.status} ${res.statusText}`;
    const error = new Error(detail);
    error.data = data;
    throw error;
  }
  return data;
}

function updateCallId(callId) {
  if (!callId) return;
  state.callId = callId;
  localStorage.setItem("console.callId", callId);
  $("#callIdInput").value = callId;
  $("#queryCallId").value = callId;
  setText("#activeCall", callId);
}

function renderLast(data) {
  state.lastResponse = data;
  setJson("#lastResponse", data || {});
  const total = data && data.latency_ms ? data.latency_ms.total : null;
  setText("#lastLatency", total === null || total === undefined ? "-" : `${total} ms`);
}

function addMessage(role, text, meta = "") {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.innerHTML = `
    <div class="meta">${meta || (role === "user" ? "用户" : "机器人")}</div>
    <div class="bubble"></div>
  `;
  item.querySelector(".bubble").textContent = text || "";
  $("#timeline").appendChild(item);
  $("#timeline").scrollTop = $("#timeline").scrollHeight;
}

async function refreshOverview() {
  try {
    const health = await api("/healthz");
    setText("#healthStatus", health.status || "ok");
    setText("#runtimeMode", health.offline_mode ? "offline" : "database");
    setText("#runtimeLine", `运行中 · ${state.apiBase}`);
    if (health.knowledge_version) setText("#knowledgeVersion", health.knowledge_version);
  } catch (err) {
    setText("#healthStatus", "error");
    setText("#runtimeLine", err.message);
  }

  try {
    const knowledge = await api("/api/v1/admin/knowledge/version");
    setText("#knowledgeVersion", knowledge.version || "-");
  } catch {
    // healthz is enough for the top bar when admin endpoints are unavailable.
  }
}

async function startCall(event) {
  event.preventDefault();
  const inlineCase = parseJsonInput($("#inlineCase").value, null);
  const body = {
    case_id: inlineCase ? undefined : ($("#caseId").value.trim() || undefined),
    case: inlineCase || undefined,
    call_id: $("#customCallId").value.trim() || undefined,
    force: $("#forceStart").checked,
  };
  const data = await api("/api/v1/dialog/start", { method: "POST", body });
  updateCallId(data.call_id);
  renderLast(data.opening);
  addMessage("bot", data.opening.reply, `OPENING · ${data.opening.node_after}`);
  toast("通话已开始");
}

async function sendTurn(event) {
  event.preventDefault();
  const callId = $("#callIdInput").value.trim() || state.callId;
  const text = $("#userText").value.trim();
  if (!callId) throw new Error("缺少 call_id");
  if (!text) throw new Error("缺少用户文本");
  addMessage("user", text);
  const data = await api("/api/v1/dialog/turn", {
    method: "POST",
    body: { call_id: callId, text },
  });
  updateCallId(data.call_id);
  renderLast(data);
  addMessage("bot", data.reply, `${data.action_type || "-"} · ${data.node_after || "-"}`);
  toast("已返回话术");
}

async function endCall() {
  const callId = $("#callIdInput").value.trim() || state.callId;
  if (!callId) throw new Error("缺少 call_id");
  const result = $("#endResult").value.trim();
  const data = await api(`/api/v1/calls/${encodeURIComponent(callId)}/end`, {
    method: "POST",
    body: { result: result || undefined },
  });
  renderLast(data);
  addMessage("system", `通话结束：${data.call_result || result || "正常结束"}`, "系统");
  toast("通话已结束");
}

async function loadCallState() {
  const callId = $("#callIdInput").value.trim() || state.callId;
  if (!callId) throw new Error("缺少 call_id");
  const data = await api(`/api/v1/calls/${encodeURIComponent(callId)}/state`);
  renderLast(data);
  toast("状态已加载");
}

async function lookupCase(event) {
  event.preventDefault();
  const caseId = $("#lookupCaseId").value.trim();
  if (!caseId) throw new Error("缺少案件编号");
  const data = await api(`/api/v1/cases/${encodeURIComponent(caseId)}`);
  setJson("#caseOutput", data);
  toast("案件已加载");
}

async function importCases(event) {
  event.preventDefault();
  const body = parseJsonInput($("#caseImportJson").value, null);
  if (!body) throw new Error("缺少导入 JSON");
  const data = await api("/api/v1/cases/import", { method: "POST", body });
  setJson("#caseImportOutput", data);
  toast("案件已导入");
}

async function queryCall(event) {
  event.preventDefault();
  const submitter = event.submitter;
  const kind = submitter ? submitter.dataset.kind : "transcript";
  const callId = $("#queryCallId").value.trim() || state.callId;
  if (!callId) throw new Error("缺少 call_id");
  const pathMap = {
    transcript: `/api/v1/calls/${encodeURIComponent(callId)}/transcript`,
    quality: `/api/v1/calls/${encodeURIComponent(callId)}/quality`,
    state: `/api/v1/calls/${encodeURIComponent(callId)}/state`,
  };
  const data = await api(pathMap[kind]);
  setJson("#callQueryOutput", data);
  toast("通话数据已加载");
}

async function viewKnowledge(event) {
  event.preventDefault();
  const table = $("#knowledgeTable").value;
  const data = await api(`/api/v1/admin/knowledge/${encodeURIComponent(table)}`);
  setJson("#knowledgeOutput", data);
  toast("快照已加载");
}

async function reloadKnowledge() {
  const data = await api("/api/v1/admin/knowledge/reload", { method: "POST", body: {} });
  setJson("#knowledgeOutput", data);
  if (data.version) setText("#knowledgeVersion", data.version);
  toast("知识库已热更新");
}

async function updateKnowledge(event) {
  event.preventDefault();
  const table = $("#updateTable").value;
  const pk = $("#updatePk").value.trim();
  if (!pk) throw new Error("缺少主键");
  const body = parseJsonInput($("#knowledgePayload").value, {});
  const data = await api(`/api/v1/admin/knowledge/${encodeURIComponent(table)}/${encodeURIComponent(pk)}`, {
    method: "PUT",
    body,
  });
  setJson("#knowledgeUpdateOutput", data);
  toast("知识条目已保存");
}

async function refreshDnc() {
  const data = await api("/api/v1/admin/dnc");
  setJson("#dncOutput", data);
  toast("DNC 已刷新");
}

async function submitDnc(event) {
  event.preventDefault();
  const submitter = event.submitter;
  const action = submitter ? submitter.dataset.action : "add";
  const phone = $("#dncPhone").value.trim();
  if (!phone) throw new Error("缺少手机号");
  const method = action === "remove" ? "DELETE" : "POST";
  const data = await api(`/api/v1/admin/dnc/${encodeURIComponent(phone)}`, { method });
  setJson("#dncOutput", data);
  await refreshDnc();
}

async function loadMetrics() {
  const data = await api("/metrics");
  setText("#metricsOutput", data);
  toast("指标已加载");
}

function switchTab(name) {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.tab === name));
  $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
}

function bindSafe(selector, event, handler) {
  $(selector).addEventListener(event, async (ev) => {
    try {
      await handler(ev);
    } catch (err) {
      const detail = err.data ? pretty(err.data) : err.message;
      toast(detail);
      if (selector.includes("Form") || selector.includes("Knowledge")) {
        console.error(err);
      }
    }
  });
}

function init() {
  $("#apiBase").value = state.apiBase;
  $("#callIdInput").value = state.callId;
  $("#queryCallId").value = state.callId;
  setText("#activeCall", state.callId || "-");
  $("#caseImportJson").value = pretty(demoImport);

  $$(".nav-item").forEach((item) => item.addEventListener("click", () => switchTab(item.dataset.tab)));
  $("#apiBase").addEventListener("change", () => {
    state.apiBase = $("#apiBase").value.trim() || window.location.origin;
    localStorage.setItem("console.apiBase", state.apiBase);
    refreshOverview();
  });

  $("#useDemoCase").addEventListener("click", () => {
    $("#caseId").value = "CASE20260610001";
    $("#inlineCase").value = "";
    $("#forceStart").checked = true;
  });
  $("#clearTimeline").addEventListener("click", () => {
    $("#timeline").innerHTML = "";
    renderLast({});
  });
  $("#copyActiveCall").addEventListener("click", () => {
    $("#queryCallId").value = state.callId || $("#callIdInput").value.trim();
  });

  bindSafe("#refreshAll", "click", refreshOverview);
  bindSafe("#startForm", "submit", startCall);
  bindSafe("#turnForm", "submit", sendTurn);
  bindSafe("#endCall", "click", endCall);
  bindSafe("#loadState", "click", loadCallState);
  bindSafe("#caseLookupForm", "submit", lookupCase);
  bindSafe("#caseImportForm", "submit", importCases);
  bindSafe("#callQueryForm", "submit", queryCall);
  bindSafe("#knowledgeViewForm", "submit", viewKnowledge);
  bindSafe("#reloadKnowledge", "click", reloadKnowledge);
  bindSafe("#knowledgeUpdateForm", "submit", updateKnowledge);
  bindSafe("#refreshDnc", "click", refreshDnc);
  bindSafe("#dncForm", "submit", submitDnc);
  bindSafe("#loadMetrics", "click", loadMetrics);

  refreshOverview();
}

document.addEventListener("DOMContentLoaded", init);
