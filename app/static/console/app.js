const state = {
  apiBase: localStorage.getItem("console.apiBase") || window.location.origin,
  callId: localStorage.getItem("console.callId") || "",
  lastResponse: null,
  policy: {
    loaded: false,
    kind: localStorage.getItem("console.policyKind") || "templates",
    selectedId: "",
    templates: [],
    strategies: [],
    nodes: [],
    labels: [],
  },
};

const demoImport = {
  rows: [
    {
      case_id: "CASE_ED_001",
      respondent_name: "刘某华",
      debtor_name: "刘某华",
      debtor_phone: "13900000001",
      respondent_dir: "贵阳市观山湖区林城东路205号405室",
      plaintiff_name: "贵阳天某有限公司",
      creditor_name: "贵阳天某有限公司",
      court_name: "某某区人民法院",
      court_contact: "0851-376428",
      lawsuit_type: "买卖合同纠纷",
      claim_amount: "12500元",
      court_liantime: "2026年5月13日",
      court_CBBM: "民一庭",
      court_CBFG: "张某勤",
    },
  ],
};

function okctiSample(type) {
  const callid = state.callId || `OKCTI${Date.now()}`;
  const base = {
    callid,
    caller: "95000000",
    callee: "13900000000",
    direct: 1,
    type,
    usrtype: type === "QA" ? 2 : 0,
    usrcontent: type === "QA" ? "是我，什么事" : "",
    usrrecurl: "",
    fsx: 1,
    ch: 1,
    sysid: 1,
    taskid: "TASK_DEMO",
    calltaskid: "CASE_ED_001",
    oricaller: "",
    video: false,
    respondent_name: "刘某华",
    respondent_dir: "贵阳市观山湖区林城东路205号405室",
    plaintiff_name: "贵阳天某有限公司",
    court_name: "某某区人民法院",
    court_contact: "0851-376428",
    lawsuit_type: "买卖合同纠纷",
    claim_amount: "12500元",
  };
  if (type === "END") {
    base.talktimelong = 60;
    base.callresult = 1;
  }
  return base;
}

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

function asList(payload, key) {
  const raw = payload ? payload[key] : null;
  if (Array.isArray(raw)) return raw;
  if (raw && typeof raw === "object") return Object.values(raw);
  return [];
}

function toLines(value) {
  if (!Array.isArray(value)) return "";
  return value.filter((item) => item !== null && item !== undefined && String(item).trim()).join("\n");
}

