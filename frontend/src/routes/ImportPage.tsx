/**
 * 导入电子书（UI-100 阶段3 重制）
 * 契约保持：bookApi.import 逐文件导入；全部成功后跳 /edit/{bookId}；
 * 支持格式清单镜像后端 ParserFactory.FORMAT_MAP（.chm 自 PY-102 起无解析器故不列），
 * 漂移由 tests/test_wf20_markdown.py 的跨层契约断言拦住。
 */
import { useCallback, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertTriangle, ArrowRight, FileUp, Trash2, UploadCloud, X } from "lucide-react";
import { bookApi } from "../services/api";
import { useI18n } from "../i18n";
import { useToast } from "../components/ui/Toast";
import {
  Alert,
  Badge,
  Button,
  Card,
  FileRow,
  Hint,
  Page,
  PageHeader,
  ProgressBar,
  StackForm,
} from "../components/ui";
import { Dropzone } from "../components/ui/Page";

const SUPPORTED_FORMATS = [
  ".epub", ".mobi", ".azw3", ".pdf", ".txt", ".md", ".html", ".htm", ".docx",
];

interface ImportResult {
  success: boolean;
  name: string;
  error?: string;
  bookId?: string;
}

// 源文件自带乱码 (PY-326)：导入成功但原文就是脏的，必须让用户看见计数
interface QualityNotice {
  name: string;
  fffd: number;
  box: number;
  control: number;
  dbl: number;
}

