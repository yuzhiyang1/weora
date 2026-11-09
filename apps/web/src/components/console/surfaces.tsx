import {
  Children,
  isValidElement,
  useId,
  type ComponentProps,
  type ReactNode,
} from "react";
import {
  Alert,
  Avatar as HeroAvatar,
  Card as HeroCard,
  Chip,
  Modal as HeroModal,
  ProgressBar,
  Skeleton as HeroSkeleton,
  Spinner,
  Tabs as HeroTabs,
} from "@heroui/react";
import { Inbox } from "lucide-react";
import { cn } from "../../lib/utils";

type ContentProps = { children?: ReactNode; className?: string };
type Tone = "info" | "warning" | "danger" | "success";

export function Banner({
  title,
  description,
  type = "info",
  icon,
  className,
}: {
  title?: ReactNode;
  description?: ReactNode;
  type?: Tone;
  icon?: ReactNode;
  closeIcon?: ReactNode;
  className?: string;
}) {
  return (
    <Alert
      status={type === "info" ? "accent" : type}
      role={type === "danger" ? "alert" : "status"}
      className={cn("console-notice", className)}
    >
      <Alert.Indicator>{icon}</Alert.Indicator>
      <Alert.Content>
        {title && <Alert.Title>{title}</Alert.Title>}
        {description && <Alert.Description>{description}</Alert.Description>}
      </Alert.Content>
    </Alert>
  );
}

export function Tag({
  children,
  color = "grey",
  className,
  prefixIcon,
}: ContentProps & { color?: string; size?: string; prefixIcon?: ReactNode }) {
  const tone =
    color === "green"
      ? "success"
      : color === "red"
        ? "danger"
        : ["orange", "yellow"].includes(color)
          ? "warning"
          : ["blue", "light-blue"].includes(color)
            ? "accent"
            : "default";
  return (
    <Chip
      size="sm"
      color={tone}
      variant="soft"
      className={cn("console-tag", className)}
    >
      {prefixIcon}
      {children}
    </Chip>
  );
}

export function Avatar({
  children,
  shape,
}: ContentProps & { color?: string; size?: string; shape?: string }) {
  return (
    <HeroAvatar
      size="sm"
      className={shape === "square" ? "avatar-square" : undefined}
    >
      <HeroAvatar.Fallback>{children}</HeroAvatar.Fallback>
    </HeroAvatar>
  );
}

export function Empty({
  title,
  description,
  image,
  children,
}: ContentProps & {
  title?: ReactNode;
  description?: ReactNode;
  image?: ReactNode;
}) {
  return (
    <div className="console-empty">
      <span className="empty-icon" aria-hidden="true">
        {image ?? <Inbox size={24} />}
      </span>
      {title && <h3>{title}</h3>}
      {description && <p>{description}</p>}
      {children}
    </div>
  );
}

export function Spin({ tip = "正在加载…" }: { tip?: string; size?: string }) {
  return (
    <div role="status" className="console-loading">
      <Spinner size="sm" />
      <span>{tip}</span>
    </div>
  );
}

export function Card({
  title,
  description,
  headerExtraContent,
  children,
  className,
}: ContentProps & {
  title?: ReactNode;
  description?: ReactNode;
  headerExtraContent?: ReactNode;
}) {
  return (
    <HeroCard className={cn("console-card", className)}>
      {(title || headerExtraContent) && (
        <HeroCard.Header className="console-card-heading">
          {description ? (
            <div>
              <HeroCard.Title>{title}</HeroCard.Title>
              <HeroCard.Description>{description}</HeroCard.Description>
            </div>
          ) : (
            <HeroCard.Title>{title}</HeroCard.Title>
          )}
          {headerExtraContent}
        </HeroCard.Header>
      )}
      <HeroCard.Content>{children}</HeroCard.Content>
    </HeroCard>
  );
}

