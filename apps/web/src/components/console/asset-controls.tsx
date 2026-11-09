import { buttonVariants } from "@heroui/styles";
import { ArrowLeft } from "lucide-react";
import { Link } from "react-router-dom";
import { Button } from "./controls";
import { Banner } from "./surfaces";

export function AssetBackLink({ to, label }: { to: string; label: string }) {
  return (
    <Link
      to={to}
      className={`${buttonVariants({ variant: "outline" })} asset-back-link`}
    >
      <ArrowLeft size={16} aria-hidden="true" />
      {label}
    </Link>
  );
}

export function AssetDetailsLink({ to, name }: { to: string; name: string }) {
  return (
    <Link
      to={to}
      aria-label={`查看 ${name}`}
      className={buttonVariants({ variant: "outline", size: "sm" })}
    >
      查看详情
    </Link>
  );
}

export function AssetLoadError({
  title,
  description,
  onRetry,
}: {
  title: string;
  description: string;
  onRetry: () => void;
}) {
  return (
    <Banner
      closeIcon={null}
      title={title}
      type="danger"
      description={
        <div className="asset-load-feedback">
          <p>{description}</p>
          <Button size="small" onClick={onRetry}>
            重新加载
          </Button>
        </div>
      }
    />
  );
}
