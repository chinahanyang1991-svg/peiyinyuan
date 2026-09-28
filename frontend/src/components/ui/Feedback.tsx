import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Check,
  CircleAlert,
  CircleCheck,
  Inbox,
  Info,
  TriangleAlert,
  X,
} from "lucide-react";
import { clamp, cx } from "../../lib/cx";
import { cssVars } from "../../lib/style";
import { useI18n } from "../../i18n";
import type { Tone } from "./Badge";

/* --------------------------------------------------------------------------
   加载 / 骨架
   -------------------------------------------------------------------------- */
export function Spinner({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <span
      className={cx("spinner", className)}
      style={cssVars({ "spin-size": `${size}px` })}
      role="status"
      aria-hidden="true"
    />
  );
}

export function Skeleton({ height = 14, width, className }: { height?: number; width?: string | number; className?: string }) {
  return (
    <div
      className={cx("skeleton", className)}
      style={{ height: typeof height === "number" ? `${height}px` : height, width }}
      aria-hidden="true"
    />
  );
}

export function LoaderBlock({ children, spinSize }: { children?: ReactNode; spinSize?: number }) {
  return (
    <div className="loader-block">
      <Spinner size={spinSize ?? 24} />
      {children ? <span>{children}</span> : null}
    </div>
  );
}

/* --------------------------------------------------------------------------
   EmptyState
   -------------------------------------------------------------------------- */
export interface EmptyStateProps {
  icon?: LucideIcon;
  title: ReactNode;
  desc?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon = Inbox, title, desc, actions, className }: EmptyStateProps) {
  return (
    <div className={cx("empty", className)}>
      <div className="empty__art">
        <Icon size={22} aria-hidden="true" />
      </div>
      <h3 className="empty__title">{title}</h3>
      {desc ? <p className="empty__desc">{desc}</p> : null}
      {actions ? <div className="empty__actions">{actions}</div> : null}
    </div>
  );
}

/* --------------------------------------------------------------------------
   Alert / Hint
   -------------------------------------------------------------------------- */
const ALERT_ICONS: Record<Tone, LucideIcon> = {
  neutral: Info,
  accent: Info,
  info: Info,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleAlert,
};

export interface AlertProps {
  tone?: Tone;
  title?: ReactNode;
  icon?: LucideIcon | null;
  action?: ReactNode;
  onDismiss?: () => void;
  children?: ReactNode;
  className?: string;
}

