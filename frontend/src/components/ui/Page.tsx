import { useCallback, useRef, useState, type ChangeEvent, type DragEvent, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { FileText, UploadCloud } from "lucide-react";
import { cx } from "../../lib/cx";
import { useI18n } from "../../i18n";

/* --------------------------------------------------------------------------
   Page / PageHeader
   -------------------------------------------------------------------------- */
export function Page({
  children,
  wide = false,
  flush = false,
  className,
}: {
  children: ReactNode;
  wide?: boolean;
  flush?: boolean;
  className?: string;
}) {
  return <div className={cx("page", wide && "page--wide", flush && "page--flush", className)}>{children}</div>;
}

export function PageHeader({
  title,
  subtitle,
  actions,
  breadcrumb,
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  breadcrumb?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cx("page-head", className)}>
      <div className="page-head__text">
        {breadcrumb}
        <h1 className="page-title">{title}</h1>
        {subtitle ? <p className="page-subtitle">{subtitle}</p> : null}
      </div>
      {actions ? <div className="page-head__actions">{actions}</div> : null}
    </header>
  );
}

export function SectionTitle({
  children,
  actions,
  className,
}: {
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("u-row u-center u-between u-wrap u-gap-3", className)}>
      <h2 className="section-title">{children}</h2>
      {actions}
    </div>
  );
}

/* --------------------------------------------------------------------------
   Toolbar / FilterRow / 表单布局
   -------------------------------------------------------------------------- */
export function Toolbar({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("toolbar", className)}>{children}</div>;
}

export function ToolbarSpacer() {
  return <span className="toolbar__spacer" />;
}

export function ToolbarDivider() {
  return <span className="toolbar__divider" aria-hidden="true" />;
}

export function FilterRow({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("filter-row", className)}>{children}</div>;
}

export function StackForm({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("stack-form", className)}>{children}</div>;
}

export function GridForm({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("grid-form", className)}>{children}</div>;
}

/* --------------------------------------------------------------------------
   Stat
   -------------------------------------------------------------------------- */
export function StatRow({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("stat-row", className)}>{children}</div>;
}

