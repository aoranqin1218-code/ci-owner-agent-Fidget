const {
  createProgram,
  lineOf,
  loadTypescript,
  normalizeFile,
  readInput,
  relativeToRepo,
  respond,
  symbolKey
} = require("./common");

function main() {
  const input = readInput();
  const loaded = loadTypescript();
  if (!loaded.ok) return respond({ ok: false, error: loaded.error, callers: [] });
  const ts = loaded.ts;
  const programResult = createProgram(ts, input.repoPath, input.tsconfig);
  if (!programResult.ok) return respond({ ok: false, error: programResult.error, callers: [] });
  const { program, checker } = programResult;
  const maxResults = Math.max(0, input.maxResults || 50);
  const definitionFile = normalizeFile(input.definitionFile || "");
  function resolvedSymbol(node) {
    let symbol = checker.getSymbolAtLocation(node);
    if (symbol && (symbol.flags & ts.SymbolFlags.Alias)) {
      symbol = checker.getAliasedSymbol(symbol);
    }
    return symbol;
  }
  let targetKey = null;
  for (const sourceFile of program.getSourceFiles()) {
    if (sourceFile.isDeclarationFile) continue;
    if (relativeToRepo(input.repoPath, sourceFile.fileName) !== definitionFile) continue;
    function visit(node) {
      if (targetKey) return;
      if (ts.isIdentifier(node) && node.text === input.symbol) {
        targetKey = symbolKey(resolvedSymbol(node));
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  if (!targetKey) {
    return respond({ ok: false, error: `definition symbol not resolved by TypeChecker: ${input.symbol}`, callers: [] });
  }

  const callers = [];
  const seen = new Set();
  for (const sourceFile of program.getSourceFiles()) {
    if (sourceFile.isDeclarationFile) continue;
    function visit(node) {
      if (callers.length >= maxResults) return;
      if (ts.isIdentifier(node) && node.text === input.symbol) {
        if (symbolKey(resolvedSymbol(node)) === targetKey) {
          const file = relativeToRepo(input.repoPath, sourceFile.fileName);
          const line = lineOf(ts, sourceFile, node.getStart());
          const key = `${file}:${line}:${node.getStart()}`;
          if (!seen.has(key)) {
            seen.add(key);
            callers.push({
              symbol: input.symbol,
              file,
              line,
              text: node.parent ? node.parent.getText(sourceFile).slice(0, 300) : node.getText(sourceFile),
              resolvedByTypeChecker: true
            });
          }
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  respond({ ok: true, callers, truncated: callers.length >= maxResults });
}

main();
