import {
  forwardRef,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import type { LucideIcon } from "lucide-react";
import { cx } from "../../lib/cx";

/* --------------------------------------------------------------------------
   Field —— label / hint / error 包裹层
   -------------------------------------------------------------------------- */
export interface FieldProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  optional?: boolean;
  htmlFor?: string;
  inline?: boolean;
  className?: string;
  children: ReactNode;
}

export function Field({
  label,
  hint,
  error,
  optional = false,
  htmlFor,
  inline = false,
  className,
  children,
}: FieldProps) {
  return (
    <div className={cx("field", inline && "field--inline", className)}>
      {label ? (
        <label className="field__label" htmlFor={htmlFor}>
          <span>{label}</span>
          {optional ? <span className="field__optional">·</span> : null}
        </label>
      ) : null}
      {children}
      {error ? (
        <span className="field__error" role="alert">
          {error}
        </span>
      ) : hint ? (
        <span className="field__hint">{hint}</span>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------------------
   Input
   -------------------------------------------------------------------------- */
export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  controlSize?: "sm" | "md" | "lg";
  invalid?: boolean;
  icon?: LucideIcon;
  affixRight?: ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { controlSize = "md", invalid, icon: Icon, affixRight, className, ...rest },
  ref
) {
  const input = (
    <input
      ref={ref}
      className={cx(
        "control",
        controlSize !== "md" && `control--${controlSize}`,
        invalid && "control--invalid",
        className
      )}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  );

  if (!Icon && !affixRight) return input;

  return (
    <span className={cx("input-affix", !Icon && affixRight && "input-affix--right")}>
      {Icon ? <Icon size={15} aria-hidden="true" /> : null}
      {input}
      {affixRight}
    </span>
  );
});

/* --------------------------------------------------------------------------
   Textarea
   -------------------------------------------------------------------------- */
export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid, className, rows = 4, ...rest },
  ref
) {
  return (
    <textarea
      ref={ref}
      rows={rows}
      className={cx("control", invalid && "control--invalid", className)}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  );
});

/* --------------------------------------------------------------------------
   Select（原生 select + 自绘箭头，保留键盘与移动端原生选择器）
   -------------------------------------------------------------------------- */
export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectGroup {
  label: string;
  options: SelectOption[];
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "size"> {
  controlSize?: "sm" | "md" | "lg";
  options?: SelectOption[];
  groups?: SelectGroup[];
  placeholder?: string;
  invalid?: boolean;
  children?: ReactNode;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { controlSize = "md", options, groups, placeholder, invalid, className, children, ...rest },
  ref
) {
  const select = (
    <select
      ref={ref}
      className={cx(
        "control",
        controlSize !== "md" && `control--${controlSize}`,
        invalid && "control--invalid",
        className
      )}
      aria-invalid={invalid || undefined}
      {...rest}
    >
      {placeholder !== undefined ? <option value="">{placeholder}</option> : null}
      {options?.map((opt) => (
        <option key={opt.value} value={opt.value} disabled={opt.disabled}>
          {opt.label}
        </option>
      ))}
      {groups?.map((group) => (
        <optgroup key={group.label} label={group.label}>
          {group.options.map((opt) => (
            <option key={opt.value} value={opt.value} disabled={opt.disabled}>
              {opt.label}
            </option>
          ))}
        </optgroup>
      ))}
      {children}
    </select>
  );

  return <span className={cx("select-wrap", controlSize === "sm" && "select-wrap--sm")}>{select}</span>;
});

/* --------------------------------------------------------------------------
   Checkbox / Radio / Switch
   -------------------------------------------------------------------------- */
export interface CheckProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: ReactNode;
  indeterminate?: boolean;
}

export const Checkbox = forwardRef<HTMLInputElement, CheckProps>(function Checkbox(
  { label, indeterminate, className, ...rest },
  ref
) {
  return (
    <label className={cx("check", className)}>
      <input
        ref={(node) => {
          if (node) node.indeterminate = Boolean(indeterminate);
          if (typeof ref === "function") ref(node);
          else if (ref) (ref as React.MutableRefObject<HTMLInputElement | null>).current = node;
        }}
        type="checkbox"
        {...rest}
      />
      <span>{label}</span>
    </label>
  );
});

export const Radio = forwardRef<HTMLInputElement, CheckProps>(function Radio(
  { label, className, ...rest },
  ref
) {
  return (
    <label className={cx("check", className)}>
      <input ref={ref} type="radio" {...rest} />
      <span>{label}</span>
    </label>
  );
});

export interface SwitchProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: ReactNode;
}

export const Switch = forwardRef<HTMLInputElement, SwitchProps>(function Switch(
  { label, className, ...rest },
  ref
) {
  return (
    <label className={cx("switch", className)}>
      <input ref={ref} type="checkbox" role="switch" {...rest} />
      <span className="switch__track" aria-hidden="true" />
      <span>{label}</span>
    </label>
  );
});

/* --------------------------------------------------------------------------
   Slider（range + 数值回显）
   -------------------------------------------------------------------------- */
export interface SliderProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange"> {
  label?: ReactNode;
  value: number;
  onValueChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  /** 数值展示文案（已本地化） */
  valueText?: ReactNode;
  minText?: ReactNode;
  maxText?: ReactNode;
  className?: string;
}

export function Slider({
  label,
  value,
  onValueChange,
  min,
  max,
  step = 1,
  valueText,
  minText,
  maxText,
  className,
  ...rest
}: SliderProps) {
  const id = useId();
  return (
    <div className={cx("slider", className)}>
      <div className="slider__head">
        <label htmlFor={id}>{label}</label>
        {valueText !== undefined ? <span className="slider__value">{valueText}</span> : null}
      </div>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onValueChange(Number(event.target.value))}
        {...rest}
      />
      {minText !== undefined || maxText !== undefined ? (
        <div className="slider__scale">
          <span>{minText}</span>
          <span>{maxText}</span>
        </div>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------------------
   NumberInput（增益 / LUFS / 毫秒等数值列，紧凑样式）
   -------------------------------------------------------------------------- */
export interface NumberInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange"> {
  value: number;
  onValueChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  className?: string;
  ariaLabel: string;
}

export function NumberInput({
  value,
  onValueChange,
  min,
  max,
  step,
  suffix,
  className,
  ariaLabel,
  ...rest
}: NumberInputProps) {
  return (
    <input
      type="number"
      inputMode="decimal"
      className={cx("control", "control--sm", "num-input", className)}
      value={Number.isFinite(value) ? value : ""}
      min={min}
      max={max}
      step={step}
      aria-label={ariaLabel}
      aria-valuetext={suffix ? `${value} ${suffix}` : undefined}
      onChange={(event) => {
        const next = event.target.valueAsNumber;
        onValueChange(Number.isFinite(next) ? next : 0);
      }}
      {...rest}
    />
  );
}
