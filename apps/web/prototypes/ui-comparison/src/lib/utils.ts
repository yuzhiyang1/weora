import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** 合并业务样式与 shadcn 变体，避免同一属性的 Tailwind 类互相覆盖。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
