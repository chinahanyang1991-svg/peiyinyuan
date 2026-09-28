/**
 * i18n 词典类型层
 * zh.ts 是唯一真相源：en.ts 必须完整满足 typeof zh，缺译文会在 tsc 阶段报错。
 */

export type DictLeaf = string | readonly string[];

/** 把嵌套词典展开为 "a.b.c" 形式的点号路径联合类型（数组视为叶子） */
export type DictPaths<T> = {
  [K in keyof T & string]: T[K] extends DictLeaf
    ? K
    : T[K] extends object
      ? `${K}.${DictPaths<T[K]>}`
      : never;
}[keyof T & string];

/**
 * 把 as const 词典的字面量类型放宽为 string / readonly string[]，
 * 供其他语言词典做结构一致性校验：少键、多键、层级错位都会在 tsc 阶段报错。
 */
export type DictShape<T> = {
  [K in keyof T]: T[K] extends readonly string[]
    ? readonly string[]
    : T[K] extends string
      ? string
      : T[K] extends object
        ? DictShape<T[K]>
        : T[K];
};

export type Interpolation = Record<string, string | number>;

export interface LocaleDef {
  /** BCP-47 标签，用于 Intl.* 与 <html lang> */
  tag: string;
  /** 顶栏切换按钮上显示的短名 */
  short: string;
  /** 母语名（不自译，保证任何语言下都能认出） */
  nativeName: string;
}
