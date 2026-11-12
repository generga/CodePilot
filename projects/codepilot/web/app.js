const state = {
  tasks: new Map(),
  selectedTaskId: null,
  polling: null,
  pollingInFlight: false,
  pollDelayMs: 5000,
  taskListSignature: "",
  detailTaskId: null,
  detailSignatures: {},
};

const $ = (id) => document.getElementById(id);
const taskIdsKey = "codepilot-console-task-ids";

function apiKey() {
  return $("api-key").value.trim();
}

function approvalKey() {
  return $("approval-key").value.trim() || apiKey();
}

function headers(key = apiKey(), write = false) {
  const result = { "X-API-Key": key };
  if (write) result["Idempotency-Key"] = crypto.randomUUID();
  return result;
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    throw new Error(payload.detail || `HTTP ${response.status}`);
  }
  return payload;
}

function saveTaskIds() {
  sessionStorage.setItem(taskIdsKey, JSON.stringify([...state.tasks.keys()]));
}

function formatTime(value) {
  if (!value) return "--";
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function setHealth(ok, label) {
  $("health-status").dataset.state = ok ? "ok" : "error";
  $("health-label").textContent = label;
}

async function checkHealth() {
  try {
    await request("/healthz");
    setHealth(true, "API 在线");
  } catch (_) {
    setHealth(false, "API 不可用");
  }
}

function renderMetrics(metrics) {
  const tasks = metrics?.tasks || {};
  const latency = metrics?.latency_ms || {};
  const model = metrics?.model || {};
  const worker = metrics?.worker || {};
  $("metric-success").textContent = `${((tasks.success_rate || 0) * 100).toFixed(1)}%`;
  $("metric-duration").textContent = `${Math.round(latency.average_execution || 0)} ms`;
  $("metric-calls").textContent = model.calls || 0;
  $("metric-tokens").textContent = model.total_tokens || 0;
  $("metric-queue").textContent = worker.queue_depth || 0;
}

async function refreshMetrics() {
  if (!apiKey()) return;
  try {
    renderMetrics(await request("/v1/metrics?window_minutes=60", { headers: headers() }));
  } catch (_) { /* task controls remain usable when metrics are unavailable */ }
}

function statusLabel(status) {
  return {
    pending: "排队中",
    running: "执行中",
    cancelling: "取消中",
    waiting_approval: "待审批",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[status] || status || "未知";
}

function renderTaskList() {
  const list = $("task-list");
  $("task-count").textContent = state.tasks.size;
  if (!state.tasks.size) {
    list.innerHTML = '<div class="empty-state">还没有任务</div>';
    return;
  }
  list.innerHTML = [...state.tasks.values()].reverse().map((task) => `
    <button class="task-item" data-task-id="${escapeHtml(task.task_id)}" data-active="${task.task_id === state.selectedTaskId}">
      <span class="task-item-title">${escapeHtml(task.issue)}</span>
      <span class="task-item-meta"><span>${escapeHtml(statusLabel(task.status))}</span><span>${escapeHtml(formatTime(task.updated_at))}</span></span>
    </button>
  `).join("");
  list.querySelectorAll(".task-item").forEach((item) => {
    item.addEventListener("click", () => selectTask(item.dataset.taskId));
  });
}

function renderView() {
  const taskListSignature = JSON.stringify([
    state.selectedTaskId,
    ...[...state.tasks.values()].map((task) => [
      task.task_id,
      task.issue,
      task.status,
      task.updated_at,
    ]),
  ]);
  if (taskListSignature !== state.taskListSignature) {
    state.taskListSignature = taskListSignature;
    renderTaskList();
  }
  renderDetail(state.tasks.get(state.selectedTaskId));
}

function selectTask(taskId) {
  state.selectedTaskId = taskId;
  renderView();
}

function renderDetail(task) {
  if (!task) {
    state.detailTaskId = null;
    state.detailSignatures = {};
    $("empty-detail").classList.remove("hidden");
    $("task-detail").classList.add("hidden");
    return;
  }
  $("empty-detail").classList.add("hidden");
  $("task-detail").classList.remove("hidden");
  if (state.detailTaskId !== task.task_id) {
    state.detailTaskId = task.task_id;
    state.detailSignatures = {};
  }
  updateDetailSection("header", [task.task_id, task.issue, task.status, task.repo_path, task.created_by, task.retry_count, task.updated_at], () => {
    $("detail-task-id").textContent = `TASK ${task.task_id.slice(0, 10).toUpperCase()}`;
    $("detail-issue").textContent = task.issue;
    const pill = $("detail-status");
    pill.textContent = statusLabel(task.status);
    pill.dataset.status = task.status;
    $("detail-meta").innerHTML = `
      <span>仓库 <strong>${escapeHtml(task.repo_path)}</strong></span>
      <span>创建者 <strong>${escapeHtml(task.created_by)}</strong></span>
      <span>重试次数 <strong>${escapeHtml(task.retry_count ?? 0)}</strong></span>
      <span>更新时间 <strong>${escapeHtml(formatTime(task.updated_at))}</strong></span>
    `;
  });
  updateDetailSection("progress", [task.status], () => renderProgress(task));
  updateDetailSection("attempts", task.attempts || [task], () => renderAttempts(task));
  updateDetailSection("events", task.events || [], () => renderEvents(task));
  updateDetailSection("actions", [task.status, task.session_id, task.retryable], () => renderActions(task));
  updateDetailSection("result", [task.relevant_files || [], task.tests_passed, task.test_output, task.patch, task.errors || []], () => {
    const files = task.relevant_files || [];
    $("file-count").textContent = files.length;
    $("file-list").innerHTML = files.length ? files.map((file) => `<li>${escapeHtml(file)}</li>`).join("") : "<li>暂无文件</li>";
    $("test-state").textContent = task.tests_passed === true ? "通过" : task.tests_passed === false ? "未通过" : "未运行";
    $("test-state").style.color = task.tests_passed === true ? "var(--success)" : task.tests_passed === false ? "var(--danger)" : "";
    $("test-output").textContent = task.test_output || "等待测试结果";
    $("diff-output").textContent = task.patch || "暂无待审批补丁";
    const errors = task.errors || [];
    $("error-box").textContent = errors.join("\n");
    $("error-box").classList.toggle("hidden", errors.length === 0);
  });
}

function updateDetailSection(name, value, render) {
  const signature = JSON.stringify(value);
  if (state.detailSignatures[name] === signature) return;
  state.detailSignatures[name] = signature;
  render();
}

function renderProgress(task) {
  const steps = ["pending", "running", "waiting_approval", "completed"];
  const terminal = ["failed", "cancelled"].includes(task.status);
  const index = steps.indexOf(task.status);
  $("progress-track").innerHTML = steps.map((step, i) => {
    const done = !terminal && index > i;
    const active = !terminal && index === i;
    return `<span class="progress-step" data-done="${done}" data-active="${active}" title="${statusLabel(step)}"></span>`;
  }).join("");
}

function renderAttempts(task) {
  const attempts = task.attempts?.length ? task.attempts : [task];
  $("attempt-count").textContent = attempts.length;
  $("attempt-list").innerHTML = attempts.map((attempt) => `
    <button class="attempt-item" data-task-id="${escapeHtml(attempt.task_id)}" data-active="${attempt.task_id === task.task_id}">
      <span><strong>#${escapeHtml(attempt.retry_count ?? 0)}</strong> ${escapeHtml(statusLabel(attempt.status))}</span>
      <span>${escapeHtml(attempt.task_id.slice(0, 8).toUpperCase())}</span>
    </button>
  `).join("");
  $("attempt-list").querySelectorAll(".attempt-item").forEach((item) => {
    item.addEventListener("click", () => {
      const taskId = item.dataset.taskId;
      if (!state.tasks.has(taskId)) {
        const attempt = attempts.find((candidate) => candidate.task_id === taskId);
        if (attempt) state.tasks.set(taskId, attempt);
      }
      selectTask(taskId);
      if (!state.tasks.get(taskId)?.repo_path) refreshTask(taskId);
    });
  });
}

function renderEvents(task) {
  const events = task.events || [];
  const labels = {
    "task.created": "任务创建",
    "task.started": "开始执行",
    "task.waiting_approval": "等待审批",
    "approval.resolved": "审批完成",
    "task.retry.requested": "请求重试",
    "task.interrupted": "服务重启中断",
    "task.cancel.requested": "请求取消",
    "task.cancelled": "任务取消",
    "task.completed": "任务完成",
    "task.failed": "任务失败",
    "task.updated": "状态更新",
  };
  const eventList = $("event-list");
  const eventSignature = `${task.task_id}:${JSON.stringify(events)}`;
  if (eventList.dataset.signature === eventSignature) return;
  eventList.dataset.signature = eventSignature;
  $("event-count").textContent = events.length;
  eventList.innerHTML = events.length ? events.slice().reverse().map((event) => `
    <div class="event-item">
      <span class="event-marker" data-event-type="${escapeHtml(event.event_type)}"></span>
      <div class="event-content">
        <strong>${escapeHtml(labels[event.event_type] || event.event_type)}</strong>
        <span>${escapeHtml(event.actor || "system")} · ${escapeHtml(formatTime(event.created_at))}</span>
      </div>
    </div>
  `).join("") : '<div class="event-empty">暂无审计事件</div>';
}

function renderActions(task) {
  const row = $("action-row");
  const buttons = [];
  if (task.status === "waiting_approval" && task.session_id) {
    buttons.push('<button class="primary-button" data-action="approve" type="button"><span aria-hidden="true">✓</span> 批准并继续</button>');
    buttons.push('<button class="danger-button" data-action="reject" type="button"><span aria-hidden="true">×</span> 拒绝补丁</button>');
  }
  if (task.status === "failed" && task.retryable !== false) {
    buttons.push('<button class="primary-button" data-action="retry" type="button"><span aria-hidden="true">↻</span> 重新执行</button>');
  }
  if (!["completed", "failed", "cancelled"].includes(task.status)) {
    buttons.push('<button class="secondary-button" data-action="cancel" type="button"><span aria-hidden="true">■</span> 取消任务</button>');
  }
  row.innerHTML = buttons.join("");
  row.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => handleAction(button.dataset.action, task));
  });
}

