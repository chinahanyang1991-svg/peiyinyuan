import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { cx } from "../../lib/cx";
import { useI18n } from "../../i18n";

export type ToastTone = "success" | "error" | "warning" | "info";

export interface ToastItem {
  id: number;
  tone: ToastTone;
  title?: string;
  message?: string;
  duration: number;
  leaving?: boolean;
}

export interface ToastOptions {
  title?: string;
  message?: string;
  duration?: number;
}

interface ToastApi {
  show: (tone: ToastTone, options: ToastOptions) => number;
  success: (message: string, options?: Omit<ToastOptions, "message">) => number;
  error: (message: string, options?: Omit<ToastOptions, "message">) => number;
  warning: (message: string, options?: Omit<ToastOptions, "message">) => number;
  info: (message: string, options?: Omit<ToastOptions, "message">) => number;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const ICONS: Record<ToastTone, LucideIcon> = {
  success: CircleCheck,
  error: CircleAlert,
  warning: TriangleAlert,
  info: Info,
};

const DEFAULT_DURATION: Record<ToastTone, number> = {
  success: 3200,
  info: 3600,
  warning: 5000,
  error: 6500,
};

/** 错误文案长度不可控：长文本走 message，短标题走 title */
function normalizeErrorMessage(fallback: string, err: unknown): string {
  if (!err) return fallback;
  if (err instanceof Error && err.message) return `${fallback} — ${err.message}`;
  if (typeof err === "string") return `${fallback} — ${err}`;
  return fallback;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const closeLabel = t("common.close");
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const timers = useRef(new Map<number, number>());

  const remove = useCallback((id: number) => {
    setItems((prev) => prev.filter((item) => item.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const startLeave = useCallback(
    (id: number) => {
      setItems((prev) => prev.map((item) => (item.id === id ? { ...item, leaving: true } : item)));
      window.setTimeout(() => remove(id), 180);
    },
    [remove]
  );

  const dismiss = useCallback(
    (id: number) => {
      const timer = timers.current.get(id);
      if (timer !== undefined) window.clearTimeout(timer);
      startLeave(id);
    },
    [startLeave]
  );

  const show = useCallback(
    (tone: ToastTone, options: ToastOptions) => {
      seq.current += 1;
      const id = seq.current;
      const duration = options.duration ?? DEFAULT_DURATION[tone];
      setItems((prev) => {
        const next = [...prev, { id, tone, title: options.title, message: options.message, duration }];
        return next.length > 5 ? next.slice(next.length - 5) : next;
      });
      if (duration > 0) {
        timers.current.set(
          id,
          window.setTimeout(() => startLeave(id), duration)
        );
      }
      return id;
    },
    [startLeave]
  );

  useEffect(() => {
    const map = timers.current;
    return () => {
      map.forEach((timer) => clearTimeout(timer));
      map.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      show,
      dismiss,
      success: (message, options) => show("success", { ...options, message }),
      error: (message, options) => show("error", { ...options, message }),
      warning: (message, options) => show("warning", { ...options, message }),
      info: (message, options) => show("info", { ...options, message }),
    }),
    [show, dismiss]
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div className="toast-stack" role="region" aria-live="polite" aria-atomic="false">
          {items.map((item) => {
            const Icon = ICONS[item.tone];
            return (
              <div
                key={item.id}
                className={cx("toast", `toast--${item.tone}`, item.leaving && "toast--leaving")}
                role={item.tone === "error" ? "alert" : "status"}
              >
                <Icon size={17} className="toast__icon" aria-hidden="true" />
                <div className="toast__content">
                  {item.title ? <div className="toast__title">{item.title}</div> : null}
                  {item.message ? <div className="toast__msg">{item.message}</div> : null}
                </div>
                <button
                  type="button"
                  className="toast__close"
                  onClick={() => dismiss(item.id)}
                  aria-label={closeLabel}
                  title={closeLabel}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </div>
            );
          })}
        </div>,
        document.body
      )}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 <ToastProvider> 内使用");
  return ctx;
}

export { normalizeErrorMessage };
