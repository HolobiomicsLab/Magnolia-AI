/** Resolve and invoke the one Python installation used by Magnolia plugins. */
import { existsSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const MODULE_DIR = dirname(fileURLToPath(import.meta.url))
export const PACKAGE_ROOT = resolve(MODULE_DIR, "../..")
export const PACKAGE_SRC = join(PACKAGE_ROOT, "mcp-servers/compchem-memory/src")
export const REPOSITORY_ROOT = resolve(PACKAGE_ROOT, "..")
const VENV_PYTHON = join(REPOSITORY_ROOT, ".venv", "bin", "python3")

const MODULE_BOOTSTRAP = [
  "import runpy,sys",
  "source,module,*args=sys.argv[1:]",
  "sys.path.insert(0,source)",
  "sys.argv=[module,*args]",
  "runpy.run_module(module,run_name='__main__')",
].join(";")

/** Return configured Python, the repository venv, or the documented fallback. */
export function resolveMagnoliaPython(): string {
  const configured = String(process.env.MAGNOLIA_PYTHON ?? "").trim()
  if (configured) return configured
  return existsSync(VENV_PYTHON) ? VENV_PYTHON : "python3"
}

/** Build an isolated module invocation independent of cwd and PYTHONPATH. */
export function isolatedModuleArgs(module: string, args: string[] = []): string[] {
  return ["-I", "-c", MODULE_BOOTSTRAP, PACKAGE_SRC, module, ...args]
}
