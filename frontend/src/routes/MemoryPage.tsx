/**
 * 项目记忆（UI-100 阶段3 重制）
 * 契约保持：GET /memory/、GET /memory/search?q=、POST /memory/、DELETE /memory/{id}。
 */
import { useCallback, useEffect, useState } from "react";
import { BookmarkPlus, Search, Trash2 } from "lucide-react";
import api from "../services/api";
import { useI18n } from "../i18n";
import { useToast } from "../components/ui/Toast";
import { useConfirm } from "../components/ui/Confirm";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Input,
  Page,
  PageHeader,
  SectionTitle,
  StackForm,
  Textarea,
  Toolbar,
} from "../components/ui";

interface Memory {
  id: string;
  title: string;
  content: string;
  memory_type: string;
  tags: string[];
  created_at: string;
}

export default function MemoryPage() {
  const { t, format } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();

  const [memories, setMemories] = useState<Memory[]>([]);
  const [search, setSearch] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const loadMemories = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get("/memory/");
      setMemories(res.data.memories ?? []);
    } catch {
      toast.error(t("common.networkError"));
    }
    setLoading(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t]);

  useEffect(() => {
    loadMemories();
  }, [loadMemories]);

  const handleSearch = async () => {
    const word = search.trim();
    if (!word) {
      loadMemories();
      return;
    }
    setLoading(true);
    try {
      const res = await api.get(`/memory/search?q=${encodeURIComponent(word)}`);
      setMemories(res.data.results ?? []);
    } catch {
      toast.error(t("common.networkError"));
    }
    setLoading(false);
  };

  const handleCreate = async () => {
    if (!title.trim()) {
      toast.warning(t("memory.createNeedTitle"));
      return;
    }
    setSaving(true);
    try {
      await api.post("/memory/", { title, content, tags: [] });
      setTitle("");
      setContent("");
      toast.success(t("memory.createOk"));
      loadMemories();
    } catch {
      toast.error(t("memory.createFailed"));
    }
    setSaving(false);
  };

  const handleDelete = async (memory: Memory) => {
    const ok = await confirm({
      message: t("memory.deleteConfirm"),
      tone: "danger",
      confirmLabel: t("common.delete"),
    });
    if (!ok) return;
    try {
      await api.delete(`/memory/${memory.id}`);
      toast.success(t("memory.deleteOk"));
      loadMemories();
    } catch {
      toast.error(t("memory.deleteFailed"));
    }
  };

  return (
    <Page>
      <PageHeader title={t("memory.title")} subtitle={t("memory.subtitle")} />

      <Card
        title={t("memory.createTitle")}
        icon={BookmarkPlus}
        style={{ maxWidth: "44rem" }}
      >
        <StackForm>
          <Field label={t("memory.titleLabel")}>
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={t("memory.titlePlaceholder")}
            />
          </Field>
          <Field label={t("memory.contentLabel")}>
            <Textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder={t("memory.contentPlaceholder")}
              rows={3}
            />
          </Field>
          <div>
            <Button
              variant="primary"
              icon={BookmarkPlus}
              loading={saving}
              disabled={!title.trim()}
              onClick={handleCreate}
            >
              {t("memory.submit")}
            </Button>
          </div>
        </StackForm>
      </Card>

      <SectionTitle className="u-mt-8" actions={<span className="hint">{t("memory.count", { count: memories.length })}</span>}>
        {t("memory.listTitle")}
      </SectionTitle>

      <Toolbar>
        <div className="u-row u-gap-2" style={{ flex: "1 1 14rem", minWidth: 0 }}>
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder={t("memory.searchPlaceholder")}
            aria-label={t("memory.searchPlaceholder")}
            icon={Search}
            className="u-w-full"
          />
          <Button variant="secondary" onClick={handleSearch}>
            {t("common.search")}
          </Button>
        </div>
      </Toolbar>

      {loading ? (
        <p className="hint">{t("common.loading")}</p>
      ) : memories.length === 0 ? (
        <Card plain style={{ borderStyle: "dashed" }}>
          <EmptyState icon={BookmarkPlus} title={t("memory.emptyTitle")} desc={t("memory.emptyDesc")} />
        </Card>
      ) : (
        <div style={{ display: "grid", gap: "var(--space-3)" }}>
          {memories.map((m) => (
            <Card key={m.id}>
              <div className="u-row u-center u-between u-gap-3 u-wrap">
                <div style={{ minWidth: 0, flex: "1 1 12rem" }}>
                  <p className="u-strong">{m.title}</p>
                  {m.content ? (
                    <p className="hint" style={{ marginTop: 2 }}>
                      {m.content.length > 200 ? `${m.content.slice(0, 200)}…` : m.content}
                    </p>
                  ) : null}
                  <div className="u-row u-gap-2 u-center u-wrap u-mt-2">
                    {m.memory_type ? <Badge tone="neutral">{m.memory_type}</Badge> : null}
                    {m.tags?.map((tag) => (
                      <Badge key={tag} tone="accent">
                        {tag}
                      </Badge>
                    ))}
                    <span className="hint u-num">{format.dateTime(m.created_at)}</span>
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  icon={Trash2}
                  aria-label={t("common.delete")}
                  onClick={() => handleDelete(m)}
                />
              </div>
            </Card>
          ))}
        </div>
      )}
    </Page>
  );
}
