/**
 * 作品库（UI-100 阶段3 重制）
 * 契约保持：
 *  - taskApi.list / taskApi.delete 与 /api/tasks/{id}/download 下载入口不变；
 *  - 状态（已完成/失败/其他）筛选、格式筛选、时间排序、体积/时长预估逻辑不变；
 *  - 任务详情可查看并跳转 /convert/:taskId 跟进度。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AudioLines,
  Clock3,
  ExternalLink,
  FileAudio,
  HardDrive,
  Inbox,
  LibraryBig,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import { taskApi } from "../services/api";
import { useI18n } from "../i18n";
import { useToast } from "../components/ui/Toast";
import { useConfirm } from "../components/ui/Confirm";

import {
  Alert,
  Badge,
  Button,
  Card,
  DetailList,
  EmptyState,
  Input,
  Modal,
  Page,
  PageHeader,
  Select,
  Skeleton,
  Stat,
  StatRow,
  StatusBadge,
  Toolbar,
  ToolbarDivider,
  ToolbarSpacer,
} from "../components/ui";

interface Task {
  id: string;
  project_id: string;
  book_id: string;
  status: string;
  progress: number;
  current_stage: string;
  output_format: string;
  output_path: string;
  output_size_bytes: number | null;
  total_chars: number;
  processed_chars: number;
  error_message: string;
  elapsed_seconds: number;
  settings?: { task_type?: string } | null;
  started_at: string;
  completed_at: string;
  created_at: string;
}

/** 按字数预估体积的旧口径（bytes-per-char），**只在拿不到真实字节数时兜底**，
 *  且必须以"预估"字样出口 —— 见 sizeOf。 */
const BYTES_PER_CHAR: Record<string, number> = {
  m4b: 800,
  mp3: 1200,
  wav: 8000,
  flac: 4000,
};

const FORMATS = ["M4B", "MP3", "WAV", "FLAC"] as const;

type StatusChoice = "" | "completed" | "failed" | "other";
type SortChoice = "newest" | "oldest";

