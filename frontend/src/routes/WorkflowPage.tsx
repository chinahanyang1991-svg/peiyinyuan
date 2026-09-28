/**
 * 工作流模板（UI-100 阶段3 重制）
 * 契约保持：GET /workflows/templates、DELETE /workflows/templates/{id}；
 * stages 数组 [{name,label}] 结构不变，label 优先、否则查 enums.stage。
 * U-5：模板不再是只读展示——选一本书后 POST /templates/{id}/run 起真实任务并跳进度页。
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Play, RefreshCw, Workflow as WorkflowIcon, Trash2 } from "lucide-react";
import api, { bookApi } from "../services/api";
import { useI18n } from "../i18n";
import { useToast } from "../components/ui/Toast";
import { useConfirm } from "../components/ui/Confirm";
import {
  Badge,
  BadgeStack,
  Button,
  Card,
  EmptyState,
  Field,
  Page,
  PageHeader,
  SectionTitle,
  Select,
  Toolbar,
} from "../components/ui";

interface Template {
  id: string;
  name: string;
  description?: string;
  stages: { name: string; label: string }[];
  engine: string;
  output_format: string;
}

interface BookOption {
  id: string;
  title?: string;
}

export default function WorkflowPage() {
  const { t, tEnum } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();
  const navigate = useNavigate();

  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [books, setBooks] = useState<BookOption[]>([]);
  const [bookId, setBookId] = useState("");
  const [runningId, setRunningId] = useState<string | null>(null);

  useEffect(() => {
    // 书列表只服务于"运行"的目标选择：取不到就在控制台留痕并把列表置空，
    // 但不弹错——模板浏览本身不该被它挡住。
    bookApi
      .list()
      .then((res) => setBooks(res.data.books ?? []))
      .catch((err) => {
        console.error(err);
        setBooks([]);
      });
  }, []);

  const loadTemplates = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get("/workflows/templates");
      setTemplates(res.data.templates ?? []);
    } catch {
      toast.error(t("common.networkError"));
    }
    setLoading(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t]);

  useEffect(() => {
    loadTemplates();
  }, [loadTemplates]);

  const handleRun = async (tpl: Template) => {
    if (!bookId) {
      toast.warning(t("workflow.runNeedBook"));
      return;
    }
    setRunningId(tpl.id);
    try {
      const res = await api.post(`/workflows/templates/${tpl.id}/run`, { book_id: bookId });
      const taskId: string | undefined = res.data?.task?.id;
      toast.success(t("workflow.runOk"));
      if (taskId) navigate(`/convert/${taskId}`);
    } catch (err) {
      // 后端的 detail 是人话（缺书 / 缺 ffmpeg / 格式不支持），比"失败"二字有用得多。
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
      toast.error(typeof detail === "string" ? detail : t("workflow.runFailed"));
    }
    setRunningId(null);
  };

  const handleDelete = async (tpl: Template) => {
    const ok = await confirm({
      message: t("workflow.deleteConfirm"),
      tone: "danger",
      confirmLabel: t("common.delete"),
    });
    if (!ok) return;
    try {
      await api.delete(`/workflows/templates/${tpl.id}`);
      toast.success(t("workflow.deleteOk"));
      loadTemplates();
    } catch {
      toast.error(t("workflow.deleteFailed"));
    }
  };

  return (
    <Page>
      <PageHeader
        title={t("workflow.title")}
        subtitle={t("workflow.subtitle")}
        actions={
          <Button variant="secondary" icon={RefreshCw} onClick={loadTemplates} loading={loading}>
            {t("common.refresh")}
          </Button>
        }
      />

      <Toolbar>
        <div style={{ flex: "1 1 16rem", minWidth: 0 }}>
          <Field label={t("workflow.bookLabel")}>
            <Select
              aria-label={t("workflow.bookLabel")}
              value={bookId}
              onChange={(e) => setBookId(e.target.value)}
              placeholder={t("workflow.bookPlaceholder")}
              options={books.map((b) => ({ value: b.id, label: b.title || b.id }))}
            />
          </Field>
        </div>
      </Toolbar>

      <SectionTitle>{t("workflow.listTitle")}</SectionTitle>

      {loading ? (
        <p className="hint">{t("common.loading")}</p>
      ) : templates.length === 0 ? (
        <Card plain style={{ borderStyle: "dashed" }}>
          <EmptyState
            icon={WorkflowIcon}
            title={t("workflow.emptyTitle")}
            desc={t("workflow.emptyDesc")}
          />
        </Card>
      ) : (
        <div style={{ display: "grid", gap: "var(--space-3)" }}>
          {templates.map((tpl) => (
            <Card
              key={tpl.id}
              title={tpl.name}
              desc={tpl.description}
              icon={WorkflowIcon}
              actions={
                <div className="btn-group">
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={Play}
                    loading={runningId === tpl.id}
                    disabled={runningId !== null && runningId !== tpl.id}
                    onClick={() => void handleRun(tpl)}
                  >
                    {t("workflow.runBtn")}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={Trash2}
                    aria-label={t("common.delete")}
                    onClick={() => handleDelete(tpl)}
                  />
                </div>
              }
            >
              <BadgeStack>
                <Badge tone="info">
                  {/* 模板可以不声明引擎（此时按当前配音模式定嗓），空串不是漏字段 */}
                  {t("workflow.engineLabel")}: {tpl.engine || t("workflow.engineByTier")}
                </Badge>
                <Badge tone="neutral">
                  {t("workflow.outputLabel")}: {(tpl.output_format || "—").toUpperCase()}
                </Badge>
              </BadgeStack>
              <div className="u-row u-gap-2 u-wrap u-mt-2">
                <span className="segment-label">{t("workflow.stagesLabel")}</span>
                <BadgeStack>
                  {(tpl.stages ?? []).map((s, i) => (
                    <Badge key={`${tpl.id}-${s.name}-${i}`} tone="accent">
                      {s.label || tEnum("stage", s.name, s.name)}
                    </Badge>
                  ))}
                </BadgeStack>
              </div>
            </Card>
          ))}
        </div>
      )}
    </Page>
  );
}
