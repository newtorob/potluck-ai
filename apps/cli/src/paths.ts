import { homedir } from "node:os";
import { resolve, join } from "node:path";

/** Match the Python runtime; isolated CLI tests must never use real user state. */
export function dataDir(): string {
  return process.env.POTLUCK_DATA_DIR ? resolve(process.env.POTLUCK_DATA_DIR) : join(homedir(), ".potluck");
}
