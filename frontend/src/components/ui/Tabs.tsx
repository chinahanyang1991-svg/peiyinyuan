import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cx } from "../../lib/cx";

export interface TabItem {
  id: string;
  label: ReactNode;
  icon?: LucideIcon;
  count?: number;
  badge?: ReactNode;
}

export interface TabsProps {
  items: TabItem[];
  active: string;
  onChange: (id: string) => void;
  variant?: "underline" | "pill";
  className?: string;
  ariaLabel?: string;
}

export function Tabs({ items, active, onChange, variant = "underline", className, ariaLabel }: TabsProps) {
  return (
    <div
      className={cx("tabs", variant === "pill" && "tabs--pill", className)}
      role="tablist"
      aria-label={ariaLabel}
    >
      {items.map((item) => {
        const isActive = item.id === active;
        const Icon = item.icon;
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`tab-${item.id}`}
            aria-selected={isActive}
            aria-controls={`panel-${item.id}`}
            className={cx("tab", isActive && "tab--active")}
            onClick={() => onChange(item.id)}
          >
            {Icon ? <Icon size={15} aria-hidden="true" /> : null}
            <span>{item.label}</span>
            {typeof item.count === "number" ? (
              <span className="tab__count">{item.count}</span>
            ) : null}
            {item.badge}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  id,
  active,
  children,
}: {
  id: string;
  active: string;
  children: ReactNode;
}) {
  if (id !== active) return null;
  return (
    <div className="tab-panel" role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`}>
      {children}
    </div>
  );
}

export function TabPanels({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("tab-panels", className)}>{children}</div>;
}
