import { forwardRef, type ComponentProps, type ReactNode } from "react";
import {
  Autocomplete,
  Button as HeroButton,
  Checkbox as HeroCheckbox,
  Input as HeroInput,
  InputGroup,
  Label,
  ListBox,
  SearchField,
  Select as HeroSelect,
  Spinner,
  TextArea as HeroTextArea,
} from "@heroui/react";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";

type ButtonProps = Omit<
  ComponentProps<typeof HeroButton>,
  "type" | "size" | "className" | "children"
> & {
  children?: ReactNode;
  className?: string;
  disabled?: boolean | undefined;
  htmlType?: "button" | "submit" | "reset";
  type?: "primary" | "secondary" | "tertiary" | "danger" | "warning";
  theme?: "solid" | "light" | "borderless";
  size?: "small" | "default" | "large";
  loading?: boolean;
  block?: boolean;
  icon?: ReactNode;
  iconPosition?: "left" | "right";
};

/** 仅适配业务提交约定，外观、焦点和按压反馈全部由 HeroUI 提供。 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      children,
      htmlType = "button",
      type,
      theme,
      size,
      loading,
      block,
      icon,
      iconPosition = "left",
      className,
      disabled,
      ...props
    },
    ref,
  ) {
    const variant =
      type === "danger"
        ? "danger"
        : theme === "borderless"
          ? "ghost"
          : theme === "solid"
            ? "primary"
            : theme === "light"
              ? "secondary"
              : "outline";
    return (
      <HeroButton
        {...props}
        ref={ref}
        type={htmlType}
        variant={variant}
        size={size === "large" ? "lg" : size === "small" ? "sm" : "md"}
        fullWidth={!!block}
        isIconOnly={!children && !!icon}
        isDisabled={!!disabled || !!loading}
        aria-busy={loading || undefined}
        className={cn("console-button", className)}
      >
        {loading ? (
          <Spinner size="sm" aria-hidden="true" />
        ) : iconPosition === "left" ? (
          icon
        ) : null}
        {children}
        {!loading && iconPosition === "right" ? icon : null}
      </HeroButton>
    );
  },
);

type InputProps = Omit<
  ComponentProps<typeof HeroInput>,
  "onChange" | "size" | "prefix" | "className"
> & {
  className?: string;
  onChange?: (value: string) => void;
  prefix?: ReactNode;
  suffix?: ReactNode;
  showClear?: boolean;
  size?: "small" | "default" | "large";
  mode?: string;
};

/** 保留原生 label、密码和 ref 语义；装饰与清除按钮使用 HeroUI 输入组。 */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  {
    onChange,
    prefix,
    suffix,
    showClear,
    size,
    mode,
    className,
    variant = "secondary",
    fullWidth = true,
    ...props
  },
  ref,
) {
  const input = (
    <HeroInput
      {...props}
      variant={variant}
      fullWidth={fullWidth}
      ref={ref}
      className={cn(
        "console-input",
        !prefix && !suffix && !showClear && className,
      )}
      type={mode === "password" ? "password" : (props.type ?? "text")}
      onChange={(event) => onChange?.(event.target.value)}
    />
  );
  if (!prefix && !suffix && !showClear) return input;
  return (
    <InputGroup
      variant={variant}
      fullWidth={fullWidth}
      className={cn(
        "console-input-wrap",
        size === "large" && "input-large",
        className,
      )}
    >
      {prefix && (
        <InputGroup.Prefix aria-hidden="true">{prefix}</InputGroup.Prefix>
      )}
      <InputGroup.Input
        {...props}
        ref={ref}
        className="console-input"
        type={mode === "password" ? "password" : (props.type ?? "text")}
        onChange={(event) => onChange?.(event.target.value)}
      />
      {showClear && !!props.value && (
        <InputGroup.Suffix>
          <HeroButton
            size="sm"
            variant="ghost"
            isIconOnly
            aria-label="清除输入"
            onPress={() => onChange?.("")}
          >
            <X size={14} />
          </HeroButton>
        </InputGroup.Suffix>
      )}
      {suffix && <InputGroup.Suffix>{suffix}</InputGroup.Suffix>}
    </InputGroup>
  );
});

