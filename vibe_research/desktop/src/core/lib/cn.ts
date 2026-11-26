/**
 * Core 自己的 classnames 合并。
 *
 * 🔴 不能 import 垂类里的 `@/lib/utils` —— Core 不许依赖垂类（前端边界棘轮会红）。
 *    直接用同两个 npm 包，行为与垂类那份一致。
 */
import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
