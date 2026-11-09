import { useState } from "react";
import { Input, Select } from "../../components/console";

/** 不虚构模型目录：可继承团队配置、沿用当前覆盖值，或显式输入运行时支持的 ID。 */
export function CoordinationModelField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string | null;
  onChange: (value: string | null) => void;
}) {
  const [custom, setCustom] = useState(false);
  return (
    <div className="form-field">
      <span>{label}</span>
      <Select
        aria-label={label}
        value={custom ? "custom" : value ? "current" : "team"}
        optionList={[
          { value: "team", label: "使用团队默认模型" },
          ...(!custom && value ? [{ value: "current", label: value }] : []),
          { value: "custom", label: "指定模型 ID" },
        ]}
        onChange={(next) => {
          setCustom(next === "custom");
          if (next === "team") onChange(null);
        }}
      />
      {custom && (
        <Input
          aria-label={`${label} ID`}
          value={value ?? ""}
          placeholder="留空使用团队默认模型"
          pattern="[A-Za-z0-9_.-]*"
          onChange={(next) => onChange(next || null)}
        />
      )}
    </div>
  );
}
