import type { HTMLAttributes, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cx } from "../../lib/cx";

export interface CardProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  title?: ReactNode;
  desc?: ReactNode;
  icon?: LucideIcon;
  actions?: ReactNode;
  footer?: ReactNode;
  /** 内容区内边距：pad（默认）/ tight / none */
  body?: "pad" | "tight" | "none";
  interactive?: boolean;
  flush?: boolean;
  plain?: boolean;
  /** 直接用 children 当 body 之外的完整结构时置为 true */
  bare?: boolean;
}

export function Card({
  title,
  desc,
  icon: Icon,
  actions,
  footer,
  body = "pad",
  interactive = false,
  flush = false,
  plain = false,
  bare = false,
  className,
  children,
  ...rest
}: CardProps) {
  const hasHead = Boolean(title || desc || actions || Icon);
  return (
    <div
      className={cx(
        "card",
        interactive && "card--interactive",
        flush && "card--flush",
        plain && "card--plain",
        className
      )}
      {...rest}
    >
      {bare ? (
        children
      ) : (
        <>
          {hasHead ? (
            <div className="card__head">
              <div style={{ minWidth: 0 }}>
                <div className="card__title">
                  {Icon ? <Icon size={16} aria-hidden="true" /> : null}
                  <span style={{ minWidth: 0 }}>{title}</span>
                </div>
                {desc ? <p className="card__desc">{desc}</p> : null}
              </div>
              {actions ? <div className="card__actions">{actions}</div> : null}
            </div>
          ) : null}
          <div className={cx("card__body", body !== "pad" && `card__body--${body}`)}>{children}</div>
          {footer ? <div className="card__foot">{footer}</div> : null}
        </>
      )}
    </div>
  );
}

export function CardSection({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("card__section", className)}>
      {title || actions ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "var(--space-3)" }}>
          <h3 className="card__title" style={{ fontSize: "var(--text-sm)" }}>
            {title}
          </h3>
          {actions ? <div className="card__actions">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}

export function CardGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("card-grid", className)}>{children}</div>;
}
