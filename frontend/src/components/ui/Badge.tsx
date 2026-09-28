import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { X } from "lucide-react";
import { cx } from "../../lib/cx";

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";

export interface BadgeProps {
  tone?: Tone;
  size?: "sm" | "lg";
  dot?: boolean;
  icon?: LucideIcon;
  solid?: boolean;
  title?: string;
  children: ReactNode;
  className?: string;
}

export function Badge({
  tone = "neutral",
  size = "sm",
  dot = false,
  icon: Icon,
  solid = false,
  title,
  children,
  className,
}: BadgeProps) {
  return (
    <span
      className={cx("badge", tone !== "neutral" && `badge--${tone}`, size === "lg" && "badge--lg", solid && "badge--solid", className)}
      title={title}
    >
      {dot ? <span className="badge__dot" aria-hidden="true" /> : null}
      {Icon ? <Icon size={12} aria-hidden="true" /> : null}
      <span className="badge__text" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
        {children}
      </span>
    </span>
  );
}

export function BadgeStack({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("badge-stack", className)}>{children}</span>;
}

/** 可删除 chip（别名列表等） */
export interface ChipProps {
  children: ReactNode;
  onRemove?: () => void;
  removeLabel?: string;
  className?: string;
}

export function Chip({ children, onRemove, removeLabel, className }: ChipProps) {
  return (
    <span className={cx("chip", className)}>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{children}</span>
      {onRemove ? (
        <button
          type="button"
          className="chip__remove"
          onClick={onRemove}
          aria-label={removeLabel}
          title={removeLabel}
        >
          <X size={12} aria-hidden="true" />
        </button>
      ) : null}
    </span>
  );
}

export function ChipStack({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("chip-stack", className)}>{children}</span>;
}

/** 可点选中的关键词 chip（在线库推荐搜索词 / 类别筛选等） */
export function ChipButton({
  children,
  onClick,
  disabled,
  active = false,
  className,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  active?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={cx("chip-btn", active && "is-active", className)}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active || undefined}
    >
      {children}
    </button>
  );
}

export function Swatch({ color, className }: { color: string; className?: string }) {
  return <span className={cx("swatch", className)} style={{ background: color }} aria-hidden="true" />;
}