async function handleAction(action, task) {
  try {
    if (action === "approve" || action === "reject") {
      const result = await request(`/v1/approval-sessions/${task.session_id}/decision`, {
        method: "POST",
        headers: { ...headers(approvalKey(), true), "Content-Type": "application/json" },
        body: JSON.stringify({ approved: action === "approve" }),
      });
      mergeTask({ ...task, status: result.status, phase: result.status, patch_applied: result.patch_applied, tests_passed: result.tests_passed, errors: result.errors, final_message: result.final_message });
    } else if (action === "cancel") {
      const result = await request(`/v1/tasks/${task.task_id}/cancel`, { method: "POST", headers: headers(apiKey(), true) });
      mergeTask(result);
    } else if (action === "retry") {
      const result = await request(`/v1/tasks/${task.task_id}/retry`, { method: "POST", headers: headers(apiKey(), true) });
      state.tasks.set(result.task_id, result);
      state.selectedTaskId = result.task_id;
      saveTaskIds();
      renderTaskList();
      renderDetail(result);
      startPolling();
      $("form-message").textContent = "重试任务已创建";
    }
  } catch (error) {
    $("form-message").textContent = error.message;
  }
}

function mergeTask(task, { render = true } = {}) {
  state.tasks.set(task.task_id, { ...(state.tasks.get(task.task_id) || {}), ...task });
  saveTaskIds();
  if (render) renderView();
}

