/**
 * Turbine VS Code Extension — Phase 18
 *
 * Phase 18 additions on top of Phase 6 base:
 *  - Native diff editor (vscode.diff) for completed workers via "View Diff" button
 *  - Accept/Reject per worker: accept stages files via `git add`, reject reverts via `git checkout HEAD`
 *  - Click-to-navigate from a worker row to the first file it modified
 *  - Runs tree view (TreeDataProvider) showing past sessions and their runs
 *  - Inline diagnostics (squiggles) for files flagged in the repair loop
 */

import * as vscode from "vscode";
import * as cp from "child_process";
import * as path from "path";
import * as fs from "fs";

// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
  const diagCollection = vscode.languages.createDiagnosticCollection("turbine");
  context.subscriptions.push(diagCollection);

  const runsProvider = new RunsTreeDataProvider(context);
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("turbine.runsView", runsProvider)
  );

  const provider = new TurbineViewProvider(context, diagCollection, runsProvider);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      TurbineViewProvider.VIEW_ID,
      provider,
      { webviewOptions: { retainContextWhenHidden: true } }
    ),

    // Command palette shortcuts
    vscode.commands.registerCommand("turbine.run", () =>
      provider.submitFromCommand(false)
    ),
    vscode.commands.registerCommand("turbine.runDryRun", () =>
      provider.submitFromCommand(true)
    ),

    // Phase 18: open native VS Code diff editor for a worker's changes
    vscode.commands.registerCommand(
      "turbine.showWorkerDiff",
      (workerId: string, files: string[], target: string) =>
        showWorkerDiff(workerId, files, target)
    ),

    // Phase 18: accept a worker's changes (git add)
    vscode.commands.registerCommand(
      "turbine.acceptWorker",
      (files: string[], target: string) => stageWorkerFiles(files, target, true)
    ),

    // Phase 18: reject a worker's changes (git checkout HEAD --)
    vscode.commands.registerCommand(
      "turbine.rejectWorker",
      (files: string[], target: string) => stageWorkerFiles(files, target, false)
    ),

    // Phase 18: navigate to the first file a worker touched
    vscode.commands.registerCommand(
      "turbine.navigateToFile",
      (filePath: string) => navigateToFile(filePath)
    ),

    // Phase 18: clear all Turbine diagnostics
    vscode.commands.registerCommand("turbine.clearDiagnostics", () =>
      diagCollection.clear()
    ),

    // Phase 18: refresh the Runs tree view
    vscode.commands.registerCommand("turbine.refreshRuns", () =>
      runsProvider.refresh()
    ),
  );
}

export function deactivate(): void {}

// ---------------------------------------------------------------------------
// Phase 18: Native diff editor
// ---------------------------------------------------------------------------

async function showWorkerDiff(
  workerId: string,
  files: string[],
  target: string
): Promise<void> {
  if (!files || files.length === 0) {
    vscode.window.showInformationMessage(`Worker ${workerId}: no files modified.`);
    return;
  }

  // Open diffs for each modified file. For multiple files, open them all.
  for (const relPath of files) {
    const absPath = path.join(target, relPath);
    if (!fs.existsSync(absPath)) { continue; }

    // The file on disk IS the new version (Turbine already committed it).
    // HEAD~1 (or the git index) has the original. Use vscode.diff with
    // a git HEAD URI for the left side and the workspace file for the right.
    const rightUri  = vscode.Uri.file(absPath);

    // Try to get the pre-Turbine version via `git show HEAD:relPath`
    let originalContent: string | undefined;
    try {
      originalContent = await new Promise<string>((resolve, reject) => {
        cp.exec(
          `git show HEAD:${relPath}`,
          { cwd: target, encoding: "utf8" },
          (err, stdout) => err ? reject(err) : resolve(stdout)
        );
      });
    } catch {
      // File didn't exist at HEAD (new file) — diff against empty
      originalContent = "";
    }

    const leftUri = vscode.Uri.parse(
      `turbine-original:${encodeURIComponent(workerId)}/${encodeURIComponent(relPath)}`
    );

    // Register a simple content provider for the left side
    const disposable = vscode.workspace.registerTextDocumentContentProvider(
      "turbine-original",
      {
        provideTextDocumentContent: () => originalContent ?? "",
      }
    );

    await vscode.commands.executeCommand(
      "vscode.diff",
      leftUri,
      rightUri,
      `Turbine: ${relPath} (${workerId})`
    );

    // Dispose after a short delay so the document can be opened
    setTimeout(() => disposable.dispose(), 5000);
  }
}

