import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** 合并业务布局与 HeroUI 的工具类，避免同一属性的 Tailwind 类互相覆盖。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
