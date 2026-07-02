const {
  gitShow,
  isTsFile,
  lineOf,
  loadTypescript,
  readInput,
  respond,
  runGit
} = require("./common");

function changedRanges(diffText) {
  const ranges = [];
  const re = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/gm;
  let match;
  while ((match = re.exec(diffText))) {
    const start = Number(match[1]);
    const count = match[2] === undefined ? 1 : Number(match[2]);
    if (count > 0) {
      ranges.push({ start, end: start + count - 1 });
    }
  }
  return ranges;
}

function intersects(a, b) {
  return a.start <= b.end && b.start <= a.end;
}

function nodeName(ts, node, sourceFile) {
  if (node.name && node.name.getText) return node.name.getText(sourceFile);
  if (ts.isVariableDeclaration(node.parent) && node.parent.name) return node.parent.name.getText(sourceFile);
  if (ts.isPropertyAssignment(node.parent) && node.parent.name) return node.parent.name.getText(sourceFile);
  return "<anonymous>";
}

function collectFunctions(ts, sourceFile) {
  const functions = [];
  function visit(node) {
    if (
      ts.isFunctionDeclaration(node) ||
      ts.isMethodDeclaration(node) ||
      ts.isFunctionExpression(node) ||
      ts.isArrowFunction(node)
    ) {
      functions.push({
        name: nodeName(ts, node, sourceFile),
        startLine: lineOf(ts, sourceFile, node.getStart(sourceFile)),
        endLine: lineOf(ts, sourceFile, node.end)
      });
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return functions;
}

function main() {
  const input = readInput();
  const loaded = loadTypescript();
  if (!loaded.ok) return respond({ ok: false, error: loaded.error, changedFunctions: [] });
  const ts = loaded.ts;
  const changedFunctions = [];
  const files = (input.files || []).filter(isTsFile);
  for (const file of files) {
    const diff = runGit(input.repoPath, ["diff", "--unified=0", input.baseCommit, input.headCommit, "--", file]);
    if (!diff.ok) {
      return respond({ ok: false, error: `git diff failed for ${file}: ${diff.stderr || diff.code}`, changedFunctions });
    }
    const ranges = changedRanges(diff.stdout);
    if (!ranges.length) continue;
    const content = gitShow(input.repoPath, input.headCommit, file);
    if (!content.ok) {
      return respond({ ok: false, error: `git show failed for ${file}: ${content.stderr || content.code}`, changedFunctions });
    }
    const sourceFile = ts.createSourceFile(file, content.stdout, ts.ScriptTarget.Latest, true);
    for (const fn of collectFunctions(ts, sourceFile)) {
      if (ranges.some((range) => intersects(range, { start: fn.startLine, end: fn.endLine }))) {
        changedFunctions.push({ ...fn, file, changeType: "modified" });
      }
    }
  }
  respond({ ok: true, changedFunctions });
}

main();
