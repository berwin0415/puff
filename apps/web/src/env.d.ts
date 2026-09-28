/**
 * Opts into strict typing of `import.meta.env`: only the keys declared here (on
 * top of Rsbuild's built-ins) can be read from client code.
 */
interface RsbuildTypeOptions {
  strictImportMetaEnv: true;
}

interface ImportMetaEnv {
  /** Base URL the browser uses to reach the API. Defaults to `/api`. */
  readonly PUBLIC_API_BASE_URL?: string;
}