export function Alert({ tone = "info", title, icon, action, onDismiss, children, className }: AlertProps) {
  const { t } = useI18n();
  const Icon = icon === null ? null : (icon ?? ALERT_ICONS[tone]);
  return (
    <div className={cx("alert", `alert--${tone}`, className)} role={tone === "danger" ? "alert" : "status"}>
      {Icon ? <Icon size={16} aria-hidden="true" /> : null}
      <div className="alert__body">
        {title ? <span className="alert__title">{title}</span> : null}
        {children ? <div>{children}</div> : null}
      </div>
      {action}
      {onDismiss ? (
        <button
          type="button"
          className="toast__close"
          onClick={onDismiss}
          aria-label={t("common.close")}
          title={t("common.close")}
        >
          <X size={14} aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}

export function Hint({
  tone = "neutral",
  children,
  className,
}: {
  tone?: "neutral" | "warning" | "danger";
  children: ReactNode;
  className?: string;
}) {
  return <p className={cx("hint", tone !== "neutral" && `hint--${tone}`, className)}>{children}</p>;
}

/* --------------------------------------------------------------------------
   Progress：条 / 环
   -------------------------------------------------------------------------- */
export interface ProgressBarProps {
  value: number;
  max?: number;
  tone?: "accent" | "success" | "warning" | "danger";
  thickness?: "thin" | "md" | "thick";
  label?: ReactNode;
  /** 右侧数值文案（已本地化） */
  valueText?: ReactNode;
  indeterminate?: boolean;
  className?: string;
}

export function ProgressBar({
  value,
  max = 100,
  tone = "accent",
  thickness = "md",
  label,
  valueText,
  indeterminate = false,
  className,
}: ProgressBarProps) {
  const pct = max > 0 ? clamp((value / max) * 100, 0, 100) : 0;
  return (
    <div
      className={cx(
        "progress",
        thickness !== "md" && `progress--${thickness}`,
        tone !== "accent" && `progress--${tone}`,
        indeterminate && "progress--indeterminate",
        className
      )}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : Math.round(pct)}
      aria-label={typeof label === "string" ? label : undefined}
    >
      {(label || valueText) && (
        <div className="progress__meta">
          <span>{label}</span>
          {valueText ? <span className="progress__value">{valueText}</span> : null}
        </div>
      )}
      <div className="progress__track">
        <div className="progress__fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export interface ProgressRingProps {
  value: number;
  max?: number;
  size?: number;
  stroke?: number;
  tone?: "accent" | "success" | "warning" | "danger" | "running";
  /** 中心主文案，默认显示百分比 */
  text?: ReactNode;
  sub?: ReactNode;
  ariaLabel?: string;
  className?: string;
}

export function ProgressRing({
  value,
  max = 100,
  size = 112,
  stroke = 8,
  tone = "accent",
  text,
  sub,
  ariaLabel,
  className,
}: ProgressRingProps) {
  const pct = max > 0 ? clamp((value / max) * 100, 0, 100) : 0;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c - (pct / 100) * c;

  return (
    <div
      className={cx("ring", tone !== "accent" && `ring--${tone}`, className)}
      style={{ width: size, height: size }}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct)}
      aria-label={ariaLabel}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          className="ring__track"
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
        />
        <circle
          className="ring__fill"
          cx={size / 2}
          cy={size / 2}
          r={r}
          strokeWidth={stroke}
          strokeDasharray={c}
          strokeDashoffset={offset}
        />
      </svg>
      <div className="ring__label">
        <span className="ring__pct">{text ?? `${Math.round(pct)}%`}</span>
        {sub ? <span className="ring__sub">{sub}</span> : null}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------------
   Steps（向导步骤条）
   -------------------------------------------------------------------------- */
export interface StepItem {
  id: string;
  label: ReactNode;
}

export interface StepsProps {
  items: StepItem[];
  activeId: string;
  /** 返回已完成步骤的判定；默认按“当前之前的都算完成” */
  isDone?: (id: string, index: number) => boolean;
  onSelect?: (id: string, index: number) => void;
  /** 尚未解锁的步骤禁用点击 */
  maxReachable?: number;
  className?: string;
}

export function Steps({ items, activeId, isDone, onSelect, maxReachable, className }: StepsProps) {
  const activeIndex = Math.max(
    0,
    items.findIndex((item) => item.id === activeId)
  );
  return (
    <div className={cx("steps", className)} role="list">
      {items.map((item, index) => {
        const isActive = index === activeIndex;
        const done = isDone ? isDone(item.id, index) : index < activeIndex;
        const locked = maxReachable !== undefined && index > maxReachable;
        return (
          <div key={item.id} style={{ display: "contents" }}>
            {index > 0 ? (
              <span className={cx("step-connector", index <= activeIndex && "is-done")} aria-hidden="true" />
            ) : null}
            <button
              type="button"
              role="listitem"
              className={cx("step", isActive && "step--current", done && "step--done")}
              aria-current={isActive ? "step" : undefined}
              disabled={locked || !onSelect}
              onClick={() => onSelect?.(item.id, index)}
            >
              <span className="step__no">{done ? <Check size={11} aria-hidden="true" /> : index + 1}</span>
              <span>{item.label}</span>
            </button>
          </div>
        );
      })}
    </div>
  );
}

/* --------------------------------------------------------------------------
   Pipeline（九阶段 / cast 分析阶段）
   -------------------------------------------------------------------------- */
export type PipelineState = "pending" | "current" | "done" | "failed";

export interface PipelineItem {
  id: string;
  label: ReactNode;
  state: PipelineState;
  hint?: ReactNode;
}

export function Pipeline({ items, className }: { items: PipelineItem[]; className?: string }) {
  return (
    <ol className={cx("pipeline", className)}>
      {items.map((item, index) => (
        <li key={item.id} className="pipeline__node" data-state={item.state}>
          <span className="pipeline__dot" aria-hidden="true">
            {item.state === "done" ? (
              <Check size={11} />
            ) : item.state === "failed" ? (
              <X size={11} />
            ) : (
              index + 1
            )}
          </span>
          <span className="pipeline__label">{item.label}</span>
          {item.hint ? <span className="pipeline__label u-num">{item.hint}</span> : null}
        </li>
      ))}
    </ol>
  );
}
