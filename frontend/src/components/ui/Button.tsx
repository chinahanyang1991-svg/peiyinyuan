import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cx } from "../../lib/cx";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "outline"
  | "danger"
  | "danger-soft"
  | "success"
  | "warning";

export type ButtonSize = "xs" | "sm" | "md" | "lg";

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** 占满父容器宽度 */
  block?: boolean;
  /** 加载中：显示 spinner 并禁用点击 */
  loading?: boolean;
  icon?: LucideIcon;
  iconRight?: LucideIcon;
  children?: ReactNode;
}

const ICON_SIZE: Record<ButtonSize, number> = {
  xs: 13,
  sm: 14,
  md: 16,
  lg: 18,
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "secondary",
    size = "md",
    block = false,
    loading = false,
    icon: Icon,
    iconRight: IconRight,
    children,
    className,
    disabled,
    type = "button",
    ...rest
  },
  ref
) {
  const isIconOnly = !children && Boolean(Icon) && !IconRight;
  const px = ICON_SIZE[size];

  return (
    <button
      ref={ref}
      type={type}
      className={cx(
        "btn",
        `btn--${variant}`,
        size !== "md" && `btn--${size}`,
        block && "btn--block",
        isIconOnly && "btn--icon",
        className
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? (
        <span className="btn__spinner" aria-hidden="true" />
      ) : Icon ? (
        <Icon size={px} aria-hidden="true" />
      ) : null}
      {children ? <span>{children}</span> : null}
      {IconRight && !loading ? <IconRight size={px} aria-hidden="true" /> : null}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonProps, "icon" | "children"> {
  icon: LucideIcon;
  /** 无障碍名称：图标按钮必填 */
  label: string;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { icon: Icon, label, size = "sm", variant = "ghost", className, ...rest },
  ref
) {
  return (
    <Button
      ref={ref}
      icon={Icon}
      size={size}
      variant={variant}
      aria-label={label}
      title={label}
      className={className}
      {...rest}
    />
  );
});

export function ButtonGroup({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cx("btn-group", className)}>{children}</div>;
}
