import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { execFileSync } from "node:child_process";

const temporary = mkdtempSync(join(tmpdir(), "efqa-types-"));
try {
  for (const [spec, output] of [["openapi.yaml", "api.ts"], ["external-openapi.yaml", "external.ts"]]) {
    const generated = join(temporary, output);
    execFileSync(process.execPath, [resolve("node_modules/openapi-typescript/bin/cli.js"), `../contracts/${spec}`, "-o", generated]);
    if (readFileSync(generated, "utf8").replaceAll("\r\n", "\n") !== readFileSync(`src/generated/${output}`, "utf8").replaceAll("\r\n", "\n")) {
      throw new Error(`Outdated generated TypeScript: ${output}`);
    }
  }
  console.log("TypeScript generation matches both OpenAPI sources");
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
