/**
 * 转换任务页（/convert 与 /convert/:taskId）
 *
 * 后端契约（保持原样，勿改）：
 *  - GET  /api/dubbing/projects                 -> { projects: [{ id, name, book_id }] }
 *  - GET  /api/tasks                            -> { tasks: Task[] }
 *  - GET  /api/tasks/{id}                        -> { task: Task }
 *  - POST /api/tasks/convert { project_id, book_id, output_format, settings }
 *  - POST /api/tasks/{id}/pause | resume | cancel | DELETE /api/tasks/{id}
 *  - WS   /api/tasks/{id}/progress（失败时回落到 2s 轮询 GET /api/tasks/{id}）
 *  - GET  /api/tasks/{id}/download
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Ban,
  Download,
  FileAudio,
  Gauge,
  History,
  Pause,
  Play,
  RefreshCw,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { dubbingApi, taskApi } from "../services/api";
import { useI18n } from "../i18n";
import {
  Alert,
  Badge,
  Button,
  Card,
  DetailList,
  EmptyState,
  Field,
  LoaderBlock,
  Page,
  PageHeader,
  Pipeline,
  ProgressRing,
  ResponsiveTable,
  SectionTitle,
  Segmented,
  Select,
  StackForm,
  StatusBadge,
  Toolbar,
  ToolbarSpacer,
  useConfirm,
  useToast,
  type PipelineItem,
  type PipelineState,
  type TableColumn,
} from "../components/ui";
import TtsModeToggle from "../components/TtsModeToggle";
import { usePrefsStore } from "../store/prefsStore";
import { cx } from "../lib/cx";

interface Task {
  id: string;
  project_id: string;
  book_id: string;
  status: string;
  progress: number;
  current_stage: string;
  output_format: string;
  output_path: string;
  total_chars: number;
  processed_chars: number;
  error_message: string;
  elapsed_seconds: number;
  started_at: string;
  completed_at: string;
  created_at: string;
}

interface Project {
  id: string;
  name: string;
  book_id: string;
}

/** 九阶段机器码，顺序即流水线顺序；文案取自 enums.stage */
const STAGE_KEYS = [
  "import",
  "parse",
  "clean",
  "split",
  "analyze",
  "assign",
  "synthesize",
  "postprocess",
  "package",
] as const;

const FORMATS = ["m4b", "mp3", "wav", "flac"] as const;

const FINISHED = ["completed", "failed", "cancelled"];
/** 断线重连上限：1s/2s/4s 三跳后就把"只剩轮询"这件事讲给用户，而不是静默放弃 */
const RECONNECT_LIMIT = 3;
/** 轮询连续失败上限（2s 一跳 = 约 10s）：任务已被删除时靠它收口 */
const POLL_FAIL_LIMIT = 5;

/** FastAPI 的错误信息在 detail 里，长度不可控，只做类型收敛不做截断 */
function detailOf(err: unknown): string | null {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  return null;
}

type RingTone = "accent" | "success" | "warning" | "danger" | "running";

function toneOf(status: string): RingTone {
  if (status === "failed" || status === "cancelled") return "danger";
  if (status === "completed") return "success";
  if (status === "paused") return "warning";
  if (status === "running") return "running";
  return "accent";
}

