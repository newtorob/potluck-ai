/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_LOCAL_API_BASE?: string;
  readonly VITE_CLOUD_API_BASE?: string;
  readonly VITE_COORDINATOR_URL?: string;
  readonly VITE_DEFAULT_MODEL?: string;
  readonly VITE_MCP_TOKEN?: string;
  readonly DEV?: boolean;
  readonly PROD?: boolean;
  readonly MODE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