export function Stat({
  icon: Icon,
  value,
  label,
  hint,
  className,
}: {
  icon?: LucideIcon;
  value: ReactNode;
  label: ReactNode;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("stat", className)}>
      {Icon ? (
        <span className="stat__icon">
          <Icon size={18} aria-hidden="true" />
        </span>
      ) : null}
      <div className="stat__body">
        <span className="stat__value">{value}</span>
        <span className="stat__label">{label}</span>
        {hint ? <span className="hint u-xs">{hint}</span> : null}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------------
   DetailList
   -------------------------------------------------------------------------- */
export interface DetailRowDef {
  label: ReactNode;
  value: ReactNode;
  /** 等宽展示（ID / 路径） */
  mono?: boolean;
}

export function DetailList({ items, className }: { items: DetailRowDef[]; className?: string }) {
  return (
    <dl className={cx("detail-list", className)}>
      {items.map((item, index) => (
        <div
          key={typeof item.label === "string" ? item.label : index}
          className={cx("detail-row", item.mono && "detail-row--stack")}
        >
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/* --------------------------------------------------------------------------
   ResponsiveTable：桌面表格 / 手机卡片同一份列定义
   -------------------------------------------------------------------------- */
export interface TableColumn<T> {
  key: string;
  header: ReactNode;
  render: (row: T, index: number) => ReactNode;
  /** 手机上不出现在卡片里（如次要列） */
  hideOnMobile?: boolean;
  align?: "left" | "right" | "center";
}

export interface ResponsiveTableProps<T> {
  columns: Array<TableColumn<T>>;
  rows: T[];
  rowKey: (row: T, index: number) => string;
  onRowClick?: (row: T, index: number) => void;
  isActive?: (row: T, index: number) => boolean;
  ariaLabel?: string;
  className?: string;
}

export function ResponsiveTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  isActive,
  ariaLabel,
  className,
}: ResponsiveTableProps<T>) {
  return (
    <>
      <div className={cx("table-wrap", className)}>
        <table className="table">
          <caption className="sr-only">{ariaLabel}</caption>
          <thead>
            <tr>
              {columns.map((col) => (
                <th
                  key={col.key}
                  scope="col"
                  style={col.align === "right" ? { textAlign: "right" } : undefined}
                  className={cx(col.align === "right" && "table__actions")}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr
                key={rowKey(row, index)}
                data-clickable={onRowClick ? "true" : undefined}
                className={cx(isActive?.(row, index) && "is-active")}
                onClick={onRowClick ? () => onRowClick(row, index) : undefined}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={cx(col.align === "right" && "table__actions")}
                    style={col.align === "right" ? { textAlign: "right" } : undefined}
                  >
                    {col.render(row, index)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className={cx("table-cards", className)}>
        {rows.map((row, index) => (
          <dl
            key={rowKey(row, index)}
            className={cx("table-card", isActive?.(row, index) && "is-active")}
            onClick={onRowClick ? () => onRowClick(row, index) : undefined}
          >
            {columns
              .filter((col) => !col.hideOnMobile)
              .map((col) => (
                <div className="table-card__row" key={col.key}>
                  <dt>{col.header}</dt>
                  <dd>{col.render(row, index)}</dd>
                </div>
              ))}
          </dl>
        ))}
      </div>
    </>
  );
}

/* --------------------------------------------------------------------------
   Segmented（单选分段控制器）
   -------------------------------------------------------------------------- */
export interface SegmentedOption {
  value: string;
  label: ReactNode;
  desc?: ReactNode;
  icon?: LucideIcon;
  disabled?: boolean;
}

export function Segmented({
  name,
  value,
  options,
  onChange,
  className,
}: {
  name: string;
  value: string;
  options: SegmentedOption[];
  onChange: (value: string) => void;
  className?: string;
}) {
  return (
    <div className={cx("segmented", className)} role="radiogroup" aria-label={name}>
      {options.map((option) => {
        const active = option.value === value;
        const Icon = option.icon;
        return (
          <label
            key={option.value}
            className="segmented__item"
            data-active={active ? "true" : undefined}
          >
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={active}
              disabled={option.disabled}
              onChange={() => onChange(option.value)}
            />
            {Icon ? <Icon size={16} aria-hidden="true" /> : null}
            <span className="segmented__label">{option.label}</span>
            {option.desc ? <span className="segmented__desc">{option.desc}</span> : null}
          </label>
        );
      })}
    </div>
  );
}

/* --------------------------------------------------------------------------
   Dropzone（电子书 / 参考音频上传）
   -------------------------------------------------------------------------- */
export interface DropzoneProps {
  accept?: string;
  multiple?: boolean;
  onFiles: (files: File[]) => void;
  title: ReactNode;
  hint?: ReactNode;
  icon?: LucideIcon;
  /** filled：已选中文件的绿色态 */
  state?: "idle" | "filled";
  className?: string;
}

export function Dropzone({
  accept,
  multiple = false,
  onFiles,
  title,
  hint,
  icon: Icon = UploadCloud,
  state = "idle",
  className,
}: DropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  const take = useCallback(
    (list: FileList | null) => {
      if (!list || list.length === 0) return;
      onFiles(Array.from(list));
    },
    [onFiles]
  );

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setOver(false);
    take(event.dataTransfer?.files ?? null);
  };

  return (
    <div
      className={cx("dropzone", over && "is-over", state === "filled" && "is-filled", className)}
      role="button"
      tabIndex={0}
      onClick={() => inputRef.current?.click()}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDragOver={(event) => {
        event.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
    >
      <span className="dropzone__icon">
        <Icon size={20} aria-hidden="true" />
      </span>
      <span className="dropzone__title">{title}</span>
      {hint ? <span className="hint">{hint}</span> : null}
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        multiple={multiple}
        className="sr-only"
        onChange={(event: ChangeEvent<HTMLInputElement>) => {
          take(event.target.files);
          event.target.value = "";
        }}
      />
    </div>
  );
}

/* --------------------------------------------------------------------------
   FileRow（待导入文件条目）
   -------------------------------------------------------------------------- */
export function FileRow({
  name,
  meta,
  status,
  actions,
  className,
}: {
  name: ReactNode;
  meta?: ReactNode;
  status?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("file-row", className)}>
      <span className="file-row__icon">
        <FileText size={16} aria-hidden="true" />
      </span>
      <span className="file-row__body">
        <span className="file-row__name">{name}</span>
        {meta ? <span className="file-row__meta">{meta}</span> : null}
      </span>
      {status}
      {actions}
    </div>
  );
}

/* --------------------------------------------------------------------------
   WizardNav（向导底栏）
   -------------------------------------------------------------------------- */
export function WizardNav({
  left,
  children,
  className,
}: {
  left?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("wizard-nav", className)}>
      <div className="btn-group">{left}</div>
      <div className="btn-group">{children}</div>
    </div>
  );
}

/* --------------------------------------------------------------------------
   AudioRow（试听播放器）
   -------------------------------------------------------------------------- */
export function AudioRow({ src, title }: { src: string; title?: string }) {
  const { t } = useI18n();
  return (
    <div className="audio-row">
      {/* 原生控件保证跨浏览器可用，不引入额外播放器依赖 */}
      <audio src={src} controls preload="none" title={title ?? t("common.preview")} />
    </div>
  );
}