// ---------------------------------------------------------------------------
// Phase 18: Accept / Reject worker files
// ---------------------------------------------------------------------------

async function stageWorkerFiles(
  files: string[],
  target: string,
  accept: boolean
): Promise<void> {
  if (!files || files.length === 0) { return; }

  if (accept) {
    // Stage all files
    const cmd = `git add ${files.map(f => `"${f}"`).join(" ")}`;
    cp.exec(cmd, { cwd: target }, (err) => {
      if (err) {
        vscode.window.showErrorMessage(`Turbine: git add failed — ${err.message}`);
      } else {
        vscode.window.showInformationMessage(
          `Turbine: ${files.length} file(s) staged.`
        );
      }
    });
  } else {
    // Revert files to HEAD
    const cmd = `git checkout HEAD -- ${files.map(f => `"${f}"`).join(" ")}`;
    cp.exec(cmd, { cwd: target }, (err) => {
      if (err) {
        vscode.window.showErrorMessage(`Turbine: git checkout failed — ${err.message}`);
      } else {
        vscode.window.showInformationMessage(
          `Turbine: ${files.length} file(s) reverted to HEAD.`
        );
      }
    });
  }
}

// ---------------------------------------------------------------------------
// Phase 18: Navigate to file
// ---------------------------------------------------------------------------

async function navigateToFile(filePath: string): Promise<void> {
  if (!filePath || !fs.existsSync(filePath)) {
    vscode.window.showWarningMessage(`Turbine: file not found — ${filePath}`);
    return;
  }
  const doc = await vscode.workspace.openTextDocument(filePath);
  await vscode.window.showTextDocument(doc, { preview: false });
}

// ---------------------------------------------------------------------------
// Phase 18: Runs tree view
// ---------------------------------------------------------------------------

class RunsTreeDataProvider implements vscode.TreeDataProvider<RunsTreeItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<RunsTreeItem | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  constructor(private readonly _context: vscode.ExtensionContext) {}

  refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: RunsTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: RunsTreeItem): RunsTreeItem[] {
    const sessions = this._context.globalState.get<SessionRecord[]>(
      "turbine.sessions", []
    );

    if (!element) {
      // Top level: sessions
      return sessions.map(s => {
        const label = (s.label || "Session").slice(0, 50);
        const d = new Date(s.ts).toLocaleDateString(undefined, {
          month: "short", day: "numeric",
        });
        const item = new RunsTreeItem(
          `${d} — ${label}`,
          s.runs.length > 0
            ? vscode.TreeItemCollapsibleState.Collapsed
            : vscode.TreeItemCollapsibleState.None,
          "session",
          s
        );
        item.description = `${s.runs.length} run${s.runs.length !== 1 ? "s" : ""}`;
        item.iconPath = new vscode.ThemeIcon("history");
        return item;
      });
    }

    if (element.type === "session" && element.session) {
      // Second level: runs within session
      return (element.session.runs || []).map(r => {
        const t = new Date(r.ts).toLocaleTimeString(undefined, {
          hour: "2-digit", minute: "2-digit"
        });
        const req = (r.request || "").slice(0, 50);
        const ok  = r.workersSucceeded === r.workersTotal;
        const item = new RunsTreeItem(
          req || "(no request)",
          vscode.TreeItemCollapsibleState.None,
          "run",
          undefined,
          r
        );
        item.description = `${t} · ${r.workersSucceeded}/${r.workersTotal} workers`;
        item.iconPath = new vscode.ThemeIcon(
          ok ? "pass" : "warning",
          ok
            ? new vscode.ThemeColor("testing.iconPassed")
            : new vscode.ThemeColor("testing.iconFailed")
        );
        item.tooltip = r.diagnosis || req;
        return item;
      });
    }

    return [];
  }
}

