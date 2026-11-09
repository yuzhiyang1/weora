import { buttonVariants } from "@heroui/styles";
import { ArrowLeft as IconArrowLeft, Wrench as IconWrench } from "lucide-react";
import { Empty, Typography } from "../../components/console";
import { Link } from "react-router-dom";

type PlaceholderPageProps = {
  eyebrow: string;
  title: string;
  description: string;
};

export function PlaceholderPage({
  eyebrow,
  title,
  description,
}: PlaceholderPageProps) {
  return (
    <section className="placeholder-page page-enter">
      <span className="page-eyebrow">{eyebrow}</span>
      <Typography.Title heading={1}>{title}</Typography.Title>
      <Typography.Paragraph>{description}</Typography.Paragraph>
      <div className="placeholder-surface">
        <Empty
          description="可以先在工作台发起任务、管理 Agent 和沉淀团队 Skills。"
          image={<IconWrench size={32} />}
          title="功能筹备中"
        >
          <Link className={buttonVariants({ variant: "outline" })} to="/">
            <IconArrowLeft />
            返回概览
          </Link>
        </Empty>
      </div>
    </section>
  );
}