function fromLines(value) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function fromTokens(value) {
  return value
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function nullable(value) {
  const text = String(value || "").trim();
  return text || null;
}

function numberOrNull(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

function idSuffix() {
  return new Date().toISOString().replace(/\D/g, "").slice(4, 14);
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
    const scene = health.conversation_scene === "electronic_delivery" ? "电子送达" : "调解";
    setText("#runtimeMode", `${health.offline_mode ? "offline" : "database"} · ${scene}`);
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

async function reloadKnowledgeSnapshot() {
  const data = await api("/api/v1/admin/knowledge/reload", { method: "POST", body: {} });
  if (data.version) setText("#knowledgeVersion", data.version);
  return data;
}

async function reloadKnowledge() {
  const data = await reloadKnowledgeSnapshot();
  setJson("#knowledgeOutput", data);
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

function policyId(item) {
  return state.policy.kind === "strategies" ? item.strategy_id : item.template_id;
}

function policyText(item) {
  if (state.policy.kind === "strategies") {
    return [
      item.strategy_id,
      item.strategy_name,
      item.nodes,
      item.intents,
      item.goal,
      item.instruction,
      item.allowed_actions,
      item.forbidden_actions,
      item.need_llm,
      item.risk_level,
      item.fallback_template_id,
    ].join(" ").toLowerCase();
  }
  return [
    item.template_id,
    item.node_id,
    item.strategy_id,
    item.intent_label,
    item.template_text,
    (item.variants || []).join(" "),
    item.variables,
    item.remark,
  ].join(" ").toLowerCase();
}

function csvHas(value, target) {
  if (!target) return true;
  return String(value || "")
    .split(/[,\s/，、]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .includes(target);
}

function setSelectOptions(el, rows, allText, valueOf, labelOf) {
  const previous = el.value;
  el.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = allText;
  el.appendChild(all);
  rows.forEach((row) => {
    const option = document.createElement("option");
    option.value = valueOf(row);
    option.textContent = labelOf(row);
    el.appendChild(option);
  });
  el.value = Array.from(el.options).some((option) => option.value === previous) ? previous : "";
}

function setDatalistOptions(selector, rows, valueOf) {
  const el = $(selector);
  el.innerHTML = "";
  rows.forEach((row) => {
    const option = document.createElement("option");
    option.value = valueOf(row);
    el.appendChild(option);
  });
}

function syncPolicyControls() {
  const isTemplate = state.policy.kind === "templates";
  $("#policyKind").value = state.policy.kind;
  $("#templateEditor").classList.toggle("hidden", !isTemplate);
  $("#strategyEditor").classList.toggle("hidden", isTemplate);
  $("#policyStrategyFilter").disabled = !isTemplate;
  $("#policyStatusFilter").disabled = !isTemplate;
  if (!isTemplate) {
    $("#policyStrategyFilter").value = "";
    $("#policyStatusFilter").value = "";
  }
}

function renderPolicyFilters() {
  const nodes = [...state.policy.nodes].sort((a, b) => String(a.node_id).localeCompare(String(b.node_id)));
  const strategies = [...state.policy.strategies].sort((a, b) => String(a.strategy_id).localeCompare(String(b.strategy_id)));
  const labels = [...state.policy.labels].sort((a, b) => String(a.label_id).localeCompare(String(b.label_id)));
  const templates = [...state.policy.templates].sort((a, b) => String(a.template_id).localeCompare(String(b.template_id)));

  setSelectOptions(
    $("#policyNodeFilter"),
    nodes,
    "全部节点",
    (row) => row.node_id,
    (row) => `${row.node_id} · ${row.node_name || "-"}`
  );
  setSelectOptions(
    $("#policyStrategyFilter"),
    strategies,
    "全部策略",
    (row) => row.strategy_id,
    (row) => `${row.strategy_id} · ${row.strategy_name || "-"}`
  );
  setDatalistOptions("#policyNodeOptions", nodes, (row) => row.node_id);
  setDatalistOptions("#policyStrategyOptions", strategies, (row) => row.strategy_id);
  setDatalistOptions("#policyIntentOptions", labels, (row) => row.label_id);
  setDatalistOptions("#policyTemplateOptions", templates, (row) => row.template_id);
}

function filteredPolicyItems() {
  const kind = state.policy.kind;
  const items = kind === "strategies" ? state.policy.strategies : state.policy.templates;
  const query = $("#policySearch").value.trim().toLowerCase();
  const node = $("#policyNodeFilter").value;
  const strategy = $("#policyStrategyFilter").value;
  const status = $("#policyStatusFilter").value;

  return items
    .filter((item) => !query || policyText(item).includes(query))
    .filter((item) => {
      if (!node) return true;
      return kind === "strategies" ? csvHas(item.nodes, node) : item.node_id === node;
    })
    .filter((item) => kind !== "templates" || !strategy || item.strategy_id === strategy)
    .filter((item) => {
      if (kind !== "templates" || !status) return true;
      return status === "enabled" ? item.enabled !== false : item.enabled === false;
    })
    .sort((a, b) => String(policyId(a)).localeCompare(String(policyId(b))));
}

function renderPolicyList() {
  syncPolicyControls();
  const list = $("#policyList");
  const items = filteredPolicyItems();
  list.innerHTML = "";
  setText("#policyListCount", String(items.length));
  setText("#policyListHint", state.policy.kind === "strategies" ? "条策略" : "条话术");

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "没有匹配配置";
    list.appendChild(empty);
    return;
  }

  items.forEach((item) => {
    const id = policyId(item);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `policy-list-item${id === state.policy.selectedId ? " active" : ""}`;
    btn.dataset.id = id;

    const top = document.createElement("div");
    top.className = "policy-list-top";
    const title = document.createElement("strong");
    title.textContent = state.policy.kind === "strategies" ? item.strategy_name || id : id;
    const badge = document.createElement("span");
    badge.className = "mini-badge";
    badge.textContent = state.policy.kind === "strategies"
      ? (item.need_llm || "否")
      : (item.enabled === false ? "停用" : "启用");
    top.append(title, badge);

    const meta = document.createElement("div");
    meta.className = "policy-list-meta";
    if (state.policy.kind === "strategies") {
      meta.textContent = [item.strategy_id, item.nodes, item.intents, item.risk_level].filter(Boolean).join(" · ");
    } else {
      const variants = Array.isArray(item.variants) && item.variants.length ? `${item.variants.length}变体` : "无变体";
      meta.textContent = [item.node_id, item.strategy_id, item.intent_label, variants].filter(Boolean).join(" · ");
    }

    const desc = document.createElement("div");
    desc.className = "policy-list-desc";
    desc.textContent = state.policy.kind === "strategies" ? (item.goal || item.instruction || "") : (item.template_text || "");

    btn.append(top, meta, desc);
    btn.addEventListener("click", () => selectPolicyItem(id));
    list.appendChild(btn);
  });
}

function refreshPolicyListSelection() {
  const items = filteredPolicyItems();
  if (items.length && !items.some((item) => policyId(item) === state.policy.selectedId)) {
    selectPolicyItem(policyId(items[0]));
    return;
  }
  renderPolicyList();
}

function fillTemplateForm(item, stateText = "已选择") {
  $("#templateId").value = item.template_id || "";
  $("#templateEnabled").value = item.enabled === false ? "false" : "true";
  $("#templateNodeId").value = item.node_id || "";
  $("#templateStrategyId").value = item.strategy_id || "";
  $("#templateIntent").value = item.intent_label || "";
  $("#templateComplianceLevel").value = item.compliance_level || "高";
  $("#templateText").value = item.template_text || "";
  $("#templateVariants").value = toLines(item.variants || []);
  $("#templateVariables").value = Array.isArray(item.variables) ? item.variables.join(", ") : "";
  $("#templateQualityScore").value = item.quality_score ?? "";
  $("#templateCanDirect").checked = item.can_direct !== false;
  $("#templateNeedRewrite").checked = Boolean(item.need_rewrite);
  $("#templateRemark").value = item.remark || "";
  setText("#templateDirtyState", stateText);
}

function fillStrategyForm(item, stateText = "已选择") {
  $("#strategyId").value = item.strategy_id || "";
  $("#strategyName").value = item.strategy_name || "";
  $("#strategyNodes").value = item.nodes || "";
  $("#strategyIntents").value = item.intents || "";
  $("#strategyNeedLlm").value = item.need_llm || "否";
  $("#strategyRiskLevel").value = item.risk_level || "低";
  $("#strategyGoal").value = item.goal || "";
  $("#strategyInstruction").value = item.instruction || "";
  $("#strategyAllowedActions").value = item.allowed_actions || "";
  $("#strategyForbiddenActions").value = item.forbidden_actions || "";
  $("#strategyFallbackTemplateId").value = item.fallback_template_id || "";
  setText("#strategyDirtyState", stateText);
}

function selectPolicyItem(id) {
  const items = state.policy.kind === "strategies" ? state.policy.strategies : state.policy.templates;
  const item = items.find((row) => policyId(row) === id);
  if (!item) return;
  state.policy.selectedId = id;
  if (state.policy.kind === "strategies") {
    fillStrategyForm(item);
  } else {
    fillTemplateForm(item);
  }
  renderPolicyList();
}

function newPolicyItem() {
  state.policy.selectedId = "";
  if (state.policy.kind === "strategies") {
    fillStrategyForm({
      strategy_id: `STR_CUSTOM_${idSuffix()}`,
      strategy_name: "",
      nodes: "",
      intents: "",
      goal: "",
      instruction: "",
      allowed_actions: "",
      forbidden_actions: "",
      need_llm: "可",
      risk_level: "中",
      fallback_template_id: "",
    }, "新建");
  } else {
    fillTemplateForm({
      template_id: `TPL_CUSTOM_${idSuffix()}`,
      node_id: "",
      strategy_id: "",
      intent_label: "",
      template_text: "",
      variants: [],
      variables: [],
      can_direct: true,
      need_rewrite: true,
      compliance_level: "高",
      quality_score: 90,
      enabled: true,
      remark: "",
    }, "新建");
  }
  renderPolicyList();
}

function duplicatePolicyItem(kind) {
  if (kind === "strategies") {
    const item = collectStrategyPayload();
    fillStrategyForm({
      ...item.payload,
      strategy_id: `${item.pk}_COPY_${idSuffix()}`,
    }, "新建副本");
  } else {
    const item = collectTemplatePayload();
    fillTemplateForm({
      ...item.payload,
      template_id: `${item.pk}_COPY_${idSuffix()}`,
    }, "新建副本");
  }
  state.policy.selectedId = "";
  renderPolicyList();
}

function collectTemplatePayload() {
  const pk = $("#templateId").value.trim();
  if (!pk) throw new Error("缺少模板ID");
  const text = $("#templateText").value.trim();
  if (!text) throw new Error("缺少话术文本");
  return {
    pk,
    payload: {
      node_id: nullable($("#templateNodeId").value),
      strategy_id: nullable($("#templateStrategyId").value),
      intent_label: nullable($("#templateIntent").value),
      template_text: text,
      variants: fromLines($("#templateVariants").value),
      variables: fromTokens($("#templateVariables").value),
      can_direct: $("#templateCanDirect").checked,
      need_rewrite: $("#templateNeedRewrite").checked,
      compliance_level: $("#templateComplianceLevel").value || "高",
      quality_score: numberOrNull($("#templateQualityScore").value),
      enabled: $("#templateEnabled").value === "true",
      remark: nullable($("#templateRemark").value),
    },
  };
}

function collectStrategyPayload() {
  const pk = $("#strategyId").value.trim();
  if (!pk) throw new Error("缺少策略ID");
  const name = $("#strategyName").value.trim();
  if (!name) throw new Error("缺少策略名称");
  const instruction = $("#strategyInstruction").value.trim();
  if (!instruction) throw new Error("缺少策略指令");
  return {
    pk,
    payload: {
      strategy_name: name,
      nodes: nullable($("#strategyNodes").value),
      intents: nullable($("#strategyIntents").value),
      goal: nullable($("#strategyGoal").value),
      instruction,
      allowed_actions: nullable($("#strategyAllowedActions").value),
      forbidden_actions: nullable($("#strategyForbiddenActions").value),
      need_llm: $("#strategyNeedLlm").value || "否",
      risk_level: $("#strategyRiskLevel").value || "低",
      fallback_template_id: nullable($("#strategyFallbackTemplateId").value),
    },
  };
}

async function loadPolicyConfig(options = {}) {
  setText("#policyConfigStatus", "加载中");
  const [templates, strategies, nodes, labels] = await Promise.all([
    api("/api/v1/admin/knowledge/editable/templates"),
    api("/api/v1/admin/knowledge/editable/strategies"),
    api("/api/v1/admin/knowledge/editable/nodes"),
    api("/api/v1/admin/knowledge/editable/labels"),
  ]);
  state.policy.templates = asList(templates, "templates");
  state.policy.strategies = asList(strategies, "strategies");
  state.policy.nodes = asList(nodes, "nodes");
  state.policy.labels = asList(labels, "labels");
  state.policy.loaded = true;
  renderPolicyFilters();
  syncPolicyControls();
  const selected = options.selectedId || state.policy.selectedId;
  const items = filteredPolicyItems();
  const next = selected && items.some((item) => policyId(item) === selected)
    ? selected
    : (items[0] ? policyId(items[0]) : "");
  if (next) selectPolicyItem(next);
  else {
    state.policy.selectedId = "";
    newPolicyItem();
  }
  const editable = templates.editable || strategies.editable;
  setText("#policyConfigStatus", `${editable ? "可保存" : "只读"} · ${state.policy.templates.length}话术/${state.policy.strategies.length}策略`);
}

async function saveTemplateEditor(event) {
  event.preventDefault();
  const { pk, payload } = collectTemplatePayload();
  await api(`/api/v1/admin/knowledge/templates/${encodeURIComponent(pk)}`, {
    method: "PUT",
    body: payload,
  });
  await reloadKnowledgeSnapshot();
  state.policy.selectedId = pk;
  await loadPolicyConfig({ selectedId: pk });
  toast("话术已保存并生效");
}

async function saveStrategyEditor(event) {
  event.preventDefault();
  const { pk, payload } = collectStrategyPayload();
  await api(`/api/v1/admin/knowledge/strategies/${encodeURIComponent(pk)}`, {
    method: "PUT",
    body: payload,
  });
  await reloadKnowledgeSnapshot();
  state.policy.selectedId = pk;
  await loadPolicyConfig({ selectedId: pk });
  toast("策略已保存并生效");
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

async function postOkcti(event) {
  event.preventDefault();
  const body = parseJsonInput($("#okctiJson").value, null);
  if (!body) throw new Error("缺少 OKCTI 请求 JSON");
  const data = await api("/ivr/okcti/welcome", { method: "POST", body });
  setText("#okctiOutput", data);
  if (body.callid) updateCallId(body.callid);
  toast("OKCTI SSE 已返回");
}

function switchTab(name) {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.tab === name));
  $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
  if (name === "policy" && !state.policy.loaded) {
    loadPolicyConfig().catch((err) => toast(err.message));
  }
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
  $("#policyKind").value = state.policy.kind;
  setText("#activeCall", state.callId || "-");
  $("#caseId").value = "";
  $("#inlineCase").value = pretty(demoImport.rows[0]);
  $("#caseImportJson").value = pretty(demoImport);
  $("#okctiJson").value = pretty(okctiSample("START"));

  $$(".nav-item").forEach((item) => item.addEventListener("click", () => switchTab(item.dataset.tab)));
  $("#apiBase").addEventListener("change", () => {
    state.apiBase = $("#apiBase").value.trim() || window.location.origin;
    localStorage.setItem("console.apiBase", state.apiBase);
    state.policy.loaded = false;
    refreshOverview();
  });
  $("#policyKind").addEventListener("change", () => {
    state.policy.kind = $("#policyKind").value;
    state.policy.selectedId = "";
    localStorage.setItem("console.policyKind", state.policy.kind);
    syncPolicyControls();
    refreshPolicyListSelection();
  });
  ["#policySearch", "#policyNodeFilter", "#policyStrategyFilter", "#policyStatusFilter"].forEach((selector) => {
    $(selector).addEventListener("input", refreshPolicyListSelection);
    $(selector).addEventListener("change", refreshPolicyListSelection);
  });

  $("#useDemoCase").addEventListener("click", () => {
    $("#caseId").value = "";
    $("#inlineCase").value = pretty(demoImport.rows[0]);
    $("#forceStart").checked = true;
  });
  $("#clearTimeline").addEventListener("click", () => {
    $("#timeline").innerHTML = "";
    renderLast({});
  });
  $("#copyActiveCall").addEventListener("click", () => {
    $("#queryCallId").value = state.callId || $("#callIdInput").value.trim();
  });
  $("#okctiStartSample").addEventListener("click", () => {
    $("#okctiJson").value = pretty(okctiSample("START"));
  });
  $("#okctiQaSample").addEventListener("click", () => {
    $("#okctiJson").value = pretty(okctiSample("QA"));
  });
  $("#okctiEndSample").addEventListener("click", () => {
    $("#okctiJson").value = pretty(okctiSample("END"));
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
  bindSafe("#refreshPolicyConfig", "click", () => loadPolicyConfig());
  bindSafe("#newPolicyItem", "click", newPolicyItem);
  bindSafe("#duplicateTemplate", "click", () => duplicatePolicyItem("templates"));
  bindSafe("#duplicateStrategy", "click", () => duplicatePolicyItem("strategies"));
  bindSafe("#templateEditor", "submit", saveTemplateEditor);
  bindSafe("#strategyEditor", "submit", saveStrategyEditor);
  bindSafe("#refreshDnc", "click", refreshDnc);
  bindSafe("#dncForm", "submit", submitDnc);
  bindSafe("#loadMetrics", "click", loadMetrics);
  bindSafe("#okctiForm", "submit", postOkcti);

  refreshOverview();
}

document.addEventListener("DOMContentLoaded", init);
