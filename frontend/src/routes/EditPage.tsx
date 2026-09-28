/**
 * 文本编辑（UI-100 阶段3 重制）
 * 契约保持：bookApi.list/get/getChapters/getChapterContent/updateChapter/mergeChapters；
 * 无 bookId 时自动跳最近一本书；合并需 >=2 选中；改名回车即存；保存后本地字数联动。
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Check, Clapperboard, Merge, PenLine, Save, X } from "lucide-react";
import { bookApi } from "../services/api";
import { useI18n } from "../i18n";
import { useToast } from "../components/ui/Toast";
import {
  Badge,
  BadgeStack,
  Button,
  Card,
  Checkbox,
  EmptyState,
  Field,
  Hint,
  Input,
  LoaderBlock,
  Modal,
  Page,
  PageHeader,
} from "../components/ui";

interface Chapter {
  id: string;
  book_id?: string;
  title: string;
  index: number;
  char_count: number;
  content?: string;
  content_preview?: string;
}

interface Book {
  id: string;
  title: string;
  author: string;
  format: string;
  total_chars: number;
  chapter_count: number;
}

export default function EditPage() {
  const { bookId } = useParams<{ bookId: string }>();
  const navigate = useNavigate();
  const { t, format } = useI18n();
  const toast = useToast();

  const [book, setBook] = useState<Book | null>(null);
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [loading, setLoading] = useState(true);

  const [activeChapterId, setActiveChapterId] = useState<string | null>(null);
  const [chapterContent, setChapterContent] = useState("");
  const [contentLoading, setContentLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [lastSaved, setLastSaved] = useState("");

  const [editingChapter, setEditingChapter] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");

  const [selectedChapters, setSelectedChapters] = useState<Set<string>>(new Set());
  const [mergeTitle, setMergeTitle] = useState("");
  const [mergeOpen, setMergeOpen] = useState(false);
  const [merging, setMerging] = useState(false);

  const loadBook = useCallback(
    async (id: string) => {
      setLoading(true);
      try {
        const [bookRes, chaptersRes] = await Promise.all([
          bookApi.get(id),
          bookApi.getChapters(id),
        ]);
        setBook(bookRes.data.book);
        setChapters(chaptersRes.data.chapters ?? []);
      } catch {
        setBook(null);
        toast.error(t("common.loadFailed"));
      }
      setLoading(false);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [t]
  );

  useEffect(() => {
    if (!bookId) {
      (async () => {
        try {
          const res = await bookApi.list();
          const books = res.data.books ?? [];
          if (books.length > 0) {
            navigate(`/edit/${books[0].id}`, { replace: true });
          } else {
            setLoading(false);
          }
        } catch (err) {
          console.error("加载书籍列表失败:", err);
          // 只停 loading 会让页面看着"本来就是空的"，说不了话的后端必须留痕
          toast.error(t("common.loadFailed"));
          setLoading(false);
        }
      })();
      return;
    }
    loadBook(bookId);
  }, [bookId, loadBook, navigate, toast, t]);

  const openChapterEditor = async (chapterId: string) => {
    setActiveChapterId(chapterId);
    setContentLoading(true);
    setChapterContent("");
    try {
      const res = await bookApi.getChapterContent(chapterId);
      setChapterContent(res.data.content || "");
    } catch {
      toast.error(t("edit.loadContentFailed"));
    }
    setContentLoading(false);
  };

  const saveChapterContent = async () => {
    if (!activeChapterId) return;
    setSaving(true);
    try {
      await bookApi.updateChapter(activeChapterId, { content: chapterContent });
      setLastSaved(new Date().toLocaleTimeString());
      setChapters((prev) =>
        prev.map((ch) =>
          ch.id === activeChapterId ? { ...ch, char_count: chapterContent.length } : ch
        )
      );
      setBook((prev) => {
        if (!prev) return prev;
        const newTotal = chapters.reduce(
          (sum, ch) =>
            sum + (ch.id === activeChapterId ? chapterContent.length : ch.char_count),
          0
        );
        return { ...prev, total_chars: newTotal };
      });
      toast.success(t("common.saved"));
    } catch {
      toast.error(t("common.saveFailed"));
    }
    setSaving(false);
  };

  const handleRename = async (chapterId: string) => {
    const next = editTitle.trim();
    if (!next) {
      setEditingChapter(null);
      return;
    }
    try {
      await bookApi.updateChapter(chapterId, { title: next });
      setChapters((prev) => prev.map((ch) => (ch.id === chapterId ? { ...ch, title: next } : ch)));
      toast.success(t("common.saved"));
    } catch {
      toast.error(t("edit.renameFailed"));
    }
    setEditingChapter(null);
  };

  const toggleSelect = (chapterId: string) => {
    setSelectedChapters((prev) => {
      const next = new Set(prev);
      if (next.has(chapterId)) next.delete(chapterId);
      else next.add(chapterId);
      return next;
    });
  };

  const mergeChapters = async () => {
    if (!bookId || selectedChapters.size < 2) {
      toast.warning(t("edit.mergeNeedTwo"));
      return;
    }
    setMerging(true);
    try {
      const response = await bookApi.mergeChapters(
        bookId,
        Array.from(selectedChapters),
        mergeTitle.trim() || t("edit.mergeDefault")
      );
      const data = response.data || {};
      if (data.status === "ok") {
        await loadBook(bookId);
        setSelectedChapters(new Set());
        setMergeOpen(false);
        setMergeTitle("");
        toast.success(t("edit.mergeOk"));
      } else {
        toast.error(data.message ? `${t("edit.mergeFailed")} — ${data.message}` : t("edit.mergeFailed"));
      }
    } catch {
      toast.error(t("edit.mergeFailed"));
    }
    setMerging(false);
  };

  if (loading) {
    return (
      <Page>
        <LoaderBlock>{t("common.loading")}</LoaderBlock>
      </Page>
    );
  }

  if (!book) {
    return (
      <Page>
        <Card plain style={{ borderStyle: "dashed" }}>
          <EmptyState
            icon={PenLine}
            title={t("edit.noBookTitle")}
            desc={t("edit.noBookDesc")}
            actions={
              <Button variant="primary" onClick={() => navigate("/import")}>
                {t("edit.goImport")}
              </Button>
            }
          />
        </Card>
      </Page>
    );
  }

  const activeChapter = chapters.find((c) => c.id === activeChapterId);

  return (
    <Page wide>
      <PageHeader
        title={book.title}
        subtitle={t("edit.subtitle")}
        breadcrumb={
          <BadgeStack>
            <Badge tone="neutral">
              {t("edit.author")}: {book.author || t("edit.anonymous")}
            </Badge>
            <Badge tone="neutral">
              {t("edit.format")}: {(book.format || "—").toUpperCase()}
            </Badge>
            <Badge tone="accent">
              {t("edit.totalChars")}: {format.number(book.total_chars || 0)}
            </Badge>
            <Badge tone="info">
              {t("edit.chapters")}: {chapters.length}
            </Badge>
          </BadgeStack>
        }
        actions={
          <Button variant="primary" icon={Clapperboard} onClick={() => navigate(`/dubbing/new?bookId=${bookId}`)}>
            {t("edit.toDubbing")}
          </Button>
        }
      />

      <div className="edit-layout">
        <Card
          className="chapter-panel"
          title={t("edit.bookPanel")}
          desc={t("edit.chapterCount", { count: chapters.length })}
          body="tight"
          actions={
            <Button
              size="sm"
              variant="secondary"
              icon={Merge}
              disabled={selectedChapters.size < 2}
              onClick={() => setMergeOpen(true)}
            >
              {t("edit.mergeSelected", { count: selectedChapters.size })}
            </Button>
          }
        >
          <div className="chapter-scroll">
            {chapters.map((ch, idx) => (
              <div
                key={ch.id}
                className="chapter-item"
                data-active={activeChapterId === ch.id ? "true" : "false"}
                onClick={() => openChapterEditor(ch.id)}
              >
                <span onClick={(e) => e.stopPropagation()}>
                  <Checkbox
                    label=""
                    checked={selectedChapters.has(ch.id)}
                    onChange={() => toggleSelect(ch.id)}
                    aria-label={ch.title}
                  />
                </span>
                <span className="chapter-item__body">
                  {editingChapter === ch.id ? (
                    <span className="inline-edit" onClick={(e) => e.stopPropagation()}>
                      <Input
                        autoFocus
                        value={editTitle}
                        controlSize="sm"
                        onChange={(e) => setEditTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleRename(ch.id);
                          if (e.key === "Escape") setEditingChapter(null);
                        }}
                      />
                      <Button size="xs" variant="ghost" icon={Check} aria-label={t("common.confirm")} onClick={() => handleRename(ch.id)} />
                      <Button size="xs" variant="ghost" icon={X} aria-label={t("common.cancel")} onClick={() => setEditingChapter(null)} />
                    </span>
                  ) : (
                    <>
                      <span
                        className="chapter-item__title"
                        title={ch.title}
                        onClick={(e) => {
                          e.stopPropagation();
                          setEditingChapter(ch.id);
                          setEditTitle(ch.title);
                        }}
                      >
                        #{idx + 1} {ch.title}
                      </span>
                      <span className="chapter-item__meta">
                        <span>{t("edit.charCount", { count: format.number(ch.char_count || 0) })}</span>
                      </span>
                      <span className="chapter-item__preview">
                        {ch.content_preview?.slice(0, 100) || t("edit.selectHint")}
                      </span>
                    </>
                  )}
                </span>
              </div>
            ))}
            {chapters.length === 0 ? <EmptyState title={t("common.empty")} /> : null}
          </div>
        </Card>

        <Card
          className="editor-panel"
          title={activeChapter?.title || t("edit.title")}
          body="none"
          actions={
            activeChapterId ? (
              <BadgeStack>
                {lastSaved ? (
                  <Hint>
                    <span className="u-xs u-muted">{t("edit.savedAt", { time: lastSaved })}</span>
                  </Hint>
                ) : null}
                <Button
                  size="sm"
                  variant="primary"
                  icon={Save}
                  loading={saving}
                  disabled={contentLoading}
                  onClick={saveChapterContent}
                >
                  {saving ? t("common.saving") : t("edit.saveBtn")}
                </Button>
              </BadgeStack>
            ) : null
          }
        >
          {activeChapterId ? (
            contentLoading ? (
              <LoaderBlock spinSize={18}>{t("common.loading")}</LoaderBlock>
            ) : (
              <>
                <textarea
                  className="editor-area"
                  value={chapterContent}
                  onChange={(e) => setChapterContent(e.target.value)}
                  placeholder={t("edit.unedited")}
                  spellCheck={false}
                />
                <div className="card__foot u-row u-between">
                  <span className="hint u-num">
                    {t("edit.currentChars", { count: format.number(chapterContent.length) })}
                  </span>
                </div>
              </>
            )
          ) : (
            <div style={{ padding: "var(--space-8)" }}>
              <EmptyState
                icon={PenLine}
                title={t("edit.editorEmptyTitle")}
                desc={t("edit.editorEmptyDesc")}
              />
              <div className="u-mt-4" style={{ maxWidth: "28rem", marginInline: "auto" }}>
                <Hint className="u-mt-1">· {t("edit.tip1")}</Hint>
                <Hint className="u-mt-1">· {t("edit.tip2")}</Hint>
                <Hint className="u-mt-1">· {t("edit.tip3")}</Hint>
              </div>
            </div>
          )}
        </Card>
      </div>

      <Modal
        open={mergeOpen}
        onClose={() => setMergeOpen(false)}
        title={t("edit.mergeTitle", { count: selectedChapters.size })}
        desc={t("edit.mergeDesc")}
        width="28rem"
        footer={
          <>
            <Button variant="ghost" onClick={() => setMergeOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button variant="primary" loading={merging} onClick={mergeChapters}>
              {t("edit.mergeConfirm")}
            </Button>
          </>
        }
      >
        <Field label={t("edit.mergePlaceholder")}>
          <Input
            autoFocus
            value={mergeTitle}
            onChange={(e) => setMergeTitle(e.target.value)}
            placeholder={t("edit.mergeDefault")}
          />
        </Field>
      </Modal>
    </Page>
  );
}
