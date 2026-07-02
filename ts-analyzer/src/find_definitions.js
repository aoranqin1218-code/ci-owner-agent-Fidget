const {
  createProgram,
  lineOf,
  loadTypescript,
  readInput,
  relativeToRepo,
  respond
} = require("./common");

function main() {
  const input = readInput();
  const loaded = loadTypescript();
  if (!loaded.ok) return respond({ ok: false, error: loaded.error, definitions: [] });
  const ts = loaded.ts;
  const programResult = createProgram(ts, input.repoPath, input.tsconfig);
  if (!programResult.ok) return respond({ ok: false, error: programResult.error, definitions: [] });
  const { program, checker } = programResult;
  const wanted = new Set(input.symbols || []);
  const seen = new Set();
  const definitions = [];
  for (const sourceFile of program.getSourceFiles()) {
    if (sourceFile.isDeclarationFile) continue;
    function visit(node) {
      if (ts.isIdentifier(node) && wanted.has(node.text)) {
        let symbol = checker.getSymbolAtLocation(node);
        if (symbol && (symbol.flags & ts.SymbolFlags.Alias)) {
          symbol = checker.getAliasedSymbol(symbol);
        }
        const declarations = symbol ? symbol.getDeclarations() || [] : [];
        for (const decl of declarations) {
          const file = relativeToRepo(input.repoPath, decl.getSourceFile().fileName);
          const key = `${node.text}:${file}:${decl.pos}:${decl.end}`;
          if (seen.has(key)) continue;
          seen.add(key);
          definitions.push({
            symbol: node.text,
            file,
            startLine: lineOf(ts, decl.getSourceFile(), decl.getStart()),
            endLine: lineOf(ts, decl.getSourceFile(), decl.end),
            kind: ts.SyntaxKind[decl.kind],
            resolvedByTypeChecker: true
          });
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  respond({ ok: true, definitions });
}

main();