export default function ConvertPage() {
  const { taskId } = useParams<{ taskId: string }>();
  const navigate = useNavigate();
  const { t, tEnum, format } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();

  const [currentTask, setCurrentTask] = useState<Task | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [outputFormat, setOutputFormat] = useState<string>("m4b");
  const [deleting, setDeleting] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [wsError, setWsError] = useState<string | null>(null);
  /** 指令#1：配音模式（在线/本地）是用户偏好，跟着请求发给后端 `settings.tier` */
  const ttsTier = usePrefsStore((state) => state.ttsTier);
  const setTtsTier = usePrefsStore((state) => state.setTtsTier);

  const wsRef = useRef<WebSocket | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** 主动关闭（切页 / 任务终态）与意外断线必须分得开：否则 onclose 会跟自己的重连抢方向 */
  const closingByUsRef = useRef(false);
  const reconnectTriesRef = useRef(0);
  const pollFailRef = useRef(0);

  const stopTransport = useCallback(() => {
    closingByUsRef.current = true;
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, []);

  /** WebSocket 不可用时的 HTTP 轮询后备；连续失败要自己收口，不许无限空转 */
  const startPolling = useCallback(
    (id: string) => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
      pollTimerRef.current = setInterval(async () => {
        try {
          const response = await taskApi.get(id);
          const task: Task | undefined = response.data.task;
          pollFailRef.current = 0;
          if (task) {
            setCurrentTask(task);
            if (FINISHED.includes(task.status)) stopTransport();
          }
        } catch (err) {
          pollFailRef.current += 1;
          console.error("轮询进度失败:", err);
          if (pollFailRef.current >= POLL_FAIL_LIMIT) {
            // 多半是任务已被删除：停下并报出来，比一直"看着像在跑"诚实
            setWsError(t("convert.pollGone"));
            stopTransport();
          }
        }
      }, 2000);
    },
    [stopTransport, t]
  );

  const connectWebSocket = useCallback(
    (id: string) => {
      if (wsRef.current) {
        closingByUsRef.current = true;
        wsRef.current.close();
        wsRef.current = null;
      }
      closingByUsRef.current = false;
      setWsError(null);

      const open = () => {
        const host = window.location.host;
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const ws = new WebSocket(`${protocol}//${host}/api/tasks/${id}/progress`);
        wsRef.current = ws;

        ws.onopen = () => {
          reconnectTriesRef.current = 0;
          setWsError(null);
        };
        ws.onmessage = (event) => {
          let data: Partial<Task>;
          try {
            data = JSON.parse(event.data) as Partial<Task>;
          } catch (err) {
            console.error("进度消息解析失败:", err);
            return;
          }
          setCurrentTask((prev) => (prev ? { ...prev, ...data } : (data as Task)));
          if (data.status && FINISHED.includes(data.status)) stopTransport();
        };
        ws.onerror = (err) => {
          // 浏览器随后一定还会给一次 onclose，恢复动作统一放在那里，避免两条路各修一次
          console.error("WebSocket错误:", err);
        };
        ws.onclose = () => {
          if (closingByUsRef.current || wsRef.current !== ws) return;
          wsRef.current = null;
          setWsError(t("convert.wsFallback"));
          startPolling(id);
          if (reconnectTriesRef.current < RECONNECT_LIMIT) {
            const delay = 1000 * 2 ** reconnectTriesRef.current;
            reconnectTriesRef.current += 1;
            if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
            reconnectTimerRef.current = setTimeout(open, delay);
          }
        };
      };

      open();
    },
    [startPolling, stopTransport, t]
  );

  const loadTasks = useCallback(async () => {
    try {
      const response = await taskApi.list();
      const list: Task[] = response.data.tasks || [];
      setTasks(
        [...list].sort(
          (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        )
      );
    } catch (err) {
      console.error("加载任务列表失败:", err);
    }
  }, []);

  const loadProjects = useCallback(async () => {
    try {
      const response = await dubbingApi.listProjects();
      const list: Project[] = response.data.projects || [];
      setProjects(list);
      if (list.length > 0) setSelectedProjectId(list[0].id);
    } catch (err) {
      console.error("加载项目列表失败:", err);
    }
  }, []);

  const loadCurrentTask = useCallback(async (id: string) => {
    try {
      const response = await taskApi.get(id);
      setCurrentTask(response.data.task);
      setError(null);
    } catch (err) {
      setError(detailOf(err) ?? t("common.loadFailed"));
      console.error(err);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const init = async () => {
      setLoading(true);
      await Promise.all([loadTasks(), loadProjects()]);
      setLoading(false);
    };
    init();
    return stopTransport;
  }, [loadTasks, loadProjects, stopTransport]);

  useEffect(() => {
    if (taskId) {
      loadCurrentTask(taskId);
      connectWebSocket(taskId);
    } else {
      setCurrentTask(null);
      stopTransport();
    }
    return stopTransport;
  }, [taskId, loadCurrentTask, connectWebSocket, stopTransport]);

  const handleStartConvert = async () => {
    if (!selectedProjectId) {
      toast.warning(t("convert.noProject"));
      return;
    }
    try {
      setStarting(true);
      setError(null);
      const response = await taskApi.startConvert({
        project_id: selectedProjectId,
        book_id: projects.find((p) => p.id === selectedProjectId)?.book_id || selectedProjectId,
        output_format: outputFormat,
        settings: { tier: ttsTier },
      });
      const newTask = response.data.task || response.data;
      navigate(`/convert/${newTask.id}`);
      toast.success(t("convert.started"));
      await loadTasks();
    } catch (err) {
      setError(detailOf(err) ?? t("convert.startFailed"));
      toast.error(detailOf(err) ?? t("convert.startFailed"));
      console.error(err);
    } finally {
      setStarting(false);
    }
  };

  const runAction = async (action: "pause" | "resume" | "cancel") => {
    if (!currentTask) return;
    try {
      await taskApi[action](currentTask.id);
      await loadCurrentTask(currentTask.id);
      toast.info(
        action === "pause" ? t("convert.paused") : action === "resume" ? t("convert.resumed") : t("convert.cancelled")
      );
    } catch (err) {
      toast.error(detailOf(err) ?? t("common.operationFailed"));
      console.error(err);
    }
  };

  const handleDownload = (task: Task) => {
    if (!task.output_path) return;
    window.open(`/api/tasks/${task.id}/download`, "_blank");
  };

  const handleDeleteTask = async (id: string) => {
    const ok = await confirm({
      title: t("common.confirmDeleteTitle"),
      message: t("convert.deleteConfirm"),
      tone: "danger",
    });
    if (!ok) return;
    try {
      setDeleting(id);
      await taskApi.delete(id);
      toast.success(t("common.deleted"));
      await loadTasks();
      if (currentTask?.id === id) {
        setCurrentTask(null);
        navigate("/convert", { replace: true });
      }
    } catch (err) {
      toast.error(detailOf(err) ?? t("common.deleteFailed"));
      console.error(err);
    } finally {
      setDeleting(null);
    }
  };

  const percentage = currentTask ? Math.round((currentTask.progress || 0) * 100) : 0;

  const pipelineItems = useMemo(() => {
    const current = currentTask ? STAGE_KEYS.indexOf(currentTask.current_stage as never) : -1;
    const failed = currentTask?.status === "failed";
    return STAGE_KEYS.map((key, index): PipelineItem => {
      let state: PipelineState = "pending";
      if (index < current) state = "done";
      else if (index === current) state = failed ? "failed" : "current";
      return { id: key, label: tEnum("stage", key), state };
    });
  }, [currentTask, tEnum]);

  const columns = useMemo<Array<TableColumn<Task>>>(() => {
    return [
      {
        key: "task",
        header: t("convert.columns.task"),
        render: (task) => <span className="mono u-xs">{task.id.slice(0, 8)}…</span>,
      },
      {
        key: "status",
        header: t("convert.columns.status"),
        render: (task) => <StatusBadge status={task.status} />,
      },
      {
        key: "progress",
        header: t("convert.columns.progress"),
        render: (task) => {
          const pct = Math.round((task.progress || 0) * 100);
          return (
            <span className={cx("u-row u-center", "u-nowrap")} style={{ gap: "var(--space-2)" }}>
              <span
                className="mini-bar"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
              >
                <span className="mini-bar__fill" style={{ width: `${pct}%` }} />
              </span>
              <span className="u-xs u-num u-nowrap">{pct}%</span>
            </span>
          );
        },
      },
      {
        key: "format",
        header: t("convert.columns.format"),
        hideOnMobile: true,
        render: (task) => (
          <Badge tone="neutral" size="sm">
            {(task.output_format || "—").toUpperCase()}
          </Badge>
        ),
      },
      {
        key: "created",
        header: t("convert.columns.created"),
        hideOnMobile: true,
        align: "left",
        render: (task) => <span className="u-xs u-num u-nowrap">{format.dateTime(task.created_at)}</span>,
      },
      {
        key: "actions",
        header: t("convert.columns.actions"),
        align: "right",
        render: (task) =>
          FINISHED.includes(task.status) ? (
            <span onClick={(event) => event.stopPropagation()}>
              <Button
                size="xs"
                variant="danger-soft"
                icon={Trash2}
                loading={deleting === task.id}
                disabled={deleting === task.id}
                onClick={() => handleDeleteTask(task.id)}
              >
                {t("common.delete")}
              </Button>
            </span>
          ) : null,
      },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t, tEnum, format, deleting, tasks.length]);

  const projectOptions = useMemo(
    () => projects.map((project) => ({ value: project.id, label: project.name || project.id })),
    [projects]
  );

  const formatOptions = useMemo(
    () =>
      FORMATS.map((item) => ({
        value: item,
        label: item.toUpperCase(),
        desc: item === "m4b" ? t("convert.formatRecommended") : undefined,
      })),
    [t]
  );

  return (
    <Page>
      <PageHeader
        title={t("convert.title")}
        subtitle={t("convert.subtitle")}
        actions={
          taskId ? (
            <Button variant="ghost" icon={ArrowLeft} onClick={() => navigate("/convert")}>
              {t("common.back")}
            </Button>
          ) : null
        }
      />

      <div className="convert-layout">
        {error ? (
          <Alert tone="danger" title={error} onDismiss={() => setError(null)} />
        ) : null}
        {wsError && taskId ? <Alert tone="warning" title={wsError} icon={TriangleAlert} /> : null}

        {loading ? (
          <Card>
            <LoaderBlock>{t("common.loading")}</LoaderBlock>
          </Card>
        ) : (
          <>
            {taskId && currentTask ? (
              <Card
                title={t("convert.currentTask")}
                icon={Gauge}
                actions={
                  <Toolbar>
                    <ToolbarSpacer />
                    <StatusBadge status={currentTask.status} size="lg" />
                  </Toolbar>
                }
              >
                <div className="task-current">
                  <div className="task-current__ring">
                    <ProgressRing
                      value={percentage}
                      tone={toneOf(currentTask.status)}
                      sub={tEnum("stage", currentTask.current_stage)}
                      ariaLabel={t("convert.currentTask")}
                    />
                  </div>
                  <div className="u-stack u-col">
                    <Pipeline items={pipelineItems} />
                    <div className="task-metrics">
                      <div className="metric">
                        <span className="metric__label">{t("convert.processed")}</span>
                        <span className="metric__value">
                          {format.number(currentTask.processed_chars || 0)} /{" "}
                          {format.number(currentTask.total_chars || 0)}
                        </span>
                      </div>
                      <div className="metric">
                        <span className="metric__label">{t("convert.elapsed")}</span>
                        <span className="metric__value">
                          {format.seconds(currentTask.elapsed_seconds)}
                        </span>
                      </div>
                      <div className="metric">
                        <span className="metric__label">{t("convert.stage")}</span>
                        <span className="metric__value">
                          {tEnum("stage", currentTask.current_stage, t("common.unknown"))}
                        </span>
                      </div>
                      <div className="metric">
                        <span className="metric__label">{t("convert.output")}</span>
                        <span className="metric__value">
                          {(currentTask.output_format || "—").toUpperCase()}
                        </span>
                      </div>
                    </div>
                    <div className="btn-group u-wrap">
                      {currentTask.status === "running" ? (
                        <>
                          <Button variant="warning" icon={Pause} onClick={() => runAction("pause")}>
                            {t("convert.pause")}
                          </Button>
                          <Button variant="danger" icon={Ban} onClick={() => runAction("cancel")}>
                            {t("convert.cancelTask")}
                          </Button>
                        </>
                      ) : null}
                      {currentTask.status === "paused" ? (
                        <>
                          <Button variant="primary" icon={Play} onClick={() => runAction("resume")}>
                            {t("convert.resume")}
                          </Button>
                          <Button variant="danger" icon={Ban} onClick={() => runAction("cancel")}>
                            {t("convert.cancelTask")}
                          </Button>
                        </>
                      ) : null}
                      {currentTask.status === "queued" ? (
                        <Button variant="danger" icon={Ban} onClick={() => runAction("cancel")}>
                          {t("convert.cancelTask")}
                        </Button>
                      ) : null}
                      {currentTask.status === "completed" ? (
                        <Button
                          variant="success"
                          icon={Download}
                          disabled={!currentTask.output_path}
                          onClick={() => handleDownload(currentTask)}
                        >
                          {t("convert.download")}
                        </Button>
                      ) : null}
                      <Button
                        variant="ghost"
                        icon={RefreshCw}
                        onClick={() => loadCurrentTask(currentTask.id)}
                      >
                        {t("common.refresh")}
                      </Button>
                    </div>
                  </div>
                </div>

                {currentTask.status === "failed" && currentTask.error_message ? (
                  <Alert tone="danger" title={t("convert.errorMessage")} className="u-mt-4">
                    <span className="u-sm u-break">{currentTask.error_message}</span>
                  </Alert>
                ) : null}

                {currentTask.status === "completed" ? (
                  <DetailList
                    className="u-mt-4"
                    items={[
                      { label: t("convert.output"), value: currentTask.output_format.toUpperCase() },
                      {
                        label: t("convert.filePath"),
                        value: currentTask.output_path || "—",
                        mono: true,
                      },
                    ]}
                  />
                ) : null}
              </Card>
            ) : null}

            {taskId && !currentTask ? (
              <Card>
                <EmptyState
                  icon={FileAudio}
                  title={t("convert.notFound")}
                  desc={t("convert.notFoundDesc")}
                  actions={
                    <Button variant="primary" icon={ArrowLeft} onClick={() => navigate("/convert")}>
                      {t("common.back")}
                    </Button>
                  }
                />
              </Card>
            ) : null}

            {!taskId ? (
              <Card title={t("convert.newTitle")} desc={t("convert.newDesc")} icon={Play}>
                <StackForm>
                  <Field label={t("convert.project")} htmlFor="convert-project">
                    <Select
                      id="convert-project"
                      value={selectedProjectId}
                      placeholder={t("convert.projectPlaceholder")}
                      options={projectOptions}
                      onChange={(event) => setSelectedProjectId(event.target.value)}
                    />
                  </Field>
                  <Field label={t("convert.outputFormat")}>
                    <Segmented
                      name="convert-output-format"
                      value={outputFormat}
                      options={formatOptions}
                      onChange={setOutputFormat}
                    />
                  </Field>
                  <Field label={t("ttsMode.label")}>
                    <TtsModeToggle
                      name="convert-tts-tier"
                      value={ttsTier}
                      onChange={setTtsTier}
                    />
                  </Field>
                  <div>
                    <Button
                      variant="primary"
                      icon={Play}
                      loading={starting}
                      disabled={!selectedProjectId}
                      onClick={handleStartConvert}
                    >
                      {starting ? t("convert.starting") : t("convert.startBtn")}
                    </Button>
                  </div>
                </StackForm>
                {projects.length === 0 ? (
                  <Alert tone="info" className="u-mt-4" title={t("convert.noProjects")} />
                ) : null}
              </Card>
            ) : null}

            <div>
              <SectionTitle
                actions={
                  <span className="hint u-num">
                    {t("common.total")} {format.number(tasks.length)}
                  </span>
                }
              >
                {t("convert.taskList")}
              </SectionTitle>
              <Card body="none">
                {tasks.length === 0 ? (
                  <EmptyState
                    icon={History}
                    title={t("convert.emptyTasks")}
                    desc={t("convert.emptyTasksDesc")}
                  />
                ) : (
                  <ResponsiveTable
                    columns={columns}
                    rows={tasks}
                    rowKey={(task) => task.id}
                    ariaLabel={t("convert.taskList")}
                    isActive={(task) => currentTask?.id === task.id}
                    onRowClick={(task) => navigate(`/convert/${task.id}`)}
                  />
                )}
              </Card>
              <p className="hint u-mt-2">{t("convert.taskListDesc")}</p>
            </div>
          </>
        )}
      </div>
    </Page>
  );
}