class RunsTreeItem extends vscode.TreeItem {
  constructor(
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState,
    public readonly type: "session" | "run",
    public readonly session?: SessionRecord,
    public readonly run?: RunRecord,
  ) {
    super(label, collapsibleState);
  }
}

// ---------------------------------------------------------------------------
// WebviewViewProvider
// ---------------------------------------------------------------------------

class TurbineViewProvider implements vscode.WebviewViewProvider {
  static readonly VIEW_ID = "turbine.sidebarView";

  private _view?: vscode.WebviewView;
  private _activeProc?: cp.ChildProcess;
  private _currentTarget: string = "";

  // Phase 21: inline change highlighting
  private readonly _changedDecType = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor("diffEditor.insertedLineBackground"),
    overviewRulerColor: new vscode.ThemeColor("editorGutter.addedBackground"),
    overviewRulerLane: vscode.OverviewRulerLane.Left,
  });
  private _runFiles: string[] = [];

  constructor(
    private readonly _context: vscode.ExtensionContext,
    private readonly _diagCollection: vscode.DiagnosticCollection,
    private readonly _runsProvider: RunsTreeDataProvider,
  ) {
    this._context.subscriptions.push(this._changedDecType);
  }

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _resolverContext: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this._view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this._context.extensionUri, "media"),
      ],
    };

    webviewView.webview.html = this._buildHtml(webviewView.webview);

    webviewView.webview.onDidReceiveMessage((msg: WebViewMessage) => {
      switch (msg.command) {
        case "run":
          this._runTurbine(msg.request, msg.dryRun ?? false, msg.newChat ?? false);
          break;
        case "cancel":
          this._cancel();
          break;
        case "ready": {
          const sessions = this._context.globalState.get<SessionRecord[]>("turbine.sessions", []);
          this._post({ event: "_turbine_init", sessions });
          break;
        }
        case "saveSessions": {
          const saveMsg = msg as unknown as SaveSessionsMessage;
          if (Array.isArray(saveMsg.sessions)) {
            this._context.globalState.update("turbine.sessions", saveMsg.sessions.slice(0, 30));
            this._runsProvider.refresh();
          }
          break;
        }
        // Phase 18: diff/accept/reject/navigate forwarded from WebView
        case "showDiff": {
          const m = msg as unknown as WorkerActionMessage;
          vscode.commands.executeCommand(
            "turbine.showWorkerDiff", m.workerId, m.files, this._currentTarget
          );
          break;
        }
        case "acceptWorker": {
          const m = msg as unknown as WorkerActionMessage;
          vscode.commands.executeCommand(
            "turbine.acceptWorker", m.files, this._currentTarget
          );
          break;
        }
        case "rejectWorker": {
          const m = msg as unknown as WorkerActionMessage;
          vscode.commands.executeCommand(
            "turbine.rejectWorker", m.files, this._currentTarget
          );
          break;
        }
        case "navigateFile": {
          const m = msg as unknown as NavigateFileMessage;
          if (m.file && this._currentTarget) {
            const abs = path.join(this._currentTarget, m.file);
            vscode.commands.executeCommand("turbine.navigateToFile", abs);
          }
          break;
        }
        // Phase 19: write the user's clarification answer to the process stdin
        case "clarificationAnswer": {
          if (this._activeProc?.stdin && msg.answer) {
            this._activeProc.stdin.write(msg.answer + "\n", "utf8");
          }
          break;
        }
      }
    });
  }

  async submitFromCommand(dryRun: boolean): Promise<void> {
    const request = await vscode.window.showInputBox({
      prompt: "Describe the change you want Turbine to make",
      placeHolder: "e.g. Add input validation to the login form",
      ignoreFocusOut: true,
    });
    if (request) {
      await vscode.commands.executeCommand(
        `${TurbineViewProvider.VIEW_ID}.focus`
      );
      this._runTurbine(request, dryRun, false);
    }
  }

  private _cancel(): void {
    if (this._activeProc) {
      this._activeProc.kill();
      this._activeProc = undefined;
      this._post({ event: "log", level: "error", message: "Cancelled by user." });
      this._post({ event: "_turbine_exit", code: -1 });
    }
  }

  private async _runTurbine(
    request: string,
    dryRun: boolean,
    newChat: boolean
  ): Promise<void> {
    const config = vscode.workspace.getConfiguration("turbine");
    const pythonPath: string = config.get("pythonPath", "");
    const extraArgs: string[] = config.get("extraArgs", []);

    let cmd: string;
    let baseArgs: string[];
    let target: string;

    const pythonPathExplicit = pythonPath && pythonPath !== "turbine";
    if (pythonPathExplicit) {
      const parts = pythonPath.split(" ");
      cmd = parts[0];
      baseArgs = parts.slice(1);
      const folders = vscode.workspace.workspaceFolders;
      if (!folders || folders.length === 0) {
        vscode.window.showErrorMessage("Turbine: No workspace folder is open.");
        return;
      }
      if (folders.length === 1) {
        target = folders[0].uri.fsPath;
      } else {
        const picked = await vscode.window.showQuickPick(
          folders.map(f => ({ label: f.name, description: f.uri.fsPath, folder: f })),
          { placeHolder: "Which folder should Turbine run on?" }
        );
        if (!picked) { return; }
        target = picked.folder.uri.fsPath;
      }
    } else {
      let resolvedPython = "";

      let dir = this._context.extensionUri.fsPath;
      for (let i = 0; i < 6; i++) {
        const win  = path.join(dir, ".venv", "Scripts", "python.exe");
        const unix = path.join(dir, ".venv", "bin", "python");
        if (fs.existsSync(win))  { resolvedPython = win;  break; }
        if (fs.existsSync(unix)) { resolvedPython = unix; break; }
        const parent = path.dirname(dir);
        if (parent === dir) { break; }
        dir = parent;
      }

      if (resolvedPython) {
        cmd = resolvedPython;
        baseArgs = ["-m", "turbine"];
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
          vscode.window.showErrorMessage("Turbine: No workspace folder is open.");
          return;
        }
        if (folders.length === 1) {
          target = folders[0].uri.fsPath;
        } else {
          const picked = await vscode.window.showQuickPick(
            folders.map(f => ({ label: f.name, description: f.uri.fsPath, folder: f })),
            { placeHolder: "Which folder should Turbine run on?" }
          );
          if (!picked) { return; }
          target = picked.folder.uri.fsPath;
        }
      } else {
        cmd = "turbine";
        baseArgs = [];
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
          vscode.window.showErrorMessage("Turbine: No workspace folder open and no .venv found.");
          return;
        }
        target = folders[0].uri.fsPath;
      }
    }

    // Phase 18: remember the target so commands (diff, accept, navigate) know the root
    this._currentTarget = target;

    const args = [
      ...baseArgs,
      target,
      request,
      "--json-events",
      "--no-ui",
      "--interactive",  // Phase 19: let the extension answer clarification prompts via stdin
      ...(dryRun ? ["--dry-run"] : []),
      ...(newChat ? ["--new-chat"] : []),
      ...extraArgs,
    ];

    if (this._activeProc) {
      this._activeProc.kill();
    }

    // Phase 18: clear old diagnostics at the start of each run
    this._diagCollection.clear();

    // Phase 21: reset file tracking and wipe previous change highlights
    this._runFiles = [];
    for (const ed of vscode.window.visibleTextEditors) {
      ed.setDecorations(this._changedDecType, []);
    }

    this._post({ event: "_turbine_start", target, request, dryRun });

    this._post({ event: "log", level: "debug", message: `extensionUri: ${this._context.extensionUri.fsPath}` });
    this._post({ event: "log", level: "debug", message: `target: ${target}` });
    this._post({ event: "log", level: "debug", message: `cmd: ${cmd}` });
    this._post({ event: "log", level: "debug", message: `args: ${args.join(" ")}` });

    const proc = cp.spawn(cmd, args, {
      cwd: target,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
      },
      shell: false,
    });
    this._activeProc = proc;

    let buffer = "";

    proc.stdout.on("data", (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      let idx: number;
      while ((idx = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (!line) { continue; }
        try {
          const ev = JSON.parse(line) as Record<string, unknown>;
          this._handleEvent(ev, target);
          this._post(ev);
        } catch {
          this._post({ event: "log", level: "debug", message: line });
        }
      }
    });

    proc.stderr.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8").trim();
      if (text) { this._post({ event: "log", level: "error", message: text }); }
    });

    proc.on("close", (code: number | null) => {
      if (buffer.trim()) {
        this._post({ event: "log", level: "debug", message: buffer.trim() });
      }
      this._post({ event: "_turbine_exit", code: code ?? -1 });
      this._activeProc = undefined;
    });

    proc.on("error", (err: Error) => {
      vscode.window.showErrorMessage(
        `Turbine: Failed to start process — ${err.message}`
      );
      this._post({ event: "log", level: "error", message: `Failed to start: ${err.message}` });
    });
  }

  /**
   * Phase 18: intercept events to apply IDE-side effects (diagnostics, tree refresh).
   * The event is ALSO forwarded to the WebView unchanged.
   */
  private _handleEvent(ev: Record<string, unknown>, target: string): void {
    // worker_repair → add diagnostic squiggles to flagged files
    if (ev["event"] === "worker_repair") {
      const files = (ev["files"] as string[] | undefined) || [];
      for (const relPath of files) {
        const absPath = path.join(target, relPath);
        if (!fs.existsSync(absPath)) { continue; }
        const uri = vscode.Uri.file(absPath);
        const diag = new vscode.Diagnostic(
          new vscode.Range(0, 0, 0, 0),
          `Turbine: worker ${ev["ticket_id"] ?? "?"} needs repair — tests failed in this file.`,
          vscode.DiagnosticSeverity.Warning
        );
        diag.source = "Turbine";
        const existing = this._diagCollection.get(uri) ?? [];
        this._diagCollection.set(uri, [...existing, diag]);
      }
    }

    // worker_done (success) → remove diagnostics; collect files for change highlighting
    if (ev["event"] === "worker_done" && ev["success"] === true) {
      const files = (ev["files"] as string[] | undefined) || [];
      for (const relPath of files) {
        const absPath = path.join(target, relPath);
        const uri = vscode.Uri.file(absPath);
        // Remove only the Turbine diagnostics for this file
        const remaining = (this._diagCollection.get(uri) ?? []).filter(
          d => d.source !== "Turbine"
        );
        if (remaining.length === 0) {
          this._diagCollection.delete(uri);
        } else {
          this._diagCollection.set(uri, remaining);
        }
        // Phase 21: track for change decoration
        if (!this._runFiles.includes(relPath)) {
          this._runFiles.push(relPath);
        }
      }
    }

    // Phase 21: apply change highlights once the run finishes (skip dry runs)
    if (ev["event"] === "done_detail" && !ev["dry_run"]) {
      void this._applyChangeDecorations();
    }
  }

  /** Phase 21: open changed files and mark added lines with a green highlight. */
  private async _applyChangeDecorations(): Promise<void> {
    if (this._runFiles.length === 0) { return; }

    for (const relPath of this._runFiles) {
      const absPath = path.join(this._currentTarget, relPath);
      if (!fs.existsSync(absPath)) { continue; }

      // Ask git for the per-file diff (no context lines) to find added ranges.
      let diffOutput: string;
      try {
        diffOutput = await new Promise<string>((resolve, reject) => {
          cp.exec(
            `git diff HEAD~1 HEAD -U0 -- "${relPath.replace(/"/g, '\\"')}"`,
            { cwd: this._currentTarget, encoding: "utf8" },
            (err, stdout) => err ? reject(err) : resolve(stdout)
          );
        });
      } catch {
        continue; // not a git repo, or no prior commit
      }

      const ranges = parseAddedRanges(diffOutput);
      if (ranges.length === 0) { continue; }

      // Open the file if not already visible; preserve focus on the panel.
      const fileUri = vscode.Uri.file(absPath);
      let editor = vscode.window.visibleTextEditors.find(
        e => e.document.uri.fsPath === absPath
      );
      if (!editor) {
        const doc = await vscode.workspace.openTextDocument(fileUri);
        editor = await vscode.window.showTextDocument(doc, {
          preserveFocus: true,
          preview: false,
        });
      }

      editor.setDecorations(this._changedDecType, ranges);
    }
  }

  private _post(event: Record<string, unknown>): void {
    this._view?.webview.postMessage(event);
  }

  private _buildHtml(webview: vscode.Webview): string {
    const mediaPath = vscode.Uri.joinPath(
      this._context.extensionUri, "media", "panel.html"
    );
    const nonce = generateNonce();
    let html = fs.readFileSync(mediaPath.fsPath, "utf8");
    html = html.replace(/\{\{NONCE\}\}/g, nonce);
    const mediaBase = webview.asWebviewUri(
      vscode.Uri.joinPath(this._context.extensionUri, "media")
    ).toString();
    html = html.replace(/\{\{MEDIA_BASE\}\}/g, mediaBase);
    return html;
  }
}

