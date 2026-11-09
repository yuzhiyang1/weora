import { buttonVariants } from "@heroui/react";
import { ArrowLeft } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Select } from "../../components/console";
import { BusinessSpacePicker } from "./SettingsEnvironment";
import "./settings-mobile.css";

/** 手机端直接暴露上下文与返回入口；路由切换仍经过原有未保存修改保护。 */
export function SettingsMobileNavigation({
  items,
}: {
  items: readonly { path: string; label: string }[];
}) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  return (
    <section className="settings-mobile-context" aria-label="手机端设置导航">
      <Link to="/" className={buttonVariants({ variant: "outline" })}>
        <ArrowLeft size={16} aria-hidden="true" />
        返回工作台
      </Link>
      <BusinessSpacePicker />
      <Select
        aria-label="设置栏目"
        value={pathname}
        optionList={items.map((item) => ({
          value: item.path,
          label: `设置 / ${item.label}`,
        }))}
        onChange={(value) => {
          if (value !== pathname) navigate(String(value));
        }}
      />
    </section>
  );
}
