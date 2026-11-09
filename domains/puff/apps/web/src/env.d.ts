/**
 * 开启 `import.meta.env` 的严格类型：只有这里声明的键（以及 Rsbuild 内置键）
 * 才能在客户端代码中读取。
 */
interface RsbuildTypeOptions {
  strictImportMetaEnv: true;
}

interface ImportMetaEnv {
  /** 浏览器访问 API 的基地址，默认 `/api`。 */
  readonly PUBLIC_API_BASE_URL?: string;
}