export const TextArea = forwardRef<
  HTMLTextAreaElement,
  Omit<ComponentProps<typeof HeroTextArea>, "onChange" | "className"> & {
    className?: string;
    onChange?: (value: string) => void;
  }
>(function TextArea({ onChange, className, ...props }, ref) {
  return (
    <HeroTextArea
      {...props}
      variant={props.variant ?? "secondary"}
      ref={ref}
      className={cn("console-textarea", className)}
      onChange={(event) => onChange?.(event.target.value)}
    />
  );
});

type CheckboxProps = {
  children?: ReactNode;
  checked?: boolean;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  onChange?: (event: { target: { checked: boolean } }) => void;
};

export function Checkbox({
  children,
  checked,
  disabled,
  onChange,
  ...props
}: CheckboxProps) {
  return (
    <HeroCheckbox
      {...props}
      className="console-checkbox"
      isSelected={!!checked}
      isDisabled={!!disabled}
      onChange={(selected) => onChange?.({ target: { checked: selected } })}
    >
      <HeroCheckbox.Content>
        <HeroCheckbox.Control>
          <HeroCheckbox.Indicator />
        </HeroCheckbox.Control>
        {children && <Label>{children}</Label>}
      </HeroCheckbox.Content>
    </HeroCheckbox>
  );
}

type SelectOption = {
  value: string | number;
  label: ReactNode;
  disabled?: boolean;
};
type SelectProps = {
  optionList: SelectOption[];
  value?: string | number | (string | number)[] | undefined;
  onChange?: (value: string | number | (string | number)[]) => void;
  placeholder?: string;
  multiple?: boolean;
  loading?: boolean;
  disabled?: boolean;
  "aria-label"?: string;
  "aria-labelledby"?: string;
};

/** 搜索只过滤候选项，选中值仍由业务管理；多选过滤后不会丢失其他仓库。 */
export function Select({
  optionList,
  value,
  onChange,
  placeholder = "请选择",
  multiple,
  loading,
  disabled,
  ...aria
}: SelectProps) {
  return (
    <HeroSelect
      className="console-select"
      variant="secondary"
      fullWidth
      {...(aria["aria-labelledby"]
        ? { "aria-labelledby": aria["aria-labelledby"] }
        : { "aria-label": aria["aria-label"] ?? placeholder })}
      placeholder={placeholder}
      isDisabled={!!disabled || !!loading}
      selectionMode={multiple ? "multiple" : "single"}
      value={
        multiple
          ? Array.isArray(value)
            ? value
            : []
          : Array.isArray(value)
            ? (value[0] ?? null)
            : (value ?? null)
      }
      onChange={(selected) => onChange?.(selected ?? "")}
    >
      <HeroSelect.Trigger>
        <HeroSelect.Value />
        {loading ? <Spinner size="sm" /> : <HeroSelect.Indicator />}
      </HeroSelect.Trigger>
      <HeroSelect.Popover className="console-select-popover">
        <Autocomplete.Filter
          filter={(text, input) =>
            text.toLocaleLowerCase().includes(input.toLocaleLowerCase())
          }
        >
          <SearchField aria-label="搜索选项" autoFocus>
            <HeroInput placeholder="搜索选项…" />
          </SearchField>
          <ListBox
            aria-label="可选项"
            items={optionList}
            disabledKeys={optionList
              .filter((option) => option.disabled)
              .map((option) => option.value)}
            renderEmptyState={() => (
              <div className="picker-empty">没有匹配选项</div>
            )}
          >
            {(option) => (
              <ListBox.Item id={option.value} textValue={String(option.label)}>
                {option.label}
                <ListBox.ItemIndicator />
              </ListBox.Item>
            )}
          </ListBox>
        </Autocomplete.Filter>
      </HeroSelect.Popover>
    </HeroSelect>
  );
}
