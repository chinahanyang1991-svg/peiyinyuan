import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { Modal } from "./Modal";
import { Button } from "./Button";
import { useI18n } from "../../i18n";

export interface ConfirmOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** danger 时确认键使用危险色（删除类操作） */
  tone?: "neutral" | "danger" | "primary";
}

export type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/**
 * Promise 化的确认框，替代 window.confirm。
 * 页面写法： `if (!(await confirm({ message, tone: 'danger' }))) return;`
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolveRef = useRef<((ok: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>((next) => {
    return new Promise<boolean>((resolve) => {
      resolveRef.current?.(false);
      resolveRef.current = resolve;
      setOptions(next);
    });
  }, []);

  const settle = useCallback((ok: boolean) => {
    const resolve = resolveRef.current;
    resolveRef.current = null;
    setOptions(null);
    resolve?.(ok);
  }, []);

  const value = useMemo(() => confirm, [confirm]);

  const tone = options?.tone ?? "primary";

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      <Modal
        open={Boolean(options)}
        onClose={() => settle(false)}
        title={options?.title ?? t("common.confirm")}
        width="26rem"
        footer={
          <>
            <Button variant="ghost" onClick={() => settle(false)}>
              {options?.cancelLabel ?? t("common.cancel")}
            </Button>
            <Button variant={tone === "danger" ? "danger" : "primary"} onClick={() => settle(true)} autoFocus>
              {options?.confirmLabel ?? t("common.confirm")}
            </Button>
          </>
        }
      >
        <p className="u-sm" style={{ color: "var(--text-secondary)", lineHeight: "var(--leading-relaxed)" }}>
          {options?.message}
        </p>
      </Modal>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm 必须在 <ConfirmProvider> 内使用");
  return ctx;
}
