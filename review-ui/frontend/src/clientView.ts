import { createContext } from "react";

/** The client's copy of the UI (CLIENT_VIEW=true in /api/config): Page Details shows only
 * the Final values, headed Algorithm (2026-10-27, user). */
export const ClientViewContext = createContext(false);
