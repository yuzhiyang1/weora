import { TabPane, Tabs } from "../../components/console";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { rulesPath, skillsPath } from "./routes";
import "./resources.css";

export function ResourcesLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const active = location.pathname === rulesPath ? "rules" : "skills";
  return (
    <Tabs
      ariaLabel="资源"
      className="resource-tabs"
      activeKey={active}
      onChange={(key) => navigate(key === "rules" ? rulesPath : skillsPath)}
    >
      <TabPane itemKey="skills" tab="Skills">{active === "skills" && <Outlet />}</TabPane>
      <TabPane itemKey="rules" tab="Rules">{active === "rules" && <Outlet />}</TabPane>
    </Tabs>
  );
}

export function LegacySkillsRedirect() {
  const { pathname, search, hash } = useLocation();
  // 保留收藏和外链中的 Skill ID、查询参数与锚点。
  return <Navigate replace to={{ pathname: skillsPath + pathname.slice("/skills".length), search, hash }} />;
}
