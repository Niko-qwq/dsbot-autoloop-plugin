(() => {
  // ui/plan.tsx
  var phases = ["RESEARCH", "DESIGN", "IMPLEMENT", "VERIFY", "RELEASE"];
  function planSections(markdown) {
    if (!markdown) return [];
    const sections = [];
    let current = { title: null, blocks: [] };
    let block;
    for (const raw of markdown.split("\n")) {
      const line = raw.trim();
      if (!line) {
        block = void 0;
        continue;
      }
      const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) {
        if (current.blocks.length) sections.push(current);
        const documentTitle = heading[1] === "#" && !sections.length;
        current = { title: documentTitle ? null : heading[2], blocks: [] };
        block = void 0;
        continue;
      }
      const list = /^([-*+]|\d+[.)])\s+(.+)$/.exec(line);
      const kind = !list ? "p" : /^\d/.test(list[1]) ? "ol" : "ul";
      if (block?.kind !== kind) {
        block = { kind, lines: [] };
        current.blocks.push(block);
      }
      block.lines.push(list?.[2] ?? line);
    }
    if (current.blocks.length) sections.push(current);
    return sections;
  }
  function excerpt(section) {
    const first = section?.blocks[0];
    return first?.kind === "p" ? first.lines.join(" ") : first?.lines[0];
  }
  function planSummary(plan) {
    const sections = planSections(plan.goal_tree);
    const find = (pattern) => sections.find((section) => pattern.test(section.title ?? ""));
    const goal = find(/项目目标|目标|概述|愿景|goal|objective|overview/i) ?? sections[0];
    return {
      goal: excerpt(goal),
      approach: excerpt(find(/玩法|方案|策略|实现思路|approach|strategy|design/i) ?? find(/范围|scope/i)),
      delivery: excerpt(find(/交付物|交付|成果|deliverable|output/i)),
      acceptance: excerpt(find(/验收|验证标准|acceptance|validation/i))
    };
  }
  function createPlanViews(React) {
    function Inline({ text }) {
      return /* @__PURE__ */ React.createElement(React.Fragment, null, text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((part, index) => part.startsWith("`") && part.endsWith("`") && part.length > 2 ? /* @__PURE__ */ React.createElement("code", { key: index }, part.slice(1, -1)) : part.startsWith("**") && part.endsWith("**") && part.length > 4 ? /* @__PURE__ */ React.createElement("strong", { key: index }, part.slice(2, -2)) : part));
    }
    function PhasePath({ plan, t }) {
      const stages = phases.filter((phase) => plan.tasks.some((task) => task.phase === phase));
      return stages.length > 0 && /* @__PURE__ */ React.createElement("ol", { className: "autoloop-phase-path", "aria-label": t("planPhases") }, stages.map((phase) => /* @__PURE__ */ React.createElement("li", { key: phase }, t(`phase.${phase}`))));
    }
    function IssueSources({ ids, sources, t }) {
      const linked = [...new Set(ids)].filter((id) => sources.has(id));
      if (linked.length === 0) return null;
      return /* @__PURE__ */ React.createElement("details", { className: "autoloop-issue-sources", "data-source-issues": true }, /* @__PURE__ */ React.createElement("summary", null, t("sourceIssues"), linked.length > 1 && /* @__PURE__ */ React.createElement("small", null, linked.length)), /* @__PURE__ */ React.createElement("ul", null, linked.map((id) => /* @__PURE__ */ React.createElement("li", { key: id, "data-source-issue": id }, sources.get(id)))));
    }
    function PlanDetails({ plan, t }) {
      const names = new Map(plan.tasks.map((task) => [task.task_id, task.title]));
      const sources = new Map(plan.sources?.map((source) => [source.id, source.summary]));
      const groups = phases.map((phase) => ({ phase, tasks: plan.tasks.filter((task) => task.phase === phase) })).filter((group) => group.tasks.length > 0);
      const ungrouped = plan.tasks.filter((task) => !task.phase || !phases.includes(task.phase));
      const taskList = (tasks) => /* @__PURE__ */ React.createElement("ol", { className: "autoloop-plan-tasks" }, tasks.map((task) => /* @__PURE__ */ React.createElement("li", { key: task.task_id, "data-plan-task": task.task_id }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-plan-task-title" }, /* @__PURE__ */ React.createElement("strong", null, task.title), /* @__PURE__ */ React.createElement("small", null, t(`status.${task.status}`))), task.dependencies.length > 0 && /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, t("dependencies")), " ", task.dependencies.map((id) => names.get(id) ?? id).join(" \xB7 ")), task.acceptance && /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, t("acceptance")), " ", /* @__PURE__ */ React.createElement(Inline, { text: task.acceptance })), /* @__PURE__ */ React.createElement(IssueSources, { ids: task.source_issue_ids, sources, t }))));
      return /* @__PURE__ */ React.createElement("div", { className: "autoloop-plan-details" }, planSections(plan.goal_tree).map(({ title, blocks }, index) => /* @__PURE__ */ React.createElement("section", { key: index, className: "autoloop-plan-section" }, title && /* @__PURE__ */ React.createElement("h4", null, /* @__PURE__ */ React.createElement(Inline, { text: title })), blocks.map(({ kind, lines }, blockIndex) => {
        if (kind === "p") return /* @__PURE__ */ React.createElement("p", { key: blockIndex }, /* @__PURE__ */ React.createElement(Inline, { text: lines.join(" ") }));
        const List = kind;
        return /* @__PURE__ */ React.createElement(List, { key: blockIndex }, lines.map((line, lineIndex) => /* @__PURE__ */ React.createElement("li", { key: lineIndex }, /* @__PURE__ */ React.createElement(Inline, { text: line }))));
      }))), /* @__PURE__ */ React.createElement("section", { className: "autoloop-plan-section" }, /* @__PURE__ */ React.createElement("h4", null, t("planTasks")), groups.map(({ phase, tasks }) => /* @__PURE__ */ React.createElement("section", { key: phase, "data-plan-phase": phase, className: "autoloop-plan-phase" }, /* @__PURE__ */ React.createElement("h5", null, t(`phase.${phase}`)), taskList(tasks))), ungrouped.length > 0 && (groups.length > 0 ? /* @__PURE__ */ React.createElement("section", { className: "autoloop-plan-phase" }, /* @__PURE__ */ React.createElement("h5", null, t("phase.other")), taskList(ungrouped)) : taskList(ungrouped))));
    }
    function PlanRevision({ plan, t, compact = false }) {
      const revision = plan.revision;
      if (!revision) return null;
      const { count = 0 } = revision;
      return /* @__PURE__ */ React.createElement("div", { className: "autoloop-plan-revision", "data-compact": compact, "data-revision": revision.status, role: revision.status === "FAILED" ? "alert" : "status" }, /* @__PURE__ */ React.createElement("strong", null, t(`revision.${revision.status}`)), count > 1 && /* @__PURE__ */ React.createElement("small", { className: "autoloop-revision-count" }, t("revision.issues", { count })), revision.issue && /* @__PURE__ */ React.createElement("p", { className: "autoloop-revision-issue" }, revision.issue), revision.error && /* @__PURE__ */ React.createElement("p", { className: "autoloop-issue-error" }, revision.error));
    }
    return { Inline, PhasePath, IssueSources, PlanDetails, PlanRevision };
  }

  // ui/chat_cards.ts
  var API = "/api/autoloop/";
  var POLL_MS = 2e3;
  async function request(path, init = {}) {
    const response = await fetch(API + path, { cache: "no-store", ...init });
    if (!response.ok) {
      const message = response.headers.get("content-type")?.includes("application/json") ? (await response.json()).error : void 0;
      throw new Error(message || `HTTP ${response.status}`);
    }
    return await response.json();
  }
  function sendAction(action, projectId, taskId, eventId, confirmWorkerStopped = false) {
    return request("action", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Autoloop-Project": projectId,
        ...eventId ? { "X-Autoloop-Event": eventId } : {}
      },
      body: JSON.stringify({ action, task_id: taskId, ...confirmWorkerStopped ? { confirm_worker_stopped: true } : {} })
    });
  }
  function money(amount) {
    return "$" + amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  }
  function usePoll(React, path, cachedPaths = 1) {
    const [values, setValues] = React.useState(() => /* @__PURE__ */ new Map());
    const [failure, setFailure] = React.useState();
    React.useEffect(() => {
      const controller = new AbortController();
      let pending = false;
      const refresh = async () => {
        if (pending) return;
        pending = true;
        try {
          const next = await request(path, { signal: controller.signal });
          if (!controller.signal.aborted) {
            setValues((previous) => {
              const cache = new Map(previous);
              cache.delete(path);
              cache.set(path, next);
              const oldest = cache.keys().next();
              if (cache.size > cachedPaths && !oldest.done) cache.delete(oldest.value);
              return cache;
            });
            setFailure(void 0);
          }
        } catch (problem) {
          if (!controller.signal.aborted) {
            if (cachedPaths > 1) setValues((previous) => {
              if (!previous.has(path)) return previous;
              const cache = new Map(previous);
              cache.delete(path);
              return cache;
            });
            setFailure({ path, message: problem instanceof Error ? problem.message : String(problem) });
          }
        } finally {
          pending = false;
        }
      };
      void refresh();
      const timer = window.setInterval(refresh, POLL_MS);
      return () => {
        window.clearInterval(timer);
        controller.abort();
      };
    }, [path, cachedPaths]);
    return { value: values.get(path), error: failure?.path === path ? failure.message : void 0 };
  }
  function retryActionLabel(reason, t) {
    if (reason === "PM_ACCEPTANCE_FAILED") return t("verification.retry");
    if (reason === "WORKER_INTERRUPTED") return t("run.retryInterrupted");
  }
  function createActionButtons(React) {
    return function ActionButtons({ actions, taskId, t, projectId, eventId, retryLabel, confirmWorkerStopped = false, disabled = false }) {
      const [busy, setBusy] = React.useState(false);
      const [sent, setSent] = React.useState(false);
      const [error, setError] = React.useState();
      const submit = async (action) => {
        setBusy(true);
        setError(void 0);
        try {
          await sendAction(action, projectId, taskId, eventId, action === "RETRY" && confirmWorkerStopped);
          if (eventId) setSent(true);
        } catch (problem) {
          setError(problem instanceof Error ? problem.message : String(problem));
        } finally {
          setBusy(false);
        }
      };
      return React.createElement(
        "div",
        { className: "autoloop-controls", "aria-busy": busy },
        ...actions.map((action) => React.createElement("button", {
          type: "button",
          key: action,
          disabled: disabled || busy || sent,
          onClick: () => void submit(action),
          "data-autoloop-action": action
        }, action === "RETRY" && retryLabel ? retryLabel : t(`action.${action}`))),
        sent ? React.createElement("small", { role: "status" }, t("queued")) : null,
        error ? React.createElement("p", { role: "alert", className: "autoloop-error" }, error) : null
      );
    };
  }
  function createDraftRetry(React) {
    const e = React.createElement;
    function RetryButton({ draft, sessionId, t }) {
      const inFlight = React.useRef(false);
      const [status, setStatus] = React.useState("ready");
      const [error, setError] = React.useState();
      const retry = async () => {
        if (inFlight.current || status !== "ready") return;
        inFlight.current = true;
        setStatus("submitting");
        setError(void 0);
        try {
          const query = new URLSearchParams({ session_id: sessionId });
          await request("retry-draft?" + query, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: draft.project_id, attempt: draft.attempt ?? 0 })
          });
          setStatus("queued");
        } catch (problem) {
          setError(problem instanceof Error ? problem.message : String(problem));
          setStatus("ready");
        } finally {
          inFlight.current = false;
        }
      };
      return e(
        "div",
        { className: "autoloop-controls", "aria-busy": status === "submitting" },
        e("button", {
          type: "button",
          disabled: status !== "ready",
          onClick: () => void retry(),
          "data-autoloop-action": "RETRY_DRAFT"
        }, t(status === "submitting" ? "draft.retrying" : "draft.retry")),
        status === "queued" && e("small", { role: "status" }, t("draft.QUEUED")),
        error && e("p", { className: "autoloop-error", role: "alert" }, error)
      );
    }
    return function DraftRetry(props) {
      const { draft } = props;
      return draft.status === "FAILED" ? e(RetryButton, { ...props, key: `${draft.project_id}:${draft.attempt ?? 0}` }) : null;
    };
  }
  function registerChatCards(slots, React, namespace, openPanel) {
    const e = React.createElement;
    const ActionButtons = createActionButtons(React);
    const DraftRetry = createDraftRetry(React);
    const { Inline, PhasePath, PlanDetails, PlanRevision } = createPlanViews(React);
    function PlanDisclosure({ plan, t }) {
      return e(
        "details",
        { className: "autoloop-plan-disclosure" },
        e(
          "summary",
          null,
          e("span", { className: "autoloop-show-plan" }, t("openPlan")),
          e("span", { className: "autoloop-hide-plan" }, t("closePlan")),
          e("i", { "aria-hidden": "true" })
        ),
        e(PlanDetails, { plan, t })
      );
    }
    function PMActivityCard({ pm, sessionId, t }) {
      const { activity, draft, name, plan } = pm;
      const status = activity?.status ?? (draft?.status === "PLANNING" ? "RUNNING" : draft?.status) ?? (plan ? "READY" : null);
      if (!status) return null;
      const label = t(`pmActivity.${status}`);
      const readyPlan = status === "READY" ? plan : null;
      const goal = readyPlan ? planSummary(readyPlan).goal : null;
      const failure = status === "FAILED" ? activity?.error ?? draft?.error : null;
      const running = status === "RUNNING" || status === "LONG_RUNNING";
      const duration = running && activity?.started_at != null ? Math.max(0, Math.floor(Date.now() / 1e3 - activity.started_at)) : null;
      return e(
        "div",
        { className: "autoloop-tail autoloop-chat", "aria-label": t("tab") },
        e(
          "section",
          {
            className: "autoloop-chat-card autoloop-pm-chat",
            "aria-label": label,
            "data-autoloop-card": "PM_ACTIVITY",
            "data-state": status
          },
          e(
            "header",
            { className: "autoloop-pm-chat-head" },
            e("h3", null, readyPlan?.name ?? name),
            duration != null && e("small", { className: "autoloop-meta" }, t("duration", { count: duration }))
          ),
          e(
            "div",
            { role: status === "FAILED" ? "alert" : "status" },
            e("span", { className: "autoloop-chat-card-kind" }, label),
            activity?.public_text && e(
              "div",
              { className: "autoloop-pm-public-note" },
              e("small", { className: "autoloop-meta" }, t("pmActivity.note")),
              e("p", { className: "autoloop-pm-public-text" }, activity.public_text)
            ),
            failure && e("p", { className: "autoloop-error" }, failure)
          ),
          draft && e(DraftRetry, { draft, sessionId, t }),
          goal && e("p", { className: "autoloop-plan-goal" }, e(Inline, { text: goal })),
          readyPlan && e(PhasePath, { plan: readyPlan, t }),
          readyPlan && e(PlanDisclosure, { plan: readyPlan, t })
        )
      );
    }
    function PMProposalCard({ name, proposal, sessionId, t }) {
      const [phase, setPhase] = React.useState("ready");
      const [error, setError] = React.useState();
      const apply = async () => {
        if (phase !== "ready") return;
        setPhase("applying");
        setError(void 0);
        try {
          const query = new URLSearchParams({ session_id: sessionId });
          await request("pm-plan?" + query, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proposal_id: proposal.id })
          });
          setPhase("applied");
        } catch (problem) {
          setError(problem instanceof Error ? problem.message : String(problem));
          setPhase("ready");
        }
      };
      if (phase === "applied") return null;
      const titles = new Map([...proposal.added, ...proposal.updated].map((task) => [task.task_id, task.title]));
      const groups = [
        { kind: "added", tasks: proposal.added },
        { kind: "updated", tasks: proposal.updated },
        { kind: "removed", tasks: proposal.removed }
      ];
      const label = t("pmProposal.title");
      const tier = (value) => value === 1 ? t("readOnly") : value === 2 ? t("write") : `Tier ${value}`;
      return e(
        "div",
        { className: "autoloop-tail autoloop-chat" },
        e(
          "section",
          {
            className: "autoloop-chat-card autoloop-pm-chat autoloop-pm-proposal",
            "aria-label": label,
            "aria-busy": phase === "applying",
            "data-autoloop-card": "PM_PROPOSAL"
          },
          e(
            "header",
            { className: "autoloop-pm-chat-head" },
            e("h3", null, name),
            e("span", { className: "autoloop-meta" }, label)
          ),
          e("p", null, proposal.summary),
          ...groups.filter((group) => group.tasks.length).map(({ kind, tasks }) => e(
            "section",
            { className: "autoloop-pm-proposal-group", key: kind },
            e("h4", null, t("pmProposal." + kind)),
            e("ul", null, ...tasks.map((task) => e(
              "li",
              { key: task.task_id },
              e("span", null, task.title),
              "acceptance" in task && task.acceptance && e(
                "p",
                { className: "autoloop-meta" },
                t("pmProposal.acceptance", { criterion: task.acceptance })
              ),
              "previous_permission_tier" in task && task.previous_permission_tier && task.permission_tier && e(
                "p",
                { className: "autoloop-meta" },
                t("pmProposal.permission", { previous: tier(task.previous_permission_tier), next: tier(task.permission_tier) })
              ),
              "replaced_by" in task && task.replaced_by && e(
                "p",
                { className: "autoloop-meta" },
                t("pmProposal.replacedBy", { task: titles.get(task.replaced_by) ?? task.replaced_by })
              )
            )))
          )),
          e(
            "footer",
            { className: "autoloop-controls" },
            e(
              "button",
              { type: "button", disabled: phase === "applying", onClick: () => void apply() },
              t(phase === "applying" ? "pmProposal.applying" : "pmProposal.apply")
            ),
            error && e("p", { className: "autoloop-error", role: "alert" }, error)
          )
        )
      );
    }
    function PlanCard({ plan, budget, queued, t }) {
      const { goal, approach, delivery, acceptance } = planSummary(plan);
      const facts = [["planApproach", approach], ["artifacts", delivery], ["acceptance", acceptance]];
      const preview = plan.tasks.slice(0, 3);
      const revisionPending = plan.revision?.status === "QUEUED" || plan.revision?.status === "UPDATING";
      const heading = t(revisionPending ? "previousPlan" : "startCard");
      return e(
        "section",
        { className: "autoloop-review-card", "aria-label": heading, "data-autoloop-card": "PLAN" },
        e(
          "header",
          { className: "autoloop-review-heading" },
          e("p", { className: "autoloop-review-label" }, heading),
          e("h3", null, plan.name),
          goal && e("p", { className: "autoloop-plan-goal" }, e(Inline, { text: goal })),
          e(PhasePath, { plan, t })
        ),
        e(PlanRevision, { plan, t }),
        e(
          "div",
          { className: "autoloop-plan-preview" },
          facts.some(([, value]) => Boolean(value)) && e(
            "dl",
            { className: "autoloop-plan-facts" },
            ...facts.map(([key, value]) => value && e("div", { key }, e("dt", null, t(key)), e("dd", null, e(Inline, { text: value }))))
          ),
          e("h4", null, t("planTasks")),
          e("ol", { className: "autoloop-preview-tasks" }, ...preview.map((task) => e("li", { key: task.task_id }, task.title)))
        ),
        e(PlanDisclosure, { plan, t }),
        e(
          "footer",
          { className: "autoloop-review-footer" },
          e("span", null, t("startDetails", { count: plan.tasks.length, budget: money(budget) })),
          queued ? e("small", { role: "status" }, t("queued")) : plan.approval_token && e(ActionButtons, {
            key: plan.approval_token,
            actions: ["START_PROJECT"],
            eventId: plan.approval_token,
            taskId: null,
            projectId: plan.id,
            t
          })
        )
      );
    }
    function Cards({ sessionId, t, mode }) {
      const query = new URLSearchParams({ session_id: sessionId });
      const { value, error } = usePoll(React, "outbox?" + query);
      if (mode === "pm") {
        if (error || value?.role !== "pm" || !value.pm) return null;
        const { proposal, name } = value.pm;
        return proposal ? e(PMProposalCard, { key: proposal.id, proposal, name, sessionId, t }) : e(PMActivityCard, { pm: value.pm, sessionId, t });
      }
      if (value?.role === "pm") return null;
      if (value?.draft) {
        const draft = value.draft;
        const failed = draft.status === "FAILED";
        const label = t(`draft.${draft.status}`);
        return e(
          "div",
          { className: "autoloop-tail autoloop-chat", "aria-label": t("tab") },
          e(
            "section",
            {
              className: "autoloop-chat-card",
              "aria-label": label,
              role: failed ? "alert" : "status",
              "data-autoloop-card": "DRAFT",
              "data-state": draft.status
            },
            e("span", { className: "autoloop-chat-card-kind" }, label),
            failed && draft.error && e("p", { className: "autoloop-error" }, draft.error),
            e(DraftRetry, { draft, sessionId, t })
          )
        );
      }
      if (!value?.active) return error ? e("p", { role: "alert", className: "autoloop-error" }, error) : null;
      const { snapshot, queued, plan } = value;
      const projectStatus = snapshot.dag.project_status;
      return e(
        "div",
        { className: "autoloop-tail autoloop-chat", "aria-label": t("tab") },
        projectStatus === "IDLE" && plan ? e(PlanCard, { plan, budget: snapshot.budget.max_total_cost_usd, queued: queued.includes(null), t }) : null,
        projectStatus === "COMPLETED" || projectStatus === "IDLE" ? null : e(StatusStrip, { snapshot, t })
      );
    }
    function StatusStrip({ snapshot, t }) {
      const { dag, active_workers } = snapshot;
      const done = dag.tasks.filter((task) => task.status === "COMPLETED").length;
      const attention = dag.tasks.filter((task) => task.status === "FAILED").length;
      return e(
        "div",
        { className: "autoloop-strip", "data-state": dag.project_status, "aria-label": t("tab") },
        attention > 0 ? e("span", { className: "autoloop-strip-alert", title: t("attention") }, "!") : e("span", { className: "autoloop-strip-dot" }),
        e("span", { className: "autoloop-strip-status" }, t(`status.${dag.project_status}`)),
        e("span", { className: "autoloop-strip-meta" }, t("stripTasks", { done, total: dag.tasks.length })),
        active_workers > 0 ? e("span", { className: "autoloop-strip-meta" }, t("stripWorkers", { count: active_workers })) : null,
        e("button", { type: "button", className: "autoloop-strip-open", onClick: openPanel, "data-autoloop-open-panel": true }, t("openPanel"))
      );
    }
    slots.inject("conversation.chat.turnTail", () => slots.register({
      name: "conversation.chat.turnTail",
      id: "autoloop-cards",
      order: 700,
      locale: namespace,
      inject: (sessionId) => ({ sessionId })
    }, ({ turn, useProjection, sessionId, t }) => {
      const anchorTurn = useProjection("autoloopTurn");
      const latestTurn = useProjection("autoloopLatestTurn");
      const mode = anchorTurn ? "chief" : "pm";
      if ((mode === "chief" ? anchorTurn : latestTurn) !== turn.turn) return null;
      return e(Cards, { key: sessionId, mode, t, sessionId });
    }));
  }

  // ui/notices.ts
  function previewHref(value) {
    if (!value) return void 0;
    try {
      const url = new URL(value);
      if (url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) && !url.username && !url.password && url.origin !== window.location.origin) return url.href;
    } catch {
    }
  }
  function registerNotices(slots, React, namespace) {
    let notices = [];
    const listeners = /* @__PURE__ */ new Set();
    const subscribe = (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    };
    const snapshot = () => notices;
    const e = React.createElement;
    const ActionButtons = createActionButtons(React);
    function connect() {
      const stream = new EventSource("/api/autoloop/notices");
      stream.onmessage = (event) => {
        notices = JSON.parse(event.data);
        for (const listener of listeners) listener();
      };
      return () => stream.close();
    }
    function WebsiteLink({ href, t }) {
      return e(
        "a",
        { className: "autoloop-preview-link", href, target: "_blank", rel: "noopener noreferrer" },
        t("notice.openPreview"),
        e("span", { "aria-hidden": true }, "\u2197")
      );
    }
    function Evidence({ report, t }) {
      return e(
        "div",
        { className: "autoloop-delivery-evidence" },
        !!report.acceptance_checks?.length && e("ul", null, ...report.acceptance_checks.map((check) => e(
          "li",
          { key: check.criterion_id },
          check.description,
          " \xB7 ",
          t(check.verified ? "notice.checkVerified" : "notice.checkPending")
        ))),
        report.outcomes.length > 0 && e("ul", null, ...report.outcomes.map((outcome, index) => e("li", { key: index }, e("strong", null, outcome.title), outcome.summary && e("p", null, outcome.summary)))),
        report.artifacts.length > 0 && e(
          "section",
          null,
          e("h4", null, t("artifacts")),
          e("ul", null, ...report.artifacts.map((artifact) => e("li", { key: artifact.path }, e("code", { title: artifact.label }, artifact.path))))
        )
      );
    }
    function NoticeDetails({ notice, t }) {
      const completion = notice.type === "PROJECT_COMPLETED";
      const heading = completion ? t("notice.showEvidence") : t("notice." + notice.type);
      const href = previewHref(notice.preview_url);
      return e(
        "details",
        { className: "autoloop-notice-details", "data-autoloop-card": notice.type },
        e("summary", null, heading),
        e(
          "div",
          null,
          notice.task && e("strong", null, notice.task),
          !completion && notice.summary && e("p", null, notice.summary),
          notice.report && e(Evidence, { report: notice.report, t }),
          href && e(WebsiteLink, { href, t }),
          notice.receipt ? e("small", { role: "status" }, t("receipt." + notice.receipt.action)) : !!notice.actions?.length && e(ActionButtons, {
            key: notice.id,
            actions: notice.actions,
            taskId: notice.task_id ?? null,
            projectId: notice.project_id,
            eventId: notice.id,
            t,
            retryLabel: retryActionLabel(notice.reason_code, t),
            confirmWorkerStopped: notice.confirm_worker_stopped
          })
        )
      );
    }
    function useProjectNotices(session) {
      return React.useSyncExternalStore(subscribe, snapshot).filter((notice) => notice.session_id === session || notice.current_session_id === session);
    }
    slots.inject("conversation.chat.turnTail", () => slots.register({
      name: "conversation.chat.turnTail",
      id: "autoloop-chat-notices",
      order: 695,
      locale: namespace,
      inject: (sessionId) => ({ sessionId })
    }, ({ turn, sessionId, t }) => {
      const entries = useProjectNotices(sessionId).filter((notice) => notice.session_id === sessionId && notice.turn === turn.turn);
      if (!entries.length) return null;
      return e("div", { className: "autoloop-chat" }, ...entries.map((notice) => e(NoticeDetails, { key: notice.id, notice, t })));
    }));
    return { NoticeDetails, WebsiteLink, useProjectNotices, connect };
  }

  // ui/project_pm.ts
  var TEMPORARY_PREFIX = "[Autoloop temporary name: ";
  var RESERVED_NAMES = /* @__PURE__ */ new Set(["all", "everyone", "\u6240\u6709\u4EBA", "\u5168\u4F53", "user", "you"]);
  var normalize = (name) => name.trim().toLowerCase();
  function clip(name, length) {
    return name.slice(0, length).replace(/[\ud800-\udbff]$/, "").trimEnd();
  }
  function displayText(value) {
    return (value ?? "").replace(/[@`\s\u0000-\u001f\u007f]+/g, " ").trim().replace(/^["'“”「『《]+|["'“”」』》]+$/g, "").trim();
  }
  function freeName(base, title, roster, current) {
    const taken = new Set(roster.filter((bot) => bot.id !== current?.id).map((bot) => normalize(bot.name)));
    if (!taken.has(normalize(base))) return base;
    const qualified = `${clip(base, 16)} \xB7 ${clip(title, 20)}`;
    if (title && !taken.has(normalize(qualified))) return qualified;
    let number = 2;
    while (taken.has(normalize(`${base} ${number}`))) number += 1;
    return `${base} ${number}`;
  }
  async function botRequest(endpoint, payload = {}, signal) {
    const response = await fetch("/api/bot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint, payload }),
      signal
    });
    if (!response.ok) throw new Error("DS Bot HTTP " + response.status);
    const result = await response.json();
    if (!result.ok) throw new Error(result.error.message);
    return result.value;
  }
  async function syncProjectPMs(targets, names, signal) {
    const locks = navigator.locks;
    const sync = async () => {
      const { bots } = await botRequest("state", {}, signal);
      for (const target of targets) {
        signal.throwIfAborted();
        const marker = `[Autoloop PM project:${target.id}]`;
        const bot = bots.find((item) => item.instructions?.split("\n", 1)[0] === marker);
        const title = target.title !== target.id ? displayText(target.title) : "";
        const proposed = clip(displayText(target.nickname), 30);
        const nickname = RESERVED_NAMES.has(normalize(proposed)) ? "" : proposed;
        const pending = target.title === void 0 && !nickname;
        const temporary = (name2) => `${TEMPORARY_PREFIX}${name2}]`;
        if (bot) {
          const generated = bot.instructions?.split("\n", 2)[1] === temporary(bot.name);
          if (pending || !generated) continue;
        } else if (!target.create || pending && !locks) continue;
        const base = nickname || (title ? `${clip(title, 28)} PM` : pending ? names.pending : names.project);
        const name = freeName(base, nickname ? title : "", bots, bot);
        if (bot) {
          await botRequest("update-bot", { id: bot.id, name });
          bot.name = name;
        } else {
          const created = await botRequest("create-bot", {
            name,
            role: "\u9879\u76EE\u4E3B\u7BA1",
            avatar: { shape: "cat" },
            brief: "\u8BF7\u7B80\u77ED\u4ECB\u7ECD\u4F60\u53EF\u4EE5\u56DE\u7B54\u672C\u9879\u76EE\u7684\u8FDB\u5C55\uFF0C\u5E76\u548C\u7528\u6237\u8BA8\u8BBA\u65B9\u6848\u53CA\u9A8C\u6536\u3002",
            instructions: marker + (pending ? `
${temporary(name)}` : "") + "\n\u4F60\u662F\u5F53\u524D Autoloop \u9879\u76EE\u7684\u4E3B\u7BA1\u3002\u7528 autoloop_status \u6838\u5BF9\u9879\u76EE\u548C\u4EFB\u52A1\uFF0C\u5728\u5BF9\u8BDD\u4E2D\u8BA8\u8BBA\u76EE\u6807\u3001\u65B9\u6848\u548C\u9A8C\u6536\u3002\u7528\u6237\u9009\u5B9A\u65B9\u5411\u540E\uFF0C\u63D0\u4EA4\u53EF\u786E\u8BA4\u7684\u6700\u5C0F\u8BA1\u5212\u53D8\u66F4\u3002"
          });
          bots.push(created);
        }
      }
    };
    return locks ? locks.request("autoloop-native-pm-roster", { signal }, sync) : sync();
  }

  // ui/managed_product.tsx
  function githubLink(value) {
    if (!value) return void 0;
    try {
      const url = new URL(value);
      if (url.protocol === "https:" && url.hostname === "github.com" && !url.username && !url.password) return url.href;
    } catch {
    }
  }
  function productPath(session, product, version) {
    const query = new URLSearchParams({ session_id: session });
    if (product) query.set("repository_id", String(product.repository_id));
    if (version) query.set("version", version);
    return `product?${query}`;
  }
  function createManagedProductPanel(React) {
    function ChiefSetup({ session, bots, label, submit, busy }) {
      const setupPath = `github-setup?${new URLSearchParams({ session_id: session })}`;
      const { value: github, error: loadError } = usePoll(React, setupPath);
      const [registration, setRegistration] = React.useState();
      const [error, setError] = React.useState();
      const install = githubLink(github?.installUrl);
      const register = githubLink(registration?.registrationUrl);
      async function connect(event) {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        setError(void 0);
        try {
          setRegistration(await request(setupPath, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ organization: String(data.get("organization") ?? "").trim(), allowRepositoryCreation: data.has("allow_create") })
          }));
        } catch (problem) {
          setError(problem instanceof Error ? problem.message : String(problem));
        }
      }
      function attach(event) {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        const read = (name) => String(data.get(name) ?? "").trim();
        void submit({
          operation: "attach",
          owner: read("owner"),
          name: read("name"),
          title: read("title"),
          pm: read("pm"),
          usage: read("usage"),
          budget: Number(data.get("budget")),
          rollback: data.has("rollback"),
          confirmed: data.has("confirmed"),
          create: data.has("create"),
          account_type: data.has("organization_account") ? "organization" : "personal"
        });
      }
      return /* @__PURE__ */ React.createElement("details", { className: "autoloop-project autoloop-managed-setup" }, /* @__PURE__ */ React.createElement("summary", null, label("GitHub \u4E0E\u4EA7\u54C1\u63A5\u5165", "GitHub and product setup")), github?.connected ? /* @__PURE__ */ React.createElement("p", null, label("GitHub App \u5DF2\u6CE8\u518C", "GitHub App registered"), github.slug ? ` \xB7 ${github.slug}` : "", install && /* @__PURE__ */ React.createElement(React.Fragment, null, " \xB7 ", /* @__PURE__ */ React.createElement("a", { href: install, target: "_blank", rel: "noreferrer" }, label("\u5B89\u88C5\u5230\u4EA7\u54C1\u4ED3\u5E93", "Install on product repositories")))) : register ? /* @__PURE__ */ React.createElement("form", { action: register, method: "post", target: "_blank", rel: "noreferrer" }, /* @__PURE__ */ React.createElement("input", { type: "hidden", name: "manifest", value: JSON.stringify(registration?.manifest) }), /* @__PURE__ */ React.createElement("button", { type: "submit" }, label("\u5728 GitHub \u521B\u5EFA App", "Create the GitHub App"))) : /* @__PURE__ */ React.createElement("form", { onSubmit: connect }, /* @__PURE__ */ React.createElement("label", null, label("GitHub \u7EC4\u7EC7\uFF08\u53EF\u7559\u7A7A\uFF09", "GitHub organization (optional)"), /* @__PURE__ */ React.createElement("input", { name: "organization" })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("input", { type: "checkbox", name: "allow_create" }), " ", label("\u6253\u5F00\u5EFA\u5E93\u6743\u9650", "Enable repository provisioning")), /* @__PURE__ */ React.createElement("button", { type: "submit" }, label("\u51C6\u5907 GitHub App", "Prepare GitHub App"))), (error || loadError) && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error", role: "alert" }, error ?? loadError), /* @__PURE__ */ React.createElement("form", { onSubmit: attach }, /* @__PURE__ */ React.createElement("label", null, label("\u4EA7\u54C1\u540D\u79F0", "Product name"), /* @__PURE__ */ React.createElement("input", { name: "title", required: true, maxLength: 160 })), /* @__PURE__ */ React.createElement("label", null, label("\u4ED3\u5E93\u6240\u6709\u8005", "Repository owner"), /* @__PURE__ */ React.createElement("input", { name: "owner", required: true })), /* @__PURE__ */ React.createElement("label", null, label("\u4ED3\u5E93\u540D\u79F0", "Repository name"), /* @__PURE__ */ React.createElement("input", { name: "name", required: true })), ["pm", "usage"].map((role) => /* @__PURE__ */ React.createElement("label", { key: role }, role === "pm" ? "PM Bot" : label("\u4F7F\u7528 Bot", "Usage Bot"), /* @__PURE__ */ React.createElement("select", { name: role, required: true, defaultValue: "" }, /* @__PURE__ */ React.createElement("option", { value: "", disabled: true }, label("\u9009\u62E9 Bot", "Choose a Bot")), bots.map((bot) => /* @__PURE__ */ React.createElement("option", { key: bot.id, value: bot.id }, bot.name))))), /* @__PURE__ */ React.createElement("label", null, label("\u7248\u672C\u9884\u7B97\uFF08\u7F8E\u5143\uFF09", "Version budget (USD)"), /* @__PURE__ */ React.createElement("input", { name: "budget", type: "number", min: "0.01", max: "10000", step: "0.01", defaultValue: "10", required: true })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("input", { name: "create", type: "checkbox" }), " ", label("\u521B\u5EFA\u65B0\u79C1\u6709\u4ED3\u5E93", "Create a private repository")), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("input", { name: "organization_account", type: "checkbox" }), " ", label("\u4F7F\u7528\u7EC4\u7EC7\u8D26\u53F7\u5EFA\u5E93", "Create under an organization")), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("input", { name: "rollback", type: "checkbox" }), " ", label("\u6388\u6743\u56DE\u9000\u5DF2\u9A8C\u8BC1\u7684\u5DE5\u4F5C\u6D41", "Authorize rollback to a verified workflow")), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("input", { name: "confirmed", type: "checkbox", required: true }), " ", label("\u786E\u8BA4\u89D2\u8272\u3001\u9884\u7B97\u548C\u672C\u673A\u53D1\u5E03\u8303\u56F4", "Confirm roles, budget and local deployment")), /* @__PURE__ */ React.createElement("button", { type: "submit", disabled: busy || bots.length < 2 }, label("\u7ED1\u5B9A\u4EA7\u54C1", "Bind product"))));
    }
    function WorkflowPlan({ product, version, session, label, submit, busy }) {
      const { value, error } = usePoll(React, productPath(session, product, version.version));
      const plan = value?.plan;
      return /* @__PURE__ */ React.createElement("details", { className: "autoloop-project", open: true }, /* @__PURE__ */ React.createElement("summary", null, label("\u7248\u672C\u8BA1\u5212", "Version plan"), " \xB7 ", version.version, " ", plan?.status && `\xB7 ${plan.status}`), (error || plan?.error) && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error", role: "alert" }, error ?? plan?.error), !plan?.tasks?.length && /* @__PURE__ */ React.createElement("p", { className: "autoloop-meta" }, plan?.draft_status ?? label("\u7B49\u5F85 PM \u62DF\u5B9A\u8BA1\u5212", "Waiting for the PM plan")), !!plan?.tasks?.length && /* @__PURE__ */ React.createElement("ul", null, plan.tasks.map((task) => /* @__PURE__ */ React.createElement("li", { key: task.task_id }, task.title, " \xB7 ", task.status))), plan?.status === "IDLE" && !!plan.tasks?.length && /* @__PURE__ */ React.createElement(
        "button",
        {
          type: "button",
          disabled: busy,
          onClick: () => void submit({ operation: "start", repository_id: product.repository_id, version: version.version })
        },
        label("\u786E\u8BA4\u542F\u52A8\u7248\u672C", "Confirm and start version")
      ));
    }
    function ProductHistory({ product, canReview, label, submit, busy }) {
      const trials = product.trial_history ?? (product.trials ? [product.trials] : []);
      const operations = Object.values(product.delivery.completed);
      const repository = repositoryLink(product);
      return /* @__PURE__ */ React.createElement(React.Fragment, null, !!trials.length && /* @__PURE__ */ React.createElement("details", { className: "autoloop-project", open: true }, /* @__PURE__ */ React.createElement("summary", null, label("\u5019\u9009\u8BD5\u7528", "Candidate trials")), trials.map((trial) => /* @__PURE__ */ React.createElement("div", { key: trial.build.id }, /* @__PURE__ */ React.createElement("p", null, trial.build.version, " \xB7 ", trial.verified_count, "/", trial.required_count, " \xB7 ", trial.status, /* @__PURE__ */ React.createElement("code", { title: trial.build.artifact_sha256 }, " \xB7 ", trial.build.artifact_sha256.slice(0, 12))), trial.runs.map((run) => /* @__PURE__ */ React.createElement("div", { className: "autoloop-project-actions", key: run.id }, /* @__PURE__ */ React.createElement("span", null, run.id.slice(0, 8), " \xB7 ", run.verification?.verdict ?? label("\u5F85\u6838\u9A8C", "Pending review")), run.open_url && /* @__PURE__ */ React.createElement("a", { href: run.open_url, target: "_blank", rel: "noreferrer" }, label("\u6838\u5BF9\u4EA7\u7269", "Review tool")), canReview && trial.status === "active" && !run.verification && ["pass", "fail"].map((verdict) => /* @__PURE__ */ React.createElement("button", { key: verdict, type: "button", disabled: busy, onClick: () => void submit({
        operation: "accept_trial",
        repository_id: product.repository_id,
        build_id: trial.build.id,
        run_id: run.id,
        verdict
      }) }, verdict === "pass" ? label("\u786E\u8BA4\u91C7\u7EB3", "Accept") : label("\u9000\u56DE", "Reject")))))))), /* @__PURE__ */ React.createElement("details", { className: "autoloop-project", open: true }, /* @__PURE__ */ React.createElement("summary", null, label("GitHub Issue \u6536\u4EF6\u7BB1", "GitHub issue inbox")), !product.issues.length && /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, label("\u6682\u65E0 Issue", "No issues")), product.issues.map((issue) => /* @__PURE__ */ React.createElement("details", { className: "autoloop-managed-issue", key: issue.number }, /* @__PURE__ */ React.createElement("summary", null, "#", issue.number, " \xB7 ", issue.title, " ", /* @__PURE__ */ React.createElement("small", null, issue.triage, " \xB7 ", issue.state)), /* @__PURE__ */ React.createElement("a", { href: `${repository}/issues/${issue.number}`, target: "_blank", rel: "noreferrer" }, label("\u5728 GitHub \u67E5\u770B", "Open in GitHub")), product.versions.filter((version) => version.issue_numbers.includes(issue.number)).map((version) => /* @__PURE__ */ React.createElement("p", { key: version.version }, version.version, " \xB7 ", version.released ? label("\u66FE\u53D1\u5E03", "Released") : label("\u5F00\u53D1\u4E2D", "In development"), version.pull_request?.number && /* @__PURE__ */ React.createElement(React.Fragment, null, " \xB7 ", /* @__PURE__ */ React.createElement("a", { href: `${repository}/pull/${version.pull_request.number}`, target: "_blank", rel: "noreferrer" }, "PR")))), trials.filter((trial) => trial.build.issue_numbers.includes(issue.number)).map((trial) => /* @__PURE__ */ React.createElement("p", { key: trial.build.id }, label("\u8BD5\u7528", "Trials"), " \xB7 ", trial.build.version, " \xB7 ", trial.verified_count, "/", trial.required_count)), operations.filter((item) => item.build.issue_numbers.includes(issue.number) || item.expected?.issue_numbers.includes(issue.number)).map((item) => /* @__PURE__ */ React.createElement("p", { key: item.request.id }, item.request.kind, " \xB7 ", item.build.version))))), !!operations.length && /* @__PURE__ */ React.createElement("details", { className: "autoloop-project" }, /* @__PURE__ */ React.createElement("summary", null, label("\u53D1\u5E03\u4E0E\u56DE\u9000\u8BB0\u5F55", "Release and rollback history")), operations.slice().reverse().map((item) => /* @__PURE__ */ React.createElement("p", { key: item.request.id }, item.request.kind, " \xB7 ", /* @__PURE__ */ React.createElement(
        "a",
        {
          href: `${repository}/releases/tag/${encodeURIComponent(item.build.version)}`,
          target: "_blank",
          rel: "noreferrer"
        },
        item.build.version
      )))), !!product.recent_runs.length && /* @__PURE__ */ React.createElement("details", { className: "autoloop-project", open: true }, /* @__PURE__ */ React.createElement("summary", null, label("\u8FD0\u884C\u4E0E\u751F\u6210\u5236\u54C1", "Workflow runs and artifacts")), product.recent_runs.slice().reverse().map((run) => /* @__PURE__ */ React.createElement("div", { key: run.id }, /* @__PURE__ */ React.createElement("p", null, run.build.version, " \xB7 ", run.id.slice(0, 8), " \xB7 ", run.status), run.open_url && /* @__PURE__ */ React.createElement("p", { className: "autoloop-project-actions" }, /* @__PURE__ */ React.createElement("a", { href: run.open_url, target: "_blank", rel: "noreferrer" }, label("\u6253\u5F00\u5C0F\u5DE5\u5177", "Open tool")), /* @__PURE__ */ React.createElement("a", { href: run.download_url }, label("\u7F51\u9875 ZIP", "Web ZIP")), run.miniapp_url && /* @__PURE__ */ React.createElement("a", { href: run.miniapp_url }, label("\u5C0F\u7EA2\u4E66\u5C0F\u7A0B\u5E8F ZIP", "Xiaohongshu Mini App ZIP"))), run.error && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error" }, run.error)))));
    }
    return function ManagedProducts({ session, chinese }) {
      const label = (zh, en) => chinese ? zh : en;
      const path = productPath(session);
      const { value, error } = usePoll(React, path);
      const [selected, setSelected] = React.useState();
      const [busy, setBusy] = React.useState(false);
      const [errorAction, setErrorAction] = React.useState();
      const product = value?.products.find((item) => item.repository_id === selected) ?? value?.products[0];
      const actor = value?.actor;
      const canReview = product && [product.roles.pm, product.roles.chief].includes(actor?.id ?? "");
      const target = product?.delivery.current["ds-bot-local"];
      const version = product?.versions.findLast((item) => !item.released);
      async function submit(body) {
        setBusy(true);
        setErrorAction(void 0);
        try {
          await request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        } catch (problem) {
          setErrorAction(problem instanceof Error ? problem.message : String(problem));
        } finally {
          setBusy(false);
        }
      }
      return /* @__PURE__ */ React.createElement("section", { className: "autoloop-scroll autoloop-catalog autoloop-managed", "aria-label": label("\u4EA7\u54C1", "Products") }, (error || errorAction) && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error", role: "alert" }, errorAction ?? error), !value && !error && /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, label("\u6B63\u5728\u8BFB\u53D6\u4EA7\u54C1\u2026", "Loading products\u2026")), product && value && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("label", { className: "autoloop-managed-select" }, label("\u4EA7\u54C1", "Product"), /* @__PURE__ */ React.createElement("select", { value: product.repository_id, onChange: (event) => setSelected(Number(event.currentTarget.value)) }, value.products.map((item) => /* @__PURE__ */ React.createElement("option", { key: item.repository_id, value: item.repository_id }, item.title)))), /* @__PURE__ */ React.createElement("article", { className: "autoloop-project" }, /* @__PURE__ */ React.createElement("h3", null, product.title), /* @__PURE__ */ React.createElement("p", null, product.repository), /* @__PURE__ */ React.createElement("p", null, label("\u9ED8\u8BA4\u5DE5\u4F5C\u6D41", "Default workflow"), " \xB7 ", target?.version ?? label("\u5F85\u53D1\u5E03", "Unreleased"), target && /* @__PURE__ */ React.createElement("code", { title: target.artifact_sha256 }, " \xB7 ", target.artifact_sha256.slice(0, 12))), product.delivery.pending && /* @__PURE__ */ React.createElement("p", { role: "status" }, label("\u53D1\u5E03\u64CD\u4F5C", "Delivery operation"), " \xB7 ", product.delivery.pending.stage), product.sync_error && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error", role: "alert" }, product.sync_error)), canReview && version && /* @__PURE__ */ React.createElement(WorkflowPlan, { product, version, session, label, submit, busy }), /* @__PURE__ */ React.createElement(ProductHistory, { product, canReview: canReview === true, label, submit, busy })), value && !product && /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, label("\u6B64 Bot \u6682\u672A\u5173\u8054\u4EA7\u54C1\u3002", "This Bot has no bound products.")), actor?.chief && /* @__PURE__ */ React.createElement(ChiefSetup, { session, bots: value?.setup_bots ?? [], label, submit, busy }));
    };
  }
  function repositoryLink(product) {
    return `https://github.com/${product.repository.split("/").map(encodeURIComponent).join("/")}`;
  }

  // ui/panel.css
  var panel_default = `.autoloop-panel, .autoloop-chat {
  --loop-paper: var(--dsw-alias-bg-layer-1, #fff); --loop-ink: var(--dsw-alias-label-primary, #1d1d1f);
  --loop-sub: var(--dsw-alias-label-secondary, #64646b); --loop-blue: #006edb; --loop-green: #238754; --loop-orange: #b86822;
  --loop-canvas: color-mix(in srgb, var(--loop-paper) 96%, var(--loop-ink));
  --loop-card: var(--loop-paper);
  --loop-line: color-mix(in srgb, var(--loop-ink) 9%, transparent);
  --loop-shadow: 0 1px 2px rgba(23,23,26,.04), 0 8px 28px -18px rgba(23,23,26,.18);
  color: var(--loop-ink);
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; container-type: inline-size;
}
.autoloop-panel {
  display: flex; flex-direction: column; width: 100%; height: 100%; min-height: 0; overflow: hidden;
  background: var(--loop-canvas);
}
.autoloop-panel h2, .autoloop-panel h3, .autoloop-panel h4, .autoloop-panel p { margin: 0; }
.autoloop-top { flex: none; padding: 23px 20px 13px; border-bottom: 1px solid var(--loop-line); background: color-mix(in srgb, var(--loop-paper) 85%, transparent); backdrop-filter: blur(24px) saturate(120%); }
.autoloop-heading, .autoloop-project-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.autoloop-top h2 { font-size: clamp(22px, 6cqw, 29px); font-weight: 720; line-height: 1.18; letter-spacing: -.045em; overflow-wrap: anywhere; }
.autoloop-heading p { color: var(--loop-sub); font-size: 12px; margin-top: 4px; }
.autoloop-mark, .autoloop-project-title i { position: relative; flex: none; width: 29px; height: 29px; border-radius: 8px; background: var(--loop-line); color: var(--loop-sub); }
.autoloop-mark::after, .autoloop-project-title i::after { content: ''; position: absolute; inset: 7px; border: 1.5px solid currentColor; border-radius: 3px; background: linear-gradient(transparent 43%, currentColor 43% 53%, transparent 53%); }
.autoloop-panel[data-role=pm] .autoloop-mark::after { inset: 7px 6px; background: linear-gradient(90deg, transparent 39%, currentColor 39% 50%, transparent 50%); }
.autoloop-line { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; }
.autoloop-line strong, .autoloop-line h3 { min-width: 0; overflow-wrap: anywhere; }
.autoloop-fold, .autoloop-file-path button, .autoloop-project-actions > button {
  color: var(--loop-ink); font: inherit; cursor: pointer; background: var(--loop-card); border: 1px solid var(--loop-line); border-radius: 9px; box-shadow: var(--loop-shadow);
}
.autoloop-fold { height: 30px; width: 30px; line-height: 1; font-size: 21px; flex: none; border-radius: 50%; color: var(--loop-sub); }
.autoloop-fold:hover, .autoloop-project-actions > button:hover, .autoloop-file-path button:hover { background: var(--loop-paper); color: var(--loop-blue); }
.autoloop-nav { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 2px; margin-top: 16px; padding: 3px; border-radius: 10px; background: color-mix(in srgb, var(--loop-ink) 7%, transparent); }
.autoloop-nav button { display: flex; align-items: center; justify-content: center; gap: 5px; padding: 5px 2px; color: var(--loop-sub); background: transparent; border: 1px solid transparent; border-radius: 8px; font: inherit; font-weight: 550; cursor: pointer; white-space: nowrap; }
.autoloop-nav button:hover { color: var(--loop-ink); }
.autoloop-nav button[aria-pressed=true] { background: var(--loop-paper); color: var(--loop-ink); border-color: var(--loop-line); box-shadow: 0 1px 3px #20273c1b; font-weight: 670; }
.autoloop-nav small { min-width: 15px; padding: 0 4px; border-radius: 10px; background: var(--loop-blue); color: #fff; font-size: 9px; line-height: 15px; }
.autoloop-tab-icon { position: relative; display: inline-block; width: 11px; height: 10px; border: 1px solid currentColor; border-radius: 3px; opacity: .8; }
.autoloop-tab-icon[data-kind=overview] { background: linear-gradient(90deg, transparent 42%, currentColor 42% 52%, transparent 52%); }
.autoloop-tab-icon[data-kind=inbox] { border-radius: 2px 2px 4px 4px; background: linear-gradient(transparent 58%, currentColor 58% 68%, transparent 68%); }
.autoloop-tab-icon[data-kind=files] { height: 8px; margin-top: 2px; }
.autoloop-tab-icon[data-kind=files]::before { content: ''; position: absolute; top: -3px; left: 0; width: 5px; height: 2px; border: 1px solid currentColor; border-bottom: 0; border-radius: 2px 2px 0 0; }
.autoloop-panel button:focus-visible, .autoloop-chat button:focus-visible,
.autoloop-task summary:focus-visible, .autoloop-plan-disclosure summary:focus-visible, .autoloop-issue-sources > summary:focus-visible { outline: 2px solid var(--loop-blue); outline-offset: 2px; }
.autoloop-scroll { min-height: 0; overflow: auto; padding: 16px 21px 21px; }
.autoloop-empty { margin: 17px 21px; padding: 21px; border-radius: 17px; border: 1px solid var(--loop-line); background: var(--loop-card); }
.autoloop-empty h3 { font-size: 16px; letter-spacing: -.02em; }
.autoloop-empty p { margin-top: 7px; color: var(--loop-sub); }
.autoloop-catalog { display: flex; flex-direction: column; gap: 12px; }
.autoloop-managed summary { cursor: pointer; font-weight: 650; overflow-wrap: anywhere; }
.autoloop-managed details { padding-bottom: 11px; }
.autoloop-managed form { display: grid; gap: 8px; margin: 12px 0; }
.autoloop-managed label:not(:has(input[type=checkbox])) { display: grid; gap: 3px; }
.autoloop-managed input:not([type=checkbox]), .autoloop-managed select { min-width: 0; width: 100%; padding: 4px;
  border: 1px solid var(--loop-line); border-radius: 5px; background: var(--loop-paper); color: var(--loop-ink); }
.autoloop-managed button { border: 1px solid var(--loop-line); border-radius: 5px; padding: 4px 7px; color: var(--loop-blue); background: var(--loop-paper); cursor: pointer; }
.autoloop-managed button:disabled { opacity: .55; cursor: default; }
.autoloop-managed a { color: var(--loop-blue); overflow-wrap: anywhere; }
.autoloop-managed-issue { margin-top: 8px; }
.autoloop-project { padding: 15px 15px 4px; border: 1px solid var(--loop-line); border-radius: 15px; background: var(--loop-card); box-shadow: var(--loop-shadow); }
.autoloop-project[data-draft-project] { padding-bottom: 14px; }
.autoloop-project.autoloop-clarification { padding-bottom: 14px; }
.autoloop-clarification .autoloop-clarification-question { margin-top: 10px; overflow-wrap: anywhere; }
.autoloop-clarification ul { padding-left: 17px; margin: 5px 0; color: var(--loop-sub); overflow-wrap: anywhere; }
.autoloop-clarification .autoloop-meta { margin-top: 8px; }
.autoloop-project h3, .autoloop-section-title h3 { font-size: 17px; line-height: 1.22; font-weight: 685; letter-spacing: -.032em; }
.autoloop-project-title { gap: 9px; }
.autoloop-project .autoloop-project-goal { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; margin-top: 9px; font-size: 12.5px; line-height: 1.55; }
.autoloop-project-next { margin-top: 12px; }
.autoloop-project-next h4 { color: var(--loop-sub); font-size: 11px; font-weight: 650; }
.autoloop-project-next ul { list-style: none; margin: 3px 0 0; padding: 0; }
.autoloop-project-next li { position: relative; margin: 3px 0; padding-left: 12px; color: var(--loop-ink); font-size: 12px; }
.autoloop-project-next li::before { position: absolute; content: ''; width: 4px; height: 4px; top: .55em; left: 2px; border-radius: 50%; background: var(--loop-sub); }
.autoloop-project-meter { margin: 13px 0 10px; }
.autoloop-project progress { appearance: none; display: block; width: 100%; height: 3px; margin: 5px 0 0; border: 0; border-radius: 5px; background: var(--loop-line); }
.autoloop-project progress::-webkit-progress-bar { border-radius: 5px; background: var(--loop-line); }
.autoloop-project progress::-webkit-progress-value { border-radius: 5px; background: var(--loop-blue); }
.autoloop-project[data-state=COMPLETED] progress::-webkit-progress-value { background: var(--loop-green); }
.autoloop-project-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 4px 0; border-top: 1px solid var(--loop-line); }
.autoloop-project-actions > button, .autoloop-pm-plan, .autoloop-back { padding: 6px 0; border: 0; background: transparent; color: var(--loop-blue); box-shadow: none; font: inherit; font-size: 12px; font-weight: 570; cursor: pointer; }
.autoloop-project-actions > button:hover, .autoloop-pm-plan:hover, .autoloop-back:hover { text-decoration: underline; background: transparent; }
.autoloop-project-actions .autoloop-view-plan { margin-right: auto; }
.autoloop-view-plan span, .autoloop-back span { font-size: 18px; line-height: .75; vertical-align: -.04em; }
.autoloop-back span { margin-right: 4px; }
.autoloop-pm-plan { margin-left: auto; flex: none; }
.autoloop-meta, .autoloop-muted, .autoloop-panel time { color: var(--loop-sub); font-size: 11.5px; line-height: 1.6; font-variant-numeric: tabular-nums; }
.autoloop-badge, .autoloop-live-count { border-radius: 12px; display: inline-flex; align-items: center; flex: none; padding: 3px 8px; background: var(--loop-line); font-size: 10.5px; line-height: 1.25; font-weight: 660; white-space: nowrap; }
.autoloop-live-count { color: var(--loop-sub); }
.autoloop-badge[data-state=ACTIVE], .autoloop-badge[data-state=RUNNING], .autoloop-badge[data-state=READY] { background: color-mix(in srgb, var(--loop-card) 83%, #6d9cf6); }
.autoloop-badge[data-state=COMPLETED] { background: color-mix(in srgb, var(--loop-card) 79%, #82d1ab); }
.autoloop-badge[data-state=FAILED], .autoloop-badge[data-state=BLOCKED], .autoloop-badge[data-state=ALERT] { background: color-mix(in srgb, var(--loop-card) 78%, #f3b08c); }
.autoloop-project-work { flex: 1; min-height: 0; display: grid; grid-template-rows: minmax(0, 1fr) minmax(0, 2fr); }
.autoloop-slice { overflow: auto; min-height: 0; padding: 0 21px 13px; }
.autoloop-runs { border-top: 1px solid var(--loop-line); background: color-mix(in srgb, var(--loop-paper) 26%, transparent); }
.autoloop-section-title { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 12px 0 9px; background: var(--loop-canvas); }
.autoloop-runs .autoloop-section-title { background: color-mix(in srgb, var(--loop-paper) 26%, var(--loop-canvas)); }
.autoloop-groups { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; gap: 8px; }
.autoloop-group { --loop-tone: var(--loop-blue); padding: 9px; min-width: 0; min-height: 66px; border-radius: 12px; border: 1px solid var(--loop-line); background: color-mix(in srgb, var(--loop-paper) 92%, var(--loop-tone)); box-shadow: var(--loop-shadow); }
.autoloop-group[data-group=done] { --loop-tone: var(--loop-green); }
.autoloop-group[data-group=blocked] { --loop-tone: var(--loop-orange); }
.autoloop-group h4 { display: flex; align-items: center; gap: 5px; color: var(--loop-sub); font-size: 11px; white-space: nowrap; }
.autoloop-group h4 i { width: 6px; height: 6px; flex: none; border-radius: 50%; background: var(--loop-tone); }
.autoloop-group-count { display: block; line-height: 1.08; margin: 3px 0; font-weight: 710; font-size: 23px; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.autoloop-task { border-top: 1px solid var(--loop-line); }
.autoloop-task > summary { cursor: pointer; display: flex; align-items: center; gap: 3px; justify-content: space-between; padding: 4px 0; list-style: none; font-size: 11.5px; }
.autoloop-task > summary::-webkit-details-marker { display: none; }
.autoloop-task > summary span { min-width: 0; overflow-wrap: anywhere; }
.autoloop-task > summary i { color: var(--loop-sub); font-size: 16px; font-style: normal; line-height: .8; }
.autoloop-task[open] > summary i { transform: rotate(90deg); }
.autoloop-task-body { padding: 2px 0 7px; color: var(--loop-sub); font-size: 11px; overflow-wrap: anywhere; }
.autoloop-task-body .autoloop-line { flex-wrap: wrap; gap: 3px; }
.autoloop-timeline, .autoloop-inbox { list-style: none; padding: 0; margin: 0; }
.autoloop-timeline { position: relative; display: grid; gap: 8px; padding: 2px 0 0 28px; }
.autoloop-timeline::before { content: ''; position: absolute; left: 8px; inset-block: 16px 14px; width: 1px; background: var(--loop-line); }
.autoloop-timeline > li, .autoloop-inbox li { position: relative; padding: 10px 11px; border-radius: 12px; border: 1px solid var(--loop-line); background: var(--loop-card); box-shadow: var(--loop-shadow); }
.autoloop-timeline > li::before { content: '\xB7'; position: absolute; left: -29px; top: 11px; width: 16px; height: 16px; display: grid; place-items: center; border-radius: 50%; background: #aaaebb; color: #fff; font-size: 12px; font-weight: 700; line-height: 1; }
.autoloop-timeline > li[data-state=COMPLETED]::before { content: '\u2713'; background: var(--loop-green); font-size: 10px; }
.autoloop-timeline > li[data-state=RUNNING]::before { content: '\u2022'; background: var(--loop-blue); box-shadow: 0 0 0 3px #3478f635; }
.autoloop-timeline > li[data-state=FAILED]::before { content: '!'; background: var(--loop-orange); }
.autoloop-run-detail, .autoloop-inbox p { margin-top: 4px !important; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--loop-sub); font-size: 11.5px; }
.autoloop-checkpoint { margin-top: 6px; color: var(--loop-sub); font-size: 11.5px; }
.autoloop-inbox { display: grid; gap: 8px; }
.autoloop-inbox small { color: var(--loop-sub); white-space: nowrap; font-size: 10.5px; }
.autoloop-inbox small[data-pending=true] { color: var(--loop-blue); font-weight: 700; }
.autoloop-inbox [data-feedback=FAILED] small, .autoloop-inbox .autoloop-issue-error { color: var(--dsw-alias-state-error-primary, #aa3830); }
.autoloop-issue-state { display: flex; align-items: center; gap: 4px; }
.autoloop-issue-delete { flex: none; display: grid; place-items: center; width: 24px; height: 24px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: var(--loop-sub); font: inherit; font-size: 17px; line-height: 1; cursor: pointer; }
.autoloop-issue-delete:hover:not(:disabled) { background: var(--loop-line); color: var(--loop-ink); }
.autoloop-issue-delete:focus-visible { outline: 2px solid var(--loop-blue); outline-offset: 1px; }
.autoloop-issue-delete:disabled { opacity: .55; cursor: default; }
.autoloop-file-path { display: flex; align-items: center; gap: 7px; padding: 6px 9px; margin-bottom: 9px; border-radius: 9px; border: 1px solid var(--loop-line); background: var(--loop-card); }
.autoloop-file-path code { overflow-wrap: anywhere; font: inherit; }
.autoloop-file-row { display: flex; align-items: center; gap: 9px; width: 100%; padding: 7px 9px; margin-bottom: 3px; border: 0; border-radius: 7px; color: inherit; background: transparent; cursor: pointer; text-align: left; font: inherit; }
.autoloop-file-row:hover { background: var(--loop-card); }
.autoloop-file-row > span:nth-child(2) { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.autoloop-file-icon { width: 12px; height: 14px; border-radius: 3px; border: 1px solid #7f93cb; background: var(--loop-card); flex: none; }
.autoloop-file-icon[data-folder=true] { width: 15px; height: 10px; border-color: #5a8fe5; background: linear-gradient(#b5ccff, #79a5fc); }
.autoloop-files pre { padding: 12px; background: var(--loop-card); border: 1px solid var(--loop-line); border-radius: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }
@container (max-width: 365px) {
  .autoloop-top, .autoloop-scroll, .autoloop-slice { padding-inline: 13px; }
  .autoloop-group { padding-inline: 6px; } .autoloop-groups { gap: 5px; }
}
.autoloop-error { color: var(--dsw-alias-state-error-primary, #ac4435); white-space: pre-wrap; overflow-wrap: anywhere; }
.autoloop-controls, .autoloop-strip { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.autoloop-controls button, .autoloop-strip-open { cursor: pointer; border: 0; border-radius: 999px; background: var(--loop-canvas); color: inherit; font: inherit; font-weight: 600; padding: 6px 12px; }
.autoloop-controls button:disabled { opacity: .55; cursor: default; }
.autoloop-controls button:hover:not(:disabled), .autoloop-strip-open:hover { background: color-mix(in srgb, var(--loop-canvas) 94%, var(--loop-ink)); }
.autoloop-controls [data-autoloop-action=START_PROJECT] { background: var(--loop-blue); color: #fff; padding: 8px 16px; }
.autoloop-controls [data-autoloop-action=START_PROJECT]:hover:not(:disabled) { background: #075cbb; }
.autoloop-chat-card { padding: 12px; margin: 8px 0; border: 1px solid var(--dsw-alias-border-l3); border-radius: 12px; }
.autoloop-chat-card p { white-space: pre-wrap; overflow-wrap: anywhere; }
.autoloop-chat-card-kind { font-weight: 650; }
.autoloop-notice-details { margin-top: 8px; overflow-wrap: anywhere; }
.autoloop-notice-details > summary { color: var(--loop-blue); cursor: pointer; width: fit-content; }
.autoloop-notice-details > div { padding: 8px 0; }
.autoloop-notice-details p { white-space: pre-wrap; }
.autoloop-delivery-evidence ul { margin: 6px 0; padding-inline-start: 18px; }
.autoloop-delivery-evidence li + li { margin-top: 8px; }
.autoloop-preview-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid color-mix(in srgb, var(--loop-ink) 17%, transparent);
  border-radius: 7px;
  background: transparent;
  color: var(--loop-ink);
  cursor: pointer;
  font-size: 12px;
  font-weight: 580;
  line-height: 1.5;
  text-decoration: none;
}
.autoloop-preview-link:hover { background: color-mix(in srgb, var(--loop-ink) 8%, transparent); color: var(--loop-ink); text-decoration: none; }
.autoloop-preview-link:focus-visible { outline: 2px solid var(--loop-ink); outline-offset: 2px; }
.autoloop-preview-link > [aria-hidden=true] { color: var(--loop-sub); }
.autoloop-pm-chat-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 8px; align-items: baseline; margin-bottom: 5px; }
.autoloop-pm-chat-head h3 { margin: 0; font-size: 16px; line-height: 1.3; overflow-wrap: anywhere; }
.autoloop-pm-chat p { margin: 6px 0 0; }
.autoloop-pm-proposal-group { margin-top: 10px; }
.autoloop-pm-proposal-group h4 { margin: 0 0 3px; font-size: 12px; }
.autoloop-pm-proposal-group ul { margin: 0; padding-inline-start: 20px; }
.autoloop-pm-proposal-group li + li { margin-top: 4px; }
.autoloop-pm-proposal-group li p { margin-top: 0; }
.autoloop-pm-proposal footer { margin-top: 12px; }
.autoloop-pm-proposal footer button { background: var(--loop-blue); color: #fff; }
.autoloop-pm-proposal footer button:hover:not(:disabled) { background: #075cbb; }
.autoloop-pm-public-note { margin-top: 6px; }
.autoloop-pm-public-note p { margin-top: 2px; }
.autoloop-strip-meta { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.autoloop-strip { font-size: 12px; margin-top: 7px; padding: 6px 8px; border: 1px solid var(--dsw-alias-border-l3); border-radius: 9px; }
.autoloop-strip-dot { width: 6px; height: 6px; border-radius: 50%; background: #3478f6; }
.autoloop-strip-alert { font-weight: 700; color: var(--dsw-alias-state-warn-primary); }

.autoloop-review-card { margin: 10px 0; padding: 19px 21px 15px; border: 1px solid var(--loop-line); border-radius: 19px; background: color-mix(in srgb, var(--loop-paper) 98%, var(--loop-ink)); box-shadow: var(--loop-shadow); overflow-wrap: anywhere; }
.autoloop-review-card h3, .autoloop-review-card h4, .autoloop-review-card p, .autoloop-review-card dl { margin: 0; }
.autoloop-review-heading h3, .autoloop-plan-pane-heading h3 { font-size: clamp(21px, 4.8cqw, 27px); letter-spacing: -.048em; line-height: 1.15; font-weight: 725; }
.autoloop-review-heading .autoloop-review-label { margin-bottom: 6px; color: var(--loop-sub); font-size: 11.5px; font-weight: 600; }
.autoloop-review-heading .autoloop-plan-goal { margin-top: 9px; color: var(--loop-sub); line-height: 1.6; }
.autoloop-plan-preview { margin: 15px 0 2px; padding: 0 12px 8px; border-radius: 12px; background: var(--loop-paper); border: 1px solid var(--loop-line); }
.autoloop-review-card:has(.autoloop-plan-disclosure[open]) .autoloop-plan-preview { display: none; }
.autoloop-plan-facts > div { display: grid; grid-template-columns: 5em minmax(0, 1fr); align-items: baseline; gap: 9px; padding: 8px 0; border-bottom: 1px solid var(--loop-line); }
.autoloop-plan-facts dt { color: var(--loop-sub); font-size: 11.5px; font-weight: 600; }
.autoloop-plan-facts dd { min-width: 0; margin: 0; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.autoloop-plan-preview h4 { margin: 9px 0 4px; font-size: 12px; font-weight: 650; }
.autoloop-preview-tasks { margin: 0 0 0 1.6em; padding: 0; }
.autoloop-preview-tasks > li { padding: 3px 0 3px 2px; }
.autoloop-preview-tasks > li::marker { color: var(--loop-sub); font-variant-numeric: tabular-nums; }
.autoloop-plan-disclosure { margin-top: 7px; }
.autoloop-plan-disclosure > summary { display: flex; width: max-content; max-width: 100%; align-items: center; gap: 8px; padding: 8px 0; cursor: pointer; color: var(--loop-blue); list-style: none; font-weight: 580; }
.autoloop-plan-disclosure > summary::-webkit-details-marker { display: none; }
.autoloop-plan-disclosure > summary i { width: 6px; height: 6px; border: solid currentColor; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); margin-top: -2px; }
.autoloop-plan-disclosure[open] > summary i { transform: rotate(225deg); margin-top: 2px; }
.autoloop-plan-disclosure:not([open]) .autoloop-hide-plan, .autoloop-plan-disclosure[open] .autoloop-show-plan { display: none; }
.autoloop-review-footer { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; padding-top: 12px; margin-top: 5px; border-top: 1px solid var(--loop-line); }
.autoloop-review-footer > span { color: var(--loop-sub); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.autoloop-plan-pane { overflow-wrap: anywhere; }
.autoloop-plan-pane-heading { margin: 10px 0 14px; }
.autoloop-plan-pane-heading h3:focus { outline: none; }
.autoloop-plan-pane-heading p { margin-top: 5px; }
.autoloop-plan-pane-footer { position: sticky; bottom: 0; margin: 12px -4px 0; padding: 10px 4px; border-top: 1px solid var(--loop-line); background: color-mix(in srgb, var(--loop-canvas) 87%, transparent); backdrop-filter: blur(17px); }
.autoloop-plan-details { display: grid; gap: 9px; }
.autoloop-phase-path { display: flex; flex-wrap: wrap; align-items: center; gap: 3px 8px; margin: 8px 0 0; padding: 0; list-style: none; color: var(--loop-sub); font-size: 11.5px; }
.autoloop-phase-path li { display: inline-flex; align-items: center; gap: 7px; }
.autoloop-phase-path li:not(:last-child)::after { content: ''; width: 4px; height: 4px; border: solid currentColor; border-width: 1px 1px 0 0; transform: rotate(45deg); opacity: .75; }
.autoloop-plan-section { background: var(--loop-paper); border-radius: 12px; border: 1px solid var(--loop-line); padding: 10px 12px; }
.autoloop-plan-section h4 { font-size: 13px; font-weight: 690; letter-spacing: -.01em; margin: 0 0 4px; }
.autoloop-plan-section p, .autoloop-plan-section ul, .autoloop-plan-section ol { margin: 5px 0 0; line-height: 1.52; }
.autoloop-plan-section ul, .autoloop-plan-section ol { padding-left: 1.45em; }
.autoloop-plan-section li + li { margin-top: 4px; }
.autoloop-plan-phase { margin-top: 10px; }
.autoloop-plan-phase + .autoloop-plan-phase { padding-top: 8px; border-top: 1px solid var(--loop-line); }
.autoloop-plan-phase h5 { margin: 0; color: var(--loop-sub); font-size: 12px; font-weight: 670; }
.autoloop-plan-section code, .autoloop-plan-facts code, .autoloop-plan-goal code, .autoloop-project-goal code { padding: 1px 2px; border-radius: 3px; background: var(--loop-canvas); font-size: .92em; }
.autoloop-plan-tasks > li + li { border-top: 1px solid var(--loop-line); padding-top: 8px; margin-top: 8px; }
.autoloop-plan-task-title { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 7px; }
.autoloop-plan-task-title small { color: var(--loop-sub); font-size: 10.5px; white-space: nowrap; }
.autoloop-plan-tasks p { color: var(--loop-sub); font-size: 12px; }
.autoloop-plan-tasks p > strong { margin-right: 4px; }
.autoloop-issue-sources { margin-top: 5px; font-size: 11.5px; }
.autoloop-issue-sources > summary { width: fit-content; color: var(--loop-blue); cursor: pointer; }
.autoloop-issue-sources > summary small { margin-inline-start: 6px; color: var(--loop-sub); font-size: inherit; }
.autoloop-issue-sources > ul { list-style: none; padding: 0; }
.autoloop-issue-sources li { margin-top: 4px; padding-inline-start: 7px; border-inline-start: 2px solid var(--loop-line); color: var(--loop-sub); white-space: pre-wrap; overflow-wrap: anywhere; }
.autoloop-plan-revision { margin: 10px 0; padding: 9px 11px; border-radius: 10px; background: var(--loop-canvas); border: 1px solid var(--loop-line); font-size: 12px; }
.autoloop-revision-count { margin-inline-start: 8px; color: var(--loop-sub); font-size: inherit; }
.autoloop-plan-pane .autoloop-plan-revision { background: var(--loop-paper); }
.autoloop-plan-revision > p { margin: 3px 0 0; color: var(--loop-sub); white-space: pre-wrap; overflow-wrap: anywhere; }
.autoloop-plan-revision[data-revision=FAILED] > strong, .autoloop-plan-revision > .autoloop-issue-error { color: var(--dsw-alias-state-error-primary, #aa3830); }
.autoloop-plan-revision[data-compact=true] > .autoloop-revision-issue { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.autoloop-pm-revision { padding: 0 21px; flex: none; }
.autoloop-pm-revision .autoloop-plan-revision { background: var(--loop-paper); margin-bottom: 0; }
@container (max-width: 370px) {
  .autoloop-review-card { padding: 14px 13px 12px; border-radius: 14px; }
  .autoloop-plan-facts > div { grid-template-columns: 4em minmax(0, 1fr); gap: 6px; }
  .autoloop-plan-facts dd { -webkit-line-clamp: 3; }
}
`;

  // ui/side_panel.tsx
  var ID = "dsbot-autoloop-plugin";
  var KIND = "autoloop";
  var FOLDED = "autoloop:right-folded";
  var PM_NAME = "Autoloop PM";
  var FILE_CACHE_PATHS = 8;
  var dictionaries = {
    zh: {
      tab: "Autoloop",
      description: "\u9879\u76EE\u603B\u53F0\u4E0E Worker \u8FDB\u5EA6",
      console: "\u9879\u76EE\u603B\u53F0",
      worker: "Worker \u65F6\u95F4\u8F74",
      stages: "\u4EFB\u52A1\u5206\u7EC4",
      overview: "\u9879\u76EE",
      inbox: "\u6536\u4EF6\u7BB1",
      files: "\u6587\u4EF6",
      managed: "\u4EA7\u54C1",
      fold: "\u6298\u53E0\u53F3\u680F",
      openPanel: "\u6253\u5F00\u9762\u677F",
      projectId: "\u9879\u76EE \xB7 {id}",
      pending: "\u5F85\u8FDB\u884C",
      done: "\u5DF2\u5B8C\u6210",
      blocked: "\u963B\u585E",
      working: "{count} \u4E2A\u8FD0\u884C\u4E2D",
      tasksDone: "{done}/{total} \u9879\u5B8C\u6210",
      projectCount: "{count} \u4E2A\u9879\u76EE",
      duration: "\u7528\u65F6 {count} \u79D2",
      budget: "DSH \u9884\u7B97",
      empty: "\u8FD8\u6CA1\u6709\u9879\u76EE",
      emptyBody: "\u5728 Chief \u804A\u5929\u4E2D\u8BF4\u300C\u7528 Autoloop \u521B\u5EFA\u9879\u76EE\uFF0C\u76EE\u6807\u662F\u2026\u300D\u3002",
      noRuns: "Worker \u5F00\u59CB\u6267\u884C\u540E\u4F1A\u663E\u793A\u5728\u8FD9\u91CC\u3002",
      noInbox: "\u6CA1\u6709\u5F85\u5904\u7406\u7684\u4E8B\u9879\u3002",
      noFiles: "\u9879\u76EE\u76EE\u5F55\u8FD8\u662F\u7A7A\u7684\u3002",
      noItems: "\u7A7A",
      openFiles: "\u67E5\u770B\u76EE\u5F55",
      back: "\u8FD4\u56DE\u4E0A\u4E00\u7EA7",
      root: "\u9879\u76EE\u76EE\u5F55",
      cutoff: "\u5DF2\u663E\u793A\u524D\u4E00\u90E8\u5206\u5185\u5BB9",
      queued: "\u5DF2\u63D0\u4EA4",
      waiting: "\u5F85\u5904\u7406",
      feedbackError: "\u672A\u5E94\u7528",
      attention: "\u5F85\u5904\u7406",
      stripTasks: "{done}/{total} \u4EFB\u52A1",
      stripWorkers: "{count} \u8FD0\u884C\u4E2D",
      loading: "\u6B63\u5728\u8BFB\u53D6\u2026",
      acceptance: "\u9A8C\u6536",
      dependencies: "\u4F9D\u8D56",
      artifacts: "\u4EA4\u4ED8\u7269",
      attempt: "\u7B2C {count} \u6B21",
      readOnly: "\u53EA\u8BFB",
      write: "\u8BFB\u5199",
      checkpointSaved: "\u5DF2\u4FDD\u5B58\u68C0\u67E5\u70B9",
      startCard: "\u8BA1\u5212\u5F85\u786E\u8BA4",
      previousPlan: "\u4E0A\u4E00\u7248\u8BA1\u5212",
      startDetails: "{count} \u9879\u4EFB\u52A1 \xB7 \u9884\u7B97\u4E0A\u9650 {budget}",
      planApproach: "\u65B9\u6848",
      planTasks: "\u4EFB\u52A1\u5B89\u6392",
      planNext: "\u63A5\u4E0B\u6765",
      openPlan: "\u67E5\u770B\u5B8C\u6574\u8BA1\u5212",
      closePlan: "\u6536\u8D77\u5B8C\u6574\u8BA1\u5212",
      backProjects: "\u8FD4\u56DE\u9879\u76EE",
      planPhases: "\u9879\u76EE\u9636\u6BB5",
      "phase.RESEARCH": "\u8C03\u7814",
      "phase.DESIGN": "\u5B9E\u73B0\u51B3\u7B56",
      "phase.IMPLEMENT": "\u5B9E\u65BD",
      "phase.VERIFY": "\u9A8C\u8BC1",
      "phase.RELEASE": "\u4EA4\u4ED8",
      "phase.other": "\u5176\u4ED6\u4EFB\u52A1",
      clarification: "\u5F85\u6F84\u6E05",
      clarificationReply: "\u8BF7\u5728 Chief \u5BF9\u8BDD\u4E2D\u7684\u63D0\u95EE\u5361\u56DE\u590D\u3002",
      "revision.QUEUED": "\u8BA1\u5212\u4FEE\u6539\u5DF2\u6392\u961F",
      "revision.UPDATING": "PM \u6B63\u5728\u4FEE\u6539\u8BA1\u5212",
      "revision.APPLIED": "\u8BA1\u5212\u5DF2\u6309\u53CD\u9988\u66F4\u65B0",
      "revision.FAILED": "\u8BA1\u5212\u66F4\u65B0\u5931\u8D25",
      "revision.issues": "\u5171 {count} \u6761\u53CD\u9988",
      "feedback.QUEUED": "\u6392\u961F\u4E2D",
      "feedback.PROCESSING": "PM \u6B63\u5728\u5904\u7406",
      "feedback.APPLIED": "\u5DF2\u5E94\u7528",
      "feedback.FAILED": "\u5904\u7406\u5931\u8D25",
      "feedback.SUPERSEDED": "\u5DF2\u88AB\u65B0\u53CD\u9988\u66FF\u4EE3",
      "action.DELETE_FEEDBACK": "\u5220\u9664 Issue",
      "pmActivity.QUEUED": "\u5DF2\u6392\u961F",
      "pmActivity.RUNNING": "PM \u6B63\u5728\u5904\u7406",
      "pmActivity.LONG_RUNNING": "PM \u4ECD\u5728\u5904\u7406",
      "pmActivity.READY": "PM \u5DF2\u63D0\u4EA4\u7ED3\u679C",
      "pmActivity.FAILED": "PM \u5904\u7406\u5931\u8D25",
      "pmActivity.note": "PM \u5907\u6CE8",
      "pmProposal.title": "\u5F85\u786E\u8BA4\u7684\u8BA1\u5212\u53D8\u66F4",
      "pmProposal.added": "\u65B0\u589E\u4EFB\u52A1",
      "pmProposal.updated": "\u8C03\u6574\u4EFB\u52A1",
      "pmProposal.removed": "\u79FB\u9664\u4EFB\u52A1",
      "pmProposal.acceptance": "\u9A8C\u6536 \xB7 {criterion}",
      "pmProposal.replacedBy": "\u7531\u300C{task}\u300D\u63A5\u7EED",
      "pmProposal.permission": "\u4EFB\u52A1\u6743\u9650 \xB7 {previous} \u2192 {next}",
      "pmProposal.apply": "\u786E\u8BA4\u5E76\u5E94\u7528",
      "pmProposal.applying": "\u6B63\u5728\u5E94\u7528\u2026",
      sourceIssues: "\u5173\u8054\u53CD\u9988",
      "draft.QUEUED": "\u7ACB\u9879\u8BF7\u6C42\u5DF2\u63A5\u6536",
      "draft.PLANNING": "PM \u6B63\u5728\u62DF\u5B9A\u8BA1\u5212",
      "draft.FAILED": "\u7ACB\u9879\u5931\u8D25",
      "draft.retry": "\u91CD\u8BD5\u89C4\u5212",
      "draft.retrying": "\u6B63\u5728\u63D0\u4EA4\u2026",
      pmDraftName: "\u7ACB\u9879 PM",
      pmProjectName: "\u9879\u76EE PM",
      archived: "\u5DF2\u5F52\u6863 \xB7 \u53EA\u8BFB",
      unavailable: "\u9879\u76EE\u8BB0\u5F55\u6682\u4E0D\u53EF\u7528",
      "action.START_PROJECT": "\u786E\u8BA4\u542F\u52A8",
      "action.PAUSE_PROJECT": "\u6682\u505C",
      "action.RESUME_PROJECT": "\u6062\u590D",
      "action.RETRY": "\u91CD\u8BD5",
      "action.RETRY_FEEDBACK": "\u91CD\u8BD5\u53CD\u9988",
      "notice.PROJECT_ALERT": "\u9879\u76EE\u63D0\u9192",
      "notice.PROJECT_COMPLETED": "\u9879\u76EE\u5B8C\u6210",
      "notice.openPreview": "\u6253\u5F00\u7F51\u9875",
      "receipt.RETRY": "\u5DF2\u5B89\u6392\u91CD\u8BD5",
      "notice.showEvidence": "\u67E5\u770B\u9A8C\u6536\u548C\u4EA7\u7269",
      "notice.ALERT_ESCALATED": "\u9879\u76EE\u53D7\u963B",
      "notice.checkVerified": "\u5DF2\u9A8C\u8BC1",
      "notice.checkPending": "\u5F85\u9A8C\u8BC1",
      "delivery.attention": "\u901A\u77E5\u6295\u9012\u5F85\u5904\u7406",
      "delivery.retry": "\u91CD\u8BD5\u6295\u9012",
      "delivery.queued": "\u5DF2\u63D0\u4EA4\u6838\u5BF9",
      "verification.pending": "{count} \u9879\u5F85 PM \u9A8C\u6536",
      "verification.passed": "PM \u9A8C\u6536\u901A\u8FC7",
      "verification.rejected": "PM \u9A8C\u6536\u9000\u56DE",
      "run.executed": "\u6267\u884C\u7ED3\u675F",
      "verification.retry": "\u91CD\u65B0\u9A8C\u6536",
      "run.retryInterrupted": "\u786E\u8BA4\u65E7\u8FD0\u884C\u5DF2\u505C\u6B62\u5E76\u91CD\u8BD5",
      "status.IDLE": "\u5F85\u542F\u52A8",
      "status.ACTIVE": "\u8FD0\u884C\u4E2D",
      "status.PAUSED": "\u5DF2\u6682\u505C",
      "status.COMPLETED": "\u5DF2\u5B8C\u6210",
      "status.ALERT": "\u5F85\u5904\u7406",
      "status.PENDING": "\u5F85\u4F9D\u8D56",
      "status.READY": "\u5C31\u7EEA",
      "status.RUNNING": "\u6267\u884C\u4E2D",
      "status.FAILED": "\u5931\u8D25",
      "status.BLOCKED": "\u5DF2\u963B\u585E",
      "status.INTERRUPTED": "\u5DF2\u4E2D\u65AD",
      "status.PENDING_VERIFY": "\u5F85 PM \u9A8C\u6536",
      "event.USER_FEEDBACK": "\u9879\u76EE Issue"
    },
    en: {
      tab: "Autoloop",
      description: "Projects and Worker activity",
      console: "Project desk",
      worker: "Worker timeline",
      stages: "Tasks",
      overview: "Projects",
      inbox: "Inbox",
      files: "Files",
      managed: "Products",
      fold: "Collapse sidebar",
      openPanel: "Open panel",
      projectId: "Project \xB7 {id}",
      pending: "To do",
      done: "Done",
      blocked: "Blocked",
      working: "{count} running",
      tasksDone: "{done}/{total} done",
      projectCount: "{count} projects",
      duration: "{count}s elapsed",
      budget: "DSH budget",
      empty: "No projects",
      emptyBody: "Ask Chief to start an Autoloop project in chat.",
      noRuns: "Worker activity appears here once a task starts.",
      noInbox: "Nothing to handle.",
      noFiles: "This project folder is empty.",
      noItems: "Empty",
      openFiles: "Browse files",
      back: "Parent directory",
      root: "Project folder",
      cutoff: "Showing the first part",
      queued: "Action queued",
      waiting: "Pending",
      feedbackError: "Unapplied",
      attention: "Needs attention",
      stripTasks: "{done}/{total} tasks",
      stripWorkers: "{count} running",
      loading: "Loading\u2026",
      acceptance: "Acceptance",
      dependencies: "Depends on",
      artifacts: "Artifacts",
      attempt: "Attempt {count}",
      readOnly: "Read-only",
      write: "Read/write",
      checkpointSaved: "Checkpoint saved",
      startCard: "Plan for review",
      previousPlan: "Previous plan",
      startDetails: "{count} tasks \xB7 Budget limit {budget}",
      planApproach: "Approach",
      planTasks: "Task plan",
      planNext: "Up next",
      openPlan: "View full plan",
      closePlan: "Hide full plan",
      backProjects: "Back to projects",
      planPhases: "Project phases",
      "phase.RESEARCH": "Research",
      "phase.DESIGN": "Implementation decisions",
      "phase.IMPLEMENT": "Implementation",
      "phase.VERIFY": "Validation",
      "phase.RELEASE": "Delivery",
      "phase.other": "Other tasks",
      clarification: "Awaiting clarification",
      clarificationReply: "Answer using the question card in Chief\u2019s chat.",
      "revision.QUEUED": "Plan revision queued",
      "revision.UPDATING": "PM is revising the plan",
      "revision.APPLIED": "Plan updated from feedback",
      "revision.FAILED": "Plan revision failed",
      "revision.issues": "{count} issues",
      "feedback.QUEUED": "Queued",
      "feedback.PROCESSING": "PM is working",
      "feedback.APPLIED": "Applied",
      "feedback.FAILED": "Failed",
      "feedback.SUPERSEDED": "Replaced by newer feedback",
      "action.DELETE_FEEDBACK": "Delete issue",
      "pmActivity.QUEUED": "Queued",
      "pmActivity.RUNNING": "PM is working",
      "pmActivity.LONG_RUNNING": "PM is still working",
      "pmActivity.READY": "PM submitted the result",
      "pmActivity.FAILED": "PM could not complete the request",
      "pmActivity.note": "PM note",
      "pmProposal.title": "Plan changes for review",
      "pmProposal.added": "Added tasks",
      "pmProposal.updated": "Updated tasks",
      "pmProposal.removed": "Removed tasks",
      "pmProposal.acceptance": "Acceptance \xB7 {criterion}",
      "pmProposal.replacedBy": "Continues as {task}",
      "pmProposal.permission": "Task permissions \xB7 {previous} \u2192 {next}",
      "pmProposal.apply": "Confirm and apply",
      "pmProposal.applying": "Applying\u2026",
      sourceIssues: "Linked feedback",
      "draft.QUEUED": "Project request received",
      "draft.PLANNING": "PM is drafting the plan",
      "draft.FAILED": "Project draft failed",
      "draft.retry": "Retry planning",
      "draft.retrying": "Submitting\u2026",
      pmDraftName: "Planning PM",
      pmProjectName: "Project PM",
      archived: "Archived \xB7 Read-only",
      unavailable: "Project record unavailable",
      "action.START_PROJECT": "Start",
      "action.PAUSE_PROJECT": "Pause",
      "action.RESUME_PROJECT": "Resume",
      "action.RETRY": "Retry",
      "action.RETRY_FEEDBACK": "Retry feedback",
      "notice.PROJECT_ALERT": "Project alert",
      "notice.PROJECT_COMPLETED": "Project complete",
      "notice.openPreview": "Open website",
      "receipt.RETRY": "Retry scheduled",
      "notice.showEvidence": "Verification and files",
      "notice.ALERT_ESCALATED": "Project blocked",
      "notice.checkVerified": "Verified",
      "notice.checkPending": "Pending verification",
      "delivery.attention": "Notification needs attention",
      "delivery.retry": "Retry delivery",
      "delivery.queued": "Queued for verification",
      "verification.pending": "{count} awaiting PM verification",
      "verification.passed": "PM accepted",
      "verification.rejected": "PM requested changes",
      "run.executed": "Execution ended",
      "verification.retry": "Retry verification",
      "run.retryInterrupted": "Confirm the old run stopped and retry",
      "status.IDLE": "Awaiting start",
      "status.ACTIVE": "Active",
      "status.PAUSED": "Paused",
      "status.COMPLETED": "Complete",
      "status.ALERT": "Needs attention",
      "status.PENDING": "Waiting",
      "status.READY": "Ready",
      "status.RUNNING": "Running",
      "status.FAILED": "Failed",
      "status.BLOCKED": "Blocked",
      "status.INTERRUPTED": "Interrupted",
      "status.PENDING_VERIFY": "Awaiting PM verification",
      "event.USER_FEEDBACK": "Project issue"
    }
  };
  var timeFormat = new Intl.DateTimeFormat(void 0, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  var panelPath = (session) => "panel?" + new URLSearchParams({ session_id: session });
  var blocked = /* @__PURE__ */ new Set(["BLOCKED", "FAILED"]);
  function registerPanel(React, ctx, notices) {
    const ManagedProducts = createManagedProductPanel(React);
    const ActionButtons = createActionButtons(React);
    const DraftRetry = createDraftRetry(React);
    const { NoticeDetails, useProjectNotices, WebsiteLink } = notices;
    const { Inline, PhasePath, IssueSources, PlanDetails, PlanRevision } = createPlanViews(React);
    const { mounted } = ctx.sidebarRight;
    const Stamp = ({ seconds }) => {
      const date = new Date(seconds * 1e3);
      return /* @__PURE__ */ React.createElement("time", { dateTime: date.toISOString() }, timeFormat.format(date));
    };
    const Badge = ({ state, t: t2 }) => /* @__PURE__ */ React.createElement("span", { className: "autoloop-badge", "data-state": state }, t2("status." + state));
    function DraftProject({ draft, session, t: t2 }) {
      const label = t2("draft." + draft.status);
      return /* @__PURE__ */ React.createElement("article", { className: "autoloop-project", "data-state": draft.status, "data-draft-project": draft.project_id }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-project-title" }, /* @__PURE__ */ React.createElement("i", { "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("h3", null, draft.title))), /* @__PURE__ */ React.createElement("p", { className: "autoloop-meta", role: draft.error ? "alert" : "status" }, label, draft.error && " \xB7 " + draft.error), /* @__PURE__ */ React.createElement(DraftRetry, { draft, sessionId: session, t: t2 }));
    }
    function ClarificationProject({ clarification, t: t2 }) {
      return /* @__PURE__ */ React.createElement("article", { className: "autoloop-project autoloop-clarification", "data-state": "CLARIFYING", "aria-label": t2("clarification") }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("h3", null, clarification.title), /* @__PURE__ */ React.createElement("span", { className: "autoloop-badge", "data-state": "CLARIFYING" }, t2("clarification"))), /* @__PURE__ */ React.createElement("p", { className: "autoloop-clarification-question" }, clarification.question), clarification.options.length > 0 && /* @__PURE__ */ React.createElement("ul", null, clarification.options.map((option, index) => /* @__PURE__ */ React.createElement("li", { key: index }, option))), /* @__PURE__ */ React.createElement("p", { className: "autoloop-meta" }, t2("clarificationReply")));
    }
    function TaskRow({ task, t: t2, projectId, sourceIds, sources, capability }) {
      const latest = task.worker_runs?.at(-1);
      const reason = latest?.escalation?.reason_code;
      const verdict = latest?.acceptance?.verdict;
      return /* @__PURE__ */ React.createElement("details", { className: "autoloop-task", "data-task-id": task.task_id, "data-state": task.status }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", null, task.title), /* @__PURE__ */ React.createElement("i", { "aria-hidden": "true" }, "\u203A")), /* @__PURE__ */ React.createElement("div", { className: "autoloop-task-body" }, /* @__PURE__ */ React.createElement("p", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("small", null, task.task_id), /* @__PURE__ */ React.createElement(Badge, { state: task.status, t: t2 })), task.acceptance && /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, t2("acceptance")), " ", task.acceptance), task.dependencies.length > 0 && /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, t2("dependencies")), " ", task.dependencies.join(" \xB7 ")), /* @__PURE__ */ React.createElement(IssueSources, { ids: sourceIds, sources, t: t2 }), task.error_msg && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error" }, task.error_msg), verdict && /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, t2(verdict.verdict === "VERIFIED" ? "verification.passed" : "verification.rejected")), " ", verdict.reason), task.output_artifacts.length > 0 && /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, t2("artifacts")), " ", task.output_artifacts.map((path) => path.split("/").at(-1)).join(" \xB7 ")), projectId && capability && /* @__PURE__ */ React.createElement(
        ActionButtons,
        {
          key: capability.event_id,
          t: t2,
          taskId: task.task_id,
          projectId,
          eventId: capability.event_id,
          actions: capability.actions,
          retryLabel: retryActionLabel(reason, t2),
          confirmWorkerStopped: capability.confirm_worker_stopped
        }
      )));
    }
    function ProjectWork({ tasks, t: t2, projectId, plan, taskActions }) {
      const sources = new Map(plan?.sources?.map((source) => [source.id, source.summary]));
      const taskSources = new Map(plan?.tasks.map((task) => [task.task_id, task.source_issue_ids]));
      const groups = [
        { key: "pending", nodes: tasks.filter((task) => task.status !== "COMPLETED" && !blocked.has(task.status)) },
        { key: "done", nodes: tasks.filter((task) => task.status === "COMPLETED") },
        { key: "blocked", nodes: tasks.filter((task) => blocked.has(task.status)) }
      ];
      const runs = tasks.flatMap((task) => (task.worker_runs ?? []).map((run, index) => ({ ...run, task, index }))).sort((a, b) => b.started_at - a.started_at);
      return /* @__PURE__ */ React.createElement("div", { className: "autoloop-project-work" }, /* @__PURE__ */ React.createElement("section", { className: "autoloop-slice", "aria-label": t2("stages") }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-section-title" }, /* @__PURE__ */ React.createElement("h3", null, t2("stages"))), /* @__PURE__ */ React.createElement("div", { className: "autoloop-groups" }, groups.map(({ key, nodes }) => /* @__PURE__ */ React.createElement("div", { className: "autoloop-group", key, "data-group": key }, /* @__PURE__ */ React.createElement("h4", null, /* @__PURE__ */ React.createElement("i", { "aria-hidden": "true" }), t2(key)), /* @__PURE__ */ React.createElement("strong", { className: "autoloop-group-count" }, nodes.length), nodes.map((task) => /* @__PURE__ */ React.createElement(
        TaskRow,
        {
          key: task.task_id,
          task,
          t: t2,
          projectId,
          sourceIds: taskSources.get(task.task_id),
          sources,
          capability: taskActions[task.task_id]
        }
      )))))), /* @__PURE__ */ React.createElement("section", { className: "autoloop-slice autoloop-runs", "aria-label": t2("worker") }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-section-title" }, /* @__PURE__ */ React.createElement("h3", null, t2("worker")), /* @__PURE__ */ React.createElement("small", { className: "autoloop-live-count" }, t2("working", { count: runs.filter((run) => run.status === "RUNNING").length }))), runs.length === 0 ? /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, t2("noRuns")) : /* @__PURE__ */ React.createElement("ol", { className: "autoloop-timeline" }, runs.map((run) => /* @__PURE__ */ React.createElement("li", { "data-state": run.status, key: run.task.task_id + "/" + run.index }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("strong", null, run.task.task_id, " \xB7 ", run.task.title), run.status === "COMPLETED" ? /* @__PURE__ */ React.createElement("small", { className: "autoloop-meta" }, t2("run.executed")) : /* @__PURE__ */ React.createElement(Badge, { state: run.status, t: t2 })), /* @__PURE__ */ React.createElement("div", { className: "autoloop-meta" }, /* @__PURE__ */ React.createElement(Stamp, { seconds: run.started_at }), " \xB7 ", t2("attempt", { count: run.index + 1 }), " \xB7 ", t2(run.task.permission_tier === 1 ? "readOnly" : "write"), run.finished_at != null && " \xB7 " + t2("duration", { count: Math.max(0, Math.round(run.finished_at - run.started_at)) })), run.checkpoint_path && /* @__PURE__ */ React.createElement("div", { className: "autoloop-checkpoint" }, t2("checkpointSaved")), run.detail && /* @__PURE__ */ React.createElement("p", { className: "autoloop-run-detail" }, run.detail))))));
    }
    function Files({ project, session, t: t2 }) {
      const [path, setPath] = React.useState(".");
      const query = "files?" + new URLSearchParams({ project_id: project.id, session_id: session, path });
      const { value, error } = usePoll(React, query, FILE_CACHE_PATHS);
      const file = value?.path === path ? value : void 0;
      const up = () => setPath(path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ".");
      return /* @__PURE__ */ React.createElement("section", { className: "autoloop-scroll autoloop-files", "aria-label": t2("files") }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-file-path" }, path !== "." && /* @__PURE__ */ React.createElement("button", { type: "button", onClick: up, "aria-label": t2("back") }, "\u2039"), /* @__PURE__ */ React.createElement("code", { title: project.work_dir }, path === "." ? t2("root") : path)), error ? /* @__PURE__ */ React.createElement("p", { className: "autoloop-error", role: "alert" }, error) : !file ? /* @__PURE__ */ React.createElement("p", null, t2("loading")) : file.kind === "file" ? /* @__PURE__ */ React.createElement("pre", null, file.text) : file.entries.length === 0 ? /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, t2("noFiles")) : file.entries.map((entry) => /* @__PURE__ */ React.createElement("button", { className: "autoloop-file-row", key: entry.path, type: "button", onClick: () => setPath(entry.path) }, /* @__PURE__ */ React.createElement("span", { className: "autoloop-file-icon", "data-folder": entry.directory, "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("span", null, entry.name), /* @__PURE__ */ React.createElement("span", { "aria-hidden": "true" }, "\u203A"))), file?.truncated && /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, t2("cutoff")));
    }
    function ProjectPreview({ project, t: t2 }) {
      const plan = project.plan;
      if (!plan) return null;
      const { goal } = planSummary(plan);
      const nextTasks = plan.tasks.filter((task) => task.status === "RUNNING" || task.status === "READY");
      const tasks = nextTasks.length ? nextTasks : plan.tasks.filter((task) => task.status !== "COMPLETED");
      const revisionPending = plan.revision?.status === "QUEUED" || plan.revision?.status === "UPDATING";
      return /* @__PURE__ */ React.createElement(React.Fragment, null, goal && /* @__PURE__ */ React.createElement("p", { className: "autoloop-project-goal" }, /* @__PURE__ */ React.createElement(Inline, { text: goal })), /* @__PURE__ */ React.createElement(PhasePath, { plan, t: t2 }), /* @__PURE__ */ React.createElement(PlanRevision, { plan, t: t2, compact: true }), !revisionPending && tasks.length > 0 && /* @__PURE__ */ React.createElement("section", { className: "autoloop-project-next" }, /* @__PURE__ */ React.createElement("h4", null, t2("planNext")), /* @__PURE__ */ React.createElement("ul", null, tasks.slice(0, 2).map((task) => /* @__PURE__ */ React.createElement("li", { key: task.task_id }, task.title)))));
    }
    function ProjectPlanView({ project, budget, allowStart, onBack, t: t2 }) {
      const { plan } = project;
      const heading = React.useRef(null);
      React.useEffect(() => {
        heading.current?.focus({ preventScroll: true });
      }, []);
      return /* @__PURE__ */ React.createElement("section", { className: "autoloop-scroll autoloop-plan-pane", "aria-label": t2("openPlan"), "data-project-plan": project.id }, /* @__PURE__ */ React.createElement("button", { className: "autoloop-back", type: "button", onClick: onBack }, /* @__PURE__ */ React.createElement("span", { "aria-hidden": "true" }, "\u2039"), t2("backProjects")), /* @__PURE__ */ React.createElement("header", { className: "autoloop-plan-pane-heading" }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("h3", { ref: heading, tabIndex: -1 }, project.name), /* @__PURE__ */ React.createElement(Badge, { state: project.status, t: t2 })), /* @__PURE__ */ React.createElement("p", { className: "autoloop-meta" }, budget ? t2("startDetails", { count: project.total, budget: money(budget.max_total_cost_usd) }) : t2("tasksDone", { done: project.done, total: project.total }))), /* @__PURE__ */ React.createElement(PlanRevision, { plan, t: t2 }), /* @__PURE__ */ React.createElement(PlanDetails, { plan, t: t2 }), allowStart && plan.approval_token && /* @__PURE__ */ React.createElement("footer", { className: "autoloop-plan-pane-footer" }, /* @__PURE__ */ React.createElement(
        ActionButtons,
        {
          key: plan.approval_token,
          t: t2,
          taskId: null,
          projectId: project.id,
          eventId: plan.approval_token,
          actions: ["START_PROJECT"]
        }
      )));
    }
    function InboxIssue({ event, projectId, t: t2, onDeleteError }) {
      const submitted = React.useRef(false);
      const [busy, setBusy] = React.useState(false);
      const remove = async () => {
        if (!projectId || submitted.current) return;
        submitted.current = true;
        setBusy(true);
        onDeleteError(projectId);
        try {
          await sendAction("DELETE_FEEDBACK", projectId, null, event.event_id);
        } catch (problem) {
          submitted.current = false;
          setBusy(false);
          onDeleteError(projectId, problem instanceof Error ? problem.message : String(problem));
        }
      };
      const deleteLabel = t2("action.DELETE_FEEDBACK");
      return /* @__PURE__ */ React.createElement("li", { "data-event": event.type, "data-feedback": event.feedback?.status, "aria-busy": busy }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("strong", null, t2("event." + event.type), event.task_id ? " \xB7 " + event.task_id : ""), /* @__PURE__ */ React.createElement("div", { className: "autoloop-issue-state" }, /* @__PURE__ */ React.createElement("small", { "data-pending": !event.consumed }, t2(event.feedback ? `feedback.${event.feedback.status}` : event.consumed ? "feedbackError" : "waiting")), projectId && /* @__PURE__ */ React.createElement(
        "button",
        {
          className: "autoloop-issue-delete",
          type: "button",
          disabled: busy,
          onClick: () => void remove(),
          "data-autoloop-action": "DELETE_FEEDBACK",
          "aria-label": deleteLabel,
          title: deleteLabel
        },
        "\xD7"
      ))), event.payload.summary && /* @__PURE__ */ React.createElement("p", null, event.payload.summary), event.feedback?.error && /* @__PURE__ */ React.createElement("p", { className: "autoloop-issue-error" }, event.feedback.error), projectId && event.feedback?.retry_event_id && /* @__PURE__ */ React.createElement(
        ActionButtons,
        {
          t: t2,
          taskId: null,
          projectId,
          eventId: event.feedback.retry_event_id,
          actions: ["RETRY_FEEDBACK"],
          disabled: busy
        }
      ), /* @__PURE__ */ React.createElement(Stamp, { seconds: event.timestamp }));
    }
    function NotificationRetry({ notice, session, t: t2 }) {
      const [sending, setSending] = React.useState(false);
      const [feedback, setFeedback] = React.useState();
      const retry = async () => {
        setSending(true);
        setFeedback(void 0);
        try {
          const { project_id, event_id, request_id } = notice;
          await request("chief-delivery-retry?" + new URLSearchParams({ session_id: session }), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id, event_id, request_id })
          });
          setFeedback({ text: t2("delivery.queued"), failed: false });
        } catch (error) {
          setFeedback({ text: error instanceof Error ? error.message : String(error), failed: true });
        } finally {
          setSending(false);
        }
      };
      return /* @__PURE__ */ React.createElement("section", { className: "autoloop-project", role: "status" }, /* @__PURE__ */ React.createElement("strong", null, t2("delivery.attention"), " \xB7 ", notice.project), notice.error && /* @__PURE__ */ React.createElement("p", null, notice.error), /* @__PURE__ */ React.createElement("div", { className: "autoloop-project-actions" }, /* @__PURE__ */ React.createElement("button", { type: "button", onClick: retry, disabled: sending }, t2("delivery.retry"))), feedback && /* @__PURE__ */ React.createElement("p", { role: feedback.failed ? "alert" : "status" }, feedback.text));
    }
    function Panel({ t: t2, session }) {
      const projectNotices = useProjectNotices(session);
      const { value, error } = usePoll(React, panelPath(session));
      const [selectedTab, setTab] = React.useState();
      const [fileProject, setFileProject] = React.useState();
      const [planProject, setPlanProject] = React.useState();
      const [issueDeleteError, setIssueDeleteError] = React.useState();
      if (!value) return /* @__PURE__ */ React.createElement("div", { className: "autoloop-panel", role: error ? "alert" : "status" }, error ?? t2("loading"));
      const { snapshot, projects, inbox, role, draft, live, pm_project_id: pmProjectId } = value;
      const tab = selectedTab ?? (role ? "overview" : "managed");
      const clarification = role === "chief" ? value.clarification : null;
      const current = projects.find((project) => project.id === snapshot.dag.project_id);
      const files = projects.find((project) => project.id === fileProject) ?? current;
      const selectedPlan = projects.find((project) => project.id === planProject);
      const openPlan = (projectId) => {
        setPlanProject(projectId);
        setTab("plan");
      };
      const unresolved = inbox.filter((event) => event.type === "USER_FEEDBACK" && (event.feedback ? ["QUEUED", "PROCESSING", "FAILED"].includes(event.feedback.status) : !event.consumed));
      const pending = unresolved.length;
      const pmProgress = current && t2("tasksDone", { done: current.done, total: current.total });
      const pmCaption = draft ? t2("projectId", { id: draft.project_id }) : pmProgress && !live ? t2("archived") + " \xB7 " + pmProgress : pmProgress ?? t2("unavailable");
      const actions = snapshot.dag.project_status === "ACTIVE" ? ["PAUSE_PROJECT"] : ["PAUSED", "ALERT"].includes(snapshot.dag.project_status) ? ["RESUME_PROJECT"] : [];
      return /* @__PURE__ */ React.createElement("aside", { className: "autoloop-panel", "aria-label": t2("tab"), "data-role": role, "data-pm-project": pmProjectId }, /* @__PURE__ */ React.createElement("header", { className: "autoloop-top" }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-heading" }, /* @__PURE__ */ React.createElement("i", { className: "autoloop-mark", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h2", null, role === "pm" ? current?.name ?? draft?.title ?? PM_NAME : t2("console")), /* @__PURE__ */ React.createElement("p", null, role === "pm" ? pmCaption : t2("projectCount", { count: projects.length + Number(Boolean(draft)) + Number(Boolean(clarification)) })))), role === "pm" && current?.plan && /* @__PURE__ */ React.createElement("button", { className: "autoloop-pm-plan", type: "button", onClick: () => tab === "plan" ? setTab("overview") : openPlan(current.id) }, t2(tab === "plan" ? "closePlan" : "openPlan")), /* @__PURE__ */ React.createElement("button", { type: "button", className: "autoloop-fold", "aria-label": t2("fold"), onClick: () => {
        localStorage.setItem(FOLDED, "1");
        ctx.sidebarRight.toggleExpanded();
      } }, "\u203A")), /* @__PURE__ */ React.createElement("nav", { className: "autoloop-nav", "aria-label": t2("tab") }, ["overview", "inbox", "files", "managed"].map((next) => /* @__PURE__ */ React.createElement(
        "button",
        {
          type: "button",
          key: next,
          "aria-pressed": next === tab || next === "overview" && tab === "plan",
          onClick: () => setTab(next)
        },
        /* @__PURE__ */ React.createElement("i", { className: "autoloop-tab-icon", "data-kind": next, "aria-hidden": "true" }),
        t2(next),
        next === "inbox" && pending > 0 && /* @__PURE__ */ React.createElement("small", null, pending)
      )))), error && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error", role: "alert" }, error), tab === "managed" ? /* @__PURE__ */ React.createElement(ManagedProducts, { session, chinese: t2("managed") === "\u4EA7\u54C1" }) : tab === "plan" && selectedPlan?.plan ? /* @__PURE__ */ React.createElement(
        ProjectPlanView,
        {
          key: selectedPlan.id,
          project: selectedPlan,
          t: t2,
          onBack: () => setTab("overview"),
          budget: selectedPlan.id === current?.id && live ? snapshot.budget : null,
          allowStart: role === "chief" && selectedPlan.id === current?.id && live
        }
      ) : tab === "inbox" ? /* @__PURE__ */ React.createElement("section", { className: "autoloop-scroll", "aria-label": t2("inbox") }, issueDeleteError && issueDeleteError.projectId === current?.id && /* @__PURE__ */ React.createElement("p", { className: "autoloop-error", role: "alert" }, issueDeleteError.message), unresolved.length === 0 ? /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, t2("noInbox")) : /* @__PURE__ */ React.createElement("ul", { className: "autoloop-inbox", "data-inbox-state": "unresolved" }, unresolved.map((event) => /* @__PURE__ */ React.createElement(
        InboxIssue,
        {
          key: event.event_id,
          event,
          projectId: live ? current?.id ?? null : null,
          t: t2,
          onDeleteError: (projectId, message) => setIssueDeleteError(message ? { projectId, message } : void 0)
        }
      )))) : tab === "files" ? files ? /* @__PURE__ */ React.createElement(Files, { key: files.id, project: files, session, t: t2 }) : /* @__PURE__ */ React.createElement("p", { className: "autoloop-muted" }, t2("noFiles")) : role === "pm" && current ? /* @__PURE__ */ React.createElement(React.Fragment, null, current.plan?.revision && /* @__PURE__ */ React.createElement("div", { className: "autoloop-pm-revision" }, /* @__PURE__ */ React.createElement(PlanRevision, { plan: current.plan, t: t2, compact: true })), /* @__PURE__ */ React.createElement(
        ProjectWork,
        {
          tasks: snapshot.dag.tasks,
          t: t2,
          projectId: live ? current.id : null,
          plan: current.plan,
          taskActions: snapshot.task_actions
        }
      )) : !projects.length && !draft && !clarification ? /* @__PURE__ */ React.createElement("div", { className: "autoloop-empty" }, /* @__PURE__ */ React.createElement("h3", null, t2(role === "pm" ? "unavailable" : "empty")), role !== "pm" && /* @__PURE__ */ React.createElement("p", null, t2("emptyBody"))) : /* @__PURE__ */ React.createElement("section", { className: "autoloop-scroll autoloop-catalog", "aria-label": t2(role === "pm" ? "tab" : "console") }, clarification && /* @__PURE__ */ React.createElement(ClarificationProject, { clarification, t: t2 }), draft && /* @__PURE__ */ React.createElement(DraftProject, { draft, session, t: t2 }), role === "chief" && value.notification_attention?.map((notice) => /* @__PURE__ */ React.createElement(NotificationRetry, { key: notice.request_id, notice, session, t: t2 })), projects.map((project) => {
        const href = previewHref(project.preview_url);
        return /* @__PURE__ */ React.createElement("article", { className: "autoloop-project", key: project.id, "data-state": project.status }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("h3", null, project.name), /* @__PURE__ */ React.createElement(Badge, { state: project.status, t: t2 })), /* @__PURE__ */ React.createElement(ProjectPreview, { project, t: t2 }), /* @__PURE__ */ React.createElement("div", { className: "autoloop-project-meter" }, /* @__PURE__ */ React.createElement("div", { className: "autoloop-line" }, /* @__PURE__ */ React.createElement("p", { className: "autoloop-meta" }, t2("tasksDone", { done: project.done, total: project.total }), project.pending_verify ? " \xB7 " + t2("verification.pending", { count: project.pending_verify }) : "", project.blocked > 0 ? " \xB7 " + t2("blocked") + " " + project.blocked : ""), project.id === current?.id && /* @__PURE__ */ React.createElement("p", { className: "autoloop-meta", title: t2("budget") }, money(snapshot.budget.current_cost_usd), " / ", money(snapshot.budget.max_total_cost_usd))), /* @__PURE__ */ React.createElement("progress", { max: project.total || 1, value: project.done, "aria-label": t2("tasksDone", { done: project.done, total: project.total }) })), projectNotices.filter((notice) => notice.project_id === project.id).map((notice) => /* @__PURE__ */ React.createElement(NoticeDetails, { key: notice.id, notice, t: t2 })), /* @__PURE__ */ React.createElement("div", { className: "autoloop-project-actions" }, href && /* @__PURE__ */ React.createElement(WebsiteLink, { href, t: t2 }), project.plan && /* @__PURE__ */ React.createElement("button", { className: "autoloop-view-plan", type: "button", onClick: () => openPlan(project.id) }, t2("openPlan"), " ", /* @__PURE__ */ React.createElement("span", { "aria-hidden": "true" }, "\u203A")), project.id === current?.id && actions.length > 0 && /* @__PURE__ */ React.createElement(ActionButtons, { taskId: null, t: t2, projectId: project.id, actions: [...actions] }), /* @__PURE__ */ React.createElement("button", { type: "button", onClick: () => {
          setFileProject(project.id);
          setTab("files");
        } }, t2("openFiles"))));
      })));
    }
    const t = ctx.locale.bind(KIND);
    ctx.effect(() => ctx.sidebarRightTabs.register({
      id: ID,
      kind: KIND,
      title: () => t("tab"),
      guide: [{ id: KIND, order: 60, title: () => t("tab"), description: () => t("description") }]
    }), "Autoloop right sidebar tab");
    ctx.slots.inject("sidebar.right.pane.tab", () => ctx.slots.register(
      { name: "sidebar.right.pane.tab", key: ID, locale: KIND },
      ({ t: t2, useTabInfo }) => {
        const session = React.useSyncExternalStore(mounted.subscribe, mounted.getSnapshot);
        return useTabInfo().tab.visible && session ? /* @__PURE__ */ React.createElement(Panel, { key: session, t: t2, session }) : null;
      }
    ));
  }
  window.__ModuleLoader__.load({ id: ID, factory(require2) {
    const React = require2("react");
    return { inject: ["slots", "locale", "sidebarRightTabs", "sidebarRight"], apply(ctx) {
      const open = () => {
        localStorage.removeItem(FOLDED);
        ctx.sidebarRight.openTab(KIND, { destination: "sidebar" });
      };
      ctx.effect(() => ctx.locale.register(KIND, dictionaries), "Autoloop copy");
      ctx.effect(() => {
        const style = document.createElement("style");
        style.textContent = panel_default;
        document.head.append(style);
        return () => style.remove();
      }, "Autoloop styles");
      const notices = registerNotices(ctx.slots, React, KIND);
      ctx.effect(notices.connect, "Autoloop notice feed");
      registerPanel(React, ctx, notices);
      registerChatCards(ctx.slots, React, KIND, open);
      ctx.effect(() => {
        const { mounted } = ctx.sidebarRight;
        const synchronized = /* @__PURE__ */ new Set();
        const t = ctx.locale.bind(KIND);
        let leave = () => {
        };
        const entered = () => {
          leave();
          const session = mounted.getSnapshot();
          if (!session) return;
          const controller = new AbortController();
          const { signal } = controller;
          let timer;
          let first = true;
          let chief = false;
          let warned = false;
          const refresh = async () => {
            try {
              const panel = await request(panelPath(session), { signal });
              if (signal.aborted) return;
              if (first && panel.role && !localStorage.getItem(FOLDED)) open();
              first = false;
              chief = panel.role === "chief";
              if (!chief) return;
              const active = panel.live && panel.snapshot.dag.project_status !== "COMPLETED" ? panel.snapshot.dag.project_id : null;
              const targets = panel.projects.map((project) => ({
                key: `project:${project.id}`,
                id: project.id,
                title: project.name,
                nickname: project.pm_name,
                create: project.id === active
              }));
              if (panel.draft) targets.unshift({
                key: `draft:${panel.draft.project_id}`,
                id: panel.draft.project_id,
                title: void 0,
                nickname: panel.draft.pm_name,
                create: true
              });
              const next = targets.filter((target) => !synchronized.has(target.key));
              if (next.length) {
                await syncProjectPMs(next, { pending: t("pmDraftName"), project: t("pmProjectName") }, signal);
                for (const item of next) synchronized.add(item.key);
              }
              warned = false;
            } catch (error) {
              if (!signal.aborted && !warned) console.warn("Autoloop sidebar:", error);
              warned = true;
            } finally {
              if (!signal.aborted && (chief || first)) timer = setTimeout(refresh, 2e3);
            }
          };
          leave = () => {
            controller.abort();
            clearTimeout(timer);
          };
          void refresh();
        };
        const unsubscribe = mounted.subscribe(entered);
        entered();
        return () => {
          unsubscribe();
          leave();
        };
      }, "Autoloop sidebar for Chief and PM");
    } };
  } });
})();