async function refreshTask(taskId) {
  try {
    const task = await request(`/v1/tasks/${taskId}`, { headers: headers() });
    let merged = { ...(state.tasks.get(taskId) || {}), ...task };
    const [lineage, audit] = await Promise.all([
      request(`/v1/tasks/${taskId}/attempts`, { headers: headers() }).catch(() => null),
      request(`/v1/tasks/${taskId}/events?limit=200`, { headers: headers() }).catch(() => null),
    ]);
    if (lineage) merged.attempts = lineage.attempts;
    if (audit) merged.events = audit.events;
    if (task.status === "waiting_approval" && task.session_id) {
      const approval = await request(`/v1/approval-sessions/${task.session_id}`, { headers: headers() }).catch(() => null);
      if (approval) {
        merged.patch = approval.patch;
        merged.relevant_files = approval.relevant_files;
      }
    }
    mergeTask(merged, { render: false });
  } catch (error) {
    if (error.message.includes("404")) {
      state.tasks.delete(taskId);
      saveTaskIds();
    }
  }
}

async function refreshAll() {
  if (state.pollingInFlight) return;
  state.pollingInFlight = true;
  try {
    await checkHealth();
    await refreshMetrics();
    if (!apiKey()) return;
    try {
      const response = await request("/v1/tasks?limit=50", { headers: headers() });
      response.tasks.forEach((task) => state.tasks.set(task.task_id, {
        ...(state.tasks.get(task.task_id) || {}),
        ...task,
      }));
      saveTaskIds();
    } catch (error) {
      $("form-message").textContent = error.message;
    }
    const activeStatuses = new Set(["pending", "running", "cancelling", "waiting_approval"]);
    const taskIdsToRefresh = new Set(
      [...state.tasks.values()]
        .filter((task) => activeStatuses.has(task.status))
        .map((task) => task.task_id),
    );
    const selectedTask = state.tasks.get(state.selectedTaskId);
    if (selectedTask && activeStatuses.has(selectedTask.status)) {
      taskIdsToRefresh.add(selectedTask.task_id);
    }
    await Promise.all([...taskIdsToRefresh].map(refreshTask));
    renderView();
  } finally {
    state.pollingInFlight = false;
    schedulePolling();
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("form-message");
  message.textContent = "正在提交...";
  try {
    const task = await request("/v1/tasks", {
      method: "POST",
      headers: { ...headers(apiKey(), true), "Content-Type": "application/json" },
      body: JSON.stringify({ issue: $("issue").value.trim(), repo_path: $("repo-path").value.trim() }),
    });
    sessionStorage.setItem("codepilot-api-key", apiKey());
    state.tasks.set(task.task_id, task);
    state.selectedTaskId = task.task_id;
    saveTaskIds();
    renderTaskList();
    renderDetail(task);
    message.textContent = "任务已提交";
    startPolling();
  } catch (error) {
    message.textContent = error.message;
  }
});

$("refresh-button").addEventListener("click", refreshAll);
$("copy-diff").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("diff-output").textContent);
  $("form-message").textContent = "Diff 已复制";
});

const savedKey = sessionStorage.getItem("codepilot-api-key");
if (savedKey) $("api-key").value = savedKey;
try {
  const savedIds = JSON.parse(sessionStorage.getItem(taskIdsKey) || "[]");
  savedIds.forEach((id) => state.tasks.set(id, { task_id: id, issue: "加载中...", status: "pending", updated_at: null }));
} catch (_) { /* ignore malformed browser session data */ }
renderTaskList();
refreshAll();

function schedulePolling() {
  if (state.polling) window.clearTimeout(state.polling);
  state.polling = window.setTimeout(() => {
    state.polling = null;
    if (!document.hidden) refreshAll();
    else schedulePolling();
  }, state.pollDelayMs);
}

function startPolling() {
  if (state.polling || state.pollingInFlight) return;
  schedulePolling();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll();
});
startPolling();