export default function LibraryPage() {
  const { t, tEnum, format } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();
  const navigate = useNavigate();

  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const [search, setSearch] = useState("");
  const [statusChoice, setStatusChoice] = useState<StatusChoice>("");
  const [formatChoice, setFormatChoice] = useState("");
  const [sort, setSort] = useState<SortChoice>("newest");

  const [selected, setSelected] = useState<Task | null>(null);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const response = await taskApi.list();
      setTasks(response.data.tasks ?? []);
    } catch {
      setError(true);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  const handleDelete = async (task: Task) => {
    const ok = await confirm({
      message: t("library.deleteConfirm"),
      tone: "danger",
      confirmLabel: t("common.delete"),
    });
    if (!ok) return;
    try {
      await taskApi.delete(task.id);
      setTasks((prev) => prev.filter((item) => item.id !== task.id));
      setSelected((prev) => (prev?.id === task.id ? null : prev));
      toast.success(t("common.deleted"));
    } catch {
      toast.error(t("common.deleteFailed"));
    }
  };

  const handleDownload = (task: Task) => {
    window.open(`/api/tasks/${task.id}/download`, "_blank");
  };

  const filtered = useMemo(() => {
    const word = search.trim().toLowerCase();
    return tasks
      .filter((task) => {
        // 角色分析任务不是"作品"：它不产音频，列在这里等于把一条分析记录摆成成品
        // ——体积只能是按字数编出来的估算，点下载必定 404。
        if (task.settings?.task_type === "cast_analysis") return false;
        if (word) {
          const hay = `${task.project_id ?? ""} ${task.book_id ?? ""} ${task.id}`.toLowerCase();
          if (!hay.includes(word)) return false;
        }
        if (statusChoice === "completed" && task.status !== "completed") return false;
        if (statusChoice === "failed" && task.status !== "failed") return false;
        if (statusChoice === "other" && (task.status === "completed" || task.status === "failed"))
          return false;
        if (formatChoice && task.output_format?.toUpperCase() !== formatChoice) return false;
        return true;
      })
      .sort((a, b) => {
        const ta = new Date(a.created_at).getTime() || 0;
        const tb = new Date(b.created_at).getTime() || 0;
        return sort === "newest" ? tb - ta : ta - tb;
      });
  }, [tasks, search, statusChoice, formatChoice, sort]);

  /** 体积先读后端给的**真实字节数**（产物文件 stat），读不到才按字数估 (作品库两处失真之一)。
   *  `estimated` 一路带到出口：估算值不许长得像事实。 */
  const hasProduct = (task: Task) => Boolean(task.output_size_bytes);
  const isSettled = (task: Task) =>
    task.status === "completed" || task.status === "failed" || task.status === "cancelled";

  const sizeOf = (task: Task) => {
    if (task.output_size_bytes) return { bytes: task.output_size_bytes, estimated: false };
    // 已经收尾却没有产物文件的任务不给任何体积数字：跑出来的"预估"会被读成"这本有多少兆"，
    // 而它其实对应一个都不存在的文件（失败的、只做过角色分析的）。
    if (isSettled(task)) return { bytes: 0, estimated: false };
    const chars = task.total_chars || task.processed_chars || 0;
    if (!chars) return { bytes: 0, estimated: false };
    return { bytes: chars * (BYTES_PER_CHAR[task.output_format] || 1000), estimated: true };
  };

  const sizeText = (task: Task) => {
    const size = sizeOf(task);
    if (!size.bytes) return "—";
    const text = format.bytes(size.bytes);
    return size.estimated ? `${t("common.estimated")} ${text}` : text;
  };

  /** 一处字数的显示口径：没解析过正文的任务 `total_chars` 是 0，那不等于"0 字的书"。 */
  const charText = (chars: number) =>
    chars ? `${format.number(chars)} ${t("common.charsUnit")}` : "—";

  const stats = useMemo(() => {
    const completed = tasks.filter(
      (task) => task.status === "completed" && task.settings?.task_type !== "cast_analysis",
    );
    const totalDuration = completed.reduce((sum, task) => sum + (task.elapsed_seconds || 0), 0);
    let totalBytes = 0;
    let bytesEstimated = false;
    for (const task of completed) {
      const size = sizeOf(task);
      totalBytes += size.bytes;
      bytesEstimated = bytesEstimated || size.estimated;
    }
    return { count: completed.length, totalDuration, totalBytes, bytesEstimated };
  }, [tasks]);

  const resetFilters = () => {
    setSearch("");
    setStatusChoice("");
    setFormatChoice("");
  };

  const hasFilters = Boolean(search || statusChoice || formatChoice);
  const isEmptyLibrary = !loading && !error && tasks.length === 0;

  return (
    <Page>
      <PageHeader
        title={t("library.title")}
        subtitle={t("library.subtitle")}
        actions={
          <Button variant="secondary" icon={RefreshCw} onClick={loadTasks} loading={loading}>
            {t("common.refresh")}
          </Button>
        }
      />

      <StatRow>
        <Stat icon={LibraryBig} value={format.number(stats.count)} label={t("library.statCompleted")} />
        <Stat
          icon={Clock3}
          value={stats.totalDuration ? format.seconds(stats.totalDuration) : "—"}
          label={t("library.statDuration")}
        />
        <Stat
          icon={HardDrive}
          value={stats.totalBytes ? format.bytes(stats.totalBytes) : "—"}
          label={t("library.statSize")}
          hint={stats.bytesEstimated ? t("common.estimated") : undefined}
        />
      </StatRow>

      <Toolbar>
        <div className="u-row u-gap-2" style={{ flex: "1 1 14rem", minWidth: 0 }}>
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("library.searchPlaceholder")}
            aria-label={t("library.searchPlaceholder")}
            icon={Search}
            className="u-w-full"
          />
        </div>
        <ToolbarSpacer />
        <Select
          aria-label={t("library.filterStatus")}
          controlSize="sm"
          value={statusChoice}
          onChange={(e) => setStatusChoice(e.target.value as StatusChoice)}
          placeholder={t("library.filterStatus")}
          options={[
            { value: "completed", label: t("library.statusCompleted") },
            { value: "failed", label: t("library.statusFailed") },
            { value: "other", label: t("library.statusOther") },
          ]}
        />
        <Select
          aria-label={t("library.filterFormat")}
          controlSize="sm"
          value={formatChoice}
          onChange={(e) => setFormatChoice(e.target.value)}
          placeholder={t("library.filterFormat")}
          options={FORMATS.map((f) => ({ value: f, label: f }))}
        />
        <ToolbarDivider />
        <Select
          aria-label={t("library.sortNewest")}
          controlSize="sm"
          value={sort}
          onChange={(e) => setSort(e.target.value as SortChoice)}
          options={[
            { value: "newest", label: t("library.sortNewest") },
            { value: "oldest", label: t("library.sortOldest") },
          ]}
        />
      </Toolbar>

      {error ? (
        <Alert
          tone="danger"
          title={t("common.loadFailed")}
          action={
            <Button size="sm" variant="secondary" onClick={loadTasks}>
              {t("common.retry")}
            </Button>
          }
        >
          {t("common.networkError")}
        </Alert>
      ) : null}

      {loading ? (
        <div className="library-grid">
          {Array.from({ length: 6 }, (_, i) => (
            <Card key={i} plain>
              <Skeleton height={16} width="60%" />
              <div style={{ height: 10 }} />
              <Skeleton height={12} />
              <div style={{ height: 8 }} />
              <Skeleton height={12} width="70%" />
            </Card>
          ))}
        </div>
      ) : isEmptyLibrary ? (
        <Card plain style={{ borderStyle: "dashed" }}>
          <EmptyState
            icon={Inbox}
            title={t("library.emptyTitle")}
            desc={t("library.emptyDesc")}
            actions={
              <>
                <Button variant="primary" icon={FileAudio} onClick={() => navigate("/import")}>
                  {t("library.emptyCtaImport")}
                </Button>
                <Button variant="secondary" onClick={() => navigate("/convert")}>
                  {t("library.emptyCtaConvert")}
                </Button>
              </>
            }
          />
        </Card>
      ) : filtered.length === 0 ? (
        <Card plain style={{ borderStyle: "dashed" }}>
          <EmptyState
            icon={Search}
            title={t("library.noMatchTitle")}
            desc={t("library.noMatchDesc")}
            actions={
              hasFilters ? (
                <Button variant="secondary" onClick={resetFilters}>
                  {t("library.resetFilters")}
                </Button>
              ) : null
            }
          />
        </Card>
      ) : (
        <div className="library-grid">
          {filtered.map((task) => (
            <Card key={task.id} className="work-card" body="tight">
              <div className="work-card__cover">
                <span className="stat__icon" aria-hidden="true">
                  <AudioLines size={18} />
                </span>
                <StatusBadge status={task.status} />
              </div>
              <p className="work-card__title">
                {t("library.cardTitle", { id: task.id.slice(0, 8) })}
              </p>
              <div className="work-card__meta">
                <span className="work-card__stat">
                  <Badge tone="neutral">{(task.output_format || "—").toUpperCase()}</Badge>
                </span>
                <span className="work-card__stat">
                  {charText(task.total_chars || task.processed_chars || 0)}
                </span>
                <span className="work-card__stat">{sizeText(task)}</span>
              </div>
              <p className="hint u-truncate">{task.book_id || task.project_id || task.id}</p>
              <div className="work-card__actions">
                <Button size="sm" variant="secondary" onClick={() => setSelected(task)}>
                  {t("library.detailTitle")}
                </Button>
                {task.status === "completed" && hasProduct(task) ? (
                  <Button size="sm" variant="primary" icon={HardDrive} onClick={() => handleDownload(task)}>
                    {t("common.download")}
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  aria-label={t("common.delete")}
                  onClick={() => handleDelete(task)}
                />
              </div>
            </Card>
          ))}
        </div>
      )}

      <Modal
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={t("library.detailTitle")}
        desc={selected ? t("library.cardTitle", { id: selected.id.slice(0, 8) }) : undefined}
        width="34rem"
        footer={
          selected ? (
            <>
              <Button variant="ghost" onClick={() => setSelected(null)}>
                {t("common.close")}
              </Button>
              <Button variant="secondary" icon={ExternalLink} onClick={() => navigate(`/convert/${selected.id}`)}>
                {t("library.openTask")}
              </Button>
              {selected.status === "completed" && hasProduct(selected) ? (
                <Button variant="primary" icon={HardDrive} onClick={() => handleDownload(selected)}>
                  {t("common.download")}
                </Button>
              ) : null}
            </>
          ) : null
        }
      >
        {selected ? (
          <DetailList
            items={[
              { label: t("library.fieldTaskId"), value: selected.id, mono: true },
              { label: t("library.fieldProjectId"), value: selected.project_id || "—", mono: true },
              { label: t("library.fieldBookId"), value: selected.book_id || "—", mono: true },
              {
                label: t("library.fieldStatus"),
                value: <StatusBadge status={selected.status} size="lg" />,
              },
              {
                label: t("library.fieldProgress"),
                value: format.percent((selected.progress || 0) / 100),
              },
              {
                label: t("library.fieldStage"),
                value: tEnum("stage", selected.current_stage, "—"),
              },
              {
                label: t("library.fieldFormat"),
                value: (selected.output_format || "—").toUpperCase(),
              },
              { label: t("library.fieldPath"), value: selected.output_path || "—", mono: true },
              {
                label: sizeOf(selected).estimated
                  ? t("library.fieldEstSize")
                  : t("library.fieldSize"),
                value: sizeText(selected),
              },
              {
                label: t("library.fieldTotalChars"),
                value: selected.total_chars ? format.number(selected.total_chars) : "—",
              },
              {
                label: t("library.fieldProcessedChars"),
                value: selected.processed_chars ? format.number(selected.processed_chars) : "—",
              },
              { label: t("library.fieldElapsed"), value: format.seconds(selected.elapsed_seconds) },
              { label: t("library.fieldCreated"), value: format.dateTime(selected.created_at) },
              { label: t("library.fieldStarted"), value: format.dateTime(selected.started_at) },
              { label: t("library.fieldCompleted"), value: format.dateTime(selected.completed_at) },
              ...(selected.error_message
                ? [{ label: t("library.fieldError"), value: selected.error_message }]
                : []),
            ]}
          />
        ) : null}
      </Modal>
    </Page>
  );
}