export default function ImportPage() {
  const { t, format } = useI18n();
  const toast = useToast();
  const navigate = useNavigate();

  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState({ current: 0, total: 0, name: "" });
  const [failed, setFailed] = useState<ImportResult[]>([]);
  const [notices, setNotices] = useState<QualityNotice[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  const isValid = (file: File) =>
    SUPPORTED_FORMATS.some((ext) => file.name.toLowerCase().endsWith(ext));

  const addFiles = useCallback((incoming: File[]) => {
    const accepted = incoming.filter(isValid);
    if (accepted.length > 0) setFiles((prev) => [...prev, ...accepted]);
  }, []);

  const totalSize = files.reduce((sum, file) => sum + file.size, 0);

  const handleUpload = async () => {
    if (files.length === 0) return;
    setUploading(true);
    setFailed([]);
    setNotices([]);
    setProgress({ current: 0, total: files.length, name: "" });

    const results: ImportResult[] = [];
    const found: QualityNotice[] = [];
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      setProgress({ current: i + 1, total: files.length, name: file.name });
      try {
        const res = await bookApi.import(file);
        const book = res.data?.book;
        results.push({ success: true, name: file.name, bookId: book?.id });
        const quality = book?.extra_data?.text_quality;
        if (quality?.dirty) {
          const s = quality.stats || {};
          found.push({
            name: file.name,
            fffd: Number(s.fffd) || 0,
            box: Number(s.cjk_box) || 0,
            control: Number(s.control_char) || 0,
            dbl: Number(s.double_encoding) || 0,
          });
        }
      } catch (err: unknown) {
        const detail =
          (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          (err instanceof Error ? err.message : "");
        results.push({ success: false, name: file.name, error: detail });
      }
    }

    setUploading(false);
    const failures = results.filter((r) => !r.success);
    setFailed(failures);
    setNotices(found);
    setFiles((prev) => prev.filter((_, i) => results[i]?.success));

    if (failures.length > 0) {
      toast.warning(t("import.doneSome", { ok: results.length - failures.length, failed: failures.length }));
    } else {
      if (found.length > 0) {
        toast.warning(t("import.dirtyToast", { count: found.length }));
      } else {
        toast.success(t("import.doneAll", { count: results.length }));
      }
      const first = results.find((r) => r.success && r.bookId);
      if (first?.bookId) navigate(`/edit/${first.bookId}`);
    }
  };

  return (
    <Page>
      <PageHeader title={t("import.title")} subtitle={t("import.subtitle")} />

      <div style={{ display: "grid", gap: "var(--space-4)" }}>
        <Dropzone
          accept={SUPPORTED_FORMATS.join(",")}
          multiple
          onFiles={addFiles}
          state={files.length > 0 ? "filled" : "idle"}
          title={t("import.dropTitle")}
          hint={t("import.dropHint")}
          icon={UploadCloud}
        />

        {files.length > 0 || uploading ? (
          <Card
            title={t("import.selected", { count: files.length, size: format.bytes(totalSize) })}
            actions={
              <Button
                size="sm"
                variant="ghost"
                icon={Trash2}
                disabled={uploading}
                onClick={() => setFiles([])}
              >
                {t("import.clearAll")}
              </Button>
            }
          >
            <StackForm>
              <div style={{ display: "grid", gap: "var(--space-2)" }}>
                {files.map((file, i) => (
                  <FileRow
                    key={`${file.name}-${i}`}
                    name={file.name}
                    meta={format.bytes(file.size)}
                    actions={
                      <Button
                        size="xs"
                        variant="ghost"
                        icon={X}
                        aria-label={t("common.remove")}
                        disabled={uploading}
                        onClick={() => setFiles((prev) => prev.filter((_, idx) => idx !== i))}
                      />
                    }
                  />
                ))}
              </div>

              {uploading ? (
                <div>
                  <ProgressBar
                    value={progress.current}
                    max={progress.total || 1}
                    label={t("import.working", { current: progress.current, total: progress.total })}
                  />
                  <Hint className="u-mt-1">{t("import.workingName", { name: progress.name })}</Hint>
                </div>
              ) : null}

              <div>
                <Button
                  variant="primary"
                  size="lg"
                  icon={FileUp}
                  loading={uploading}
                  disabled={files.length === 0}
                  onClick={handleUpload}
                >
                  {uploading ? t("common.searching") : t("import.start", { count: files.length })}
                </Button>
              </div>
            </StackForm>
          </Card>
        ) : null}

        {notices.length > 0 ? (
          <Alert
            tone="warning"
            icon={AlertTriangle}
            title={t("import.dirtyBadge")}
            action={
              <Button size="sm" variant="ghost" onClick={() => setNotices([])}>
                {t("common.close")}
              </Button>
            }
          >
            <Hint>{t("import.dirtyHint")}</Hint>
            <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "grid", gap: 2 }}>
              {notices.map((n, i) => (
                <li key={`${n.name}-${i}`}>
                  {n.name}
                  {" — "}
                  {t("import.dirtyCounts", {
                    fffd: n.fffd, box: n.box, control: n.control, dbl: n.dbl,
                  })}
                </li>
              ))}
            </ul>
          </Alert>
        ) : null}

        {failed.length > 0 ? (
          <Alert
            tone="warning"
            icon={AlertTriangle}
            title={t("import.failedBadge")}
            action={
              <Button size="sm" variant="ghost" onClick={() => setFailed([])}>
                {t("common.close")}
              </Button>
            }
          >
            <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "grid", gap: 2 }}>
              {failed.map((f, i) => (
                <li key={`${f.name}-${i}`}>
                  {f.name}
                  {f.error ? ` — ${f.error}` : ""}
                </li>
              ))}
            </ul>
          </Alert>
        ) : null}

        <Card title={t("import.tipTitle")} plain>
          <div style={{ display: "grid", gap: "var(--space-2)" }}>
            <Hint>{t("import.tip1")}</Hint>
            <Hint>{t("import.tip2")}</Hint>
            <Hint>{t("import.tip3")}</Hint>
            <div className="u-row u-gap-2 u-center u-mt-1">
              <Badge tone="neutral">{t("import.supported")}</Badge>
              {SUPPORTED_FORMATS.map((ext) => (
                <Badge key={ext} tone="info">
                  {ext.replace(".", "").toUpperCase()}
                </Badge>
              ))}
            </div>
            <div>
              <Button
                size="sm"
                variant="ghost"
                iconRight={ArrowRight}
                onClick={() => navigate("/edit")}
              >
                {t("import.jumpEdit")}
              </Button>
            </div>
          </div>
        </Card>
      </div>
    </Page>
  );
}