// ---------------------------------------------------------------------------
// Types / helpers
// ---------------------------------------------------------------------------

interface RunRecord {
  id: string;
  ts: number;
  request: string;
  dryRun: boolean;
  workersSucceeded: number;
  workersTotal: number;
  filesWritten: number;
  diffLines: number;
  diagnosis: string;
  tickets: { id: string; description: string }[];
}

interface SessionRecord {
  id: string;
  label: string;
  ts: number;
  runs: RunRecord[];
}

interface WebViewMessage {
  command: "run" | "cancel" | "ready" | "saveSessions"
        | "showDiff" | "acceptWorker" | "rejectWorker" | "navigateFile"
        | "clarificationAnswer";
  request: string;
  dryRun?: boolean;
  newChat?: boolean;
  answer?: string;  // Phase 19: clarification gate answer
}

interface SaveSessionsMessage {
  command: "saveSessions";
  sessions: SessionRecord[];
}

interface WorkerActionMessage {
  command: "showDiff" | "acceptWorker" | "rejectWorker";
  workerId: string;
  files: string[];
}

interface NavigateFileMessage {
  command: "navigateFile";
  file: string;
}

function generateNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let nonce = "";
  for (let i = 0; i < 32; i++) {
    nonce += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return nonce;
}

/**
 * Phase 21: parse `git diff -U0` output and return VS Code Ranges for every
 * hunk that adds lines in the new file.  Pure-deletion hunks (count == 0) are
 * skipped — there is nothing to highlight in that case.
 */
function parseAddedRanges(diffOutput: string): vscode.Range[] {
  const ranges: vscode.Range[] = [];
  const hunkRe = /^@@ [^+]*\+(\d+)(?:,(\d+))? @@/gm;
  let m: RegExpExecArray | null;
  while ((m = hunkRe.exec(diffOutput)) !== null) {
    const startLine = parseInt(m[1], 10) - 1;          // git is 1-based → 0-based
    const count     = m[2] !== undefined ? parseInt(m[2], 10) : 1;
    if (count === 0) { continue; }                     // pure deletion
    ranges.push(new vscode.Range(startLine, 0, startLine + count - 1, Number.MAX_SAFE_INTEGER));
  }
  return ranges;
}
