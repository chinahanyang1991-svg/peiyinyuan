import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cx } from "../../lib/cx";
import { cssVars } from "../../lib/style";
import { useI18n } from "../../i18n";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  desc?: ReactNode;
  footer?: ReactNode;
  /** 底栏左右分开（左侧放提示文案） */
  footerSplit?: boolean;
  width?: string;
  flushBody?: boolean;
  /** 关闭按 ESC 退出 */
  disableEscape?: boolean;
  /** 点击遮罩是否关闭 */
  closeOnOverlay?: boolean;
  ariaLabel?: string;
  children: ReactNode;
}

export function Modal({
  open,
  onClose,
  title,
  desc,
  footer,
  footerSplit = false,
  width,
  flushBody = false,
  disableEscape = false,
  closeOnOverlay = true,
  ariaLabel,
  children,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const { t } = useI18n();
  const closeLabel = t("common.close");

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !disableEscape) {
        event.stopPropagation();
        onClose();
      }
      if (event.key === "Tab") {
        trapFocus(panelRef.current, event);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.classList.add("no-scroll");
    const previous = panelRef.current?.querySelector<HTMLElement>(
      "input:not([type=hidden]), select, textarea, button, [tabindex]:not([tabindex='-1'])"
    );
    previous?.focus();
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("no-scroll");
    };
  }, [open, onClose, disableEscape]);

  if (!open) return null;

  return createPortal(
    <div
      className="modal-overlay"
      onMouseDown={(event) => {
        if (closeOnOverlay && event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        style={width ? cssVars({ "modal-w": width }) : undefined}
      >
        {title ? (
          <div className="modal__head">
            <div style={{ minWidth: 0 }}>
              <h2 className="modal__title">{title}</h2>
              {desc ? <p className="card__desc">{desc}</p> : null}
            </div>
            <button
              type="button"
              className="modal__close"
              onClick={onClose}
              aria-label={closeLabel}
              title={closeLabel}
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="modal__close"
            onClick={onClose}
            aria-label={closeLabel}
            title={closeLabel}
            style={{ position: "absolute", top: "var(--space-3)", right: "var(--space-3)", zIndex: 1 }}
          >
            <X size={18} aria-hidden="true" />
          </button>
        )}
        <div className={cx("modal__body", flushBody && "modal__body--flush")}>{children}</div>
        {footer ? <div className={cx("modal__foot", footerSplit && "modal__foot--split")}>{footer}</div> : null}
      </div>
    </div>,
    document.body
  );
}

function trapFocus(panel: HTMLElement | null, event: KeyboardEvent): void {
  if (!panel) return;
  const focusables = Array.from(
    panel.querySelectorAll<HTMLElement>(
      "a[href], button:not(:disabled), input:not([type=hidden]):not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])"
    )
  ).filter((node) => node.offsetParent !== null || node.tagName === "INPUT");
  if (focusables.length === 0) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;

  if (event.shiftKey && (active === first || !panel.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  side?: "left" | "right";
  label: string;
  children: ReactNode;
}

/** 抽屉：移动端导航用 */
export function Drawer({ open, onClose, side = "left", label, children }: DrawerProps) {
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.classList.add("no-scroll");
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("no-scroll");
    };
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div className="drawer-overlay" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <div
        className={cx("drawer", side === "right" && "drawer--right")}
        role="dialog"
        aria-modal="true"
        aria-label={label}
      >
        {children}
      </div>
    </div>,
    document.body
  );
}
