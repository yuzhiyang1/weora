import { createContext, useContext, useEffect, useRef } from "react";

export const SettingsDraftsContext = createContext<
  ((key: symbol, dirty: boolean) => void) | null
>(null);

/** 草稿注册独立于页面和空间选择器，供已有业务弹窗复用且不形成循环依赖。 */
export function useSettingsDirty(dirty: boolean) {
  const register = useContext(SettingsDraftsContext);
  const key = useRef(Symbol("settings-draft"));
  useEffect(() => {
    const id = key.current;
    register?.(id, dirty);
    return () => register?.(id, false);
  }, [register, dirty]);
}
