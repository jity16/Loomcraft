// Node's strip-types test runner needs an explicit module extension for source
// imports, while the TypeScript build emits ``layout.js`` from ``layout.ts``.
// This tiny source shim keeps both paths pointing at the same implementation.
export * from "./layout.ts";