/** 受控弹窗由 HeroUI 管理焦点、滚动和关闭恢复，提交时同时锁定各关闭入口。 */
export function Modal({
  title,
  visible,
  onCancel,
  children,
  width = 640,
  maskClosable = true,
  closeOnEsc = true,
}: ContentProps & {
  title: ReactNode;
  visible: boolean;
  onCancel: () => void;
  width?: number;
  centered?: boolean;
  footer?: ReactNode;
  maskClosable?: boolean;
  closeOnEsc?: boolean;
}) {
  const headingId = useId();
  return (
    <HeroModal.Backdrop
      isOpen={visible}
      isDismissable={maskClosable}
      isKeyboardDismissDisabled={!closeOnEsc}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <HeroModal.Container size="lg" placement="center" scroll="inside">
        <HeroModal.Dialog
          className="console-dialog"
          {...(typeof title === "string"
            ? { "aria-label": title }
            : { "aria-labelledby": headingId })}
          style={{
            width: `min(${width}px, calc(100vw - 32px))`,
            maxWidth: "none",
          }}
        >
          {(closeOnEsc || maskClosable) && (
            <HeroModal.CloseTrigger aria-label="关闭" />
          )}
          <HeroModal.Header>
            <HeroModal.Heading id={headingId}>{title}</HeroModal.Heading>
          </HeroModal.Header>
          <HeroModal.Body className="console-dialog-body">
            {children}
          </HeroModal.Body>
        </HeroModal.Dialog>
      </HeroModal.Container>
    </HeroModal.Backdrop>
  );
}

type TabPaneProps = ContentProps & { itemKey: string; tab: ReactNode };
export function TabPane({ children }: TabPaneProps) {
  return <>{children}</>;
}
export function Tabs({
  activeKey,
  onChange,
  children,
  className,
}: ContentProps & { activeKey: string; onChange: (key: string) => void }) {
  const panes = Children.toArray(children).filter(isValidElement<TabPaneProps>);
  return (
    <HeroTabs
      selectedKey={activeKey}
      onSelectionChange={(key) => onChange(String(key))}
      className={className ?? ""}
    >
      <HeroTabs.ListContainer>
        <HeroTabs.List aria-label="详情内容">
          {panes.map((pane) => (
            <HeroTabs.Tab
              id={pane.props.itemKey}
              key={pane.props.itemKey}
              className="whitespace-nowrap"
            >
              {pane.props.tab}
              <HeroTabs.Indicator />
            </HeroTabs.Tab>
          ))}
        </HeroTabs.List>
      </HeroTabs.ListContainer>
      {panes.map((pane) => (
        <HeroTabs.Panel id={pane.props.itemKey} key={pane.props.itemKey}>
          {pane.props.children}
        </HeroTabs.Panel>
      ))}
    </HeroTabs>
  );
}

function Title({
  heading = 2,
  children,
  ...props
}: ContentProps & { heading?: 1 | 2 | 3 | 4 | 5 | 6 }) {
  const Heading = `h${heading}` as "h1";
  return <Heading {...props}>{children}</Heading>;
}
function Text({
  children,
  type,
  strong,
}: ContentProps & { type?: string; strong?: boolean }) {
  return (
    <span
      className={cn(type && "text-muted-foreground", strong && "font-semibold")}
    >
      {children}
    </span>
  );
}
function Paragraph({ children, type }: ContentProps & { type?: string }) {
  return <p className={cn(type && "text-muted-foreground")}>{children}</p>;
}
export const Typography = { Title, Text, Paragraph };

export function Skeleton({
  placeholder,
}: {
  active?: boolean;
  placeholder?: ReactNode;
}) {
  return (
    <div role="status" aria-label="正在加载" className="console-skeleton">
      {placeholder ?? (
        <>
          <HeroSkeleton className="h-5 w-1/3" />
          <HeroSkeleton className="h-32 w-full" />
        </>
      )}
    </div>
  );
}
Skeleton.Title = function SkeletonTitle(props: ComponentProps<"div">) {
  return (
    <HeroSkeleton {...props} className={cn("h-8 w-1/2", props.className)} />
  );
};

export function Progress({
  percent,
  showInfo = true,
}: {
  percent: number;
  showInfo?: boolean;
  stroke?: string;
}) {
  return (
    <ProgressBar
      aria-label="上传进度"
      value={Math.max(0, Math.min(100, percent))}
    >
      {showInfo && <ProgressBar.Output />}
      <ProgressBar.Track>
        <ProgressBar.Fill />
      </ProgressBar.Track>
    </ProgressBar>
  );
}
