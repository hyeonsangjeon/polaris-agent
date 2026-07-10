/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_POLARIS_DEMO?: "0" | "1";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
