import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'yaml';

const scriptDir = fileURLToPath(new URL('.', import.meta.url));
const specPath = resolve(scriptDir, '../../contracts/openapi.yaml');
const baselinePath = resolve(scriptDir, '../../contracts/compat-baseline.json');

export function snapshot(spec) {
  const propertyShape = value => ({
    type: value.type || null,
    ref: value.$ref || null,
    const: value.const ?? null,
    enum: value.enum || [],
    format: value.format || null,
    minimum: value.minimum ?? null,
    maximum: value.maximum ?? null,
    minLength: value.minLength ?? null,
    maxLength: value.maxLength ?? null,
  });
  const operations = {};
  for (const [path, methods] of Object.entries(spec.paths || {})) {
    for (const [method, operation] of Object.entries(methods)) {
      if (!['get', 'post', 'put', 'patch', 'delete'].includes(method)) continue;
      operations[`${method.toUpperCase()} ${path}`] = Object.keys(operation.responses || {}).sort();
    }
  }
  const schemas = {};
  for (const [name, schema] of Object.entries(spec.components?.schemas || {})) {
    schemas[name] = {
      required: [...(schema.required || [])].sort(),
      properties: Object.fromEntries(Object.entries(schema.properties || {}).sort(([a], [b]) => a.localeCompare(b)).map(([field, value]) => [field, propertyShape(value)])),
      enum: [...(schema.enum || [])].sort(),
      type: schema.type || null,
      allOf: (schema.allOf || []).map(value => value.$ref || JSON.stringify(value)),
      oneOf: (schema.oneOf || []).map(value => value.$ref || JSON.stringify(value)),
    };
  }
  return { version: spec.info.version, operations, schemas };
}

export function breakingChanges(previous, current) {
  const errors = [];
  for (const [name, responses] of Object.entries(previous.operations)) {
    const newResponses = current.operations[name];
    if (!newResponses) { errors.push(`Removed operation ${name}`); continue; }
    for (const code of responses) if (!newResponses.includes(code)) errors.push(`Removed response ${code} from ${name}`);
  }
  for (const [name, oldSchema] of Object.entries(previous.schemas)) {
    const next = current.schemas[name];
    if (!next) { errors.push(`Removed schema ${name}`); continue; }
    for (const [field, oldShape] of Object.entries(oldSchema.properties)) {
      const newShape = next.properties[field];
      if (!newShape) errors.push(`Removed ${name}.${field}`);
      else if (JSON.stringify(oldShape) !== JSON.stringify(newShape)) errors.push(`Changed ${name}.${field}`);
    }
    for (const field of next.required) if (!oldSchema.required.includes(field)) errors.push(`New required ${name}.${field}`);
    for (const field of oldSchema.required) if (!next.required.includes(field)) errors.push(`Removed required ${name}.${field}`);
    for (const value of oldSchema.enum) if (!next.enum.includes(value)) errors.push(`Removed enum ${name}.${value}`);
    if (JSON.stringify(oldSchema.type) !== JSON.stringify(next.type)) errors.push(`Changed type of ${name}`);
    for (const ref of oldSchema.allOf) if (!next.allOf.includes(ref)) errors.push(`Removed allOf ${ref} from ${name}`);
    for (const ref of oldSchema.oneOf) if (!next.oneOf.includes(ref)) errors.push(`Removed oneOf ${ref} from ${name}`);
  }
  return errors;
}

const current = snapshot(parse(readFileSync(specPath, 'utf8')));
if (process.argv.includes('--write-baseline')) {
  writeFileSync(baselinePath, JSON.stringify(current, null, 2) + '\n');
  console.log(`Wrote ${baselinePath}`);
} else if (process.argv.includes('--self-test')) {
  const changed = structuredClone(current);
  changed.schemas.SourceEventBase.required.push('new_mandatory_field');
  changed.schemas.InspectionResult.enum = changed.schemas.InspectionResult.enum.filter(value => value !== 'defect_signs');
  const found = breakingChanges(current, changed);
  if (found.length !== 2) throw new Error(`Expected two breaking changes, found ${found.length}: ${found.join('; ')}`);
  console.log(`Detected deliberate incompatibilities: ${found.join('; ')}`);
} else {
  const previous = JSON.parse(readFileSync(baselinePath, 'utf8'));
  const errors = breakingChanges(previous, current);
  if (errors.length) { console.error(errors.join('\n')); process.exitCode = 1; }
  else console.log(`Compatible with baseline ${previous.version}`);
}
