const fs = require("fs");
const path = require("path");

function respond(payload) {
  process.stdout.write(JSON.stringify(payload));
}

function readInput() {
  try {
    return JSON.parse(process.argv[2] || "{}");
  } catch (error) {
    respond({ ok: false, error: `invalid JSON input: ${error.message}` });
    process.exit(0);
  }
}

function loadTypescript() {
  try {
    return { ok: true, ts: require("typescript") };
  } catch (error) {
    return { ok: false, error: `typescript package is not installed: ${error.message}` };
  }
}

function lineOf(ts, sourceFile, pos) {
  return sourceFile.getLineAndCharacterOfPosition(pos).line + 1;
}

function parseTsconfig(ts, repoPath, tsconfig) {
  const configPath = path.resolve(repoPath, tsconfig || "tsconfig.json");
  if (!fs.existsSync(configPath)) {
    return { ok: false, error: `tsconfig not found: ${configPath}` };
  }
  const configFile = ts.readConfigFile(configPath, ts.sys.readFile);
  if (configFile.error) {
    return { ok: false, error: ts.flattenDiagnosticMessageText(configFile.error.messageText, "\n") };
  }
  const parsed = ts.parseJsonConfigFileContent(configFile.config, ts.sys, path.dirname(configPath));
  if (parsed.errors && parsed.errors.length) {
    return {
      ok: false,
      error: parsed.errors.map((item) => ts.flattenDiagnosticMessageText(item.messageText, "\n")).join("; ")
    };
  }
  return { ok: true, configPath, parsed };
}

function createProgram(ts, repoPath, tsconfig) {
  const parsedConfig = parseTsconfig(ts, repoPath, tsconfig);
  if (!parsedConfig.ok) {
    return parsedConfig;
  }
  try {
    const program = ts.createProgram({
      rootNames: parsedConfig.parsed.fileNames,
      options: parsedConfig.parsed.options
    });
    return { ok: true, program, checker: program.getTypeChecker(), configPath: parsedConfig.configPath };
  } catch (error) {
    return { ok: false, error: `failed to create TypeScript Program: ${error.message}` };
  }
}

function normalizeFile(file) {
  return file.replace(/\\/g, "/");
}

function relativeToRepo(repoPath, fileName) {
  return normalizeFile(path.relative(repoPath, fileName));
}

function symbolKey(symbol) {
  if (!symbol) return null;
  const declarations = symbol.getDeclarations ? symbol.getDeclarations() || [] : [];
  if (!declarations.length) return symbol.getName();
  return declarations
    .map((decl) => `${normalizeFile(decl.getSourceFile().fileName)}:${decl.pos}:${decl.end}:${symbol.getName()}`)
    .sort()
    .join("|");
}

module.exports = {
  createProgram,
  lineOf,
  loadTypescript,
  normalizeFile,
  readInput,
  relativeToRepo,
  respond,
  symbolKey
};
